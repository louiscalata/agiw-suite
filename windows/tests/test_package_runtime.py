"""Import the Windows observer from only the installer's staged Python inventory."""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


WINDOWS = Path(__file__).resolve().parents[1]
REPO = WINDOWS.parent


def root_resources():
    """Read the actual root-resource foreach list copied by install.ps1."""
    source = (WINDOWS / "install.ps1").read_text(encoding="utf-8")
    match = re.search(
        r"foreach \(\$file in ([^\r\n]+)\) \{\s*"
        r"\$files\.Add\(@\(\(Join-Path \$repo \$file\), \$file\)\)\s*\}",
        source,
    )
    if match is None:
        raise AssertionError("Windows installer root resource inventory was not found")
    return re.findall(r"'([^']+)'", match.group(1))


class WindowsPackageRuntimeTests(unittest.TestCase):
    def test_observer_imports_from_clean_staged_installer_inventory(self):
        # Keep the repository and its parent out of the subprocess import path.
        # Copy exactly the Python module groups install.ps1 ships, not a guessed
        # list of dependencies that could hide an incomplete installer inventory.
        with tempfile.TemporaryDirectory(prefix=".windows-package-test-", dir=REPO) as tmp:
            stage = Path(tmp)
            package = stage / "agiw_win"
            package.mkdir()
            for source in (WINDOWS / "agiw_win").glob("*.py"):
                shutil.copyfile(source, package / source.name)
            for name in root_resources():
                shutil.copyfile(REPO / name, stage / name)
            code = (
                "import pathlib, sys; "
                "sys.path.insert(0, str(pathlib.Path.cwd())); "
                "import agiw_win.server, inference_collect, remote_machines; "
                "assert pathlib.Path(remote_machines.__file__).parent == pathlib.Path.cwd(); "
                "print('staged Windows observer import: PASS')"
            )
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", code], cwd=stage,
                capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("staged Windows observer import: PASS", result.stdout)


if __name__ == "__main__":
    unittest.main()
