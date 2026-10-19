"""Deterministic offline inputs for the real monitor collector and publisher.

Only acquisition boundaries are substituted. No sockets, model commands, shares,
or installed state are accessed by this fixture.
"""
import contextlib
import copy
from unittest import mock

import telemetry

NOW = 1_800_000_000.0
API = {'models': [{'key': 'fixture/model', 'loaded_instances': [{'id': 'fixture/model'}]}]}
CLI = {'models': [{'identifier': 'fixture/model', 'status': 'generating'}]}


def collect(*, api=None, cli=None, peers=None):
    source = lambda key: {'id': key, 'state': 'unavailable', 'ageSeconds': None, 'detail': 'Fixture unavailable'}
    with contextlib.ExitStack() as stack:
        for name, value in {
            '_lms_server_status': {'port': 1234, 'running': True},
            '_inventory_listener': (982, 'lmstudio', '/fixture/LM Studio'),
            '_pair_snapshot': {'state': 'unknown'},
            '_bounded_http': API if api is None else api,
            '_bounded_lms': CLI if cli is None else cli,
            '_afm_status': {'state': 'unknown'},
            'observe_router': {},
            '_pipeline': ({'status': 'unknown', 'pipelines': []}, source('pipeline-router')),
            '_online_code_mode': {'state': 'unknown'},
            '_windows_worker': ({'state': 'unknown'}, source('windows-worker')),
            '_windows_headless': {'state': 'unknown'},
            '_windows_jobs': ({'journal': 'absent', 'inFlight': [], 'recent': []}, source('windows-jobs')),
            '_canary': ({'enabled': None}, source('canary')),
            'collect_nisi_v02': ({'runtimeIntegrity': 'UNKNOWN'}, source('nisi-v02-runtime')),
            '_route_components': [],
        }.items():
            stack.enter_context(mock.patch.object(telemetry, name,
                **({'side_effect': value} if isinstance(value, Exception) else {'return_value': copy.deepcopy(value)})))
        stack.enter_context(mock.patch.object(telemetry.peer_feed, 'peer_feeds', return_value=copy.deepcopy(peers or [])))
        stack.enter_context(mock.patch.object(telemetry.time, 'time', return_value=NOW))
        return telemetry.collect_snapshot()

# Input mutations are independent of outputs. The oracle below names only fields
# that depend on damaged evidence; unrelated healthy sources remain observable.
import datetime as dt
import fcntl
import os
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import activity
import local_callers
import machine_state
import peer_feed
import server

MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'stale', 'hidden', 'future')
SOURCES = ('api', 'cli', 'windows', 'linux', 'callers', 'router', 'worker', 'jobs', 'model-age', 'activity')


def iso(at):
    return dt.datetime.fromtimestamp(at, dt.timezone.utc).isoformat()


def peer(platform, mode=None):
    host = platform + '-fixture'
    doc = {'schemaVersion': 1, 'kind': 'agiw-peer', 'platform': platform, 'host': host,
           'observedAt': iso(NOW), 'health': 'ok', 'modelsTotal': 1,
           'models': [{'id': 'fixture/model', 'loadedState': 'loaded', 'activity': 'busy'}]}
    if mode in ('missing', 'unreadable', 'hidden'):
        return peer_feed._feed_failure(platform, host, 'read_error' if mode != 'missing' else 'not_observed')
    if mode == 'malformed':
        doc = '{broken'
    elif mode == 'partial':
        doc['models'].append({'id': '', 'loadedState': 'loaded'})
        doc['modelsTotal'] = 2
    elif mode == 'partial-zero':
        doc['models'] = [{'id': 'inactive', 'loadedState': 'inactive'}, {'id': '', 'loadedState': 'loaded'}]
        doc['modelsTotal'] = 2
    elif mode == 'capped':
        doc['modelsTotal'] = 33
        doc['models'] += [{'id': f'capped-{i}', 'loadedState': 'loaded', 'activity': 'busy'} for i in range(32)]
    elif mode in ('stale', 'future'):
        doc['observedAt'] = iso(NOW + (60 if mode == 'future' else -60))
    return peer_feed.summarize_document(doc, 'share' if platform == 'windows' else 'ssh', NOW,
                                         platform=platform, host=host)


def model_input(source, mode=None):
    payload = copy.deepcopy(API if source == 'api' else CLI)
    if mode in ('missing', 'unreadable', 'hidden'):
        return FileNotFoundError() if mode == 'missing' else PermissionError()
    if mode == 'malformed':
        return {'models': 'not a list'}
    if mode == 'partial':
        payload['models'].append({'loaded_instances': [{}], 'status': 'generating'})
    if mode == 'capped':
        payload['models'] += [({'key': f'capped-{i}', 'loaded_instances': []} if source == 'api'
                               else {'identifier': f'capped-{i}', 'status': 'idle'}) for i in range(256)]
    # Freshness mutations enter below at the parsed observation envelope, the
    # same relative ages the fast publisher overlay carries. API has no timestamp.
    return payload


def callers(mode=None, models=None):
    def runner(args, **kwargs):
        if mode in ('unreadable', 'missing'):
            raise PermissionError('fixture')
        listener = '-sTCP:LISTEN' in args
        out = 'p982\ncLM Studio\nn127.0.0.1:1234\n' if listener else 'p982\ncLM Studio\nn127.0.0.1:1234->127.0.0.1:50000\np9\ncCodex\nn127.0.0.1:50000->127.0.0.1:1234\n'
        code, err = 0, ''
        if mode == 'hidden':
            out, code = '', 1
        elif mode == 'malformed':
            out = 'unparseable output\n'
        elif mode == 'partial':
            err = 'lsof: permission denied for a process'
        elif mode == 'capped':
            out = 'x' * ((1 << 20) + 1)
        return subprocess.CompletedProcess(args, code, out, err)
    result = local_callers.LocalCallers(runner, clock=lambda: 100, executable=lambda pid: '/fixture/LM Studio').sample([{'host': 'mac', 'state': 'busy'}] if models is None else models, port=1234, listener=(982, '/fixture/LM Studio'), activity_known=True)
    if mode in ('stale', 'future'):
        result[1]['ageSeconds'] = 30 if mode == 'stale' else -30
    return result


def router_fixture(root, mode=None):
    # Real journal reader with private local fixture directories and acquisition
    # substitutions for lsof/ps. No process is started or contacted.
    path = root / ('router-' + str(mode)); path.mkdir(mode=0o700)
    for part in ('active', 'notes', 'locks', 'archive', 'checkpoints'):
        (path / part).mkdir(mode=0o700)
    (path / 'owner.lock').touch(mode=0o600)
    record = {'schemaVersion': 1, 'runId': 'fixture-route', 'inputSha256': 'a' * 64,
              'startedUnix': NOW - 1, 'stage': 'started', 'checkpoint': None}
    if mode in ('malformed', 'partial', 'stale', 'future'):
        if mode in ('stale', 'future'):
            record['startedUnix'] = NOW - 600 if mode == 'stale' else NOW + 600
        active = path / 'active.json'
        active.write_text('{broken' if mode == 'malformed' else json.dumps(record if mode != 'partial' else {}))
        active.chmod(0o600)
    with contextlib.ExitStack() as stack:
        if mode == 'projection-cap':
            for index in range(9):
                run_id = f'live-{index}'
                fd = os.open(path / 'locks' / (run_id + '.lock'), os.O_CREAT | os.O_RDWR, 0o600)
                stack.callback(os.close, fd)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                info = os.fstat(fd)
                active = path / 'active' / (run_id + '.json')
                active.write_text(json.dumps({**record, 'runId': run_id, 'runLock': {'dev': info.st_dev, 'ino': info.st_ino}}))
                active.chmod(0o600)
        for key, val in {'_ROUTER_ROOT': path, '_ACTIVE_PATH': path / 'active.json',
                         '_PENDING_PATH': path / 'pending.json', '_LAUNCHER_ROOT': path / 'launcher',
                         '_READINESS_PATH': path / 'readiness.json'}.items():
            stack.enter_context(mock.patch.object(telemetry, key, val))
        stack.enter_context(mock.patch.object(telemetry, '_router_openers', return_value=[]))
        stack.enter_context(mock.patch.object(telemetry, '_router_namespace_initialized', return_value=mode != 'missing'))
        if mode in ('unreadable', 'hidden'):
            stack.enter_context(mock.patch.object(telemetry, '_router_listing', return_value=([], False, 'unsafe')))
        elif mode == 'capped':
            stack.enter_context(mock.patch.object(telemetry, '_router_listing', return_value=([], True, 'ok')))
        obs = telemetry.observe_router(NOW)
        pipeline, source = telemetry._pipeline(NOW, obs)
        mode = telemetry._online_code_mode(NOW, iso(NOW), obs)
        telemetry._mark_live_route(pipeline, source, mode)
        return (pipeline, source), mode


def worker(mode=None, root=None):
    # Exercise the real cached heartbeat projection; prohibit its refresh thread.
    cmd = root / 'worker-fixture'
    cmd.write_text('fixture only; never executed')
    cmd.chmod(0o700)
    record = {'ok': True, 'evidence_scope': telemetry._WINDOWS_WORKER_SCOPE,
              'age': 0, 'models': ['fixture/model']}
    if mode in ('missing', 'unreadable', 'hidden'):
        raw = PermissionError()
    elif mode == 'malformed':
        raw = '{bad'
    else:
        if mode == 'partial': record['models'].append(None)
        if mode == 'capped': record['models'] *= 129
        raw = json.dumps(record)
    with mock.patch.object(telemetry, '_WINDOWS_WORKER_CMD', cmd), \
         mock.patch.object(telemetry, '_windows_worker_reader_blocked', return_value=False), \
         mock.patch.object(telemetry, '_bounded_command',
             **({'side_effect': raw} if isinstance(raw, Exception) else {'return_value': raw})), \
         mock.patch.object(telemetry.time, 'time', return_value=NOW):
        acquired = telemetry._windows_worker_probe_locked()
    cache = dict(telemetry._WINDOWS_WORKER_CACHE)
    cache.update(checkedMonotonic=1e30, probeRunning=False, heartbeatUnix=NOW,
                 modelsAdvertised=['fixture/model'], modelCount=1, lanes=None, hardware=None,
                 workerCondition=None, probeError=False, probePaused=False, probeBusy=False)
    if acquired is None:
        cache.update(heartbeatUnix=None, probeError=True)
    elif mode in ('stale', 'future'):
        cache['heartbeatUnix'] = NOW + (600 if mode == 'future' else -600)
    with mock.patch.object(telemetry, '_WINDOWS_WORKER_CACHE', cache):
        return telemetry._windows_worker(NOW)


def jobs(mode=None):
    record = {'v': 1, 'event': 'enqueued', 'id': 'mac-20260925-080000-' + 'a' * 32,
              'unix': NOW - 1, 'model': 'fixture/model', 'timeout': 60, 'pid': 99}
    raw = json.dumps(record).encode() + b'\n'
    if mode == 'missing':
        raw = None
    elif mode in ('unreadable', 'hidden'):
        raw = PermissionError()
    elif mode == 'malformed':
        raw = b'{broken\n'
    elif mode == 'partial':
        raw += b'{broken\n'
    elif mode == 'partial-success':
        raw += json.dumps({**record, 'event': 'result', 'status': 'success', 'elapsedSeconds': 1}).encode() + b'\n{broken\n'
    elif mode == 'capped':
        raw = b'{}\n' * 2001 + raw
    elif mode in ('stale', 'future'):
        record['unix'] = NOW + (600 if mode == 'future' else -600)
        raw = json.dumps(record).encode() + b'\n'
    with mock.patch.object(telemetry, '_read_windows_journal',
            **({'side_effect': raw} if isinstance(raw, Exception) else {'return_value': raw})), \
         mock.patch.object(telemetry, '_pid_alive', return_value=True):
        result, source = telemetry._windows_jobs(NOW)
        # A stale observation is not a quiet, freshly read event log.
        if mode == 'stale': result['observationAgeSeconds'] = 600
        return result, source


def activity_fixture(root, mode=None):
    path = root / ('activity-' + str(mode)); path.mkdir(mode=0o700)
    archive, receipts = path / 'archive', path / 'receipts'
    if mode != 'missing':
        archive.mkdir(mode=0o700); receipts.mkdir(mode=0o700)
        record = {'runId': 'archive-fixture', 'finishedUnix': NOW - 1,
                  'result': {'kind': 'codemode.router.v1', 'runId': 'archive-fixture', 'status': 'RESPONSE_VALIDATED'}}
        if mode in ('stale', 'future'):
            record['finishedUnix'] = NOW + (600 if mode == 'future' else -600)
        file = archive / 'archive-fixture.json'
        file.write_text('{bad' if mode == 'malformed' else json.dumps(record))
        file.chmod(0o600)
        if mode == 'partial':
            (archive / 'broken.json').write_text('{}'); (archive / 'broken.json').chmod(0o600)
        if mode in ('hidden', 'unreadable'):
            archive.chmod(0o755)
    with mock.patch.object(activity, '_ROOT', path), mock.patch.object(activity, '_RECEIPTS', receipts), \
         mock.patch.object(activity, '_active_rows', return_value=([], False, 'ok')), \
         mock.patch.object(activity, '_MAX_ARCHIVE_SCAN', 0 if mode == 'capped' else 4096):
        result = activity.collect_activity(NOW)
        # Preserve the degraded-observation dimension; old terminal events
        # in a current scan are separately tested by the positive generator.
        if mode == 'stale': result['sampledAt'] = NOW - 600
        return result


def matrix():
    yield {}
    for source in SOURCES:
        for mode in MODES:
            yield {source: mode}
    for a, b in itertools.combinations(SOURCES, 2):
        for ma, mb in itertools.product(MODES, repeat=2):
            yield {a: ma, b: mb}
    for mode in MODES:
        yield {'model-age': mode, 'profile': 'idle'}
    yield {'profile': 'idle'}
    yield {'profile': 'empty'}
    yield {'feed-age': 'stale'}
    yield {'feed-age': 'future'}
    yield {'router': 'projection-cap'}
    yield {'combined-cap': True}
    yield {'windows': 'partial-zero'}
    yield {'jobs': 'partial-success'}


def generate():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        routers = {mode: router_fixture(root, mode) for mode in (None, *MODES, 'projection-cap')}
        cached = {name: {mode: fn(mode) for mode in (None, *MODES)} for name, fn in
                  [('worker', lambda mode: worker(mode, root)), ('jobs', jobs),
                   ('activity', lambda mode: activity_fixture(root, mode))]}
        cached['jobs']['partial-success'] = jobs('partial-success')
        for mutations in matrix():
            api, cli = model_input('api', mutations.get('api')), model_input('cli', mutations.get('cli'))
            if mutations.get('combined-cap'):
                api = {'models': [{'key': f'inactive-{i}', 'loaded_instances': []} for i in range(256)]}
                cli = {'models': [{'identifier': 'outside', 'status': 'generating'}]}
            if mutations.get('profile') == 'idle': cli['models'][0]['status'] = 'idle'
            if mutations.get('profile') == 'empty': api, cli = {'models': []}, {'models': []}
            snap = collect(api=api, cli=cli,
                           peers=[peer(p, mutations.get(p)) for p in ('windows', 'linux')])
            for key in ('api', 'cli'):
                mode = mutations.get(key)
                if mode in ('stale', 'future'):
                    age = 30 if mode == 'stale' else -30
                    source = 'lmstudio-api' if key == 'api' else 'lms-ps'
                    for row in snap['sources'] + snap['models']:
                        if row.get('id') == source or row.get('source') == source:
                            row['ageSeconds'] = age
            mode = mutations.get('model-age')
            if mode:
                for row in snap['models']:
                    if mode == 'missing':
                        row.pop('ageSeconds', None)
                    else:
                        row['ageSeconds'] = {'unreadable': None, 'hidden': None, 'malformed': 'bad',
                                             'partial': None, 'capped': None, 'stale': 30, 'future': -30}[mode]
            snap['machines'][0] = machine_state.local_summary(snap)
            (snap['pipeline'], router_source), snap['onlineCodeMode'] = copy.deepcopy(routers[mutations.get('router')])
            snap['windowsWorker'], worker_source = copy.deepcopy(cached['worker'][mutations.get('worker')])
            snap['localCallers'], callers_source = callers(mutations.get('callers'), snap['models'])
            snap['windowsJobs'], jobs_source = copy.deepcopy(cached['jobs'][mutations.get('jobs')])
            act = copy.deepcopy(cached['activity'][mutations.get('activity')])
            snap['activity'] = {key: act[key] for key in ('runs', 'historyBound', 'sampledAt')}
            if 'complete' in act: snap['activity']['complete'] = act['complete']
            replacements = [router_source, worker_source, callers_source, jobs_source, *act['sources']]
            snap['sources'] = [s for s in snap['sources'] if s['id'] not in {r['id'] for r in replacements}] + replacements
            with mock.patch.object(telemetry, '_JEV_ENV_PATH', root / 'no-env'), \
                 mock.patch.object(telemetry, '_last_jev_judgment', return_value=(None, None)):
                snap['components'] = telemetry._route_components(snap['pipeline'],
                    {'runtimeIntegrity': 'VERIFIED', 'hostBinding': 'VERIFIED'}, {r['id']: r for r in snap['models']}, NOW,
                    models_complete=snap['machines'][0]['modelsComplete'])
            store = server.SnapshotStore()
            with mock.patch.object(server.time, 'time', return_value=NOW):
                store.publish(snap)
            read_now = NOW + (10 if mutations.get('feed-age') == 'stale' else -10 if mutations.get('feed-age') == 'future' else 0)
            with mock.patch.object(server.time, 'time', return_value=read_now):
                published = store.read()
            try:
                presence = peer_feed.mac_presence(published, 'fixture-mac')
                presence_error = None
            except (ValueError, peer_feed.InventoryUnknown) as exc:
                presence, presence_error = None, str(exc)
            yield {'case': mutations, 'now': read_now, 'snapshot': published,
                   'presence': presence, 'presenceError': presence_error}


if __name__ == '__main__':
    for case in generate():
        print(json.dumps(case, allow_nan=False, separators=(',', ':')))
