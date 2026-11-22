"""Probe forwarded workers, pre-analyze the fixed program task, dispatch one local job.

Worker credentials are never sent to Jev. Every worker keeps its bearer token over the
tunnel; readiness and dispatch are both authenticated and bounded.
"""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import fleet_jev
from . import fleet_local_pilot as pilot
from . import fleet_routing_plan as routing
from . import windows_worker as worker_module
from . import windows_worker_receipt_check as receipts


class RefusedError(RuntimeError):
    """A fail-closed refusal; the message is a stable code."""


Refused = RefusedError  # established name used by callers and tests


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise Refused("REDIRECT_REFUSED")


TIMEOUT = 3
# A dispatched job may run up to the pilot's 25s child wall timeout plus store/receipt time.
JOB_TIMEOUT = 45


def worker_url(value, allow_lan=False):
    if not isinstance(value, str) or not value:
        raise Refused("URL_REQUIRED")
    parts = urlsplit(value)
    if (
        parts.scheme != "http"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not parts.port
    ):
        raise Refused("URL_REFUSED")
    if parts.hostname != "127.0.0.1" and not allow_lan:
        raise Refused("NON_LOOPBACK_URL_REQUIRES_ALLOW_LAN")
    return f"http://127.0.0.1:{parts.port}" if parts.hostname == "127.0.0.1" else value.rstrip("/")


def job_timeout(name, value=None):
    """HTTP wait for one dispatched job: the lane's model timeout plus margin, unless configured."""
    if value is None:
        lane = pilot.lane_defaults(name)
        if "job_timeout_s" in lane:
            value = lane["job_timeout_s"]
        elif "model_timeout_s" in lane:
            value = lane["model_timeout_s"] + 60
        else:
            return 90
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 1200:
        raise Refused("JOB_TIMEOUT_INVALID")
    return value


def _response_format(name, value):
    try:
        return pilot.response_format(name, value)
    except pilot.Refused as exc:
        raise Refused(str(exc)) from exc


def _request_mode(name, value):
    try:
        return pilot.request_mode(name, value)
    except pilot.Refused as exc:
        raise Refused(str(exc)) from exc


def _jev_mode(mode):
    if mode not in ("required", "off"):
        raise Refused("JEV_PREANALYSIS_INVALID")
    return mode


def load_workers(path):
    """Read and validate the per-worker config. Tokens stay out of returned logs."""
    try:
        config = receipts.parse_receipt(Path(path).read_bytes())
    except (ValueError, RecursionError):
        raise Refused("CONFIG_INVALID") from None
    if not isinstance(config, dict):
        raise Refused("CONFIG_INVALID")
    _jev_mode(config.get("jev_preanalysis", "required"))
    rows = config.get("workers")
    if not isinstance(rows, dict) or not rows:
        raise Refused("WORKERS_REQUIRED")
    workers = {}
    for name, row in rows.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise Refused("WORKER_CONFIG_INVALID")
        if name not in routing.WORKERS:
            raise Refused("UNKNOWN_WORKER_ID")
        token_file = row.get("token_file")
        if not isinstance(token_file, str) or not token_file:
            raise Refused("TOKEN_REQUIRED")
        token = worker_module.load_token(token_file)
        workers[name] = {
            "url": worker_url(row.get("url"), row.get("allow_lan") is True),
            "token": token,
            "model": row.get("model"),
            "job_timeout_s": job_timeout(name, row.get("job_timeout_s")),
            "model_timeout_s": row.get("model_timeout_s"),
            "disable_thinking": row.get("disable_thinking") is True,
            "max_tokens": row.get("max_tokens"),
            "response_format": _response_format(name, row.get("response_format")),
            "request_mode": _request_mode(name, row.get("request_mode")),
        }
    return workers, config


class FleetRouterClient:
    def __init__(self, workers, opener=None, now_fn=None, *, judge=None, jev_preanalysis="required"):
        self.workers = workers
        self.judge = judge
        self.jev_preanalysis = _jev_mode(jev_preanalysis)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.now_fn = now_fn or time.time

    def request(self, name, method, path, body=None):
        row = self.workers[name]
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": "Bearer " + row["token"].decode("utf-8")}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(row["url"] + path, data=data, headers=headers, method=method)
        timeout = row.get("job_timeout_s", JOB_TIMEOUT) if method == "POST" else TIMEOUT
        with self.opener.open(req, timeout=timeout) as response:
            return response.status, receipts.parse_receipt(response.read(65537))

    def probe(self):
        now = self.now_fn()
        workers = {}
        for name in self.workers:
            try:
                status, ready = self.request(name, "GET", "/v1/ready")
            except pilot.PROBE_ERRORS:
                continue  # unreachable, 401, redirect or malformed: unknown, not eligible
            if status != 200 or not isinstance(ready, dict):
                continue
            if ready.get("worker_id") != name:
                continue
            busy = ready.get("busy")
            if busy is not True and busy is not False:
                continue
            workers[name] = routing.WorkerEvidence(
                observed_at=now,
                local_model_ready=ready.get("model_ready") is True,
                authenticated=True,
                lease_available=not busy,
                busy=busy,
            )
        return workers, now

    def _dispatch_route(self, program, decision):
        route = {"worker": decision.worker, "preanalysis": decision.preanalysis, "reason": decision.reason}
        if program == worker_module.fleet_repo_patch.PROGRAM:
            return dict(route, reason="jev-not-applicable")
        if self.jev_preanalysis == "off":
            return route
        evidence = fleet_jev.preanalyze(program, self.judge)
        evidence.pop("status")
        decision = routing.RoutePlan.for_local(decision.worker, jev_preanalyzed=True)
        return dict(route, preanalysis=decision.preanalysis, reason=decision.reason, jev=evidence)

    def run(self, job_id, program="nisi-smoke.mjs", workload="routine", payload=None):
        if program not in worker_module.PROGRAMS:
            raise Refused("PROGRAM_REFUSED")
        evidence, now = self.probe()
        decision = routing.plan("local", workload, evidence, routing.HostedEvidence(), now=now, source="agent")
        if decision.worker is None:
            return {"outcome": "BLOCKED", "reason": decision.reason, "worker": None, "job_id": job_id}
        name = decision.worker
        try:
            route = self._dispatch_route(program, decision)
        except fleet_jev.JevError as exc:
            reason = "JEV_PREANALYSIS_FAILED:" + exc.code
            return {"outcome": "BLOCKED", "reason": reason, "worker": name, "job_id": job_id}
        body = {
            "job_id": job_id,
            "route": route,
            "program": program,
        }
        if payload is not None:
            body["payload"] = payload
        row = self.workers[name]
        if row.get("model_timeout_s") is not None:
            body["model_timeout_s"] = row["model_timeout_s"]
        if row.get("max_tokens") is not None:
            body["max_tokens"] = row["max_tokens"]
        if row.get("disable_thinking") is True:
            body["disable_thinking"] = True
        if row.get("response_format") is not None:
            body["response_format"] = row["response_format"]
        if row.get("request_mode") not in (None, "chat"):
            body["request_mode"] = row["request_mode"]
        try:
            status, receipt = self.request(name, "POST", "/v1/jobs", body)
        except pilot.PROBE_ERRORS:
            return {"outcome": "BLOCKED", "reason": "DISPATCH_FAILED", "worker": name, "job_id": job_id}
        if status != 200:
            return {"outcome": "BLOCKED", "reason": "DISPATCH_REFUSED", "worker": name, "job_id": job_id}
        verdict = receipts.validate_job_receipt(
            receipt,
            expected_worker=name,
            expected_model=self.workers[name].get("model"),
            job_id=job_id,
            now=self.now_fn(),
        )
        if not verdict["valid"] or receipt.get("reportStored") is not True:
            return {
                "outcome": "BLOCKED",
                "reason": "RECEIPT_INVALID:" + str(verdict["reason"]),
                "worker": name,
                "job_id": job_id,
            }
        if receipt["route"] != route:
            return {"outcome": "BLOCKED", "reason": "RECEIPT_INVALID:ROUTE_MISMATCH", "worker": name, "job_id": job_id}
        return {
            "outcome": receipt["outcome"],
            "worker": name,
            "job_id": job_id,
            "receipt_path": receipt.get("receiptPath"),
            "report_path": verdict["report_path"],
            "reportStored": True,
        }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--program", default="nisi-smoke.mjs")
    p.add_argument("--job-id", default=None)
    p.add_argument("--workload", choices=["routine", "gpu-heavy"], default="routine")
    a = p.parse_args(argv)
    job_id = a.job_id or f"agiw-route-{time.time_ns()}"
    try:
        workers, config = load_workers(a.config)
        client = FleetRouterClient(
            workers,
            judge=fleet_jev.JevJudge(os.environ.get(fleet_jev.KEY_ENV)),
            jev_preanalysis=config.get("jev_preanalysis", "required"),
        )
        result = client.run(job_id, a.program, workload=config.get("workload", a.workload))
    except (Refused, worker_module.Refused) as exc:
        result = {"outcome": "BLOCKED", "reason": str(exc), "job_id": job_id}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        result = {"outcome": "BLOCKED", "reason": "ROUTER_INPUT_INVALID", "job_id": job_id}
    pilot.emit(json.dumps(result, sort_keys=True))
    return 0 if result["outcome"] != "BLOCKED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
