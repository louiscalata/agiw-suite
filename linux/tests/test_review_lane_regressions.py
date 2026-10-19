"""Regression coverage for unknown-safe Linux lane summaries."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect, lanes


class RateCompletenessTests(unittest.TestCase):
    def test_partial_busy_slot_sample_is_unknown(self):
        previous = {(0, 11): (10.0, 100.0)}
        lane = {"slots": [
            {"id": 0, "taskId": 11, "decodedTokens": 40, "isProcessing": True},
            {"id": 1, "taskId": 12, "decodedTokens": None, "isProcessing": True},
        ]}
        for changes in ({"decodedTokens": None}, {"taskId": None, "decodedTokens": 20},
                        {"taskId": 12, "decodedTokens": 20}):
            with self.subTest(changes=changes):
                lane["slots"][1].update(changes)
                rate, current = lanes.live_decode_rate(previous, lane, 101.0)
                self.assertIsNone(rate)
                self.assertEqual(current[(0, 11)], (40.0, 101.0))

    def test_all_busy_slots_need_two_poll_evidence(self):
        previous = {(0, 11): (10.0, 100.0), (1, 12): (5.0, 100.0)}
        lane = {"slots": [
            {"id": 0, "taskId": 11, "decodedTokens": 40, "isProcessing": True},
            {"id": 1, "taskId": 12, "decodedTokens": 20, "isProcessing": True},
        ]}
        rate, _ = lanes.live_decode_rate(previous, lane, 101.0)
        self.assertEqual(rate, 45.0)


class OllamaLaneTests(unittest.TestCase):
    def test_collects_passive_ollama_lane_from_correlated_process_listener(self):
        class System:
            def snapshot(self, previous=None):
                return {"processes": [{"pid": 23, "kind": "ollama", "name": "ollama", "startTimeTicks": 123}],
                        "network": {"state": "up", "listeners": [{"pid": 23, "name": "ollama",
                                                                       "localPort": 11435, "local": "127.0.0.1",
                                                                       "startTimeTicks": 123,
                                                                       "ownershipVerified": True}]},
                        "boot": {"bootId": "fixture"}}

        collector = collect.Collector(root="/", home="/nonexistent", system=System(),
                                      fetch=lambda *args: (_ for _ in ()).throw(AssertionError("no Ollama API")),
                                      clock=lambda: 100.0, host="fixture")
        with patch.object(collect.probes, "probe_pair", return_value={"models": []}), \
                patch.object(collect.probes, "probe_wrapper", return_value=[]), \
                patch.object(collect.probes, "coordinator_status", return_value={"id": "coordinator"}), \
                patch.object(collect.probes, "read_codex_state", return_value={}), \
                patch.object(collect.probes, "read_pair_files", return_value={}), \
                patch.object(collect.probes, "last_rates", return_value={}):
            snapshot = collector.collect()
        row = next(lane for lane in snapshot["lanes"] if lane.get("id") == "pair-ollama")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["health"], "unknown")
        self.assertTrue(row["processPresent"])
        self.assertTrue(row["listenerPresent"])
        self.assertEqual(row["reason"], "application-health-unverified")
        self.assertEqual(row["activity"], "unknown")
        self.assertIsNone(row["liveTokensPerSecond"])

    def test_ollama_lane_stays_unknown_without_listener_evidence(self):
        class System:
            def snapshot(self, previous=None):
                return {"processes": [{"pid": 23, "kind": "ollama", "name": "ollama", "startTimeTicks": 123}],
                        "network": {"state": "up", "listeners": []}, "boot": {"bootId": "fixture"}}

        collector = collect.Collector(root="/", home="/nonexistent", system=System(), fetch=lambda *args: None,
                                      clock=lambda: 100.0, host="fixture")
        with patch.object(collect.probes, "probe_pair", return_value={"models": []}), \
                patch.object(collect.probes, "probe_wrapper", return_value=[]), \
                patch.object(collect.probes, "coordinator_status", return_value={"id": "coordinator"}), \
                patch.object(collect.probes, "read_codex_state", return_value={}), \
                patch.object(collect.probes, "read_pair_files", return_value={}), \
                patch.object(collect.probes, "last_rates", return_value={}):
            snapshot = collector.collect()
        row = next(lane for lane in snapshot["lanes"] if lane.get("id") == "pair-ollama")
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["health"], "unknown")
        self.assertIsNone(row["liveTokensPerSecond"])


if __name__ == "__main__":
    unittest.main()
