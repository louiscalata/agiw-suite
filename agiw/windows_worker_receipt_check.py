"""Bounded offline/native-local evidence checker; never connects, dispatches or admits."""

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import time

from . import fleet_jev

LIMIT = 65536
SOURCE_LIMIT = 4 * 1024 * 1024
JOB_KIND = "agiw.windows.job-receipt.v1"
JOB_SCHEMA_VERSION = 2
OUTCOMES = ("COMPLETED", "FAILED", "BLOCKED")
ROUTE_FIELDS = ("worker", "preanalysis", "reason")


def valid_route(route, expected_worker, program=None):
    if not isinstance(route, dict) or route.get("worker") != expected_worker:
        return False
    if route.get("preanalysis") == "local":
        return set(route) == set(ROUTE_FIELDS) and (
            route.get("reason") == "strict-local"
            or (route.get("reason") == "jev-not-applicable" and program == "repo-patch")
        )
    return (
        route.get("preanalysis") == "jev"
        and route.get("reason") == "jev-preanalyzed"
        and set(route) == {*ROUTE_FIELDS, "jev"}
        and fleet_jev.valid_evidence(route["jev"], program)
    )


def valid_commit(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def identity(s, windows=None):
    """File identity for change detection. On Windows (Py3.13+) st_ctime_ns differs between
    path and handle stats for the same unchanged file, so it is left out there only."""
    if windows is None:
        windows = os.name == "nt"
    base = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    return base if windows else base + (s.st_ctime_ns,)


def read_regular(path, limit):
    """Refuse symlinks in every component; bound reads and detect concurrent edits."""
    p = os.path.abspath(path)
    current = os.path.splitdrive(p)[0] + os.sep
    for component in p[len(current) :].split(os.sep):
        current = os.path.join(current, component)
        info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ValueError("linked-path")
    before = os.lstat(p)
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("unsafe-file")
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        start = os.fstat(fd)
        raw = b""
        while len(raw) <= limit:
            chunk = os.read(fd, min(65536, limit + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        end = os.fstat(fd)
        if (
            not stat.S_ISREG(start.st_mode)
            or len(raw) > limit
            or identity(before) != identity(start)
            or identity(start) != identity(end)
            or identity(end) != identity(os.lstat(p))
        ):
            raise ValueError("changed-file")
        return raw
    finally:
        os.close(fd)


def parse_receipt(raw):
    def unique(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError("duplicate-field")
            d[k] = v
        return d

    def invalid(_):
        raise ValueError("nonfinite-json")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid)


def number(v):
    try:
        return type(v) in (int, float) and math.isfinite(v)
    except OverflowError:
        return False


def evaluate(receipt, *, expected_model, now, actual_digest=None, expected_digest=None):
    """All receipt values are untrusted claims. Local hash matching is not deployment proof."""
    result = {
        "kind": "agiw.windows.offline-evidence.v1",
        "admission": False,
        "nativeExecution": "NOT_RUN",
        "transportIdentity": "UNVERIFIED",
        "workerDeployment": "UNVERIFIED",
        "observerOnly": False,
        "heartbeatClaim": "INVALID",
        "exactModelAdvertised": False,
        "workerVersionClaimPresent": False,
        "localSourceDigest": "NOT_CHECKED",
    }
    if expected_digest is not None:
        if not isinstance(expected_digest, str) or not re.fullmatch("[0-9a-f]{64}", expected_digest):
            result["localSourceDigest"] = "INVALID_BASELINE"
        elif actual_digest is None:
            result["localSourceDigest"] = "SOURCE_NOT_READ"
        else:
            result["localSourceDigest"] = "MATCH" if actual_digest == expected_digest else "MISMATCH"
    if not isinstance(receipt, dict):
        return result
    if receipt.get("scope") == "local-observer" or "workerVersion" in receipt:
        result["observerOnly"] = True
        result["heartbeatClaim"] = "OBSERVER_NOT_WORKER_PROOF"
        return result
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        return result
    stamp = receipt.get("updated_unix")
    if not number(now) or not number(stamp):
        return result
    age = now - stamp
    if not number(age) or age < 0 or age > 30:
        result["heartbeatClaim"] = "STALE_OR_FUTURE"
        return result
    if receipt.get("status") != "running":
        result["heartbeatClaim"] = "NOT_RUNNING"
        return result
    result["heartbeatClaim"] = "FRESH_RUNNING_CLAIM_ONLY"
    models = receipt.get("models")
    result["exactModelAdvertised"] = (
        isinstance(expected_model, str)
        and bool(expected_model)
        and isinstance(models, list)
        and bool(models)
        and len(models) <= 64
        and all(isinstance(m, str) and 0 < len(m) <= 200 for m in models)
        and expected_model in models
    )
    version = receipt.get("worker_version")
    result["workerVersionClaimPresent"] = isinstance(version, str) and bool(
        re.fullmatch(r"[0-9A-Za-z._+-]{1,16}", version)
    )
    return result


def _job_identity_problem(receipt, expected_worker, expected_model, job_id):
    if (
        receipt.get("kind") != JOB_KIND
        or type(receipt.get("schema_version")) is not int
        or receipt["schema_version"] != JOB_SCHEMA_VERSION
    ):
        return "SCHEMA_INVALID"
    if not isinstance(expected_worker, str) or not expected_worker or receipt.get("worker_id") != expected_worker:
        return "worker-mismatch"
    rid = receipt.get("job_id")
    if not isinstance(rid, str) or not rid or len(rid) > 200:
        return "job-invalid"
    if job_id is not None and rid != job_id:
        return "job-mismatch"
    model = receipt.get("model")
    if not isinstance(model, str) or not model or len(model) > 200:
        return "model-invalid"
    if expected_model is not None and model != expected_model:
        return "model-mismatch"
    if receipt.get("outcome") not in OUTCOMES:
        return "outcome-invalid"
    return None


def _job_provenance_problem(receipt, expected_worker):
    if "route" not in receipt:
        return "ROUTE_MISSING"
    if not valid_route(receipt["route"], expected_worker, receipt.get("program")):
        return "ROUTE_INVALID"
    commit = receipt.get("workerCommit")
    if "workerCommit" not in receipt or (commit is not None and not valid_commit(commit)):
        return "WORKER_COMMIT_INVALID"
    return None


def _job_evidence_problem(receipt, now):
    if receipt.get("reportStored") is not True:
        return "report-not-stored"
    run_id = receipt.get("runId")
    if not isinstance(run_id, str) or not re.fullmatch("[0-9A-Fa-f-]{36}", run_id):
        return "run-id-invalid"
    digest = receipt.get("reportSha256")
    if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
        return "sha-invalid"
    path = receipt.get("reportPath")
    if not isinstance(path, str) or not path or len(path) > 4096:
        return "report-path-invalid"
    started, finished = receipt.get("started_unix"), receipt.get("finished_unix")
    if not number(started) or not number(finished) or finished < started:
        return "timestamps-invalid"
    if now is not None and number(now):
        age = now - finished
        if age < -1 or age > 300:
            return "stale"
    return None


def validate_job_receipt(receipt, *, expected_worker, expected_model=None, job_id=None, now=None):
    """Check an untrusted job receipt. This is evidence validation, never dispatch authorization.

    Local hash and path matching are claims from a worker we cannot independently attest;
    it never proves native execution, a lease, or that report bytes are genuine.
    """
    result = {"kind": JOB_KIND, "valid": False, "reason": None}
    if not isinstance(receipt, dict):
        result["reason"] = "not-object"
        return result
    reason = (
        _job_identity_problem(receipt, expected_worker, expected_model, job_id)
        or _job_provenance_problem(receipt, expected_worker)
        or _job_evidence_problem(receipt, now)
    )
    if reason:
        result["reason"] = reason
        return result
    path = receipt["reportPath"]
    result["valid"] = True
    result["outcome"] = receipt["outcome"]
    result["report_path"] = path
    return result


def _emit(text):
    sys.stdout.write(str(text) + "\n")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--mode",
        choices=["heartbeat", "job"],
        default="heartbeat",
        help="heartbeat (default) or a saved job receipt from agiw.windows_worker",
    )
    p.add_argument("--receipt", required=True, help="Explicit saved heartbeat or job receipt file, never a live queue")
    p.add_argument("--expected-model", required=True)
    p.add_argument("--expected-worker", help="Required in job mode: worker id the receipt must name")
    p.add_argument("--job-id", help="Optional job id the receipt must name (job mode)")
    p.add_argument("--worker-source", help="Optional explicit local worker source file")
    p.add_argument("--expected-sha256", help="Independent trusted baseline digest, not a receipt value")
    args = p.parse_args(argv)
    if args.mode == "job":
        if not args.expected_worker:
            p.error("--expected-worker is required with --mode job")
        try:
            receipt = parse_receipt(read_regular(args.receipt, LIMIT))
        except (OSError, ValueError, TypeError, RecursionError):
            _emit(json.dumps({"kind": JOB_KIND, "valid": False, "reason": "input-unreadable-unsafe-or-malformed"}))
            return 2
        # Saved receipts are checked for shape and identity only; freshness is a dispatch-time check.
        result = validate_job_receipt(
            receipt, expected_worker=args.expected_worker, expected_model=args.expected_model, job_id=args.job_id
        )
        _emit(json.dumps(result, sort_keys=True))
        return 0 if result["valid"] else 1
    try:
        receipt = parse_receipt(read_regular(args.receipt, LIMIT))
        actual = (
            hashlib.sha256(read_regular(args.worker_source, SOURCE_LIMIT)).hexdigest() if args.worker_source else None
        )
        report = evaluate(
            receipt,
            expected_model=args.expected_model,
            now=time.time(),
            actual_digest=actual,
            expected_digest=args.expected_sha256,
        )
        _emit(json.dumps(report, sort_keys=True))
        # Zero means a completed diagnostic, NEVER admission or native acceptance.
        return 0
    except (OSError, ValueError, TypeError):
        _emit(
            json.dumps(
                {
                    "kind": "agiw.windows.offline-evidence.v1",
                    "admission": False,
                    "status": "NOT_RUN",
                    "reason": "input-unreadable-unsafe-or-malformed",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
