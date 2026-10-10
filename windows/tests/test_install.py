"""Installer destination checks, exercised without changing an installed copy."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "install.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallerDestinationTest(unittest.TestCase):
    def invoke(self, destination: str):
        return subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPT), "-Destination", destination],
                              capture_output=True, text=True, timeout=15)

    def test_missing_destination_beneath_junction_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            os.symlink(target, link, target_is_directory=True)
            result = self.invoke(str(link / "new-install"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shared, remote or a link", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_unc_destination_is_rejected(self):
        result = self.invoke(r"\\localhost\C$\agiw-test-never-create")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shared, remote or a link", result.stderr)


if __name__ == "__main__":
    unittest.main()
