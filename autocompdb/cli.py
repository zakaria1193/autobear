import argparse
import filecmp
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from decimal import Decimal
from itertools import count
from pathlib import Path

from . import providers
from .capture import (
    capture, lock, missing_abbreviated, patch, quiet_evidence, quiet_hint,
    read_database, refresh_database, scan_log,
)
from .clangd import configure_clangd, verify
from .progress import report_wait
from .selection import fzf_choice
from .state import (
    DATABASE_NAME, Error, Plan, activate_profile, active_profile, logged_configuration, profile_names,
    profile_state, project_context, targets,
)
from .variants import saved_variants, update_variants


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Expected a positive integer.")
    return number


def arguments(argv):
    parser = argparse.ArgumentParser(prog="autocompdb", description="Capture GNU build commands for clangd.")
    commands = parser.add_subparsers(dest="action", required=True)
    init = commands.add_parser("init", help="Import a build log and discover clangd profiles.")
    log = commands.add_parser("refresh", help="Update the selected build's databases from a log.")
    log.add_argument("log", type=Path, help="Path to the build log.")
    for command in (init, log):
        command.add_argument("--log-directory", type=Path,
                             help="Original working directory for commands outside make markers.")
    build = commands.add_parser("build", help="Capture and repair a make build.")
    repair = commands.add_parser("patch", help="Retry repair from the build log.")
    check = commands.add_parser("verify", help="Check database coverage and run clangd.")
    switch = commands.add_parser("switch", help="Select or list build profiles.")
    selection = switch.add_mutually_exclusive_group()
    selection.add_argument("profile", nargs="?", help="Profile to select. Omit to open the interactive menu.")
    selection.add_argument("--list", action="store_true", help="Pick a profile with fzf or print profile names.")
    for command in (init, log, build, repair, check, switch):
        command.add_argument("--build", default=Path.cwd(), type=Path,
                             help="Directory for the database and plan. Defaults to the current directory.")
    init.add_argument("log", type=Path, help="Representative log from the build to analyze.")
    init.add_argument("source", type=Path, help="Source directory.")
    init.add_argument("--profile", help="Profile name. Omit to let the LLM infer a name.")
    init.add_argument("--target", action="append", default=[], help="Make target. Repeat for more targets.")
    build.add_argument("--jobs", type=positive, help="Number of parallel make jobs.")
    check.add_argument("--sample-size", type=positive, default=2, help="Maximum sample size per file group.")
    check.add_argument("--query-driver", action="append", default=[], help="Compiler path that clangd can execute.")
    return parser.parse_args(argv)


def log_path(args):
    path = args.log.resolve()
    if not path.is_file():
        raise Error(f"No build log exists at {path}.")
    if args.log_directory is not None and not args.log_directory.is_absolute():
        raise Error("Use an absolute path for the original log directory.")
    return path


def import_match(plan, source, requested_targets, log, configuration):
    if plan is None or not plan.analyzed or not plan.database.is_file():
        return False, None
    if plan.source != source or (requested_targets and plan.targets != requested_targets):
        return False, False
    if not plan.log.is_file():
        return False, None
    if filecmp.cmp(plan.log, log, shallow=False):
        return True, True
    incoming_args, incoming_systems = configuration
    previous_args, previous_systems = logged_configuration(plan.log)
    shared = incoming_systems.keys() & previous_systems.keys()
    if any(incoming_systems[key] != previous_systems[key] for key in shared):
        return False, False
    if incoming_args is not None and previous_args is not None and (incoming_args or previous_args or shared):
        return False, incoming_args == previous_args
    return False, None


def find_import(plans, matches, active):
    for name in dict.fromkeys([active, *plans]):
        if matches.get(name, (False, None))[0]:
            return plans[name]
    compatible = [plans[name] for name, (_, same_configuration) in matches.items() if same_configuration]
    return compatible[0] if len(compatible) == 1 else None


def choose_import_profile(plan, suggestion, fallback, plans, matches, variants):
    base = suggestion or fallback
    profile_state(plan.build, base)
    for index in count(1):
        name = base if index == 1 else f"{base}.{index}"
        if name in variants:
            continue
        existing = plans.get(name)
        if existing is None or not existing.analyzed or not existing.database.is_file():
            return name
        _, same_configuration = matches[name]
        if same_configuration or (same_configuration is None and existing.source == plan.source
                                    and existing.configure_args == plan.configure_args
                                    and existing.targets == plan.targets):
            return name


def init(args, build, get_llm):
    source = args.source.resolve()
    if not source.is_dir():
        raise Error(f"No source directory exists at {source}.")
    log = log_path(args)
    variants = saved_variants(build)
    selected = active_profile(build)
    if args.profile in variants:
        raise Error(f"Profile {args.profile} is generated from {variants[args.profile]}. "
                    f"Use --profile {variants[args.profile]} to import its build log.")
    if args.target:
        targets(args.target)
    if args.profile is not None:
        profile_state(build, args.profile)
    active = variants.get(selected, selected)
    names = [name for name in profile_names(build) if args.profile is None or name == args.profile]
    plans = {name: Plan.load(build, name) for name in names}
    configuration = logged_configuration(log) if plans else (None, {})
    matches = {name: import_match(plan, source, args.target, log, configuration) for name, plan in plans.items()}
    existing = plans.get(args.profile) if args.profile is not None else find_import(plans, matches, active)
    name = args.profile or (existing.profile if existing else None)
    same_log, same_configuration = matches.get(name, (False, None))
    if same_configuration is False:
        raise Error(f"Profile {name} has a database for another configuration. Use --profile with a new name.")
    selected_targets = existing.targets if same_configuration else args.target
    log_directory = args.log_directory or (existing.log_directory if existing else None)
    print(f"Reading source and log excerpts{' for profile ' + name if name else ''}.", flush=True)
    print(f"Source directory {source}", flush=True)
    print(f"Build log {log}", flush=True)
    with report_wait("Excerpt collection"):
        scan_plan = Plan(source, build, [], [], [], [], log_directory=log_directory)
        _, abbreviated = scan_log(scan_plan, path=log)
        if same_log and log_directory == existing.log_directory:
            missing = missing_abbreviated(abbreviated, read_database(existing.database))
            reuse = all(quiet_hint(existing, step) for step in missing)
        else:
            reuse = False
        if not reuse:
            context = project_context(source, build, selected_targets, log)
        if abbreviated:
            print(f"Found {len(abbreviated)} abbreviated compile steps.", flush=True)
            if not reuse:
                print("The LLM will annotate the missing commands.", flush=True)
                context["abbreviated_builds"] = quiet_evidence(source, abbreviated)
    if reuse:
        print(f"Reusing the saved plan for profile {name}.", flush=True)
        return refresh_profile(existing, log, log_directory, get_llm, selected)
    llm = get_llm()
    print(f"LLM {llm.name} ({llm.model or 'default model'}). "
          "Analyzing targets, configure options, and prerequisites. This can take a few minutes.", flush=True)
    with report_wait("Build analysis"):
        response = llm.request("analyze", context)
    if not isinstance(response, dict):
        raise Error("Expected an object for the analysis response.")
    response = dict(response)
    suggestion = response.pop("profile_name", "")
    plan = Plan.validate(source, build, response, selected_targets)
    if name is None:
        if not isinstance(suggestion, str):
            raise Error("Expected a string for the suggested profile name.")
        name = choose_import_profile(plan, suggestion, active, plans, matches, variants)
        existing = plans.get(name)
        if matches.get(name, (False, None))[1]:
            plan.targets = existing.targets
        log_directory = args.log_directory or (existing.log_directory if existing else None)
    plan.profile = name
    state = plan.state
    observed = {str(item.directory) for item in abbreviated}
    plan.quiet_builds = {directory: annotation for directory, annotation in plan.quiet_builds.items()
                         if directory in observed}
    if args.profile is not None and existing and existing.analyzed and existing.database.is_file() and not same_configuration and (
        existing.targets != plan.targets or existing.configure_args != plan.configure_args
    ):
        raise Error(f"Profile {name} has a database for another configuration. Use --profile with a new name.")
    if same_log and log_directory == existing.log_directory:
        plan.quiet_builds = existing.quiet_builds | plan.quiet_builds
    state.mkdir(parents=True, exist_ok=True)
    plan.save()
    activate_profile(plan)
    print(f"Selected build profile {name}. Saved {state / 'plan.json'}.", flush=True)
    print(f"Make targets {shlex.join(plan.targets)}", flush=True)
    print(f"Configure options {shlex.join(plan.configure_args) or '(none)'}", flush=True)
    for step in plan.prerequisites:
        print(f"Prerequisite in {step['directory']}. Make targets {shlex.join(step['targets'])}", flush=True)
    if plan.generated_globs:
        print(f"Generated file patterns {shlex.join(plan.generated_globs)}", flush=True)
    return refresh_profile(plan, log, log_directory, get_llm, selected)


def refresh(args, build, get_llm):
    path = log_path(args)
    selected = active_profile(build)
    name = saved_variants(build).get(selected, selected)
    if (profile_state(build, name) / "plan.json").is_file():
        plan = Plan.load(build, name)
    else:
        plan = Plan(build, build, [], [], ["all"], [], analyzed=False)
    return refresh_profile(plan, path, args.log_directory, get_llm, selected)


def refresh_profile(plan, path, log_directory, get_llm, selected):
    plan.log_directory = log_directory or plan.log_directory or plan.build
    if path != plan.log.resolve():
        print(f"Saving a copy of the build log to {plan.log}.", flush=True)
        with tempfile.NamedTemporaryFile(dir=plan.state, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                with path.open("rb") as log:
                    shutil.copyfileobj(log, stream)
                stream.close()
                os.replace(temporary, plan.log)
            finally:
                temporary.unlink(missing_ok=True)
    plan.save()
    activate_profile(plan)
    status = refresh_database(plan, get_llm)
    update_variants(plan, selected)
    return status


def profile_choices(build, variants):
    active = active_profile(build)
    choices = {}
    for name in [*profile_names(build), *sorted(variants)]:
        marker = "*" if name == active else " "
        choices[f"{marker} {name}"] = name
    return choices


def choose_profile(choices):
    if not sys.stdin.isatty():
        raise Error("Specify a profile name or use autocompdb switch --list.")
    if not choices:
        raise Error("No build profiles. Run autocompdb init first.")
    selected = fzf_choice("Build profile", choices)
    if selected is not None:
        return selected
    numbers = {str(index): name for index, name in enumerate(choices.values(), 1)}
    for index, label in enumerate(choices, 1):
        print(f"  {index}. {label}")
    try:
        while True:
            answer = input("Build profile? ").strip()
            selected = numbers.get(answer, answer)
            if selected in choices.values():
                return selected
            print("Select a profile from the list.")
    except (EOFError, KeyboardInterrupt) as exc:
        raise Error("Selection canceled.") from exc


def switch_profile(args, build):
    variants = saved_variants(build)
    name = args.profile
    if args.list:
        choices = profile_choices(build, variants)
        if choices and sys.stdin.isatty() and sys.stdout.isatty():
            name = fzf_choice("Build profile", choices)
        if name is None:
            for label in choices:
                print(label)
            return 0
    if name is None:
        name = choose_profile(profile_choices(build, variants))
    variant = name if name in variants else None
    plan = Plan.load(build, variants.get(name, name))
    activate_profile(plan, variant)
    print(f"Selected build profile {name}.")
    return 0


def main(argv=None, get_llm=None):
    args = arguments(argv)
    get_llm = get_llm or (lambda: providers.settings(query=args.action == "init"))
    usage = []

    def tracked_llm():
        llm = get_llm()
        if isinstance(llm, providers.Provider):
            return replace(llm, usage=usage)
        return llm

    build = args.build.resolve()
    try:
        state = build / ".autobear"
        with lock(state):
            if args.action == "switch":
                return switch_profile(args, build)
            if args.action == "init":
                return init(args, build, tracked_llm)
            if args.action == "refresh":
                return refresh(args, build, tracked_llm)
            selected = active_profile(build)
            owner = saved_variants(build).get(selected)
            plan = Plan.load(build, owner)
            variant = selected if owner is not None else None
            activate_profile(plan, variant)
            if args.action == "build":
                if plan.log_directory is not None:
                    raise Error("This directory uses imported logs. Run autocompdb refresh to update its database.")
                capture(plan, args.jobs)
                unresolved = refresh_database(plan, tracked_llm, allow_empty=True)
                conflict = configure_clangd(plan)
                return int(unresolved or conflict)
            if args.action == "patch":
                unresolved = patch(plan, tracked_llm)
                update_variants(plan, selected)
                return int(unresolved)
            database = profile_state(build, variant) / DATABASE_NAME if variant else plan.database
            return verify(plan, args.sample_size, args.query_driver, database=database)
    except (Error, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"autocompdb: {exc}", file=sys.stderr)
        return 1
    finally:
        if usage:
            sys.stdout.flush()
            print_usage(usage)


def print_usage(usage):
    tokens = [item.tokens for item in usage if item.tokens is not None]
    costs = [item.usd for item in usage if item.usd is not None]
    summary = f"{sum(tokens):,} tokens" if tokens else "Token count unavailable"
    if tokens and len(tokens) < len(usage):
        summary += " (partial)"
    if costs:
        summary += f", ${sum(costs, Decimal('0.00')):f}"
        if len(costs) < len(usage):
            summary += " (partial)"
    print(f"LLM usage: {summary}", file=sys.stderr)
