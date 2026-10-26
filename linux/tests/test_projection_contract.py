import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect, server


class DetailProjection(unittest.TestCase):
    def snapshot(self):
        value = collect.unknown_snapshot(now=100, host="fixture")
        value["gpus"] = [{"name": "AMD GPU (1002:67df)", "utilizationPercent": 25,
                          "memoryUsedMiB": 256, "memoryTotalMiB": 8192, "temperatureC": None, "powerW": 37,
                          "sources": {"utilizationPercent": "/sys/class/drm/card7/device/gpu_busy_percent",
                                      "memoryUsedMiB": "/sys/class/drm/card7/device/mem_info_vram_used",
                                      "memoryTotalMiB": "/sys/class/drm/card7/device/mem_info_vram_total",
                                      "powerW": "/sys/class/drm/card7/device/hwmon/hwmon12/power1_input"}}]
        value["historicalRates"] = [{"role": "code", "kind": "last-measured", "observedUnix": 90,
                                     "ageSeconds": 0, "promptTokensPerSecond": 80, "generationTokensPerSecond": 31}]
        value["network"] = {"state": "up", "callersState": "known",
                            "listeners": [{"localPort": 14318, "name": "nvpair-node-info", "exposed": True,
                                           "plaintext": True, "local": "192.0.2.10", "remote": "198.51.100.8", "pid": 42}],
                            "callers": [{"name": "codex", "lane": "pair-llamacpp", "connections": 1, "pid": 43}]}
        return value

    def test_gpu_keeps_each_measurement_source_and_missing_value(self):
        value = self.snapshot()
        row = collect.project_status(value)["gpus"][0]
        for key in ("utilizationPercent", "memoryUsedMiB", "memoryTotalMiB", "powerW"):
            self.assertEqual(row[key], value["gpus"][0][key])
            self.assertEqual(row["sources"][key], value["gpus"][0]["sources"][key])
        self.assertIsNone(row["temperatureC"])
        self.assertIsNone(row["sources"]["temperatureC"])

    def test_gpu_source_accepts_only_its_exact_kernel_field(self):
        for source in ("/private/fixture-secret", "/sys/class/drm/card7/device/../secret",
                       "/sys/class/drm/card7/device/mem_info_vram_used", "token=fixture-secret"):
            value = self.snapshot()
            value["gpus"][0]["sources"]["powerW"] = source
            projected = collect.project_status(value)
            self.assertIsNone(projected["gpus"][0]["sources"]["powerW"])
            self.assertNotIn("fixture-secret", json.dumps(projected))

    def test_historical_rates_keep_role_age_and_unverified_model_label(self):
        row = collect.project_status(self.snapshot())["historicalRates"][0]
        self.assertEqual((row["kind"], row["observedUnix"], row["ageSeconds"]), ("last-measured", 90, 10))
        self.assertEqual((row["promptTokensPerSecond"], row["generationTokensPerSecond"]), (80, 31))
        self.assertIsNone(row["model"])
        self.assertEqual(row["modelLabelSource"], "role-map-unverified")

    def test_invalid_historical_observation_never_becomes_live_or_zero(self):
        for change in ({"observedUnix": 101}, {"observedUnix": None}, {"kind": "live"},
                       {"promptTokensPerSecond": True, "generationTokensPerSecond": float("inf")}):
            value = self.snapshot()
            value["historicalRates"][0].update(change)
            row = collect.project_status(value)["historicalRates"][0]
            self.assertEqual(row["kind"], "unknown")
            self.assertIsNone(row["ageSeconds"])
            self.assertIsNone(row["promptTokensPerSecond"])
            self.assertIsNone(row["generationTokensPerSecond"])

    def test_network_preserves_ports_names_and_exposure_without_addresses_or_pids(self):
        row = collect.project_status(self.snapshot())["network"]
        self.assertEqual(row["listeners"], [{"localPort": 14318, "name": "nvpair-node-info",
                                             "exposed": True, "plaintext": True}])
        self.assertEqual(row["callers"], [{"name": "codex", "lane": "pair-llamacpp", "connections": 1}])
        self.assertEqual((row["state"], row["callersState"]), ("up", "known"))
        self.assertNotIn("192.0.2.10", json.dumps(row))
        self.assertNotIn("pid", json.dumps(row))

    def test_new_details_are_allowlisted_and_bounded(self):
        value = self.snapshot()
        for row in (value["historicalRates"][0], value["network"]["listeners"][0], value["network"]["callers"][0]):
            row.update(prompt="fixture-secret", path="/private/fixture-secret", arguments=["fixture-secret"])
        value["historicalRates"][0].update(model="fixture-secret", expectedModel="fixture-secret",
                                            source="fixture-secret", reason="fixture-secret")
        value["gpus"][0]["sources"]["private"] = "fixture-secret"
        value["historicalRates"] *= 100
        value["network"]["listeners"] *= 100
        value["network"]["callers"] *= 100
        projected = collect.project_status(value)
        self.assertEqual(len(projected["historicalRates"]), 1)
        self.assertEqual(len(projected["network"]["listeners"]), 32)
        self.assertEqual(len(projected["network"]["callers"]), 32)
        self.assertEqual(projected["network"]["state"], "unknown")
        self.assertEqual(projected["network"]["callersState"], "unknown")
        self.assertNotIn("fixture-secret", json.dumps(projected))
        self.assertLessEqual(len(server.encode(projected)), 65536)

    def test_invalid_network_rows_and_names_cannot_claim_complete_callers(self):
        for change in ({"name": "/private/codex"}, {"lane": "private-lane"}, {"connections": True}):
            value = self.snapshot()
            value["network"]["callers"][0].update(change)
            row = collect.project_status(value)["network"]
            self.assertEqual(row["callersState"], "unknown")
            self.assertNotIn("/private", json.dumps(row))
        value = self.snapshot()
        value["network"]["listeners"][0]["localPort"] = True
        self.assertEqual(collect.project_status(value)["network"]["state"], "unknown")

    def test_missing_network_stays_unknown(self):
        row = collect.project_status(collect.unknown_snapshot(now=100))["network"]
        self.assertEqual((row["state"], row["callersState"]), ("unknown", "unknown"))

    def test_release_identity_only_accepts_full_lowercase_commit(self):
        for release in ("a" * 40, "b" * 64):
            value = self.snapshot()
            value["releaseCommit"] = release
            self.assertEqual(collect.project_status(value)["releaseCommit"], release)
        for release in ("a" * 39, "B" * 40, "/private/fixture-secret", True):
            value = self.snapshot()
            value["releaseCommit"] = release
            self.assertIsNone(collect.project_status(value)["releaseCommit"])


class ProjectionConsumer(unittest.TestCase):
    snapshot = DetailProjection.snapshot

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"

    def test_cached_once_preserves_details_without_refreshing_age(self):
        value = self.snapshot()
        value["releaseCommit"] = "a" * 40
        projected = collect.project_status(value)
        server.write_status(self.state, projected)
        collector = mock.Mock()
        result = json.loads(server.once(collector, state_dir=self.state, clock=lambda: 105))
        collector.collect.assert_not_called()
        self.assertEqual(result, projected)
        self.assertEqual(result["historicalRates"][0]["ageSeconds"], 10)

    def test_cache_consumption_revalidates_nested_extension_values(self):
        projected = collect.project_status(self.snapshot())
        projected["gpus"][0]["sources"]["powerW"] = "/private/fixture-secret"
        projected["historicalRates"][0].update(source="fixture-secret", expectedModel="fixture-secret")
        projected["network"]["callers"][0].update(name="/private/fixture-secret", arguments="fixture-secret")
        server.write_status(self.state, projected)
        result = server.cached_status(self.state / "linux-status.json", 105)
        self.assertIsNotNone(result)
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(result["network"]["callersState"], "unknown")

    def test_cache_rejects_malformed_extension_shapes(self):
        for change in ({"historicalRates": {}}, {"historicalRates": [None]}, {"historicalRates": [{}, {}, {}]},
                       {"network": []}, {"network": {"listeners": "bad", "callers": []}},
                       {"network": {"listeners": [], "callers": [None]}}):
            value = collect.project_status(self.snapshot())
            value.update(copy.deepcopy(change))
            server.write_status(self.state, value)
            self.assertIsNone(server.cached_status(self.state / "linux-status.json", 105))


if __name__ == "__main__":
    unittest.main()
