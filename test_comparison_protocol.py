"""Shape checks for the two-machine full-suite comparison protocol (design only)."""
import itertools
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
PROTOCOL = ROOT / "docs" / "data" / "agiw-comparison-protocol.json"
PERFORMANCE = ROOT / "docs" / "performance.md"


class ComparisonProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.p = json.loads(PROTOCOL.read_text(encoding="utf-8"))

    def test_two_active_hosts_only(self):
        self.assertEqual(self.p["client_hosts"], ["Mac", "Windows"])
        hosts = {r["client_host"] for r in self.p["results"]}
        self.assertEqual(hosts, {"Mac", "Windows"})
        self.assertNotIn("Linux", json.dumps(self.p["results"]))

    def test_full_factorial_twelve_conditions(self):
        expected = set(itertools.product(self.p["client_hosts"], self.p["workflows"],
                                         self.p["jev_treatment"]["settings"]))
        got = [(r["client_host"], r["workflow"], r["jev"]) for r in self.p["results"]]
        self.assertEqual(len(got), len(set(got)), "duplicate condition")
        self.assertEqual(set(got), expected)
        self.assertEqual(len(got), 12)
        self.assertEqual(self.p["design"]["condition_count"], 12)

    def test_all_metric_cells_null_and_not_run(self):
        metrics = set(self.p["metrics"])
        self.assertEqual(len(metrics), 7)
        cells = 0
        for r in self.p["results"]:
            self.assertEqual(r["status"], "NOT_RUN")
            self.assertEqual(set(r["measurements"]), metrics)
            for v in r["measurements"].values():
                self.assertIsNone(v)
                cells += 1
        self.assertEqual(cells, 84)
        self.assertEqual(self.p["design"]["metric_cells"], 84)
        self.assertEqual(self.p["status"], "DESIGN_ONLY_NOT_RUN")

    def test_trial_counts_keep_per_condition_rate(self):
        d, old = self.p["design"], self.p["supersedes"]
        self.assertEqual(old["conditions"], 18)
        self.assertEqual(old["metric_cells"], 126)
        self.assertEqual(d["exploratory_executor_trials"], 3 * d["condition_count"])
        self.assertEqual(d["repeated_executor_trials"], 9 * d["condition_count"])

    def test_performance_doc_matches_protocol(self):
        text = PERFORMANCE.read_text(encoding="utf-8")
        self.assertIn("12 conditions on two machines", text)
        self.assertIn("0 of 84 metric cells", text)
        self.assertIn("data/agiw-comparison-protocol.json", text)


if __name__ == "__main__":
    unittest.main()
