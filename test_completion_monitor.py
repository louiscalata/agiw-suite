"""Display-only completion evidence projection tests."""
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import activity


def evidence(verdict="VERIFIED", total=1, verified=1, progress="HISTORY_UNKNOWN"):
    return {
        "kind": "agiw.completion-evidence.v1",
        "verdict": verdict,
        "code": "COVERAGE_ASSESSED",
        "criteriaTotal": total,
        "criteriaVerified": verified,
        "verifiedCriterionIndices": list(range(verified)),
        "criteriaUnassessed": total - verified,
        "evidenceFingerprint": "a" * 64,
        "progressStatus": progress,
        "stopReason": "NO_PROGRESS" if progress == "NO_PROGRESS" else None,
        "dispatchAuthorized": False,
        "workflowAcceptance": "NOT_ESTABLISHED",
        "releaseAcceptance": "NOT_ESTABLISHED",
    }


def archive(completion=None):
    result = {"kind": "codemode.router.v1", "schemaVersion": 1,
              "runId": "evidence-run", "requestSha256": "b" * 64,
              "status": "RESPONSE_VALIDATED", "advisoryOnly": True,
              "accepted": False, "authorizing": False, "filesApplied": False,
              "recoveryRequired": False, "tests": "NOT_RUN",
              "certification": "NOT_RUN"}
    if completion is not None:
        result["completionEvidence"] = completion
    return {"schemaVersion": 1, "runId": "evidence-run",
            "inputSha256": "b" * 64, "exitCode": 0, "stage": "final_validation",
            "checkpoint": {}, "finishedUnix": 1800000000.0, "result": result}


def receipt(router_sha="b" * 64, status="RESPONSE_VALIDATED"):
    return {"kind": "codemode.monitor.run-receipt.v1", "schemaVersion": 1,
            "runId": "evidence-run",
            "finishedAt": datetime.fromtimestamp(1800000000.0, timezone.utc).isoformat(),
            "route": {"status": status, "client": "codex"},
            "ownerDecision": {"status": "REJECTED"},
            "intakeDecision": {"route": "edit", "routeState": "recorded"},
            "review": {"structuredFindingCount": 0},
            "bindings": {"routerRequestSha256": router_sha},
            "calls": [{"role": "author", "reportedModel": "local/author",
                       "status": "RESPONSE_VALIDATED", "elapsedMs": 5,
                       "bindings": {"runId": "opencode.advisory:evidence-run.draft"},
                       "usage": {"inputTokens": 3, "outputTokens": 2,
                                 "totalTokens": 5, "complete": True},
                       "prompt": "private model prompt"}]}


class CompletionMonitorTests(unittest.TestCase):
    def project(self, value):
        return activity._project_archive(archive(value), 1800000010.0)

    def test_valid_verdicts_copy_only_safe_coverage_fields(self):
        for sample in (
            evidence(),
            evidence("PARTIAL", 2, 1),
            evidence("UNVERIFIED", 1, 0),
            evidence("VERIFIED", 1, 1, "NO_PROGRESS"),
        ):
            with self.subTest(sample=sample["verdict"]):
                projected = self.project(sample)
                display = projected["completionEvidence"]
                self.assertEqual(display["verdict"], sample["verdict"])
                self.assertEqual(display["criteriaTotal"], sample["criteriaTotal"])
                self.assertEqual(display["criteriaVerified"], sample["criteriaVerified"])
                self.assertEqual(display["criteriaUnassessed"], sample["criteriaUnassessed"])
                self.assertEqual(display["progressStatus"], sample["progressStatus"])
                self.assertEqual(display["stopReason"], sample["stopReason"])
                self.assertEqual(projected["testsStatus"], "NOT_RUN")
                self.assertEqual(projected["completionEvidenceSource"], "router-archive")
                serialized = json.dumps(projected)
                self.assertNotIn("verifiedCriterionIndices", serialized)
                self.assertNotIn("evidenceFingerprint", serialized)
                self.assertNotIn("dispatchAuthorized", serialized)
                self.assertNotIn("workflowAcceptance", serialized)

    def test_missing_fingerprint_or_indices_cannot_claim_verified_coverage(self):
        sample = evidence("PARTIAL", 2, 1)
        del sample["evidenceFingerprint"]
        self.assertIsNone(self.project(sample)["completionEvidence"])
        sample = evidence()
        del sample["verifiedCriterionIndices"]
        self.assertIsNone(self.project(sample)["completionEvidence"])

    def test_missing_and_malformed_fields_never_synthesize_verified(self):
        self.assertIsNone(activity._project_archive(archive(), 1800000010.0)["completionEvidence"])
        sample = evidence()
        malformed = [
            None, [], "VERIFIED", {"verdict": "VERIFIED"},
            {**sample, "criteriaTotal": True},
            {**sample, "criteriaVerified": 65},
            {**sample, "criteriaUnassessed": 1},
            {**sample, "verdict": "PARTIAL"},
            {**sample, "code": {}},
            {**sample, "progressStatus": []},
            {**sample, "dispatchAuthorized": True},
            {**sample, "workflowAcceptance": "ACCEPTED"},
            {**sample, "releaseAcceptance": "SHIPPABLE"},
            {**sample, "stopReason": "NO_PROGRESS"},
            {**sample, "verifiedCriterionIndices": [0, 0]},
            {**sample, "verifiedCriterionIndices": [1]},
            {**evidence("PARTIAL", 3, 2), "verifiedCriterionIndices": [1, 0]},
            {**sample, "evidenceFingerprint": "raw proof text"},
            {**sample, "evidenceFingerprint": None},
            {**sample, "criteriaTotal": 0, "criteriaVerified": 0,
             "criteriaUnassessed": 0, "verifiedCriterionIndices": [],
             "verdict": "UNVERIFIED"},
            {**sample, "code": "VALIDATION_UNBOUND", "verdict": "UNVERIFIED",
             "criteriaVerified": 0, "criteriaUnassessed": 1},
            {**sample, "criterionText": "private acceptance criterion"},
            {**sample, "proofReceipts": ["private signed proof"]},
            {**sample, "candidate": {"content": "private source"}},
            {**sample, "proofReceipts": ["x" * 3000000]},
        ]
        for value in malformed:
            with self.subTest(value=str(value)[:90]):
                self.assertIsNone(self.project(value)["completionEvidence"])

    def test_unverified_error_codes_and_test_status(self):
        sample = evidence("UNVERIFIED", 1, 0)
        sample["code"] = "VALIDATION_UNBOUND"
        sample["evidenceFingerprint"] = None
        self.assertEqual(self.project(sample)["completionEvidence"]["code"], "VALIDATION_UNBOUND")
        sample["code"] = "EVIDENCE_GATE_UNAVAILABLE"
        self.assertEqual(self.project(sample)["completionEvidence"]["code"], "EVIDENCE_GATE_UNAVAILABLE")
        sample["criteriaTotal"] = 0
        sample["criteriaUnassessed"] = 0
        self.assertEqual(self.project(sample)["completionEvidence"]["criteriaTotal"], 0)
        record = archive(sample)
        record["result"]["tests"] = "UNTRUSTED_CLAIM"
        self.assertIsNone(activity._project_archive(record, 1800000010.0)["testsStatus"])

    def test_no_progress_requires_exact_status_and_stop_reason(self):
        sample = evidence(progress="NO_PROGRESS")
        self.assertEqual(self.project(sample)["completionEvidence"]["stopReason"], "NO_PROGRESS")
        sample["stopReason"] = None
        self.assertIsNone(self.project(sample)["completionEvidence"])
        sample = evidence(progress="NO_PROGRESS")
        sample["evidenceFingerprint"] = None
        self.assertIsNone(self.project(sample)["completionEvidence"])

    def test_failed_route_cannot_project_a_verified_completion(self):
        record = archive(evidence())
        record["result"]["status"] = "CHECKS_FAILED"
        self.assertIsNone(activity._project_archive(record, 1800000010.0)["completionEvidence"])

    def test_owner_envelope_contradictions_leave_legacy_row_but_no_completion(self):
        base = archive(evidence())
        changes = (
            ("exitCode", lambda r: r.update(exitCode=3)),
            ("request hash", lambda r: r["result"].update(requestSha256="a" * 64)),
            ("record hash", lambda r: r.update(inputSha256="a" * 64)),
            ("accepted", lambda r: r["result"].update(accepted=True)),
            ("advisory", lambda r: r["result"].update(advisoryOnly=False)),
            ("recovery", lambda r: r["result"].update(recoveryRequired=True)),
            ("authorizing", lambda r: r["result"].update(authorizing=True)),
            ("files applied", lambda r: r["result"].update(filesApplied=True)),
            ("tests", lambda r: r["result"].update(tests="PASSED")),
            ("certification", lambda r: r["result"].update(certification="PASSED")),
            ("schema", lambda r: r.update(schemaVersion=2)),
            ("missing owner key", lambda r: r.pop("checkpoint")),
        )
        for name, change in changes:
            with self.subTest(name=name):
                row = json.loads(json.dumps(base))
                change(row)
                projected = activity._project_archive(row, 1800000010.0)
                self.assertEqual(projected["status"], "RESPONSE_VALIDATED")
                self.assertIsNone(projected["completionEvidence"])
                self.assertIsNone(projected["completionEvidenceSource"])
                self.assertIsNone(projected["testsStatus"])

    @staticmethod
    def write(path: Path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def test_exact_trace_merges_archive_coverage_into_receipt_without_losing_receipt_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "router"
            archive_dir = root / "archive"
            receipt_dir = Path(folder) / "receipts"
            archive_dir.mkdir(parents=True, mode=0o700)
            receipt_dir.mkdir(mode=0o700)
            row = archive(evidence())
            row["result"]["candidate"] = {"files": [{"content": "private candidate"}]}
            self.write(archive_dir / "evidence-run.json", row)
            self.write(receipt_dir / "evidence-run.json", receipt())
            with mock.patch.object(activity, "_ROOT", root), \
                 mock.patch.object(activity, "_RECEIPTS", receipt_dir), \
                 mock.patch.object(activity, "_active_rows", return_value=([], False, "ok")):
                result = activity.collect_activity(now=1800000010.0)
        self.assertEqual(len(result["runs"]), 1)
        projected = result["runs"][0]
        self.assertEqual(projected["source"], "monitor-receipt")
        self.assertEqual(projected["hostAcceptance"], "REJECTED")
        self.assertEqual(projected["calls"][0]["model"], "local/author")
        self.assertEqual(projected["calls"][0]["usage"]["totalTokens"], 5)
        self.assertEqual(projected["completionEvidence"]["verdict"], "VERIFIED")
        self.assertEqual(projected["completionEvidenceSource"], "router-archive")
        self.assertEqual(projected["testsStatus"], "NOT_RUN")
        serialized = json.dumps(result)
        self.assertNotIn("private candidate", serialized)
        self.assertNotIn("private model prompt", serialized)

    def test_nonmatching_trace_cannot_splice_archive_coverage_into_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "router"
            archive_dir = root / "archive"
            receipt_dir = Path(folder) / "receipts"
            archive_dir.mkdir(parents=True, mode=0o700)
            receipt_dir.mkdir(mode=0o700)
            self.write(archive_dir / "evidence-run.json", archive(evidence()))
            self.write(receipt_dir / "evidence-run.json", receipt(router_sha="a" * 64))
            with mock.patch.object(activity, "_ROOT", root), \
                 mock.patch.object(activity, "_RECEIPTS", receipt_dir), \
                 mock.patch.object(activity, "_active_rows", return_value=([], False, "ok")):
                result = activity.collect_activity(now=1800000010.0)
        self.assertEqual(len(result["runs"]), 2)
        by_source = {row["source"]: row for row in result["runs"]}
        self.assertNotIn("completionEvidence", by_source["monitor-receipt"])
        self.assertEqual(by_source["router-archive"]["completionEvidence"]["verdict"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
