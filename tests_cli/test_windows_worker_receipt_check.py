import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agiw.windows_worker_receipt_check import (
    JOB_KIND,
    evaluate,
    main,
    parse_receipt,
    read_regular,
    validate_job_receipt,
)


class ReceiptCheckTests(unittest.TestCase):
    def setUp(self):
        self.r = {
            "schema_version": 1,
            "updated_unix": 99,
            "status": "running",
            "models": ["fixture-model"],
            "worker_version": "1.3",
        }

    def check(self, **kwargs):
        return evaluate(self.r, expected_model="fixture-model", now=100, **kwargs)

    def test_fresh_claim_never_admits(self):
        r = self.check()
        self.assertTrue(r["exactModelAdvertised"])
        self.assertTrue(r["workerVersionClaimPresent"])
        self.assertFalse(r["admission"])
        self.assertEqual(r["workerDeployment"], "UNVERIFIED")

    def test_observer_badge_is_not_worker(self):
        for update in ({"scope": "local-observer"}, {"workerVersion": "win-1.1.0"}):
            self.r.update(update)
            self.assertEqual(self.check()["heartbeatClaim"], "OBSERVER_NOT_WORKER_PROOF")

    def test_stale_future_and_bad_timestamp(self):
        for value in (0, 101, None, True, float("nan"), 10**400):
            self.r["updated_unix"] = value
            self.assertFalse(self.check()["exactModelAdvertised"])

    def test_schema_is_exact_integer(self):
        for v in (True, "1", 2, None):
            self.r["schema_version"] = v
            self.assertEqual(self.check()["heartbeatClaim"], "INVALID")

    def test_not_running(self):
        self.r["status"] = "stopped"
        self.assertEqual(self.check()["heartbeatClaim"], "NOT_RUNNING")

    def test_model_exact_no_alias_or_case_guess(self):
        for models in (["Fixture-model"], ["other"], "fixture-model", [None], ["fixture-model"] * 65):
            self.r["models"] = models
            self.assertFalse(self.check()["exactModelAdvertised"])

    def test_digest_match_is_not_deployment(self):
        r = self.check(actual_digest="a" * 64, expected_digest="a" * 64)
        self.assertEqual(r["localSourceDigest"], "MATCH")
        self.assertFalse(r["admission"])
        self.assertEqual(r["workerDeployment"], "UNVERIFIED")

    def test_digest_failure_states(self):
        for args, want in (
            ({"expected_digest": "bad"}, "INVALID_BASELINE"),
            ({"expected_digest": "a" * 64}, "SOURCE_NOT_READ"),
            ({"actual_digest": "b" * 64, "expected_digest": "a" * 64}, "MISMATCH"),
        ):
            self.assertEqual(self.check(**args)["localSourceDigest"], want)

    def test_untrusted_digest_in_receipt_ignored(self):
        self.r["sha256"] = "a" * 64
        self.assertEqual(self.check()["localSourceDigest"], "NOT_CHECKED")

    def test_duplicate_and_nonfinite_json_refused(self):
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b"{"):
            with self.assertRaises(ValueError):
                parse_receipt(data)

    def test_safe_bounded_local_read(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "r"
            p.write_bytes(b"{}")
            self.assertEqual(read_regular(p, 2), b"{}")
            with self.assertRaises(ValueError):
                read_regular(p, 1)
            with self.assertRaises(ValueError):
                read_regular(Path(td).resolve(), 20)

    def test_cli_redacts_payload_and_paths(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "receipt"
            self.r["output"] = "SECRET_FIXTURE"
            p.write_text(json.dumps(self.r))
            out = io.StringIO()
            with (
                contextlib.redirect_stdout(out),
                patch("agiw.windows_worker_receipt_check.time.time", return_value=100),
            ):
                self.assertEqual(main(["--receipt", str(p), "--expected-model", "fixture-model"]), 0)
            self.assertNotIn(td, out.getvalue())
            self.assertNotIn("SECRET_FIXTURE", out.getvalue())
            self.assertFalse(json.loads(out.getvalue())["admission"])

    def test_cli_malformed_redacted(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "bad"
            p.write_text("{")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--receipt", str(p), "--expected-model", "x"]), 2)
            self.assertNotIn(td, out.getvalue())


class JobReceiptCheckTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {
            "kind": JOB_KIND,
            "schema_version": 2,
            "job_id": "job-1",
            "worker_id": "dual-gpu-pc",
            "workerCommit": "a" * 40,
            "route": {"worker": "dual-gpu-pc", "preanalysis": "local", "reason": "strict-local"},
            "model": "fixture-model",
            "outcome": "COMPLETED",
            "reportStored": True,
            "runId": "0017efb7-de84-4fc5-95ba-3ea152847ffd",
            "reportSha256": "b" * 64,
            "reportPath": "C:/AGIW/report-store/x.json",
            "started_unix": 98,
            "finished_unix": 99,
        }

    def check(self):
        return validate_job_receipt(self.receipt, expected_worker="dual-gpu-pc", now=100)

    def assert_refused(self, reason):
        verdict = self.check()
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], reason)

    def test_missing_route(self):
        del self.receipt["route"]
        self.assert_refused("ROUTE_MISSING")

    def test_invalid_route(self):
        route = self.receipt["route"]
        for invalid in (None, dict(route, worker="m5-macbook-pro"), dict(route, preanalysis="not local")):
            with self.subTest(route=invalid):
                self.receipt["route"] = invalid
                self.assert_refused("ROUTE_INVALID")

    def test_missing_worker_commit(self):
        del self.receipt["workerCommit"]
        self.assert_refused("WORKER_COMMIT_INVALID")

    def test_invalid_worker_commit(self):
        for commit in ("g" * 40, "A" * 40, "a" * 39, "a" * 40 + "\n", 123):
            with self.subTest(commit=commit):
                self.receipt["workerCommit"] = commit
                self.assert_refused("WORKER_COMMIT_INVALID")

    def test_null_worker_commit(self):
        self.receipt["workerCommit"] = None
        self.assertTrue(self.check()["valid"])

    def test_legacy_or_malformed_schema(self):
        for version in (1, "2", 2.0):
            with self.subTest(version=version):
                self.receipt["schema_version"] = version
                self.assert_refused("SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()
