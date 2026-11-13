"""Bounded CLI projections retain the server's fleet evidence decisions."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import agiw_status
from fleet_state import build_fleet_state
import remote_machines
from test_agiw_status import snapshot as legacy_snapshot


FIXTURE = json.loads((Path(__file__).parent / "test_support/fleet-state.json").read_text())
NOW = FIXTURE["now"]


def projected_snapshot():
    snapshot = copy.deepcopy(FIXTURE["snapshot"])
    snapshot["machines"] = [
        dict(snapshot["machines"][0], reasonCode="observed", ageSeconds=1),
        agiw_status.machine_summary("windows", "windows", now=NOW),
    ]
    remote_machines.project_snapshot(snapshot)
    return snapshot


class FleetTruthCliTests(unittest.TestCase):
    def test_registry_json_has_allowlisted_rows_and_compact_fleet_shape(self):
        snapshot = projected_snapshot()
        snapshot["inference"]["remoteMachines"] = {"state": "invalid", "detail": "private diagnostic"}
        snapshot["fleetState"] = build_fleet_state(snapshot, now=NOW)
        allowed = {"id", "host", "platform", "label", "observationSource", "replaces", "observedAt"}
        normalized = {"state", "reasonCode", "modelsComplete", "loadedCount", "busyCount",
                      "promptTokensPerSecond", "generationTokensPerSecond", "handsOff"}
        originals = {}
        for row in snapshot["machines"]:
            if row.get("observationSource") == "registry":
                # Future producer fields and large lane evidence must not enlarge this summary.
                row.update(health="ok", reason="raw producer reason", extraEvidence="x" * 10000)
                originals[row["id"]] = copy.deepcopy(row)
        result = agiw_status._summary(snapshot, NOW)
        for row in result["machines"]:
            if row.get("observationSource") != "registry":
                continue
            original = originals[row["id"]]
            self.assertEqual(set(row), (set(original) & allowed) | normalized)
            self.assertEqual({key: row[key] for key in allowed if key in row},
                             {key: original[key] for key in allowed if key in original})
            self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "host_not_measured"))
            self.assertIsNone(row["loadedCount"])
            self.assertIsNone(row["busyCount"])
        fleet = result["fleetState"]
        self.assertEqual(set(fleet), {"summary", "counts", "completeness", "registry"})
        self.assertEqual(fleet["registry"], {"reason": "registry_invalid"})
        self.assertNotIn("facts", fleet)
        encoded = json.dumps(result, separators=(",", ":"))
        self.assertLess(len(encoded.encode("utf-8")), 4096)
        self.assertNotIn("raw producer reason", encoded)
        self.assertNotIn("private diagnostic", encoded)

    def test_cli_preserves_already_aged_server_fleet_without_rebuilding(self):
        snapshot = projected_snapshot()
        served = build_fleet_state(snapshot, now=NOW + 10)
        self.assertGreater(served["counts"]["stale"], 0)
        snapshot["fleetState"] = served
        # A server read ages the original facts before legacy expiry clears readings.
        # The CLI must keep those decisions even if the remaining snapshot differs.
        snapshot["inference"]["nodes"] = []
        with patch.object(agiw_status, "build_fleet_state", side_effect=AssertionError("rebuilt served fleet")):
            result = agiw_status._summary(snapshot, NOW + 10)
        self.assertEqual(result["fleetState"], {key: served[key]
                         for key in ("summary", "counts", "completeness")})

    def test_cli_builds_fleet_only_when_server_contract_is_absent(self):
        snapshot = projected_snapshot()
        expected = build_fleet_state(snapshot, now=NOW)
        with patch.object(agiw_status, "build_fleet_state", wraps=build_fleet_state) as build:
            result = agiw_status._summary(snapshot, NOW)
        build.assert_called_once_with(snapshot, now=NOW)
        self.assertEqual(result["fleetState"], {key: expected[key]
                         for key in ("summary", "counts", "completeness")})

    def test_cli_can_project_server_fleet_without_registry_membership(self):
        snapshot = legacy_snapshot()
        snapshot["fleetState"] = {"summary": "Endpoint coverage unknown; no lane evidence",
                                  "counts": {"lanes": 0, "answering": None},
                                  "completeness": "partial", "facts": [{"evidence": "omit"}]}
        with patch.object(agiw_status, "build_fleet_state", side_effect=AssertionError("rebuilt served fleet")):
            result = agiw_status._summary(snapshot, 1000)
        self.assertEqual(result["fleetState"], {key: snapshot["fleetState"][key]
                         for key in ("summary", "counts", "completeness")})

    def test_legacy_snapshot_without_fleet_or_registry_keeps_existing_shape(self):
        snapshot = legacy_snapshot()
        with patch.object(agiw_status, "build_fleet_state", side_effect=AssertionError("rebuilt legacy fleet")):
            result = agiw_status._summary(snapshot, 1000)
        self.assertEqual(result, {"schemaVersion": 1, "sampledAt": 1000, "machines": snapshot["machines"]})


if __name__ == "__main__":
    unittest.main()
