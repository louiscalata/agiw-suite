"""Deterministic single/pair source degradations through the real Python publishers.

No hosts, sockets, subprocess probes or models: only fixture input, production
SnapshotStore / presence projection / JSON atomic writer, and private temp files.
The eight mutations retain healthy dependent rows deliberately: lost evidence must
invalidate them, rather than relying on every producer to remove its old payload.
"""
import copy
import itertools
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'windows'), str(ROOT)]
from agiw_win import probes, collect
from agiw_win.server import SnapshotStore
import peer_feed

NOW = 1_800_000_000.0
MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'stale', 'hidden', 'future')
WINDOWS = ('lane-config', 'hands-off', 'nvidia-pair', 'lane-inventory', 'llama-slots', 'lane-processes', 'codemode-runs', 'route-queue', 'pc-gpu', 'mem-guard', 'agiw-peers')
MAC = ('pipeline-router', 'mac-gpu', 'mem-guard', 'windows-telemetry', 'windows-worker', 'lmstudio-api', 'lms-ps', 'local-callers', 'windows-jobs', 'router-active', 'monitor-receipts', 'router-archive')


def source(sid):
    return dict(id=sid, label=sid, state='live', ageSeconds=0.0, detail='Complete fixture read')


def healthy(host, active=False):
    specs = [dict(id=lane, port=1235+i, alias=f'model-{lane}', parallel=2, context=128, device='CPU')
             for i, lane in enumerate(('fast', 'deep'))]
    lanes = []
    for spec in specs:
        def fetch(url, *_):
            if url.endswith('/health'):
                return {'status': 'ok'}
            if url.endswith('/v1/models'):
                return {'data': [{'id': spec['alias']}]}
            return [{'id': i, 'is_processing': active, 'n_ctx': 64} for i in range(2)]
        lanes.append({**probes.probe_lane(spec, fetch), 'ageSeconds': 0.0})
    rows = [probes.lane_model_row(lane, spec, NOW) for lane, spec in zip(lanes, specs)]
    for row in rows:
        row.update(host=host, queued=0)
    if host == 'mac':
        for row in rows:
            row['source'] = 'lms-ps'
    peer = dict(host='other', platform='macos', source='agiw-peers', version='1',
                fresh=True, ageSeconds=0, modelsComplete=True,
                models=[dict(id='peer-model', loadedState='loaded', activity='idle')])
    result = dict(schemaVersion=1, host=host, fixtureExpectedWorking=2 if active else 0, sampledAt=NOW, fullSampledAt=NOW,
                observedAt='2027-01-15T08:00:00Z', observerVersion='0.2.0', activityKnown=True,
                models=rows, lanes=lanes, clients=[], sources=[source(sid) for sid in (WINDOWS if host == 'windows' else MAC)],
                localCallers=[], pcGpus=[dict(index=0, name='Fixture GPU', utilizationPercent=0,
                    memoryUsedMiB=1, memoryTotalMiB=1024, temperatureC=35, powerW=10)],
                macGpu=dict(ageSeconds=0, utilizationPercent=0, allocatedBytes=1024),
                windowsLive=dict(via='share', ageSeconds=0, health='ok', modelsComplete=True, models=[dict(id='pc-model', loadedState='loaded', activity='idle')]),
                memory=dict(level='ok', availablePercent=50, totalBytes=1000, availableBytes=500, consumers=[], consumersKnown=True),
                peers=[peer], macPeer=collect.mac_from_presence([peer], 'other'), windowsWorker=dict(state='advertised', ageSeconds=0,
                    lanes={lane['id']:dict(up=True, model=lane['expectedModel'], ageSeconds=0,
                        slotsBusy=2 if active else 0, slotsTotal=2) for lane in lanes}),
                windowsJobs=dict(schemaVersion=1, journal='recorded', complete=True, unsettled=0, inFlight=[], recent=[dict(id='done', client='codex', lane='fast',
                    model='model-fast', state='success', ageSeconds=1, elapsedSeconds=1)],
                    availability=dict(state='available'), liveAvailable=host=='mac'),
                activity=dict(runs=[], complete=True, historyBound=dict(truncated=False)),
                pipeline=dict(status='idle', pipelinesComplete=True, pipelines=[], queue=dict(pending=0, claimed=0, completed=0, staleClaimed=0)),
                components=[probes.pair_component({'ok': True, 'processes': [], 'listeners': [], 'uninspected': 0})], handsOff=dict(state='clear', active=False))
    result['machines'] = probes.machine_rows(host='fixture-pc', sampled_at=NOW, lanes=lanes, hands=result['handsOff'],
        version='0.2.0', peers=[peer], mac_name='other')
    if host == 'mac':
        result.pop('machines', None)
    return result


def degrade(data, sid, mode):
    sources = data['sources']
    row = next(s for s in sources if s['id'] == sid)
    if mode == 'missing':
        sources.remove(row)
    elif mode == 'unreadable':
        row.update(state='error', detail='Permission denied')
    elif mode == 'malformed':
        row.update(state='invalid', detail='Malformed source')
    elif mode == 'partial':
        row.update(complete=False, detail='Read incomplete')
    elif mode == 'capped':
        row.update(truncated=True, detail='Scan capped')
    elif mode == 'stale':
        row.update(state='stale', ageSeconds=3600, detail='Observation expired')
    elif mode == 'hidden':
        row.update(state='unavailable', detail='Socket or permission not visible')
    elif mode == 'future':
        row.update(ageSeconds=-60, detail='Clock ahead')


def cases():
    for host, inputs in [('windows', WINDOWS), ('mac', MAC)]:
        yield host, ()
        for sid in inputs:
            for mode in MODES:
                yield host, ((sid, mode),)
        for left, right in itertools.combinations(inputs, 2):
            for modes in itertools.product(MODES, repeat=2):
                yield host, ((left, modes[0]), (right, modes[1]))


def source_cases(bases):
    for host, changes in cases():
        data = copy.deepcopy(bases[host])
        for sid, mode in changes:
            degrade(data, sid, mode)
        yield data, changes, changes


def edge_cases():
    data = healthy('mac')
    data['windowsJobs']['recent'] = [dict(id=f'job-{i}', state='success', lane='fast', client='codex', ageSeconds=i+1) for i in range(13)]
    yield data, (('activity-counts', 'capped'),), (('displayed-activity-rows', 'capped'),)
    data = healthy('mac')
    data['windowsJobs']['liveAvailable'] = False
    data['windowsJobs']['recent'] = []
    data['activity']['runs'] = [dict(runId=f'route-{i}', source='router-active', activity='running', ageSeconds=i) for i in range(13)]
    yield data, (('activity-live-counts', 'capped'),), (('displayed-running-routes', 'capped'),)
    for mode in ('residency', 'activity'):
        data = healthy('mac')
        data['windowsLive']['models'] = [dict(id='one', loadedState='loaded', activity='busy'),
            dict(id='two', loadedState='unknown' if mode == 'residency' else 'loaded', activity='unknown')]
        yield data, (('pc-report', mode),), (('pc-report', mode),)
    for host in ('mac', 'windows'):
        for mode in ('missing-age', 'stale-age', 'future-age', 'partial-residency'):
            data = healthy(host)
            if mode == 'partial-residency':
                data['models'][0]['loaded'] = None
            elif mode == 'missing-age':
                data['models'][0].pop('ageSeconds')
            else:
                data['models'][0]['ageSeconds'] = 3600 if mode == 'stale-age' else -60
            yield data, (('model-row', mode),), (('model-row', mode),)


def main():
    bases = {host: healthy(host) for host in ('mac', 'windows')}
    busy_bases = {host: healthy(host, active=True) for host in ('mac', 'windows')}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        from fix9_raw import raw_cases
        from fix10_dimensions import dimensions
        from fix11_dimensions import dimensions as two_sided
        from fix1_dimensions import dimensions as integrated
        for data, changes, inputs in itertools.chain(source_cases(bases), source_cases(busy_bases), raw_cases(healthy), edge_cases(), dimensions(healthy), two_sided(healthy), integrated(healthy)):
            host = data['host']
            if host == 'windows':
                store = SnapshotStore()
                store.publish(data)
                snapshot = store.read()
                status = probes.pc_status(snapshot)
                presence = probes.peer_presence(snapshot, 'fixture-pc')
            else:
                # Root store is the Mac publisher, never constructs a server or binds a socket.
                from server import SnapshotStore as MacStore
                store = MacStore()
                store.publish(data)
                snapshot = store.read()
                try:
                    presence = peer_feed.mac_presence(snapshot, 'fixture-mac')
                    status = presence
                except peer_feed.InventoryUnknown as exc:
                    presence = status = dict(unknownReason=str(exc))
            packet = dict(host=host, changes=changes, inputs=inputs, snapshot=snapshot, status=status, presence=presence)
            path = root / 'snapshot.json'
            probes.write_json_atomic(path, packet, stop_at=root)
            print(path.read_text())


if __name__ == '__main__':
    main()
