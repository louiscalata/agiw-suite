"""Token-authenticated, loopback-first Jev job worker. Single slot, zero retries.

No hosted routes, no model loads, no credential discovery and no token logging.
Executes one routed Nisi job at a time through the isolated local pilot.
"""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import fleet_local_pilot as pilot
from . import windows_worker_receipt_check as receipt_check

PROGRAMS = pilot.PROGRAMS
BODY_LIMIT = 16 * 1024


class Refused(RuntimeError):
    pass


class Busy(Refused):
    pass


def validate_bind(bind, allow_lan=False):
    if not isinstance(bind, str) or not bind:
        raise Refused('BIND_REFUSED')
    loopback = bind in ('::1', 'localhost') or bind.startswith('127.')
    if not loopback and not allow_lan:
        raise Refused('NON_LOOPBACK_BIND_REQUIRES_ALLOW_LAN')
    return bind


def load_token(path):
    """A shared token is a regular, non-symlink 0600 file of at least 32 bytes."""
    p = Path(path)
    info = os.lstat(str(p))
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise Refused('TOKEN_FILE_UNSAFE')
    if os.name == 'posix' and stat.S_IMODE(info.st_mode) != 0o600:
        raise Refused('TOKEN_MODE_UNSAFE')
    raw = p.read_bytes()
    if len(raw) > 4096:
        raise Refused('TOKEN_TOO_LARGE')
    token = raw.strip()
    if len(token) < 32:
        raise Refused('TOKEN_TOO_SHORT')
    return token


def authorized(header, token):
    if not isinstance(header, str) or not header.startswith('Bearer '):
        return False
    return hmac.compare_digest(header[7:].encode('utf-8'), token)


def _safe_name(value):
    return re.sub('[^A-Za-z0-9._-]', '_', value)[:80] or 'job'


def write_receipt(directory, receipt):
    """Atomically publish a 0600 receipt. No token or private payload is included."""
    target_dir = Path(directory)
    target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.receipt-', dir=str(target_dir))
    try:
        final = str(target_dir / ('%s-%d.json' % (
            _safe_name(str(receipt.get('job_id', 'job'))), time.time_ns())))
        receipt['receiptPath'] = final
        with os.fdopen(fd, 'w') as handle:
            json.dump(receipt, handle, sort_keys=True)
            handle.write('\n')
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

    def __init__(self, worker_id, model, model_endpoint, receipt_dir, report_store=None,
                 execute=None, probe=None, model_timeout_s=None, disable_thinking=False, max_tokens=None):
        if not isinstance(worker_id, str) or not worker_id:
            raise Refused('WORKER_ID_REQUIRED')
        try:
            pilot.check_worker_id(worker_id)  # only ids the router config knows
        except pilot.Refused as exc:
            raise Refused(str(exc))
        self.worker_id = worker_id
        self.model = model
        self.model_endpoint = model_endpoint
        pilot.endpoint(model_endpoint)
        self.receipt_dir = Path(receipt_dir)
        try:
            self.model_timeout_s = pilot.model_timeout_s(worker_id, model_timeout_s)
        except pilot.Refused as exc:
            raise Refused(str(exc))
        self.disable_thinking = disable_thinking is True
        try:
            self.max_tokens = pilot.max_tokens(worker_id, max_tokens)
        except pilot.Refused as exc:
            raise Refused(str(exc))
        self.report_store = report_store
        self._execute = execute or pilot.execute
        self._probe = probe or (lambda: pilot.detect_models(pilot.endpoint(model_endpoint)))
        self._lock = threading.Lock()
        self.busy = False

    def ready(self):
        try:
            models, source = self._probe()
            if not isinstance(models, list):
                models, source = [], None
        except Exception:
            models, source = [], None
        return {
            'worker_id': self.worker_id,
            'model_ready': self.model in models,
            'models': models,
            'busy': self.busy,
            'observed_at': time.time(),
        }

    def validate_job(self, job):
        if not isinstance(job, dict):
            raise Refused('BODY_INVALID')
        job_id = job.get('job_id')
        if not isinstance(job_id, str) or not job_id or len(job_id) > 200:
            raise Refused('JOB_ID_REQUIRED')
        route = job.get('route')
        if not isinstance(route, dict) or route.get('worker') != self.worker_id:
            raise Refused('ROUTE_REFUSED')
        if job.get('program') not in PROGRAMS:
            raise Refused('PROGRAM_REFUSED')
        if job.get('model_timeout_s') is not None:
            try:
                pilot.model_timeout_s(self.worker_id, job['model_timeout_s'])
            except pilot.Refused as exc:
                raise Refused(str(exc))
        if job.get('max_tokens') is not None:
            try:
                pilot.max_tokens(self.worker_id, job['max_tokens'])
            except pilot.Refused as exc:
                raise Refused(str(exc))
        if job.get('disable_thinking') not in (None, True, False):
            raise Refused('DISABLE_THINKING_INVALID')
        return job_id

    def run_job(self, job):
        job_id = self.validate_job(job)
        if not self._lock.acquire(False):
            raise Busy('BUSY')
        self.busy = True
        started = time.time()
        outcome = 'BLOCKED'
        timeout_s = pilot.model_timeout_s(self.worker_id, job.get('model_timeout_s', self.model_timeout_s))
        disable_thinking = job['disable_thinking'] if job.get('disable_thinking') in (True, False) else self.disable_thinking
        tokens = job.get('max_tokens') if job.get('max_tokens') is not None else self.max_tokens
        failure = None
        report = {}
        result = None
        try:
            result = self._execute(
                'live', self.model_endpoint, self.model, 'agent', job['program'],
                report_store=self.report_store, worker_id=self.worker_id,
                timeout_s=timeout_s, disable_thinking=disable_thinking, max_output_tokens=tokens,
            )
            report = result.get('report') if isinstance(result, dict) and isinstance(result.get('report'), dict) else {}
            outcome = report.get('outcome')
            if outcome not in receipt_check.OUTCOMES:
                outcome = 'BLOCKED'
            if report.get('reportStored') is not True or result.get('reportStored') is False:
                outcome = 'BLOCKED'
        except Exception as exc:
            outcome = 'BLOCKED'  # a failure is never COMPLETED; no retries
            failure = pilot.sanitize_text('%s: %s' % (type(exc).__name__, exc))
        finally:
            finished = time.time()
            receipt = {
                'kind': receipt_check.JOB_KIND,
                'schema_version': 1,
                'job_id': job_id,
                'worker_id': self.worker_id,
                'model': self.model,
                'program': job.get('program'),
                'outcome': outcome,
                'reportStored': report.get('reportStored') is True,
                'runId': report.get('runId'),
                'reportPath': result.get('reportPath') if isinstance(result, dict) else None,
                'reportSha256': report.get('storedReportSha256'),
                'code': pilot.sanitize_text(report.get('code')) if isinstance(report.get('code'), str) else None,
                'modelTimeoutS': timeout_s,
                'disableThinking': disable_thinking,
                'maxTokens': tokens,
                'authorEvidence': result.get('authorEvidence', []) if isinstance(result, dict) else [],
                'childExitCode': result.get('childExitCode') if isinstance(result, dict) else None,
                'failure': failure,
                'started_unix': started,
                'finished_unix': finished,
            }
            write_receipt(self.receipt_dir, receipt)
            self.busy = False
            self._lock.release()
        return receipt


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    worker = None
    token = b''

    def log_message(self, fmt, *args):
        pass  # never log paths, ids or any request metadata that could carry a token

    def _send(self, code, payload):
        body = json.dumps(payload).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return authorized(self.headers.get('Authorization'), self.token)

    def do_GET(self):
        if not self._authorized():
            return self._send(401, {'error': 'UNAUTHORIZED'})
        if self.path != '/v1/ready':
            return self._send(404, {'error': 'NOT_FOUND'})
        self._send(200, self.worker.ready())

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {'error': 'UNAUTHORIZED'})
        if self.path != '/v1/jobs':
            return self._send(404, {'error': 'NOT_FOUND'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except (TypeError, ValueError):
            return self._send(400, {'error': 'LENGTH_INVALID'})
        if length <= 0 or length > BODY_LIMIT:
            return self._send(413, {'error': 'BODY_TOO_LARGE'})
        try:
            job = json.loads(self.rfile.read(length))
        except ValueError:
            return self._send(400, {'error': 'BODY_INVALID'})
        try:
            self.worker.validate_job(job)
        except Refused as exc:
            return self._send(400, {'error': str(exc)})
        try:
            receipt = self.worker.run_job(job)
        except Busy:
            return self._send(409, {'error': 'BUSY'})
        except Refused as exc:
            return self._send(400, {'error': str(exc)})
        self._send(200, receipt)


def create_server(worker, token, bind='127.0.0.1', port=0, allow_lan=False):
    validate_bind(bind, allow_lan)
    handler = type('BoundWorkerHandler', (Handler,), {'worker': worker, 'token': token})
    return ThreadingHTTPServer((bind, port), handler)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker-id', required=True)
    p.add_argument('--bind', default='127.0.0.1')
    p.add_argument('--port', type=int, default=0)
    p.add_argument('--allow-lan', action='store_true')
    p.add_argument('--token-file', required=True)
    p.add_argument('--model-endpoint', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--receipt-dir', default=str(pilot.PILOT / 'worker-receipts'))
    p.add_argument('--report-store', default=None)
    p.add_argument('--model-timeout-s', type=int, default=None,
                   help='Per-lane model-call timeout (default 30s; 400s for monterey-mac-pro; max 900)')
    p.add_argument('--max-tokens', type=int, default=None,
                   help='Per-lane output-token cap (program default 64/256; lane default from fleet-workers.json)')
    p.add_argument('--disable-thinking', action='store_true',
                   help='Ask llama-server to skip reasoning per request (chat_template_kwargs/reasoning_format)')
    args = p.parse_args(argv)
    try:
        validate_bind(args.bind, args.allow_lan)
        token = load_token(args.token_file)
        worker = Worker(args.worker_id, args.model, args.model_endpoint,
                        args.receipt_dir, args.report_store,
                        model_timeout_s=args.model_timeout_s, disable_thinking=args.disable_thinking,
                        max_tokens=args.max_tokens)
        server = create_server(worker, token, args.bind, args.port, args.allow_lan)
    except (Refused, OSError, ValueError) as exc:
        print(json.dumps({'outcome': 'BLOCKED', 'reason': str(exc)}))
        return 2
    print(json.dumps({'worker_id': args.worker_id, 'bind': args.bind,
                      'port': server.server_address[1]}))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
