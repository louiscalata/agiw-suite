"""The build-input exporter carries every file wrapper-patch/sync-web.sh needs from the tree."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent


class ExportBuildInputs(unittest.TestCase):
    def test_mac_web_holds_every_sync_web_source(self):
        script = (ROOT / "wrapper-patch" / "sync-web.sh").read_text(encoding="utf-8")
        block = re.search(r"^SOURCES=\((.*?)\)", script, re.S | re.M)
        self.assertIsNotNone(block, "sync-web.sh no longer declares SOURCES")
        sources = block.group(1).split()
        self.assertIn("usage-format.mjs", sources)
        with tempfile.TemporaryDirectory(prefix="agiw-export-test-") as folder:
            out = Path(folder) / "out"
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "export-build-inputs.py"), str(out)],
                                    capture_output=True, text=True, cwd=ROOT, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            self.assertEqual(result.returncode, 0, result.stderr)
            missing = [name for name in sources if not (out / "payload" / "mac-web" / name).is_file()]
            self.assertEqual(missing, [], "sync-web.sh would stop on these")


if __name__ == "__main__":
    unittest.main()
