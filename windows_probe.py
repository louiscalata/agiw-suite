"""One bounded end-to-end Windows inference probe for the Fix control.

Run by the monitor as ``/usr/bin/python3 -I -S windows_probe.py SECONDS``.  It
uses the orchestrator's Windows transport Owner, so the job holds the owner
lock, is recorded in the owner's pending file before publication (never
resent, reconcilable by ``--windows reconcile``) and is journaled by the
dispatcher.  It prints one JSON object with no prompt or output text.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

SCRIPTS = Path.home() / ".codex/skills/local-llm-orchestrator/scripts"
PROMPT = "Reply with exactly the word READY and nothing else."
# The probe proves the route, not quality, so it only uses a model that answers
# well inside its budget. Larger models took 73-178 s in recorded runs.
PREFERRED = ("gpt-oss-20b",)
CODES = frozenset({
    "OWNER_BUSY", "OWNER_UNSAFE", "PENDING_EXISTS", "PUBLICATION_UNCERTAIN",
    "PUBLICATION_INVALID", "RESULT_INVALID", "TIMEOUT", "PENDING_INVALID",
    # Router-concurrency transport (spec R2.9 rule 6).  Owner.request takes the Mac-wide
    # fast lane token itself; none of these publishes a job.
    "LANE_BUSY", "LANE_UNKNOWN", "LANE_UNAVAILABLE", "LANE_UNSAFE",
    "PUBLICATION_INTERRUPTED", "PUBLICATION_BINDING_FAILED", "PUBLICATION_BUDGET_EXHAUSTED",
    "OWNER_LOCK_REPLACED", "PENDING_CHANGED",
})
# The transport is a router generation file: it refuses to load while an install or rollback
# is in progress (router_fence.require_loadable).  That is a probe not-run, not an error.
INSTALL_CODE = re.compile(r"ROUTER_INSTALL_[A-Z_]{1,40}\Z")


def _emit(value: dict) -> int:
    print(json.dumps(value, separators=(",", ":")))
    return 0 if value.get("status") == "success" else 3


def main(argv: list[str]) -> int:
    try:
        seconds = int(argv[1]) if len(argv) > 1 else 60
    except ValueError:
        seconds = 0
    if not 10 <= seconds <= 120:
        return _emit({"status": "not-run", "code": "INVALID_TIMEOUT"})
    sys.path.insert(0, str(SCRIPTS))
    try:
        from windows_queue_transport import Owner, TransportError
    except Exception as exc:
        code = getattr(exc, "code", None)
        if isinstance(code, str) and INSTALL_CODE.fullmatch(code):
            return _emit({"status": "not-run", "code": code})
        return _emit({"status": "not-run", "code": "OWNER_UNAVAILABLE"})
    try:
        with Owner() as owner:
            state = owner.status()
            models = state.get("models") if isinstance(state, dict) else None
            if not (isinstance(state, dict) and state.get("ok") is True
                    and isinstance(models, list) and models
                    and all(isinstance(m, str) and m.strip() for m in models)):
                return _emit({"status": "not-run", "code": "WORKER_NOT_READY"})
            model = next((m for m in PREFERRED if m in models), None)
            if model is None:
                return _emit({"status": "not-run", "code": "WORKER_NOT_READY"})
            try:
                result = owner.request(PROMPT, model, timeout=seconds)
            except Exception as exc:
                # Still holding the owner lock: a failure after publication
                # leaves a pending record, which is the job to report.
                code = getattr(exc, "code", None)
                code = code if code in CODES else (
                    "TRANSPORT_ERROR" if isinstance(exc, TransportError) else "PROBE_FAILED")
                job = getattr(exc, "job_id", None)
                if not isinstance(job, str):
                    try:
                        pending = owner.pending()
                    except Exception:
                        pending = None
                    job = pending.get("id") if isinstance(pending, dict) else None
                    job = job if isinstance(job, str) else None
                return _emit({"status": "not-run" if job is None else "unresolved",
                              "code": code, "jobId": job})
    except TransportError as exc:
        code = exc.code if exc.code in CODES else "TRANSPORT_ERROR"
        return _emit({"status": "not-run", "code": code, "jobId": None})
    except Exception:
        return _emit({"status": "not-run", "code": "PROBE_FAILED"})
    status = result.get("status") if isinstance(result, dict) else None
    if status not in ("success", "error"):
        return _emit({"status": "not-run", "code": "RESULT_INVALID"})
    output = result.get("output") if status == "success" else None
    return _emit({
        "status": status, "jobId": result.get("id"), "model": result.get("model"),
        "elapsedSeconds": result.get("elapsed_seconds"),
        "answered": isinstance(output, str) and "READY" in output.upper(),
    })


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
