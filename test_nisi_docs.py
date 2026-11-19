import json
import unittest
from pathlib import Path


class NisiDocsTests(unittest.TestCase):
    def test_agents_verify_version_matches_manifest(self):
        root = Path(__file__).resolve().parent
        manifest_path = root / "vendor" / "nisi" / "manifest.json"
        version = json.loads(manifest_path.read_text(encoding="utf-8"))["version"]
        self.assertIn(
            f"Nisi verify prints `PASS` and `{version}`.",
            (root / "AGENTS.md").read_text(encoding="utf-8"),
        )
