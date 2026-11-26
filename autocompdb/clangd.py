import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .capture import (
    SOURCE_SUFFIXES, absolute, candidates_missing, entry_args, entry_location, output_arg,
    read_database, report_abbreviated, scan_log, show_command,
)
from .state import Error, write_text

BEGIN = "# BEGIN autobear"
END = "# END autobear"
MANAGED = re.compile(r"^# BEGIN autobear\n---\nCompileFlags:\n  CompilationDatabase: "
                     r"[^\n]+\n# END autobear\n?", re.MULTILINE)
CDB = re.compile(r"Compile command from CDB is: \[(.*)\] (.*)$", re.MULTILINE)


def configure_clangd(plan):
    conflict = False
    for root in dict.fromkeys((plan.source, plan.build)):
        path = (root / ".clangd").resolve()
        content = path.read_bytes().decode("utf-8") if path.exists() else ""
        managed = list(MANAGED.finditer(content))
        user = MANAGED.sub("", content)
        if BEGIN in user or END in user or len(managed) > 1:
            print(f"Invalid autocompdb block in {path}.", file=sys.stderr)
            conflict = True
            continue
        try:
            documents = list(yaml.safe_load_all(user))
        except yaml.YAMLError as exc:
            print(f"Cannot read clangd configuration in {path}. {exc}", file=sys.stderr)
            conflict = True
            continue
        selections = []
        for document in documents:
            if isinstance(document, dict) and isinstance(document.get("CompileFlags"), dict):
                flags = document["CompileFlags"]
                if "CompilationDatabase" in flags:
                    selections.append((flags["CompilationDatabase"], bool(document.get("If"))))
        compatible = all(isinstance(value, str) and value not in {"None", "Ancestors"}
                         and (root / value).resolve() == plan.build for value, _ in selections)
        if not compatible:
            print(f"Conflicting CompileFlags.CompilationDatabase in {path}.", file=sys.stderr)
            conflict = True
            continue
        if any(not conditional for _, conditional in selections):
            if managed:
                write_text(path, user)
            continue
        block = f"{BEGIN}\n---\nCompileFlags:\n  CompilationDatabase: {json.dumps(str(plan.build))}\n{END}\n"
        if managed:
            updated = MANAGED.sub(lambda _: block, content)
        else:
            updated = content
            if updated and not updated.endswith("\n"):
                updated += "\n"
            updated += block
        write_text(path, updated)
    return conflict


def generated_files(plan):
    return {file.resolve() for pattern in plan.generated_globs for file in plan.build.glob(pattern)
            if file.is_file() and file.resolve().is_relative_to(plan.build)
            and file.suffix in SOURCE_SUFFIXES}


def source_files_on_disk(plan):
    for directory, subdirs, names in os.walk(plan.source):
        root = Path(directory)
        subdirs[:] = [name for name in subdirs if not name.startswith(".")
                      and root / name != plan.build]
        for name in names:
            path = root / name
            if path.suffix in SOURCE_SUFFIXES and path.is_file():
                yield path.resolve()


def expected_driver(args, directory):
    driver = args[0]
    if "/" in driver:
        return os.path.normpath(directory / driver)
    return shutil.which(driver) or driver


def verify(plan, sample_size, query_drivers, *, database=None):
    database = database or plan.database
    entries = read_database(database)
    by_file = {}
    for entry in entries:
        by_file.setdefault(entry_location(entry)[1], entry)
    generated = generated_files(plan)
    candidates, abbreviated = scan_log(plan)
    missing = candidates_missing(plan, entries, candidates)
    abbreviated_missing = report_abbreviated(plan, abbreviated, entries)
    print(f"Database contains {len(entries)} commands for {len(by_file)} files.")
    source_on_disk = set(source_files_on_disk(plan)) - generated
    print(f"Source files on disk with commands {len(source_on_disk & by_file.keys())}/{len(source_on_disk)}.")
    print(f"Generated files on disk with commands {len(generated & by_file.keys())}/{len(generated)}.")
    for source in sorted(generated - by_file.keys()):
        print(f"Generated file without a database entry {source}")
    print(f"Unresolved observed compiler commands {len(missing)}.")
    for candidate in missing:
        print(f"{plan.log}:{candidate.lines[0]} in {candidate.directory}")
    if not entries:
        if plan.log_directory is not None:
            raise Error("The compilation database is empty. Import a log that contains compiler commands.")
        raise Error("The compilation database is empty. Run autocompdb build in a fresh build directory.")
    clangd = shutil.which("clangd")
    if not clangd:
        raise Error("Install clangd and add it to PATH.")
    source_files = sorted(path for path in by_file.keys() - generated if path.is_relative_to(plan.source))
    generated_samples = sorted(generated & by_file.keys())
    if not source_files:
        print("No source-tree file has a database entry.", file=sys.stderr)
    if plan.generated_globs and not generated_samples:
        print("No generated file matches both the plan and the database.", file=sys.stderr)
    failed = bool(missing or abbreviated_missing or not source_files
                  or (plan.generated_globs and not generated_samples))
    for path in source_files[:sample_size] + generated_samples[:sample_size]:
        entry = by_file[path]
        directory, _, expected_output = entry_location(entry)
        command = [clangd, f"--check={path}"]
        if plan.log_directory is not None:
            command.append(f"--compile-commands-dir={plan.build}")
        if query_drivers:
            command.append(f"--query-driver={','.join(query_drivers)}")
        show_command(command)
        result = subprocess.run(command, cwd=plan.build, text=True, errors="replace",
                                capture_output=True, timeout=120)
        log = result.stdout + result.stderr
        selected = CDB.search(log)
        selected_args = shlex.split(selected[2]) if selected else []
        loaded = any(f"Loaded compilation database from {path}" in log
                     for path in (plan.public_database, database))
        driver = expected_driver(entry_args(entry), directory)
        selected_output = output_arg(selected_args)
        matches = (loaded and selected and Path(selected[1]).resolve() == directory
                   and selected_args and Path(selected_args[0]).name == Path(driver).name
                   and (not expected_output or
                        (selected_output and absolute(directory, selected_output) == expected_output)))
        if not matches or result.returncode:
            failed = True
            print(log, file=sys.stderr, end="")
            print(f"clangd check failed for {path}. Expected a database command from {directory}.",
                  file=sys.stderr)
        else:
            print(f"clangd parsed {path} using the database entry from {directory}.")
    return int(failed)
