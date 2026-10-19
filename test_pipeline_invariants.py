"""Deterministic, dependency-aware single/pair degradation of publication inputs.

The raw model and socket fixtures pass through production parsers. Every case
then calls SnapshotStore.publish and mac_presence, crosses JSON, and executes
the production web view models AND app.js DOM renderers in Node. No live I/O.
The 5,938 original cases and 339 pass-9 cases are retained. Pass 10 adds
309 two-sided/temporal cases, including all 105 original old-event inputs. Run with --check (or unittest / the JS suite).
"""
import copy
from contextlib import ExitStack
import itertools
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock
import time

import pair_status

import local_callers
import peer_feed
import server
import telemetry

NOW = 1800000000
MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'stale', 'hidden', 'future')
SOURCES = ('api', 'cli', 'callers', 'model-age', 'snapshot', 'router', 'history', 'gpu', 'worker', 'peers', 'memory', 'jobs', 'pair', 'api-instances')
OWNER = (982, '/fixture/LM Studio')
API = {'models': [{'key': name, 'loaded_instances': [{'id': name}]} for name in ('provider/a', 'provider/b')]}
CLI = {'models': [{'identifier': name, 'modelKey': name, 'status': phase, 'queued': 0}
                  for name, phase in [('provider/a', 'generating'), ('provider/b', 'idle')]]}
SOCKETS = 'p982\ncLM Studio\nn10.99.0.176:1235->10.99.0.176:51111\np9\ncCodex\nn10.99.0.176:51111->10.99.0.176:1235\n'


def source(key, **fields):
    return {'id': key, 'label': key, 'state': 'live', 'ageSeconds': 0, 'detail': 'Fixture observed', **fields}


def inventory(kind, mode, instance_mode=None, empty=False):
    payload = copy.deepcopy(API if kind == 'api' else CLI)
    if empty: payload['models'] = []
    parse = telemetry._parse_api if kind == 'api' else telemetry._parse_lms
    if mode in ('missing', 'unreadable', 'hidden'):
        raise FileNotFoundError(mode) if mode == 'missing' else PermissionError(mode)
    if mode == 'malformed':
        payload = json.loads('{"models": [null]}')
    elif mode == 'partial':
        # A positive observation remains; completeness of the other row is lost.
        if kind == 'api':
            payload['models'][1].pop('loaded_instances')
        else:
            payload['models'][1].pop('status')
    elif mode == 'capped':
        payload['models'] *= 129
    if kind == 'api' and instance_mode:
        item = payload['models'][0]
        if isinstance(item, dict):
            if instance_mode in ('missing','unreadable','hidden'): item.pop('loaded_instances', None)
            elif instance_mode == 'malformed': item['loaded_instances'] = [None]
            elif instance_mode == 'partial': item['loaded_instances'] = [{'id':'provider/a'}, {}]
            elif instance_mode == 'capped': item['loaded_instances'] = [{'id':f'alias-{i}'} for i in range(17)]
    rows = parse(payload, NOW)
    if mode in ('stale', 'future'):
        for row in rows:
            row['ageSeconds'] = 90 if mode == 'stale' else -90
    if kind == 'api' and instance_mode in ('stale','future'):
        for row in rows: row['ageSeconds'] = 90 if instance_mode == 'stale' else -90
    return rows


def caller_probe(mode):
    def runner(args, **kwargs):
        if mode in ('missing', 'unreadable'):
            raise FileNotFoundError(mode) if mode == 'missing' else PermissionError(mode)
        text = 'p982\ncLM Studio\nn*:1235\n' if '-sTCP:LISTEN' in args else SOCKETS
        if '-sTCP:ESTABLISHED' in args:
            if mode == 'empty': text = ''
            elif mode == 'malformed': text = 'pbad\ncCodex\nn10.99.0.176:51111->10.99.0.176:1235\n'
            elif mode in ('hidden', 'partial'): text = SOCKETS.split('\np9\n')[0] + '\n' if mode == 'partial' else 'p9\n' + SOCKETS.split('\np9\n')[1]
            elif mode == 'capped': text = 'x' * ((1 << 20) + 1)
        return subprocess.CompletedProcess(args, 0, text, '')
    rows, status = local_callers.LocalCallers(runner, executable=lambda _: OWNER[1]).sample(
        [{'host': 'mac', 'state': 'busy', 'loaded': True}], port=1235, listener=OWNER)
    if mode in ('stale', 'future'):
        status['ageSeconds'] = 90 if mode == 'stale' else -90
    return rows, status


def jobs_probe(mode):
    row = {'v': 1, 'id': 'mac-20260926-181500-aaaaaaaaaaaa', 'event': 'result',
           'unix': NOW, 'model': 'provider/a', 'status': 'success', 'elapsedSeconds': 1}
    if mode in ('stale', 'future'): row['unix'] += -900 if mode == 'stale' else 900
    open_row = {'v': 1, 'id': 'mac-20260926-181500-bbbbbbbbbbbb', 'event': 'enqueued',
                'unix': row['unix'], 'model': 'provider/b', 'timeout': 30}
    raw = json.dumps(row).encode() + b'\n' + json.dumps(open_row).encode() + b'\n'
    if mode == 'missing': raw = None
    elif mode == 'malformed': raw = b'{bad json}\n'
    elif mode == 'partial': raw += b'{bad json}\n'
    elif mode == 'capped': raw *= 2001
    with mock.patch.object(telemetry, '_read_windows_journal', return_value=(raw, mode == 'capped'),
                           side_effect=PermissionError(mode) if mode in ('hidden','unreadable') else None):
        jobs, status = telemetry._windows_jobs(NOW)
        # Stale *observation* is degraded; an old event in a fresh read is not.
        if mode == 'stale': status['observedAgeSeconds'] = 900
        return jobs, status


def pair_probe(mode):
    at = NOW + (900 if mode == 'future' else -900 if mode == 'stale' else 0)
    log = json.dumps({'level': 'info', 'time': telemetry._dt.datetime.fromtimestamp(at, telemetry._dt.timezone.utc).isoformat(),
                      'source': 'service-bridge', 'message': 'poll'}).encode()
    raw = {key: (None, 'missing', None, False) for key in pair_status.ALLOWED}
    raw[pair_status.LOG] = (log, 'ok', at, False)
    if mode in ('missing','unreadable','hidden'):
        return pair_status.read_pair_status(now=NOW, readings=('missing' if mode=='missing' else 'unreadable', {}))
    if mode == 'malformed': raw[pair_status.LOG] = (b'{bad json}', 'ok', at, False)
    if mode in ('partial','capped'): raw[pair_status.LOG] = (b'{partial}\n'+log, 'ok', at, True)
    return pair_status.read_pair_status(now=NOW, readings=('ok', raw))


def worker_probe(mode):
    cache = {'checkedMonotonic': time.monotonic(), 'probeRunning': True,
             'heartbeatUnix': NOW, 'modelsAdvertised': ['a', 'b'], 'modelCount': 2,
             'lanes': {'fast': {'up': True, 'model': 'a', 'kind': 'gpt-oss', 'slotsBusy': 0, 'slotsTotal': 2},
                       'deep': {'up': True, 'model': 'b', 'kind': 'qwen', 'slotsBusy': 0, 'slotsTotal': 1}}}
    if mode in ('missing','unreadable','hidden'): cache['heartbeatUnix'] = None
    elif mode == 'malformed': cache['heartbeatUnix'] = 'bad'
    elif mode in ('stale','future'): cache['heartbeatUnix'] += -900 if mode == 'stale' else 900
    with mock.patch.object(telemetry, '_WINDOWS_WORKER_CACHE', cache):
        worker, status = telemetry._windows_worker(NOW)
    if mode in ('partial','capped'):
        worker['complete'] = False
        worker['lanes']['deep'].pop('up')
    return worker, status


def fixture(changes):
    rows, sources = {}, []
    for kind, key in [('api', 'lmstudio-api'), ('cli', 'lms-ps')]:
        try:
            parsed = inventory(kind, changes.get(kind), changes.get('api-instances'), changes.get('control')=='empty')
            if kind == 'api': rows.update((row['id'], row) for row in parsed)
            else: telemetry._merge_cli_rows(rows, parsed)
            age = 90 if changes.get(kind) == 'stale' else -90 if changes.get(kind) == 'future' else 0
            sources.append(source(key, route='direct', ageSeconds=age))
        except (ValueError, OSError):
            sources.append(source(key, state='unavailable', ageSeconds=None, detail='Inventory unknown'))
    callers, caller_source = caller_probe('empty' if changes.get('control')=='empty' else changes.get('callers'))
    sources += [caller_source, source('pipeline-router'), source('activity'), source('router-active', observationAgeSeconds=0), source('monitor-receipts', observationAgeSeconds=0), source('router-archive', observationAgeSeconds=0), source('mac-gpu'),
                source('windows-worker'), source('mem-guard')]
    data = {'schemaVersion': 1, 'host': 'mac', 'sampledAt': NOW, 'fullSampledAt': NOW,
            'models': list(rows.values()), 'sources': sources, 'localCallers': callers, 'clients': [],
            'pipeline': {'status': 'idle', 'pipelines': [], 'pipelinesComplete': True, 'ageSeconds': 0},
            'onlineCodeMode': {'state': 'inactive', 'active': False, 'taskState': 'idle',
                               'runCounts': {'running': 0, 'queued': 0, 'unresolved': 0}},
            'activity': {'runs': [], 'complete': True, 'historyBound': {'truncated': False}},
            'windowsJobs': {'schemaVersion': 1, 'journal': 'recorded', 'complete': True, 'inFlight': [], 'recent': [], 'lastSuccess': None},
            'components': [],
            'macGpu': {'ageSeconds': 0, 'utilizationPercent': 40, 'allocatedBytes': 1 << 30},
            'memory': {'level': 'ok', 'availablePercent': 50, 'ageSeconds': 0,
                       'physicalBytes': 64 << 30, 'availableBytes': 32 << 30},
            'windowsWorker': {'state': 'advertised', 'ageSeconds': 0, 'modelsAdvertised': ['a', 'b'],
                              'lanes': {'fast': {'up': True, 'model': 'a', 'kind': 'gpt-oss', 'slotsBusy': 0, 'slotsTotal': 2},
                                        'deep': {'up': True, 'model': 'b', 'kind': 'qwen', 'slotsBusy': 0, 'slotsTotal': 1}}},
            'peers': [{'host': 'fixture-mac', 'platform': 'mac', 'fresh': True, 'ageSeconds': 0,
                       'loadedListComplete': True, 'models': [{'id': 'a', 'loadedState': 'loaded'}]}]}
    keys = {'router': 'pipeline', 'history': 'activity', 'gpu': 'macGpu', 'worker': 'windowsWorker',
            'memory': 'memory', 'peers': 'peers'}
    for name, mode in changes.items():
        if name == 'model-age':
            for row in data['models']:
                row['ageSeconds'] = {'missing': None, 'unreadable': None, 'malformed': 'bad',
                                     'partial': None, 'capped': None, 'stale': 90, 'hidden': None, 'future': -90}[mode]
        elif name == 'snapshot':
            data['sampledAt'] = data['fullSampledAt'] = NOW + (90 if mode == 'future' else -90)
        elif name in keys:
            key = keys[name]
            if name == 'peers':
                row = data[key][0]
                if mode in ('stale', 'future'):
                    row.update(ageSeconds=90 if mode == 'stale' else -90, fresh=mode == 'future')
                elif mode in ('partial', 'capped'):
                    row.update(loadedListComplete=False, modelsComplete=False)
                elif mode == 'malformed': row['models'] = [None]
                else: data[key] = None
            elif name == 'history':
                data[key] = {'runs': [], 'historyBound': {'truncated': True, 'missingFromAgeSeconds': 0}}
                if mode == 'capped':
                    # Complete source, capped *presentation*: the real activityFeed
                    # projection must carry its own bound instead of inventing 12.
                    data[key] = {'runs': [{'runId': f'run-{i}', 'status': 'running', 'activity': 'running',
                                           'host': 'mac', 'ageSeconds': 0} for i in range(14)],
                                 'historyBound': {'truncated': False}}
                data['windowsJobs']['journal'] = 'invalid'
            elif mode in ('missing', 'unreadable', 'hidden'):
                data[key] = None
            elif mode == 'malformed':
                data[key] = {'ageSeconds': 'bad'}
            elif mode in ('partial', 'capped'):
                data[key]['complete'] = False
                if name == 'router':
                    data[key]['runsTruncated'] = True
                    data['onlineCodeMode']['runsTruncated'] = True
                if name == 'worker': data[key]['lanes']['deep'].pop('up')
            else:
                data[key]['ageSeconds'] = 90 if mode == 'stale' else -90
            if name == 'router' and mode not in ('partial', 'capped'):
                data['onlineCodeMode'] = None
    data['windowsJobs'], job_source = jobs_probe(changes.get('jobs'))
    data['sources'].append(job_source)
    if 'history' in changes and changes['history'] != 'capped': data['windowsJobs']['complete'] = False
    data['pair'] = pair_probe(changes.get('pair'))
    data['sources'].append(pair_status.source_row(data['pair']))
    data['windowsWorker'], worker_source = worker_probe(changes.get('worker'))
    data['sources'] = [s for s in data['sources'] if s['id'] != 'windows-worker'] + [worker_source]
    # Exercise the real collector's merge, source disposition and route-readiness
    # composition too. Only I/O adapters and already tested parser returns are
    # injected; neither the publisher nor any web function is replaced.
    parsed = {}
    for kind in ('api', 'cli'):
        try: parsed[kind] = inventory(kind, changes.get(kind), changes.get('api-instances'), changes.get('control')=='empty')
        except (ValueError, OSError) as error: parsed[kind] = error
    def parse(kind):
        def read(*_args):
            if isinstance(parsed[kind], Exception): raise parsed[kind]
            return copy.deepcopy(parsed[kind])
        return read
    adapters = {'_lms_server_status': {'port': 1235, 'running': True},
                '_inventory_listener': (982, 'lmstudio', OWNER[1]), '_bounded_http': {'models': []} if changes.get('control')=='empty' else API,
                '_bounded_lms': {'models': []} if changes.get('control')=='empty' else CLI, '_pair_snapshot': data['pair'],
                '_afm_status': {'state': 'unknown'}, 'observe_router': {},
                '_pipeline': (data['pipeline'] or {'status': 'unknown'}, source('pipeline-router')),
                '_online_code_mode': data['onlineCodeMode'] or {},
                '_windows_worker': (data['windowsWorker'] or {'state': 'unknown'}, source('windows-worker')),
                '_windows_headless': {'state': 'unknown'},
                '_windows_jobs': (data['windowsJobs'], job_source), '_canary': ({}, source('canary', state='unavailable')),
                'collect_nisi_v02': ({'runtimeIntegrity':'VERIFIED','hostBinding':'VERIFIED'}, source('nisi-v02-runtime', state='unavailable')),
                '_last_jev_judgment': (None, None)}
    with ExitStack() as stack:
        for name, value in adapters.items(): stack.enter_context(mock.patch.object(telemetry, name, return_value=value))
        stack.enter_context(mock.patch.object(telemetry, '_parse_api', side_effect=parse('api')))
        stack.enter_context(mock.patch.object(telemetry, '_parse_lms', side_effect=parse('cli')))
        stack.enter_context(mock.patch.object(telemetry.time, 'time', return_value=NOW))
        stack.enter_context(mock.patch.object(telemetry, '_JEV_ENV_PATH', Path(__file__).parent / '.fixture-absent-jev'))
        stack.enter_context(mock.patch.object(peer_feed, 'windows_feed', return_value=(None, source('windows-telemetry',state='unavailable'))))
        collected = telemetry.collect_snapshot()
    # Keep injected temporal/source degradations, but require the collector's
    # models and readiness result rather than a parallel reconstruction.
    if 'model-age' not in changes:
        data['models'] = collected['models']
    data['components'] = collected['components']
    for status in collected['sources']:
        if status['id'] in ('lms-ps', 'lmstudio-api'):
            current = next(s for s in data['sources'] if s['id'] == status['id'])
            current.update({key: value for key, value in status.items() if key != 'ageSeconds'})
    return data


def cases():
    yield {}
    yield {'control': 'empty'}
    for name, mode in itertools.product(SOURCES, MODES): yield {name: mode}
    for left, right in itertools.combinations(SOURCES, 2):
        for one, two in itertools.product(MODES, repeat=2): yield {left: one, right: two}


def generate():
    for changes in cases():
        label = '+'.join(f'{k}:{v}' for k, v in changes.items()) or 'healthy'
        errors = []
        try:
            store = server.SnapshotStore()
            store.publish(fixture(changes))
            data = store.read()
            try: presence = peer_feed.mac_presence(data, 'fixture-mac')
            except peer_feed.InventoryUnknown: presence = None
        except Exception as error:
            errors.append(f'publisher raised {type(error).__name__}: {error}')
            data, presence = None, None
        yield {'label': label, 'changes': changes, 'snapshot': data, 'presence': presence, 'errors': errors}

    # Preserve every old-event/fresh-read input from the original generator,
    # with the converse oracle. The original stale-observation assertions stay.
    for changes in cases():
        if changes.get('jobs') != 'stale': continue
        data = fixture(changes)
        next(s for s in data['sources'] if s['id'] == 'windows-jobs')['observedAgeSeconds'] = 0
        store = server.SnapshotStore(); store.publish(data)
        other = {k:v for k,v in changes.items() if k != 'jobs'}
        incomplete = other.get('router') in ('partial', 'capped', 'stale', 'future')
        exact = {'coverageIncomplete': True} if incomplete else {} if 'snapshot' in other or 'history' in other else {'activityExact': {'In flight':'0','Answered':'1','Failed':'0','Routes':'0'}}
        yield {'label':'event-history-control:'+json.dumps(changes,sort_keys=True), 'dimension':'event-age-is-history',
               'property':'no-false-doubt','input':changes,'changes':other,'snapshot':store.read(),
               'errors':[],'expect':exact,'presence':None}

    from test_support.pipeline_dimensions import dimensions
    from test_support.two_sided_dimensions import dimensions as two_sided
    for item in itertools.chain(dimensions(fixture({}), NOW), two_sided(fixture({}), NOW)):
        store = server.SnapshotStore()
        store.publish(item['snapshot'])
        item['snapshot'] = store.read()
        item['presence'] = None
        yield item


def check():
    result = subprocess.run(['node', str(Path(__file__).parent / 'test_support/render-invariants.mjs')],
                            input=json.dumps(list(generate()), allow_nan=False), text=True, capture_output=True,
                            cwd=Path(__file__).parent, timeout=180)
    if result.returncode not in (0, 1): raise RuntimeError(result.stderr)
    report = json.loads(result.stdout)
    return report


class PipelineInvariantTests(unittest.TestCase):
    def test_single_and_pair_degradations(self):
        report = check()
        self.assertEqual(report['baseCases'], 5938)
        self.assertEqual(report['cases'], 6721)
        self.assertEqual(report['violations'], [], json.dumps(report['violations'][:15], indent=2))


if __name__ == '__main__':
    if '--check' in sys.argv:
        report = check()
        print(json.dumps(report, indent=2))
        sys.exit(bool(report['violations']))
    else: unittest.main()
