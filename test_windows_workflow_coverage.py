"""Keep Windows workflow triggers aligned with shipped shared dependencies."""
import fnmatch
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent


class WindowsWorkflowCoverageTests(unittest.TestCase):
    def test_windows_workflow_covers_shipped_shared_runtime_files(self):
        installer = (ROOT / "windows/install.ps1").read_text()
        shared = re.search(r"foreach \(\$file in ('usage-format\.mjs'.*?)\) \{", installer).group(1)
        paths = re.findall(r"'([^']+)'", shared)
        runtime = [p for p in paths if p.endswith((".py", ".json", ".mjs"))]
        runtime.append("online_code_repair.py")
        workflow = (ROOT / ".github/workflows/windows-edition.yml").read_text()
        filters = re.findall(r"paths: \[(.*?)\]", workflow, re.S)
        self.assertEqual(len(filters), 2)
        for event, block in zip(("push", "pull_request"), filters):
            patterns = re.findall(r"'([^']+)'", block)
            for path in runtime:
                with self.subTest(event=event, path=path):
                    self.assertTrue(any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns),
                                    f"{event} misses shipped dependency {path}")


if __name__ == "__main__":
    unittest.main()
