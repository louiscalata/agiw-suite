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
                              capture_output=True, text=True, timeout=60)

    def leftovers(self, destination: Path):
        return [p.name for p in destination.iterdir() if p.name.startswith((".agiw-stage-", ".agiw-backup-"))]

    def test_update_is_staged_keeps_state_and_rolls_back_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "agiw-win"
            first = self.invoke(str(dest))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((dest / ".agiw-win-install").is_file())
            self.assertTrue((dest / "agiw_win" / "probes.py").is_file() and (dest / "web" / "app.js").is_file())
            self.assertEqual(self.leftovers(dest), [])
            (dest / "state").mkdir()
            (dest / "state" / "observer.log").write_text("kept")
            (dest / "agiw_win" / "probes.py").write_text("OLD probes")
            (dest / "web" / "app.js").write_text("OLD app")
            # NOTICE is swapped last; a reader that never shares delete makes its replace fail mid-update.
            with open(dest / "NOTICE", "rb"):
                failed = self.invoke(str(dest))
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("rolled back", failed.stderr)
            self.assertEqual((dest / "agiw_win" / "probes.py").read_text(), "OLD probes")
            self.assertEqual((dest / "web" / "app.js").read_text(), "OLD app")
            self.assertEqual(self.leftovers(dest), [])
            again = self.invoke(str(dest))
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotEqual((dest / "agiw_win" / "probes.py").read_text(), "OLD probes")
            self.assertEqual((dest / "state" / "observer.log").read_text(), "kept")
            self.assertEqual(self.leftovers(dest), [])

    def test_dangling_link_ancestor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "gone"
            target.mkdir()
            link = root / "link"
            os.symlink(target, link, target_is_directory=True)
            target.rmdir()
            result = self.invoke(str(link / "new-install"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shared, remote or a link", result.stderr)

    def test_relative_destination_follows_the_powershell_location(self):
        # After Set-Location the process directory still points elsewhere; the install must follow PowerShell.
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            command = f"Set-Location -LiteralPath '{tmp}'; & '{SCRIPT}' -Destination rel-install"
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-Command", command], capture_output=True, text=True, timeout=60, cwd=elsewhere)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "rel-install" / ".agiw-win-install").is_file())
            self.assertFalse((Path(elsewhere) / "rel-install").exists())

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
