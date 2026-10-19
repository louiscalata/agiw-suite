"""Boundary dimensions added in pass 9; all acquisition is local and substituted.

Readers, projections, the Windows collector and the dashboard are production code.
Expected bounds below come from generated inputs, never from output flags.
"""
import contextlib
import copy
import errno
import itertools
import json
import os
from pathlib import Path
import subprocess
import stat
import tempfile
from types import SimpleNamespace
from unittest import mock

import activity
import local_callers
import telemetry
from invariant_pipeline import NOW, collect, iso, jobs
from windows.agiw_win import collect as win_collect, probes as win


def base():
    s = collect()
    s['sources'] = [source for source in s['sources'] if source['id'] not in ('windows-jobs','pipeline-router')] + [
        {'id': sid, 'state': 'live', 'ageSeconds': 0, 'observationAgeSeconds': 0} for sid in ('windows-jobs','pipeline-router','router-active','monitor-receipts','router-archive')]
    s['activity'] = {'runs': [], 'complete': True, 'historyBound': {'truncated': False}}
    s['windowsJobs'] = {'schemaVersion': 1, 'journal': 'recorded', 'complete': True, 'unsettled': 0,
                        'inFlight': [], 'recent': [], 'lastSuccess': None}
    s['pipeline'] = {'status': 'idle', 'pipelines': [], 'pipelinesComplete': True}
    return s


def case(dimension, inputs, snapshot, expected, **extra):
    return dict(dimension=dimension, input=inputs, snapshot=snapshot, expected=expected, now=NOW, **extra)


def projection(root, running, queued, age, *, verified=False):
    rows = []
    for index in range(running + queued):
        row = telemetry._router_row(f'route-{index}', 'note', None, readable=True, per_run=False)
        row.update(live=True, state='running' if index < running else 'waiting',
                   lockHeld=verified, lockBound=verified,
                   startedUnix=NOW-age, recordedUnix=NOW-age, stage='started',
                   note={'sinceUnix': NOW-age, 'untilUnix': NOW+60, 'resource': 'mac-heavy',
                         'phase': 'running' if index < running else 'waiting', 'admission': 'shared'},
                   client='claude', host='mac')
        rows.append(row)
    obs = dict(rows=rows, primary=rows[0] if rows else None, error=None, truncated=False,
               lanes={}, admission=None, install={'inProgress': False}, collisions=[])
    with mock.patch.object(telemetry, '_PENDING_PATH', root/'no-marker'), \
         mock.patch.object(telemetry, '_router_namespace_initialized', return_value=True):
        pipe, source = telemetry._pipeline(NOW, obs)
        counts = telemetry._router_projection(obs, NOW)
    return pipe, counts


def active_projection(root, count, state, history, *, age=1):
    folder = root / f'active-{count}-{state}-{history}-{age}'
    folder.mkdir(mode=0o700); (folder/'archive').mkdir(mode=0o700); (folder/'receipts').mkdir(mode=0o700)
    live = {(f'live-{i}', 'note'): {'runId': f'live-{i}', 'layout': 'note', 'live': True,
             'state': state, 'note': {'sinceUnix': NOW-age}, 'client': 'claude', 'host': 'mac'}
            for i in range(count)}
    if history:
        file = folder/'archive'/'done.json'
        file.write_text(json.dumps({'runId': 'done', 'finishedUnix': NOW-2,
            'result': {'kind': 'codemode.router.v1', 'runId': 'done', 'status': 'RESPONSE_VALIDATED'}}))
        file.chmod(0o600)
    with mock.patch.object(activity, '_ROOT', folder), \
         mock.patch.object(activity, '_RECEIPTS', folder/'receipts'), \
         mock.patch.object(activity, '_read_actives', return_value=([], False, 'ok')), \
         mock.patch.object(activity, '_live_rows', return_value=live):
        return activity.collect_activity(NOW)


def windows_snapshot(root, mode, queue_mode):
    folder = root/f'win-{mode}-{queue_mode}'; folder.mkdir()
    runs = folder/'runs'; queue = folder/'bin'/'online-code-route-queue'
    if mode != 'missing': runs.mkdir()
    if queue_mode != 'missing':
        for state in ('pending', 'claimed', 'completed'):
            (queue/state).mkdir(parents=True)
    manifest = {'run_id': 'fixture', 'metrics': [{'kind': 'local', 'stage': 'draft',
        'endpoint': 'http://127.0.0.1:1234', 'model': 'fixture/model', 'tok': 1, 'sec': 1, 'finish': 'stop'}]}
    number = {'partial': 2, 'bad': 1, 'cap40': 41, 'run-bound': 20, 'recent-cap': 6, 'good': 1, 'metrics8': 1, 'invalid-metric': 1, 'metricscap': 1,
              'missingmanifest': 1, 'future': 1, 'scan-cap': 4}.get(mode, 0)
    for i in range(number):
        run = runs/f'20261018-120000-{i:03}'; run.mkdir()
        if mode == 'missingmanifest': continue
        doc = copy.deepcopy(manifest); doc['run_id'] = f'fixture-{i}'
        if mode in ('metricscap', 'metrics8', 'recent-cap'):
            doc['metrics'] = [{**doc['metrics'][0], 'stage': f'stage{j}'} for j in range(9 if mode == 'metricscap' else 8)]
        if mode == 'invalid-metric': doc['metrics'].append(None)
        file = run/'manifest.json'
        file.write_text('{bad' if mode == 'bad' or mode == 'partial' and i == 0 else json.dumps(doc))
        os.utime(file, (NOW+60 if mode == 'future' else NOW-1,)*2)
    if queue_mode in ('capped', 'one'):
        for i in range(4 if queue_mode == 'capped' else 1): (queue/'pending'/f'{i}.json').write_text('{}')
    original_scandir = win.os.scandir
    def scan(path):
        if mode == 'unreadable' and Path(path) == runs:
            raise PermissionError(errno.EACCES, 'fixture')
        return original_scandir(path)
    with contextlib.ExitStack() as stack:
        for name, value in {'lane_specs': ([], None), 'read_peers': ([], None),
                            'configured_mac_name': None, 'share_health': ({'state': 'unknown'}, None),
                            'nisi_component': {'id': 'nisi', 'state': 'unknown'}}.items():
            stack.enter_context(mock.patch.object(win, name, return_value=value))
        stack.enter_context(mock.patch.object(win.os, 'scandir', side_effect=scan))
        if mode == 'scan-cap':
            stack.enter_context(mock.patch.object(win, 'QUEUE_MAX_ENTRIES', 3))
        if mode == 'guard': stack.enter_context(mock.patch.object(win, 'codemode_jobs', side_effect=ValueError('fixture')))
        if queue_mode == 'guard': stack.enter_context(mock.patch.object(win, 'route_queue', side_effect=ValueError('fixture')))
        if queue_mode == 'capped':
            read_queue = win.route_queue
            def capped_queue(*args):
                with mock.patch.object(win, 'QUEUE_MAX_ENTRIES', 3):
                    return read_queue(*args)
            stack.enter_context(mock.patch.object(win, 'route_queue', side_effect=capped_queue))
        collector = win_collect.Collector(share_root=folder, bin_root=folder/'bin', runs_root=runs,
            gpu_fn=lambda: ([], None), adapters_fn=lambda: [], memory_fn=lambda: ({'level': 'unknown'}, None),
            clock=lambda: NOW, host='fixture-pc')
        try: return collector.collect()
        finally: collector.close()


def generate():
    template = base()
    # Date's representable range is narrower than finite IEEE-754 numbers.
    for capture, frozen in itertools.product((NOW, None, 1e20, -1e20, 8.64e12, -8.64e12, 8.64e12+.001), ('paused', 'disconnected')):
        s = copy.deepcopy(template)
        s['sampledAt'] = s['fullSampledAt'] = capture
        yield case('capture-range', dict(capture=capture, frozen=frozen), s,
                   dict(representable=capture is not None and abs(capture) <= 8.64e12))
    for kind, neighbor in itertools.product(('empty', 'directory', 'file', 'symlink', 'linked-root', 'reparse-entry', 'reparse-root'), (False, True)):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder)
            root = parent / 'runs'; root.mkdir()
            def manifest(run):
                run.mkdir()
                path = run / 'manifest.json'
                path.write_text(json.dumps(dict(metrics=[dict(kind='local', stage='draft', model='fixture/model', tok=1, sec=1, finish='stop')])))
                os.utime(path, (NOW, NOW))
            if neighbor: manifest(root / '20261018T000000-valid')
            entry = root / '20261018T000000-run'
            if kind == 'directory': manifest(entry)
            elif kind == 'reparse-entry': manifest(entry)
            elif kind == 'file': entry.write_text('run-shaped non-directory')
            elif kind == 'symlink': entry.symlink_to(parent, target_is_directory=True)
            elif kind == 'linked-root':
                link = parent / 'link'; link.symlink_to(root, target_is_directory=True); root = link
            real_lstat = os.lstat
            def observed_lstat(path, *args, **kwargs):
                if Path(path) == (entry if kind == 'reparse-entry' else root if kind == 'reparse-root' else None):
                    return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
                return real_lstat(path, *args, **kwargs)
            with mock.patch.object(win.os, 'lstat', side_effect=observed_lstat):
                jobs, source = win.codemode_jobs(root, [], NOW)
            s = copy.deepcopy(template); s['windowsJobs'] = jobs
            yield case('run-entry-kind', dict(kind=kind, neighbor=neighbor), s,
                       dict(complete=kind in ('empty', 'directory'), retained=0 if kind in ('linked-root', 'reparse-root') else int(neighbor)+int(kind == 'directory')), source=source)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        for running, queued, age in itertools.product((0, 1, 4, 5, 8, 9), (0, 1, 4, 5, 8, 9), (0, 300, 301)):
            s = copy.deepcopy(template)
            s['pipeline'], counts = projection(root, running, queued, age)
            yield case('combined-projection', dict(running=running, queued=queued, age=age), s,
                dict(complete=running+queued <= 8 and (age <= 300 or running+queued == 0), count=running+queued,
                     countsComplete=running <= 8 and queued <= 8 and (age <= 300 or running+queued == 0)), counts=counts)
        for count, state, history in itertools.product((0, 1, 11, 12, 13, 17), ('running', 'waiting'), (False, True)):
            s = copy.deepcopy(template); act = active_projection(root, count, state, history)
            s['activity'] = {k: v for k, v in act.items() if k != 'sources'}
            yield case('active-bound', dict(count=count, state=state, history=history), s,
                       dict(complete=count+history <= 12, count=count+history), sources=act['sources'])
        for complete, count, lane_set, age, slots, identity, snapshot_age in itertools.product((True, False, None), (0, 1, 3),
                (('fast',), ('deep',), ('fast', 'deep')), (-1, 0, 3, 90, 90.01, 301), ('known', 'partial'), ('unique', 'duplicate'), (0, 1, 3)):
            s = copy.deepcopy(template)
            s['sampledAt'] = s['fullSampledAt'] = NOW-snapshot_age
            s['windowsWorker'] = {'state': 'advertised', 'ageSeconds': 0, 'lanes': {
                lane: {'up': True, 'model': 'fixture/model', 'slotsBusy': 0 if slots == 'known' else None,
                       'slotsTotal': 2 if slots == 'known' else None} for lane in lane_set}}
            j = s['windowsJobs']
            j['recent'] = [{'id': f'done-{lane}', 'client': 'claude', 'lane': lane, 'state': 'success',
                            'ageSeconds': 1, 'predictedPerSecond': 20} for lane in lane_set]
            if complete is None: j.pop('complete')
            else: j['complete'] = complete
            j['inFlight'] = [{'id': f"job-{i if identity == 'unique' else 0}", 'model': f'fixture/model-{i}', 'client': 'claude',
                'lane': lane_set[i % len(lane_set)], 'ageSeconds': 301 if identity == 'duplicate' and i % 2 else age, 'timeoutSeconds': 60} for i in range(count)]
            yield case('lane-journal', dict(complete=complete, count=count, lanes=lane_set, age=age, slots=slots, identity=identity, snapshotAge=snapshot_age), s,
                       dict(known=complete is True, live=complete is True and count > 0 and 0 <= age and age+snapshot_age <= 90,
                            liveLanes=sorted({r['lane'] for r in j['inFlight'] if complete is True and 0 <= r['ageSeconds'] and r['ageSeconds']+snapshot_age <= 90})))
        for status, order, age, lane in itertools.product(('bogus', '', None, 12, 'SUCCESS'),
                ('before', 'after'), (1, 5, 31), ('fast', 'deep')):
            s = copy.deepcopy(template)
            record = {'v': 1, 'event': 'enqueued', 'id': 'mac-20260925-080000-'+'a'*32,
                'unix': NOW-age, 'model': 'fixture/model', 'client': 'claude', 'lane': {'fast': 'amd', 'deep': 'bionic'}[lane], 'timeout': 60, 'pid': 99}
            result = {**record, 'event': 'result', 'status': status}
            records = [result, record] if order == 'before' else [record, result]
            with mock.patch.object(telemetry, '_read_windows_journal', return_value=b'\n'.join(json.dumps(r).encode() for r in records)+b'\n'), \
                 mock.patch.object(telemetry, '_pid_alive', return_value=True):
                s['windowsJobs'], source = telemetry._windows_jobs(NOW)
            yield case('invalid-result', dict(status=status, order=order, age=age, lane=lane), s, dict(complete=False), source=source)
        for mode, queue_mode in itertools.product(('empty', 'missing', 'unreadable', 'guard', 'bad', 'partial',
                'cap40', 'metricscap', 'scan-cap', 'missingmanifest', 'future', 'good', 'run-bound', 'recent-cap', 'metrics8', 'invalid-metric'), ('empty', 'missing', 'capped', 'guard', 'one')):
            s = windows_snapshot(root, mode, queue_mode)
            yield case('windows-producer', dict(mode=mode, queue=queue_mode), s,
                       dict(jobsComplete=mode in ('empty', 'good', 'run-bound', 'metrics8'), pipelineComplete=queue_mode == 'empty',
                            complete=mode in ('empty', 'good', 'run-bound', 'metrics8') and queue_mode == 'empty'))
        for flags, retained in itertools.product(itertools.product((True, False, None, 'true', 1), repeat=3), (False, True)):
            s = copy.deepcopy(template)
            for (block, key), flag in zip((('activity', 'complete'), ('windowsJobs', 'complete'), ('pipeline', 'pipelinesComplete')), flags):
                if flag is None: s[block].pop(key)
                else: s[block][key] = flag
            if retained:
                s['windowsJobs']['recent'] = [{'id': 'answer', 'state': 'success', 'ageSeconds': 1, 'client': 'claude', 'model': 'fixture/model'}]
            yield case('completeness-envelope', dict(flags=flags, retained=retained), s,
                       dict(complete=all(flag is True for flag in flags)))
        malformed = ('pBAD\ncClient\nnconnection\n', 'p\ncClient\nnconnection\n', 'p0\ncClient\nnconnection\n',
            'p-1\ncClient\nnconnection\n', 'p1.5\ncClient\nnconnection\n', 'p2147483648\ncClient\nnconnection\n',
            'p12x\ncClient\nnconnection\n', 'cBefore\nnconnection\n', 'nBefore\n', 'p9\n')
        for bad, target, adjacent, order in itertools.product(malformed, ('LISTEN', 'ESTABLISHED'), (False, True), ('before', 'after')):
            s = copy.deepcopy(template)
            def runner(args, **kwargs):
                listener = '-sTCP:LISTEN' in args
                output = 'p982\ncServer\nn127.0.0.1:1234\n' if listener else 'p982\ncServer\nn127.0.0.1:1234->127.0.0.1:50000\np9\ncClient\nn127.0.0.1:50000->127.0.0.1:1234\n'
                if '-sTCP:'+target in args:
                    output = (bad+output if order == 'before' else output+bad) if adjacent else bad
                return subprocess.CompletedProcess(args, 0, output, '')
            s['localCallers'], source = local_callers.LocalCallers(runner, executable=lambda pid: '/fixture/LM Studio').sample(s['models'], port=1234, listener=(982, '/fixture/LM Studio'), activity_known=True)
            yield case('caller-record', dict(bad=bad, target=target, adjacent=adjacent, order=order), s,
                       dict(known=False), source=source)


if __name__ == '__main__':
    for row in generate(): print(json.dumps(row, allow_nan=False, separators=(',', ':')))
