import os
import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from autobear.capture import entry_location, read_database
from autobear.cli import main
from test_autobear import RecordedProvider, plan_response, save_local_plan


BINUTILS = os.getenv("AUTOBEAR_BINUTILS_SRC")


@unittest.skipUnless(BINUTILS and platform.machine() == "x86_64"
                     and all(shutil.which(tool) for tool in ("bear", "clangd", "gcc", "make")),
                     "Set AUTOBEAR_BINUTILS_SRC to a disposable native binutils source tree.")
class BinutilsTest(unittest.TestCase):
    def test_capture_native_linker(self):
        source = Path(BINUTILS).resolve()
        plan = plan_response(
            configure_args=["--disable-gdb", "--disable-sim", "--disable-gprofng",
                            "--disable-nls", "--disable-werror", "--without-zstd"],
            targets=["all-ld"], generated_globs=["ld/eelf*.c"],
        )
        worker = RecordedProvider({"repairs": []})

        def settings():
            return {"worker": worker}

        with tempfile.TemporaryDirectory(prefix="autobear-ld-") as temp:
            build = Path(temp).resolve()
            flags = ["--build", str(build)]
            save_local_plan(source, build, plan)
            jobs = os.getenv("AUTOBEAR_TEST_JOBS", "4")
            self.assertEqual(main(["build", "--jobs", jobs, *flags], settings), 0)
            entries = read_database(build / "compile_commands.json")
            locations = {entry_location(entry)[:2] for entry in entries}
            for path in (source / "ld" / "ldmain.c", build / "ld" / "eelf_x86_64.c"):
                self.assertIn((build / "ld", path), locations)
                result = subprocess.run(["clangd", f"--check={path}"], cwd=build,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"Loaded compilation database from {build / 'compile_commands.json'}", result.stderr)
                self.assertIn(f"Compile command from CDB is: [{build / 'ld'}]", result.stderr)
            self.assertEqual(main(["verify", "--sample-size", "1", *flags], settings), 0)
