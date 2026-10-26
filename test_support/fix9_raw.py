"""Raw transport, file and cache variants for the cross-language invariant matrix."""
import copy
import itertools
import json
import os
from pathlib import Path
import tempfile
from unittest import mock

from agiw_win import probes, collect

NOW = 1_800_000_000.0
MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'stale', 'hidden', 'future')
INPUTS = ('health-response', 'model-response', 'slot-response', 'job-files', 'queue-files', 'peer-files')


def file_probe(kind, mode):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if kind == 'job-files':
            for i in range(2 if mode == 'capped' else 1):
                folder = root / f'20261018-12000{i}'
                folder.mkdir()
                if mode == 'partial':
                    continue
                path = folder / 'manifest.json'
                path.write_text('{' if mode == 'malformed' else json.dumps({'metrics': [
                    {'kind': 'local', 'model': 'model-fast', 'finish': 'stop', 'sec': 1}]}))
                os.utime(path, (NOW + 60 if mode == 'future' else NOW,) * 2)
            call = lambda: probes.codemode_jobs(root / 'absent' if mode == 'missing' else root, [], NOW)
            cap = mock.patch.object(probes, 'CODEMODE_RUNS_MAX', 1 if mode == 'capped' else 20)
        elif kind == 'queue-files':
            for name in ('pending', 'claimed', 'completed'):
                (root / name).mkdir()
            for i in range(2 if mode == 'capped' else 1):
                path = root / 'pending' / f'{i}.json'
                path.write_text('{' if mode == 'malformed' else '{}')
                os.utime(path, (NOW + 60 if mode == 'future' else NOW,) * 2)
            if mode == 'partial':
                (root / 'claimed').rmdir()
            call = lambda: probes.route_queue(root / 'absent' if mode == 'missing' else root, NOW)
            cap = mock.patch.object(probes, 'QUEUE_MAX_ENTRIES', 1 if mode == 'capped' else 1000)
        else:
            directory = probes.peer_dir(root)
            if mode != 'missing': directory.mkdir(parents=True)
            doc = dict(schemaVersion=1, kind='agiw-peer', host='other', platform='linux',
                       observedUnix=NOW + 60 if mode == 'future' else NOW,
                       modelsTotal=2 if mode == 'partial' else 1,
                       models=[dict(id='peer-model', loadedState='loaded', activity='idle')])
            if mode != 'missing':
                (directory / 'other.json').write_text('{' if mode == 'malformed' else json.dumps(doc))
            if mode == 'capped':
                (directory / 'extra.json').write_text(json.dumps({**doc, 'host': 'extra'}))
            call = lambda: probes.read_peers(root, 'fixture-pc', NOW)
            cap = mock.patch.object(probes, 'PEER_SCAN_LIMIT', 1 if mode == 'capped' else 256)
        with cap:
            if mode in ('unreadable', 'hidden'):
                with mock.patch.object(probes.os, 'scandir', side_effect=PermissionError('fixture hidden source')):
                    value, source = call()
            else:
                value, source = call()
        if mode == 'stale':
            source.update(state='stale', ageSeconds=3600, detail='Cached file observation expired')
        return value, source


def raw_cases(healthy):
    cached = {(kind, mode): file_probe(kind, mode)
              for kind in INPUTS[3:] for mode in (None, *MODES)}
    configurations = [()]
    configurations += [((kind, mode),) for kind in INPUTS for mode in MODES]
    configurations += [((left, lm), (right, rm)) for left, right in itertools.combinations(INPUTS, 2)
                       for lm, rm in itertools.product(MODES, repeat=2)]
    with tempfile.TemporaryDirectory() as tmp:
        collector = collect.Collector(Path(tmp), build={}, clock=lambda: NOW)
        try:
            for changes in configurations:
                modes = dict(changes)
                data = healthy('windows')
                expected = []
                lane_results = []
                for i, lane_id in enumerate(('fast', 'deep')):
                    spec = dict(id=lane_id, port=1235+i, alias=f'model-{lane_id}', parallel=2, device='CPU')
                    def fetch(url, *_):
                        kind = 'health-response' if url.endswith('/health') else 'model-response' if url.endswith('/v1/models') else 'slot-response'
                        mode = modes.get(kind)
                        if mode in ('unreadable', 'hidden'):
                            raise PermissionError('fixture socket hidden')
                        if mode == 'missing':
                            return None
                        if mode == 'malformed':
                            return 'not an object or list'
                        if kind == 'health-response':
                            if mode in ('partial', 'capped'):
                                return {'status': None}
                            return {'status': 'ok'}
                        if kind == 'model-response':
                            return {'data': [{'id': spec['alias']}] + ([None] if mode in ('partial', 'capped') else [])}
                        slots = [{'id': n, 'is_processing': False} for n in range(2)]
                        return {'slots': slots[:1], 'complete': False} if mode == 'partial' else {'slots': slots, 'truncated': True} if mode == 'capped' else slots
                    lane = probes.probe_lane(spec, fetch)
                    # Clock faults belong to the retained probe observation, not invented HTTP fields.
                    timed = [modes[kind] for kind in INPUTS[:3] if modes.get(kind) in ('stale', 'future')]
                    at = NOW + 60 if 'future' in timed else NOW - 3600 if timed else NOW
                    collector._lane_ownership[lane_id] = 'allowed'
                    collector._lane_owners[lane_id] = {'pid': i+1, 'created': 1}
                    collector._pollers.clear()
                    with mock.patch.object(collect, 'LanePoller') as poller:
                        poller.return_value.read.return_value = (at, lane)
                        lane = collector._lane(spec, NOW)
                    lane_results.append(lane)
                data['lanes'] = lane_results
                data['models'] = [probes.lane_model_row(lane, dict(id=lane['id'], port=lane['port'], alias=lane['expectedModel'], device='CPU'), NOW)
                                  for lane in lane_results]
                for row, lane in zip(data['models'], lane_results):
                    row['ageSeconds'] = lane['ageSeconds']
                data['windowsWorker']['lanes'] = {lane['id']: dict(up={'up': True, 'down': False, 'unknown': None}[probes.lane_health(lane)['state']],
                    health=probes.lane_health(lane), model=lane['expectedModel'], slotsBusy=lane['slotsBusy'], slotsTotal=lane['slotsTotal']) for lane in lane_results}
                sources = {s['id']: s for s in data['sources']}
                if changes:
                    for kind, mode in changes:
                        if kind in INPUTS[:3]:
                            expected.append(('llama-slots', mode))
                            if kind != 'slot-response' or mode in ('stale', 'future'):
                                expected.append(('lane-inventory', mode))
                sources['llama-slots']['state'] = 'live' if all(lane['status'] in ('idle', 'busy') for lane in lane_results) else 'unavailable'
                sources['lane-inventory']['state'] = 'live' if all(lane['status'] in probes.LANE_UP for lane in lane_results) else 'unavailable'
                for kind, field, sid in [('job-files', 'windowsJobs', 'codemode-runs'), ('queue-files', 'pipeline', 'route-queue'), ('peer-files', 'peers', 'agiw-peers')]:
                    value, src = copy.deepcopy(cached[kind, modes.get(kind)])
                    data[field] = value
                    sources[sid].update(src)
                    if kind in modes:
                        expected.append((sid, modes[kind]))
                yield data, tuple(expected), changes
        finally:
            collector.close()
