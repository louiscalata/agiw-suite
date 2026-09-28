import json
import os
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest import mock

import activity
import telemetry


class ActivityTests(unittest.TestCase):
    def test_current_jev_decision_state_requires_matching_route_choice(self):
        intake = {"model": "jev-1", "status": "JUDGED", "elapsed_ms": 4,
                  "route": {"choice": "other", "confidence": .36},
                  "decision": {"route": "other", "route_state": "UNRESOLVED_LOW_CONFIDENCE"},
                  "usage": {"input_tokens": 2, "output_tokens": 3}}
        record = {"runId": "route-test", "result": {"kind": "codemode.router.v1",
                  "runId": "route-test", "status": "PARTIAL", "intake": intake}}
        projected = activity._project_archive(record, time.time())
        self.assertEqual(projected["routeState"], "UNRESOLVED_LOW_CONFIDENCE")
        self.assertEqual(projected["calls"][0]["decision"]["routeState"], "UNRESOLVED_LOW_CONFIDENCE")
        intake["decision"]["route"] = "different"
        self.assertIsNone(activity._project_archive(record, time.time())["routeState"])

    def setUp(self):
        patcher = mock.patch.object(activity, "_receipt_files", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cross_run_or_role_receipt_cannot_attach_to_trace(self):
        row = {"runId": "opencode.advisory:other.draft", "reportedModel": "local/model", "status": "RESPONSE_VALIDATED"}
        self.assertIsNone(activity._role_identity_only({"roleEvidence": {"author": row}}, "author", "route-1"))
        row["runId"] = "opencode.advisory:route-1.review"
        self.assertIsNone(activity._role_identity_only({"roleEvidence": {"author": row}}, "author", "route-1"))

    def test_decision_metadata_excludes_text_and_invalid_confidence(self):
        self.assertIsNone(activity._label("private prompt text"))
        self.assertIsNone(activity._confidence(1.2))
        self.assertIsNone(activity._confidence(True))
        self.assertIsNone(activity._nonnegative_int(2**53))
        self.assertEqual(activity._label("UNRESOLVED_LOW_CONFIDENCE"), "UNRESOLVED_LOW_CONFIDENCE")

    def test_intake_per_call_total_is_safe_sum_or_consistent_explicit_total(self):
        derived = activity._intake_usage({"input_tokens": 1342, "output_tokens": 213})
        self.assertEqual(derived["totalTokens"], 1555)
        self.assertEqual(derived["totalSource"], "sum_of_reported_input_output")
        explicit = activity._intake_usage({"input_tokens": 1342, "output_tokens": 213,
                                           "total_tokens": 1555})
        self.assertEqual(explicit["totalTokens"], 1555)
        self.assertEqual(explicit["totalSource"], "explicit_total")
        self.assertIsNone(activity._intake_usage({"input_tokens": 1342, "output_tokens": 213,
                                                  "total_tokens": 1554})["totalTokens"])
        self.assertIsNone(activity._intake_usage({"input_tokens": 1342, "output_tokens": 213,
                                                  "total_tokens": -1})["inputTokens"])
        self.assertIsNone(activity._intake_usage({"input_tokens": 2**53 - 1, "output_tokens": 1})["outputTokens"])
        self.assertIsNone(activity._intake_usage({"input_tokens": 10})["totalTokens"])

    def _write(self, path: Path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    @staticmethod
    def _route(run_id, stage, started, recorded, evidence):
        """A whole legacy router record: the closed envelope the router writes."""
        return {"schemaVersion": 1, "runId": run_id, "inputSha256": "a" * 64, "stage": stage, "sequence": 1,
                "startedUnix": started,
                "checkpoint": {"schemaVersion": 1, "runId": run_id, "inputSha256": "a" * 64, "sequence": 1,
                               "stage": stage, "recordedUnix": recorded, "evidence": evidence}}

    def _backend_with_calls(self):
        parent = "bound-route"
        candidate, author_task, review_task = "c" * 64, "a" * 64, "b" * 64
        backend = {"candidateFingerprint": candidate,
                   "selection": {"authorModel": "local/author", "reviewerModel": "local/reviewer"},
                   "roleEvidence": {},
                   "stages": {"prepare": {"taskFingerprint": author_task,
                                             "reviewTaskFingerprint": review_task}}}
        for role, stage, model, task in (("author", "draft", "local/author", author_task),
                                         ("reviewer", "review", "local/reviewer", review_task)):
            call_run = parent + "." + stage
            call_id = "opencode.advisory:" + call_run
            receipt = {"status": "RESPONSE_VALIDATED", "requestedModel": model,
                       "reportedModel": model, "operation": stage, "taskFingerprint": task,
                       "candidateFingerprint": candidate, "runId": call_id,
                       "requestSha256": "1" * 64, "responseSha256": "2" * 64,
                       "contentSha256": "3" * 64, "resultCandidateFingerprint": candidate,
                       "elapsedMs": 100 if role == "author" else 200,
                       "usage": {"promptTokens": 10, "completionTokens": 5, "totalTokens": 15},
                       "lifecycle": {"transportSettlement": "settled"}}
            backend["roleEvidence"][role] = dict(receipt)
            result = {"status": "RESPONSE_VALIDATED", "runId": call_run, "mode": stage,
                      "accepted": False, "advisoryOnly": True, "outputMode": "json_schema",
                      "checks": "NOT_RUN", "tests": "NOT_RUN", "certification": "NOT_RUN",
                      "candidateFingerprint": candidate,
                      "binding": {"runId": call_id, "taskFingerprint": task,
                                  "candidateFingerprint": candidate},
                      "receipts": [receipt], "candidate": {"files": [{"content": "private"}]}}
            backend["stages"][stage] = {"status": "RETURNED", "operation": "work",
                                         "recoveryRequired": False, "result": result}
        return backend

    def test_nested_nisi_envelopes_enrich_identity_with_bound_usage_and_duration(self):
        backend = self._backend_with_calls()
        author = activity._verified_stage_call(backend, "author", "bound-route", "c" * 64)
        reviewer = activity._verified_stage_call(backend, "reviewer", "bound-route", "c" * 64)
        self.assertEqual((author["elapsedMs"], reviewer["elapsedMs"]), (100, 200))
        self.assertEqual(author["usage"], {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15})
        self.assertEqual(reviewer["servedModel"], "local/reviewer")

    def test_bad_stage_binding_keeps_only_unknown_role_identity(self):
        backend = self._backend_with_calls()
        backend["stages"]["draft"]["result"]["binding"]["taskFingerprint"] = "d" * 64
        enriched = activity._verified_stage_call(backend, "author", "bound-route", "c" * 64)
        base = activity._role_identity_only(backend, "author", "bound-route")
        self.assertIsNone(enriched)
        self.assertEqual(base["servedModel"], "local/author")
        self.assertEqual(base["state"], "unknown")
        self.assertIsNone(base["usage"]["totalTokens"])

    def test_duplicate_or_inconsistent_stage_receipts_refuse_enrichment(self):
        backend = self._backend_with_calls()
        env = backend["stages"]["draft"]["result"]
        env["receipts"].append(dict(env["receipts"][0]))
        self.assertIsNone(activity._verified_stage_call(backend, "author", "bound-route", "c" * 64))
        env["receipts"] = [dict(backend["roleEvidence"]["author"])]
        env["receipts"][0]["usage"]["totalTokens"] = -1
        self.assertIsNone(activity._verified_stage_call(backend, "author", "bound-route", "c" * 64))

    def test_current_checkpoint_is_projected_without_evidence_text_or_fake_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            pointer = self._write(Path(temp) / "active.json", self._route(
                "route-8", "backend_draft", time.time() - 10000, time.time() - 2,
                {"prompt": "secret prompt", "model": "do-not-project"}))
            with mock.patch.object(activity, "_ACTIVE", pointer), mock.patch.object(activity, "_archive_files", return_value=[]):
                result = activity.collect_activity()
        self.assertEqual(len(result["runs"]), 1)
        run = result["runs"][0]
        self.assertEqual(run["runId"], "route-8")
        self.assertEqual(run["stage"], "backend_draft")
        self.assertEqual(run["activity"], "unknown")
        self.assertEqual(run["calls"], [])
        self.assertIsNone(run["usage"]["totalTokens"])
        encoded = json.dumps(result)
        self.assertNotIn("secret prompt", encoded)
        self.assertNotIn("do-not-project", encoded)

    def test_stale_checkpoint_not_active_generation(self):
        with tempfile.TemporaryDirectory() as temp:
            pointer = self._write(Path(temp) / "active.json", self._route(
                "route-old", "backend_answer", time.time() - 1000, time.time() - 900, {}))
            with mock.patch.object(activity, "_ACTIVE", pointer), mock.patch.object(activity, "_archive_files", return_value=[]):
                result = activity.collect_activity()
            self.assertEqual(result["runs"][0]["status"], "stale")
            self.assertEqual(result["runs"][0]["activity"], "unknown")
            self.assertEqual(result["sources"][0]["state"], "stale")
            # A record outside the router's closed envelope (here: no digest, a partial
            # checkpoint) describes nothing: one unreadable row, none of its fields used.
            self._write(pointer, {"runId": "route-old", "stage": "backend_answer", "startedUnix": time.time(),
                                  "checkpoint": {"recordedUnix": time.time() - 900, "evidence": {}}})
            with mock.patch.object(activity, "_ACTIVE", pointer), mock.patch.object(activity, "_archive_files", return_value=[]):
                loose = activity.collect_activity()
        self.assertEqual([(r["runId"], r["status"], r["stage"], r["recordedAt"]) for r in loose["runs"]],
                         [("route-old", "unreadable", None, None)])

    def test_absent_or_invalid_pointer_has_no_run(self):
        with tempfile.TemporaryDirectory() as temp:
            absent = Path(temp) / "absent.json"
            with mock.patch.object(activity, "_ACTIVE", absent), mock.patch.object(activity, "_archive_files", return_value=[]):
                missing = activity.collect_activity()
            invalid = self._write(Path(temp) / "invalid.json", {"runId": "../../secret", "stage": "x"})
            with mock.patch.object(activity, "_ACTIVE", invalid), mock.patch.object(activity, "_archive_files", return_value=[]):
                malformed = activity.collect_activity()
        self.assertEqual(missing["runs"], [])
        self.assertEqual(malformed["runs"], [])
        self.assertEqual(malformed["sources"][0]["state"], "error")

    def test_archive_projects_only_allowlisted_receipt_and_route_fields(self):
        record = {
            "schemaVersion": 1, "runId": "route-12", "stage": "backend_response",
            "finishedUnix": 1_800_000_000.0, "exitCode": 0, "inputSha256": "a" * 64,
            "result": {
                "kind": "codemode.router.v1", "runId": "route-12", "status": "RESPONSE_VALIDATED",
                "candidate": {"files": [{"path": "secret.py", "content": "private source"}]},
                "stages": {
                    "intake": {"route": {"choice": "edit", "route_state": "routed", "confidence": 0.8},
                               "model": "jev-1", "status": "JUDGED", "elapsed_ms": 12,
                               "usage": {"input_tokens": 13, "output_tokens": 7}},
                    "backend": {"roleEvidence": {
                        "author": {"runId": "opencode.advisory:route-12.draft",
                                    "reportedModel": "local/author", "requestedModel": "local/author",
                                    "operation": "draft", "status": "RESPONSE_VALIDATED", "prompt": "never expose"},
                        "reviewer": {"runId": "opencode.advisory:route-12.review",
                                     "reportedModel": "local/reviewer", "operation": "review",
                                     "status": "RESPONSE_VALIDATED"}}}
                }
            }
        }
        projected = activity._project_archive(record, 1_800_000_010.0)
        self.assertEqual(projected["routeChoice"], "edit")
        self.assertEqual([call["role"] for call in projected["calls"]], ["author", "reviewer", "intake"])
        self.assertEqual(projected["calls"][0]["model"], "local/author")
        self.assertEqual(projected["calls"][0]["id"], "opencode.advisory:route-12.draft")
        self.assertEqual(projected["usage"], {"inputTokens": None, "outputTokens": None, "totalTokens": None})
        self.assertEqual(projected["calls"][-1]["usage"],
                         {"inputTokens": 13, "outputTokens": 7, "totalTokens": 20,
                          "totalSource": "sum_of_reported_input_output"})
        serialized = json.dumps(projected)
        self.assertNotIn("private source", serialized)
        self.assertNotIn("never expose", serialized)
        self.assertNotIn("secret.py", serialized)
        self.assertIsNone(projected["reviewConsistency"])
        self.assertEqual((projected["client"], projected["host"]), (None, None))
        record["result"].update(client="codex", selectedHost="windows")
        projected = activity._project_archive(record, 1_800_000_010.0)
        self.assertEqual((projected["client"], projected["host"]), ("codex", "windows"))
        record["result"].update(client="cursor; rm", selectedHost="cloud")
        projected = activity._project_archive(record, 1_800_000_010.0)
        self.assertEqual((projected["client"], projected["host"]), (None, None))

    def test_archive_projects_bounded_review_consistency_from_the_first_reviewed_stage(self):
        stages = {"backend": {"reviewConsistency": {"status": "SUMMARY_MAY_REPORT_DEFECT", "rule": "possible-defect",
                                                    "evidence": "may\n\x07 miss " + "x" * 400}},
                  "macReturn": {"reviewConsistency": {"status": "SUMMARY_REPORTS_DEFECT", "rule": "defect"}}}
        record = {"runId": "route-13", "result": {"kind": "codemode.router.v1", "runId": "route-13",
                                                  "status": "RESPONSE_VALIDATED", "stages": stages}}
        value = activity._project_archive(record, time.time())["reviewConsistency"]
        self.assertEqual((value["status"], value["rule"], value["stage"]),
                         ("SUMMARY_MAY_REPORT_DEFECT", "possible-defect", "backend"))
        self.assertTrue(value["evidence"].startswith("may miss x"))
        self.assertEqual(len(value["evidence"]), 160)
        stages["backend"]["reviewConsistency"] = {"status": "OK", "rule": "x"}
        value = activity._project_archive(record, time.time())["reviewConsistency"]
        self.assertEqual((value["status"], value["stage"], value["evidence"]), ("SUMMARY_REPORTS_DEFECT", "macReturn", None))
        stages["macReturn"]["reviewConsistency"]["rule"] = "bad rule; rm -rf"
        self.assertIsNone(activity._project_archive(record, time.time())["reviewConsistency"]["rule"])
        stages["macReturn"] = "not a stage"
        self.assertIsNone(activity._project_archive(record, time.time())["reviewConsistency"])

    def test_sanitized_monitor_receipt_keeps_reported_calls_separate_from_run_totals(self):
        receipt = {
            "kind": "codemode.monitor.run-receipt.v1", "schemaVersion": 1,
            "runId": "receipt-1", "startedAt": "2026-09-23T17:20:00+00:00",
            "finishedAt": "2026-09-23T17:21:00+00:00",
            "route": {"status": "RESPONSE_VALIDATED", "elapsedMs": 60000},
            "ownerDecision": {"status": "REJECTED"},
            "intakeDecision": {"route": "edit", "routeState": "low_confidence", "confidence": 0.2},
            "review": {"structuredFindingCount": 0, "summaryContradictsEmptyFindings": True},
            "logicalCallCount": 2,
            "calls": [
                {"role": "author", "reportedModel": "google/model-a", "requestedModel": "google/model-a",
                 "status": "RESPONSE_VALIDATED", "elapsedMs": 10,
                 "bindings": {"runId": "opencode.advisory:receipt-1.draft"},
                 "usage": {"inputTokens": 3, "outputTokens": 4, "totalTokens": 7, "complete": True},
                 "prompt": "no leak"},
                {"role": "reviewer", "reportedModel": "google/model-b", "requestedModel": "google/model-b",
                 "status": "RESPONSE_VALIDATED", "elapsedMs": 20,
                 "bindings": {"runId": "opencode.advisory:receipt-1.review"},
                 "usage": {"inputTokens": 5, "outputTokens": 6, "totalTokens": 11, "complete": True}},
            ],
            "usageTotals": {"inputTokens": 8, "outputTokens": 10, "totalTokens": 18, "complete": True},
            "privateText": "never project",
        }
        projected = activity._project_monitor_receipt(receipt, 1790184060.0)
        self.assertEqual(projected["status"], "RESPONSE_VALIDATED")
        self.assertEqual(projected["hostAcceptance"], "REJECTED")
        self.assertEqual(projected["calls"][0]["id"], "opencode.advisory:receipt-1.draft")
        self.assertEqual(projected["calls"][0]["servedModel"], "google/model-a")
        self.assertIsNone(projected["usage"]["totalTokens"])
        self.assertEqual(projected["usageCoverage"]["scope"], "reported-calls-only")
        self.assertEqual(projected["calls"][0]["usage"]["totalTokens"], 7)
        self.assertNotIn("never project", json.dumps(projected))
        receipt["calls"][1]["usage"]["complete"] = False
        self.assertIsNone(activity._project_monitor_receipt(receipt, 1790184060.0)["usage"]["totalTokens"])

    def test_archive_scan_caps_results_and_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            archive = root / "archive"
            archive.mkdir(parents=True, mode=0o700)
            for index in range(15):
                row = {"schemaVersion": 1, "runId": f"route-{index}", "stage": "done",
                       "finishedUnix": 1000 + index, "exitCode": 0, "inputSha256": "b" * 64,
                       "result": {"kind": "codemode.router.v1", "runId": f"route-{index}",
                                  "status": "RESPONSE_VALIDATED", "stages": {}}}
                file = archive / f"route-{index}.json"
                file.write_text(json.dumps(row))
                file.chmod(0o600)
                os.utime(file, (1000 + index, 1000 + index))
            (archive / "route-link.json").symlink_to(archive / "route-0.json")
            with mock.patch.object(activity, "_ROOT", root):
                files = activity._archive_files()
                rows = [activity._project_archive(activity._read_archive(path), 1015) for path in files]
        self.assertEqual(len(files), 12)
        self.assertEqual(rows[0]["runId"], "route-14")
        self.assertNotIn("route-link.json", [path.name for path in files])

    def test_archive_scan_sorts_past_the_first_256_names(self):
        """Spec 6.12: scan up to 4096 names, then sort by mtime; the newest archives are never lost
        to directory order once the archive holds more than 256 runs."""
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "router" / "archive"
            archive.mkdir(parents=True, mode=0o700)
            for index in range(300):
                file = archive / f"route-{index:03d}.json"
                file.write_text("{}")
                os.chmod(file, 0o600)
                os.utime(file, (1000 + index, 1000 + index))
            with mock.patch.object(activity, "_ROOT", Path(temp) / "router"):
                files = activity._archive_files()
        self.assertEqual([path.name for path in files], [f"route-{index:03d}.json" for index in range(299, 287, -1)])


class DualReadActivityTests(unittest.TestCase):
    """Router-concurrency P2 (spec 6.12, R2.9): one activity row per run of either layout,
    live only by the lock-verified observation of the same sample."""

    def setUp(self):
        from test_telemetry import _require_router_writer
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(os.path.realpath(self.temp.name)) / "router"
        for target, name, value in ((activity, "_ACTIVE", self.root / "active.json"),
                                    (activity, "_receipt_files", lambda: []),
                                    (activity, "_archive_files", lambda: []),
                                    (telemetry, "_ACTIVE_PATH", self.root / "active.json"),
                                    (telemetry, "_ROUTER_ROOT", self.root),
                                    (telemetry, "_router_openers", lambda root: None)):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(telemetry._LAST_ROUTER_OBSERVATION.update, sampledAt=None, value=None)
        per_run, legacy =_require_router_writer(self, "per-run"), _require_router_writer(self, "legacy")
        self.state, self.legacy = runpy.run_path(str(per_run)), runpy.run_path(str(legacy))
        self.owners = []
        self.addCleanup(lambda: [owner.__exit__(None, None, None) for owner in reversed(self.owners)])

    def claim(self, run_id, digest, *, begin=True, keep=True, note=None):
        owner = self.state["RouterOwner"](self.root)
        owner.__enter__()
        owner.claim(run_id, digest, until=time.monotonic() + 2,
                    meta={"client": "codex", "host": "windows", "operation": "work"})
        if begin:
            owner.begin(run_id, digest)
            owner.checkpoint("backend_draft", {"prompt": "PRIVATE_FIXTURE_TEXT"})
        if note:
            owner.note(note[0], note[1], note[2], time.monotonic() + 30)
        if keep:
            self.owners.append(owner)
        else:
            owner.__exit__(None, None, None)

    def sample(self, observed=True):
        now = time.time()
        if observed:
            with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
                telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=now, value=telemetry._router_observation(now))
        return activity.collect_activity(now)

    def test_one_row_per_run_of_both_layouts_live_only_by_the_observation(self):
        with self.legacy["RouterOwner"](self.root) as owner:      # an old quarantined single-run record
            owner.begin("legacy-old", "e" * 64)
            owner.checkpoint("backend_review", {"recoveryRequired": True})
        self.state["_atomic"](self.root / "policy.json", {"schemaVersion": 1, "kind": "codemode.router.policy.v1",
                                                          "concurrency": "multi"}, 1024)
        self.claim("run-dead", "d" * 64, keep=False)
        self.claim("run-live", "a" * 64)
        self.claim("run-queued", "b" * 64, begin=False, note=("waiting", "mac-pair", "pre-begin"))
        unobserved = self.sample(observed=False)
        rows = {row["runId"]: row for row in unobserved["runs"]}
        self.assertEqual(set(rows), {"legacy-old", "run-dead", "run-live"})
        self.assertTrue(all(row["activity"] == "unknown" for row in rows.values()))
        result = self.sample()
        rows = {row["runId"]: row for row in result["runs"]}
        self.assertEqual({key: (row["status"], row["activity"], row["layout"]) for key, row in rows.items()},
                         {"legacy-old": ("unresolved", "unknown", "legacy"),
                          "run-dead": ("unresolved", "unknown", "per-run"),
                          "run-live": ("running", "running", "per-run"),
                          "run-queued": ("waiting", "queued", "note")})
        self.assertEqual((rows["run-live"]["client"], rows["run-live"]["host"]), ("codex", "windows"))
        self.assertEqual([row["runId"] for row in result["runs"]][:2], ["run-queued", "run-live"])
        self.assertIn("2 verified live", result["sources"][0]["detail"])
        self.assertNotIn("PRIVATE_FIXTURE_TEXT", json.dumps(result))

    def test_a_record_being_published_is_one_unreadable_row_not_a_failed_sample(self):
        self.claim("run-a", "a" * 64, keep=False)
        self.claim("run-b", "b" * 64, keep=False)
        record = self.root / "active" / "run-a.json"
        os.link(record, record.with_name("run-a.json.tmp-7"))
        with mock.patch.object(activity.time, "sleep"):
            result = self.sample()
        rows = {row["runId"]: row for row in result["runs"]}
        self.assertEqual((rows["run-a"]["status"], rows["run-b"]["status"]), ("unreadable", "unresolved"))
        self.assertNotEqual(result["sources"][0]["state"], "error")

    def test_hostile_records_never_fail_the_activity_sample(self):
        """Sol #2 / #3: server.py and live_feed call collect_activity inside the whole sample, so one
        record must never raise out of it.  A per-run digest that is a list (unhashable), a legacy
        runId that is an object, or a time too large for a float is one unreadable row (or a
        reported problem); the other runs are still shown."""
        from test_telemetry import _HUGE, _private_write
        self.claim("run-ok", "a" * 64, keep=False)
        self.claim("run-bad", "b" * 64, keep=False)
        bad = self.root / "active" / "run-bad.json"
        value = json.loads(bad.read_text())
        value["inputSha256"] = []
        os.unlink(bad)
        _private_write(bad, json.dumps(value))
        _private_write(self.root / "active.json", json.dumps({"runId": {}, "inputSha256": "c" * 64}))
        result = self.sample()
        self.assertEqual({row["runId"]: row["status"] for row in result["runs"]},
                         {"run-ok": "unresolved", "run-bad": "unreadable"})
        self.assertEqual(result["sources"][0]["state"], "error")
        legacy = {"schemaVersion": 1, "runId": "legacy-huge", "inputSha256": "e" * 64, "stage": "backend_draft",
                  "sequence": 1, "startedUnix": time.time() - 20,
                  "checkpoint": {"schemaVersion": 1, "runId": "legacy-huge", "inputSha256": "e" * 64,
                                 "sequence": 1, "stage": "backend_draft", "recordedUnix": "__HUGE__",
                                 "evidence": {}}}
        os.unlink(self.root / "active.json")
        _private_write(self.root / "active.json", json.dumps(legacy).replace('"__HUGE__"', _HUGE))
        rows = {row["runId"]: row for row in self.sample()["runs"]}
        self.assertEqual((rows["legacy-huge"]["status"], rows["legacy-huge"]["recordedAt"]), ("unreadable", None))
        archive = json.loads('{"runId": "r-1", "finishedUnix": ' + _HUGE + ', "result": {"kind": "codemode.router.v1", '
                             '"runId": "r-1", "status": "PARTIAL"}}')
        item = activity._project_archive(archive, time.time())
        self.assertEqual((item["recordedAt"], item["ageSeconds"]), (None, None))
        self.assertIsNone(activity._confidence(json.loads(_HUGE)))
        # The last line of defence: whatever _active_rows raises, the sample still returns.
        with mock.patch.object(activity, "_active_rows", side_effect=RuntimeError("boom")):
            result = activity.collect_activity()
        self.assertEqual((result["runs"], result["sources"][0]["state"]), ([], "error"))

    def test_p2conv_a_deeply_nested_archive_or_receipt_is_skipped_never_raised(self):
        """Claude reviewer (Low): json.loads raises RecursionError on a deeply nested archive or
        receipt (2 MB, under the 3 MiB cap); it escaped collect_activity, so server.py published no
        full snapshot while that archive stayed among the newest 12.  It is now skipped."""
        from test_telemetry import _private_write
        deep = '{"runId":"run-deep","result":' + "[" * 1000000 + "]" * 1000000 + "}"
        archive = self.root / "archive"
        receipts = self.root.parent / "receipts"
        os.makedirs(archive, mode=0o700)
        os.makedirs(receipts, mode=0o700)
        _private_write(archive / "run-deep.json", deep)
        _private_write(receipts / "run-deep.json", deep)
        with mock.patch.object(activity, "_ROOT", self.root), mock.patch.object(activity, "_RECEIPTS", receipts), \
             mock.patch.object(activity, "_archive_files", lambda: [archive / "run-deep.json"]), \
             mock.patch.object(activity, "_receipt_files", lambda: [receipts / "run-deep.json"]):
            result = activity.collect_activity()
        self.assertEqual(result["runs"], [])
        self.assertEqual(result["sources"][0]["detail"], "No unresolved router pointer; history is reported separately")

    def test_an_observation_of_another_journal_is_never_used(self):
        self.claim("run-live", "a" * 64)
        # p2-readers converge: rows shaped exactly like this journal's own (layout, input), plus a
        # live run this journal does not list, so only the root check can refuse them.
        observed = [{"runId": "run-live", "layout": "per-run", "inputSha256": "a" * 64, "live": True, "state": "running"},
                    {"runId": "run-other", "layout": "note", "live": True, "state": "waiting", "note": None}]
        with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
            telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=time.time(), value={"root": "/elsewhere", "rows": observed})
        rows = {row["runId"]: row for row in activity.collect_activity()["runs"]}
        self.assertEqual(set(rows), {"run-live"})
        self.assertEqual(rows["run-live"]["activity"], "unknown")
        # The same rows from this journal are used: the observation binds by run, layout and input.
        with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
            telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=time.time(), value={"root": str(self.root), "rows": observed})
        rows = {row["runId"]: row for row in activity.collect_activity()["runs"]}
        self.assertEqual({key: (row["status"], row["activity"]) for key, row in rows.items()},
                         {"run-live": ("running", "running"), "run-other": ("waiting", "queued")})
        # A row that says running but is not live (never written by telemetry; defensive) is not promoted.
        with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
            telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=time.time(), value={"root": str(self.root), "rows": [
                dict(observed[0], live=False), dict(observed[1], live=False)]})
        rows = {row["runId"]: row for row in activity.collect_activity()["runs"]}
        self.assertEqual({key: row["activity"] for key, row in rows.items()}, {"run-live": "unknown"})

    def test_p2conv_an_unreadable_record_is_never_promoted_by_an_observation(self):
        """Sol N2: the observation may be up to 5 s old; a record read here that is not whole is
        never promoted to running, even by an observation row that names no input (defensive:
        telemetry never marks such a row live)."""
        from test_telemetry import _private_write
        self.claim("run-bad", "b" * 64, keep=False)
        os.unlink(self.root / "active" / "run-bad.json")
        _private_write(self.root / "active" / "run-bad.json", "{not json")
        with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
            telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=time.time(), value={"root": str(self.root), "rows": [
                {"runId": "run-bad", "layout": "per-run", "inputSha256": None, "live": True, "state": "running"}]})
        rows = [(row["runId"], row["status"], row["activity"]) for row in activity.collect_activity()["runs"]]
        self.assertEqual(rows, [("run-bad", "unreadable", "unknown")])


if __name__ == "__main__":
    unittest.main()
