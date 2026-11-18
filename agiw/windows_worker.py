"""Token-authenticated, loopback-first Jev job worker. Single slot, zero retries.

No hosted routes, no model loads, no credential discovery and no token logging.
Executes one routed Nisi job at a time through the isolated local pilot.
"""

import argparse
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import fleet_local_pilot as pilot
from . import fleet_repo_patch
from . import windows_worker_receipt_check as receipt_check

PROGRAMS = pilot.PROGRAMS + (fleet_repo_patch.PROGRAM,)
BODY_LIMIT = 16 * 1024
JEV_LIMIT = 4096


class RefusedError(RuntimeError):
    """A fail-closed refusal; the message is a stable code."""


Refused = RefusedError  # established name used by callers and tests


class Busy(Refused):
    pass


def validate_bind(bind, allow_lan=False):
    if not isinstance(bind, str) or not bind:
        raise Refused("BIND_REFUSED")
    loopback = bind in ("::1", "localhost") or bind.startswith("127.")
    if not loopback and not allow_lan:
        raise Refused("NON_LOOPBACK_BIND_REQUIRES_ALLOW_LAN")
    return bind


def load_token(path):
    """A shared token is a regular, non-symlink 0600 file of at least 32 bytes."""
    p = Path(path)
    info = os.lstat(str(p))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused("TOKEN_FILE_UNSAFE")
    if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
        raise Refused("TOKEN_MODE_UNSAFE")
    raw = p.read_bytes()
    if len(raw) > 4096:
        raise Refused("TOKEN_TOO_LARGE")
    token = raw.strip()
    if len(token) < 32:
        raise Refused("TOKEN_TOO_SHORT")
    return token


def authorized(header, token):
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[7:].encode("utf-8"), token)


def _safe_name(value):
    return re.sub("[^A-Za-z0-9._-]", "_", value)[:80] or "job"


def _git_commit():
    result = subprocess.run(
        ["git", "-C", str(pilot.ROOT), "rev-parse", "HEAD"],
        env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"),
        capture_output=True,
        encoding="ascii",
        timeout=5,
        check=True,
    )
    return result.stdout.removesuffix("\n")


def _resolve_commit(lookup):
    try:
        value = lookup()
    except Exception:  # noqa: BLE001 - optional startup metadata, including injected lookup failures
        return None
    return value if receipt_check.valid_commit(value) else None


def _validated_route(route, worker_id, program):
    if not receipt_check.valid_route(route, worker_id, program):
        raise Refused("ROUTE_REFUSED")
    saved = {field: route[field] for field in receipt_check.ROUTE_FIELDS}
    if "jev" in route:
        jev = route["jev"]
        if not isinstance(jev, dict):
            raise Refused("ROUTE_REFUSED")
        try:
            encoded = json.dumps(jev, ensure_ascii=True, allow_nan=False)
            if len(encoded) > JEV_LIMIT:
                raise Refused("ROUTE_REFUSED")
            saved["jev"] = receipt_check.parse_receipt(encoded)
        except (TypeError, ValueError, RecursionError):
            raise Refused("ROUTE_REFUSED") from None
    return saved


def write_receipt(directory, receipt):
    """Atomically publish a 0600 receipt. No token or private payload is included."""
    target_dir = Path(directory)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", dir=str(target_dir))
    try:
        final = str(target_dir / f"{_safe_name(str(receipt.get('job_id', 'job')))}-{time.time_ns()}.json")
        receipt["receiptPath"] = final
        with os.fdopen(fd, "w") as handle:
            json.dump(receipt, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return final


class Worker:
    """Holds the single job slot and exposes handler-friendly operations."""

    def __init__(
        self,
        worker_id,
        model,
        model_endpoint,
        receipt_dir,
        report_store=None,
        execute=None,
        probe=None,
        model_timeout_s=None,
        disable_thinking=False,
        max_tokens=None,
        response_format=None,
        request_mode=None,
        repo_patch=None,
        allow_local_source=False,
        *,
        commit=None,
    ):
        if not isinstance(worker_id, str) or not worker_id:
            raise Refused("WORKER_ID_REQUIRED")
        try:
            pilot.check_worker_id(worker_id)  # only ids the router config knows
        except pilot.Refused as exc:
            raise Refused(str(exc)) from exc
        self.worker_id = worker_id
        self.model = model
        self.model_endpoint = model_endpoint
        pilot.endpoint(model_endpoint)
        self.receipt_dir = Path(receipt_dir)
        try:
            self.model_timeout_s = pilot.model_timeout_s(worker_id, model_timeout_s)
        except pilot.Refused as exc:
            raise Refused(str(exc)) from exc
        self.disable_thinking = disable_thinking is True
        try:
            self.max_tokens = pilot.max_tokens(worker_id, max_tokens)
        except pilot.Refused as exc:
            raise Refused(str(exc)) from exc
        try:
            self.response_format = pilot.response_format(worker_id, response_format)
        except pilot.Refused as exc:
            raise Refused(str(exc)) from exc
        try:
            self.request_mode = pilot.request_mode(worker_id, request_mode)
            if self.request_mode != "chat":
                pilot.completion_settings(worker_id)
        except pilot.Refused as exc:
            raise Refused(str(exc)) from exc
        self.report_store = report_store
        self._execute = execute or pilot.execute
        self._repo_patch = repo_patch or fleet_repo_patch.execute_job
        self.allow_local_source = allow_local_source  # tests only: local fixture repos
        self._probe = probe or (lambda: pilot.detect_models(pilot.endpoint(model_endpoint)))
        self._commit = _resolve_commit(_git_commit if commit is None else commit)
        self._lock = threading.Lock()
        self.busy = False

    def ready(self):
        try:
            models, _source = self._probe()
        except pilot.PROBE_ERRORS:
            models = []
        if not isinstance(models, list):
            models = []
        return {
            "worker_id": self.worker_id,
            "commit": self._commit,
            "commit_source": "git" if self._commit is not None else "unavailable",
            "model_ready": self.model in models,
            "models": models,
            "busy": self.busy,
            "observed_at": time.time(),
        }

    def _validate_repo_patch_job(self, job):
        if job.get("program") != fleet_repo_patch.PROGRAM:
            return
        if not isinstance(job.get("payload"), dict):
            raise Refused("PAYLOAD_REFUSED")
        try:
            fleet_repo_patch.validate_spec(job["payload"], allow_local_source=self.allow_local_source)
        except fleet_repo_patch.Refused as exc:
            raise Refused(str(exc)) from None

    def validate_job(self, job):
        job_id, _route = self._validate_job(job)
        return job_id

    def _validate_job(self, job):
        if not isinstance(job, dict):
            raise Refused("BODY_INVALID")
        job_id = job.get("job_id")
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            raise Refused("JOB_ID_REQUIRED")
        route = _validated_route(job.get("route"), self.worker_id, job.get("program"))
        if job.get("program") not in PROGRAMS:
            raise Refused("PROGRAM_REFUSED")
        self._validate_repo_patch_job(job)
        if job.get("model_timeout_s") is not None:
            try:
                pilot.model_timeout_s(self.worker_id, job["model_timeout_s"])
            except pilot.Refused as exc:
                raise Refused(str(exc)) from exc
        if job.get("max_tokens") is not None:
            try:
                pilot.max_tokens(self.worker_id, job["max_tokens"])
            except pilot.Refused as exc:
                raise Refused(str(exc)) from exc
        if job.get("response_format") is not None:
            try:
                pilot.response_format(self.worker_id, job["response_format"])
            except pilot.Refused as exc:
                raise Refused(str(exc)) from exc
        if job.get("request_mode") is not None:
            try:
                pilot.request_mode(self.worker_id, job["request_mode"])
            except pilot.Refused as exc:
                raise Refused(str(exc)) from exc
        if job.get("disable_thinking") not in (None, True, False):
            raise Refused("DISABLE_THINKING_INVALID")
        return job_id, route

    def run_job(self, job):
        job_id, route = self._validate_job(job)
        if not self._lock.acquire(False):
            raise Busy("BUSY")
        self.busy = True
        started = time.time()
        outcome = "BLOCKED"
        timeout_s = pilot.model_timeout_s(self.worker_id, job.get("model_timeout_s", self.model_timeout_s))
        disable_thinking = (
            job["disable_thinking"] if job.get("disable_thinking") in (True, False) else self.disable_thinking
        )
        tokens = job.get("max_tokens") if job.get("max_tokens") is not None else self.max_tokens
        fmt = job.get("response_format") if job.get("response_format") is not None else self.response_format
        req_mode = job.get("request_mode") if job.get("request_mode") is not None else self.request_mode
        failure = None
        report = {}
        result = None
        try:
            if job["program"] == fleet_repo_patch.PROGRAM:
                result = self._repo_patch(
                    job["payload"],
                    model_endpoint=self.model_endpoint,
                    model=self.model,
                    report_store=self.report_store,
                    allow_local_source=self.allow_local_source,
                )
            else:
                result = self._execute(
                    "live",
                    self.model_endpoint,
                    self.model,
                    "agent",
                    job["program"],
                    report_store=self.report_store,
                    worker_id=self.worker_id,
                    timeout_s=timeout_s,
                    disable_thinking=disable_thinking,
                    max_output_tokens=tokens,
                    response_format_mode=fmt,
                    request_mode_value=req_mode,
                    route=route,
                )
            report = result.get("report") if isinstance(result, dict) and isinstance(result.get("report"), dict) else {}
            outcome = report.get("outcome")
            if outcome not in receipt_check.OUTCOMES:
                outcome = "BLOCKED"
            if report.get("reportStored") is not True or result.get("reportStored") is False:
                outcome = "BLOCKED"
        except Exception as exc:  # noqa: BLE001 - job boundary: any failure must still yield a BLOCKED receipt
            outcome = "BLOCKED"  # a failure is never COMPLETED; no retries
            failure = pilot.sanitize_text(f"{type(exc).__name__}: {exc}")
        finally:
            finished = time.time()
            receipt = {
                "kind": receipt_check.JOB_KIND,
                "schema_version": receipt_check.JOB_SCHEMA_VERSION,
                "job_id": job_id,
                "worker_id": self.worker_id,
                "workerCommit": self._commit,
                "route": route,
                "model": self.model,
                "program": job.get("program"),
                "outcome": outcome,
                "reportStored": report.get("reportStored") is True,
                "runId": report.get("runId"),
                "reportPath": result.get("reportPath") if isinstance(result, dict) else None,
                "reportSha256": report.get("storedReportSha256"),
                "code": pilot.sanitize_text(report.get("code")) if isinstance(report.get("code"), str) else None,
                "modelTimeoutS": timeout_s,
                "disableThinking": disable_thinking,
                "maxTokens": tokens,
                "responseFormat": fmt,
                "requestMode": req_mode,
                "authorEvidence": result.get("authorEvidence", []) if isinstance(result, dict) else [],
                "jevPlan": result.get("jevPlan") if isinstance(result, dict) else None,
                "childExitCode": result.get("childExitCode") if isinstance(result, dict) else None,
                "failure": failure,
                "started_unix": started,
                "finished_unix": finished,
            }
            if job.get("program") == fleet_repo_patch.PROGRAM:
                receipt["artifact"] = result.get("artifact") if isinstance(result, dict) else None
                if isinstance(result, dict) and "git_stderr" in result:
                    receipt["git_stderr"] = result["git_stderr"]
            write_receipt(self.receipt_dir, receipt)
            self.busy = False
            self._lock.release()
        return receipt


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    worker = None
    token = b""

    def log_message(self, fmt, *args):
        pass  # never log paths, ids or any request metadata that could carry a token

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return authorized(self.headers.get("Authorization"), self.token)

    def _reject(self, code, payload):
        """Refuse a POST before reading its body. Drain a bounded amount of the declared body first, so
        Windows does not reset the connection (unread data on close sends RST and the client loses the
        response), then close the connection instead of reusing it."""
        self.close_connection = True
        try:
            declared = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            declared = 0
        remaining = min(max(declared, 0), BODY_LIMIT)
        try:
            self.connection.settimeout(2)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass  # the client went away; nothing more to drain
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {"error": "UNAUTHORIZED"})
        if self.path != "/v1/ready":
            return self._send(404, {"error": "NOT_FOUND"})
        self._send(200, self.worker.ready())

    def do_POST(self):
        if not self._authorized():
            return self._reject(401, {"error": "UNAUTHORIZED"})
        if self.path != "/v1/jobs":
            return self._reject(404, {"error": "NOT_FOUND"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            return self._reject(400, {"error": "LENGTH_INVALID"})
        if length <= 0 or length > BODY_LIMIT:
            return self._reject(413, {"error": "BODY_TOO_LARGE"})
        try:
            job = receipt_check.parse_receipt(self.rfile.read(length))
        except (ValueError, RecursionError):
            return self._send(400, {"error": "BODY_INVALID"})
        try:
            self.worker.validate_job(job)
        except Refused as exc:
            return self._send(400, {"error": str(exc)})
        try:
            receipt = self.worker.run_job(job)
        except Busy:
            return self._send(409, {"error": "BUSY"})
        except Refused as exc:
            return self._send(400, {"error": str(exc)})
        self._send(200, receipt)


def create_server(worker, token, bind="127.0.0.1", port=0, allow_lan=False):
    validate_bind(bind, allow_lan)
    handler = type("BoundWorkerHandler", (Handler,), {"worker": worker, "token": token})
    return ThreadingHTTPServer((bind, port), handler)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--bind", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--allow-lan", action="store_true")
    p.add_argument("--token-file", required=True)
    p.add_argument("--model-endpoint", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--receipt-dir", default=str(pilot.PILOT / "worker-receipts"))
    p.add_argument("--report-store", default=None)
    p.add_argument(
        "--model-timeout-s",
        type=int,
        default=None,
        help="Per-lane model-call timeout (default 30s; 400s for monterey-mac-pro; max 900)",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Per-lane output-token cap (program default 64/256; lane default from fleet-workers.json)",
    )
    p.add_argument(
        "--disable-thinking",
        action="store_true",
        help="Ask llama-server to skip reasoning per request (chat_template_kwargs/reasoning_format)",
    )
    p.add_argument(
        "--response-format",
        choices=pilot.RESPONSE_FORMATS,
        default=None,
        help="Opt-in structured output: json-schema sends a strict schema (grammar-constrained, falls back to "
        "json_object if the server rejects it); lane default from fleet-workers.json",
    )
    p.add_argument(
        "--request-mode",
        choices=pilot.REQUEST_MODES,
        default=None,
        help="chat (default) or completion-grammar: /apply-template + lane prefill + grammar-constrained "
        "/completion on the same 127.0.0.1 port; lane default from fleet-workers.json",
    )
    args = p.parse_args(argv)
    try:
        validate_bind(args.bind, args.allow_lan)
        token = load_token(args.token_file)
        worker = Worker(
            args.worker_id,
            args.model,
            args.model_endpoint,
            args.receipt_dir,
            args.report_store,
            model_timeout_s=args.model_timeout_s,
            disable_thinking=args.disable_thinking,
            max_tokens=args.max_tokens,
            response_format=args.response_format,
            request_mode=args.request_mode,
        )
        server = create_server(worker, token, args.bind, args.port, args.allow_lan)
    except (Refused, OSError, ValueError) as exc:
        pilot.emit(json.dumps({"outcome": "BLOCKED", "reason": str(exc)}))
        return 2
    pilot.emit(json.dumps({"worker_id": args.worker_id, "bind": args.bind, "port": server.server_address[1]}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
