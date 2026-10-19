"""Offline replays of the expressly excluded Sol findings; never contact a service."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import activity
import server
import telemetry
from windows.agiw_win import probes, collect

NOW = 1_800_000_000.0
results = []
def record(key, reproduces, evidence):
    results.append(dict(finding=key, reproduces=bool(reproduces), evidence=evidence))
def source(sid, **extra):
    return dict(id=sid, state='live', ageSeconds=0, observationAgeSeconds=0, **extra)
def snapshot(pipeline, **extra):
    return dict(host='mac', schemaVersion=1, sampledAt=NOW, models=[], sources=[source('pipeline-router')],
                pipeline=pipeline, onlineCodeMode=dict(state='ready', active=False, taskState='idle'), **extra)

with tempfile.TemporaryDirectory(prefix='integration-findings-') as tmp:
    root = Path(tmp)
    queue = root/'queue'; queue.mkdir()
    pipe, src = probes.route_queue(queue, NOW)
    record('sol10-A1#2', pipe['status']=='idle', dict(pipeline=pipe, source=src['state']))

    runs = root/'runs'; runs.mkdir()
    (runs/'20261018T000000-run').symlink_to(root, target_is_directory=True)
    jobs, src = probes.codemode_jobs(runs, [], NOW)
    record('sol10-A1#3', jobs['complete'] and not jobs['recent'], dict(complete=jobs['complete'], source=src['state']))
    (runs/'20261018T000000-run').unlink()
    run = runs/'20261018T000000-run';run.mkdir()
    manifest=run/'manifest.json'
    manifest.write_text(json.dumps(dict(metrics=[dict(kind='local', model='m', stage='draft', tok=1, sec=1, finish='banana')])))
    os.utime(manifest, (NOW, NOW))
    jobs, src = probes.codemode_jobs(runs, [], NOW)
    record('sol10-A1#4', jobs['complete'] and any(r['state']=='error' for r in jobs['recent']), dict(complete=jobs['complete'], states=[r['state'] for r in jobs['recent']]))
    try:
        probes.loads_strict('['*100000+']'*100000)
        rejected=False
    except ValueError:
        rejected=True
    record('sol10-A1#5', not rejected, dict(explicit_depth_rejected=rejected))

    receipt=dict(runId='receipt', source='monitor-receipt', ageSeconds=1, recordedAt=NOW-1, status='RESPONSE_VALIDATED')
    active=dict(runId='live', source='router-active', ageSeconds=1, activity='running', layout='note', status='unresolved')
    with mock.patch.object(activity,'_receipt_files',return_value=activity._Listing([root/'receipt.json'])), \
         mock.patch.object(activity,'_archive_files',return_value=activity._Listing()), \
         mock.patch.object(activity,'_receipt_runs',return_value=[receipt]), \
         mock.patch.object(activity,'_active_rows',return_value=([active],False,'ok')):
        data=activity.collect_activity(NOW)
    archive=next(s for s in data['sources'] if s['id']=='router-archive')
    record('sol10-A1#6', archive['state']=='recorded', archive)

    idle=snapshot(dict(status='idle', ageSeconds=None, pipelines=[], pipelinesComplete=True))
    store=server.SnapshotStore();store.publish(idle);result=store.read()
    record('sol10-A4#2', result['pipeline']['status']=='unknown', dict(pipeline=result['pipeline'], onlineCodeMode=result['onlineCodeMode']))
    queued=snapshot(dict(status='queued', runId='verified', runsTruncated=True, pipelinesComplete=False,
                         pipelines=[dict(runId='verified', live=True, status='queued')]))
    queued['onlineCodeMode'].update(state='processing', active=True, taskState='queued', runsTruncated=True)
    store.publish(queued);result=store.read()
    record('sol10-A4#3', result['pipeline']['status']=='unknown' and result['onlineCodeMode']['active'] is None,
           dict(pipeline=result['pipeline'],onlineCodeMode=result['onlineCodeMode']))

    path=root/'jobs.jsonl'
    enqueue=dict(v=1,event='enqueued',id='mac-20260925-080000-'+'a'*32,unix=NOW-20,timeout=600,pid=99)
    done=dict(v=1,event='result',id='mac-20260925-080000-'+'b'*32,unix=NOW-1,status='success',model='model',elapsedSeconds=1,padding='x'*180)
    path.write_text(json.dumps(enqueue)+'\n'+(json.dumps(done)+'\n')*1600);path.chmod(0o600)
    with mock.patch.object(telemetry,'_WINDOWS_JOBS_PATH',path),mock.patch.object(telemetry,'_pid_alive',return_value=True):
        jobs,src=telemetry._windows_jobs(NOW)
    record('sol11-A3#1',jobs['complete'] and jobs.get('liveAvailable') and not jobs['inFlight'],
           dict(bytes=path.stat().st_size,complete=jobs['complete'],liveAvailable=jobs.get('liveAvailable'),journal=jobs['journal']))
    gpu=[dict(index=0,name='fixture',utilizationPercent=90)]
    hands=probes.hands_off(gpu,gpu_live=True,lane_pids=None,notification_state=5)
    published=probes.enforce_snapshot_evidence(dict(handsOff=hands,pcGpus=gpu,sources=[source('pc-gpu'),source('hands-off'),dict(id='lane-processes',state='error')]))
    record('sol11-A3#2',hands['active'] is True and published['handsOff']['active'] is None,
           dict(before=hands['active'],after=published['handsOff']['active']))
    paths=activity._Listing();paths.read_failed=True
    bound=activity._history_bound((paths,),[],NOW)
    record('sol11-A3#5-helper',bound['truncated'] is False,bound)
    with mock.patch.object(activity,'_receipt_files',return_value=activity._Listing()), \
         mock.patch.object(activity,'_archive_files',return_value=activity._Listing([root/'bad.json'])), \
         mock.patch.object(activity,'_read_archive',side_effect=ValueError('malformed fixture')), \
         mock.patch.object(activity,'_active_rows',return_value=([],False,'ok')):
        aggregate=activity.collect_activity(NOW)
    record('sol11-A3#5-publication',not aggregate['historyBound']['truncated'],aggregate['historyBound'])

# A3 deliberately removed the LAN probe from this passive observer. Its linked-share
# projection cannot retain the old independent failure; no LAN request is made here.
peer=dict(host='mac',platform='macos',source='agiw-peers',fresh=True,ageSeconds=2.4,modelsComplete=True,
          models=[dict(id='reported-model',loadedState='loaded')])
linked=collect.mac_from_presence([peer], 'mac')
record('A4-fix10-Windows-LAN-failure',linked.get('lanFailure')!='refused',
       dict(state=linked.get('state'),lanFailure=linked.get('lanFailure'),
            legacyProbeAvailable=hasattr(probes,'MacPeer'),scope='passive projection; original retained HTTP test is socket-blocked, and its legacy probes.MacPeer API is absent'))
print(json.dumps(results,indent=2))
