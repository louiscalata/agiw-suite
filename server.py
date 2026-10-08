#!/usr/bin/env python3
"""Loopback monitor with a passive sampler and explicit local model controls."""
from __future__ import annotations
import argparse
import copy
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
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
from online_code_repair import OnlineCodeRepair
from live_feed import LiveFeed
from gpu_probe import mac_gpu
from local_callers import LocalCallers
from mem_guard import Guard, LevelFile, unknown_block
from bundled_components import BundledComponents
from jev_connection import JevConnection
from tool_connectors import ToolConnectors, ConnectorError

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


def mac_online_code_controls_enabled():
    """Only an ordinary, same-user macOS process may expose owner actions."""
    return (sys.platform == 'darwin' and os.getuid() != 0
            and os.getuid() == os.geteuid())


def activity_is_known(rows, source_status):
    """A responding command is insufficient when loaded rows lack exact activity."""
    if source_status.get('lms-ps') != 'live':
        return False
    loaded = [row for row in rows if row.get('loaded') is True]
    for row in loaded:
        max_age = 30 if row.get('host') == 'windows' else 3
        age = row.get('ageSeconds')
        if (row.get('source') != 'lms-ps' or row.get('state') not in KNOWN_ACTIVITY
                or not isinstance(age, (int, float)) or age > max_age):
            return False
    return True


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
        data = copy.deepcopy(data)
        now = data['sampledAt']
        rows = data.get('models', [])
        source_status = {s['id']: s.get('state') for s in data.get('sources', [])}
        activity_source_live = source_status.get('lms-ps') == 'live'
        states = {f"{r['host']}:{r['id']}": {'state': r['state'], 'source': r.get('source'),
                                               'activitySourceLive': activity_source_live}
                  for r in rows}
        live = [r for r in rows if r.get('ageSeconds') is not None
                and r['ageSeconds'] <= (30 if r.get('host') == 'windows' else 3)]
        activity_known = activity_is_known(rows, source_status)
        inventory_known = activity_known or source_status.get('lmstudio-api') == 'live'
        data['activityKnown'] = activity_known
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
                                     'active': sum(r['state'] in ACTIVE for r in live) if activity_known else None,
                                     'loaded': sum(r.get('loaded') is True for r in live) if inventory_known else None})
                self.history = self.history[-90:]
            data.update(sequence=self.sequence, history=copy.deepcopy(self.history),
                        events=copy.deepcopy(self.events), intervalSeconds=1)
            self.snapshot = data
            self.changed.notify_all()

    def read(self):
        with self.lock:
            return copy.deepcopy(self.snapshot)

    def wait_for_change(self, last_sequence, timeout):
        """The newest snapshot and its sequence once it differs from last_sequence, or (None, last) on timeout."""
        with self.changed:
            if not self.changed.wait_for(lambda: self.snapshot is not None and self.sequence != last_sequence,
                                         timeout=timeout):
                return None, last_sequence
            return copy.deepcopy(self.snapshot), self.sequence


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 8
    max_connections = 8
    socket_timeout_seconds = 1.0
    connection_deadline_seconds = 2.0
    max_streams = 3
    stream_write_timeout_seconds = 5.0

    def __init__(self, address, store, *, durable_model_control=False, tool_connectors=None):
        if address[0] != '127.0.0.1':
            raise ValueError('Monitor must bind to IPv4 loopback')
        if type(durable_model_control) is not bool:
            raise ValueError('Durable model control flag must be boolean')
        self.store = store
        # Explicit opt-in only. ModelControl reads an existing journal and
        # blocks actions on missing/corrupt state; startup never provisions it.
        self.model_control = (ModelControl(store, journal_path=MODEL_JOURNAL_PATH)
                              if durable_model_control else ModelControl(store))
        self.auto_unloader = None
        self.online_code_repair = OnlineCodeRepair() if mac_online_code_controls_enabled() else None
        self.bundled_components = BundledComponents()
        self.jev_connection = JevConnection()
        self.tool_connectors = tool_connectors if tool_connectors is not None else ToolConnectors()
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
        if path == '/api/tool-connectors':
            try:
                return self.reply(200, json.dumps(self.server.tool_connectors.read(), allow_nan=False).encode(),
                                  'application/json')
            except ConnectorError as error:
                return self.connector_error(error)
        if path == '/api/stream':
            return self.stream()
        if path == '/api/snapshot':
            data = self.server.store.read()
            if data is None:
                return self.reply(503, b'{"status":"starting"}', 'application/json')
            return self.reply(200, json.dumps(data, allow_nan=False).encode(), 'application/json')
        if path == '/api/models/control':
            return self.reply(200, json.dumps(self.server.model_control.read()).encode(), 'application/json')
        if path == '/api/models/auto-unload':
            if self.server.auto_unloader is None:
                return self.control_error(501, 'Auto-unload runs only in the Monitor app.')
            return self.reply(200, json.dumps(self.server.auto_unloader.state()).encode(), 'application/json')
        if path in {'/api/online-code-mode/repair', '/api/inference/fix'}:
            if not mac_online_code_controls_enabled() or self.server.online_code_repair is None:
                return self.control_error(501, 'Online Code Mode controls belong to the Mac owner.')
            return self.reply(200, json.dumps(self.server.online_code_repair.read()).encode(),
                              'application/json')
        if path in {'/api/online-code-mode/entry', '/api/online-code-mode/headless'}:
            if not mac_online_code_controls_enabled() or self.server.online_code_repair is None:
                return self.control_error(501, 'Online Code Mode controls belong to the Mac owner.')
            return self.reply(200, json.dumps(self.server.online_code_repair.read()).encode(),
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
        if self.path not in {'/api/models/control', '/api/models/auto-unload',
                             '/api/components/nisi/check',
                             '/api/components/jev/check',
                             '/api/online-code-mode/repair',
                             '/api/online-code-mode/entry', '/api/online-code-mode/headless',
                             '/api/inference/fix', '/api/tool-connectors'}:
            return self.reply(404, b'{"status":"error","message":"Not found."}', 'application/json')
        if not self.same_origin(require_origin=True):
            return self.control_error(403, 'Exact same-origin Origin and Host are required.')
        if (self.path in {'/api/online-code-mode/repair', '/api/online-code-mode/entry',
                          '/api/online-code-mode/headless', '/api/inference/fix'}
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
            if self.path == '/api/tool-connectors':
                if type(data) is not dict or type(data.get('action')) is not str:
                    return self.control_error(400, 'Body requires an explicit connector action.')
                action = data['action']
                if action == 'add' and set(data) == {'action', 'id', 'url'}:
                    result = self.server.tool_connectors.add(data['id'], data['url'])
                    return self.reply(201, json.dumps(result, allow_nan=False).encode(), 'application/json')
                if action == 'test' and set(data) == {'action', 'id'}:
                    result = self.server.tool_connectors.test(data['id'])
                    return self.reply(202, json.dumps(result, allow_nan=False).encode(), 'application/json')
                if action == 'disconnect' and set(data) == {'action', 'id'}:
                    result = self.server.tool_connectors.disconnect(data['id'])
                    return self.reply(200, json.dumps(result, allow_nan=False).encode(), 'application/json')
                return self.control_error(400, 'Body must be add (id, url), test (id), or disconnect (id).')
            if self.path == '/api/components/jev/check':
                if data != {'action': 'connection-check'}:
                    return self.control_error(400, 'Body must contain only action: connection-check.')
                accepted = self.server.jev_connection.request_check()
            elif self.path == '/api/components/nisi/check':
                if data != {'action': 'self-check'}:
                    return self.control_error(400, 'Body must contain only action: self-check.')
                accepted = self.server.bundled_components.request_check()
            elif self.path == '/api/online-code-mode/repair':
                if data != {'action': 'check-and-repair'}:
                    return self.control_error(400, 'Body must contain only action: check-and-repair.')
                accepted = self.server.online_code_repair.request()
            elif self.path == '/api/online-code-mode/entry':
                if data != {'action': 'readiness'}:
                    return self.control_error(400, 'Body must contain only action: readiness.')
                accepted = self.server.online_code_repair.request_entry()
            elif self.path == '/api/online-code-mode/headless':
                if data not in ({'action': 'on'}, {'action': 'off'}):
                    return self.control_error(400, 'Body must contain only action: on or off.')
                accepted = self.server.online_code_repair.request_headless(data['action'])
            elif self.path == '/api/inference/fix':
                # 'all' is the one-action UI repair; older scoped clients remain valid.
                # Its Nisi component still enforces every measured recovery precondition.
                if (not isinstance(data, dict) or set(data) != {'scope'}
                        or type(data['scope']) is not str
                        or data['scope'] not in {'local', 'route', 'both', 'nisi', 'all'}):
                    return self.control_error(400, 'Body must contain only scope: all, local, route, both, or nisi.')
                accepted = self.server.online_code_repair.request_fix(data['scope'])
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
        except (UnicodeDecodeError, ValueError, RecursionError, json.JSONDecodeError):
            return self.control_error(400, 'Request body must be valid JSON with unique fields.')
        except ControlError as error:
            return self.control_error(error.status, error.message)
        except ConnectorError as error:
            return self.connector_error(error)
        return self.reply(202, json.dumps(accepted).encode(), 'application/json')

    def connector_error(self, error):
        return self.reply(error.status, json.dumps({'status': 'error', 'code': error.code,
                                                    'message': error.message}).encode(), 'application/json')

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
        """Server-Sent Events: each new snapshot as soon as it is published, a comment heartbeat otherwise."""
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


def memory_sample(guard, data):
    """snapshot['memory'] and the mem-guard source row. Any failure reads as level 'unknown' with
    the source 'unavailable'; it never interrupts the rest of the sample."""
    try:
        gpu = data.get('macGpu') or {}
        return guard.sample(gpu.get('allocatedBytes'), data.get('models'))
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


def sample(store, stop, feed=None, tick=0.15, full_interval=1.0, guard=None, auto_unloader=None):
    """One loop: a full sample every second and, between them, change-driven overlays every tick.
    main() passes the monitor's memory guard (with its watchdog); without one, the memory block
    comes from a read-only guard that never pauses, notifies, journals or publishes level.json."""
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
                data['localCallers'], callers_source = callers.sample(data.get('models', []))
                data['sources'].extend([gpu_source, callers_source])
            finally:
                # One memory reading and one watchdog tick per full sample (slow probes are cached),
                # taken even when the telemetry above failed: the watchdog resumes paused jobs.
                memory = memory_sample(guard, data if isinstance(data, dict) else {})
            data['memory'], memory_source = memory
            data['sources'].append(memory_source)
            activity = collect_activity()
            data['activity'] = {'runs': activity['runs']}
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
            store.publish(data)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--durable-model-control', action='store_true')
    args = parser.parse_args()
    store = SnapshotStore()
    server = (MonitorServer(('127.0.0.1', args.port), store, durable_model_control=True)
              if args.durable_model_control else MonitorServer(('127.0.0.1', args.port), store))
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
                     kwargs={'guard': guard, 'auto_unloader': auto}, daemon=True).start()
    def shutdown(*_):
        stop.set()
        stopping = getattr(server, 'stopping', None)
        if stopping is not None:  # ends open live streams promptly
            stopping.set()
        if getattr(server, 'tool_connectors', None) is not None:
            server.tool_connectors.stop()
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
    try:
        server.serve_forever(poll_interval=.25)
    finally:
        stop.set()
        if auto is not None:
            auto.close()
        if getattr(server, 'tool_connectors', None) is not None:
            server.tool_connectors.stop()
        if guard is not None:
            guard.close()  # resume every job the memory watchdog paused
        if server.online_code_repair is not None:
            server.online_code_repair.cancel()
        if getattr(server, 'model_control', None) is not None:
            server.model_control.cancel()
        stop_windows_worker_probe(timeout=.45)
        if server.online_code_repair is not None:
            server.online_code_repair.join(timeout=.45)
        if getattr(server, 'model_control', None) is not None:
            server.model_control.join(timeout=.45)
        server.server_close()


if __name__ == '__main__':
    main()
