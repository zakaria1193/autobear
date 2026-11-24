import os
import shutil
import subprocess

from .state import Error


def fzf_choice(prompt, choices):
    """Return the selected value, or None if fzf is unavailable."""
    executable = shutil.which("fzf")
    if executable is None:
        return None
    env = os.environ.copy()
    # Output options such as --expect add lines to the selection on stdout.
    env.pop("FZF_DEFAULT_OPTS", None)
    env.pop("FZF_DEFAULT_OPTS_FILE", None)
    try:
        result = subprocess.run(
            [executable, "--prompt", f"{prompt}> "],
            input="\n".join(choices) + "\n", stdout=subprocess.PIPE, env=env, text=True,
        )
    except KeyboardInterrupt as exc:
        raise Error("Selection canceled.") from exc
    if result.returncode in (1, 130):
        raise Error("Selection canceled.")
    if result.returncode:
        raise Error(f"fzf exited with status {result.returncode}.")
    choice = result.stdout.rstrip("\n")
    if choice not in choices:
        raise Error("fzf returned an invalid selection.")
    return choices[choice]
