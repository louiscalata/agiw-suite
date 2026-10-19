"""Collector boundaries: an unread adapter stays unknown and PAIR-owned ports are never polled."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, winsys  # noqa: E402
from agiw_win.collect import Collector, LanePoller, merge_adapters  # noqa: E402


NVIDIA = {"index": 0, "name": "NVIDIA GeForce RTX 3080 Ti", "utilizationPercent": 3.0,
          "memoryUsedMiB": 1000.0, "memoryTotalMiB": 12288.0, "temperatureC": 35.0, "powerW": 40.0}
ADAPTERS = [{"name": NVIDIA["name"], "memoryBytes": 12288 * 2 ** 20},
            {"name": "AMD Radeon RX 5700 XT", "memoryBytes": 8176 * 2 ** 20}]
FAST = {"id": "fast", "port": 1234, "alias": "test-model", "device": "CUDA0"}
LIVE = {"id": "lane-processes", "label": "Lane processes", "state": "live", "ageSeconds": 0.0, "detail": "fixture"}


class CollectorSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def collector(self, *, gpus=None, adapters=None, processes=None, fetch=None, interval=0.05):
        census = mock.Mock()
        census.sample.return_value = {"ok": True, "processes": [], "listeners": [], "uninspected": 0}
        collector = Collector(self.root, self.root / "bin", runs_root=self.root / "runs", host="test-pc",
                              clock=lambda: 1000.0, lane_processes=processes, pair_census=census,
                              fetch=fetch or mock.Mock(side_effect=AssertionError("unexpected fetch")),
                              gpu_fn=lambda: (gpus if gpus is not None else [dict(NVIDIA)],
                                              probes._source("pc-gpu", "PC GPU", "live", "fixture")),
                              adapters_fn=lambda: adapters if adapters is not None else ADAPTERS,
                              memory_fn=lambda: ({"level": "ok"}, None), notification_fn=lambda: 5,
                              lane_interval=interval, lane_wait=0.2)
        self.addCleanup(collector.close)
        return collector

    def configure(self):
        folder = self.root / "windows-llm-pipeline"
        folder.mkdir()
        (folder / "config.json").write_text(json.dumps({"runtime": {"lanes": {"fast": FAST}}}))

    @staticmethod
    def fetch(url, _timeout, _limit):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/v1/models"):
            return {"data": [{"id": FAST["alias"]}]}
        if url.endswith("/slots"):
            return [{"id": 0, "is_processing": False}]
        raise AssertionError(url)

    @staticmethod
    def processes(owner="agiw", *, known=True):
        processes = mock.Mock()
        processes.sample.return_value = ({"fast": {"pid": 77, "name": "llama-server.exe", "owner": owner,
                                                   "ownerKnown": known, "created": 1}}, [], LIVE)
        return processes

    def test_missing_5700_counter_reading_prevents_clear_in_snapshot_and_publication(self):
        collector = self.collector()
        snapshot = collector.collect()
        self.assertEqual([g["name"] for g in snapshot["pcGpus"]], [a["name"] for a in ADAPTERS])
        unread = snapshot["pcGpus"][1]
        self.assertIsNone(unread["utilizationPercent"])
        self.assertEqual((unread["source"], unread["memoryTotalMiB"]), ("adapter-inventory", 8176))
        self.assertEqual((snapshot["handsOff"]["active"], snapshot["handsOff"]["state"]), (None, "unknown"))
        self.assertEqual(snapshot["handsOff"]["unknown"], ["gpu-load-unknown"])
        self.assertIn("GPU load unavailable for AMD Radeon RX 5700 XT",
                      next(s["detail"] for s in snapshot["sources"] if s["id"] == "pc-gpu"))
        self.assertEqual(probes.pc_status(snapshot)["handsOff"]["state"], "unknown")
        self.assertEqual(probes.peer_presence(snapshot, "test-pc")["handsOff"]["state"], "unknown")

    def test_counter_sampling_failure_cannot_hide_unread_adapter(self):
        collector = self.collector()
        collector.gpu_counters = mock.Mock()
        collector.gpu_counters.sample.side_effect = OSError("PDH unavailable")
        snapshot = collector.collect()
        self.assertEqual(snapshot["handsOff"]["unknown"], ["gpu-load-unknown"])
        self.assertIsNone(snapshot["handsOff"]["active"])
        self.assertEqual(len(snapshot["pcGpus"]), 2)

    def test_counter_initialization_failure_keeps_unread_adapter_unknown(self):
        collector = self.collector()
        collector._gpu_counters_failed = False
        with mock.patch.object(winsys, "IS_WINDOWS", True), \
                mock.patch.object(winsys, "GpuCounters", side_effect=OSError("PDH unavailable"), create=True) as counters:
            snapshot = collector.collect()
            collector._gpus(1003.0)
        counters.assert_called_once_with()
        self.assertIsNone(snapshot["handsOff"]["active"])
        self.assertEqual(snapshot["handsOff"]["unknown"], ["gpu-load-unknown"])
        self.assertEqual(len(snapshot["pcGpus"]), 2)

    def test_partial_counter_inventory_keeps_missing_adapter_unknown(self):
        collector = self.collector()
        collector.gpu_counters = mock.Mock()
        collector.gpu_counters.sample.return_value = [{"name": NVIDIA["name"], "vendor": "nvidia", "luid": "nvidia",
                                                      "utilizationPercent": 3.0, "memoryUsedMiB": 1000,
                                                      "memoryTotalMiB": 12288}]
        snapshot = collector.collect()
        self.assertEqual(len(snapshot["pcGpus"]), 2)
        self.assertIsNone(snapshot["handsOff"]["active"])

    def test_known_busy_gpu_still_sets_hands_off_when_other_adapter_is_unread(self):
        collector = self.collector(gpus=[{**NVIDIA, "utilizationPercent": 90.0}])
        snapshot = collector.collect()
        self.assertIs(snapshot["handsOff"]["active"], True)
        self.assertIn("gpu-load-unknown", snapshot["handsOff"]["unknown"])
        self.assertIn("gpu-busy", snapshot["handsOff"]["reasons"])

    def test_complete_readings_are_not_duplicated_and_can_be_clear(self):
        collector = self.collector(gpus=[NVIDIA, {**NVIDIA, "name": ADAPTERS[1]["name"], "index": 1}])
        snapshot = collector.collect()
        self.assertEqual(len(snapshot["pcGpus"]), 2)
        self.assertIs(snapshot["handsOff"]["active"], False)

    def test_native_inventory_failure_cannot_claim_every_gpu_is_clear(self):
        collector = self.collector(adapters=[])
        with mock.patch.object(winsys, "IS_WINDOWS", True):
            snapshot = collector.collect()
        self.assertIsNone(snapshot["handsOff"]["active"])
        self.assertIn("gpu-load-unknown", snapshot["handsOff"]["unknown"])

    def test_incomplete_counter_process_shares_survive_nvidia_merge(self):
        counters = [{"name": NVIDIA["name"], "vendor": "nvidia", "luid": "nvidia",
                     "utilizationPercent": 90.0, "memoryUsedMiB": 1000, "memoryTotalMiB": 12288,
                     "engineShares": [{"pid": 77, "percent": 90.0}], "engineSharesComplete": False}]
        row = merge_adapters([{**NVIDIA, "utilizationPercent": 90.0}], counters)[0]
        self.assertIs(row["engineSharesComplete"], False)
        hands = probes.hands_off([row], gpu_live=True, lane_pids=frozenset({77}), notification_state=5)
        self.assertEqual(hands["cause"], "unknown")

    def test_pair_owner_is_checked_before_first_request(self):
        self.configure()
        processes = self.processes("pair")
        fetch = mock.Mock(side_effect=self.fetch)
        collector = self.collector(processes=processes, fetch=fetch)
        snapshot = collector.collect()
        self.assertEqual(snapshot["lanes"][0]["status"], "not_agiw")
        processes.sample.assert_called_once_with({1234: "fast"})
        self.assertFalse(collector._pollers)
        fetch.assert_not_called()

    def test_pair_parks_existing_poller_until_fresh_non_pair_owner(self):
        self.configure()
        processes = self.processes()
        fetch = mock.Mock(side_effect=self.fetch)
        collector = self.collector(processes=processes, fetch=fetch)
        self.assertEqual(collector.collect()["lanes"][0]["status"], "idle")
        processes.sample.return_value = self.processes("pair").sample.return_value
        self.assertEqual(collector.collect()["lanes"][0]["status"], "not_agiw")
        count = fetch.call_count
        time.sleep(0.15)
        self.assertEqual(fetch.call_count, count)
        processes.sample.return_value = ({}, [], {**LIVE, "state": "unavailable"})
        self.assertEqual(collector.collect()["lanes"][0]["status"], "unknown")
        time.sleep(0.1)
        self.assertEqual(fetch.call_count, count)
        processes.sample.return_value = self.processes().sample.return_value
        self.assertEqual(collector.collect()["lanes"][0]["status"], "idle")
        self.assertGreater(fetch.call_count, count)

    def test_uninspected_owner_never_starts_polling(self):
        self.configure()
        fetch = mock.Mock(side_effect=self.fetch)
        collector = self.collector(processes=self.processes(known=False), fetch=fetch)
        snapshot = collector.collect()
        self.assertEqual(snapshot["lanes"][0]["status"], "unknown")
        self.assertIn("ownership unavailable", snapshot["lanes"][0]["detail"])
        fetch.assert_not_called()

    def test_failed_initial_ownership_read_never_starts_polling(self):
        self.configure()
        processes = self.processes()
        processes.sample.side_effect = OSError("TCP table unavailable")
        fetch = mock.Mock(side_effect=self.fetch)
        snapshot = self.collector(processes=processes, fetch=fetch).collect()
        self.assertEqual(snapshot["lanes"][0]["status"], "unknown")
        fetch.assert_not_called()

    def test_in_flight_fetch_cannot_continue_or_publish_after_parking(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def fetch(url, timeout, limit):
            calls.append(url)
            entered.set()
            release.wait(2.0)
            return self.fetch(url, timeout, limit)

        poller = LanePoller(FAST, fetch, lambda: 1000.0, interval=0.05)
        self.addCleanup(poller.stop)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(1.0))
        poller.set_polling(False)
        release.set()
        time.sleep(0.15)
        self.assertEqual(calls, ["http://127.0.0.1:1234/health"])
        self.assertIsNone(poller.read())


if __name__ == "__main__":
    unittest.main()
