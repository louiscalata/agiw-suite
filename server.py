#!/usr/bin/env python3
"""Loopback monitor with a passive sampler and explicit local model controls."""
from __future__ import annotations
import argparse
import copy
import datetime as dt
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from telemetry import (collect_snapshot, cancel_windows_worker_probe,
                       stop_windows_worker_probe)
from activity import collect_activity
from client_models import collect_clients
from model_control import ControlError, ModelControl, capability as model_control_capability
from auto_unload import AutoUnloader, router_problem
from online_code_repair import OnlineCodeRepair, read_only_diagnosis, FIX_SCOPES, HEADLESS_COMMANDS
from live_feed import LiveFeed
from gpu_probe import mac_gpu
from local_callers import LocalCallers
from mem_guard import Guard, LevelFile, unknown_block
from bundled_components import BundledComponents
from jev_connection import JevConnection
from machine_state import FRESH_SECONDS, expire_machines, finite_number, platform_name

ROOT = Path(__file__).resolve().parent / 'web'
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/components': ('components.html', 'text/html; charset=utf-8'),
          '/components.js': ('components.js', 'text/javascript; charset=utf-8'),
          '/components.css': ('components.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/online-code-mode.mjs': ('online-code-mode.mjs', 'text/javascript; charset=utf-8'),
          '/map-layout.mjs': ('map-layout.mjs', 'text/javascript; charset=utf-8'),
          '/model-control-view.mjs': ('model-control-view.mjs', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}
ACTIVE = {'generating', 'busy'}
# The fast path may overlay a full sample only while that sample is this recent (seconds).
FULL_SAMPLE_MAX_AGE = 3.0
KNOWN_ACTIVITY = ACTIVE | {'idle'}
ROUTER_OWNER_LOCK = Path.home() / '.local/state/codemode-router/owner.lock'
MODEL_JOURNAL_PATH = Path.home() / '.local/state/inference-monitor/model-operation.json'
# The HTTP dispatcher and its gate tests consume the same complete owner-action table.
# Receipt-writing Entry is not exposed over HTTP; readiness GET uses cached evidence.
# Each choice is (request value, diagnosis action, controller method arguments).
OWNER_ACTION_ROUTES = {
    '/api/online-code-mode/repair': ('action', 'request', (
        ('check-and-repair', 'check-and-repair', ()),)),
    '/api/inference/fix': ('scope', 'request_fix', tuple(
        (scope, f'fix-{scope}', (scope,)) for scope in FIX_SCOPES)),
    '/api/online-code-mode/headless': ('action', 'request_headless', tuple(
        (action, f'headless-{action}', (action,)) for action in HEADLESS_COMMANDS)),
}


def cached_readiness(snapshot, now):
    """Project already sampled observations; never invoke an owner or renew a receipt."""
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    # Fast model overlays advance sampledAt without re-reading setup evidence.
    sampled = snapshot.get('fullSampledAt', snapshot.get('sampledAt'))
    age = now - sampled if finite_number(sampled) else None
    fresh = finite_number(age) and 0 <= age <= FULL_SAMPLE_MAX_AGE
    mode = snapshot.get('onlineCodeMode', {})
    setup = mode.get('setupState') if isinstance(mode, dict) else None
    setup = setup if fresh and setup in {'fresh', 'expired', 'absent', 'invalid', 'unknown'} else 'unknown'
    return {'action': 'readiness-status', 'readOnly': True,
            'status': 'observed' if fresh else 'unknown',
            'reasonCode': ('cached_observations' if fresh else 'not_observed' if age is None
                           else 'clock_skew' if age < 0 else 'stale'),
            'sampledAt': sampled, 'ageSeconds': age, 'setupState': setup,
            'inferenceReadiness': 'unknown',
            'machines': read_only_diagnosis(None, snapshot)['machines'],
            'message': 'Cached status only. No readiness command or model inference was run; no receipt was written.'}


def mac_online_code_controls_enabled():
    """Only an ordinary, same-user macOS process may expose owner actions."""
    return (sys.platform == 'darwin' and os.getuid() != 0
            and os.getuid() == os.geteuid())


def inventory_is_known(rows, source_status):
    """A live inventory supplies a count only when every Mac row has known residency."""
    return (any(source_status.get(key) == 'live' for key in ('lms-ps', 'lmstudio-api'))
            and all(type(row.get('loaded')) is bool and row.get('inventoryComplete') is not False
                    and observation_is_current(row.get('ageSeconds')) for row in rows
                    if row.get('host', 'mac') == 'mac' and not row.get('representedByInstances')))


def observation_is_current(age, maximum=3):
    return type(age) in (int, float) and 0 <= age <= maximum and math.isfinite(age)


def host_liveness(machines):
    """Thresholds are keyed by host identity, so a third platform is never treated as this Mac."""
    table = {host: {"freshForSeconds": seconds} for host, seconds in FRESH_SECONDS.items()}
    seen = set()
    for machine in machines:
        platform = platform_name(machine.get('platform'))
        host = machine.get('host')
        if platform and isinstance(host, str):
            entry = {key: machine.get(key) for key in ('state', 'reasonCode', 'ageSeconds')}
            entry['freshForSeconds'] = FRESH_SECONDS[platform]
            table[f'{platform}:{host}'] = entry
            table[host] = (entry if host not in seen else
                           {'state': 'unknown', 'reasonCode': 'identity_mismatch', 'freshForSeconds': None})
            seen.add(host)
    return table


def row_is_live(row, liveness):
    key = f"{row['platform']}:{row.get('host')}" if row.get('platform') else row.get('host')
    entry = liveness.get(key, {})
    age, limit = row.get('ageSeconds'), entry.get('freshForSeconds')
    return (finite_number(age) and finite_number(limit) and 0 <= age <= limit
            and entry.get('state', 'up') == 'up')


def activity_is_known(rows, source_status, liveness=None):
    """A responding command is insufficient when loaded rows lack exact activity."""
    if source_status.get('lms-ps') != 'live' or not inventory_is_known(rows, source_status):
        return False
    if any(type(row.get('loaded')) is not bool for row in rows if not row.get('representedByInstances')):
        return False
    liveness = host_liveness([]) if liveness is None else liveness
    loaded = [row for row in rows if row.get('loaded') is True and not row.get('representedByInstances')]
    for row in loaded:
        source_known = (row.get('source') == 'lms-ps' if row.get('host') == 'mac'
                        else liveness.get(row.get('host'), {}).get('state') == 'up')
        if not source_known or row.get('state') not in KNOWN_ACTIVITY or not row_is_live(row, liveness):
            return False
    return True


def source_current(source):
    if not isinstance(source, dict) or source.get('state') not in ('live', 'recorded'):
        return False
    # Router/checkpoint and dispatcher event ages are history, not the clock of
    # the lock check or journal read. Legacy envelopes retain their old contract.
    age = source.get('observationAgeSeconds', source.get('ageSeconds', 0))
    max_age = 60 if source.get('id') == 'windows-worker' else 30
    return (type(age) in (int, float) and 0 <= age <= max_age
            and source.get('complete') is not False and source.get('truncated') is not True)


# peer_feed names each host windows:<host>-telemetry. The older single-PC reader
# still publishes windows-telemetry. Either fresh source keeps the PC report.
_WINDOWS_PEER_TELEMETRY = re.compile(r'windows:[A-Za-z0-9][A-Za-z0-9._-]{0,62}-telemetry\Z')


def windows_telemetry_current(sources):
    """True when a fresh Windows telemetry source backs windowsLive."""
    if not isinstance(sources, dict):
        return False
    if source_current(sources.get('windows-telemetry')):
        return True
    for source_id, source in sources.items():
        if (isinstance(source_id, str) and _WINDOWS_PEER_TELEMETRY.fullmatch(source_id)
                and isinstance(source, dict) and source.get('platform') == 'windows'
                and source_current(source)):
            return True
    return False


def enforce_snapshot_evidence(data):
    data = copy.deepcopy(data)
    sources = {s['id']: s for s in data.get('sources', []) if isinstance(s, dict) and 'id' in s}
    known = lambda sid: source_current(sources.get(sid))
    # lms ps is both a complete residency list and exact activity. The API can
    # independently preserve residency, but never activity.
    for row in data.get('models', []):
        age = row.get('ageSeconds')
        if row.get('host') == 'mac' and (type(row.get('loaded')) is not bool or type(age) not in (int, float) or not 0 <= age <= 3):
            row.update(loaded=None, state='unknown', queued=None)
    inventory = known('lms-ps') or known('lmstudio-api')
    if not inventory:
        for row in data.get('models', []):
            if row.get('host') == 'mac':
                row.update(loaded=None, state='unknown', queued=None)
    if not known('lms-ps'):
        for row in data.get('models', []):
            if row.get('host') == 'mac':
                row.update(state='loaded' if row.get('loaded') is True else 'unloaded' if row.get('loaded') is False else 'unknown', queued=None)
    if 'pipeline' in data and not known('pipeline-router'):
        data['pipeline'] = {'status': 'unknown', 'runId': None, 'pipelines': [],
                            'reason': 'Route observation unavailable or incomplete'}
    if 'macGpu' in data and not known('mac-gpu'):
        data['macGpu'] = None
    if 'memory' in data and not known('mem-guard'):
        data['memory'] = unknown_block()
    if 'windowsLive' in data and not windows_telemetry_current(sources):
        data['windowsLive'] = None
    if 'windowsWorker' in data and not known('windows-worker'):
        data['windowsWorker'] = {'state': 'unknown', 'ageSeconds': None,
                                 'detail': 'Worker observation unavailable or incomplete'}
    if not known('local-callers'):
        data['localCallers'] = None
    if 'windowsJobs' in data and not known('windows-jobs'):
        jobs = data.get('windowsJobs')
        if isinstance(jobs, dict):
            jobs.update(availability={'state': 'unknown', 'reason': 'Job source unavailable or incomplete'}, liveAvailable=False)
    for source in data.get('sources', []):
        if source.get('state') == 'live' and not source_current(source):
            source['state'] = 'unavailable'
    return data


class SnapshotStore:
    def __init__(self):
        self.lock = threading.Lock()
        # Stream readers wait on this; every publish wakes them (see wait_for_change).
        self.changed = threading.Condition(self.lock)
        self.snapshot = None
        self.history = []
        self.events = []
        self.previous = {}
        self.sequence = 0

    def publish(self, data, *, partial=False):
        # Whole snapshots replace atomically; clients never see partial updates.
        # A partial publish is a fast-path overlay: same events logic, no new history point.
        data = enforce_snapshot_evidence(data)
        now = data['sampledAt']
        if isinstance(data.get('machines'), list):
            for machine in data['machines']:
                age = machine.get('ageSeconds')
                if machine.get('observedAt') is None and finite_number(age):
                    try:
                        machine['observedAt'] = dt.datetime.fromtimestamp(now - age, dt.timezone.utc).isoformat()
                    except (ValueError, OverflowError, OSError):
                        machine['observedAt'] = ''  # invalid evidence expires below
            expire_machines(data['machines'], now)
        liveness = host_liveness(data.get('machines', []))
        data['liveness'] = liveness
        rows = data.get('models', [])
        for source in data.get('sources', []):
            if (source.get('state') == 'live' and 'ageSeconds' in source
                    and not observation_is_current(source['ageSeconds'], 3)
                    and source.get('id') in ('lms-ps', 'lmstudio-api', 'local-callers')):
                source.update(state='unavailable', detail='Current observation unknown: invalid or expired age')
        source_status = {s['id']: s.get('state') if s.get('modelsComplete', True) is True else 'incomplete'
                         for s in data.get('sources', [])}
        for row in rows:
            if not row_is_live(row, host_liveness(data.get('machines', []))):
                row.update(loaded=None, state='unknown', queued=None)
                if isinstance(row.get('metadata'), dict):
                    for key in ('loadedInstances', 'loadedInstanceCount'):
                        row['metadata'][key] = None
                row['loadedInstanceIds'] = None
        if source_status.get('local-callers') != 'live':
            data['localCallers'] = None
        pipeline = data.get('pipeline')
        router_source = next((s for s in data.get('sources', []) if s.get('id') == 'pipeline-router'), {})
        legacy_idle_expired = (isinstance(pipeline, dict) and pipeline.get('status') == 'idle'
                               and pipeline.get('ageSeconds') is not None
                               and 'observationAgeSeconds' not in router_source
                               and not observation_is_current(pipeline.get('ageSeconds')))
        if isinstance(pipeline, dict) and (pipeline.get('complete') is False
                or pipeline.get('pipelinesComplete') is False or pipeline.get('runsTruncated') is True
                or legacy_idle_expired):
            # Bounds qualify totals, not a primary that this scan verified live.
            # Idle, in contrast, needs complete coverage. A successful empty scan
            # has no checkpoint, so its checkpoint age may legitimately be null.
            positive = pipeline.get('status') in ('running', 'queued') and bool(pipeline.get('runId'))
            pipeline['pipelinesComplete'] = False
            if not positive:
                pipeline.update(status='unknown', detail='Current route inventory unavailable or incomplete')
            if isinstance(data.get('onlineCodeMode'), dict):
                data['onlineCodeMode']['runCountsComplete'] = False
                if not positive:
                    data['onlineCodeMode'].update(state='unknown', active=None, taskState='unknown',
                                                   evidence='Current route inventory unavailable or incomplete')
        activity_source_live = source_status.get('lms-ps') == 'live'
        states = {f"{r['host']}:{r['id']}": {'state': r['state'], 'source': r.get('source'),
                                               'activitySourceLive': activity_source_live}
                  for r in rows}
        live = [r for r in rows if row_is_live(r, liveness) and not r.get('representedByInstances')]
        activity_known = activity_is_known(rows, source_status, liveness)
        inventory_known = ((activity_known or source_status.get('lmstudio-api') == 'live')
                           and inventory_is_known(rows, source_status))
        local_machine = next((machine for machine in data.get('machines', [])
                              if machine.get('host') == 'mac' and machine.get('platform') == 'mac'), None)
        loaded_count = sum(r.get('loaded') is True for r in live)
        if local_machine is not None:
            # local_summary correlates every API model with its CLI instances.
            # An inventory row is not a second unknown/busy model beside its aliases.
            activity_known = (activity_is_known([r for r in rows if r.get('host') != 'mac'],
                                                source_status, liveness)
                              and local_machine.get('state') == 'up'
                              and local_machine.get('modelsComplete') is True
                              and local_machine.get('busyCount') is not None)
            inventory_known = (inventory_known and local_machine.get('modelsComplete') is True
                               and local_machine.get('loadedCount') is not None)
            if inventory_known:
                # The summary correlates API models and CLI aliases; row counts do not.
                loaded_count = local_machine['loadedCount'] + sum(
                    r.get('loaded') is True for r in live if r.get('host') != 'mac')
        active_count = (local_machine['busyCount'] + sum(r['state'] in ACTIVE for r in live
                                                       if r.get('host') != 'mac')
                        if activity_known and local_machine is not None else
                        sum(r['state'] in ACTIVE for r in live) if activity_known else None)
        data['activityKnown'] = activity_known
        if not inventory_known:
            for component in data.get('components', []):
                if component.get('id') == 'nisi' and component.get('state') in ('ready', 'partial'):
                    component.update(state='unknown', detail='Current model inventory is incomplete; route readiness unknown',
                                     authorModel=None, reviewerModel=None)
        with self.lock:
            self.sequence += 1
            for row in rows:
                key = f"{row['host']}:{row['id']}"
                before = self.previous.get(key)
                if before is None:
                    continue
                prior_state = before['state']
                prior_source = before.get('source')
                prior_source_live = before.get('activitySourceLive') is True
                current_state = row['state']
                current_source = row.get('source')
                prior_activity = (prior_source_live and prior_source == 'lms-ps'
                                  and prior_state in KNOWN_ACTIVITY)
                current_activity = (activity_source_live and current_source == 'lms-ps'
                                    and current_state in KNOWN_ACTIVITY)
                if prior_state == current_state and prior_source == current_source:
                    continue
                if (prior_activity and not current_activity
                        and not (activity_source_live and current_state == 'unloaded')):
                    kind, label = 'evidence-loss', 'Activity visibility lost; inventory only'
                elif (not prior_activity and current_activity
                      and not (prior_state == 'unloaded' and activity_source_live)):
                    kind, label = 'evidence-recovered', 'Runtime activity reporting resumed'
                else:
                    kind, label = 'state-change', 'Observed runtime state changed'
                self.events.insert(0, {'at': now, 'model': row['name'], 'host': row['host'],
                                       'before': prior_state, 'state': current_state,
                                       'kind': kind, 'label': label,
                                       'sourceBefore': prior_source, 'source': current_source})
            self.previous = states
            self.events = self.events[:50]
            if not partial:
                self.history.append({'at': now,
                                     'active': active_count,
                                     'loaded': loaded_count if inventory_known else None})
                self.history = self.history[-90:]
            data.update(sequence=self.sequence, history=copy.deepcopy(self.history),
                        events=copy.deepcopy(self.events), intervalSeconds=1)
            self.snapshot = data
            self.changed.notify_all()

    def read(self):
        with self.lock:
            return self._read_locked()

    def _read_locked(self):
        data = copy.deepcopy(self.snapshot)
        if data is None:
            return None
        now = time.time()
        elapsed = max(0.0, now - data['sampledAt'])
        if isinstance(data.get('machines'), list):
            expire_machines(data['machines'], now)
            data['liveness'] = host_liveness(data['machines'])
        if elapsed > FULL_SAMPLE_MAX_AGE:
            data['activityKnown'] = False
        return data

    def wait_for_change(self, last_sequence, timeout):
        """Wait for a publish; on timeout also age machine evidence when the sampler has stopped."""
        with self.changed:
            if not self.changed.wait_for(lambda: self.snapshot is not None and self.sequence != last_sequence,
                                         timeout=timeout):
                if self.snapshot is None or not self.snapshot.get('machines'):
                    return None, last_sequence
            return self._read_locked(), self.sequence


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8
    max_connections = 8
    socket_timeout_seconds = 1.0
    connection_deadline_seconds = 2.0
    max_streams = 3
    stream_write_timeout_seconds = 5.0

    def __init__(self, address, store, *, durable_model_control=False, l2_open=False):
        if address[0] != '127.0.0.1':
            raise ValueError('Monitor must bind to IPv4 loopback')
        if type(durable_model_control) is not bool:
            raise ValueError('Durable model control flag must be boolean')
        if type(l2_open) is not bool:
            raise ValueError('L2-open flag must be boolean')
        self.store = store
        self.l2_open = l2_open
        self.action_diagnosis = None
        # Explicit opt-in only. ModelControl reads an existing journal and
        # blocks actions on missing/corrupt state; startup never provisions it.
        self.model_control = (ModelControl(store, journal_path=MODEL_JOURNAL_PATH)
                              if durable_model_control else ModelControl(store))
        self.auto_unloader = None
        self.online_code_repair = (OnlineCodeRepair(l2_open=l2_open, snapshot_read=store.read)
                                   if mac_online_code_controls_enabled() else None)
        self.bundled_components = BundledComponents()
        self.jev_connection = JevConnection()
        self._slots = threading.BoundedSemaphore(self.max_connections)
        self._active_connections = 0
        self._active_lock = threading.Lock()
        # Live streams are exempt from the 2 s connection deadline but capped, so
        # ordinary requests always keep most of the connection slots.
        self._streams = threading.BoundedSemaphore(self.max_streams)
        self._deadlines = {}
        self.stopping = threading.Event()
        super().__init__(address, Handler)

    def release_deadline(self, request):
        with self._active_lock:
            timer = self._deadlines.pop(id(request), None)
        if timer is not None:
            timer.cancel()

    def claim_stream(self):
        return self._streams.acquire(blocking=False)

    def release_stream(self):
        self._streams.release()

    def process_request(self, request, client_address):
        # Reject excess accepted sockets before allocating a handler thread.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        with self._active_lock:
            self._active_connections += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:
                self._active_connections -= 1
            self._slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address):
        def expire():
            # A client can drip bytes within each read timeout indefinitely.
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                request.close()
            except OSError:
                pass

        deadline = threading.Timer(self.connection_deadline_seconds, expire)
        deadline.daemon = True
        with self._active_lock:
            self._deadlines[id(request)] = deadline
        try:
            request.settimeout(self.socket_timeout_seconds)
            deadline.start()
            super().process_request_thread(request, client_address)
        finally:
            deadline.cancel()
            with self._active_lock:
                self._deadlines.pop(id(request), None)
                self._active_connections -= 1
            self._slots.release()

    def handle_error(self, *_):
        # Stalled or disconnected local readers are expected.
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def same_origin(self, *, require_origin=False):
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        host_headers = self.headers.get_all('Host', [])
        if len(host_headers) != 1 or host_headers[0] not in hosts:
            return False
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or (require_origin and len(origins) != 1):
            return False
        if origins and origins[0] != f'http://{host_headers[0]}':
            return False
        return True

    def do_GET(self):
        if not self.same_origin():
            return self.reply(403, b'Invalid host or origin', 'text/plain')
        path = urlsplit(self.path).path
        if path == '/api/components':
            return self.reply(200, json.dumps(self.server.bundled_components.status()).encode(), 'application/json')
        if path == '/api/components/jev':
            return self.reply(200, json.dumps(self.server.jev_connection.status()).encode(), 'application/json')
        if path == '/api/stream':
            return self.stream()
        if path == '/api/snapshot':
            data = self.server.store.read()
            if data is None:
                return self.reply(503, b'{"status":"starting"}', 'application/json')
            return self.reply(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
        if path == '/api/online-code-mode/readiness':
            return self.reply(200, json.dumps(cached_readiness(self.server.store.read(), time.time())).encode(),
                              'application/json')
        if path == '/api/models/control':
            return self.reply(200, json.dumps(self.server.model_control.read()).encode(), 'application/json')
        if path == '/api/models/auto-unload':
            if self.server.auto_unloader is None:
                return self.control_error(501, 'Auto-unload runs only in the Monitor app.')
            return self.reply(200, json.dumps(self.server.auto_unloader.state()).encode(), 'application/json')
        if path in OWNER_ACTION_ROUTES or path == '/api/online-code-mode/entry':
            if not mac_online_code_controls_enabled() or self.server.online_code_repair is None:
                return self.control_error(501, 'Online Code Mode controls belong to the Mac owner.')
            return self.reply(200, json.dumps(self.server.action_diagnosis or self.server.online_code_repair.read()).encode(),
                              'application/json')
        asset = ASSETS.get(path)
        if path == '/usage-format.mjs':
            try:
                return self.reply(200, (ROOT.parent / 'usage-format.mjs').read_bytes(), 'text/javascript; charset=utf-8')
            except OSError:
                return self.reply(503, b'Asset unavailable', 'text/plain')
        if asset is None:
            return self.reply(404, b'Not found', 'text/plain')
        try:
            self.reply(200, (ROOT / asset[0]).read_bytes(), asset[1])
        except OSError:
            self.reply(503, b'Asset unavailable', 'text/plain')

    def do_POST(self):
        if self.path not in OWNER_ACTION_ROUTES and self.path not in {'/api/models/control', '/api/models/auto-unload',
                             '/api/components/nisi/check',
                             '/api/components/jev/check'}:
            return self.reply(404, b'{"status":"error","message":"Not found."}', 'application/json')
        if not self.same_origin(require_origin=True):
            return self.control_error(403, 'Exact same-origin Origin and Host are required.')
        if (self.path in OWNER_ACTION_ROUTES
                and (not mac_online_code_controls_enabled()
                     or self.server.online_code_repair is None)):
            return self.control_error(501, 'Online Code Mode controls belong to the Mac owner.')
        if self.path == '/api/models/auto-unload' and self.server.auto_unloader is None:
            return self.control_error(501, 'Auto-unload runs only in the Monitor app.')
        if self.headers.get_all('Transfer-Encoding', []):
            return self.control_error(400, 'Transfer encoding is not accepted.')
        if self.headers.get_all('Content-Type', []) != ['application/json']:
            return self.control_error(415, 'Content-Type must be application/json.')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit():
            return self.control_error(411, 'A valid Content-Length is required.')
        length = int(lengths[0])
        if length < 2 or length > 1024:
            return self.control_error(413, 'Request body must be between 2 and 1024 bytes.')
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                return self.control_error(400, 'Incomplete request body.')
            def distinct(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError('duplicate JSON field')
                    value[key] = item
                return value
            data = json.loads(raw.decode('utf-8'), object_pairs_hook=distinct,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid number')))
            if self.path == '/api/components/jev/check':
                if data != {'action': 'connection-check'}:
                    return self.control_error(400, 'Body must contain only action: connection-check.')
                accepted = self.server.jev_connection.request_check()
            elif self.path == '/api/components/nisi/check':
                if data != {'action': 'self-check'}:
                    return self.control_error(400, 'Body must contain only action: self-check.')
                accepted = self.server.bundled_components.request_check()
            elif self.path in OWNER_ACTION_ROUTES:
                field, method, choices = OWNER_ACTION_ROUTES[self.path]
                choice = next((choice for choice in choices if data == {field: choice[0]}), None)
                if choice is None:
                    return self.control_error(400, f'Body must contain only {field}: ' +
                                              ', '.join(choice[0] for choice in choices) + '.')
                _, action, arguments = choice
                accepted = self.owner_action(action, lambda: getattr(self.server.online_code_repair, method)(*arguments))
            elif self.path == '/api/models/auto-unload':
                if (not isinstance(data, dict) or set(data) != {'enabled'}
                        or type(data['enabled']) is not bool):
                    return self.control_error(400, 'Body must contain only enabled: true or false.')
                try:
                    state = self.server.auto_unloader.set_enabled(data['enabled'])
                except ValueError as error:
                    return self.control_error(409, str(error))
                except OSError:
                    return self.control_error(503, 'Could not save the auto-unload setting.')
                return self.reply(200, json.dumps(state).encode(), 'application/json')
            else:
                if not isinstance(data, dict) or set(data) != {'action', 'modelId'}:
                    return self.control_error(400, 'Body must contain only action and modelId.')
                accepted = self.server.model_control.request(data['action'], data['modelId'])
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            return self.control_error(400, 'Request body must be valid JSON with unique fields.')
        except ControlError as error:
            return self.control_error(error.status, error.message)
        return self.reply(423 if accepted.get('reasonCode') == 'l2_closed' else 202, json.dumps(accepted).encode(), 'application/json')

    def owner_action(self, action, request):
        # L2 protects Repair, Fix and Headless. Local LM Studio model
        # load/unload and auto-unload are normal monitor controls outside this gate.
        if getattr(self.server, 'l2_open', False) is not True:
            store = getattr(self.server, 'store', None)
            diagnosis = read_only_diagnosis(action, store.read() if store is not None else None)
            self.server.action_diagnosis = diagnosis
            return diagnosis
        self.server.action_diagnosis = None
        return request()

    def control_error(self, status, message):
        return self.reply(status, json.dumps({'status': 'error', 'message': message}).encode(),
                          'application/json')

    def reply(self, status, payload, mime):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def stream(self):
        """Stream each publish and age machine evidence during sampler stalls; heartbeat before startup."""
        if not self.server.claim_stream():
            return self.reply(503, b'{"status":"busy","message":"Too many live streams"}', 'application/json')
        try:
            self.server.release_deadline(self.request)
            self.request.settimeout(self.server.stream_write_timeout_seconds)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.close_connection = True
            last = None
            while not self.server.stopping.is_set():
                data, sequence = self.server.store.wait_for_change(last, timeout=1.0)
                if data is None:
                    chunk = b': alive\n\n'
                else:
                    last = sequence
                    chunk = (f'id: {sequence}\nevent: snapshot\ndata: '.encode()
                             + json.dumps(data, allow_nan=False, separators=(',', ':')).encode() + b'\n\n')
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError, ValueError):
            pass
        finally:
            self.server.release_stream()


def memory_guard(**options):
    """The memory safeguard rail (mem_guard.Guard), or None if it cannot be built."""
    try:
        return Guard(**options)
    except Exception:
        return None


def memory_route_context(data):
    """(route_pair, route_live) for the memory guard's suggestions, from this sample's own Nisi
    component and pipeline. route_pair maps the Nisi edit pair's model ids to 'author' and
    'reviewer', only while the component reads 'ready' and names two different models.
    route_live is True while the pipeline reads 'running' or Nisi reads 'in-use'. A malformed
    components or pipeline value reads as no context (None, False); this never raises."""
    pair, live = None, False
    try:
        components = data.get('components')
        nisi = next((row for row in components if isinstance(row, dict) and row.get('id') == 'nisi'),
                    None) if isinstance(components, list) else None
        if nisi is not None:
            state = nisi.get('state')
            author, reviewer = nisi.get('authorModel'), nisi.get('reviewerModel')
            if (state == 'ready' and isinstance(author, str) and isinstance(reviewer, str)
                    and author.strip() and reviewer.strip() and author.casefold() != reviewer.casefold()):
                pair = {author: 'author', reviewer: 'reviewer'}
                for key, role in list(pair.items()):
                    aliases = [row['id'] for row in data.get('models') or []
                               if not row.get('representedByInstances') and row.get('modelKey') == key
                               and isinstance(row.get('id'), str)]
                    if aliases:
                        pair.pop(key)
                        pair.update({alias: role for alias in aliases})
            live = state == 'in-use'
    except Exception:
        pair, live = None, False
    try:
        pipeline = data.get('pipeline')
        if isinstance(pipeline, dict) and pipeline.get('status') == 'running':
            live = True
    except Exception:
        pass
    return pair, live


def memory_sample(guard, data):
    """snapshot['memory'] and the mem-guard source row. Any failure reads as level 'unknown' with
    the source 'unavailable'; it never interrupts the rest of the sample. The Nisi route context
    is passed only when there is some, so a guard without those keywords still samples."""
    try:
        gpu = data.get('macGpu') or {}
        route_pair, route_live = memory_route_context(data)
        context = {}
        if route_pair is not None:
            context['route_pair'] = route_pair
        if route_live:
            context['route_live'] = True
        models = data.get('models') or []
        states = {s['id']: s.get('state') for s in data.get('sources') or []}
        models = ([row for row in models if not row.get('representedByInstances')]
                  if inventory_is_known(models, states) else None)
        return guard.sample(gpu.get('allocatedBytes'), models, **context)
    except Exception:
        return unknown_block(), {'id': 'mem-guard', 'label': 'Memory guard', 'state': 'unavailable',
                                 'ageSeconds': None, 'detail': 'Memory probes failed'}


def monitor_busy(server):
    """Return a reason to defer auto-unload while a control action is running."""
    repair = server.online_code_repair
    if repair is not None and repair.read().get('status') == 'running':
        return 'an Online Code check or repair is running'
    model = server.model_control.read()
    if (model.get('status') == 'running' or model.get('workerActive') is True
            or model.get('settlement') == 'unconfirmed'
            or model.get('cleanupConfirmed') is False):
        return 'a model load or unload is running or unconfirmed'
    return None


def fresh_route():
    """Read the router again immediately before an auto-unload request."""
    import datetime
    import telemetry

    now = time.time()
    observed = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
    router = telemetry._router_observation(now)
    pipeline, _source = telemetry._pipeline(now, router)
    return {'pipeline': pipeline, 'onlineCodeMode': telemetry._online_code_mode(now, observed, router)}


@contextmanager
def router_unload_guard(*, lock_path=None, route_read=None, install_read=None):
    """Hold router admission across an auto-unload worker's CLI and confirmation.

    A fresh route read under L0 EX closes the gap between the sampler's route check and
    the asynchronous ModelControl action. Never create or replace a router owner lock.
    """
    import telemetry

    path = Path(lock_path) if lock_path is not None else ROUTER_OWNER_LOCK
    route_read = route_read or fresh_route
    install_read = install_read or telemetry._router_install_state
    if telemetry._private_dir(path.parent) != 'ok':
        raise ControlError(409, 'Router admission directory is unavailable or unsafe; auto-unload stopped.')
    with telemetry.monitor_router_hold():
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, 'O_CLOEXEC', 0))
        except OSError:
            raise ControlError(409, 'Router admission lock is unavailable; auto-unload stopped.')
        try:
            held = os.fstat(fd)
            if (not stat.S_ISREG(held.st_mode) or held.st_uid != os.getuid()
                    or stat.S_IMODE(held.st_mode) & 0o077 or held.st_nlink != 1):
                raise ControlError(409, 'Router admission lock is unsafe; auto-unload stopped.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ControlError(409, 'Router admission is busy; auto-unload stopped.')
            try:
                current = path.lstat()
            except OSError:
                raise ControlError(409, 'Router admission lock changed; auto-unload stopped.')
            if ((current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                    or current.st_nlink != 1):
                raise ControlError(409, 'Router admission lock changed; auto-unload stopped.')
            install = install_read(path.parent)
            if not isinstance(install, dict) or install.get('inProgress') is not False:
                raise ControlError(409, 'Router install state is unavailable; auto-unload stopped.')
            if install.get('state') == 'installed':
                status, fence = telemetry._observe_retry(path.parent / 'install-fence.json', 1024)
                bound = fence.get('ownerLock') if status == 'ok' and isinstance(fence, dict) else None
                if (install.get('ownerLockBound') is not True or not isinstance(bound, dict)
                        or set(bound) != {'dev', 'ino'}
                        or (bound.get('dev'), bound.get('ino')) != (held.st_dev, held.st_ino)):
                    raise ControlError(409, 'Router owner lock is not bound to the installed code; auto-unload stopped.')
            elif install.get('state') not in ('absent', 'rolled-back'):
                raise ControlError(409, 'Router install state is not ready; auto-unload stopped.')
            try:
                fresh = route_read()
                problem = (router_problem(fresh.get('pipeline'), fresh.get('onlineCodeMode'))
                           if isinstance(fresh, dict) else 'router state is unavailable')
            except Exception:
                problem = 'fresh router read failed'
            if problem is not None:
                raise ControlError(409, f'Router is not quiet ({problem}); auto-unload stopped.')
            yield
        finally:
            os.close(fd)


_DIRECT_LMSTUDIO_ENDPOINT = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})\Z")


def _verified_direct_lmstudio_port(snapshot):
    """Return only the live, direct loopback endpoint verified by this full telemetry sample."""
    sources = snapshot.get('sources') if isinstance(snapshot, dict) else None
    if not isinstance(sources, list):
        return None
    matches = [row for row in sources if isinstance(row, dict) and row.get('id') == 'lmstudio-api']
    if len(matches) != 1:
        return None
    source = matches[0]
    if source.get('state') != 'live' or source.get('route') != 'direct':
        return None
    endpoint = source.get('endpoint')
    match = _DIRECT_LMSTUDIO_ENDPOINT.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if match is None:
        return None
    port = int(match.group(1))
    return port if port <= 65535 else None


def _verified_direct_lmstudio_listener(snapshot):
    if _verified_direct_lmstudio_port(snapshot) is None:
        return None
    source = next(row for row in snapshot['sources'] if row.get('id') == 'lmstudio-api')
    owner = source.get('verifiedListener')
    if (not isinstance(owner, dict) or owner.get('kind') != 'lmstudio'
            or type(owner.get('pid')) is not int or owner['pid'] <= 0
            or not isinstance(owner.get('executable'), str) or not owner['executable']):
        return None
    return owner['pid'], owner['executable']


def sample(store, stop, feed=None, tick=0.15, full_interval=1.0, guard=None, auto_unloader=None,
           publisher=None):
    """One loop: a full sample every second and, between them, change-driven overlays every tick.
    main() passes the monitor's memory guard (with its watchdog); without one, the memory block
    comes from a read-only guard that never pauses, notifies, journals or publishes level.json.
    A presence publisher (peer_feed.PresencePublisher) adds its source rows to each full sample and
    is handed the published snapshot afterwards; it writes on its own thread and never blocks this loop."""
    feed = feed or LiveFeed()
    callers = LocalCallers()
    if guard is None:
        guard = memory_guard(watchdog=False)
    next_full = 0.0
    while not stop.is_set():
        started = time.monotonic()
        if started < next_full:
            try:
                groups, prints = feed.changed()
                snapshot = store.read() if groups else None
                # Overlay only onto a recent full sample: a failing full sampler must
                # still age out in the UI instead of being re-stamped as fresh.
                if snapshot is not None and time.time() - snapshot.get('fullSampledAt', 0) <= FULL_SAMPLE_MAX_AGE:
                    store.publish(feed.overlay(snapshot, groups), partial=True)
                    feed.set_baseline(prints)
            except Exception:
                # A fast-path failure leaves the last full sample in place; the next
                # full sample re-reads everything.
                pass
            stop.wait(max(.02, min(tick, next_full - time.monotonic())))
            continue
        next_full = started + full_interval
        # Prints taken before the full read: a change during the read is caught next tick.
        try:
            prints = feed.fingerprints()
        except Exception:
            prints = None
        data = None
        try:
            try:
                data = collect_snapshot()
                data['fullSampledAt'] = data['sampledAt']
                data['modelControl'] = model_control_capability()
                data['macGpu'], gpu_source = mac_gpu()
                activity_known = activity_is_known(data.get('models', []),
                                                  {s['id']: s.get('state') for s in data['sources']})
                data['localCallers'], callers_source = callers.sample(
                    data.get('models', []), port=_verified_direct_lmstudio_port(data),
                    listener=_verified_direct_lmstudio_listener(data),
                    activity_known=activity_known)
                data['sources'].extend([gpu_source, callers_source])
            finally:
                # One memory reading and one watchdog tick per full sample (slow probes are cached),
                # taken even when the telemetry above failed: the watchdog resumes paused jobs.
                memory = memory_sample(guard, data if isinstance(data, dict) else {})
            data['memory'], memory_source = memory
            data['sources'].append(memory_source)
            activity = collect_activity()
            data['activity'] = {'runs': activity['runs'], 'sampledAt': activity.get('sampledAt'), 'historyBound': activity.get('historyBound'),
                                'complete': activity.get('complete', False)}
            data['sources'].extend(activity['sources'])
            try:
                clients, client_sources = collect_clients()
                data['clients'] = clients
                data['sources'].extend(client_sources)
            except Exception:
                # A client metadata reader must never interrupt the runtime
                # feed or turn an old model identity into live activity.
                data['clients'] = []
                data['sources'].append({'id': 'client-metadata', 'label': 'Client metadata',
                                        'state': 'error', 'ageSeconds': None,
                                        'detail': 'Client model records unavailable'})
            if auto_unloader is not None:
                data['autoUnload'] = auto_unloader.state()
            if publisher is not None:
                try:
                    data['sources'].extend(publisher.sources())
                except Exception:
                    data['sources'].append({'id': 'agiw-presence', 'label': 'AGIW presence (share)',
                                            'state': 'error', 'ageSeconds': None,
                                            'detail': 'Presence publisher state unreadable'})
            store.publish(data)
            if publisher is not None:
                try:
                    publisher.offer(store.read)  # due every 10 s; hands over the snapshot, never waits on the share
                except Exception:
                    pass
            if prints is not None:
                feed.set_baseline(prints)
            if auto_unloader is not None:
                auto_unloader.tick(data)
        except Exception:
            # Preserve last sample's time, so the UI ages it out. Never refresh
            # a stale busy model by stamping a failed collection as fresh.
            pass
        stop.wait(max(.02, min(tick, next_full - time.monotonic())))


ENDPOINT_PATH = Path.home() / '.local/state/inference-monitor/endpoint.json'


def write_endpoint(port, path=None):
    """Tell local scripts (agiw-status --json) where the loopback observer listens; best effort."""
    path = path or ENDPOINT_PATH
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f'.endpoint-{os.getpid()}.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump({'port': port, 'pid': os.getpid(), 'startedUnix': round(time.time(), 3)}, handle)
        os.replace(tmp, path)
    except OSError:
        pass


def start_peer_link(store, report=None):
    """Serve this Mac's latest snapshot to paired machines over the LAN; pairing needs a typed PIN.

    Never raises: any failure (a busy port, a corrupt or exposed state file, a slow ACL check) leaves the link
    off, notes it on stderr and hands the exception to report (PresencePublisher.link_failed, which shows an
    'agiw-peer-link' error row under Sources). The observer keeps running either way."""
    try:
        import socket
        import peer_feed
        import peer_link
        name = socket.gethostname().split('.')[0]

        def provide():
            with store.lock:
                snapshot = store.snapshot
            return peer_feed.mac_presence(snapshot, name.lower())

        return peer_link.PeerLink(peer_link.default_state_dir(), 'mac', name, provide,
                                  broadcast=peer_link.directed_broadcasts()).start()
    except Exception as exc:
        print(json.dumps({'peerLink': 'unavailable', 'reason': type(exc).__name__}), file=sys.stderr, flush=True)
        if report is not None:
            try:
                report(exc)
            except Exception:
                pass
        return None


def stop_peer_link(link):
    """Best effort: a link that fails to stop never skips the rest of shutdown."""
    try:
        link.stop()
    except Exception:
        pass


def peer_options(publish_status=False, peer_link=False, config=None, port=None):
    """(options, publisher) for the PC edition's opt-ins: flags win, then the AGIW config.json, both off by
    default. publishStatus or peerLink writes this Mac's presence to SharedChami every 10 s; peerLink also
    starts the LAN peer link. A missing peer_feed module only turns them off."""
    try:
        import peer_feed
        options = peer_feed.link_options(publish_status, peer_link, config)
        publisher = peer_feed.PresencePublisher(peer_feed.host_id(), enabled=options['presence'],
                                                version=peer_feed.app_version(), dashboard_port=port,
                                                enabled_by=options['enabledBy'],
                                                config_source=options['configSource'])
        return options, publisher
    except Exception:
        return {'publishStatus': False, 'peerLink': False, 'presence': False, 'lanLink': False,
                'enabledBy': '', 'configSource': None}, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--durable-model-control', action='store_true')
    parser.add_argument('--l2-open', action='store_true',
                        help='enable owner actions after the L2 handover has been verified (default: read-only)')
    parser.add_argument('--publish-status', action='store_true',
                        help="write this Mac's presence to llm-lab/status/agiw-peers/<host>.json on SharedChami "
                             'every 10 s for the PC edition (opt-in; mac-status.json belongs to the Mac status script)')
    parser.add_argument('--peer-link', action='store_true',
                        help='publish presence as --publish-status does, and announce this Mac on the LAN and serve '
                             'its status to paired AGIW machines (read-only)')
    parser.add_argument('--config', type=Path,
                        help='JSON with publishStatus / peerLink booleans '
                             '(default: ~/Library/Application Support/AGIW/config.json)')
    args = parser.parse_args()
    store = SnapshotStore()
    owner_options = {'l2_open': True} if args.l2_open else {}
    if args.durable_model_control:
        owner_options['durable_model_control'] = True
    server = MonitorServer(('127.0.0.1', args.port), store, **owner_options)
    options, publisher = peer_options(args.publish_status, args.peer_link, args.config, server.server_address[1])
    print(json.dumps({'host': getattr(publisher, 'host', None), 'publishStatus': options['publishStatus'],
                      'peerLink': options['peerLink'], 'config': 'error' if options['configSource'] else 'ok'}),
          file=sys.stderr, flush=True)
    stop = threading.Event()
    # Built before sampling starts: it resumes any job a crashed monitor left paused.
    # Only this production guard publishes valid sysctl readings for sandboxed CLI callers.
    guard = memory_guard(level_file=LevelFile())
    auto = None
    if args.parent_pid and args.parent_pid == os.getppid() and mac_online_code_controls_enabled():
        auto = AutoUnloader(
            unload_fn=lambda instance: server.model_control.request(
                'unload', instance, action_guard=router_unload_guard),
            status_fn=server.model_control.read,
            busy_fn=lambda: monitor_busy(server),
            route_check_fn=fresh_route)
    server.auto_unloader = auto
    threading.Thread(target=sample, args=(store, stop),
                     kwargs={'guard': guard, 'auto_unloader': auto, 'publisher': publisher}, daemon=True).start()
    def shutdown(*_):
        stop.set()
        stopping = getattr(server, 'stopping', None)
        if stopping is not None:  # ends open live streams promptly
            stopping.set()
        cancel_windows_worker_probe()
        if server.online_code_repair is not None:
            # The Swift parent sends SIGKILL after 1.5 seconds. Kill private
            # owner process groups here, before the repair worker can be lost.
            server.online_code_repair.cancel()
        if getattr(server, 'model_control', None) is not None:
            # A killed CLI is not proof that LM Studio rolled back an accepted
            # action. Keep ModelControl's uncertainty latch until settlement.
            server.model_control.cancel()
        if guard is not None:
            # Resume paused jobs here too, not only after serve_forever's poll returns.
            guard.close()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if args.parent_pid:
        def watch_parent():
            while not stop.wait(2):
                if os.getppid() != args.parent_pid:
                    shutdown()
                    return
        threading.Thread(target=watch_parent, daemon=True).start()
    print(json.dumps({'port': server.server_address[1], 'pid': os.getpid()}), flush=True)
    write_endpoint(server.server_address[1])
    # The LAN link starts off the startup path: the app allows 10 s for the port line and five failed polls
    # after it, while the link's ACL checks run subprocesses with their own timeouts.
    link_lock = threading.Lock()
    link_slot = {'link': None, 'closed': False}
    if options['lanLink']:
        def bring_up_link():
            link = start_peer_link(store, report=getattr(publisher, 'link_failed', None))
            with link_lock:
                if not link_slot['closed']:
                    link_slot['link'] = link
                    return
            if link is not None:  # shutdown began while it was starting
                stop_peer_link(link)
        threading.Thread(target=bring_up_link, name='agiw-peer-link-start', daemon=True).start()
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        stop.set()
        if auto is not None:
            auto.close()
        if guard is not None:
            guard.close()  # resume every job the memory watchdog paused
        if server.online_code_repair is not None:
            server.online_code_repair.cancel()
        if getattr(server, 'model_control', None) is not None:
            server.model_control.cancel()
        with link_lock:
            link_slot['closed'] = True
            link = link_slot['link']
        if link is not None:
            stop_peer_link(link)
        if publisher is not None:
            publisher.close()  # a write hung on the share is abandoned (daemon thread), never waited on
        stop_windows_worker_probe(timeout=.45)
        if server.online_code_repair is not None:
            server.online_code_repair.join(timeout=.45)
        if getattr(server, 'model_control', None) is not None:
            server.model_control.join(timeout=.45)
        server.server_close()


if __name__ == '__main__':
    main()
