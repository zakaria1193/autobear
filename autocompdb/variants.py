import hashlib
import re
from collections import Counter, defaultdict

from .capture import absolute, compile_output, entry_key, read_database
from .state import (
    DATABASE_NAME, DEFAULT_PROFILE, activate_profile, profile_names, profile_state, read_json, write_json,
)


DEPENDENCY_FLAGS = {"-M", "-MM", "-MD", "-MMD", "-MG", "-MP"}
DEPENDENCY_OPTIONS = ("-MF", "-MT", "-MQ", "-MJ")
PATH_OPTIONS = ("-isystem", "-iquote", "-idirafter", "-I", "-F", "-B", "-L")


def clangd_flags(directory, source, args):
    """Compare compiler arguments independently of source, output, and dependency files."""
    flags = []
    remaining = iter(args)
    for arg in remaining:
        if arg in ("-o", *DEPENDENCY_OPTIONS):
            next(remaining, None)
        elif (arg in DEPENDENCY_FLAGS or arg in ("-c", "-S", "-pipe", str(source))
              or arg.startswith(DEPENDENCY_OPTIONS)
              or (arg.startswith(("-Wp,-MD,", "-Wp,-MMD,")) and arg.count(",") == 2)):
            continue
        elif arg == "-I-":
            flags.append(arg)
        elif arg in ("-D", "-U"):
            flags.append(arg + next(remaining, ""))
        elif arg in PATH_OPTIONS:
            value = next(remaining, "")
            flags.extend((arg, path_flag(directory, value)))
        elif option := next((option for option in PATH_OPTIONS if arg.startswith(option)), None):
            flags.extend((option, path_flag(directory, arg[len(option):])))
        elif arg in ("-include", "-imacros", "-include-pch", "--sysroot", "-isysroot"):
            value = next(remaining, "")
            flags.extend((arg, path_flag(directory, value)))
        elif arg.startswith("--sysroot="):
            flags.extend(("--sysroot", path_flag(directory, arg.split("=", 1)[1])))
        else:
            flags.append(arg)
    return tuple(flags)


def path_flag(directory, value):
    return str(absolute(directory, value)) if value and not value.startswith(("=", "$SYSROOT")) else value


def target_group(directory, source, output):
    object_directory = output.parent
    label = object_directory.name
    if object_directory == directory or label == ".libs":
        label = directory.name
    stem = output.stem
    if stem.endswith(f"-{source.stem}"):
        return directory, stem.removesuffix(f"-{source.stem}")
    return object_directory, label


def variant_preferences(entries):
    by_source = defaultdict(dict)
    sources = []
    for index, entry in enumerate(entries):
        directory, source, _, driver, args = entry_key(entry)
        _, output = compile_output(entry)
        sources.append(source)
        if output is not None:
            by_source[source][output] = (index, target_group(directory, source, output),
                                         (driver, clangd_flags(directory, source, args)))

    preferences = defaultdict(dict)
    for source, commands in by_source.items():
        if len({signature for _, _, signature in commands.values()}) < 2:
            continue
        counts = Counter(group for _, group, _ in commands.values())
        for output, (index, group, _) in commands.items():
            if counts[group] > 1:
                group = output.parent, output.name
            preferences[group][source] = index
    return sources, preferences


def saved_variants(build):
    path = profile_state(build, DEFAULT_PROFILE) / "variants.json"
    return read_json(path) if path.is_file() else {}


def update_variants(plan, selected):
    entries = read_database(plan.database)
    sources, preferences = variant_preferences(entries)
    variants = saved_variants(plan.build)
    updated = {name: owner for name, owner in variants.items() if owner != plan.profile}
    used = set(profile_names(plan.build)) | updated.keys()
    created = []
    for (directory, label), preferred in sorted(preferences.items()):
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).lstrip("._-") or "target"
        name = label if plan.profile == DEFAULT_PROFILE else f"{plan.profile}.{label}"
        if name in used:
            suffix = hashlib.sha256(f"{directory}/{label}".encode()).hexdigest()[:10]
            name = f"{name}.{suffix}"
            while name in used:
                name += "-variant"
        used.add(name)
        updated[name] = plan.profile
        state = profile_state(plan.build, name)
        state.mkdir(parents=True, exist_ok=True)
        # clangd uses the first command for a source when the database has several.
        order = list(preferred.values())
        order.extend(index for index, source in enumerate(sources) if preferred.get(source) != index)
        write_json(state / DATABASE_NAME, [entries[index] for index in order])
        if name not in variants:
            created.append(name)
    if updated != variants:
        write_json(profile_state(plan.build, DEFAULT_PROFILE) / "variants.json", updated)
    activate_profile(plan, selected if updated.get(selected) == plan.profile else None)
    for name in variants.keys() - updated.keys():
        (profile_state(plan.build, name) / DATABASE_NAME).unlink(missing_ok=True)
    if created:
        print(f"Created clangd profiles {', '.join(created)}. Use autocompdb switch --list.", flush=True)
