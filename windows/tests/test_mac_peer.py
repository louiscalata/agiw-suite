"""Mac share evidence in complete Windows samples, using only a temporary share."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402


class NoLaneProcesses:
    def sample(self, _ports):
        return {}, None, None


class MacPeerEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.share = Path(temporary.name)
        self.now = 1000.0
        self.peer_path = probes.peer_dir(self.share) / "test-mac.json"
        self.peer_path.parent.mkdir(parents=True)

        def no_fetch(*_args):
            raise AssertionError("An undeclared lane must not make an HTTP request")

        self.collector = Collector(
            self.share, self.share / "bin", host="test-pc", clock=lambda: self.now,
            fetch=no_fetch, gpu_fn=lambda: ([], probes._source("pc-gpu", "PC GPU", "unavailable", "Test")),
            adapters_fn=lambda: [], memory_fn=lambda: ({"level": "unknown"}, None),
            runs_root=self.share / "runs", lane_processes=NoLaneProcesses(),
        )
        self.addCleanup(self.collector.close)

    def report(self, *states):
        self.peer_path.write_text(json.dumps({
            "schemaVersion": 1, "kind": "agiw-peer", "host": "test-mac",
            "platform": "macos", "edition": "mac", "observedUnix": 1000.0,
            "models": [{"id": f"model-{index}", "loadedState": state}
                       for index, state in enumerate(states)],
        }), encoding="utf-8")

    def test_unknown_or_partial_residency_never_becomes_a_known_total(self):
        for states in [("unknown",), ("loaded", "unknown"), ("inactive", "unknown"), ("malformed",)]:
            with self.subTest(states=states):
                self.report(*states)
                snapshot = self.collector.collect()
                self.assertEqual(snapshot["host"], "windows")
                self.assertEqual(snapshot["macPeer"]["state"], "linked")
                self.assertIsNone(snapshot["macPeer"]["loadedCount"])

    def test_an_empty_share_inventory_does_not_prove_zero_loaded(self):
        self.report()
        mac = self.collector.collect()["macPeer"]
        self.assertEqual(mac["state"], "linked")
        self.assertEqual(mac["models"], [])
        self.assertIsNone(mac["loadedCount"])

    def test_complete_residency_retains_loaded_and_zero_loaded_counts(self):
        for states, count in [(("loaded", "inactive"), 1), (("inactive",), 0)]:
            with self.subTest(states=states):
                self.report(*states)
                mac = self.collector.collect()["macPeer"]
                self.assertEqual(mac["loadedCount"], count)
                self.assertEqual(mac["models"][0]["state"], "loaded" if count else "not-loaded")

    def test_a_share_report_does_not_claim_why_the_lan_probe_did_not_answer(self):
        self.report("loaded")
        mac = self.collector.collect()["macPeer"]
        self.assertFalse(mac["verified"])
        self.assertEqual(mac["via"], "sharedchami")
        self.assertIsNone(mac["latencyMs"])
        self.assertIsNone(mac["address"])
        self.assertIn("LAN reachability is not established", mac["detail"])
        self.assertNotIn("not open to the LAN", mac["detail"])

    def test_share_residency_expires_after_thirty_seconds(self):
        self.report("loaded")
        self.now = 1030.0
        self.assertEqual(self.collector.collect()["macPeer"]["state"], "linked")
        self.now = 1030.2
        snapshot = self.collector.collect()
        self.assertEqual(snapshot["sampledAt"], self.now)
        self.assertEqual(snapshot["macPeer"]["state"], "unknown")
        self.assertFalse(snapshot["peers"][0]["fresh"])
        self.assertEqual(snapshot["macPeer"]["models"], [])


if __name__ == "__main__":
    unittest.main()
