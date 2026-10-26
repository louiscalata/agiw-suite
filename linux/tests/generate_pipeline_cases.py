"""Deterministic single/pair source degradation through the real one-shot publisher.

No network, subprocess probes or service calls. A healthy snapshot fixture supplies
kernel observations; a second matrix exercises actual filesystem readers; HTTP is injected at the transport boundary. Snapshot sources
are degraded at their reader boundary. Unclocked kernel/HTTP readings inherit the
snapshot clock, so stale/future mutations expire the enclosing observation.
"""
import copy
import itertools
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('AGIW_INVARIANT_ROOT', ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(SOURCE / 'linux'))
from agiw_linux import collect, probes, server
from test_coordinator_units import ABSENT, unit, V8_RUNTIME, V8_SHA

from pipeline_fixture import NOW, fixture_environment, fixture_path, forbidden

# Imports from sibling generators must share this module, including fixture state.
if __name__ == "__main__":
    sys.modules["generate_pipeline_cases"] = sys.modules[__name__]
OMITTED = ('cpu', 'disks', 'temperatures', 'gpuLock', 'pairCluster', 'authority', 'lastCodexRun', 'idleAnalysis')
MODES = ('missing', 'unreadable', 'malformed', 'partial', 'capped', 'hidden', 'stale', 'future')
SOURCES = ('pair-health', 'pair-models', 'wrapper-health', 'wrapper-models', 'wrapper-slots',
           'processes', 'network', 'gpus', 'memory', 'callers', 'listeners', 'models',
           'components', 'history', 'boot', 'routes', 'coordinator-units', 'lanes', 'gpu-fields', 'model-counters', *OMITTED)
GPU = {'name': 'Fixture GPU', 'utilizationPercent': 25, 'memoryUsedMiB': 100, 'memoryTotalMiB': 8192,
       'temperatureC': 42, 'powerW': 30,
       'sources': {key: '/sys/class/drm/card1/device/' + file for key, file in
           [('utilizationPercent', 'gpu_busy_percent'), ('memoryUsedMiB', 'mem_info_vram_used'),
            ('memoryTotalMiB', 'mem_info_vram_total'), ('temperatureC', 'hwmon/hwmon0/temp1_input'),
            ('powerW', 'hwmon/hwmon0/power1_input')]}}
PROCESSES = [{'pid': 101, 'exe': probes.WRAPPER_EXE, 'kind': 'gpu-wrapper', 'startTimeTicks': 10,
              'port': 49152, 'flags': {'host': '127.0.0.1', 'alias': 'fixture-gpu'}},
             {'pid': 102, 'kind': 'pair-router', 'startTimeTicks': 11},
             {'pid': 103, 'coordinator': True, 'coordinatorRuntime': V8_RUNTIME, 'coordinatorReleaseSha256': V8_SHA}]
NETWORK = {'state': 'up', 'reason': 'observed', 'callersState': 'known',
           'listeners': [{'local': '127.0.0.1', 'localPort': port, 'pid': pid, 'startTimeTicks': start,
                          'ownershipVerified': True, 'name': 'llama-server', 'exposed': False}
                         for port, pid, start in [(49152, 101, 10), (8081, 102, 11)]],
           'callers': [{'name': 'codex', 'connections': 1, 'lane': 'gpu-wrapper'}]}
BASE = {'processes': PROCESSES, 'network': NETWORK, 'gpus': [GPU], 'gpusComplete': True,
        'boot': {'bootId': '11111111-2222-4333-8444-555555555555'},
        'memory': {'level': 'ok', 'availablePercent': 30},
        'coordinatorUnits': {'user': ABSENT, 'system': unit(active='active', sub='running', enabled='enabled', main_pid=103)}}


# JSON fixtures have no aliases or custom objects; decode a fresh independent
# copy per poll in C instead of recursively dispatching Python deepcopy.
BASE_JSON = json.dumps(BASE, allow_nan=False)


def broken(value, mode):
    if mode in ('missing', 'unreadable', 'hidden'):
        return None
    if mode == 'malformed':
        return 'malformed'
    if isinstance(value, list):
        return value + [None] if mode == 'partial' else (value or [None]) * 40
    if isinstance(value, dict):
        return {**value, 'complete': False} if mode == 'partial' else {**value, 'truncated': True}
    return None


class FixtureSystem:
    def __init__(self, mutations):
        self.mutations = mutations

    def snapshot(self, previous):
        value = json.loads(BASE_JSON)
        for source, mode in self.mutations:
            if mode in ('stale', 'future'):
                continue
            if source == 'processes':
                # read_processes returns None for unreadable, partial or capped scans.
                value['processes'] = None
            elif source == 'network':
                value['network'] = {'state': 'unknown', 'reason': mode, 'listeners': [],
                                    'callers': [], 'callersState': 'unknown'}
            elif source == 'gpus':
                value['gpus'] = broken(value['gpus'], mode)
                value['gpusComplete'] = False
            elif source == 'memory':
                value['memory'] = broken(value['memory'], mode)
                if mode == 'partial':
                    value['memory'] = {'level': 'ok'}
                elif mode == 'capped':
                    value['memory'] = {'availablePercent': 30, 'level': 'ok', 'truncated': True}
            elif source in ('callers', 'listeners'):
                value['network'][source] = broken(value['network'][source], mode)
                if source == 'callers':
                    value['network']['callersState'] = 'unknown'
                else:
                    value['network']['state'] = 'unknown'
            elif source == 'coordinator-units':
                value['coordinatorUnits'] = None if mode != 'partial' else {'user': ABSENT, 'system': {}}
            elif source == 'boot':
                value['boot'] = {}
        return value


class FixtureCollector(collect.Collector):
    def __init__(self, mutations, activity='idle'):
        self.poll = 0
        self.activity = activity
        super().__init__(root=fixture_path('root'), home=fixture_path('home'),
                         system=FixtureSystem(mutations), fetch=self.fetch, clock=lambda: NOW - 2 + self.poll, host='fixture-linux')
        self.mutations = dict(mutations)

    def fetch(self, url, timeout, cap):
        family = 'pair' if ':8081/' in url else 'wrapper'
        suffix = url.rsplit('/', 1)[-1]
        source = family + '-' + suffix
        mode = self.mutations.get(source)
        if suffix == 'health':
            value = {'status': 'ok'}
        elif suffix == 'models':
            value = {'data': [{'id': 'fixture-pair', 'status': {'value': 'loaded'}}]} if family == 'pair' \
                else {'data': [{'id': 'fixture-gpu'}]}
        else:
            value = {'slots': [{'id': 0, 'is_processing': self.activity == 'generating',
                                'id_task': 7, 'n_decoded': self.poll * 10}]}
        if mode in ('missing', 'unreadable', 'hidden'):
            raise {'missing': FileNotFoundError, 'unreadable': OSError, 'hidden': PermissionError}[mode]()
        if mode == 'malformed':
            return 'malformed'
        if mode == 'partial':
            # Explicit endpoint incompleteness must not be ignored, even with plausible rows.
            value['complete'] = False
        if mode == 'capped':
            value['truncated'] = True
        return value

    def collect(self):
        self.poll += 1
        value = super().collect()
        value['modelsComplete'] = True  # Healthy fixture explicitly attests the entire configured inventory.
        value['historicalRates'] = [{'role': 'code', 'kind': 'last-measured', 'observedUnix': NOW - 1,
                                    'generationTokensPerSecond': 30, 'promptTokensPerSecond': 80}]
        for source in OMITTED:
            value[source] = {'state': 'observed', 'count': 2, 'complete': True}
        for source, mode in self.mutations.items():
            if mode in ('stale', 'future'):
                value['sampledAt'] = NOW - 100 if mode == 'stale' else NOW + 100
            elif source in OMITTED:
                value[source] = broken(value[source], mode)
            elif source == 'models':
                value['models'] = broken(value['models'], mode)
                if mode == 'partial':
                    value['modelsComplete'] = False
            elif source == 'model-counters':
                for model in value.get('models') or []:
                    if not isinstance(model, dict):
                        continue
                    model['slotsBusy'] = 33 if mode == 'capped' else 1.5 if mode == 'malformed' else None
                    if mode != 'partial':
                        model['slotsTotal'] = 64 if mode == 'capped' else None
            elif source == 'gpu-fields':
                for gpu in value.get('gpus') or []:
                    if not isinstance(gpu, dict):
                        continue
                    for field in collect.GPU_KEYS:
                        gpu[field] = 1e16 if mode == 'capped' else 'malformed' if mode == 'malformed' else None
                    if mode == 'partial':
                        gpu.update({key: GPU[key] for key in collect.GPU_KEYS})
                        gpu['sources'] = {}
            elif source == 'lanes':
                value['lanes'] = broken(value['lanes'], mode)
            elif source == 'components':
                value['components'] = broken(value['components'], mode)
            elif source == 'history':
                value['historicalRates'] = broken(value['historicalRates'], mode)
            elif source == 'routes':
                value['authority'] = broken({'state': 'recorded', 'admission': 3}, mode)
        return value


def run(mutations, activity):
    try:
        payload = server.once(FixtureCollector(mutations, activity), clock=lambda: NOW, sleep=lambda _: None,
                              state_dir=fixture_path('no-cache'))
        return {'mutations': mutations, 'activity': activity, 'now': NOW, 'snapshot': json.loads(payload)}
    except Exception as error:
        return {'mutations': mutations, 'activity': activity, 'now': NOW, 'error': type(error).__name__ + ': ' + str(error)}


def boundary_cases():
    # One patch scope for the batch, plain callables rather than 200,000 mocks.
    with patch.object(probes, 'read_codex_state', new=lambda *a, **k: {}), \
            patch.object(probes, 'read_pair_files', new=lambda *a, **k: {}), \
            patch.object(probes, 'last_rates', new=lambda *a, **k: {}):
        for activity in ('idle', 'generating'):
            for mutations in cases():
                yield run(mutations, activity)


def cases():
    yield []
    for source in SOURCES:
        for mode in MODES:
            yield [(source, mode)]
    for first, second in itertools.combinations(SOURCES, 2):
        for a, b in itertools.product(MODES, repeat=2):
            yield [(first, a), (second, b)]


def all_cases():
    # Import before patching so READ_LAST_RATES captures the real reader.
    from raw_pipeline_cases import raw_cases
    from shape_pipeline_cases import shape_cases
    from truth_pipeline_cases import truth_cases
    with fixture_environment():
        if os.environ.get('AGIW_INVARIANT_SHAPES_ONLY') != '1' and os.environ.get('AGIW_INVARIANT_TRUTH_ONLY') != '1':
            yield from boundary_cases()
            yield from raw_cases()
        if os.environ.get('AGIW_INVARIANT_TRUTH_ONLY') != '1':
            yield from shape_cases()
        yield from truth_cases()


if __name__ == '__main__':
    # Batch identical JSON snapshots on the wire. Each case still runs its real
    # publisher; deduplication is by complete serialized bytes, not expectations
    # or an inferred equivalence between mutation modes.
    snapshots = {}
    for case in all_cases():
        if 'snapshot' in case:
            encoded = json.dumps(case['snapshot'], separators=(',', ':'), allow_nan=False, sort_keys=True)
            if encoded in snapshots:
                case.pop('snapshot')
                case['snapshotRef'] = snapshots[encoded]
            else:
                case['snapshotId'] = snapshots[encoded] = len(snapshots)
        print(json.dumps(case, separators=(',', ':'), allow_nan=False, sort_keys=True))
