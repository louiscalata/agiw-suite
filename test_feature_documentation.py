"""Keep the public feature guide and example independent of operator evidence."""
import json
import subprocess
from pathlib import Path
import unittest


REPO = Path(__file__).resolve().parent


class FeatureDocumentationTests(unittest.TestCase):
    def test_feature_guide_and_example_contain_no_private_audit_details(self):
        # Assemble prohibited audit tokens so the regression test itself is safe
        # to publish and can participate in the same repository privacy scan.
        private_tokens = (
            "lo" + "uis", "11" + "236", "11" + "237", "18" + "080",
            "bi" + "onic", "sshd" + "-sess", "meta/muse" + "-glimmer-30b",
        )
        for name in ("three-machine-pipeline.md", "remote-machines.example.json"):
            text = (REPO / "docs" / name).read_text(encoding="utf-8").casefold()
            for token in private_tokens:
                with self.subTest(file=name, token=token):
                    self.assertFalse(token in text, f"Private audit detail in {name}: {token}")

    def test_all_outgoing_files_have_no_private_owner_strings(self):
        tracked = subprocess.check_output(["git", "diff", "--name-only", "-z"], cwd=REPO)
        added = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=REPO)
        tokens = ("lo" + "uis", "11" + "236", "11" + "237", "18" + "080")
        for raw in sorted(set((tracked + added).split(b"\0")) - {b""}):
            path = REPO / raw.decode()
            if path.is_file():
                body = path.read_bytes().lower()
                for token in tokens:
                    with self.subTest(file=raw.decode(), token=token):
                        self.assertNotIn(token.encode(), body, f"Private owner string in {raw.decode()}")

    def test_example_is_a_placeholder_and_demonstrates_optional_lane_metadata(self):
        example = json.loads((REPO / "docs" / "remote-machines.example.json").read_text())
        lanes = [lane for machine in example["machines"] for lane in machine["lanes"]]
        self.assertTrue(all("placeholder" in machine["label"].lower()
                            for machine in example["machines"]))
        self.assertTrue(any(lane.get("timeoutSeconds") == 6 for lane in lanes))
        self.assertTrue(any(type(lane.get("contextTokens")) is int for lane in lanes))
        self.assertTrue(any(lane.get("thinking") in ("on", "off", "unknown") for lane in lanes))


if __name__ == "__main__":
    unittest.main()
