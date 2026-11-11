"""Run only the Android source contract, using the shell check's real path shape."""
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parent


class AndroidSourceContract(unittest.TestCase):
    def test_contract_accepts_shell_layout_path_from_either_working_directory(self):
        core = ROOT / 'android/monitor-core'
        shell = (core / 'check.sh').read_text()
        source = shell.split("<<'PY'\n", 1)[1].split('\nPY', 1)[0]
        # Preserve '..': this is the actual argument from check.sh, not a
        # normalized substitute that would miss its CI path-resolution defect.
        layout = str(core / '../app/src/main/res/layout')
        for cwd in (ROOT, core):
            with self.subTest(cwd=cwd.name):
                result = subprocess.run([sys.executable, '-', layout], input=source,
                                        text=True, capture_output=True, cwd=cwd, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('android layouts: ids and clipping checked', result.stdout)
                self.assertIn('status widget:', result.stdout)


if __name__ == '__main__':
    unittest.main()
