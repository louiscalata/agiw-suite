"""Small, read-only projection of recent router archives and its active records.

This is deliberately not a call ledger: the router does not link its run ids to
the separate Codemode batch call-audit ids.

Router-concurrency P2 (spec 6.12, R2.9): the active records are read from both
journal layouts, the legacy ``active.json`` and the newest ``active/<runId>.json``,
one row per run.  Whether a run is live comes only from the telemetry reader's lock
probes of the same sample (``telemetry.last_router_observation``); without one the
row's activity stays unknown.
"""
from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path
from typing import Any

import telemetry
from telemetry import _finite, _json_object

ROUTER_READER_CONTRACT = "codemode.router.readers.v2"

_HOME = Path.home()
_ROOT = _HOME / ".local/state/codemode-router"
_ACTIVE = _ROOT / "active.json"
_RECEIPTS = _HOME / ".local/state/inference-monitor/receipts"
# The router's active-record ceiling (MAX_CHECKPOINT): a record may carry a bounded candidate.
_MAX_BYTES = 3 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 3 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 256
_MAX_ARCHIVE_SCAN = 4096        # archive names examined before sorting by mtime
_MAX_ACTIVE_READS = 4           # per-run records read per sample, newest by mtime
_MAX_ACTIVE_LISTED = 64
_READ_RETRIES = 3
_READ_RETRY_S = 0.02
_MAX_RUNS = 12
_MAX_AGE = 300.0
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
_STAGE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")
_SAFE_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,159}\Z")
_SAFE_STATUS = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_MAX_SAFE_INT = (1 << 53) - 1
_COMPLETION_FIELDS = frozenset({
    "kind", "verdict", "code", "criteriaTotal", "criteriaVerified",
    "criteriaUnassessed", "verifiedCriterionIndices", "evidenceFingerprint",
    "progressStatus", "stopReason", "dispatchAuthorized",
    "workflowAcceptance", "releaseAcceptance",
})
_COMPLETION_CODES = frozenset({
    "COVERAGE_ASSESSED", "INPUT_INVALID", "VALIDATION_UNBOUND",
    "TEST_EVIDENCE_INVALID", "LITERAL_EVIDENCE_INVALID",
    "EVIDENCE_GATE_UNAVAILABLE",
})
_COMPLETION_PROGRESS = frozenset({
    "HISTORY_UNKNOWN", "HISTORY_UNTRUSTED", "NO_PRIOR_MATCH",
    "SAME_RUN_REPLAY", "NO_PROGRESS",
})


class _Changed(ValueError):
    """A record was being replaced or published (nlink 0 or 2) or changed while read."""


def _read_record(path: Path) -> dict[str, Any]:
    """One bounded I9 observation of an active record (legacy or per-run)."""
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode):
        raise ValueError("unsafe router pointer")
    if before.st_nlink in (0, 2):
        raise _Changed("record is being replaced or published")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino) or info.st_nlink in (0, 2):
            raise _Changed("record changed")
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1):
            raise ValueError("unsafe router pointer")
        chunks = bytearray()
        while len(chunks) <= _MAX_BYTES:
            part = os.read(fd, min(65536, _MAX_BYTES + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        after = os.fstat(fd)
        try:
            current = path.lstat()
        except FileNotFoundError:
            raise _Changed("record removed during observation") from None
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        if not identity(before) == identity(after) == identity(current):
            raise _Changed("record changed during observation")
        return _json_object(bytes(chunks), _MAX_BYTES)
    finally:
        os.close(fd)


def _read_active() -> dict[str, Any]:
    """The legacy single-run record (kept for callers of the old reader)."""
    return _read_record(_ACTIVE)


def _read_retry(path: Path) -> tuple[str, dict[str, Any] | None]:
    """('ok', value), ('absent', None) or ('unreadable', None); a record being replaced or
    published is read again up to 3 x 20 ms (spec R2.9 rule 2), never failing the sample.
    Any other failure (a RecursionError from a deeply nested record included) makes this
    one record unreadable, as telemetry's reader does."""
    for attempt in range(_READ_RETRIES):
        try:
            return "ok", _read_record(path)
        except FileNotFoundError:
            return "absent", None
        except _Changed:
            if attempt + 1 < _READ_RETRIES:
                time.sleep(_READ_RETRY_S)
        except Exception:
            return "unreadable", None
    return "unreadable", None


def _per_run_names() -> tuple[list[tuple[float, Path]], str]:
    """(at most 64 ``active/<runId>.json`` names, newest mtime first, from at most 4096
    entries; the listing's status): 'ok', 'absent' (no directory yet: lazy layout migration,
    an empty listing), 'unsafe' (a link, not a directory, another owner, group or other mode
    bits, or unreadable: nothing is listed) or 'truncated' (more than 4096 entries)."""
    directory = _ACTIVE.parent / "active"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return [], "absent"
    except OSError:
        return [], "unsafe"
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
        return [], "unsafe"
    rows = []
    status = "ok"
    try:
        with os.scandir(directory) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_ARCHIVE_SCAN:
                    status = "truncated"
                    break
                if not entry.name.endswith(".json") or not _RUN_ID.fullmatch(entry.name[:-5]):
                    continue
                try:
                    rows.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
                except OSError:
                    continue
    except FileNotFoundError:
        return [], "absent"
    except OSError:
        return [], "unsafe"
    return sorted(rows, reverse=True)[:_MAX_ACTIVE_LISTED], status


def _read_actives() -> tuple[list[dict[str, Any]], bool, str]:
    """(records, problem, listing): the legacy ``active.json`` plus the 4 newest per-run
    records, one per (runId, inputSha256), each tagged with ``_layout``.  Only a record that
    passes the router's closed envelope (telemetry's reader, the same rule for both layouts)
    is used; any other readable record is kept as ``{"runId", "_unreadable": True}`` and its
    fields are never used, not even as a dedup key.  A legacy record is folded into a per-run
    one only when that per-run record is valid and has the same input; beside an unreadable
    per-run record of its run ID it stays its own row.  ``problem``: a legacy file that cannot
    be read or names no valid run, or an unsafe active/ directory; ``listing`` is the per-run
    listing status (``_per_run_names``)."""
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    names, listing = _per_run_names()
    for _mtime, path in names[:_MAX_ACTIVE_READS]:
        status, value = _read_retry(path)
        if status == "absent":
            continue
        run_id = path.name[:-5]
        if (status != "ok" or value.get("runId") != run_id
                or telemetry._valid_active_route(value, per_run=True) is None):
            records.append({"runId": run_id, "_layout": "per-run", "_unreadable": True})
            seen.add((run_id, None))
            continue
        key = (run_id, value["inputSha256"])
        if key in seen:
            continue
        seen.add(key)
        records.append(dict(value, _layout="per-run"))
    status, legacy = _read_retry(_ACTIVE)
    problem = status == "unreadable" or listing == "unsafe"
    if status == "ok":
        run_id = legacy.get("runId")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            problem = True
        elif telemetry._valid_active_route(legacy) is None:
            records.append({"runId": run_id, "_layout": "legacy", "_unreadable": True})
        elif (run_id, legacy["inputSha256"]) not in seen:
            records.append(dict(legacy, _layout="legacy"))
    return records, problem, listing


def _live_rows() -> dict[tuple[str, str], dict[str, Any]]:
    """(runId, layout) -> the telemetry observation's row, when this sample observed this
    journal.  Keyed by layout too: one run ID may have a row in each layout (a collision)."""
    observation = telemetry.last_router_observation()
    if not observation or observation.get("root") != str(_ACTIVE.parent):
        return {}
    return {(row["runId"], row.get("layout")): row for row in observation.get("rows", []) if row.get("runId")}


def _age(timestamp: Any, now: float) -> float | None:
    if not _finite(timestamp):
        return None
    return round(max(0.0, now - timestamp), 3)


def _label(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", value) else None


_CONSISTENCY_STATUS = ("SUMMARY_REPORTS_DEFECT", "SUMMARY_MAY_REPORT_DEFECT")


def _review_consistency(stages: Any) -> dict[str, Any] | None:
    """The router's recorded summary-consistency verdict, bounded; the first reviewed stage wins."""
    if not isinstance(stages, dict):
        return None
    for stage in ("backend", "macReturn"):
        report = stages.get(stage)
        record = report.get("reviewConsistency") if isinstance(report, dict) else None
        if not isinstance(record, dict) or record.get("status") not in _CONSISTENCY_STATUS:
            continue
        evidence = record.get("evidence")
        evidence = (re.sub(r"[\s\x00-\x1f\x7f]+", " ", evidence).strip()[:160] or None) if isinstance(evidence, str) else None
        return {"status": record["status"], "rule": _label(record.get("rule")), "evidence": evidence, "stage": stage}
    return None


def _confidence(value: Any) -> float | None:
    return value if _finite(value) and 0 <= value <= 1 else None


def _nonnegative_int(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= _MAX_SAFE_INT else None


def _intake_usage(usage: Any) -> dict[str, Any]:
    """Validate intake counts and derive only the redundant per-call sum."""
    empty = {"inputTokens": None, "outputTokens": None, "totalTokens": None, "totalSource": None}
    if not isinstance(usage, dict):
        return empty
    inputs = _nonnegative_int(usage.get("input_tokens"))
    outputs = _nonnegative_int(usage.get("output_tokens"))
    if inputs is None or outputs is None or inputs + outputs > _MAX_SAFE_INT:
        return empty
    summed = inputs + outputs
    if "total_tokens" in usage:
        reported = _nonnegative_int(usage.get("total_tokens"))
        if reported is None or reported != summed:
            return empty
        return {"inputTokens": inputs, "outputTokens": outputs,
                "totalTokens": reported, "totalSource": "explicit_total"}
    return {"inputTokens": inputs, "outputTokens": outputs,
            "totalTokens": summed, "totalSource": "sum_of_reported_input_output"}


def _archive_files() -> list[Path]:
    """Enumerate at most 256 names in the private fixed archive directory."""
    try:
        info = _ROOT.joinpath("archive").lstat()
        archive = _ROOT / "archive"
        if (archive.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return []
        rows = []
        with os.scandir(archive) as entries:
            # Scan up to 4096 names, then sort by mtime: the newest runs are never cut off by
            # directory order once the archive grows past a screenful (spec 6.12).
            for index, entry in enumerate(entries):
                if index >= _MAX_ARCHIVE_SCAN:
                    break
                if not entry.name.endswith(".json") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\.json", entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077 == 0 and st.st_nlink == 1:
                        rows.append((st.st_mtime, Path(entry.path)))
                except OSError:
                    continue
        return [path for _, path in sorted(rows, reverse=True)[:_MAX_RUNS]]
    except OSError:
        return []


def _read_archive(path: Path) -> dict[str, Any]:
    """Read one bounded private archive without following links."""
    before = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("unsafe archive")
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_nlink != 1
                or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("unsafe archive")
        data = bytearray()
        while len(data) <= _MAX_ARCHIVE_BYTES:
            part = os.read(fd, min(65536, _MAX_ARCHIVE_BYTES + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        return _json_object(bytes(data), _MAX_ARCHIVE_BYTES)
    finally:
        os.close(fd)


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _role_identity_only(backend: dict[str, Any], role: str, parent_run_id: str) -> dict[str, Any] | None:
    """Retain exact saved role identity while leaving unbound activity unknown."""
    rows = backend.get("roleEvidence")
    row = rows.get(role) if isinstance(rows, dict) else None
    stage = "draft" if role == "author" else "review"
    expected_id = "opencode.advisory:" + parent_run_id + "." + stage
    if (not isinstance(row, dict) or row.get("runId") != expected_id
            or row.get("operation") != stage):
        return None
    requested, reported = row.get("requestedModel"), row.get("reportedModel")
    requested = requested if isinstance(requested, str) and _SAFE_MODEL.fullmatch(requested) else None
    reported = reported if isinstance(reported, str) and _SAFE_MODEL.fullmatch(reported) else None
    status = row.get("status")
    status = status if isinstance(status, str) and _SAFE_STATUS.fullmatch(status) else "unknown"
    return {"id": expected_id, "role": role, "model": reported, "servedModel": reported,
            "requestedModel": requested, "status": status, "state": "unknown", "elapsedMs": None,
            "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None}}


def _verified_stage_call(backend: dict[str, Any], role: str, parent_run_id: str,
                         common_candidate: Any) -> dict[str, Any] | None:
    """Bind one returned Nisi envelope to the matching role receipt."""
    stages, selection = backend.get("stages"), backend.get("selection")
    prepare = stages.get("prepare") if isinstance(stages, dict) else None
    if not isinstance(stages, dict) or not isinstance(selection, dict) or not isinstance(prepare, dict):
        return None
    stage = "draft" if role == "author" else "review"
    call_run = parent_run_id + "." + stage
    model = selection.get("authorModel" if role == "author" else "reviewerModel")
    task_fp = prepare.get("taskFingerprint" if role == "author" else "reviewTaskFingerprint")
    if not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model) or not _hex64(task_fp) or not _hex64(common_candidate):
        return None
    envelope = stages.get(stage)
    if (not isinstance(envelope, dict) or envelope.get("status") != "RETURNED"
            or envelope.get("operation") != "work" or envelope.get("recoveryRequired") is not False):
        return None
    result = envelope.get("result")
    if (not isinstance(result, dict) or result.get("status") != "RESPONSE_VALIDATED"
            or result.get("runId") != call_run or result.get("mode") != stage
            or result.get("accepted") is not False or result.get("advisoryOnly") is not True
            or result.get("outputMode") != "json_schema"
            or any(result.get(key) != "NOT_RUN" for key in ("checks", "tests", "certification"))
            or result.get("candidateFingerprint") != common_candidate):
        return None
    binding = result.get("binding")
    if (not isinstance(binding, dict) or binding.get("runId") != "opencode.advisory:" + call_run
            or binding.get("taskFingerprint") != task_fp
            or binding.get("candidateFingerprint") != common_candidate):
        return None
    receipt_rows = result.get("receipts")
    retained_rows = backend.get("roleEvidence")
    receipt = receipt_rows[0] if isinstance(receipt_rows, list) and len(receipt_rows) == 1 else None
    retained = retained_rows.get(role) if isinstance(retained_rows, dict) else None
    if not isinstance(receipt, dict) or not isinstance(retained, dict):
        return None
    expected = {"status": "RESPONSE_VALIDATED", "requestedModel": model, "reportedModel": model,
                "operation": stage, "taskFingerprint": task_fp,
                "candidateFingerprint": common_candidate, "runId": "opencode.advisory:" + call_run}
    if any(receipt.get(key) != value or retained.get(key) != value for key, value in expected.items()):
        return None
    for key in ("requestSha256", "responseSha256", "contentSha256", "resultCandidateFingerprint"):
        if not _hex64(receipt.get(key)) or retained.get(key) != receipt.get(key):
            return None
    usage = receipt.get("usage")
    if not isinstance(usage, dict) or retained.get("usage") != usage:
        return None
    inputs = _nonnegative_int(usage.get("promptTokens"))
    outputs = _nonnegative_int(usage.get("completionTokens"))
    total = _nonnegative_int(usage.get("totalTokens"))
    if inputs is None or outputs is None or total is None or inputs + outputs != total:
        return None
    elapsed = _nonnegative_int(receipt.get("elapsedMs"))
    if elapsed is None or retained.get("elapsedMs") != elapsed:
        return None
    lifecycle = receipt.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle != retained.get("lifecycle"):
        return None
    return {"id": "opencode.advisory:" + call_run, "role": role, "model": model,
            "requestedModel": model, "servedModel": model, "status": "RESPONSE_VALIDATED",
            "state": "complete", "elapsedMs": elapsed,
            "usage": {"inputTokens": inputs, "outputTokens": outputs, "totalTokens": total}}


def _completion_evidence(value: Any) -> dict[str, Any] | None:
    """Copy only bounded criterion coverage, never proof or candidate content.

    An unknown v1 field or inconsistent tuple invalidates the entire projection.
    This is a display boundary, not a verification of the router's evidence.
    """
    if not isinstance(value, dict) or value.keys() != _COMPLETION_FIELDS:
        return None
    total, verified, unassessed = (value.get(key) for key in
                                   ("criteriaTotal", "criteriaVerified", "criteriaUnassessed"))
    if (any(type(count) is not int or not 0 <= count <= 64 for count in
            (total, verified, unassessed))
            or verified + unassessed != total):
        return None
    verdict, code, progress = (value.get(key) for key in
                               ("verdict", "code", "progressStatus"))
    expected_verdict = ("VERIFIED" if total > 0 and verified == total else
                        "PARTIAL" if verified > 0 else "UNVERIFIED")
    if (value.get("kind") != "agiw.completion-evidence.v1"
            or verdict != expected_verdict
            or not isinstance(code, str) or code not in _COMPLETION_CODES
            or not isinstance(progress, str) or progress not in _COMPLETION_PROGRESS
            or (code == "COVERAGE_ASSESSED" and total == 0)
            or (code != "COVERAGE_ASSESSED" and (verified != 0 or progress != "HISTORY_UNKNOWN"))
            or value.get("dispatchAuthorized") is not False
            or value.get("workflowAcceptance") != "NOT_ESTABLISHED"
            or value.get("releaseAcceptance") != "NOT_ESTABLISHED"
            or value.get("stopReason") != ("NO_PROGRESS" if progress == "NO_PROGRESS" else None)):
        return None
    indices = value["verifiedCriterionIndices"]
    if (not isinstance(indices, list) or len(indices) != verified
            or any(type(i) is not int or not 0 <= i < total for i in indices)
            or indices != sorted(set(indices))):
        return None
    fingerprint = value.get("evidenceFingerprint")
    if code == "COVERAGE_ASSESSED":
        if not isinstance(fingerprint, str) or re.fullmatch(r"[a-f0-9]{64}", fingerprint) is None:
            return None
    elif fingerprint is not None:
        return None
    return {"verdict": verdict, "code": code, "criteriaTotal": total,
            "criteriaVerified": verified, "criteriaUnassessed": unassessed,
            "progressStatus": progress, "stopReason": value["stopReason"]}


def _completion_owner_bound(record: dict[str, Any], result: dict[str, Any],
                            run_id: str, status: str) -> bool:
    """Accept new completion display only from a coherent completed owner row."""
    digest = record.get("inputSha256")
    return (
        set(record) == {"schemaVersion", "runId", "inputSha256", "result",
                        "exitCode", "stage", "checkpoint", "finishedUnix"}
        and type(record.get("schemaVersion")) is int and record["schemaVersion"] == 1
        and type(record.get("exitCode")) is int and record["exitCode"] == 0
        and isinstance(digest, str) and re.fullmatch(r"[a-f0-9]{64}", digest) is not None
        and result.get("requestSha256") == digest and result.get("runId") == run_id
        and type(result.get("schemaVersion")) is int and result["schemaVersion"] == 1
        and status == "RESPONSE_VALIDATED"
        and result.get("advisoryOnly") is True and result.get("accepted") is False
        and result.get("authorizing") is False and result.get("filesApplied") is False
        and result.get("recoveryRequired") is False
        and result.get("tests") == "NOT_RUN" and result.get("certification") == "NOT_RUN"
        and isinstance(record.get("stage"), str) and _STAGE.fullmatch(record["stage"]) is not None
        and _finite(record.get("finishedUnix"))
    )


def _project_archive(record: dict[str, Any], now: float) -> dict[str, Any] | None:
    """Project only the router's stable envelope and typed role receipt fields."""
    run_id = record.get("runId")
    result = record.get("result")
    finished = record.get("finishedUnix")
    if (not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id)
            or not isinstance(result, dict) or result.get("kind") != "codemode.router.v1"
            or result.get("runId") != run_id):
        return None
    status = result.get("status")
    if not isinstance(status, str) or not _SAFE_STATUS.fullmatch(status):
        status = "unknown"
    age = _age(finished, now)
    calls = []
    stages = result.get("stages")
    backend = stages.get("backend") if isinstance(stages, dict) else None
    if isinstance(backend, dict):
        for role in ("author", "reviewer"):
            call = _verified_stage_call(backend, role, run_id, backend.get("candidateFingerprint"))
            if call is None:
                call = _role_identity_only(backend, role, run_id)
            if call:
                calls.append(call)
    intake = result.get("intake")
    if not isinstance(intake, dict) and isinstance(backend, dict):
        backend_stages = backend.get("stages")
        intake = backend_stages.get("intake") if isinstance(backend_stages, dict) else None
    if not isinstance(intake, dict) and isinstance(stages, dict):
        intake = stages.get("intake")
    route = intake.get("route") if isinstance(intake, dict) else None
    choice = route.get("choice") if isinstance(route, dict) else None
    if not isinstance(choice, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", choice):
        choice = None
    decision = intake.get("decision") if isinstance(intake, dict) else None
    route_state = _label(route.get("route_state")) if isinstance(route, dict) else None
    if isinstance(decision, dict) and decision.get("route") == choice and choice is not None:
        route_state = _label(decision.get("route_state"))
    usage = intake.get("usage") if isinstance(intake, dict) else None
    usage_out = {"inputTokens": None, "outputTokens": None, "totalTokens": None}
    if isinstance(usage, dict):
        jev_usage = _intake_usage(usage)
        jev_model, jev_status = intake.get("model"), intake.get("status")
        if (isinstance(jev_model, str) and _SAFE_MODEL.fullmatch(jev_model)
                and isinstance(jev_status, str) and _SAFE_STATUS.fullmatch(jev_status)):
            calls.append({"id": None, "role": "intake", "model": jev_model,
                          "requestedModel": None, "servedModel": jev_model,
                          "status": jev_status, "state": "complete" if jev_status == "JUDGED" else "unknown",
                          "elapsedMs": _nonnegative_int(intake.get("elapsed_ms")),
                          "decision": {"choice": choice,
                                       "routeState": route_state,
                                       "confidence": _confidence(route.get("confidence")) if isinstance(route, dict) else None},
                          "usage": jev_usage})
    # Individual stage metrics do not prove every attempted invocation is
    # represented. Without an independent manifest, whole-run usage is unknown.
    stage = record.get("stage")
    completion_bound = _completion_owner_bound(record, result, run_id, status)
    completion = _completion_evidence(result.get("completionEvidence")) if completion_bound else None
    return {"runId": run_id, "stage": stage if isinstance(stage, str) and _STAGE.fullmatch(stage) else None,
            "status": status, "activity": "completed", "recordedAt": finished if _finite(finished) else None,
            "ageSeconds": age, "routeChoice": choice, "routeState": route_state, "hostAcceptance": None,
            "reviewConsistency": _review_consistency(stages),
            "completionEvidence": completion,
            "completionEvidenceSource": "router-archive" if completion is not None else None,
            "testsStatus": "NOT_RUN" if completion_bound else None,
            "client": result.get("client") if result.get("client") in ("codex", "claude", "opencode") else None,
            "host": result.get("selectedHost") if result.get("selectedHost") in ("mac", "windows") else None,
            "calls": calls, "usage": usage_out,
            "usageCoverage": {"scope": "reported-calls-only", "reportedCallCount": len(calls),
                              "expectedCallCount": None, "complete": False,
                              "detail": "No independent expected-call manifest; whole-run totals unknown"},
            "source": "router-archive",
            "_routerRequestSha256": record.get("inputSha256") if isinstance(record.get("inputSha256"), str) and re.fullmatch(r"[a-f0-9]{64}", record.get("inputSha256")) else None}


def _receipt_files() -> list[Path]:
    """Enumerate recent sanitized monitor receipts; never create the directory."""
    try:
        info = _RECEIPTS.lstat()
        if (_RECEIPTS.is_symlink() or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077):
            return []
        rows = []
        with os.scandir(_RECEIPTS) as entries:
            for index, entry in enumerate(entries):
                if index >= _MAX_ARCHIVE_ENTRIES:
                    break
                if not entry.name.endswith(".json") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\.json", entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) & 0o077 == 0 and st.st_nlink == 1:
                        rows.append((st.st_mtime, Path(entry.path)))
                except OSError:
                    continue
        return [path for _, path in sorted(rows, reverse=True)[:_MAX_RUNS]]
    except OSError:
        return []


def _project_monitor_receipt(receipt: dict[str, Any], now: float) -> dict[str, Any] | None:
    if receipt.get("kind") != "codemode.monitor.run-receipt.v1" or receipt.get("schemaVersion") != 1:
        return None
    run_id = receipt.get("runId")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        return None
    route = receipt.get("route")
    owner = receipt.get("ownerDecision")
    intake = receipt.get("intakeDecision")
    review = receipt.get("review")
    if not all(isinstance(x, dict) for x in (route, owner, intake, review)):
        return None
    calls = []
    source_calls = receipt.get("calls")
    if isinstance(source_calls, list):
        for row in source_calls[:8]:
            if not isinstance(row, dict):
                continue
            role, model = row.get("role"), row.get("reportedModel")
            if (not isinstance(role, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,32}", role)
                    or not isinstance(model, str) or not _SAFE_MODEL.fullmatch(model)):
                continue
            usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
            binding = row.get("bindings") if isinstance(row.get("bindings"), dict) else {}
            call_id = binding.get("runId")
            if not isinstance(call_id, str) or len(call_id) > 180 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,179}", call_id):
                call_id = None
            if role in ("author", "reviewer") and call_id != "opencode.advisory:" + run_id + (".draft" if role == "author" else ".review"):
                continue
            requested = row.get("requestedModel")
            requested = requested if isinstance(requested, str) and _SAFE_MODEL.fullmatch(requested) else None
            status = row.get("status")
            status = status if isinstance(status, str) and _SAFE_STATUS.fullmatch(status) else "unknown"
            inputs, outputs, total = (_nonnegative_int(usage.get(k)) for k in ("inputTokens", "outputTokens", "totalTokens"))
            if not (usage.get("complete") is True and inputs is not None and outputs is not None
                    and total is not None and inputs + outputs == total):
                inputs = outputs = total = None
            calls.append({"id": call_id, "parentRunId": run_id, "role": role,
                          "model": model, "servedModel": model, "requestedModel": requested,
                          "state": "complete" if status not in ("NOT_RUN", "ERROR", "unknown") else "unknown",
                          "status": status, "elapsedMs": _nonnegative_int(row.get("elapsedMs")),
                          "startedAt": None, "finishedAt": None,
                          "usage": {"inputTokens": inputs, "outputTokens": outputs, "totalTokens": total}})
    totals = receipt.get("usageTotals") if isinstance(receipt.get("usageTotals"), dict) else {}
    started_at, finished_at = receipt.get("startedAt"), receipt.get("finishedAt")
    finished_unix = None
    if isinstance(finished_at, str):
        try:
            import datetime
            finished_unix = datetime.datetime.fromisoformat(finished_at.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError):
            pass
    route_status = route.get("status") if isinstance(route.get("status"), str) and _SAFE_STATUS.fullmatch(route.get("status")) else "unknown"
    owner_status = owner.get("status") if isinstance(owner.get("status"), str) and _SAFE_STATUS.fullmatch(owner.get("status")) else "unknown"
    for call in calls:
        call["parentRunId"] = run_id
    # A self-reported logicalCallCount is not an independent manifest of every
    # inference attempt; keep whole-run totals unknown even when included rows sum.
    reported_usage = {"inputTokens": None, "outputTokens": None, "totalTokens": None}
    route_choice = _label(intake.get("route"))
    decision = {"choice": route_choice,
                "routeState": _label(intake.get("routeState")),
                "confidence": _confidence(intake.get("confidence"))}
    for call in calls:
        if call["role"] == "intake":
            call["decision"] = decision
    return {"runId": run_id, "stage": "completed", "status": route_status,
            "hostAcceptance": owner_status, "activity": "completed",
            "client": route.get("client") if route.get("client") in ("codex", "claude", "opencode") else None,
            "recordedAt": finished_unix, "ageSeconds": _age(finished_unix, now),
            "routeChoice": decision["choice"], "routeState": decision["routeState"],
            "routeConfidence": decision["confidence"],
            "reviewFindings": _nonnegative_int(review.get("structuredFindingCount")),
            "reviewSummaryContradiction": review.get("summaryContradictsEmptyFindings") if type(review.get("summaryContradictsEmptyFindings")) is bool else None,
            "note": "Owner decision is separate from router status",
            "calls": calls, "usage": reported_usage,
            "usageCoverage": {"scope": "reported-calls-only", "reportedCallCount": len(calls),
                              "expectedCallCount": None, "complete": False,
                              "detail": "No independent expected-call manifest; whole-run totals unknown"},
            "source": "monitor-receipt",
            "_routerRequestSha256": receipt.get("bindings", {}).get("routerRequestSha256") if isinstance(receipt.get("bindings"), dict) and isinstance(receipt.get("bindings", {}).get("routerRequestSha256"), str) and re.fullmatch(r"[a-f0-9]{64}", receipt.get("bindings", {}).get("routerRequestSha256")) else None}


def _receipt_runs(now: float) -> list[dict[str, Any]]:
    result = []
    for path in _receipt_files():
        try:
            item = _project_monitor_receipt(_read_archive(path), now)
            if item is not None:
                result.append(item)
        except Exception:           # one malformed receipt (a RecursionError included) is skipped
            continue
    return result


_LIVE_STATES = ("running", "waiting", "admitting")


def _observed_live(observed: dict[str, Any] | None) -> bool:
    return bool(observed and observed.get("live") is True and observed.get("state") in _LIVE_STATES)


def _active_rows(sampled: float) -> tuple[list[dict[str, Any]], bool, str]:
    """One row per active record of either layout, plus every run this sample's observation
    verified live that the 4 record reads here did not list: a live run whose record is older
    than the newest 4, or a run queued before its first record (spec 6.12).  (rows, problem,
    listing): ``problem`` is an unreadable legacy file, a record naming an invalid run or an
    unsafe active/ directory.  Status is the lock-verified phase for a live run ('running',
    'waiting', 'admitting'), else 'stale' / 'unresolved' as recorded; ``activity`` is
    'running', 'queued' or 'unknown' (no liveness observed in this sample).  A record read
    here takes a live observation only when it is whole and names the same run, layout and
    input the observation verified: the observation may be up to 5 s old, and an unreadable
    record is never promoted to running."""
    records, problem, listing = _read_actives()
    live = _live_rows()
    rows: list[dict[str, Any]] = []
    for record in records:
        run_id = record.get("runId")
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
            problem = True
            continue
        if record.get("_unreadable"):
            stage, recorded, age, status = None, None, None, "unreadable"
        else:
            stage = record.get("stage")
            if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
                stage = None
            checkpoint = record.get("checkpoint")
            recorded = checkpoint.get("recordedUnix") if isinstance(checkpoint, dict) else None
            age = _age(recorded, sampled)
            status = "stale" if age is None or age > _MAX_AGE else "unresolved"
        observed = live.get((run_id, record["_layout"]))
        activity = "unknown"
        if observed and not observed.get("live") and observed.get("state") == "changing":
            status = "changing"     # changed while observed (a checkpoint): re-checked next sample
        # The observation describes this record only when both name the same input.
        bound = (observed if observed and not record.get("_unreadable")
                 and observed.get("inputSha256") == record.get("inputSha256") else None)
        if _observed_live(bound):
            status = bound["state"]
            activity = "running" if status == "running" else "queued"
            if age is None:
                age = _age(recorded if recorded is not None else bound.get("startedUnix"), sampled)
        rows.append({"runId": run_id, "stage": stage, "status": status,
                     "recordedAt": recorded if _finite(recorded) else None,
                     "ageSeconds": age, "activity": activity, "layout": record["_layout"],
                     "client": bound.get("client") if bound and bound.get("client") in ("codex", "claude", "opencode") else None,
                     "host": bound.get("host") if bound and bound.get("host") in ("mac", "windows") else None,
                     "calls": [], "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None}})
    listed = {(row["runId"], row["layout"]) for row in rows}
    for (run_id, layout), observed in live.items():
        if (run_id, layout) in listed or not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) \
                or layout not in ("per-run", "legacy", "note") or not _observed_live(observed):
            continue
        note = observed.get("note") if isinstance(observed.get("note"), dict) else {}
        if layout == "note":
            stage, recorded = None, note.get("sinceUnix")
        elif not os.path.lexists(str(_ACTIVE if layout == "legacy" else _ACTIVE.parent / "active" / (run_id + ".json"))):
            continue                    # its record is gone since the observation: it finished
        else:
            stage = observed.get("stage") if isinstance(observed.get("stage"), str) and _STAGE.fullmatch(observed["stage"]) else None
            recorded = observed.get("recordedUnix")
            if not _finite(recorded):
                recorded = observed.get("startedUnix")
        state = observed["state"]
        rows.append({"runId": run_id, "stage": stage, "status": state,
                     "recordedAt": recorded if _finite(recorded) else None,
                     "ageSeconds": _age(recorded, sampled),
                     "activity": "running" if state == "running" else "queued", "layout": layout,
                     "client": observed.get("client") if observed.get("client") in ("codex", "claude", "opencode") else None,
                     "host": observed.get("host") if observed.get("host") in ("mac", "windows") else None,
                     "calls": [], "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None}})
    rows.sort(key=lambda row: (row["activity"] != "unknown", row["recordedAt"] or 0.0), reverse=True)
    return rows[:_MAX_RUNS], problem, listing


def collect_activity(now: float | None = None) -> dict[str, Any]:
    """Return bounded sanitized route/call receipts; never reads raw call ledgers."""
    sampled = time.time() if now is None else now
    source = {"id": "router-active", "label": "Current router checkpoint",
              "state": "unavailable", "ageSeconds": None,
              "detail": "Router checkpoint unavailable"}
    runs = _receipt_runs(sampled)
    receipt_fresh = any(row.get("ageSeconds") is not None and row["ageSeconds"] <= _MAX_AGE for row in runs)
    receipt_source = {"id": "monitor-receipts", "label": "Sanitized call receipts",
                      "state": "live" if receipt_fresh else "stale" if runs else "unavailable",
                      "ageSeconds": min((row["ageSeconds"] for row in runs if row.get("ageSeconds") is not None), default=None),
                      "detail": "Bounded local receipt projection" if receipt_fresh else "Only historical or unavailable receipts"}
    receipt_by_id = {row["runId"]: row for row in runs}
    for path in _archive_files():
        try:
            item = _project_archive(_read_archive(path), sampled)
            if item is not None:
                receipt = receipt_by_id.get(item["runId"])
                same_trace = (receipt is not None
                              and receipt.get("_routerRequestSha256") is not None
                              and receipt.get("_routerRequestSha256") == item.get("_routerRequestSha256")
                              and receipt.get("status") == item.get("status"))
                if same_trace:
                    # The receipt owns call and host-acceptance display, while the
                    # exact matching router archive owns criterion evidence.
                    # Both sides are already projected and bounded.
                    receipt["completionEvidence"] = item["completionEvidence"]
                    receipt["completionEvidenceSource"] = item["completionEvidenceSource"]
                    receipt["testsStatus"] = item["testsStatus"]
                    continue
                runs.append(item)
        except Exception:           # one malformed archive (a RecursionError included) is skipped
            continue
    try:
        active_rows, problem, listing = _active_rows(sampled)
    except Exception:
        # One malformed record must never fail the whole sample (spec R2.9 rule 2): the
        # readers above validate every field they use and make a record that fails in any
        # other way one unreadable row; this is only the last line of defence.
        active_rows, problem, listing = [], True, "ok"
    cut = "; the active/ listing was cut at 4096 entries" if listing == "truncated" else ""
    if active_rows:
        ages = [row["ageSeconds"] for row in active_rows if row.get("ageSeconds") is not None]
        fresh = any(row["activity"] in ("running", "queued") for row in active_rows) or any(
            age <= _MAX_AGE for age in ages)
        live_count = sum(1 for row in active_rows if row["activity"] in ("running", "queued"))
        source.update(state="error" if problem else "live" if fresh else "stale",
                      ageSeconds=min(ages, default=None),
                      detail=(f"{len(active_rows)} router run record(s) sampled; {live_count} verified live by their "
                              "layout's liveness checks" if live_count else
                              "Checkpoint sampled; live execution and generation are unverified")
                      + ("; the router run directory is unsafe or another record was unreadable" if problem else "")
                      + cut)
        # Active rows carry their layout in the trace key: one run ID can have a row in each
        # layout (a collision), and the two must stay distinct.
        for item in active_rows:
            item["traceKey"] = "router-active:" + item["runId"] + ":" + item["layout"]
        active_ids = {row["runId"] for row in active_rows}
        all_runs = (active_rows + [r for r in runs if r["runId"] not in active_ids])[:_MAX_RUNS]
        for item in all_runs:
            if "traceKey" not in item:
                item["traceKey"] = item.get("source", "router-active") + ":" + item["runId"] + ":" + (item.pop("_routerRequestSha256", None) or "unknown")
        return {"runs": all_runs,
                "sources": [source, receipt_source, {"id": "router-archive", "label": "Recent completed routes",
                                     "state": "live" if any(r.get("ageSeconds") is not None and r["ageSeconds"] <= _MAX_AGE for r in runs) else "stale",
                                     "ageSeconds": min((r["ageSeconds"] for r in runs if r.get("ageSeconds") is not None), default=None),
                                     "detail": "Bounded sanitized archive records"}]}
    if problem:
        source.update(state="error", detail="Router checkpoint invalid or unreadable, or the router run directory is unsafe"
                                            + cut)
    elif listing == "truncated":
        source.update(state="error", detail="No unresolved router pointer among the listed records" + cut)
    else:
        source["detail"] = "No unresolved router pointer; history is reported separately"
    runs = runs[:_MAX_RUNS]
    for item in runs:
        item["traceKey"] = item.get("source", "router-active") + ":" + item["runId"] + ":" + (item.pop("_routerRequestSha256", None) or "unknown")
    fresh_archive = any(r.get("source") == "router-archive" and r.get("ageSeconds") is not None and r["ageSeconds"] <= _MAX_AGE for r in runs)
    archive_exists = any(r.get("source") == "router-archive" for r in runs)
    return {"runs": runs, "sources": [source, receipt_source, {"id": "router-archive",
            "label": "Recent completed routes", "state": "live" if fresh_archive else "stale" if archive_exists else "unavailable",
            "ageSeconds": min((r["ageSeconds"] for r in runs if r.get("source") == "router-archive" and r.get("ageSeconds") is not None), default=None),
            "detail": "Bounded sanitized archive records" if fresh_archive else "Only historical or unavailable archive records"}]}
