"""Socket-free contracts shared by Python, the machine UI, and the native badge."""
import copy
import json
from concurrent.futures import Future
from pathlib import Path
import unittest
from unittest import mock

from fleet_state import build_fleet_state
from inference_stats import MAP_SCHEMA, public_map
import remote_machines as remote


FIXTURE = json.loads((Path(__file__).parent / "test_support/fleet-state.json").read_text())


class FleetStateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = copy.deepcopy(FIXTURE["snapshot"])
        self.now = FIXTURE["now"]

    def fleet(self, seconds=0):
        return build_fleet_state(self.snapshot, now=self.now + seconds)

    def test_shared_fixture_is_the_production_projection(self):
        self.assertEqual(self.fleet(), FIXTURE["fleetState"])

    def test_three_declared_machines_and_unmatched_peer_keep_exact_identity(self):
        facts = self.fleet()["facts"]
        machines = {fact["id"]: fact for fact in facts if fact["scope"] == "machine"}
        self.assertEqual(set(machines), {"mac", "registry:pc", "registry:pro", "peer:spare"})
        self.assertTrue(machines["peer:spare"]["evidence"]["unmatchedPeer"])
        self.assertEqual(machines["registry:pro"]["state"], "unknown")
        self.assertEqual(machines["registry:pc"]["state"], "unknown", "answering endpoints are not host health")

    def test_required_provenance_is_present_on_every_fact(self):
        fields = {"id", "scope", "source", "observedAt", "expiresAt", "completeness", "reason", "state", "ageSeconds"}
        for fact in self.fleet()["facts"]:
            self.assertLessEqual(fields, set(fact))

    def test_incomplete_activity_totals_are_unknown_never_zero(self):
        fleet = self.fleet()
        self.assertIsNone(fleet["counts"]["loaded"])
        self.assertIsNone(fleet["counts"]["busy"])
        self.assertEqual((fleet["counts"]["answering"], fleet["counts"]["lanes"]), (3, 4))
        self.assertIn("Mac Pro deep (registry:pro:deep)", fleet["summary"])

    def test_one_failed_lane_neither_blocks_nor_blesses_aggregate(self):
        lane = self.snapshot["inference"]["nodes"][4]
        lane.update(modelId=None, health="not_measured", reason="timeout")
        fleet = self.fleet()
        self.assertEqual(fleet["state"], "coverage")
        self.assertEqual((fleet["counts"]["answering"], fleet["counts"]["unavailable"]), (2, 1))
        self.assertNotIn("blocked", fleet["summary"].lower())
        self.assertNotIn(" ok", fleet["summary"].lower())

    def test_facts_expire_on_their_own_clock(self):
        facts = {fact["id"]: fact for fact in self.fleet(4)["facts"]}
        self.assertEqual(facts["mac:lmstudio"]["state"], "stale")
        self.assertEqual(facts["registry:pc:fast"]["state"], "endpoint answering")
        self.assertEqual(facts["peer:spare"]["state"], "reported")
        self.assertEqual(facts["mac:lmstudio"]["ageSeconds"], 5)
        self.assertEqual(facts["mac:lmstudio"]["evidence"]["modelId"], "local/model", "retained history stays explicitly stale")
        self.assertEqual(self.fleet(7)["counts"]["stale"], 3)
        self.assertIsNone(self.fleet(7)["counts"]["answering"])
        self.assertTrue(self.fleet(7)["summary"].startswith("Answering count unknown; 4 lanes configured"))

    def test_zero_answering_requires_complete_current_unavailable_evidence(self):
        for node in self.snapshot["inference"]["nodes"]:
            if node.get("role") == "llama":
                node.update(health="not_measured", modelId=None, reason="timeout",
                            observedAt=self.snapshot["inference"]["observedAt"])
        fleet = self.fleet()
        self.assertEqual(fleet["counts"]["answering"], 0)
        self.assertTrue(fleet["summary"].startswith("0 of 4 lanes answering"))

    def test_new_envelope_does_not_renew_old_fact(self):
        self.snapshot["sampledAt"] += 100
        self.snapshot["inference"]["observedAt"] = "2099-01-01T00:00:00Z"
        self.assertEqual(self.fleet(7)["counts"]["stale"], 3)

    def test_missing_probe_timestamp_stays_unknown_even_with_model_id(self):
        self.snapshot["inference"]["nodes"][4]["observedAt"] = None
        fleet = self.fleet()
        fact = next(item for item in fleet["facts"] if item["id"] == "registry:pc:fast")
        self.assertEqual(fact["state"], "unknown")
        self.assertIsNone(fact["expiresAt"])

    def test_untrusted_clock_is_unknown(self):
        self.snapshot["inference"]["nodes"][4]["observedAt"] = "2099-01-01T00:00:00Z"
        fact = next(item for item in self.fleet()["facts"] if item["id"] == "registry:pc:fast")
        self.assertEqual((fact["state"], fact["reason"]), ("unknown", "clock_skew"))

    def test_matching_label_does_not_join_an_unmatched_peer(self):
        peer = self.snapshot["machines"][1]
        peer.update(label="Configured PC", host="registry:pc")
        machines = [item for item in self.fleet()["facts"] if item["scope"] == "machine"]
        self.assertEqual(len(machines), 4)
        self.assertTrue(any(item["id"] == "peer:spare" for item in machines))

    def test_declared_legacy_pc_replacement_joins_report_as_extra_evidence(self):
        peer = self.snapshot["machines"][1]
        peer.update(id="owner-pc", host="owner-pc", platform="windows", label="Reported PC")
        fleet = self.fleet()
        machines = {fact["id"]: fact for fact in fleet["facts"] if fact["scope"] == "machine"}
        self.assertEqual(set(machines), {"mac", "registry:pc", "registry:pro"})
        pc = machines["registry:pc"]
        self.assertEqual(pc["label"], "Configured PC")
        self.assertEqual(pc["state"], "reported")
        self.assertEqual(pc["evidence"]["endpointCounts"]["answering"], 2)
        self.assertFalse(pc["evidence"]["unmatchedPeer"])
        report = pc["evidence"]["peerReports"][0]
        self.assertEqual((report["id"], report["source"], report["state"]), ("owner-pc", "peer report", "reported"))
        self.assertEqual((report["observedAt"], report["ageSeconds"]), (peer["observedAt"], 2))
        self.assertEqual(report["evidence"], peer)
        stale_pc = next(fact for fact in self.fleet(31)["facts"] if fact["id"] == "registry:pc")
        self.assertEqual(stale_pc["evidence"]["peerReports"][0]["state"], "stale")

    def test_ambiguous_windows_reports_are_not_combined(self):
        first = self.snapshot["machines"][1]
        first["platform"] = "windows"
        second = dict(first, id="peer:another", host="another")
        self.snapshot["machines"].append(second)
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertTrue(machines["peer:spare"]["evidence"]["unmatchedPeer"])
        self.assertTrue(machines["peer:another"]["evidence"]["unmatchedPeer"])
        self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])

    def test_exact_other_registry_identity_wins_over_legacy_replacement(self):
        peer = self.snapshot["machines"][1]
        peer.update(id="registry:pro", platform="windows")
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertEqual(machines["registry:pro"]["state"], "reported")
        self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])

    def test_explicit_attribution_wins_over_legacy_fallback(self):
        for field in ("attributedMachineId", "registryMachineId", "machineId"):
            with self.subTest(field=field):
                peer = self.snapshot["machines"][1]
                peer.update(platform="windows")
                peer[field] = "registry:pro"
                machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
                self.assertEqual(machines["registry:pro"]["evidence"]["peerReports"][0]["id"], peer["id"])
                self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])
                peer.pop(field)

    def test_source_attribution_matches_exact_source_id(self):
        peer = self.snapshot["machines"][1]
        peer.update(platform="windows", sourceId="signed-source")
        self.snapshot["sources"] = [{"id": "signed-source", "attributedMachineId": "registry:pro"}]
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertEqual(machines["registry:pro"]["evidence"]["peerReports"][0]["id"], peer["id"])
        self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])

    def test_marked_legacy_source_joins_without_claiming_other_windows_peer(self):
        first = self.snapshot["machines"][1]
        first["platform"] = "windows"
        self.snapshot["machines"].append(dict(first, id="peer:another", host="another"))
        self.snapshot["sources"] = [{"id": "windows:spare-telemetry", "host": "spare", "legacy": True}]
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertEqual(machines["registry:pc"]["evidence"]["peerReports"][0]["id"], "peer:spare")
        self.assertTrue(machines["peer:another"]["evidence"]["unmatchedPeer"])
        self.assertNotIn("peer:spare", machines)

    def test_nested_peer_report_uses_original_clock_and_stale_reason(self):
        peer = self.snapshot["machines"][1]
        peer.update(platform="windows", observedAt="2099-01-01T00:00:00Z")
        pc = next(fact for fact in self.fleet()["facts"] if fact["id"] == "registry:pc")
        report = pc["evidence"]["peerReports"][0]
        self.assertEqual((report["state"], report["reason"]), ("unknown", "clock_skew"))
        self.assertEqual((pc["state"], pc["reason"]), ("unknown", "clock_skew"))
        peer.update(observedAt="2026-11-12T11:59:58Z", reasonCode="stale", state="unknown")
        pc = next(fact for fact in self.fleet()["facts"] if fact["id"] == "registry:pc")
        report = pc["evidence"]["peerReports"][0]
        self.assertEqual((report["state"], report["reason"], report["ageSeconds"]), ("stale", "stale", 2))

    def test_without_replacement_legacy_windows_report_stays_unmatched(self):
        self.snapshot["inference"]["nodes"][1].pop("replaces")
        self.snapshot["machines"][1]["platform"] = "windows"
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertTrue(machines["peer:spare"]["evidence"]["unmatchedPeer"])
        self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])

    def test_declared_replacement_never_joins_this_windows_observer(self):
        self.snapshot.update(host="windows")
        local = self.snapshot["machines"][0]
        local.update(id="pc", host="pc", platform="windows")
        machines = {fact["id"]: fact for fact in self.fleet()["facts"] if fact["scope"] == "machine"}
        self.assertEqual(machines["pc"]["source"], "local report")
        self.assertNotIn("peerReports", machines["registry:pc"]["evidence"])

    def test_explicit_registry_attribution_joins_without_losing_host_clock(self):
        peer = self.snapshot["machines"][1]
        peer["registryMachineId"] = "registry:pc"
        fleet = self.fleet()
        self.assertEqual(fleet["counts"]["machines"], 3)
        machine = next(item for item in fleet["facts"] if item["id"] == "registry:pc")
        self.assertEqual(machine["state"], "reported")
        self.assertEqual(machine["ageSeconds"], 2)
        self.assertEqual(machine["evidence"]["endpointCounts"]["answering"], 2)

    def test_actual_unmatched_report_survives_snapshot_projection(self):
        remote.project_snapshot(self.snapshot)
        self.assertTrue(any(row.get("id") == "peer:spare" for row in self.snapshot["machines"]))
        self.assertEqual(self.fleet()["counts"]["machines"], 4)

    def test_original_inputs_are_not_mutated(self):
        original = copy.deepcopy(self.snapshot)
        self.fleet(100)
        self.assertEqual(self.snapshot, original)

    def test_empty_or_missing_readings_are_unknown(self):
        for snapshot in ({}, {"machines": None, "inference": {"nodes": None}}):
            fleet = build_fleet_state(snapshot, now=self.now)
            self.assertEqual(fleet["state"], "unknown")
            self.assertIsNone(fleet["counts"]["busy"])
            self.assertIn("unknown", fleet["summary"])
            self.assertTrue(any(item["id"] == "mac" for item in fleet["facts"]))

    def test_windows_empty_snapshot_keeps_this_pc(self):
        fleet = build_fleet_state({"host": "windows"}, now=self.now)
        self.assertEqual([fact["id"] for fact in fleet["facts"]], ["pc"])

    def test_huge_now_is_rejected(self):
        with self.assertRaises(ValueError):
            build_fleet_state({}, now=10 ** 1000)

    def test_expiry_before_observation_is_unknown(self):
        self.snapshot["inference"]["nodes"][4]["expiresAt"] = "1970-01-01T00:00:00Z"
        lane = next(fact for fact in self.fleet()["facts"] if fact["id"] == "registry:pc:fast")
        self.assertEqual((lane["state"], lane["reason"]), ("unknown", "invalid_expiry"))

    def test_missing_timestamp_does_not_claim_stale_age(self):
        self.snapshot["machines"][1].update(observedAt=None, state="unknown", reasonCode="stale")
        peer = next(fact for fact in self.fleet()["facts"] if fact["id"] == "peer:spare")
        self.assertEqual((peer["state"], peer["ageSeconds"]), ("unknown", None))

    def test_zero_is_allowed_only_for_fresh_complete_counts(self):
        only_local = {"machines": [self.snapshot["machines"][0]]}
        self.assertEqual(build_fleet_state(only_local, now=self.now)["counts"]["busy"], 0)
        self.assertIsNone(build_fleet_state(only_local, now=self.now+10)["counts"]["busy"])

    def test_explicit_activity_loss_clears_retained_local_busy_count(self):
        self.snapshot["activityKnown"] = False
        self.snapshot["machines"][0]["busyCount"] = 1
        local = next(fact for fact in self.fleet()["facts"] if fact["id"] == "mac")
        self.assertIsNone(local["evidence"]["busyCount"])
        self.assertEqual(self.snapshot["machines"][0]["busyCount"], 1, "pure join must not mutate its input")

    def test_local_model_contradictions_do_not_keep_complete_totals(self):
        for models in ([], [{"host": "mac", "id": "local/model", "loaded": None}],
                       [{"host": "mac", "id": "local/model", "loaded": True, "inventoryComplete": False}]):
            with self.subTest(models=models):
                self.snapshot["models"] = models
                local = next(fact for fact in self.fleet()["facts"] if fact["id"] == "mac")
                self.assertNotEqual(local["completeness"], "complete")
                self.assertIsNone(local["evidence"]["loadedCount"])
                self.assertIsNone(local["evidence"]["busyCount"])

    def test_alias_rows_do_not_require_model_count_equality(self):
        self.snapshot["models"] = [{"host": "mac", "id": identity, "loaded": True}
                                   for identity in ("local/model", "local/alias")]
        local = next(fact for fact in self.fleet()["facts"] if fact["id"] == "mac")
        self.assertEqual(local["evidence"]["loadedCount"], 1)


class RegistryProbeEvidenceTests(unittest.TestCase):
    def test_last_success_is_separate_from_latest_failed_probe(self):
        target = {"id": "registry:pc:deep", "machine": "registry:pc", "label": "Deep",
                  "base": "http://127.0.0.1:21001", "timeoutSeconds": 6}
        lane = remote._lane_node(target, (None, None, "timeout", 1008), 1010, 3,
                                 ("reported/model", 1000), 1002, False)
        self.assertIsNone(lane["modelId"])
        self.assertEqual(lane["lastProbeAt"], "1970-01-01T00:16:48Z")
        self.assertEqual(lane["lastSuccessAt"], "1970-01-01T00:16:40Z")
        self.assertEqual(lane["lastSuccessModelId"], "reported/model")
        self.assertEqual((lane["probeState"], lane["probeTimeoutSeconds"]), ("completed", 6))
        self.assertEqual(lane["configuredEndpoint"], target["base"])
        document = public_map({"schema": MAP_SCHEMA, "nodes": [lane], "edges": []})
        self.assertEqual(document["nodes"][0]["lastSuccessAt"], lane["lastSuccessAt"])

    def test_no_success_date_is_invented_for_a_pending_lane(self):
        target = {"id": "registry:pc:deep", "machine": "registry:pc", "label": "Deep",
                  "base": "http://127.0.0.1:21001", "timeoutSeconds": 6}
        lane = remote._lane_node(target, None, 1000, 3, started=999, running=True)
        self.assertEqual(lane["probeState"], "running")
        self.assertIsNone(lane["lastProbeAt"])
        self.assertIsNone(lane["lastSuccessAt"])

    def test_background_completion_keeps_measured_history_without_network(self):
        probes = remote.BackgroundProbes()
        self.addCleanup(probes.close)
        success = Future()
        success.set_result(("reported/model", 12, None))
        failure = Future()
        failure.set_result((None, None, "timeout"))
        with mock.patch.object(remote.time, "time", side_effect=[1000, 1008]):
            probes._complete(success, probes.generation, "registry:pc:deep")
            probes._complete(failure, probes.generation, "registry:pc:deep")
        self.assertEqual(probes.successes["registry:pc:deep"], ("reported/model", 1000))
        self.assertEqual(probes.results["registry:pc:deep"], (None, None, "timeout", 1008))


if __name__ == "__main__":
    unittest.main()
