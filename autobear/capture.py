import contextlib
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .progress import report_wait
from .state import Error, fields, read_json, strings, write_json

BEAR_CONFIG = """schema: "4.2"
intercept:
  mode: wrapper
duplicates:
  match_on: [directory, file, arguments, output]
format:
  paths:
    directory: absolute
    file: absolute
"""
COMPILER_ENV = {"CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "CL", "_CL_"}
SOURCE_SUFFIXES = {".c", ".C", ".cc", ".cp", ".cpp", ".cxx", ".c++", ".C++", ".m", ".mm", ".i", ".ii"}
DIRECTORY = re.compile(r"^make(?:\[\d+\])?: (Entering|Leaving) directory [`'](.*)'$")
STAGE = "autobear directory "
ABBREVIATED_COMPILE = re.compile(r"^[ \t]+(?:CC|CXX)[ \t]{2,}(.+\.(?:o|lo|obj))[ \t]*$")
LIBTOOL = re.compile(r"^libtool: compile:\s*")
LIBTOOL_WRAPPER = re.compile(r"(?:^|\s)\S*[/]libtool\s.*--mode=compile(?:\s|$)")
SHELL_WORD = r"""(?:'[^']*'|"[^"$`]*"|[^\s'"`;&|$]+)"""
AUTOMAKE_SOURCE = re.compile(
    rf"`test -f ({SHELL_WORD}) \|\| echo ({SHELL_WORD})`({SHELL_WORD})"
)
DEPBASE = re.compile(
    r"^depbase=`echo ([A-Za-z0-9_./+-]+) \| "
    + re.escape(r"sed 's|[^/]*$|.deps/&|;s|\.o$||'`;")
)
READABLE_BRANCH = re.compile(rf"^if \[ -r ({SHELL_WORD}) \]; then (.*?);\s*else (.*?);\s*fi$")
NONEMPTY_BRANCH = re.compile(r"^if \[ (x[^;]*) != x \]; then (.*?);\s*else true; fi$")
DRIVER = re.compile(r"(?:[\w.+-]+-)?(?:cc|c\+\+|gcc|g\+\+|clang|clang\+\+)(?:-[\d.]+)?")
COMPILING = re.compile(r"(?:^|\s)-(?:c|S|fsyntax-only)(?:\s|$)")


def bear_command(plan):
    executable = shutil.which("bear")
    if not executable:
        raise Error("Install Bear 4.2.4 and add it to PATH.")
    version = subprocess.run([executable, "--version"], check=True, text=True, capture_output=True)
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version.stdout)
    if not match or tuple(map(int, match.groups())) < (4, 2, 4) or match[1] != "4":
        raise Error(f"Expected Bear 4.2.4 or a compatible Bear 4 release. Found {version.stdout.strip()}.")
    return [executable, "--config", str(plan.state / "bear.yml")]


@contextlib.contextmanager
def lock(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def absolute(directory, value):
    return (Path(directory) / value).resolve()


def entry_args(entry):
    if "arguments" in entry:
        return strings(entry["arguments"], "compilation arguments", allow_empty=True)
    return shlex.split(entry["command"])


def output_arg(args):
    for i, arg in enumerate(args[:-1]):
        if arg == "-o":
            return args[i + 1]
    return next((arg[2:] for arg in args if arg.startswith("-o") and len(arg) > 2), "")


def entry_location(entry):
    directory = Path(entry["directory"]).resolve()
    source = absolute(directory, entry["file"])
    output = entry.get("output") or output_arg(entry_args(entry))
    return directory, source, absolute(directory, output) if output else None


def entry_key(entry):
    directory, source, output = entry_location(entry)
    driver, *args = entry_args(entry)
    if "/" not in driver:
        driver = shutil.which(driver) or driver
    if "/" in driver:
        driver = os.path.normpath(directory / driver)
    normalized = []
    for index, arg in enumerate(args):
        if arg.startswith("-o") and len(arg) > 2 and output and absolute(directory, arg[2:]) == output:
            normalized.extend(("-o", str(output)))
        elif index and args[index - 1] == "-o" and output:
            normalized.append(str(output))
        elif not arg.startswith("-") and Path(arg).suffix in SOURCE_SUFFIXES and absolute(directory, arg) == source:
            normalized.append(str(source))
        else:
            normalized.append(arg)
    return directory, source, output, driver, tuple(normalized)


def read_database(path):
    if not path.exists():
        return []
    entries = read_json(path)
    if not isinstance(entries, list):
        raise Error(f"Expected a compilation database array in {path}.")
    try:
        for entry in entries:
            entry_key(entry)
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        raise Error(f"Invalid compilation database entry in {path}.") from exc
    return entries


def merge(entries, additions):
    seen = {entry_key(entry) for entry in entries}
    for entry in additions:
        key = entry_key(entry)
        if key not in seen:
            seen.add(key)
            entries.append(entry)


def is_probe(path):
    return path.stem == "conftest" or path.name.startswith(("conftest.", "cmTC_"))


def show_command(command):
    print(f"\033[1m{shlex.join(map(str, command))}\033[0m", flush=True)


def run_stage(command, cwd, log_path):
    show_command(command)
    with log_path.open("a", encoding="utf-8") as log:
        print(f"{STAGE}{cwd}", file=log)
        print(f"$ {shlex.join(map(str, command))}", file=log, flush=True)
        env = dict(os.environ, LC_ALL="C")
        with subprocess.Popen(command, cwd=cwd, env=env, text=True, errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
            for line in proc.stdout:
                print(line, end="")
                log.write(line)
            print(file=log)
    return proc.returncode


def capture(plan, jobs):
    bear = bear_command(plan)
    entries = read_database(plan.database)
    with tempfile.TemporaryDirectory(dir=plan.state) as temp:
        output = Path(temp) / "capture.json"
        prefix = [*bear, "--output", str(output), "--"]
        if not any((plan.build / name).is_file() for name in ("GNUmakefile", "makefile", "Makefile")):
            config_status = plan.build / "config.status"
            command = ([str(config_status)] if config_status.is_file()
                       else [str(plan.source / "configure"), *plan.configure_args])
            log = plan.state / "configure.log"
            status = run_stage([*prefix, *command], plan.build, log)
            if status:
                raise Error(f"{shlex.join(command)} exited with status {status}. See {log}.")
            output.unlink(missing_ok=True)
        make = ["make", "--print-directory", "--output-sync=recurse"]
        if jobs:
            make.append(f"--jobs={jobs}")
        steps = [*plan.prerequisites, {"directory": ".", "targets": plan.targets}]
        for step in steps:
            cwd = absolute(plan.build, step["directory"])
            command = [*make, "V=1", "--", *step["targets"]]
            output.unlink(missing_ok=True)
            status = run_stage([*prefix, *command], cwd, plan.log)
            additions = [entry for entry in read_database(output)
                         if not is_probe(entry_location(entry)[1])]
            merge(entries, additions)
            write_json(plan.database, entries)
            if status:
                raise Error(f"{shlex.join(command)} exited with status {status} in {cwd}. See {plan.log}.")
    if not entries:
        print("Bear captured zero compilations. If make was up to date, use a fresh build directory.")
    return bear


@dataclass(frozen=True)
class Candidate:
    lines: tuple
    directory: Path
    text: str
    output: str | None = None

    def payload(self):
        return {"lines": list(self.lines), "directory": str(self.directory), "text": self.text}


def expand_make_recipe(command, directory, *, inspect_files=True):
    readable = READABLE_BRANCH.fullmatch(command)
    if readable and inspect_files:
        names = shell_words(readable[1])
        if len(names) == 1:
            return readable[2 if os.access(directory / names[0], os.R_OK) else 3].strip()

    branch = NONEMPTY_BRANCH.fullmatch(command)
    if branch and not any(char in branch[1] for char in "$`\\"):
        condition = shell_words(branch[1])
        if len(condition) == 1:
            command = branch[2].strip() if condition[0] != "x" else ""

    assignment = DEPBASE.match(command)
    if assignment:
        obj = Path(assignment[1])
        depbase = obj.parent / ".deps" / obj.name.removesuffix(".o")
        command = re.sub(r"\$depbase(?=\.(?:Tpo|Po)\b)", str(depbase), command[assignment.end():]).lstrip()

    if not inspect_files:
        return command
    return expand_automake_source(command, lambda path: (directory / path).is_file())


def expand_automake_source(command, use_local):
    def source_word(match):
        local, prefix, file = (shell_words(word) for word in match.groups())
        if all(len(part) == 1 for part in (local, prefix, file)) and local == file:
            selected = file[0] if use_local(local[0]) else prefix[0] + file[0]
            return shlex.quote(selected)
        return match[0]

    return AUTOMAKE_SOURCE.sub(source_word, command)


def scan_log(plan, *, path=None):
    inspect_files = plan.log_directory is None and path is None
    path = path if path is not None else plan.log
    stack = [plan.log_directory or plan.build]
    pending = []
    numbers = []
    candidates = []
    abbreviated = []
    libtool_outputs = set()
    if not path.is_file():
        return candidates, abbreviated
    with path.open(errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            line = line.rstrip("\r\n")
            if line.startswith(STAGE):
                stack = [Path(line.removeprefix(STAGE))]
                pending.clear()
                numbers.clear()
                continue
            marker = DIRECTORY.fullmatch(line)
            if marker:
                action, path = marker.groups()
                directory = (absolute(stack[-1], path) if inspect_files
                             else Path(os.path.normpath(stack[-1] / path)))
                if action == "Entering":
                    stack.append(directory)
                elif len(stack) > 1 and directory == stack[-1]:
                    stack.pop()
                continue
            if line.startswith("$ "):
                continue
            abbreviated_compile = ABBREVIATED_COMPILE.fullmatch(line)
            if abbreviated_compile:
                abbreviated.append(Candidate((number,), stack[-1], line.strip(), abbreviated_compile[1]))
                continue
            pending.append(line.removesuffix("\\") if line.endswith("\\") else line)
            numbers.append(number)
            if line.endswith("\\"):
                continue
            text = "".join(pending).strip()
            command = LIBTOOL.sub("", text)
            libtool_compile = text != command
            command = expand_make_recipe(command, stack[-1], inspect_files=inspect_files)
            if is_compiler_recipe(command, stack[-1]) and not re.search(r"\bconftest\b", command):
                candidates.append(Candidate(tuple(numbers), stack[-1], command))
                output = output_arg(shell_words(command))
                if libtool_compile and output:
                    libtool_outputs.add((stack[-1], Path(output).stem))
            pending.clear()
            numbers.clear()
    candidates = [
        candidate for candidate in candidates
        if not LIBTOOL_WRAPPER.search(candidate.text)
        or (candidate.directory, Path(output_arg(shell_words(candidate.text))).stem)
        not in libtool_outputs
    ]
    return candidates, abbreviated


def source_component(source, directory):
    nested = source / directory.parent.name / directory.name
    component = nested if nested.is_dir() else source / directory.name
    return component if component.is_dir() and component.resolve().is_relative_to(source) else source


def quiet_evidence(source, abbreviated):
    by_directory = defaultdict(list)
    for item in abbreviated:
        by_directory[str(item.directory)].append(item.output)
    evidence = {}
    for directory, outputs in by_directory.items():
        component = source_component(source, Path(directory))
        files = sorted(entry.name for entry in component.iterdir() if entry.suffix in SOURCE_SUFFIXES)
        rules = {}
        template = component / "Makefile.am"
        makefiles = (component / "Makefile", template if template.is_file() else component / "Makefile.in")
        for makefile in makefiles:
            if not makefile.is_file() or not makefile.resolve().is_relative_to(source):
                continue
            excerpts = []
            continued = False
            with makefile.open(errors="replace") as stream:
                for line in stream:
                    if len(excerpts) >= 36:
                        break
                    if continued or re.search(r"(?:_SOURCES|FLAGS|^\s*(?:CC|CXX)\s*=|\.o:)", line):
                        excerpts.append(line[:240].rstrip())
                        continued = line.rstrip().endswith("\\")
            rules[str(makefile)] = excerpts
        evidence[directory] = {"object_files": outputs, "source_files": files[:150], "rules": rules}
    return evidence


def shell_words(text):
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def is_compiler_recipe(command, directory):
    if COMPILING.search(command):
        return True
    prefix = command.split(maxsplit=1)
    if not prefix or not DRIVER.fullmatch(Path(prefix[0]).name):
        return False
    words = shell_words(command)
    return not {"-E", "-M", "-MM"} & set(words) and bool(mentioned_sources(words[1:], directory))


def mentioned_sources(args, directory):
    return {absolute(directory, word) for word in args
            if not word.startswith("-") and Path(word).suffix in SOURCE_SUFFIXES
            and not is_probe(Path(word))}


def parse_shell(plan, bear, text, cwd):
    with tempfile.TemporaryDirectory(dir=plan.state) as temp:
        output = Path(temp) / "parsed.json"
        command = [*bear, "parse-sh", "--directory", str(cwd), "--output", str(output)]
        env = None
        if plan.log_directory is not None:
            env = {key: value for key, value in os.environ.items() if key not in COMPILER_ENV}
        result = subprocess.run(command, input=text, text=True, capture_output=True,
                                cwd=plan.build, env=env)
        if result.returncode and not output.is_file():
            return []
        return read_database(output)


def match_key(entry):
    directory, source, output, driver, args = entry_key(entry)
    # Bear's wrapper can record an absolute driver while parse-sh records its name.
    return directory, source, output, Path(driver).name, args


def parse_candidates(plan, bear, candidates):
    groups = defaultdict(list)
    for candidate in candidates:
        groups[candidate.directory].append(candidate)
    for directory, group in groups.items():
        reset = f"cd {shlex.quote(str(directory))}\n"
        text = reset + ("\n" + reset).join(item.text for item in group) + "\n"
        yield from parse_shell(plan, bear, text, directory)


def candidates_missing(plan, bear, entries, candidates, parsed=None):
    if parsed is None:
        parsed = parse_candidates(plan, bear, candidates)
    observed = {match_key(entry) for entry in entries}
    repaired = load_repairs(plan)
    covered = set()
    missing = set()
    for entry in parsed:
        location = entry_location(entry)
        if match_key(entry) in observed:
            covered.add(location)
        else:
            missing.add(location)
    result = []
    for candidate in candidates:
        words = shell_words(candidate.text)
        expected_output = output_arg(words)
        sources = mentioned_sources(words, candidate.directory)
        locations = {
            (candidate.directory, source, absolute(candidate.directory, expected_output)
             if expected_output else source.with_suffix(".o"))
            for source in sources
        }
        if not expected_output:
            locations = {(d, s, d / o.name) for d, s, o in locations}
        if locations and locations <= covered and not locations & missing:
            continue
        if "$$" in expected_output and words:
            pattern = re.escape(str(absolute(candidate.directory, expected_output))).replace(r"\$\$", r"[0-9]+")
            if any(directory == candidate.directory and source in sources and output
                   and Path(driver).name == Path(words[0]).name and re.fullmatch(pattern, str(output))
                   for directory, source, output, driver, _ in observed):
                continue
        if any(candidate.payload() == record["candidate"]
               and all(match_key(entry) in observed for entry in record["entries"])
               for record in repaired.get(candidate.lines, [])):
            continue
        if not sources and not re.search(r"\.(?:c|cc|cpp|cxx|C)\b", candidate.text):
            continue
        parsed = parse_shell(plan, bear, candidate.text + "\n", candidate.directory)
        if not parsed or any(match_key(entry) not in observed for entry in parsed):
            result.append(candidate)
    return result


def load_repairs(plan):
    path = plan.state / "repairs.json"
    result = defaultdict(list)
    if path.is_file():
        for record in read_json(path):
            result[tuple(record["candidate"]["lines"])].append(record)
    return result


def repair_matches_log(candidate, args):
    texts = [candidate.text]
    if AUTOMAKE_SOURCE.search(candidate.text):
        texts.extend(expand_automake_source(candidate.text, lambda _: local)
                     for local in (True, False))
    for text in texts:
        words = shell_words(text)
        for index, word in enumerate(words):
            end = index + len(args)
            if (word == args[0] and words[index:end] == args
                    and (end == len(words) or re.fullmatch(r"[();&|<>]+", words[end]))):
                return True
    return False


def quiet_hint(plan, candidate):
    annotation = plan.quiet_builds.get(str(candidate.directory))
    if not annotation:
        return None
    output = candidate.output
    source = annotation["sources"].get(output)
    if not source:
        object_path = Path(output)
        stem = object_path.stem.split("_la-", 1)[-1]
        component = source_component(plan.source, candidate.directory) / object_path.parent
        if not component.resolve().is_relative_to(plan.source):
            return None
        matches = [component / f"{stem}{suffix}" for suffix in SOURCE_SUFFIXES]
        matches = [path.name for path in matches if path.is_file()]
        if len(matches) != 1:
            return None
        source = str(Path(annotation["source_prefix"]) / object_path.parent / matches[0])
    if Path(source).suffix not in SOURCE_SUFFIXES:
        return None
    flags = annotation["flags"] + annotation.get("flags_by_output", {}).get(output, [])
    return {"compiler": annotation["compiler"], "flags": flags, "source": source, "output": output}


def matches_quiet_hint(candidate, args, hint):
    directory = candidate.directory
    source = absolute(directory, hint["source"])
    output = output_arg(args)
    flag_order = iter(args[1:])
    return (args[0] == hint["compiler"] and "-c" in args and bool(output)
            and absolute(directory, output) == absolute(directory, hint["output"])
            and mentioned_sources(args, directory) == {source}
            and all(flag in flag_order for flag in hint["flags"]))


def repair_batch(plan, bear, batch, provider):
    hints = {item: quiet_hint(plan, item) for item in batch if item.output is not None}
    context = {"candidates": [
        item.payload() | ({"inference": hints[item]} if item in hints else {}) for item in batch
    ]}
    if plan.prerequisites or plan.generated_globs:
        context["project"] = {
            "prerequisites": plan.prerequisites,
            "generated_globs": plan.generated_globs,
        }
    response = provider.request("repair", context)
    fields(response, "repairs")
    if not isinstance(response["repairs"], list):
        raise Error("Expected an array of repairs.")
    by_lines = {item.lines: item for item in batch}
    repaired = {}
    for repair in response["repairs"]:
        fields(repair, "lines directory arguments")
        lines = repair["lines"]
        if not isinstance(lines, list) or any(type(n) is not int for n in lines):
            raise Error("A repair needs an array of log line numbers.")
        candidate = by_lines.get(tuple(lines))
        args = strings(repair["arguments"], "repair arguments", allow_empty=True)
        if not candidate or repair["directory"] != str(candidate.directory) or not args or not args[0]:
            raise Error("A repair does not match the log line numbers and working directory.")
        if candidate in hints:
            hint = hints[candidate]
            if not hint or not matches_quiet_hint(candidate, args, hint):
                continue
        elif not repair_matches_log(candidate, args):
            continue
        sources = mentioned_sources(args, candidate.directory)
        if not sources:
            continue
        output = output_arg(args)
        parsed = parse_shell(plan, bear, shlex.join(args) + "\n", candidate.directory)
        verified = []
        for entry in parsed:
            directory, source, object_file = entry_location(entry)
            driver = entry_args(entry)[0]
            if (directory == candidate.directory and source in sources
                    and Path(driver).name == Path(args[0]).name
                    and (not output or object_file == absolute(directory, output))):
                verified.append(entry)
        if {entry_location(entry)[1] for entry in verified} == sources:
            repaired.setdefault(candidate, [])
            merge(repaired[candidate], verified)
    return repaired


def report_unresolved(plan, missing):
    for candidate in missing:
        print(f"Unresolved compiler command at {plan.log}:{candidate.lines[0]} "
              f"in {candidate.directory}.", file=sys.stderr)


def compile_output(entry):
    directory, source, output = entry_location(entry)
    if output is None and "-c" in entry_args(entry):
        output = directory / source.with_suffix(".o").name
    return directory, output


def missing_abbreviated(abbreviated, entries):
    outputs = {compile_output(entry) for entry in entries} if abbreviated else set()
    return [item for item in abbreviated
            if (item.directory.resolve(), absolute(item.directory, item.output)) not in outputs]


def report_abbreviated(plan, abbreviated, entries):
    missing = Counter(item.directory for item in missing_abbreviated(abbreviated, entries))
    if not missing:
        return False
    print(f"The log omits full compiler commands for {sum(missing.values())} build steps "
          "without matching database outputs.", file=sys.stderr)
    for directory, count in sorted(missing.items()):
        print(f"  {count} in {directory}", file=sys.stderr)
    if plan.quiet_builds:
        print("The remaining steps need more evidence about the source files or compiler flags.", file=sys.stderr)
    else:
        print("Run init with the source tree to annotate abbreviated compile steps.", file=sys.stderr)
    return True


def repair_quiet(plan, bear, get_llm, entries, abbreviated):
    seen = set()
    candidates = []
    for item in missing_abbreviated(abbreviated, entries):
        key = item.directory, item.output
        if key not in seen and quiet_hint(plan, item):
            seen.add(key)
            candidates.append(item)
    if candidates:
        print(f"Using the saved build analysis for {len(candidates)} abbreviated compile steps.", flush=True)
        repair(plan, bear, get_llm, entries, candidates)
    return report_abbreviated(plan, abbreviated, entries)


def replace_inferred(entries, observed, records):
    outputs = {compile_output(entry) for entry in observed}
    replaced = set()
    for group in records.values():
        for record in group:
            if not record.get("inferred"):
                continue
            remaining = []
            for entry in record["entries"]:
                if compile_output(entry) in outputs:
                    replaced.add(entry_key(entry))
                else:
                    remaining.append(entry)
            record["entries"] = remaining
    if replaced:
        entries[:] = [entry for entry in entries if entry_key(entry) not in replaced]
        print(f"Replaced {len(replaced)} inferred commands with commands from the log.", flush=True)
    return bool(replaced)


def save_repairs(plan, records):
    write_json(plan.state / "repairs.json", [
        record for group in records.values() for record in group if record["entries"]
    ])


def refresh_database(plan, bear, get_llm):
    print(f"Scanning the saved log for compiler commands in profile {plan.profile}.", flush=True)
    with report_wait("Build log scan"):
        candidates, abbreviated = scan_log(plan)
    print(f"Bear is parsing {len(candidates)} candidate log commands.", flush=True)
    with report_wait("Bear parsing"):
        parsed = list(parse_candidates(plan, bear, candidates))
    entries = read_database(plan.database)
    previous_count = len(entries)
    records = load_repairs(plan)
    replaced = replace_inferred(entries, parsed, records)
    merge(entries, parsed)
    if entries:
        write_json(plan.database, entries)
    if replaced:
        save_repairs(plan, records)
    print(f"Bear produced {len(parsed)} compilation commands.", flush=True)
    print("Checking which log commands still need repair.", flush=True)
    with report_wait("Log coverage check"):
        missing = candidates_missing(plan, bear, entries, candidates, parsed)
    unresolved = repair(plan, bear, get_llm, entries, missing)
    abbreviated_missing = repair_quiet(plan, bear, get_llm, entries, abbreviated)
    if entries:
        print(f"Saved {plan.public_database} with {len(entries)} compilation commands "
              f"({len(entries) - previous_count} added).", flush=True)
    else:
        print("The compilation database is empty. Check that the log contains compiler commands.", file=sys.stderr)
    return int(unresolved or abbreviated_missing or not entries)


def patch(plan, bear, get_llm):
    if not plan.log.is_file():
        raise Error(f"No build log exists at {plan.log}. Run autobear build first.")
    entries = read_database(plan.database)
    print(f"Checking {plan.log} for commands that still need repair.", flush=True)
    with report_wait("Log coverage check"):
        candidates, abbreviated = scan_log(plan)
        missing = candidates_missing(plan, bear, entries, candidates)
    unresolved = repair(plan, bear, get_llm, entries, missing)
    return repair_quiet(plan, bear, get_llm, entries, abbreviated) or unresolved


def repair(plan, bear, get_llm, entries, missing):
    if not missing:
        print("No visible compiler commands need repair.")
        return 0
    try:
        provider = get_llm()
        problem = provider.unavailable()
        if problem:
            raise Error(problem)
    except (Error, OSError, ValueError) as exc:
        print(f"Repair unavailable. {exc}", file=sys.stderr)
    else:
        batch_size = 20
        batch_count = (len(missing) + batch_size - 1) // batch_size
        print(f"LLM {provider.name} ({provider.model or 'default model'}). "
              f"Checking {len(missing)} unresolved log commands in {batch_count} batches. "
              "Each batch can take a few minutes.", flush=True)
        repaired = {}
        records = load_repairs(plan)
        for number, offset in enumerate(range(0, len(missing), batch_size), 1):
            batch = missing[offset:offset + batch_size]
            stage = f"Repair batch {number}/{batch_count}"
            print(f"{stage} started with {len(batch)} log commands.", flush=True)
            try:
                with report_wait(stage):
                    accepted = repair_batch(plan, bear, batch, provider)
                print(f"{stage} repaired {len(accepted)} log commands.", flush=True)
                for candidate, commands in accepted.items():
                    if candidate.output is None:
                        replace_inferred(entries, commands, records)
                    merge(entries, commands)
                    records[candidate.lines].append({
                        "candidate": candidate.payload(), "entries": commands,
                        "inferred": candidate.output is not None,
                    })
                inferred = sum(len(commands) for candidate, commands in accepted.items() if candidate.output is not None)
                if inferred:
                    print(f"Added {inferred} inferred compilation commands. See {plan.state / 'repairs.json'}.",
                          flush=True)
                repaired.update(accepted)
            except (Error, OSError, ValueError) as exc:
                print(f"Repair batch failed. {exc}", file=sys.stderr)
            write_json(plan.database, entries)
            save_repairs(plan, records)
        missing = [candidate for candidate in missing if candidate not in repaired]
        print(f"Repaired {len(repaired)} log commands. {len(missing)} remain unresolved.", flush=True)
    report_unresolved(plan, missing)
    return bool(missing)
