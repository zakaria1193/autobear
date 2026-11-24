# autobear

Autobear creates a clangd `compile_commands.json` from the build log of a GNU autotools/autoconf project such as binutils, GCC, GDB, or glibc. Import a log from CI, another machine, or a container without rerunning the build.

Autobear reads compiler commands directly from the log. For quiet logs, it uses saved analysis of the log and source tree to recover missing arguments. Build profiles keep native and cross builds separate. Clangd can use a separate view for each target when targets compile the same source with different flags.

The name is a nod to Bear, the compilation database tool. Autobear parses build logs itself and runs without Bear.

## Install

Install Python 3.11 or later. Then install autobear from the repository.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

## Configure the LLM

Autobear uses one LLM. Choose the strongest model you have, such as Claude Opus. Autobear calls it only when you import or repair a log, so a strong model adds little cost.

Codex is the only provider tested manually. The automated provider tests use recorded responses.

In a terminal, `init` asks for the provider and model when the TOML file is absent. It saves the choice in `~/.config/autobear/config.toml` or under `XDG_CONFIG_HOME`. CLI providers must be on `PATH` to appear in the menu. Autobear queries the Codex and OpenRouter model catalogs during setup. Choose a model by number or enter its ID if the catalog is unavailable.

For noninteractive runs, set the LLM in the TOML file or in the environment. Without a setting, autobear uses `codex-cli` with its default model.

```toml
[llm]
provider = "claude-cli"
model = "opus"
```

The available providers are `claude-cli`, `codex-cli`, `agy-cli`, and `openrouter`. Authenticate each CLI before use. The Antigravity CLI needs support for `--print`, `--output-format json`, and `--sandbox`.

For OpenRouter, set `model` to a model that supports JSON mode. Export `OPENROUTER_API_KEY` in the environment. If the LLM is unavailable, autobear preserves the entries it recovered from the log and reports the unresolved lines.

To override the file, set `AUTOBEAR_PROVIDER` or `AUTOBEAR_MODEL`.

A configuration file from an earlier version has `[architect]` and `[worker]` tables. Replace them with one `[llm]` table.

## Create and switch build profiles

A build profile keeps the compilation commands for one build configuration together. For example, one binutils profile can cover `ld.bfd`, `ld.gold`, and `as`. Use separate profiles to switch between different configurations, such as native and cross builds.

Use `init` the first time you import a build configuration, including after you change the configure target. Autobear uses the GNU system triplet from the log. It checks the target, then the host, then the build system. When GNU `config.sub` is available, Autobear uses it to expand aliases such as `arm-elf`. If the log does not identify a triplet, Autobear uses the LLM analysis. If the analysis cannot identify a triplet, the profile is named `default`. Use `--profile NAME` to choose a name.

```sh
autobear init ../arm-elf.log .
```

Run the example from the binutils source checkout. If the log identifies the ARM ELF target, `init` can name the profile `arm-unknown-elf` and activate it. If you need to specify a Make target such as `all-ld`, pass `--target all-ld` to autobear.

Use `refresh` to import a later log for the same configuration, including a rebuild that compiles a new source file. Select the matching profile first if another profile is active.

```sh
autobear switch arm-unknown-elf
autobear refresh ../arm-elf.log
```

Use `switch` alone when you want to use a database you already imported. `switch` updates the `compile_commands.json` symlink. Run `autobear switch --list` to see the profile names and the active profile marked with `*`. In a terminal with `fzf` on `PATH`, the list also lets you pick a profile.

Each build profile keeps its own plan, log, and database. If targets compile the same source into different objects with different flags, `init` adds clangd views. Each view puts its target's command first for the shared source and keeps all other commands. The views use the same plan and log.

`refresh` and `patch` also update the selected build's clangd views. `verify` checks the database for the selected profile or view.

A verbose log shows compiler command lines and working directories. For GNU make, `LC_ALL=C make --print-directory --output-sync=recurse V=1` produces this format. For a quiet log, `init` uses the source tree to help recover missing commands before it looks for target variants.

If `refresh` cannot recover commands from a quiet log, rerun `init` with the matching source tree and the same profile. The LLM reads the build rules and records compiler flags, source mappings, and generated files. Later repairs use those annotations for steps without full commands. Autobear reports inferred entries and records them in the profile's `repairs.json`. A full command from a later log replaces the inferred command for that output.

Autobear writes each command with the build machine's paths. Clangd needs the sources, headers, and generated files at those paths to use the database.

Use `--log-directory /original/build/path` if the log omits make directory markers. Without this option, commands outside those markers use the database directory. Use `--build ./output` to choose a different database directory.

## Retry or verify

Run `autobear patch` to retry unresolved commands from the saved log. Run `autobear verify` where clangd can access the recorded paths.

If your editor cannot find the database, pass `--compile-commands-dir=/path/to/database-directory` to clangd.

The `autobear build` command runs GNU make with `V=1` for saved local build plans. It reads the resulting log, so make recipes must print their compiler commands or recognizable status lines.

## Publish a release to PyPI

Register the GitHub repository as a [PyPI Trusted Publisher](https://docs.pypi.org/trusted-publishers/). In the publisher settings, use `release.yml` as the workflow file and `pypi` as the environment.

Run one of these commands from the Git checkout at the commit you want to release.

```sh
./release.py --minor
# For a major release, use ./release.py --major instead.
```

The script finds the highest `vMAJOR.MINOR.PATCH` tag across the checkout and the remote. For the first release, it starts from the version in `pyproject.toml`. It pushes the new tag to `origin` by default. Use `--remote NAME` to select another remote.

The tag starts the GitHub Actions workflow. The workflow uses the tag version to build the wheel and source distribution, checks them, and publishes them to PyPI. After publication, install the package with `python -m pip install autobear`.

## Test a log import

Import a log with one compiler command.

```sh
scratch=$(mktemp -d)
printf '%s\n' 'gcc -c ../src/hello.c -o hello.o' > "$scratch/build.log"
autobear refresh "$scratch/build.log" --log-directory /remote/build --build "$scratch"
cat "$scratch/compile_commands.json"
```

Expect one entry for `/remote/src/hello.c`. This test needs no LLM.

## Run tests

```sh
python -m unittest discover -s tests -v
```

The provider tests use recorded responses. The build tests use GCC, GNU make, and clangd when those tools are available.

Set `AUTOBEAR_BINUTILS_SRC` to a disposable x86-64 binutils source tree to enable the native acceptance test. The test writes a `.clangd` file in that source tree.

```sh
AUTOBEAR_BINUTILS_SRC=./binutils-test-copy python -m unittest discover -s tests -v
```
