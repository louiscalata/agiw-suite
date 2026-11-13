"""Run evidence contracts, using only private temporary records and mocked liveness."""
import copy
import datetime
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

import activity
import telemetry


def group(data, name):
    return [row for row in data['runs'] if row['traceKey'] in data[name + 'Keys']]


class RunEvidenceTests(unittest.TestCase):
    NOW = 1_800_000_000.0
    DIGEST = "a" * 64
    OTHER_DIGEST = "b" * 64

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "router"
        self.receipts = self.base / "receipts"
        for directory in (self.root, self.root / "archive", self.root / "active", self.receipts):
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
        for name, value in (("_ROOT", self.root), ("_ACTIVE", self.root / "active.json"),
                            ("_RECEIPTS", self.receipts)):
            patcher = mock.patch.object(activity, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(telemetry, "last_router_observation", return_value=None)
        self.observation = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, record, when=None):
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        stamp = self.NOW if when is None else when
        os.utime(str(path), (stamp, stamp))

    def receipt(self, run_id="run-1", digest=None):
        stamp = datetime.datetime.fromtimestamp(self.NOW - 10, datetime.timezone.utc).isoformat()
        return {
            "kind": "codemode.monitor.run-receipt.v1", "schemaVersion": 1,
            "runId": run_id, "startedAt": stamp, "finishedAt": stamp,
            "route": {"status": "RESPONSE_VALIDATED"},
            "ownerDecision": {"status": "REJECTED"}, "intakeDecision": {}, "review": {},
            "bindings": {"routerRequestSha256": digest or self.DIGEST},
            "calls": [{"role": "author", "requestedModel": "requested/model",
                       "reportedModel": "served/model", "status": "RESPONSE_VALIDATED",
                       "bindings": {"runId": "opencode.advisory:" + run_id + ".draft"},
                       "usage": {"inputTokens": 2, "outputTokens": 3, "totalTokens": 5,
                                 "complete": True}}],
        }

    def archive(self, run_id, digest=None, when=None):
        return {"schemaVersion": 1, "runId": run_id, "inputSha256": digest or self.DIGEST,
                "stage": "done", "finishedUnix": self.NOW - 10 if when is None else when,
                "exitCode": 0,
                "result": {"kind": "codemode.router.v1", "runId": run_id,
                           "status": "RESPONSE_VALIDATED", "stages": {}}}

    def active(self, run_id, digest):
        return {"schemaVersion": 1, "runId": run_id, "inputSha256": digest,
                "stage": "backend_draft", "sequence": 1, "startedUnix": self.NOW - 2,
                "checkpoint": {"schemaVersion": 1, "runId": run_id, "inputSha256": digest,
                               "sequence": 1, "stage": "backend_draft",
                               "recordedUnix": self.NOW - 1, "evidence": {}}}

    def current_evidence(self, observed_at=None, complete=True):
        return {"observedAt": self.NOW if observed_at is None else observed_at,
                "complete": complete, "counts": {"running": 0, "queued": 0, "unresolved": 0}}

    def test_receipt_and_calls_bind_to_exact_request_attempt(self):
        projected = activity._project_monitor_receipt(self.receipt(), self.NOW)
        attempt = "run-1:" + self.DIGEST
        self.assertEqual(projected["attemptId"], attempt)
        call = projected["calls"][0]
        self.assertEqual((call["parentRunId"], call["attemptId"]), ("run-1", attempt))
        self.assertEqual((call["requestedModel"], call["servedModel"]),
                         ("requested/model", "served/model"))
        self.assertEqual(call["usage"]["totalTokens"], 5)

    def test_foreign_attempt_cannot_contribute_a_call_or_usage(self):
        receipt = self.receipt()
        receipt["calls"][0]["bindings"]["routerRequestSha256"] = self.OTHER_DIGEST
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertEqual(projected["calls"], [])
        self.assertIsNone(projected["usage"]["totalTokens"])

    def test_nested_archive_receipt_cannot_override_the_enclosing_attempt(self):
        from test_activity import ActivityTests
        backend = ActivityTests()._backend_with_calls()
        backend['stages']['draft']['result']['receipts'][0]['bindings'] = {
            'routerRequestSha256': self.OTHER_DIGEST}
        record = self.archive('bound-route')
        record['result']['requestSha256'] = self.DIGEST
        record['result']['stages'] = {'backend': backend}
        projected = activity._project_archive(record, self.NOW)
        self.assertEqual([call['role'] for call in projected['calls']], ['reviewer'])
        self.assertEqual(projected['usageCoverage']['measuredCallCount'], 1)
        self.assertEqual(projected['calls'][0]['attemptId'], 'bound-route:' + self.DIGEST)

    def test_foreign_role_binding_cannot_attach_even_to_same_attempt(self):
        receipt = self.receipt()
        receipt["calls"][0]["bindings"]["runId"] = "opencode.advisory:run-1.review"
        self.assertEqual(activity._project_monitor_receipt(receipt, self.NOW)["calls"], [])

    def test_requested_identity_survives_missing_served_identity(self):
        receipt = self.receipt()
        receipt["calls"][0].pop("reportedModel")
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertEqual(len(projected["calls"]), 1)
        self.assertEqual(projected["calls"][0]["requestedModel"], "requested/model")
        self.assertIsNone(projected["calls"][0]["servedModel"])

    def test_missing_attempt_keeps_usage_unknown_without_inventing_an_id(self):
        receipt = self.receipt()
        receipt.pop("bindings")
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertIsNone(projected["attemptId"])
        self.assertIsNone(projected["calls"][0]["attemptId"])
        self.assertIsNone(projected["calls"][0]["usage"]["totalTokens"])
        self.assertFalse(projected["usageCoverage"]["complete"])
        self.assertEqual(projected["usageCoverage"]["measuredCallCount"], 0)

    def test_absent_receipts_leave_every_unproduced_phase_unavailable(self):
        self.write(self.root / "archive" / "run-1.json", self.archive("run-1"))
        projected = activity.collect_activity(self.NOW)["runs"][0]
        self.assertEqual(projected["calls"], [])
        self.assertIsNone(projected["usage"]["totalTokens"])
        slots = projected["analysisSlots"]
        self.assertEqual([(slot["tool"], slot["phase"]) for slot in slots],
                         [("jev", "pre"), ("nisi", "journal"), ("jev", "post")])
        for slot in slots:
            self.assertEqual((slot["runId"], slot["attemptId"]),
                             (projected["runId"], projected["attemptId"]))
            self.assertEqual(slot["state"], "unavailable")
            self.assertFalse(slot["evidenced"])
            self.assertIn("NOT AVAILABLE YET", slot["detail"])

    def test_complete_call_metrics_do_not_claim_whole_run_coverage(self):
        receipt = self.receipt()
        receipt["logicalCallCount"] = 1
        receipt["usageTotals"] = {"inputTokens": 2, "outputTokens": 3,
                                  "totalTokens": 5, "complete": True}
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertEqual(projected["calls"][0]["usage"]["totalTokens"], 5)
        self.assertIsNone(projected["usage"]["totalTokens"])
        coverage = projected["usageCoverage"]
        self.assertEqual(coverage["scope"], "reported-calls-only")
        self.assertEqual(coverage["reportedCallCount"], 1)
        self.assertEqual(coverage["measuredCallCount"], 1)
        self.assertIsNone(coverage["expectedCallCount"])
        self.assertFalse(coverage["complete"])
        self.assertIn("manifest", coverage["detail"])

    def test_intake_judgment_is_not_a_complete_jev_phase_producer(self):
        record = self.archive("run-1")
        record["result"]["intake"] = {
            "model": "jev/intake", "status": "JUDGED", "elapsed_ms": 4,
            "usage": {"input_tokens": 2, "output_tokens": 3},
            "route": {"choice": "edit", "confidence": 0.8},
        }
        projected = activity._project_archive(record, self.NOW)
        self.assertEqual(projected["calls"][0]["role"], "intake")
        self.assertEqual(projected["calls"][0]["usage"]["totalTokens"], 5)
        for phase in projected["analysisSlots"]:
            self.assertEqual(phase["state"], "unavailable")
            self.assertFalse(phase["evidenced"])
            self.assertIn("NOT AVAILABLE YET", phase["detail"])

    def test_incomplete_or_inconsistent_call_usage_stays_unknown(self):
        for update in ({"complete": False}, {"totalTokens": 6}, {"inputTokens": -1}):
            with self.subTest(update=update):
                receipt = self.receipt()
                receipt["calls"][0]["usage"].update(update)
                projected = activity._project_monitor_receipt(receipt, self.NOW)
                self.assertIsNone(projected["calls"][0]["usage"]["totalTokens"])
                self.assertFalse(projected["usageCoverage"]["complete"])
                self.assertEqual(projected["usageCoverage"]["measuredCallCount"], 0)

    def test_measured_zero_per_call_remains_zero(self):
        receipt = self.receipt()
        receipt["calls"][0]["usage"].update(inputTokens=0, outputTokens=0, totalTokens=0)
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertEqual(projected["calls"][0]["usage"]["totalTokens"], 0)
        self.assertIsNone(projected["usage"]["totalTokens"])

    def test_same_run_id_different_attempt_retains_dated_outcome_separately(self):
        self.write(self.receipts / "prior.json", self.receipt("same-run", self.OTHER_DIGEST))
        self.write(self.root / "active.json", self.active("same-run", self.DIGEST))
        self.observation.return_value = {"root": str(self.root), "observedAt": self.NOW, "rows": [
            {"runId": "same-run", "layout": "legacy", "inputSha256": self.DIGEST,
             "live": True, "state": "running"}]}
        result = activity.collect_activity(self.NOW)
        self.assertEqual(len(group(result, "current")), 1)
        self.assertEqual(len(group(result, "history")), 1)
        self.assertEqual(group(result, "current")[0]["attemptId"], "same-run:" + self.DIGEST)
        self.assertEqual(group(result, "history")[0]["attemptId"], "same-run:" + self.OTHER_DIGEST)
        self.assertEqual(group(result, "history")[0]["hostAcceptance"], "REJECTED")
        self.assertIsNone(group(result, "current")[0].get("hostAcceptance"))

    def test_history_bound_does_not_destroy_independent_current_empty_evidence(self):
        for index in range(13):
            self.write(self.root / "archive" / ("run-%02d.json" % index),
                       self.archive("run-%02d" % index, when=self.NOW - index - 10),
                       when=self.NOW - index - 10)
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        self.assertEqual(len(result["runs"]), 12)
        self.assertTrue(result["historyBound"]["truncated"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["currentEvidence"]["complete"])
        self.assertEqual(result["currentEvidence"]["count"], 0)

    def test_fresh_complete_active_read_can_establish_no_current_work(self):
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        evidence = result["currentEvidence"]
        self.assertTrue(evidence["complete"])
        self.assertEqual(evidence["count"], 0)
        self.assertEqual(evidence["observedAt"], self.NOW)
        self.assertGreater(evidence["expiresAt"], self.NOW)
        self.assertEqual(group(result, "current"), [])

    def test_absent_active_directory_is_unknown_not_a_measured_zero(self):
        (self.root / "active").rmdir()
        result = activity.collect_activity(self.NOW)
        self.assertFalse(result["currentEvidence"]["complete"])
        self.assertIsNone(result["currentEvidence"]["count"])
        self.assertTrue(result["currentEvidence"]["reason"])

    def test_stale_incomplete_or_missing_inventory_cannot_establish_zero(self):
        for evidence in (None, self.current_evidence(self.NOW - 4),
                         self.current_evidence(complete=False)):
            with self.subTest(evidence=evidence):
                result = activity.collect_activity(self.NOW, current_evidence=evidence)
                self.assertFalse(result["currentEvidence"]["complete"])
                self.assertIsNone(result["currentEvidence"]["count"])
                self.assertTrue(result["currentEvidence"]["reason"])

    def test_invalid_counts_and_clock_ahead_cannot_become_no_jobs(self):
        candidates = [self.current_evidence(self.NOW + 1.001)]
        for value in (True, -1, None, 2 ** 53):
            evidence = self.current_evidence()
            evidence["counts"]["running"] = value
            candidates.append(evidence)
        unresolved = self.current_evidence()
        unresolved["counts"]["unresolved"] = 1
        candidates.append(unresolved)
        for evidence in candidates:
            with self.subTest(evidence=evidence):
                result = activity.collect_activity(self.NOW, current_evidence=evidence)
                self.assertFalse(result["currentEvidence"]["complete"])
                self.assertIsNone(result["currentEvidence"]["count"])
                self.assertTrue(result["currentEvidence"]["reason"])

    def test_unreadable_active_record_cannot_establish_no_current_work(self):
        self.write(self.root / "active.json", {"runId": "bad-record", "stage": "draft"})
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        self.assertFalse(result["currentEvidence"]["complete"])
        self.assertIsNone(result["currentEvidence"]["count"])
        self.assertTrue(result["currentEvidence"]["reason"])

    def test_inventory_count_cannot_contradict_observed_active_work(self):
        self.write(self.root / "active.json", self.active("live-run", self.DIGEST))
        self.observation.return_value = {"root": str(self.root), "observedAt": self.NOW, "rows": [
            {"runId": "live-run", "layout": "legacy", "inputSha256": self.DIGEST,
             "live": True, "state": "running"}]}
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        self.assertEqual(group(result, "current")[0]["activity"], "running")
        self.assertFalse(result["currentEvidence"]["complete"])
        self.assertIsNone(result["currentEvidence"]["count"])

    def test_both_server_stores_expire_run_counts_on_original_evidence_clock(self):
        from server import SnapshotStore as MacStore
        sys.path.append(str(Path(__file__).resolve().parent / "windows"))
        from agiw_win.server import SnapshotStore as WindowsStore
        self.write(self.receipts / "prior.json", self.receipt())
        evidence = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        snapshot = {"sampledAt": self.NOW + 2, "models": [], "machines": [], "sources": [],
                    "activity": evidence, "inference": {}}
        for store_class in (MacStore, WindowsStore):
            with self.subTest(edition=store_class.__module__):
                store = store_class()
                store.snapshot = copy.deepcopy(snapshot)
                with mock.patch("time.time", return_value=self.NOW + 2):
                    fresh = store.read()
                self.assertEqual(fresh["activity"]["currentEvidence"]["count"], 0)
                with mock.patch("time.time", return_value=self.NOW + 4):
                    expired = store.read()
                current = expired["activity"]["currentEvidence"]
                self.assertFalse(current["complete"])
                self.assertIsNone(current["count"])
                self.assertEqual(current["observedAt"], self.NOW)
                self.assertTrue(current["reason"])
                self.assertEqual(group(expired["activity"], "history"), group(evidence, "history"))
                self.assertEqual(group(expired["activity"], "history")[0]["hostAcceptance"], "REJECTED")
                self.assertEqual(store.snapshot["activity"]["currentEvidence"]["count"], 0,
                                 "aging a read must not rewrite the saved observation")

    def test_snapshot_partitions_reference_rows_without_serializing_them_twice(self):
        self.write(self.receipts / 'prior.json', self.receipt())
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        self.assertNotIn('current', result)
        self.assertNotIn('history', result)
        self.assertEqual(result['historyKeys'], [row['traceKey'] for row in result['runs']])
        self.assertEqual(result['currentKeys'], [])

    def test_windows_without_run_producer_explains_unknown_instead_of_zero(self):
        sys.path.append(str(Path(__file__).resolve().parent / "windows"))
        from agiw_win.server import SnapshotStore
        store = SnapshotStore()
        store.snapshot = {"sampledAt": self.NOW, "models": [], "sources": []}
        with mock.patch("time.time", return_value=self.NOW):
            data = store.read()
        self.assertFalse(data["activity"]["complete"])
        self.assertIsNone(data["activity"]["currentEvidence"]["count"])
        self.assertIn("NOT AVAILABLE YET", data["activity"]["currentEvidence"]["reason"])
        self.assertIn("producer", data["activity"]["currentEvidence"]["reason"])

    def test_fresh_inventory_envelope_cannot_renew_an_older_run_lock(self):
        from server import SnapshotStore as MacStore
        sys.path.append(str(Path(__file__).resolve().parent / "windows"))
        from agiw_win.server import SnapshotStore as WindowsStore
        run = {'runId': 'older-lock', 'activity': 'running', 'status': 'running',
               'livenessObservedAt': self.NOW - 4}
        snapshot = {'sampledAt': self.NOW, 'models': [], 'sources': [],
                    'activity': {'sampledAt': self.NOW, 'runs': [run], 'current': [run],
                                 'currentEvidence': {'complete': True, 'count': 1,
                                                     'observedAt': self.NOW}}}
        for store_class in (MacStore, WindowsStore):
            store = store_class()
            store.snapshot = copy.deepcopy(snapshot)
            with mock.patch('time.time', return_value=self.NOW):
                data = store.read()['activity']
            self.assertEqual(data['runs'][0]['activity'], 'unknown')
            self.assertFalse(data['currentEvidence']['complete'])
            self.assertIsNone(data['currentEvidence']['count'])
            self.assertEqual(data['runs'][0]['livenessObservedAt'], self.NOW - 4)

    def test_undated_dropped_history_can_hide_an_event_of_any_age(self):
        bound = activity._history_bound((activity._Listing(),), [{"runId": "undated", "ageSeconds": None}], self.NOW)
        self.assertTrue(bound["truncated"])
        self.assertEqual(bound["missingFromAgeSeconds"], 0.0)

    def test_conflicting_archive_result_request_cannot_bind_calls(self):
        record = self.archive("run-1")
        record["result"]["requestSha256"] = self.OTHER_DIGEST
        record["result"]["intake"] = {"model": "jev/intake", "status": "JUDGED",
                                         "usage": {"input_tokens": 2, "output_tokens": 3}}
        projected = activity._project_archive(record, self.NOW)
        self.assertEqual(projected["status"], "RESPONSE_VALIDATED")
        self.assertIsNone(projected["attemptId"])
        self.assertEqual(projected["calls"], [])

    def test_receipt_without_timezone_does_not_invent_a_recorded_instant(self):
        receipt = self.receipt()
        receipt["finishedAt"] = "2027-01-15T08:00:00"
        projected = activity._project_monitor_receipt(receipt, self.NOW)
        self.assertIsNone(projected["recordedAt"])
        self.assertIsNone(projected["ageSeconds"])

    def test_cached_liveness_observation_expires_before_new_activity_sample(self):
        self.write(self.root / "active.json", self.active("live-run", self.DIGEST))
        self.observation.return_value = {"root": str(self.root), "observedAt": self.NOW - 4, "rows": [
            {"runId": "live-run", "layout": "legacy", "inputSha256": self.DIGEST,
             "live": True, "state": "running"}]}
        result = activity.collect_activity(self.NOW, current_evidence=self.current_evidence())
        self.assertEqual(result["runs"][0]["activity"], "unknown")
        self.assertFalse(result["currentEvidence"]["complete"])
        self.assertIsNone(result["currentEvidence"]["count"])


if __name__ == "__main__":
    unittest.main()
