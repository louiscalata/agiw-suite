"""Offline, input-derived oracles for certainty, exact healthy values and frozen views.

Observation clocks and event clocks are independent dimensions. All readers are
production functions; only acquisition (process/network/installed paths) is mocked.
"""
import contextlib
import copy
import itertools
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

import activity
import agiw_status
import machine_state
import peer_feed
import server
import telemetry
from invariant_pipeline import NOW, API, CLI, collect, peer, callers, iso
from invariant_dimensions import base, projection, active_projection
from windows.agiw_win import probes as win
from windows.agiw_win.collect import Collector
from windows.agiw_win.server import SnapshotStore as WindowsStore


def emit(dimension, inputs, snapshot, expected, **extra):
    if extra.get('source', {}).get('id') == 'codemode-runs':
        snapshot['sources'] = [r for r in snapshot['sources'] if r['id'] not in ('windows-jobs','codemode-runs')] + [extra['source']]
    return dict(dimension=dimension, input=inputs, snapshot=snapshot, expected=expected, now=NOW, **extra)


def archive(root, age=1):
    file = root/'archive'/'done.json'
    file.write_text(json.dumps({'runId': 'done', 'finishedUnix': NOW-age,
        'result': {'kind': 'codemode.router.v1', 'runId': 'done', 'status': 'RESPONSE_VALIDATED'}}))
    file.chmod(0o600)
    os.utime(file, (NOW-1,)*2)  # file observation is independent of the event


def read_activity(root):
    with mock.patch.object(activity, '_ROOT', root), mock.patch.object(activity, '_RECEIPTS', root/'receipts'), \
         mock.patch.object(activity, '_active_rows', return_value=([], False, 'ok')):
        return activity.collect_activity(NOW)


METRIC = {'kind': 'local', 'stage': 'draft', 'endpoint': 'http://127.0.0.1:1234',
          'model': 'fixture/model', 'tok': 40, 'sec': 2, 'finish': 'stop'}


def manifest(root, metrics, age=1):
    run = root/'20261018-120000'; run.mkdir()
    file = run/'manifest.json'; file.write_text(json.dumps(metrics)); os.utime(file, (NOW-age,)*2)
    return win.codemode_jobs(root, [{'id': 'fast', 'port': 1234}], NOW)


def journal(count=1, age=1, success=False):
    records = []
    for i in range(count):
        record = {'v': 1, 'event': 'enqueued', 'id': f'mac-20260925-080000-{i:032x}',
                  'unix': NOW-age-1 if success else NOW-age, 'model': 'fixture/model',
                  'timeout': 60, 'pid': 99, 'client': 'claude', 'lane': 'amd'}
        records.append(record)
        if success:
            records.append({**record, 'event': 'result', 'unix': NOW-age, 'status': 'success',
                'elapsedSeconds': 2, 'completionTokens': 40, 'predictedPerSecond': 20, 'promptPerSecond': 100})
    with mock.patch.object(telemetry, '_read_windows_journal', return_value=b''.join(json.dumps(r).encode()+b'\n' for r in records)), \
         mock.patch.object(telemetry, '_pid_alive', return_value=True):
        return telemetry._windows_jobs(NOW)


def healthy(root, variation='baseline', value=0):
    api, cli = copy.deepcopy(API), copy.deepcopy(CLI)
    cli['models'][0].update(queued=0, parallel=2, contextLength=4096, sizeBytes=1_000_000_000)
    if variation == 'one-more-model':
        api['models'].append({'key': 'fixture/second', 'loaded_instances': [{'id': 'fixture/second'}]})
        cli['models'].append({'identifier': 'fixture/second', 'status': 'idle', 'queued': 0})
    if variation.startswith('alias'):
        api['models'][0]['loaded_instances'][0]['id'] = 'fixture-alias'
        cli['models'][0]['identifier'] = 'fixture-alias'
        cli['models'][0]['queued'] = value
    if variation == 'idle-lane': cli['models'][0]['status'] = 'idle'
    s = collect(api=api, cli=cli, peers=[peer(p) for p in ('windows', 'linux')])
    s['sources'] = [r for r in s['sources'] if r['id'] not in ('pipeline-router','windows-worker','router-active','monitor-receipts','router-archive')] + [
        {'id':sid, 'state':'live', 'ageSeconds':0, 'observationAgeSeconds':0} for sid in ('pipeline-router','windows-worker','router-active','monitor-receipts','router-archive')]
    s['pipeline'] = {'status': 'idle', 'pipelines': [], 'pipelinesComplete': True}
    s['activity'] = {'runs': [], 'complete': True, 'historyBound': {'truncated': False}}
    success = variation in ('recent-success', 'success-boundary')
    job_age = value if success or variation == 'job-boundary' else 1
    count = 2 if variation == 'one-more-job' else 0 if variation == 'idle-lane' else 1
    s['windowsJobs'], job_source = journal(count, job_age, success)
    s['localCallers'], caller_source = callers()
    s['sources'] = [r for r in s['sources'] if r['id'] not in ('windows-jobs', 'local-callers')] + [job_source, caller_source]
    s['windowsWorker'] = {'state': 'advertised', 'ageSeconds': 0, 'modelsAdvertised': ['fixture/model'],
        'headless': {'state': 'off'}, 'lanes': {'fast': {'up': True, 'model': 'fixture/model',
        'slotsBusy': 0 if variation == 'idle-lane' or success else 1, 'slotsTotal': 2}}}
    if variation == 'archive-event-age':
        archive(root, value); s['activity'] = read_activity(root)
        s['sources'].extend(s['activity']['sources'])
    if variation == 'model-boundary':
        for row in s['models']:
            if row['host'] == 'mac': row['ageSeconds'] = value
        for row in s['sources']:
            if row['id'] in ('lmstudio-api', 'lms-ps'): row['ageSeconds'] = value
        s['machines'][0] = machine_state.local_summary(s)
    if variation == 'peer-boundary':
        for row in s['machines'][1:]: row.update(observedAt=iso(NOW-value), ageSeconds=value)
        s['windowsLive'].update(observedAt=iso(NOW-value), ageSeconds=value)
    if variation == 'heartbeat-boundary': s['windowsWorker']['ageSeconds'] = value
    read_now = NOW+value if variation == 'feed-boundary' else NOW
    expected = dict(loaded=2 if variation == 'one-more-model' else 1,
                    busy=0 if variation == 'idle-lane' else 1, queued=value if variation.startswith('alias') else 0,
                    open=0 if success else count, answered=count if success else 0, failed=0,
                    routes=1 if variation == 'archive-event-age' else 0, currentSuccess=success,
                    slotsBusy=s['windowsWorker']['lanes']['fast']['slotsBusy'],
                    models=['fixture/model', 'fixture/second'] if variation == 'one-more-model' else ['fixture/model'])
    store = server.SnapshotStore()
    with mock.patch.object(server.time, 'time', return_value=NOW):
        store.publish(s)
        s = store.read()
    return s, expected, read_now


def windows_healthy(root, count, busy, lane_age, cache_age):
    """Real lane parser, collector caches and publisher with local input fixtures."""
    specs=[{'id':('fast','deep')[i], 'port':1234+i, 'alias':f'fixture/model-{i}', 'device':'CUDA0',
            'parallel':2, 'context':32768} for i in range(count)]
    runs=root/'runs'; runs.mkdir()
    manifest(runs, {'metrics':[METRIC]}, age=600)
    for state in ('pending','claimed','completed'):(root/'bin'/'online-code-route-queue'/state).mkdir(parents=True)
    def lane(spec, now):
        def fetch(url, *args):
            if url.endswith('/health'):return {'status':'ok'}
            if url.endswith('/v1/models'):return {'data':[{'id':spec['alias']}]}
            return [{'id':i,'is_processing':busy and i==0,'n_ctx':16384,'next_token':[{'n_decoded':12}]} for i in range(2)]
        return {**win.probe_lane(spec,fetch), 'ageSeconds':lane_age, 'liveTokensPerSecond':55 if busy else None}
    with contextlib.ExitStack() as stack:
        for name, value in {'lane_specs':(specs,win._source('lane-config','Lane layout','live','Complete fixture configuration')), 'read_peers':([],None), 'configured_mac_name':None,
                            'share_health':({'state':'ready'},None), 'nisi_component':{'id':'nisi','state':'ready'}}.items():
            stack.enter_context(mock.patch.object(win,name,return_value=value))
        stack.enter_context(mock.patch.object(Collector,'_lane',side_effect=lane))
        collector=Collector(share_root=root,bin_root=root/'bin',runs_root=runs,clock=lambda:NOW,host='fixture-pc',
            gpu_fn=lambda:([],None),adapters_fn=lambda:[],memory_fn=lambda:({'level':'unknown'},None))
        try:
            collector._jobs(specs,NOW-cache_age)
            s=collector.collect()
        finally:collector.close()
    store=WindowsStore();store.publish(s)
    return store.read()


def generate():
    template = base()
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder); serial = itertools.count()
        def directory():
            path = root/str(next(serial)); path.mkdir(mode=0o700)
            return path
        for state, kind, neighbor in itertools.product(('pending', 'claimed', 'completed'), ('symlink', 'directory', 'fifo'), (False, True)):
            path = directory()
            for name in ('pending', 'claimed', 'completed'): (path/name).mkdir()
            candidate = path/state/'job.json'
            if kind == 'symlink': candidate.symlink_to(path/'missing')
            elif kind == 'directory': candidate.mkdir()
            else: os.mkfifo(candidate)
            if neighbor:
                file = path/state/'valid.json'; file.write_text('{}'); os.utime(file,(NOW-1,)*2)
            s = copy.deepcopy(template); s['pipeline'], source = win.route_queue(path, NOW)
            yield emit('queue-entry', dict(state=state, kind=kind, neighbor=neighbor), s, {}, source=source)
        documents = {'missing-metrics': {}, 'empty-metrics': {'metrics': []},
                     'minimal-local': {'metrics': [{'kind': 'local', 'finish': 'stop'}]},
                     'valid': {'metrics': [METRIC]},
                     'zero-time': {'metrics': [{**METRIC,'sec':0}]},
                     'zero-tokens': {'metrics': [{**METRIC,'tok':0}]}}
        for field in ('model', 'sec', 'tok', 'stage', 'finish'):
            doc = copy.deepcopy(METRIC); doc.pop(field)
            documents['missing-'+field] = {'metrics': [doc]}
        for mode, neighbor in itertools.product(documents, (False, True)):
            doc = copy.deepcopy(documents[mode])
            if neighbor and isinstance(doc.get('metrics'), list): doc['metrics'].append({**METRIC, 'stage':'review'})
            s = copy.deepcopy(template); s['windowsJobs'], source = manifest(directory(), doc)
            yield emit('manifest-schema', dict(mode=mode, neighbor=neighbor), s,
                       dict(complete=mode in ('valid', 'empty-metrics','zero-time','zero-tokens'),
                            retained=int(mode in ('valid','zero-time','zero-tokens'))+int(neighbor and mode!='missing-metrics')), source=source)
        for target, kind, neighbor in itertools.product(('archive', 'receipts'), ('symlink', 'directory', 'fifo'), (False, True)):
            path = directory(); (path/'archive').mkdir(mode=0o700); (path/'receipts').mkdir(mode=0o700)
            candidate = path/target/'bad.json'
            if kind == 'symlink': candidate.symlink_to(path/'missing')
            elif kind == 'directory': candidate.mkdir()
            else: os.mkfifo(candidate)
            if neighbor: archive(path)
            s = copy.deepcopy(template); s['activity'] = read_activity(path)
            yield emit('history-entry', dict(target=target, kind=kind, neighbor=neighbor), s, dict(retained=int(neighbor)))
        for count, statuses, stages in itertools.product((2, 3, 8), ('success', 'mixed'), ('same', 'distinct')):
            metrics = [{**METRIC, 'stage': 'draft' if stages=='same' else f'stage{i}',
                        'finish': 'length' if statuses=='mixed' and i%2 else 'stop'} for i in range(count)]
            s = copy.deepcopy(template); s['windowsJobs'], source = manifest(directory(), {'run_id':'duplicate','metrics':metrics})
            yield emit('metric-identity', dict(count=count, statuses=statuses, stages=stages), s,
                       dict(answered=sum(m['finish']=='stop' for m in metrics), failed=sum(m['finish']!='stop' for m in metrics)), source=source)
        for complete, state, count, hands in itertools.product((True, False, None), ('up', 'unknown'), (0, 2), (False, True, None)):
            s = collect(peers=[peer(p) for p in ('windows','linux')]); row=s['machines'][1]
            row.update(state=state, reasonCode='timeout' if state=='unknown' else 'observed', modelsComplete=complete,
                       loadedCount=count if state=='up' else None, busyCount=count if state=='up' else None, handsOff=hands)
            try: cli=agiw_status._summary(copy.deepcopy(s), NOW); error=None
            except agiw_status.StatusUnavailable as exc: cli=None; error=str(exc)
            yield emit('cli-envelope', dict(complete=complete,state=state,count=count,hands=hands), s, {}, cli=cli,error=error)
        for count in (0, 1, 11, 12, 13):
            path = directory()
            for index in range((count+7)//8):
                parent = path/f'20261018-120000-{index}'; parent.mkdir()
                file = parent/'manifest.json'
                file.write_text(json.dumps({'run_id':f'run-{index}', 'metrics':[{**METRIC,'stage':f'stage{j}'} for j in range(min(8,count-index*8))]}))
                os.utime(file,(NOW-1,)*2)
            s=copy.deepcopy(template); s['windowsJobs'], source=win.codemode_jobs(path,[{'id':'fast','port':1234}],NOW)
            yield emit('activity-window',dict(count=count),s,dict(complete=count<=12),source=source)
        for count,busy,lane_age,cache_age in itertools.product((1,2),(False,True),(0,2.999),(0,9.999)):
            s=windows_healthy(directory(),count,busy,lane_age,cache_age)
            yield emit('windows-healthy',dict(count=count,busy=busy,laneAge=lane_age,cacheAge=cache_age),s,{})
            for frozen,elapsed in itertools.product(('paused','disconnected'),(0,3600)):
                yield {**emit('frozen-capture',dict(host='windows',count=count,busy=busy,laneAge=lane_age,
                    cacheAge=cache_age,frozen=frozen,elapsed=elapsed),s,{}),'now':NOW+elapsed}
        for age,success in itertools.product((0,2.999,3.001,-1,None,'0'),(False,True)):
            s=copy.deepcopy(template);s['windowsJobs'],source=journal(success=success)
            s['windowsJobs']['observationAgeSeconds']=age
            yield emit('journal-observation',dict(age=age,success=success),s,dict(current=type(age) in (int,float) and 0<=age<=3))
        for count,state,age in itertools.product((1,2,8,9),('running','waiting'),(299.999,300.001,600,86400)):
            s=copy.deepcopy(template)
            s['pipeline'],counts=projection(directory(),count if state=='running' else 0,count if state=='waiting' else 0,age,verified=True)
            yield emit('route-event-age',dict(count=count,state=state,age=age),s,dict(complete=count<=8),counts=counts)
        for count,state,age in itertools.product((1,12,13),('running','waiting'),(1,301,86400)):
            s=copy.deepcopy(template);s['activity']=active_projection(directory(),count,state,False,age=age)
            yield emit('active-event-age',dict(count=count,state=state,age=age),s,dict(complete=count<=12))
        variations = [('baseline',0),('one-more-model',0),('one-more-job',0),('alias',0),('alias-queued',3),('idle-lane',0)]
        for dimension, ages in {'recent-success':(1,299,301,600), 'success-boundary':(1799.999,),
                'archive-event-age':(1,299,301,86400), 'model-boundary':(2.999,), 'peer-boundary':(29.999,),
                'heartbeat-boundary':(59.999,), 'feed-boundary':(2.999,), 'job-boundary':(89.999,)}.items():
            variations.extend((dimension,age) for age in ages)
        for variation, value in variations:
            path=directory(); (path/'archive').mkdir(mode=0o700); (path/'receipts').mkdir(mode=0o700)
            s, expected, now = healthy(path, variation, value)
            yield {**emit('healthy-exact', dict(variation=variation,value=value), s, expected), 'now':now}
            for frozen, elapsed in itertools.product(('paused','disconnected'), (0, 3600)):
                yield {**emit('frozen-capture', dict(variation=variation,value=value,frozen=frozen,elapsed=elapsed), s, expected),
                       'now':now+elapsed}


if __name__ == '__main__':
    for row in generate(): print(json.dumps(row, allow_nan=False, separators=(',', ':')))
