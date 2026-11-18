"""Local-only Jev router client: probe forwarded workers, plan, dispatch one job.

Hosted routes are never used. Every worker keeps its bearer token over the
tunnel; readiness and dispatch are both authenticated and bounded.
"""
import argparse
import json
import time
from pathlib import Path
import urllib.request
from urllib.parse import urlsplit

from . import fleet_routing_plan as routing
from . import windows_worker as worker_module
from . import windows_worker_receipt_check as receipts
from . import fleet_local_pilot as pilot

class Refused(RuntimeError): pass
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): raise Refused('REDIRECT_REFUSED')

TIMEOUT = 3
# A dispatched job may run up to the pilot's 25s child wall timeout plus store/receipt time.
JOB_TIMEOUT = 45


def worker_url(value, allow_lan=False):
    if not isinstance(value, str) or not value:
        raise Refused('URL_REQUIRED')
    parts = urlsplit(value)
    if (parts.scheme != 'http' or parts.username or parts.password
            or parts.query or parts.fragment or parts.path not in ('', '/')
            or not parts.port):
        raise Refused('URL_REFUSED')
    if parts.hostname != '127.0.0.1' and not allow_lan:
        raise Refused('NON_LOOPBACK_URL_REQUIRES_ALLOW_LAN')
    return 'http://127.0.0.1:%d' % parts.port if parts.hostname == '127.0.0.1' else value.rstrip('/')


def job_timeout(name, value=None):
    """HTTP wait for one dispatched job: the lane's model timeout plus margin, unless configured."""
    if value is None:
        lane = pilot.lane_defaults(name)
        if 'job_timeout_s' in lane:
            value = lane['job_timeout_s']
        elif 'model_timeout_s' in lane:
            value = lane['model_timeout_s'] + 60
        else:
            return 90
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 1200:
        raise Refused('JOB_TIMEOUT_INVALID')
    return value


def load_workers(path):
    """Read and validate the per-worker config. Tokens stay out of returned logs."""
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict): raise Refused('CONFIG_INVALID')
    rows = config.get('workers')
    if not isinstance(rows, dict) or not rows: raise Refused('WORKERS_REQUIRED')
    workers = {}
    for name, row in rows.items():
        if not isinstance(name, str) or not name or not isinstance(row, dict):
            raise Refused('WORKER_CONFIG_INVALID')
        if name not in routing.WORKERS:
            raise Refused('UNKNOWN_WORKER_ID')
        token_file = row.get('token_file')
        if not isinstance(token_file, str) or not token_file:
            raise Refused('TOKEN_REQUIRED')
        token = worker_module.load_token(token_file)
        workers[name] = {
            'url': worker_url(row.get('url'), row.get('allow_lan') is True),
            'token': token,
            'model': row.get('model'),
            'job_timeout_s': job_timeout(name, row.get('job_timeout_s')),
            'model_timeout_s': row.get('model_timeout_s'),
            'disable_thinking': row.get('disable_thinking') is True,
            'max_tokens': row.get('max_tokens'),
        }
    return workers, config


class FleetRouterClient:
    def __init__(self, workers, opener=None, now_fn=None):
        self.workers = workers
        self.opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())
        self.now_fn = now_fn or time.time

    def request(self, name, method, path, body=None):
        row = self.workers[name]
        data = json.dumps(body).encode('utf-8') if body is not None else None
        headers = {'Authorization': 'Bearer ' + row['token'].decode('utf-8')}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(row['url'] + path, data=data, headers=headers, method=method)
        timeout = row.get('job_timeout_s', JOB_TIMEOUT) if method == 'POST' else TIMEOUT
        with self.opener.open(req, timeout=timeout) as response:
            return response.status, json.loads(response.read(65537))

    def probe(self):
        now = self.now_fn()
        workers = {}
        for name in self.workers:
            try:
                status, ready = self.request(name, 'GET', '/v1/ready')
            except Exception:
                continue  # unreachable, 401, redirect or malformed: unknown, not eligible
            if status != 200 or not isinstance(ready, dict):
                continue
            if ready.get('worker_id') != name:
                continue
            busy = ready.get('busy') is True
            workers[name] = routing.WorkerEvidence(
                observed_at=now,
                local_model_ready=ready.get('model_ready') is True,
                authenticated=True,
                lease_available=not busy,
                busy=busy,
            )
        return workers, now

    def run(self, job_id, program='nisi-smoke.mjs', workload='routine', payload=None):
        if program not in worker_module.PROGRAMS: raise Refused('PROGRAM_REFUSED')
        evidence, now = self.probe()
        decision = routing.plan('local', workload, evidence, routing.HostedEvidence(),
                                now=now, source='agent')
        if decision.worker is None:
            return {'outcome': 'BLOCKED', 'reason': decision.reason,
                    'worker': None, 'job_id': job_id}
        name = decision.worker
        body = {'job_id': job_id,
                'route': {'worker': name, 'preanalysis': decision.preanalysis,
                          'reason': decision.reason},
                'program': program}
        if payload is not None:
            body['payload'] = payload
        row = self.workers[name]
        if row.get('model_timeout_s') is not None:
            body['model_timeout_s'] = row['model_timeout_s']
        if row.get('max_tokens') is not None:
            body['max_tokens'] = row['max_tokens']
        if row.get('disable_thinking') is True:
            body['disable_thinking'] = True
        try:
            status, receipt = self.request(name, 'POST', '/v1/jobs', body)
        except Exception:
            return {'outcome': 'BLOCKED', 'reason': 'DISPATCH_FAILED',
                    'worker': name, 'job_id': job_id}
        if status != 200:
            return {'outcome': 'BLOCKED', 'reason': 'DISPATCH_REFUSED',
                    'worker': name, 'job_id': job_id}
        verdict = receipts.validate_job_receipt(
            receipt, expected_worker=name, expected_model=self.workers[name].get('model'),
            job_id=job_id, now=self.now_fn())
        if not verdict['valid'] or receipt.get('reportStored') is not True:
            return {'outcome': 'BLOCKED', 'reason': 'RECEIPT_INVALID:' + str(verdict['reason']),
                    'worker': name, 'job_id': job_id}
        return {'outcome': receipt['outcome'], 'worker': name, 'job_id': job_id,
                'receipt_path': receipt.get('receiptPath'),
                'report_path': verdict['report_path'], 'reportStored': True}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--program', default='nisi-smoke.mjs')
    p.add_argument('--job-id', default=None)
    p.add_argument('--workload', choices=['routine', 'gpu-heavy'], default='routine')
    a = p.parse_args(argv)
    job_id = a.job_id or ('agiw-route-%d' % time.time_ns())
    try:
        workers, config = load_workers(a.config)
        result = FleetRouterClient(workers).run(
            job_id, a.program, workload=config.get('workload', a.workload))
    except (Refused, OSError, ValueError, KeyError, TypeError) as exc:
        result = {'outcome': 'BLOCKED', 'reason': str(exc), 'job_id': job_id}
    print(json.dumps(result, sort_keys=True))
    return 0 if result['outcome'] != 'BLOCKED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
