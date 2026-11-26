import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from .progress import report_wait
from .state import Error, fields, read_json, strings, write_json

SOURCE_SUFFIXES = {".c", ".C", ".cc", ".cp", ".cpp", ".cxx", ".c++", ".C++", ".m", ".mm", ".i", ".ii"}
DIRECTORY = re.compile(r"^make(?:\[\d+\])?: (Entering|Leaving) directory [`'](.*)'$")
STAGE = "autocompdb directory "
ABBREVIATED_COMPILE = re.compile(r"^[ \t]+(?:CC|CXX)[ \t]{2,}(.+\.(?:o|lo|obj))[ \t]*$")
LIBTOOL = re.compile(r"^libtool: compile:\s*")
LIBTOOL_WRAPPER = re.compile(r"(?:^|\s)\S*[/]libtool\s.*--mode=compile(?:\s|$)")
LIBTOOL_REDIRECT = re.compile(r"\s+>/dev/null\s+2>&1$")
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
PROMOTED_OUTPUT = re.compile(
    rf"^(.+?)\s*&&\s*(?:\S*/)?(?:ba)?sh\s+(?:\S+/)?move-if-change\s+"
    rf"([A-Za-z0-9_./+-]+\$\$[A-Za-z0-9_.+-]*)\s+({SHELL_WORD})(?:\s*&&\s*touch\s+{SHELL_WORD})?$"
)
DRIVER = re.compile(r"(?:[\w.+-]+-)?(?:cc|c\+\+|gcc|g\+\+|clang|clang\+\+)(?:-[\d.]+)?")
COMPILING = re.compile(r"(?:^|\s)-(?:c|S|fsyntax-only)(?:\s|$)")
COMPILER_WRAPPERS = {"ccache", "distcc", "icecc", "sccache"}
OPTION_WITH_VALUE = {
    "-B", "-D", "-I", "-L", "-MF", "-MQ", "-MT", "-U", "-Xassembler", "-Xclang",
    "-Xlinker", "-Xpreprocessor", "-idirafter", "-imacros", "-include",
    "-include-pch", "-iprefix", "-iquote", "-isystem", "-isysroot",
    "-iwithprefix", "-iwithprefixbefore", "-target", "-x", "--sysroot",
    "--target",
}
LITERAL_WORD = re.compile(
    r"""(?:\\[^\n]|'[^']*'|"(?:\\["\\]|[^"\\$\x60])*"|[^ \t\r\n'"\\$\x60;&|<>()*?\[{}~])+"""
)


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
    if not any((plan.build / name).is_file() for name in ("GNUmakefile", "makefile", "Makefile")):
        config_status = plan.build / "config.status"
        command = ([str(config_status)] if config_status.is_file()
                   else [str(plan.source / "configure"), *plan.configure_args])
        log = plan.state / "configure.log"
        status = run_stage(command, plan.build, log)
        if status:
            raise Error(f"{shlex.join(command)} exited with status {status}. See {log}.")
    make = ["make", "--print-directory", "--output-sync=recurse"]
    if jobs:
        make.append(f"--jobs={jobs}")
    steps = [*plan.prerequisites, {"directory": ".", "targets": plan.targets}]
    for step in steps:
        cwd = absolute(plan.build, step["directory"])
        command = [*make, "V=1", "--", *step["targets"]]
        status = run_stage(command, cwd, plan.log)
        if status:
            candidates, _ = scan_log(plan)
            entries = read_database(plan.database)
            merge_observed(plan, entries, parse_candidates(candidates))
            if not plan.database.exists():
                write_json(plan.database, entries)
            raise Error(f"{shlex.join(command)} exited with status {status} in {cwd}. See {plan.log}.")


@dataclass(frozen=True)
class Candidate:
    lines: tuple
    directory: Path
    text: str
    output: str | None = None

    def payload(self):
        return {"lines": list(self.lines), "directory": str(self.directory), "text": self.text}


def expand_make_recipe(command, directory, *, inspect_files=True):
    promoted = PROMOTED_OUTPUT.fullmatch(command)
    if promoted:
        invocation, temporary, destination = promoted.groups()
        # The log identifies the permanent output even when it omits the shell PID.
        updated, changed = re.subn(rf"(?<!\S)-o\s+{re.escape(temporary)}(?=\s|$)",
                                   lambda _: f"-o {destination}", invocation)
        if changed == 1:
            command = updated

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
            if libtool_compile:
                command = LIBTOOL_REDIRECT.sub("", command)
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


def compilation_entries(directory, args):
    while args and Path(args[0]).name in COMPILER_WRAPPERS:
        args = args[1:]
    if not args or not DRIVER.fullmatch(Path(args[0]).name):
        return []
    sources = []
    options = iter(enumerate(args[1:], 1))
    mode = ""
    output = ""
    positional = False
    for index, word in options:
        if not positional:
            if word in {"-E", "-M", "-MM"}:
                return []
            if word in {"-c", "-S", "-fsyntax-only"}:
                mode = word
                continue
            if word == "-o":
                _, output = next(options, (None, ""))
                if not output:
                    return []
                continue
            if word.startswith("-o"):
                output = word[2:]
                continue
            if word in OPTION_WITH_VALUE:
                if next(options, None) is None:
                    return []
                continue
            if word == "--":
                positional = True
                continue
            if word.startswith("-"):
                continue
        if Path(word).suffix in SOURCE_SUFFIXES and not is_probe(Path(word)):
            sources.append((index, word))

    if output and len(sources) > 1 and mode:
        return []
    source_indexes = {index for index, _ in sources}
    entries = []
    for index, word in sources:
        entry = {
            "directory": str(directory),
            "arguments": [arg for i, arg in enumerate(args) if i == index or i not in source_indexes],
            "file": str(absolute(directory, word)),
        }
        if output:
            entry["output"] = str(absolute(directory, output))
        elif mode == "-c":
            entry["output"] = str(directory / Path(word).with_suffix(".o").name)
        elif mode == "-S":
            entry["output"] = str(directory / Path(word).with_suffix(".s").name)
        entries.append(entry)
    return entries


def parse_candidate(candidate):
    directory = candidate.directory
    text = candidate.text
    commands = [[]]
    while text:
        text = text.lstrip()
        if not text or text.startswith("#"):
            break
        if text.startswith("&&"):
            commands.append([])
            text = text[2:]
            continue
        token = LITERAL_WORD.match(text)
        if not token:
            return []
        end = token.end()
        if end < len(text) and not (text[end].isspace() or text.startswith("&&", end)):
            return []
        word, = shlex.split(token[0])
        commands[-1].append(word)
        text = text[end:]

    for index, args in enumerate(commands):
        if len(args) == 2 and args[0] == "cd" and not args[1].startswith("-"):
            directory = absolute(directory, args[1])
            continue
        # Automake can move the dependency file after a successful compile.
        remaining = commands[index + 1:]
        if remaining and not (len(remaining) == 1 and remaining[0][:1] == ["mv"]):
            return []
        return compilation_entries(directory, args)
    return []


def match_key(entry):
    directory, source, output, driver, args = entry_key(entry)
    return directory, source, output, Path(driver).name, args


def parse_candidates(candidates):
    return {candidate: parse_candidate(candidate) for candidate in candidates}


def candidates_missing(plan, entries, candidates, parsed=None):
    if parsed is None:
        parsed = parse_candidates(candidates)
    observed = {match_key(entry) for entry in entries}
    repaired = load_repairs(plan)
    result = []
    for candidate, commands in parsed.items():
        if commands:
            if any(match_key(entry) not in observed for entry in commands):
                result.append(candidate)
            continue
        if any(candidate.payload() == record["candidate"]
               and all(match_key(entry) in observed for entry in record["entries"])
               for record in repaired.get(candidate.lines, [])):
            continue
        words = shell_words(candidate.text)
        sources = mentioned_sources(words, candidate.directory)
        output = output_arg(words)
        if "$$" in output and words:
            pattern = re.escape(str(absolute(candidate.directory, output))).replace(r"\$\$", r"[0-9]+")
            if any(directory == candidate.directory and source in sources and object_file
                   and Path(driver).name == Path(words[0]).name and re.fullmatch(pattern, str(object_file))
                   for directory, source, object_file, driver, _ in observed):
                continue
        if sources or any(word.startswith("@") for word in words) or re.search(r"\.(?:c|cc|cpp|cxx|C)\b", candidate.text):
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


def matches_quiet_hint(candidate, args, hint, entries):
    directory = candidate.directory
    source = absolute(directory, hint["source"])
    output = output_arg(args)
    flag_order = iter(args[1:])
    return (args[0] == hint["compiler"] and "-c" in args and bool(output)
            and absolute(directory, output) == absolute(directory, hint["output"])
            and {entry_location(entry)[1] for entry in entries} == {source}
            and all(flag in flag_order for flag in hint["flags"]))


def repair_batch(plan, batch, provider):
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
        entries = compilation_entries(candidate.directory, args)
        if not entries:
            continue
        if candidate in hints:
            hint = hints[candidate]
            if not hint or not matches_quiet_hint(candidate, args, hint, entries):
                continue
        elif not repair_matches_log(candidate, args):
            continue
        merge(repaired.setdefault(candidate, []), entries)
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


def repair_quiet(plan, get_llm, entries, abbreviated):
    seen = set()
    candidates = []
    for item in missing_abbreviated(abbreviated, entries):
        key = item.directory, item.output
        if key not in seen and quiet_hint(plan, item):
            seen.add(key)
            candidates.append(item)
    if candidates:
        print(f"Using the saved build analysis for {len(candidates)} abbreviated compile steps.", flush=True)
        repair(plan, get_llm, entries, candidates)
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


def merge_observed(plan, entries, parsed):
    observed = [entry for commands in parsed.values() for entry in commands]
    records = load_repairs(plan)
    replaced = replace_inferred(entries, observed, records)
    merge(entries, observed)
    if entries:
        write_json(plan.database, entries)
    if replaced:
        save_repairs(plan, records)


def refresh_database(plan, get_llm, *, allow_empty=False):
    print(f"Scanning the saved log for compiler commands in profile {plan.profile}.", flush=True)
    with report_wait("Build log scan"):
        candidates, abbreviated = scan_log(plan)
    with report_wait("Compiler command parsing"):
        parsed = parse_candidates(candidates)
    entries = read_database(plan.database)
    previous_count = len(entries)
    merge_observed(plan, entries, parsed)
    print(f"Parsed {sum(map(len, parsed.values()))} compilation commands.", flush=True)
    print("Checking which log commands still need repair.", flush=True)
    with report_wait("Log coverage check"):
        missing = candidates_missing(plan, entries, candidates, parsed)
    unresolved = repair(plan, get_llm, entries, missing)
    abbreviated_missing = repair_quiet(plan, get_llm, entries, abbreviated)
    if entries:
        print(f"Saved {plan.public_database} with {len(entries)} compilation commands "
              f"({len(entries) - previous_count} added).", flush=True)
    elif allow_empty:
        if not plan.database.exists():
            write_json(plan.database, entries)
        print("No compilation commands found. If make was up to date, use a fresh build directory.")
    else:
        print("The compilation database is empty. Check that the log contains compiler commands.", file=sys.stderr)
    return int(unresolved or abbreviated_missing or (not allow_empty and not entries))


def patch(plan, get_llm):
    if not plan.log.is_file():
        raise Error(f"No build log exists at {plan.log}. Run autocompdb build first.")
    entries = read_database(plan.database)
    print(f"Checking {plan.log} for commands that still need repair.", flush=True)
    with report_wait("Log coverage check"):
        candidates, abbreviated = scan_log(plan)
        missing = candidates_missing(plan, entries, candidates)
    unresolved = repair(plan, get_llm, entries, missing)
    return repair_quiet(plan, get_llm, entries, abbreviated) or unresolved


def repair(plan, get_llm, entries, missing):
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
                    accepted = repair_batch(plan, batch, provider)
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
