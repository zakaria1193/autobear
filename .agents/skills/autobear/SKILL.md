---
name: autobear
description: Create a clangd compilation database from a verbose GNU build log. Use for logs from binutils, GCC, GDB, glibc, and similar projects.
---

# Refresh from a GNU build log

Run `autobear refresh LOG` from the directory that will contain the database. Use `--log-directory PATH` if a remote log omits make directory markers.

To create a profile, run `autobear init LOG SRC --profile NAME`. Use a representative log for that variant. `init` creates the profile and refreshes its database. Later repairs use the saved findings to interpret unresolved commands.

Use `autobear switch --list` to inspect the profiles and `autobear switch NAME` to select one. Import logs for the active profile.

Use `autobear patch` to retry unresolved commands from the saved log. Check that clangd can access the recorded paths before you run `autobear verify`.
