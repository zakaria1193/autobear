import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


class Error(Exception):
    pass


def write_text(path, content):
    if path.exists() and path.read_bytes() == content.encode("utf-8"):
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(content.encode("utf-8"))
            stream.close()
            if path.exists():
                temporary.chmod(stat.S_IMODE(path.stat().st_mode))
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def strings(value, name, *, allow_empty=False):
    if not isinstance(value, list) or any(
        not isinstance(item, str) or (not allow_empty and not item) or any(c in item for c in "\0\r\n")
        for item in value
    ):
        raise Error(f"Invalid string array for {name}.")
    return value


def fields(value, names):
    if not isinstance(value, dict) or value.keys() != set(names.split()):
        raise Error(f"Expected object fields {names}.")
    return value


def targets(value):
    result = strings(value, "targets")
    if not result or any(not re.fullmatch(r"[A-Za-z0-9_./+][A-Za-z0-9_./+-]*", t) for t in result):
        raise Error("Expected at least one make target. Use target names without options or assignments.")
    return result


DEFAULT_PROFILE = "default"
DATABASE_NAME = "compile_commands.json"
STATE_DIRECTORY = ".autobear"


@contextmanager
def build_lock(build):
    state = build / STATE_DIRECTORY
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def profile_state(build, name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise Error("Use letters, digits, dots, underscores, or hyphens in a profile name.")
    root = build / STATE_DIRECTORY
    return root if name == DEFAULT_PROFILE else root / "profiles" / name


def active_profile(build):
    public = build / DATABASE_NAME
    if public.is_symlink():
        target = Path(os.path.normpath(build / os.readlink(public)))
        parent = build / STATE_DIRECTORY / "profiles"
        if target.name == DATABASE_NAME and target.parent.parent == parent:
            return target.parent.name
    return DEFAULT_PROFILE


def profile_names(build):
    default = profile_state(build, DEFAULT_PROFILE)
    names = [DEFAULT_PROFILE] if (default / "plan.json").is_file() else []
    root = default / "profiles"
    if root.is_dir():
        names.extend(path.name for path in sorted(root.iterdir())
                     if (path / "plan.json").is_file())
    return names


def activate_profile(plan, variant=None):
    public = plan.public_database
    database = plan.database if variant is None else profile_state(plan.build, variant) / DATABASE_NAME
    relative = database.relative_to(plan.build)
    if public.is_symlink() and Path(os.readlink(public)) == relative:
        return
    default_database = profile_state(plan.build, DEFAULT_PROFILE) / DATABASE_NAME
    if public.is_file() and not default_database.exists() and (
        not public.is_symlink() or active_profile(plan.build) == DEFAULT_PROFILE
    ):
        write_text(default_database, public.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(dir=public.parent) as temp:
        link = Path(temp) / DATABASE_NAME
        link.symlink_to(relative)
        link.replace(public)


def quiet_annotations(value):
    if not isinstance(value, dict):
        raise Error("Expected quiet build annotations by working directory.")
    for directory, annotation in value.items():
        if not isinstance(directory, str) or not Path(directory).is_absolute():
            raise Error("Expected an absolute directory for quiet build annotations.")
        if not isinstance(annotation, dict):
            raise Error("Expected an object for each quiet build annotation.")
        fields({key: item for key, item in annotation.items() if key != "flags_by_output"},
               "compiler flags source_prefix sources")
        flags_by_output = annotation.get("flags_by_output", {})
        if not isinstance(flags_by_output, dict):
            raise Error("Expected quiet build flags by object name.")
        for flags in flags_by_output.values():
            strings(flags, "object compiler flags")
        if not isinstance(annotation["compiler"], str) or not annotation["compiler"]:
            raise Error("Expected a compiler in quiet build annotations.")
        strings(annotation["flags"], "quiet build compiler flags")
        if not isinstance(annotation["source_prefix"], str):
            raise Error("Expected a source prefix in quiet build annotations.")
        sources = annotation["sources"]
        if not isinstance(sources, dict) or any(
            not isinstance(path, str) or not path for path in sources.values()
        ):
            raise Error("Expected source paths by object name in quiet build annotations.")
    return value


@dataclass
class Plan:
    source: Path
    build: Path
    configure_args: list
    prerequisites: list
    targets: list
    generated_globs: list
    log_directory: Path | None = None
    profile: str = DEFAULT_PROFILE
    analyzed: bool = True
    quiet_builds: dict = field(default_factory=dict)

    @classmethod
    def validate(cls, source, build, response, explicit=()):
        if not isinstance(response, dict):
            raise Error("Expected an object for the analysis response.")
        response = dict(response)
        annotations = quiet_annotations(response.pop("quiet_builds", {}))
        fields(response, "configure_args prerequisites targets generated_globs")
        args = strings(response["configure_args"], "configure_args")
        if any(not re.fullmatch(r"--[\w-]+(?:=[^\r\n\x00]*)?", arg) for arg in args):
            raise Error("Configure arguments must be long GNU configure options.")
        prerequisites = []
        if not isinstance(response["prerequisites"], list):
            raise Error("Expected an array of prerequisite steps.")
        for step in response["prerequisites"]:
            fields(step, "directory targets")
            directory = step["directory"]
            if not isinstance(directory, str) or not directory or "\0" in directory:
                raise Error("Expected a prerequisite directory relative to the build directory.")
            relative = Path(directory)
            if relative.is_absolute() or not (build / relative).resolve().is_relative_to(build):
                raise Error(f"Prerequisite directory is outside the build directory. {directory}")
            prerequisites.append({"directory": str(relative), "targets": targets(step["targets"])})
        globs = strings(response["generated_globs"], "generated_globs")
        if any(Path(pattern).is_absolute() or ".." in Path(pattern).parts for pattern in globs):
            raise Error("Generated file globs must be relative to the build directory.")
        return cls(source, build, args, prerequisites, targets(list(explicit) or response["targets"]),
                   globs, quiet_builds=annotations)

    @property
    def state(self):
        return profile_state(self.build, self.profile)

    @property
    def database(self):
        return self.state / DATABASE_NAME

    @property
    def public_database(self):
        return self.build / DATABASE_NAME

    @property
    def log(self):
        return self.state / ("imported.log" if self.log_directory is not None else "build.log")

    def save(self):
        value = vars(self) | {"source": str(self.source), "build": str(self.build)}
        del value["profile"]
        if self.log_directory is None:
            del value["log_directory"]
        else:
            value["log_directory"] = str(self.log_directory)
        write_json(self.state / "plan.json", value)

    @classmethod
    def load(cls, build, profile=None):
        name = profile if profile is not None else active_profile(build)
        path = profile_state(build, name) / "plan.json"
        if not path.is_file():
            raise Error(f"No build profile named {name} in {build}.")
        value = read_json(path)
        if not isinstance(value, dict):
            raise Error("Expected a JSON object in the saved plan.")
        log_directory = value.pop("log_directory", None)
        analyzed = value.pop("analyzed", log_directory is None or value.get("source") != str(build))
        if type(analyzed) is not bool:
            raise Error("Expected a boolean for the saved analysis status.")
        if log_directory is not None and (
            not isinstance(log_directory, str) or not Path(log_directory).is_absolute()
        ):
            raise Error("The original log directory must be an absolute path.")
        if value.pop("build", None) != str(build):
            raise Error("The build directory differs from the saved plan. Run autobear init again.")
        source = value.pop("source", None)
        if not isinstance(source, str) or not Path(source).is_absolute():
            raise Error("The saved source directory must be an absolute path.")
        plan = cls.validate(Path(source).resolve(), build, value)
        plan.profile = name
        plan.analyzed = analyzed
        if log_directory is not None:
            plan.log_directory = Path(log_directory)
        return plan


SYSTEM_TYPE = re.compile(r"^\s*checking (build|host|target) system type\.\.\. (?:\(cached\) )?([\w.+-]+)\s*$")
SYSTEM_OPTION = re.compile(r"--(build|host|target)(?:=(.*))?")
SYSTEM_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
GNU_TRIPLET = re.compile(r"[A-Za-z0-9_]+-[A-Za-z0-9_.+]+-[A-Za-z0-9_.+-]+")


def canonical_triplet(source, value):
    if not isinstance(value, str) or not SYSTEM_ALIAS.fullmatch(value):
        return None
    scripts = [source / "config.sub", source / "build-aux" / "config.sub", Path("/usr/share/misc/config.sub")]
    if installed := shutil.which("config.sub"):
        scripts.append(Path(installed))
    if script := next((path for path in scripts if path.is_file()), None):
        try:
            result = subprocess.run(["/bin/sh", str(script), value], cwd=source,
                                    capture_output=True, text=True, check=True, timeout=5)
            triplet = result.stdout.strip()
            if GNU_TRIPLET.fullmatch(triplet):
                return triplet
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return value if GNU_TRIPLET.fullmatch(value) else None


def profile_triplet(source, args, systems):
    aliases = {}
    arguments = iter(args or ())
    for arg in arguments:
        if match := SYSTEM_OPTION.fullmatch(arg):
            kind, value = match.groups()
            aliases[kind] = value if value is not None else next(arguments, "")
    for kind in ("target", "host", "build"):
        if triplet := canonical_triplet(source, systems.get(kind) or aliases.get(kind)):
            return triplet
    return None


def logged_configuration(path):
    """Return configure arguments when unambiguous and the first reported system types."""
    commands = set()
    systems = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if match := SYSTEM_TYPE.fullmatch(line):
                systems.setdefault(*match.groups())
            if len(commands) > 1:
                continue
            if "Run: cd " in line:
                line = line.split("Run: cd ", 1)[1].partition("; ")[2]
            if not re.match(r"^\s*(?:(?:\S*/)?(?:bash|sh)\s+)?(?:\S*/)?configure(?:\s|$)", line):
                continue
            try:
                words = shlex.split(line)
            except ValueError:
                commands.update({None, ()})
                continue
            script = 0 if Path(words[0]).name == "configure" else 1
            if any(word in {";", "&&", "||", "|", "&"} for word in words[script + 1:]):
                commands.update({None, ()})
                continue
            commands.add(tuple(words[script + 1:]))
    return (next(iter(commands)) if len(commands) == 1 else None), systems


def analysis_log(path):
    directory = re.compile(r"^make(?:\[\d+\])?: Entering directory [`'](.*)'$")
    setup = re.compile(
        r"(?:^|[\s/])(?:configure|config\.status|bison|yacc|flex|"
        r"gen[-\w]+|generate[-\w]*|prebuild|bootstrap)(?=[:.\s]|$)|"
        r"\b(?:--(?:target|host|build)=|(?:all|configure)-[\w-]+\b)"
    )
    compiler = re.compile(r"(?:^|\s)-(?:c|S)(?:\s|$)")
    generator = re.compile(r"^\s+(?:GEN|YACC|LEX)\s+\S+\.(?:c|h|cc)\s*$")
    head, steps, commands, build_commands, system_types, generated = [], [], [], [], [], []
    tail = deque(maxlen=6)
    components = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.rstrip()
            snippet = f"{number}: {line[:480]}"
            if not line:
                continue
            if len(head) < 8:
                head.append(snippet)
            tail.append(snippet)
            match = directory.fullmatch(line)
            if match and len(components) < 16:
                components[Path(match[1]).name] = None
            if len(steps) < 20 and (match or setup.search(line)):
                steps.append(snippet)
            if len(commands) < 8 and compiler.search(line):
                commands.append(snippet)
            if len(build_commands) < 12 and "Run: cd " in line and re.search(
                r"; (?:\S+/)?make\s|(?:^|/)configure(?:\s|$)", line
            ):
                build_commands.append(f"{number}: {line[:1800]}")
            if len(system_types) < 9 and SYSTEM_TYPE.fullmatch(line):
                system_types.append(snippet)
            if len(generated) < 32 and generator.fullmatch(line):
                generated.append(snippet)
    if not head:
        raise Error(f"The build log at {path} is empty.")
    return list(dict.fromkeys([*head, *build_commands, *system_types, *steps, *commands, *generated, *tail])), components


def project_context(source, build, explicit, log):
    build_log, log_components = analysis_log(log)
    excerpt_paths = [root / name for root in (source, build) for name in (
        "config.status", "configure", "configure.ac", "Makefile", "Makefile.in", "Makefile.am"
    )]
    components = list(dict.fromkeys([
        *(target.removeprefix("all-") for target in explicit),
        *log_components,
        "ld", "bfd", "gcc", "gdb", "csu", "elf",
    ]))
    for component in components[:12]:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+", component) or component in {".", ".."}:
            continue
        excerpt_paths.extend(root / component / name for root in (source, build)
                             for name in ("Makefile", "Makefile.in", "Makefile.am", "config.status"))
    excerpts = {}
    budget = 42_000
    needles = re.compile(
        r"^ *(?:ac_cs_config=|PACKAGE_(?:NAME|VERSION)=|AC_INIT\b|"
        r"--(?:enable|disable|with|without)-|all(?:-[\w-]+)?:|"
        r"(?:prebuild|bootstrap|generate|gen-[\w-]+)\s*:|"
        r"[^\s:=]+(?:sources|SOURCES|GENERATED|generated)\s*[:+?]?=)"
    )
    for path in dict.fromkeys(excerpt_paths):
        if budget <= 0:
            break
        if not path.is_file():
            continue
        limit = min(3000, budget)
        with path.open(errors="replace") as stream:
            head = stream.read(limit // 3)
            stream.seek(0)
            relevant = ""
            target_rules = ""
            available = limit - len(head)
            ordinary_limit = available // 2 if explicit else available
            for number, line in enumerate(stream, 1):
                if number > 120_000:
                    break
                if len(target_rules) < available // 2 and any(line.startswith(f"{t}:") for t in explicit):
                    target_rules += f"{number}: {line}"[:320]
                elif len(relevant) < ordinary_limit and needles.search(line):
                    relevant += f"{number}: {line}"[:320]
                if len(relevant) >= ordinary_limit and (not explicit or len(target_rules) >= available // 2):
                    break
        excerpts[str(path)] = head + (target_rules + relevant)[:available]
        budget -= len(excerpts[str(path)])
    compilers = {name: shutil.which(name) for name in ("cc", "c++", "gcc", "g++", "clang", "clang++")}
    for name in ("CC", "CXX"):
        if os.environ.get(name):
            words = shlex.split(os.environ[name])
            if words:
                compilers[name] = shutil.which(words[0])
    return {
        "source": str(source), "build": str(build), "targets": list(explicit),
        "local_compilers": compilers, "excerpts": excerpts, "build_log_excerpt": build_log,
    }
