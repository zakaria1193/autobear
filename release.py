#!/usr/bin/env python3

import argparse
import re
import shlex
import subprocess
import sys
import tomllib


VERSION = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"


def git(*args, capture=False):
    command = ["git", *args]
    print(f"\033[1m{shlex.join(command)}\033[0m", file=sys.stderr, flush=True)
    return subprocess.run(command, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def previous_version(remote):
    local_tags = git("tag", "--list", capture=True).splitlines()
    remote_refs = git("ls-remote", "--tags", "--refs", "--", remote, capture=True).splitlines()
    remote_tags = [line.split("\t", 1)[1].removeprefix("refs/tags/") for line in remote_refs]
    versions = [match.groups() for tag in [*local_tags, *remote_tags]
                if (match := re.fullmatch(f"v{VERSION}", tag))]
    if not versions:
        metadata = tomllib.loads(git("show", "HEAD:pyproject.toml", capture=True))
        match = re.fullmatch(VERSION, metadata["project"]["version"])
        if match is None:
            raise ValueError("Expected a major.minor.patch version in pyproject.toml.")
        versions = [match.groups()]
    return max(tuple(map(int, version)) for version in versions)


def main():
    parser = argparse.ArgumentParser(description="Push a major or minor PyPI release tag for HEAD.")
    bump = parser.add_mutually_exclusive_group(required=True)
    bump.add_argument("--major", action="store_true", help="Increment the major version.")
    bump.add_argument("--minor", action="store_true", help="Increment the minor version.")
    parser.add_argument("--remote", default="origin", help="Remote to push to. Defaults to origin.")
    args = parser.parse_args()
    try:
        commit = git("rev-parse", "--verify", "HEAD", capture=True).strip()
        major, minor, _ = previous_version(args.remote)
        tag = f"v{major + 1}.0.0" if args.major else f"v{major}.{minor + 1}.0"
        git("push", "--", args.remote, f"{commit}:refs/tags/{tag}")
        git("tag", tag, commit)
    except (subprocess.CalledProcessError, OSError, ValueError, KeyError) as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    print(f"Pushed release tag {tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
