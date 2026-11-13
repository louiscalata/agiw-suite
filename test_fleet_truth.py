"""Offline regressions for fleet evidence, CLI projection."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agiw_status
from fleet_state import build_fleet_state
import remote_machines as remote
from test_fleet_servers import MacStore, WindowsStore

ROOT = Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT / "test_support/fleet-state.json").read_text())
NOW = FIXTURE["now"]
STAMP = FIXTURE["snapshot"]["inference"]["observedAt"]


def lane(reason, **extra):
    return {"id": "example:lane", "machine": "mac", "role": "llama",
            "label": "Example lane", "health": "not_measured", "modelId": None,
            "observedAt": STAMP, "reason": reason, **extra}


class FleetTruthTests(unittest.TestCase):
    def test_unverified_identity_is_unknown_not_unavailable(self):
        for reason in ("inventory_unverified", "load_state_unreported", "identity_unknown",
                       "multiple_loaded", "model_unreported", "model_not_loaded",
                       "no_models", "models_unreadable", "future_reason"):
            with self.subTest(reason=reason):
                fleet = build_fleet_state({"inference": {"nodes": [lane(reason)]}}, now=NOW)
                fact = next(f for f in fleet["facts"] if f["scope"] == "lane")
                self.assertEqual((fact["state"], fact["completeness"]), ("unknown", "unknown"))
                self.assertIsNone(fleet["counts"]["answering"])
                self.assertEqual(fleet["counts"]["unavailable"], 0)
                self.assertEqual(fleet["completeness"], "partial")

    def test_answered_identity_unknown_requires_response_evidence(self):
        for reason in ("load_state_unreported", "identity_unknown", "multiple_loaded"):
            with self.subTest(reason=reason):
                fleet = build_fleet_state({"inference": {"nodes": [lane(reason)]}}, now=NOW)
                fact = next(f for f in fleet["facts"] if f["scope"] == "lane")
                self.assertIs(fact["evidence"].get("endpointAnswered"), True)
                self.assertIn("answered, identity unknown", fleet["summary"])
        fleet = build_fleet_state({"inference": {"nodes": [lane("inventory_unverified")]}}, now=NOW)
        self.assertNotIn("answered, identity unknown", fleet["summary"])

    def test_inconclusive_probe_reasons_remain_unknown(self):
        for reason in ("incomplete_body", "oversize", "invalid_utf8", "missing_length",
                       "ambiguous_framing", "invalid_length", "probe_failed",
                       "url_not_loopback", "collector_failed"):
            with self.subTest(reason=reason):
                fleet = build_fleet_state({"inference": {"nodes": [lane(reason)]}}, now=NOW)
                fact = next(f for f in fleet["facts"] if f["scope"] == "lane")
                self.assertEqual((fact["state"], fact["completeness"], fact["reason"]),
                                 ("unknown", "unknown", reason))
                self.assertNotIn("endpointAnswered", fact["evidence"])
                self.assertIsNone(fleet["counts"]["answering"])
                self.assertEqual(fleet["counts"]["unavailable"], 0)
                self.assertEqual(fleet["counts"]["unknown"], 1)
                self.assertEqual(fleet["completeness"], "partial")
                self.assertNotIn("answered, identity unknown", fleet["summary"])

    def test_registry_multiple_models_answered_but_fetch_error_does_not(self):
        config = {"schemaVersion": 1, "machines": [{"id": "example", "label": "Example",
                  "lanes": [{"id": "lane", "label": "Example lane", "localPort": 21001}]}]}
        for fetch, answered in ((lambda _: (200, '{"data":[{"id":"a"},{"id":"b"}]}', 1), True),
                                (lambda _: (_ for _ in ()).throw(ValueError("fetch failed")), False)):
            with self.subTest(answered=answered):
                nodes = remote.collect(config, fetch=fetch, now=NOW)
                fleet = build_fleet_state({"inference": {"nodes": nodes}}, now=NOW)
                fact = next(f for f in fleet["facts"] if f["scope"] == "lane")
                self.assertEqual(fact["state"], "unknown")
                self.assertEqual(fact["evidence"].get("endpointAnswered") is True, answered)

    def test_unconfigured_lane_is_not_in_endpoint_coverage(self):
        fleet = build_fleet_state({"inference": {"nodes": [lane("not_configured")]}}, now=NOW)
        self.assertEqual(fleet["counts"]["lanes"], 0)
        self.assertIsNone(fleet["counts"]["answering"])
        self.assertFalse(any(f["scope"] == "lane" for f in fleet["facts"]))

    def test_actual_transport_failures_remain_endpoint_scoped(self):
        for reason in ("refused", "timeout", "unavailable", "upstream_error", "tunnel_down"):
            with self.subTest(reason=reason):
                fleet = build_fleet_state({"inference": {"nodes": [lane(reason)]}}, now=NOW)
                self.assertEqual(fleet["counts"]["unavailable"], 1)
                self.assertEqual(fleet["counts"]["answering"], 0)

    def test_invalid_registry_is_visible_and_partial_in_both_api_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text("{")
            with self.assertLogs("remote_machines", level="WARNING"):
                config = remote.load_config(path)
        document = {"nodes": [lane("timeout")], "edges": [], "observedAt": STAMP}
        remote.extend_map(document, config, [])
        snapshot = {"sampledAt": NOW, "inference": document}
        for store_class in (MacStore, WindowsStore):
            with self.subTest(edition=store_class.__module__):
                store = store_class()
                store.snapshot = copy.deepcopy(snapshot)
                with patch("time.time", return_value=NOW):
                    result = store.read()
                fleet = result["fleetState"]
                self.assertEqual(fleet["registry"]["reason"], "registry_invalid")
                self.assertEqual(fleet["completeness"], "partial")
                self.assertIsNone(fleet["counts"]["answering"])
                self.assertIn("registry invalid", fleet["summary"])
                self.assertEqual(result["inference"]["fleetState"], fleet)

    def test_cli_accepts_projected_registry_without_promoting_host_health(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                snapshot = copy.deepcopy(FIXTURE["snapshot"])
                # Use the existing public Mac fixture and a never-observed Windows placeholder.
                snapshot["machines"] = [dict(snapshot["machines"][0], reasonCode="observed", ageSeconds=1),
                    agiw_status.machine_summary("windows", "windows", now=NOW)]
                if not replacement:
                    snapshot["inference"]["nodes"][1].pop("replaces")
                remote.project_snapshot(snapshot)
                result = agiw_status._summary(snapshot, NOW)
                self.assertEqual(result["machines"][0]["state"], "up")
                self.assertNotIn("schema_invalid", json.dumps(result))
                self.assertIn("fleetState", result)
                self.assertEqual(result["fleetState"]["counts"]["answering"], 3)
                registry = [r for r in result["machines"] if r.get("observationSource") == "registry"]
                self.assertEqual(len(registry), 2)
                self.assertTrue(all(r["state"] == "unknown" for r in registry))
                self.assertTrue(all(r["loadedCount"] is None for r in registry))
                self.assertEqual(any(r["platform"] == "windows" for r in result["machines"]), not replacement)

    def test_cli_registry_cannot_bypass_normal_host_validation(self):
        snapshot = copy.deepcopy(FIXTURE["snapshot"])
        snapshot["machines"] = [dict(snapshot["machines"][0], reasonCode="observed", ageSeconds=1),
                               {"host": "invalid", "platform": "remote", "observationSource": "registry",
                                "replaces": "legacy-pc"}]
        with self.assertRaises(agiw_status.StatusUnavailable):
            agiw_status._summary(snapshot, NOW)



if __name__ == "__main__":
    unittest.main()
