import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect


class Projection(unittest.TestCase):
    def sample(self):
        value = collect.unknown_snapshot(now=100, host="fixture")
        value.update(models=[{"id": "/private/models/test.gguf", "loadedState": "loaded",
                             "lane": "gpu-wrapper-101", "activity": "generating", "prompt": "private", "liveTokensPerSecond": 12}],
                     modelInventoryLanes=["pair-llamacpp", "gpu-wrapper-101"],
                     lanes=[{"id": "pair-llamacpp", "health": "up", "status": "serving"},
                            {"id": "gpu-wrapper-101", "health": "up", "status": "slots_unknown"}],
                     modelsComplete=True, boot={"bootId": "private"}, authority={"private": "state"})
        return value

    def test_allowlist_excludes_private_details_and_paths(self):
        value = collect.project_status(self.sample())
        self.assertEqual(value["models"][0]["id"], "test.gguf")
        self.assertNotIn("prompt", value["models"][0])
        self.assertNotIn("boot", value)
        self.assertNotIn("authority", value)
        self.assertEqual((value["platform"], value["kind"]), ("linux", "agiw-peer"))

    def test_unknown_never_becomes_zero_idle_or_down(self):
        value = collect.project_status(collect.unknown_snapshot(now=100))
        self.assertEqual(value["health"], "unknown")
        self.assertIsNone(value["modelsTotal"])
        self.assertIsNone(value["memory"]["availablePercent"])
        self.assertFalse(value["modelsComplete"])

    def test_caps_and_sorts_models_without_claiming_complete(self):
        value = self.sample()
        value["models"] = [{"id": str(i), "loadedState": "inactive"} for i in range(40)]
        value["models"][-1]["loadedState"] = "loaded"
        result = collect.project_status(value)
        self.assertEqual(len(result["models"]), 32)
        self.assertEqual(result["models"][0]["id"], "39")
        self.assertIsNone(result["modelsTotal"])

    def test_malformed_row_voids_complete_count(self):
        value = self.sample()
        value["models"].append("bad")
        self.assertFalse(collect.project_status(value)["modelsComplete"])

    def test_nonfinite_and_boolean_counters_are_unknown(self):
        value = self.sample()
        value["models"][0].update(slotsBusy=True, liveTokensPerSecond=float("nan"))
        value["gpus"] = [{"name": "Polaris", "memoryUsedMiB": float("inf")}]
        row = collect.project_status(value)
        self.assertIsNone(row["models"][0]["slotsBusy"])
        self.assertIsNone(row["models"][0]["liveTokensPerSecond"])
        self.assertIsNone(row["gpus"][0]["memoryUsedMiB"])

    def test_complete_inventory_count_is_explicit(self):
        value = collect.project_status(self.sample())
        self.assertEqual(value["modelsTotal"], 1)
        self.assertTrue(value["modelsComplete"])

    def test_unknown_residency_never_publishes_idle_or_zero_counters(self):
        value = self.sample()
        value["models"][0].update(loadedState="unknown", activity="idle", slotsBusy=0, liveTokensPerSecond=0)
        row = collect.project_status(value)["models"][0]
        self.assertEqual(row["activity"], "unknown")
        self.assertIsNone(row["slotsBusy"])
        self.assertIsNone(row["liveTokensPerSecond"])

    def test_sanitized_duplicate_ids_void_complete_count(self):
        value = self.sample()
        value["models"].append({"id": "/other/test.gguf", "loadedState": "loaded"})
        self.assertFalse(collect.project_status(value)["modelsComplete"])

    def test_component_reason_and_no_freeform_details(self):
        value = self.sample()
        value["components"] = [{"id": "heartbeatUnit", "health": "unknown", "reason": "stale", "detail": "private"}]
        self.assertEqual(collect.project_status(value)["components"],
                         [{"id": "heartbeatUnit", "state": "unknown", "reason": "stale"}])


class Collection(unittest.TestCase):
    def test_fixture_system_and_fetch_only_pair_allowlist(self):
        calls = []
        def fetch(url, *_):
            calls.append(url)
            return {"status": "ok"} if url.endswith("/health") else {"models": [{"id": "pair-model", "status": {"value": "unloaded"}}]}
        system = mock.Mock()
        system.snapshot.return_value = {"boot": {"bootId": "fixture"}, "gpus": [], "gpusComplete": True, "memory": {},
                                        "processes": [{"pid": 321, "kind": "pair-router", "startTimeTicks": 100}],
                                        "network": {"state": "up", "listeners": [{"local": "127.0.0.1", "localPort": 8081,
                                            "pid": 321, "startTimeTicks": 100, "ownershipVerified": True}]}}
        with mock.patch.object(collect.probes, "read_codex_state", return_value={}), \
                mock.patch.object(collect.probes, "read_pair_files", return_value={}), \
                mock.patch.object(collect.probes, "last_rates", return_value={}):
            collector = collect.Collector(system=system, fetch=fetch, clock=lambda: 100, host="fixture")
            first, second = collector.collect(), collector.collect()
        self.assertEqual(set(calls), {"http://127.0.0.1:8081/health", "http://127.0.0.1:8081/models"})
        self.assertEqual(first["health"], "up")
        self.assertEqual(second["models"][0]["loadedState"], "inactive")
        self.assertIsNone(second["models"][0]["liveTokensPerSecond"])
        self.assertFalse(second["modelsComplete"])
        self.assertIn("gpu-wrapper", [row["id"] for row in second["lanes"]])
        self.assertEqual(second["contention"]["state"], "unknown")
        self.assertEqual(system.snapshot.call_args.args[0], system.snapshot.return_value)

    def test_wrapper_rate_history_does_not_cross_pid(self):
        system = mock.Mock()
        system.snapshot.return_value = {"boot": {"bootId": "fixture"},
                                        "processes": [{"pid": 1, "startTimeTicks": 10}, {"pid": 2, "startTimeTicks": 11}]}
        lane = {"id": "gpu-wrapper", "pid": 1, "port": 9000, "status": "busy", "health": "up",
                "phase": "generating", "servedModel": "model", "slotsBusy": 1, "slotsTotal": 1,
                "slots": [{"id": 0, "taskId": 2, "nDecoded": 10, "processing": True}]}
        with mock.patch.object(collect.probes, "probe_pair", return_value={}), \
                mock.patch.object(collect.probes, "probe_wrapper", return_value=[lane]), \
                mock.patch.object(collect.probes, "coordinator_status", return_value={}), \
                mock.patch.object(collect.probes, "read_codex_state", return_value={}), \
                mock.patch.object(collect.probes, "read_pair_files", return_value={}), \
                mock.patch.object(collect.probes, "last_rates", return_value={}), \
                mock.patch.object(collect.lanes, "live_decode_rate", return_value=(None, {"task": "prior"})) as rate:
            collector = collect.Collector(system=system, clock=lambda: 100)
            collector.collect()
            lane["pid"] = 2
            collector.collect()
            self.assertEqual(rate.call_args.args[0], {})

    def test_pair_contention_is_observed_without_any_action(self):
        system = mock.Mock()
        system.snapshot.return_value = {"boot": {}, "processes": []}
        lane = {"id": "wrapper", "pid": 1, "port": 9000, "status": "busy", "health": "up", "slots": []}
        pair = {"models": [{"id": "p", "loadedState": "loaded"}]}
        with mock.patch.object(collect.probes, "probe_pair", return_value=pair), \
                mock.patch.object(collect.probes, "probe_wrapper", return_value=[lane]), \
                mock.patch.object(collect.probes, "read_codex_state", return_value={}), \
                mock.patch.object(collect.probes, "read_pair_files", return_value={"attribution": "PAIR says"}), \
                mock.patch.object(collect.probes, "last_rates", return_value={}):
            value = collect.Collector(system=system, clock=lambda: 100).collect()
        self.assertEqual(value["contention"]["state"], "detected")
        self.assertEqual(value["pairCluster"]["attribution"], "PAIR says")


if __name__ == "__main__":
    unittest.main()
