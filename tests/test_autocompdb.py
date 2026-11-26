import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from autocompdb.capture import entry_args, entry_location, read_database
from autocompdb.clangd import configure_clangd
from autocompdb.cli import main
from autocompdb.progress import report_wait
from autocompdb.providers import Model, Provider, choose_model, choose_provider, model_catalog, settings
from autocompdb.state import Error, Plan


def plan_response(**updates):
    return {"configure_args": [], "prerequisites": [], "targets": ["all"],
            "generated_globs": [], **updates}


def save_local_plan(source, build, response):
    plan = Plan.validate(source, build, response)
    plan.state.mkdir(parents=True, exist_ok=True)
    plan.save()


class RecordedProvider:
    name = "recorded"
    model = ""

    def __init__(self, answer=None, **by_task):
        self.answer = answer
        self.by_task = by_task
        self.calls = []

    def unavailable(self):
        return ""

    def request(self, task, context):
        self.calls.append((task, context))
        answer = self.by_task.get(task, self.answer)
        if callable(answer):
            return answer(context)
        return answer


class ProgressTest(unittest.TestCase):
    def test_wait_reports_elapsed_time_before_finishing(self):
        heartbeat = Event()
        messages = []

        def record(message, *, flush):
            messages.append((message, flush))
            if "still running" in message:
                heartbeat.set()

        with patch("autocompdb.progress.print", side_effect=record, create=True):
            with report_wait("Build analysis", interval=0.01):
                self.assertTrue(heartbeat.wait(2))
        self.assertIn("Build analysis still running", messages[0][0])
        self.assertIn("Build analysis finished", messages[-1][0])
        self.assertTrue(all(flush for _, flush in messages))


class ProvidersTest(unittest.TestCase):
    def test_cli_response_fixtures(self):
        envelopes = {
            "claude-cli": lambda value: {"is_error": False, "result": value},
            "agy-cli": lambda value: {"status": "SUCCESS", "response": value},
            "codex-cli": lambda value: value,
        }
        for name, envelope in envelopes.items():
            for mode in ("valid", "malformed", "failed"):
                with self.subTest(name=name, mode=mode):
                    def run(command, **options):
                        value = '{"targets":["all"]}' if mode == "valid" else "[malformed"
                        if name == "codex-cli" and mode != "failed":
                            output = command[command.index("--output-last-message") + 1]
                            Path(output).write_text(value)
                        return subprocess.CompletedProcess(command, int(mode == "failed"),
                                                           json.dumps(envelope(value)), "")
                    with patch("autocompdb.providers.shutil.which", return_value="/bin/provider"), \
                            patch("autocompdb.providers.subprocess.run", side_effect=run):
                        self.check_response(Provider(name, "example"), mode)

    def test_openrouter_response_fixtures(self):
        from urllib.error import HTTPError

        for mode in ("valid", "malformed", "failed"):
            with self.subTest(mode=mode):
                def open_response(request, timeout):
                    if mode == "failed":
                        raise HTTPError(request.full_url, 503, "Unavailable", {}, None)
                    value = '{"targets":["all"]}' if mode == "valid" else "[]"
                    return io.StringIO(json.dumps({"choices": [{"message": {"content": value}}]}))
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "fixture"}), \
                        patch("autocompdb.providers.urlopen", side_effect=open_response):
                    self.check_response(Provider("openrouter", "example"), mode)

    def check_response(self, provider, mode):
        if mode == "valid":
            self.assertEqual(provider.request("analyze", {}), {"targets": ["all"]})
        else:
            with self.assertRaises(Error):
                provider.request("analyze", {})



    def test_interactive_init_saves_the_llm_before_analyzing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / "build.log"
            log.write_text("gcc -c main.c -o main.o\n")
            config_home = root / "config"
            config = config_home / "autocompdb" / "config.toml"
            env = {"XDG_CONFIG_HOME": str(config_home), "OPENROUTER_API_KEY": "fixture-key"}
            catalog = {"models": [
                {"slug": "strong", "display_name": "Strong Model", "visibility": "list"},
                {"slug": "small", "display_name": "Small Model", "visibility": "list"},
                {"slug": "hidden", "display_name": "Hidden Model", "visibility": "hide"},
            ]}
            result = subprocess.CompletedProcess(["codex", "debug", "models"], 0, json.dumps(catalog), "")

            def request(provider, task, context):
                self.assertEqual((task, provider.name, provider.model), ("analyze", "codex-cli", "strong"))
                self.assertTrue(config.is_file())
                return plan_response()

            with patch.dict(os.environ, env, clear=True), \
                    patch("autocompdb.providers.sys.stdin.isatty", return_value=True), \
                    patch("autocompdb.providers.shutil.which", side_effect=lambda cmd: cmd if cmd == "codex" else None), \
                    patch("autocompdb.providers.subprocess.run", return_value=result) as query, \
                    patch("builtins.input", side_effect=["invalid", "", "nope", "1"]) as ask, \
                    patch.object(Provider, "request", autospec=True, side_effect=request), \
                    patch("autocompdb.cli.refresh_profile", return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                args = ["init", str(log), str(root), "--build", str(root / "output")]
                self.assertEqual(main(args), 0)
                self.assertEqual(ask.call_count, 4)
                self.assertEqual(settings(), Provider("codex-cli", "strong"))
                self.assertEqual(main(args), 0)
                self.assertEqual(ask.call_count, 4)
                query.assert_called_once()
                self.assertEqual(query.call_args.args[0], ["codex", "debug", "models"])
            self.assertIn("such as Claude Opus", output.getvalue())
            self.assertIn("Select a provider from the list.", output.getvalue())
            self.assertIn("Select a model from the list.", output.getvalue())
            self.assertIn("2. Small Model (small)", output.getvalue())
            self.assertNotIn("Hidden Model", output.getvalue())
            self.assertTrue(config.read_text().startswith("[llm]\n"))
            self.assertNotIn("fixture-key", config.read_text())

    def test_old_role_tables_need_one_llm_table(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "autocompdb" / "config.toml"
            config.parent.mkdir()
            config.write_text('[architect]\nprovider = "claude-cli"\n')
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": temp}, clear=True), \
                    self.assertRaisesRegex(Error, r"with one \[llm\] table"):
                settings()

    def test_openrouter_catalog_filters_models_without_json_mode(self):
        catalog = {"data": [
            {"id": "example/json", "name": "JSON Model", "supported_parameters": ["response_format"]},
            {"id": "example/text", "supported_parameters": []},
        ]}
        with patch("autocompdb.providers.urlopen", return_value=io.StringIO(json.dumps(catalog))) as query:
            self.assertEqual(model_catalog("openrouter"), [Model("example/json", "JSON Model")])
        self.assertEqual(query.call_args.args[0].full_url, "https://openrouter.ai/api/v1/models")

    def test_catalog_failure_allows_manual_model_id(self):
        with patch("autocompdb.providers.model_catalog", side_effect=OSError), \
                patch("builtins.input", return_value="custom"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(choose_model("LLM", "codex-cli", True, {}), "custom")

    def test_fzf_selects_provider_and_searches_the_full_model_catalog(self):
        models = [Model(f"model-{index}", f"Model {index}") for index in range(30)]

        def select(command, *, input, **options):
            self.assertEqual(command[0], "/usr/bin/fzf")
            lines = input.splitlines()
            if "provider" in command[-1]:
                selected = "codex-cli"
            else:
                self.assertEqual(len(lines), 31)
                self.assertIn("CLI default", lines)
                selected = "Model 29 (model-29)"
            self.assertIn(selected, lines)
            return subprocess.CompletedProcess(command, 0, f"{selected}\n")

        with patch.dict(os.environ, {}, clear=True), \
                patch("autocompdb.selection.shutil.which", return_value="/usr/bin/fzf"), \
                patch("autocompdb.selection.subprocess.run", side_effect=select), \
                patch("builtins.input", side_effect=AssertionError("fzf handles both selections")):
            self.assertEqual(choose_provider(["codex-cli"], {"codex-cli": models}),
                             Provider("codex-cli", "model-29"))

    def test_noninteractive_init_keeps_environment_settings_without_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            env = {"XDG_CONFIG_HOME": temp, "AUTOCOMPDB_PROVIDER": "agy-cli", "AUTOCOMPDB_MODEL": "chosen"}
            with patch.dict(os.environ, env, clear=True), \
                    patch("autocompdb.providers.sys.stdin.isatty", return_value=False), \
                    patch("builtins.input") as ask:
                self.assertEqual(settings(query=True), Provider("agy-cli", "chosen"))
                ask.assert_not_called()
            self.assertFalse((Path(temp) / "autocompdb" / "config.toml").exists())



class StateTest(unittest.TestCase):
    def test_conflicting_clangd_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".clangd"
            user = "CompileFlags: {CompilationDatabase: Ancestors}\n"
            path.write_text(user)
            plan = Plan.validate(root, root, plan_response())
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(configure_clangd(plan))
            self.assertEqual(path.read_text(), user)

    def test_in_tree_clangd_configuration_preserves_user_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".clangd"
            user = "Diagnostics:\n  UnusedIncludes: Strict\n"
            path.write_text(user)
            plan = Plan.validate(root, root, plan_response())
            self.assertFalse(configure_clangd(plan))
            content = path.read_text()
            self.assertTrue(content.startswith(user))
            self.assertEqual(content.count("CompilationDatabase:"), 1)
            self.assertFalse(configure_clangd(plan))
            self.assertEqual(path.read_text(), content)

    def test_init_validates_model_output_and_preserves_explicit_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "configure").write_text("#!/bin/sh\n")
            component = root / "variant"
            component.mkdir()
            makefile = component / "Makefile.am"
            makefile.write_text("generate: spec.y\n")
            log = root / "variant.log"
            log.write_text("./configure --enable-variant\n" + "noise\n" * 100
                           + "make[1]: Entering directory '/remote/build/variant'\n"
                           + "bison spec.y -o generated.c\ngcc -c generated.c -o generated.o\n")
            provider = RecordedProvider(plan_response())
            args = ["init", str(log), str(root), "--build", str(root / "build"), "--target", "all-ld"]
            with patch("autocompdb.cli.refresh_profile", return_value=0):
                self.assertEqual(main(args, lambda: provider), 0)
            self.assertEqual(Plan.load(root / "build").targets, ["all-ld"])
            self.assertEqual(len(provider.calls), 1)
            task, context = provider.calls[0]
            self.assertEqual(task, "analyze")
            excerpt = "\n".join(context["build_log_excerpt"])
            self.assertIn("./configure --enable-variant", excerpt)
            self.assertIn("bison spec.y -o generated.c", excerpt)
            self.assertIn(str(makefile), context["excerpts"])
            self.assertLess(len(context["build_log_excerpt"]), 40)
            provider.answer = plan_response(prerequisites=[{"directory": "..", "targets": ["all"]}])
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args, lambda: provider), 1)
            self.assertEqual(Plan.load(root / "build").targets, ["all-ld"])


    def test_init_requires_a_nonempty_log_before_calling_the_llm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def unexpected_provider():
                self.fail("The analysis requires a build log.")

            log = root / "build.log"
            args = ["init", str(log), str(root), "--build", str(root / "db")]
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as missing:
                    main(["init", str(log)], unexpected_provider)
                self.assertEqual(missing.exception.code, 2)
                self.assertEqual(main(args, unexpected_provider), 1)
                log.write_text("   \n")
                self.assertEqual(main(args, unexpected_provider), 1)


    def test_switch_preserves_a_legacy_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            default = Plan.validate(root, root, plan_response())
            default.state.mkdir()
            default.save()
            legacy = root / "compile_commands.json"
            content = json.dumps([{"directory": str(root), "file": "main.c",
                                   "arguments": ["gcc", "-c", "main.c"]}])
            legacy.write_text(content)
            named = Plan.validate(root, root, plan_response(targets=["all-ld"]))
            named.profile = "ld.bfd"
            named.state.mkdir(parents=True)
            named.save()
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["switch", "ld.bfd", "--build", str(root)]), 0)
                self.assertEqual(main(["switch", "default", "--build", str(root)]), 0)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.read_text(), content)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("gcc", "make", "clangd")),
                     "Requires GCC, GNU make and clangd.")
class BuildTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.source = root / "src"
        self.source.mkdir()
        self.build = root / "build"
        (self.source / "hello world.c").write_text("int hello(void) { return 1; }\n")
        compiler = shutil.which("gcc")
        configure = self.source / "configure"
        configure.write_text(
            "#!/bin/sh\nset -eu\n"
            "printf 'int main(void) { return 0; }\\n' > conftest.c\n"
            "gcc -c conftest.c -o conftest.o\nrm -f conftest.c conftest.o\n"
            "mkdir -p nested\n"
            "cat > Makefile <<'MAKE'\n"
            "all:\n\t$(MAKE) -C nested all\n"
            "fail:\n\tfalse\n"
            "MAKE\n"
            "cat > nested/Makefile <<'MAKE'\n"
            "all: normal.o second.o generated.o\n"
            "normal.o: ../../src/hello\\ world.c\n"
            "\tgcc '-DLABEL=a b' -c '../../src/hello world.c' -o normal.o\n"
            "second.o: ../../src/hello\\ world.c\n"
            "\tgcc -DSECOND -c '../../src/hello world.c' -o second.o\n"
            "generated.c:\n"
            "\tprintf 'int generated(void) { return 2; }\\n' > generated.c\n"
            "generated.o: generated.c\n"
            f"\tif true; then {shlex.quote(compiler)} -c generated.c -o generated.o; fi\n"
            "MAKE\n"
        )
        configure.chmod(0o755)
        self.local_plan = plan_response(generated_globs=["nested/generated.c"])

        def recorded_repairs(context):
            repairs = []
            for candidate in context["candidates"]:
                if candidate["text"].startswith("if true; then "):
                    repairs.append({
                        "lines": candidate["lines"], "directory": candidate["directory"],
                        "arguments": [compiler, "-c", "generated.c", "-o", "generated.o"],
                    })
            return {"repairs": repairs}

        self.llm = RecordedProvider(recorded_repairs)

    def run_cli(self, action, *flags):
        args = [action, "--build", str(self.build), *flags]
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            status = main(args, lambda: self.llm)
        return status, stdout.getvalue() + stderr.getvalue()

    def assert_status(self, expected, action, *flags):
        status, output = self.run_cli(action, *flags)
        self.assertEqual(status, expected, output)
        return output

    def test_capture_repair_and_verify(self):
        save_local_plan(self.source, self.build, self.local_plan)
        self.assert_status(0, "build", "--jobs", "2")
        database = self.build / "compile_commands.json"
        entries = read_database(database)
        locations = {entry_location(entry) for entry in entries}
        nested = self.build / "nested"
        source = self.source / "hello world.c"
        self.assertEqual(locations, {
            (nested, source, nested / "normal.o"), (nested, source, nested / "second.o"),
            (nested, nested / "generated.c", nested / "generated.o"),
        })
        self.assertTrue(any("-DLABEL=a b" in entry_args(entry) for entry in entries))
        self.assertTrue(self.llm.calls)
        original = database.read_bytes()
        calls = len(self.llm.calls)
        self.assert_status(0, "patch")
        self.assert_status(0, "build")
        self.assertEqual(len(self.llm.calls), calls)
        self.assertEqual(database.read_bytes(), original)
        output = self.assert_status(0, "verify")
        self.assertEqual(output.count("clangd parsed "), 2)

    def test_failed_build_preserves_log(self):
        save_local_plan(self.source, self.build, plan_response(targets=["fail"]))
        output = self.assert_status(1, "build")
        path = self.build / ".autobear" / "build.log"
        self.assertIn(str(path), output)
        self.assertIn("false", path.read_text())

    def test_llm_failure_preserves_capture_and_allows_retry(self):
        def fail(_):
            raise Error("LLM fixture failed.")

        reply = self.llm.answer
        self.llm.answer = fail
        save_local_plan(self.source, self.build, self.local_plan)
        self.assert_status(1, "build")
        database = self.build / "compile_commands.json"
        self.assertEqual(len(read_database(database)), 2)
        self.llm.answer = reply
        self.assert_status(0, "patch")
        self.assertEqual(len(read_database(database)), 3)


    def test_existing_in_tree_build_and_empty_incremental_capture(self):
        self.build = self.source
        (self.source / "configure").write_text("#!/bin/sh\nexit 1\n")
        (self.source / "Makefile").write_text(
            "all: hello.o\nhello.o: hello\\ world.c\n"
            "\tgcc -c 'hello world.c' -o hello.o\n"
        )
        save_local_plan(self.source, self.build, plan_response())
        self.assert_status(0, "build")
        database = self.build / "compile_commands.json"
        self.assertEqual(len(read_database(database)), 1)
        self.assert_status(0, "verify")
        database.resolve().unlink()
        (self.build / ".autobear" / "build.log").unlink()
        output = self.assert_status(0, "build")
        self.assertIn("use a fresh build directory", output)
        self.assertEqual(read_database(database), [])

class RefreshTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.build = root / "db"
        self.log = root / "remote.log"
        self.source = root / "src"
        self.source.mkdir()
        (self.source / "configure").write_text("#!/bin/sh\nexit 1\n")

    def invoke(self, command, get_llm=None):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            status = main([*command, "--build", str(self.build)], get_llm)
        self.output = out.getvalue()
        return status, err.getvalue()

    def test_reinit_keeps_saved_targets_and_recovers_quiet_commands(self):
        directory = "/remote/build/ld"
        configure = "Run: cd /remote/build; /bin/bash ../src/configure --enable-gold\n"
        commands = (
            f"make: Entering directory '{directory}'\n"
            "gcc -c ../../src/base.c -o base.o\n"
            "  CC       ld_bfd-shared.o\n"
            "  CC       ld_alt-shared.o\n"
        )
        self.log.write_text(f"first run: {configure}{commands}")
        init = ["init", str(self.log), str(self.source)]
        original = RecordedProvider(plan_response(configure_args=["--enable-gold"]))
        status, error = self.invoke(init, lambda: original)
        self.assertEqual(status, 1)
        self.assertIn("2 build steps without matching database outputs", error)

        def recover(context):
            repairs = []
            for candidate in context["candidates"]:
                hint = candidate["inference"]
                repairs.append({
                    "lines": candidate["lines"], "directory": candidate["directory"],
                    "arguments": [hint["compiler"], *hint["flags"], "-c", hint["source"], "-o", hint["output"]],
                })
            return {"repairs": repairs}

        llm = RecordedProvider(analyze=plan_response(
            configure_args=["--enable-gold"], targets=["all-ld"],
            quiet_builds={directory: {
                "compiler": "gcc", "flags": [], "source_prefix": "../../src",
                "sources": {name: "../../src/shared.c" for name in ("ld_bfd-shared.o", "ld_alt-shared.o")},
                "flags_by_output": {"ld_bfd-shared.o": ["-DBFD"], "ld_alt-shared.o": ["-DALT"]},
            }},
        ), repair=recover)
        self.assertEqual(self.invoke(init, lambda: llm), (0, ""))
        self.assertEqual(Plan.load(self.build).targets, ["all"])
        self.assertEqual(llm.calls[0][1]["targets"], ["all"])
        self.assertEqual(self.invoke(["switch", "ld_alt"]), (0, ""))

        def unexpected_provider():
            self.fail("The saved build plan can be reused without a provider.")

        self.assertEqual(self.invoke(init, unexpected_provider), (0, ""))
        self.assertIn("Reusing the saved plan", self.output)
        self.assertEqual(self.invoke(["switch", "--list"]), (0, ""))
        self.assertIn("* ld_alt\n", self.output)

        self.log.write_text(f"second run: {configure}{commands}")
        self.assertEqual(self.invoke(init, lambda: llm), (0, ""))
        task, context = llm.calls[-1]
        self.assertEqual((task, context["targets"]), ("analyze", ["all"]))
        self.log.write_text(f"third run: {configure.replace('--enable-gold', '--disable-gold')}{commands}")
        status, error = self.invoke([*init, "--profile", "default"], unexpected_provider)
        self.assertEqual(status, 1)
        self.assertIn("Use --profile with a new name", error)
        self.assertEqual(Plan.load(self.build, "default").log.read_text(), f"second run: {configure}{commands}")

    def test_init_names_new_configurations_from_the_analysis(self):
        init = ["init", str(self.log), str(self.source)]
        contents = {}
        for triplet, name in (("arm-elf", "arm-elf"), ("aarch64-elf", "aarch64-elf"),
                              ("riscv64-elf", "aarch64-elf.2")):
            log = (f"./configure --target={triplet}\n" + "noise\n" * 20
                   + f"checking target system type... {triplet}\n"
                   + f"gcc -DTRIPLET={triplet} -c main.c -o main.o\n" + "noise\n" * 20)
            self.log.write_text(log)
            contents[name] = log
            provider = RecordedProvider(plan_response(
                configure_args=[f"--target={triplet}"], profile_name=name.split(".")[0],
            ))
            self.assertEqual(self.invoke(init, lambda: provider), (0, ""))
            self.assertEqual(Plan.load(self.build).profile, name)
            self.assertIn(f"checking target system type... {triplet}",
                          "\n".join(provider.calls[0][1]["build_log_excerpt"]))

        self.log.write_text(contents["arm-elf"])

        def unexpected_provider():
            self.fail("An identical log should reuse its profile.")

        self.assertEqual(self.invoke(init, unexpected_provider), (0, ""))
        self.assertEqual(Plan.load(self.build).profile, "arm-elf")
        self.assertEqual(self.invoke(["switch", "--list"]), (0, ""))
        self.assertEqual(self.output, "  aarch64-elf\n  aarch64-elf.2\n* arm-elf\n")
        provider = RecordedProvider(plan_response(profile_name="ignored"))
        self.assertEqual(self.invoke([*init, "--profile", "manual"], lambda: provider), (0, ""))
        self.assertEqual(Plan.load(self.build).profile, "manual")

    def test_remote_log_parses_without_a_model_or_local_build(self):
        self.log.write_text(
            "make: Entering directory '/remote/job'\n"
            "make[1]: Entering directory '/remote/job/sub'\n"
            "cd deeper && gcc -c '../hello world.c' -o hello.o\n"
            "gcc -DTEST -c another.c -o another.o\n"
            "make[1]: Leaving directory '/remote/job/sub'\n"
            "gcc -c root.c -o root.o\n"
            "make: Leaving directory '/remote/job'\n"
        )

        def unexpected_provider():
            self.fail("A supported build log needs no model.")

        status, error = self.invoke(["refresh", str(self.log)], unexpected_provider)
        self.assertEqual(status, 0, error)
        database = self.build / "compile_commands.json"
        self.assertEqual({entry_location(e)[:2] for e in read_database(database)}, {
            (Path("/remote/job/sub/deeper"), Path("/remote/job/sub/hello world.c")),
            (Path("/remote/job/sub"), Path("/remote/job/sub/another.c")),
            (Path("/remote/job"), Path("/remote/job/root.c")),
        })
        self.assertFalse((self.build / "Makefile").exists())
        original = database.read_bytes()
        self.assertEqual(self.invoke(["refresh", str(self.log)], unexpected_provider)[0], 0)
        self.assertEqual(database.read_bytes(), original)

    def test_import_preserves_literal_arguments_and_skips_option_values_as_sources(self):
        self.log.write_text(
            "cd 'sub dir' && ccache arm-none-eabi-gcc -include prefix.c "
            "'-DLABEL=a;b && c' -c first.c second.c && mv first.Tpo first.Po\n"
        )

        def unexpected_provider():
            self.fail("Literal compiler commands need no model.")

        status, error = self.invoke(
            ["refresh", str(self.log), "--log-directory", "/remote/job"], unexpected_provider,
        )
        self.assertEqual(status, 0, error)
        entries = read_database(self.build / "compile_commands.json")
        self.assertEqual(len(entries), 2)
        for name, entry in zip(("first", "second"), entries):
            directory = Path("/remote/job/sub dir")
            self.assertEqual(entry_location(entry), (directory, directory / f"{name}.c", directory / f"{name}.o"))
            self.assertEqual(entry_args(entry), [
                "arm-none-eabi-gcc", "-include", "prefix.c", "-DLABEL=a;b && c", "-c", f"{name}.c",
            ])

    def test_import_recovers_libtool_commands_and_promoted_outputs(self):
        self.log.write_text(
            "/bin/bash ./libtool --mode=compile gcc -c plugin.c -o plugin.lo\n"
            "libtool: compile: gcc -fPIC -c plugin.c -o .libs/plugin.o\n"
            "libtool: compile: gcc -c plugin.c -o plugin.o >/dev/null 2>&1\n"
            "gcc -o doc/chw$$ -g -O2 \\\n"
            "  doc/chew.c && \\\n"
            "/bin/bash ../move-if-change doc/chw$$ doc/chew && touch doc/chew.stamp\n"
        )

        def unexpected_provider():
            self.fail("The log contains the compiler arguments and permanent outputs.")

        status, error = self.invoke(
            ["refresh", str(self.log), "--log-directory", "/remote/job"], unexpected_provider,
        )
        self.assertEqual(status, 0, error)
        entries = read_database(self.build / "compile_commands.json")
        self.assertEqual({entry_location(entry)[2] for entry in entries}, {
            Path("/remote/job/.libs/plugin.o"), Path("/remote/job/plugin.o"), Path("/remote/job/doc/chew"),
        })
        command = next(entry_args(entry) for entry in entries if entry["file"].endswith("/doc/chew.c"))
        self.assertEqual(command, ["gcc", "-o", "doc/chew", "-g", "-O2", "doc/chew.c"])

    def test_refresh_reports_abbreviated_linker_steps_without_calling_a_model(self):
        self.log.write_text(
            "make: Entering directory '/remote/build/ld'\n"
            "gcc -c covered.c\n"
            "  CC       covered.o\n"
            "  CC       ldmain.o\n"
            "  CC       eelf_x86_64.o\n"
            "  CCLD     ld-new\n"
            "make: Leaving directory '/remote/build/ld'\n"
        )

        def unexpected_provider():
            self.fail("Abbreviated status lines cannot be sent to the LLM.")

        status, error = self.invoke(["refresh", str(self.log)], unexpected_provider)
        self.assertEqual(status, 1)
        self.assertIn("2 build steps without matching database outputs", error)
        self.assertIn("2 in /remote/build/ld", error)
        self.assertIn("Run init with the source tree", error)
        entry, = read_database(self.build / "compile_commands.json")
        self.assertEqual(entry_location(entry)[1], Path("/remote/build/ld/covered.c"))
        self.assertEqual(self.invoke(["patch"], unexpected_provider)[0], 1)

    def test_quiet_linker_fallback_uses_analysis_hints_and_yields_to_the_verbose_log(self):
        directory = "/remote/build/ld"
        (self.source / "ld").mkdir()
        (self.source / "ld/ldmain.c").write_text("int main(void) { return 0; }\n")
        (self.source / "ld/Makefile.am").write_text("AM_CPPFLAGS = -I. -DHAVE_CONFIG_H\n")
        self.log.write_text(
            f"make: Entering directory '{directory}'\n"
            "  CC       ldmain.o\n"
            "  GEN      eelf_x86_64.c\n"
            "  CC       eelf_x86_64.o\n"
        )

        def annotate(context):
            evidence = context["abbreviated_builds"][directory]
            self.assertEqual(evidence["object_files"], ["ldmain.o", "eelf_x86_64.o"])
            self.assertIn("ldmain.c", evidence["source_files"])
            self.assertIn("AM_CPPFLAGS", str(evidence["rules"]))
            return plan_response(quiet_builds={directory: {
                "compiler": "gcc", "flags": ["-DHAVE_CONFIG_H", "-I."],
                "source_prefix": "../../src/ld",
                "sources": {"eelf_x86_64.o": "eelf_x86_64.c"},
            }})

        def repair_log(context):
            result = []
            for item in context["candidates"]:
                hint = item["inference"]
                result.append({
                    "lines": item["lines"], "directory": item["directory"],
                    "arguments": [hint["compiler"], *hint["flags"], "-c", hint["source"],
                                  "-o", hint["output"]],
                })
            return {"repairs": result}

        llm = RecordedProvider(analyze=annotate, repair=repair_log)
        status, error = self.invoke(["init", str(self.log), str(self.source)], lambda: llm)
        self.assertEqual(status, 0, error)
        self.assertIn("Added 2 inferred compilation commands", self.output)
        plan = Plan.load(self.build)
        entries = read_database(plan.database)
        self.assertEqual({entry_location(e)[1].name for e in entries}, {"ldmain.c", "eelf_x86_64.c"})
        records = json.loads((plan.state / "repairs.json").read_text())
        self.assertTrue(all(record["inferred"] for record in records))

        def no_model():
            self.fail("A verbose command supersedes the inferred entry without a model.")

        self.log.write_text(
            f"make: Entering directory '{directory}'\n"
            "gcc -DVERBOSE -c ../../src/ld/ldmain.c -o ldmain.o\n"
        )
        status, error = self.invoke(["refresh", str(self.log)], no_model)
        self.assertEqual(status, 0, error)
        entries = read_database(plan.database)
        self.assertEqual(len(entries), 2)
        ldmain, = (entry for entry in entries if entry_location(entry)[1].name == "ldmain.c")
        self.assertIn("-DVERBOSE", entry_args(ldmain))
        self.assertNotIn("-DHAVE_CONFIG_H", entry_args(ldmain))
        self.assertIn("Replaced 1 inferred commands", self.output)

    def test_init_creates_target_views_after_quiet_command_recovery(self):
        directory = "/remote/build/ld"
        self.log.write_text(
            f"make: Entering directory '{directory}'\n"
            "  CC       ld_bfd-shared.o\n"
            "  CC       ld_alt-shared.o\n"
        )
        def recover(context):
            repairs = []
            for candidate in context["candidates"]:
                hint = candidate["inference"]
                repairs.append({
                    "lines": candidate["lines"], "directory": candidate["directory"],
                    "arguments": [hint["compiler"], *hint["flags"], "-c", hint["source"], "-o", hint["output"]],
                })
            return {"repairs": repairs}

        llm = RecordedProvider(analyze=plan_response(quiet_builds={directory: {
            "compiler": "gcc", "flags": [], "source_prefix": "../../src",
            "sources": {name: "../../src/shared.c" for name in ("ld_bfd-shared.o", "ld_alt-shared.o")},
            "flags_by_output": {"ld_bfd-shared.o": ["-DTARGET=1"], "ld_alt-shared.o": ["-DTARGET=2"]},
        }}), repair=recover)
        self.assertEqual(self.invoke(["init", str(self.log), str(self.source)], lambda: llm), (0, ""))
        self.assertEqual(self.invoke(["switch", "ld_alt"]), (0, ""))
        database = self.build / "compile_commands.json"
        self.assertIn("-DTARGET=2", entry_args(read_database(database)[0]))
        self.log.write_text(
            f"make: Entering directory '{directory}'\n"
            "gcc -DTARGET=1 -c ../../src/shared.c -o ld_alt-shared.o\n"
        )
        self.assertEqual(self.invoke(["refresh", str(self.log)]), (0, ""))
        self.assertEqual(self.invoke(["switch", "--list"]), (0, ""))
        self.assertEqual(self.output, "* default\n")

    def test_init_analyzes_and_repairs_with_one_llm(self):
        self.log.write_text(
            "gcc -c base.c -o base.o\n"
            "if true; then gcc -c generated.c -o generated.o; fi\n"
        )

        def repair_log(context):
            candidate, = context["candidates"]
            return {"repairs": [{
                "lines": candidate["lines"], "directory": candidate["directory"],
                "arguments": ["gcc", "-c", "generated.c", "-o", "generated.o"],
            }]}

        llm = RecordedProvider(analyze=plan_response(
            configure_args=["--target=arm-elf"],
            prerequisites=[{"directory": ".", "targets": ["generate"]}],
            generated_globs=["generated.c"],
        ), repair=repair_log)

        with patch("autocompdb.cli.providers.settings", return_value=llm) as settings_for:
            status, error = self.invoke([
                "init", str(self.log), str(self.source), "--log-directory", "/remote/job",
            ])
        self.assertEqual(status, 0, error)
        self.assertEqual([call.kwargs for call in settings_for.call_args_list], [{"query": True}] * 2)
        self.assertEqual([task for task, _ in llm.calls], ["analyze", "repair"])
        for message in (
            "This can take a few minutes.", "Make targets all", "Configure options --target=arm-elf",
            "Prerequisite in .", "Generated file patterns generated.c", "Parsed 1 compilation commands.",
            "Repair batch 1/1 repaired 1 log commands.", "with 2 compilation commands (2 added).",
        ):
            self.assertIn(message, self.output)
        entries = read_database(self.build / "compile_commands.json")
        self.assertEqual({entry_location(entry)[1] for entry in entries}, {
            Path("/remote/job/base.c"), Path("/remote/job/generated.c"),
        })

    def test_refresh_repairs_an_imported_log_without_new_analysis(self):
        prerequisite = {"directory": ".", "targets": ["generate"]}

        def recover(context):
            self.assertEqual(context["project"]["prerequisites"], [prerequisite])
            self.assertEqual(context["project"]["generated_globs"], ["gen/*.c"])
            candidate, = context["candidates"]
            return {"repairs": [{
                "lines": candidate["lines"], "directory": candidate["directory"],
                "arguments": ["gcc", "-c", "gen/a.c", "-o", "a.o"],
            }]}

        llm = RecordedProvider(analyze=plan_response(prerequisites=[prerequisite], generated_globs=["gen/*.c"]),
                               repair=recover)
        self.log.write_text("gcc -c base.c -o base.o\n")
        status, error = self.invoke(
            ["init", str(self.log), str(self.source), "--log-directory", "/remote/job"], lambda: llm,
        )
        self.assertEqual(status, 0, error)
        self.log.write_text("gcc -c `test -f 'gen/a.c' || echo '/source/'`gen/a.c -o a.o\n")
        args = ["refresh", str(self.log), "--log-directory", "/remote/job"]
        with tempfile.TemporaryDirectory() as config_home, \
                patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home, "OPENROUTER_API_KEY": "",
                                        "AUTOCOMPDB_PROVIDER": "openrouter"}):
            status, error = self.invoke(args)
        self.assertEqual(status, 1)
        self.assertIn("OpenRouter needs a model", error)
        status, error = self.invoke(args, lambda: llm)
        self.assertEqual(status, 0, error)
        self.assertEqual([task for task, _ in llm.calls], ["analyze", "repair"])
        entries = read_database(self.build / "compile_commands.json")
        self.assertEqual({entry_location(entry)[1] for entry in entries}, {
            Path("/remote/job/base.c"), Path("/remote/job/gen/a.c"),
        })


    def test_switch_profiles_selects_independent_logs_and_databases(self):
        host = "--host=x86_64-pc-linux-gnu"
        profile_targets = {"ld-x86_64-arm-elf": "arm-elf", "ld-x86_64-riscv64-elf": "riscv64-elf"}
        databases = {}
        logs = {}
        for profile, target in profile_targets.items():
            configure_target = f"--target={target}"
            provider = RecordedProvider(plan_response(
                configure_args=[host, configure_target], targets=["all-ld"],
            ))
            self.log.write_text(
                f"./configure {host} {configure_target}\n"
                f"make: Entering directory '/remote/{target}/ld'\n"
                f"gcc -DPROFILE_{target.split('-')[0]} -c ldmain.c -o ldmain.o\n"
            )
            status, error = self.invoke(
                ["init", str(self.log), str(self.source), "--profile", profile],
                lambda: provider,
            )
            self.assertEqual(status, 0, error)
            database = self.build / "compile_commands.json"
            databases[profile] = database.read_bytes()
            logs[profile] = self.log.read_bytes()
            self.assertEqual(len(read_database(database)), 1)
            self.assertEqual(len(provider.calls), 1)
        first, second = profile_targets
        self.assertNotEqual(databases[first], databases[second])

        def unexpected_provider():
            self.fail("Switching profiles requires no LLM.")

        status, error = self.invoke(["switch", first], unexpected_provider)
        self.assertEqual(status, 0, error)
        self.assertEqual(database.read_bytes(), databases[first])
        self.assertEqual(Plan.load(self.build).log.read_bytes(), logs[first])
        with contextlib.redirect_stdout(io.StringIO()) as listing:
            status = main(["switch", "--list", "--build", str(self.build)], unexpected_provider)
        self.assertEqual(status, 0)
        self.assertEqual(listing.getvalue(), f"* {first}\n  {second}\n")
        self.assertEqual(self.invoke(["switch", "unknown"], unexpected_provider)[0], 1)
        self.assertEqual(database.read_bytes(), databases[first])
        self.assertEqual(self.invoke(["switch", second], unexpected_provider)[0], 0)
        self.assertEqual(database.read_bytes(), databases[second])

    def test_init_creates_target_views_for_conflicting_commands(self):
        shared = self.source / "shared.c"
        shared.write_text("int shared(void) { return TARGET; }\n")
        (self.source / "unique.c").write_text("int unique(void) { return 0; }\n")
        (self.source / "other.c").write_text("int other(void) { return 0; }\n")
        self.log.write_text(
            f"make: Entering directory '{self.source}'\n"
            f"gcc -DTARGET=1 -MD -MF first.d -c {shared} -o ld_bfd-shared.o\n"
            f"gcc -DTARGET=2 -MD -MF second.d -c {shared} -o ld_alt-shared.o\n"
            f"gcc -c {self.source / 'unique.c'} -o unique.o\n"
            "gcc -MD -MF first.d -MT first.o -c other.c -o first.o\n"
            "gcc -MD -MF second.d -MT second.o -c other.c -o second.o\n"
        )
        provider = RecordedProvider(plan_response())
        init = ["init", str(self.log), str(self.source)]
        self.assertEqual(self.invoke(init, lambda: provider), (0, ""))
        self.assertEqual(self.invoke(["switch", "--list"]), (0, ""))
        self.assertEqual([line.split()[1 if line.startswith("*") else 0] for line in self.output.splitlines()],
                         ["default", "ld_alt", "ld_bfd"])
        database = self.build / "compile_commands.json"
        original = read_database(database)
        self.assertEqual(len(original), 5)
        for profile, flag in (("ld_bfd", "-DTARGET=1"), ("ld_alt", "-DTARGET=2")):
            self.assertEqual(self.invoke(["switch", profile]), (0, ""))
            entries = read_database(database)
            self.assertCountEqual([entry_args(entry) for entry in entries],
                                  [entry_args(entry) for entry in original])
            self.assertIn(flag, entry_args(next(entry for entry in entries if entry_location(entry)[1] == shared)))
            if clangd := shutil.which("clangd"):
                result = subprocess.run(
                    [clangd, f"--check={shared}", f"--compile-commands-dir={self.build}"],
                    capture_output=True, text=True, check=True,
                )
                command = next(line for line in result.stderr.splitlines() if "Compile command from CDB" in line)
                self.assertIn(flag, command)
        self.assertEqual(self.invoke(["refresh", str(self.log)]), (0, ""))
        self.assertEqual(self.invoke(["patch"]), (0, ""))
        if shutil.which("clangd"):
            self.assertEqual(self.invoke(["verify"]), (0, ""))
        self.assertEqual(self.invoke(["switch", "--list"]), (0, ""))
        self.assertIn("* ld_alt\n", self.output)
        status, error = self.invoke([*init, "--profile", "ld_alt"])
        self.assertEqual(status, 1)
        self.assertIn("generated from default", error)
