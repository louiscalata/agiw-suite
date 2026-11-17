"""Keep golden replay discoverable and its corpus covered by workflow triggers."""

import fnmatch
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = "windows/tests/fixtures/windows_collect_golden/cases.json"
DISCOVERY = """
import importlib.util
import json
import sys
import unittest
from pathlib import Path

root = Path(sys.argv[1])
tests = root / "windows/tests"
sys.path[:0] = [str(tests), str(root / "windows")]
spec = importlib.util.spec_from_file_location("test_collect_golden", tests / "test_collect_golden.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
if sys.argv[2] == "prepend-root":
    sys.path.insert(0, str(root))
try:
    suite = unittest.TestLoader().discover(str(tests), pattern="test_probes.py")
except ImportError as error:
    outcome = {"error": str(error)}
else:
    outcome = {
        "module": Path(sys.modules["test_probes"].__file__).resolve().relative_to(root).as_posix(),
        "tests": suite.countTestCases(),
    }
sys.stdout.write(json.dumps(outcome))
"""


class CollectGoldenDiscoveryTests(unittest.TestCase):
    def discover_probes(self, mode):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", DISCOVERY, str(ROOT), mode],
            cwd=ROOT / "windows",
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return json.loads(result.stdout)

    def test_harness_preserves_windows_probe_discovery(self):
        outcome = self.discover_probes("unchanged")
        self.assertEqual(outcome.get("module"), "windows/tests/test_probes.py", outcome)
        self.assertGreater(outcome["tests"], 0)

    def test_root_prepend_reproduces_discovery_collision(self):
        outcome = self.discover_probes("prepend-root")
        self.assertIn("incorrectly imported", outcome.get("error", ""), outcome)

    def test_corpus_is_covered_by_both_workflow_events(self):
        self.assertTrue((ROOT / CORPUS).is_file())
        workflow = (ROOT / ".github/workflows/windows-edition.yml").read_text(encoding="utf-8")
        filters = re.findall(r"paths: \[(.*?)\]", workflow, re.S)
        self.assertEqual(len(filters), 2)
        for event, block in zip(("push", "pull_request"), filters):
            patterns = re.findall(r"'([^']+)'", block)
            with self.subTest(event=event):
                self.assertTrue(any(fnmatch.fnmatchcase(CORPUS, pattern) for pattern in patterns))
                self.assertFalse(
                    any(
                        fnmatch.fnmatchcase("test_support/windows_collect_golden/cases.json", pattern)
                        for pattern in patterns
                    )
                )


if __name__ == "__main__":
    unittest.main()
