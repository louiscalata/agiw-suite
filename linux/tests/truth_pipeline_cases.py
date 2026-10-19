"""Two-sided contract dimensions. Expected values come from supplied observations.

Observation age, last-measured file mtime, and browser capture time are deliberately
independent. No HTTP, machine or model-service calls are made.
"""
import copy
import itertools
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from agiw_linux import probes, server
from pipeline_fixture import fixture_path, forbidden
from generate_pipeline_cases import FixtureCollector, GPU, NOW
from shape_pipeline_cases import emit, aggregate, check

READ_LAST_RATES = probes.last_rates


def base_snapshot(activity='generating'):
    fixture = FixtureCollector([], activity)
    fixture.collect()
    value = fixture.collect()
    # Supply complete slot observations for both inventory lanes. The real PAIR
    # endpoint does not supply these; the degradation matrix keeps that unknown.
    for lane in value['lanes']:
        if lane['id'] in ('pair-llamacpp', 'gpu-wrapper-101'):
            lane.update(status='busy' if activity == 'generating' else 'idle', phase=activity,
                        slotsBusy=int(activity == 'generating'), slotsTotal=1,
                        liveTokensPerSecond=10 if activity == 'generating' else None)
    for model in value['models']:
        model.update(activity=activity, slotsBusy=int(activity == 'generating'), slotsTotal=1,
                     liveTokensPerSecond=10 if activity == 'generating' else None)
    # server.once adds heartbeatUnit=unknown for a fallback probe: it is not a
    # complete daemon observation. Cache the explicitly observed fixture below.
    value['components'] = [{'id': 'pair', 'state': 'up', 'reason': 'observed'},
                           {'id': 'coordinator', 'state': 'active', 'health': 'up', 'reason': 'intended-unit-active-observed'}]
    return value


def publish_or_cache(value, dimension, coordinates, checks, cached):
    if not cached:
        return emit(value, dimension, coordinates, checks)
    entry = emit(base_snapshot(), dimension, coordinates, checks)
    peer = entry['snapshot']
    peer.update(models=copy.deepcopy(value['models']), modelsComplete=True, modelsTotal=len(value['models']))
    class NoProbe:
        def collect(self):
            raise AssertionError('cache projection unexpectedly fell back to live probing')
    import json
    with tempfile.TemporaryDirectory() as temporary:
        state = Path(temporary).resolve() / 'state'
        server.write_status(state, peer)
        entry['snapshot'] = json.loads(server.once(NoProbe(), clock=lambda: NOW, state_dir=state))
    return entry


def exact_fields(activity, model_ids):
    """All supported displayed values, independently specified, including zeros.

Unobserved host-wide routes/activity and inapplicable idle throughput stay unknown;
these are not healthy/complete sources and must never be fabricated by the oracle.
"""
    expected = {'health': 'up', 'modelsTotal': str(len(model_ids)), 'loaded': str(len(model_ids)),
                'gpus': '1', 'memory': '30', 'memoryLevel': 'ok', 'callers': '1', 'listeners': '2',
                'component:pair': 'up', 'component:coordinator': 'up',
                'history:code:promptTokensPerSecond': '80 (last measured)',
                'history:code:generationTokensPerSecond': '30 (last measured)'}
    unknown = ['routes', 'activity', 'component:heartbeatUnit', 'lane:coordinator:activity',
               'lane:pair-ollama:activity', 'lane:pair-ollama:health', 'lane:qwen35-vulkan:activity',
               'lane:qwen35-vulkan:health']
    expected['lane:coordinator:health'] = 'up'
    for key in GPU['sources']:
        expected['gpu:0:' + key] = str(GPU[key])
    for model_id, lane_id in model_ids:
        # JS encodes the public identity; this fixture describes it without
        # using production helpers or translating expected values from output.
        from urllib.parse import quote
        prefix = 'model:' + quote(model_id, safe="~()*!.'-") + ':'
        expected.update({prefix + 'loaded': 'loaded', prefix + 'activity': activity,
                         prefix + 'slotsBusy': '1' if activity == 'generating' else '0', prefix + 'slotsTotal': '1'})
        expected['lane:' + lane_id + ':health'] = 'up'
        expected['lane:' + lane_id + ':activity'] = activity
        if activity == 'generating':
            expected[prefix + 'liveTokensPerSecond'] = '10'
        else:
            unknown.append(prefix + 'liveTokensPerSecond')
    return expected, unknown


def truth_cases():
    with patch.object(probes, '_http_get', new=forbidden), \
            patch.object(probes, 'read_codex_state', new=lambda *a, **k: {}), \
            patch.object(probes, 'read_pair_files', new=lambda *a, **k: {}), \
            patch.object(probes, 'last_rates', new=lambda *a, **k: {}):
        base = base_snapshot()
        # F1: consumer receives JSON strings that Python permits but URI encoding
        # rejects. Include paired surrogates/Unicode as healthy controls below.
        for identity, position, neighbor in itertools.product(
                ('\ud800', '\udfff', 'prefix\ud800suffix'), (0, 1), (False, True)):
            entry = emit(base, 'invalid-unicode-identity', [identity, position, neighbor], [])
            row = copy.deepcopy(entry['snapshot']['models'][position])
            row['id'] = identity
            entry['snapshot']['models'] = [row] + (entry['snapshot']['models'][:1] if neighbor else [])
            entry['snapshot']['modelsTotal'] = len(entry['snapshot']['models'])
            entry['expect'] = [{'group': 'F1', 'invalidIdentity': True, 'aggregate': False}]
            entry['property'] = 'no-false-certainty'
            yield entry

        for lane, activity, cached in itertools.product(
                ('missing', None, '', 'gpu-wrapper-404', 'coordinator'), ('idle', 'generating'), (False, True)):
            value = base_snapshot(activity)
            value['models'] = [copy.deepcopy(value['models'][-1])]
            if lane == 'missing':
                value['models'][0].pop('lane', None)
            else:
                value['models'][0]['lane'] = lane
            checks = aggregate(False, group='F2') + [{'group': 'F2', 'allModelsUnknown': True}]
            entry = publish_or_cache(value, 'required-model-lane', [lane, activity, cached], checks, cached)
            entry['property'] = 'no-false-certainty'
            yield entry

        for count, activity, inactive, cached, reverse in itertools.product(
                (2, 3, 32), ('idle', 'generating'), (False, True), (False, True), (False, True)):
            value = base_snapshot(activity)
            model = value['models'][-1]
            value['models'] = [{**model, 'id': f'wrapper-{i}', 'lane': 'gpu-wrapper-101',
                                'loadedState': 'inactive' if inactive and i == count - 1 else 'loaded'} for i in range(count)]
            if reverse:
                value['models'].reverse()
            checks = aggregate(False, group='F3') + [{'group': 'F3', 'allModelsUnknown': True}]
            entry = publish_or_cache(value, 'exclusive-wrapper-identity', [count, activity, inactive, cached, reverse], checks, cached)
            entry['property'] = 'no-false-certainty'
            yield entry

        # Full visible-field oracle: benign variations are isolated, then crossed
        # with activity, fresh boundary and cache replay (no pairwise drops).
        variations = ('baseline', 'one-more-model', 'alias', 'unicode-alias', 'unicode-boundary', 'old-file-mtime', 'recent-measurement')
        for variation, activity, age, cached in itertools.product(variations, ('idle', 'generating'), (0, 9.999), (False, True)):
            value = base_snapshot(activity)
            identities = [('fixture-pair', 'pair-llamacpp'), ('fixture-gpu', 'gpu-wrapper-101')]
            if variation == 'one-more-model':
                value['models'].append({**value['models'][-1], 'id': 'another-model', 'lane': 'gpu-wrapper-202'})
                value['lanes'].append({**next(row for row in value['lanes'] if row['id'] == 'gpu-wrapper-101'), 'id': 'gpu-wrapper-202'})
                value['modelInventoryLanes'].append('gpu-wrapper-202')
                identities.append(('another-model', 'gpu-wrapper-202'))
            if variation in ('alias', 'unicode-alias', 'unicode-boundary'):
                identity = 'alias:q4' if variation == 'alias' else '🚀' * 120 if variation == 'unicode-boundary' else 'model-🚀'
                value['models'][-1]['id'] = '/private/models/' + identity
                identities[-1] = (identity, 'gpu-wrapper-101')
            history_age = 86400 if variation == 'old-file-mtime' else .001 if variation == 'recent-measurement' else 1
            # Real log reader: history uses the file mtime, not the peer clock.
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary).resolve(strict=True) / 'last-code.log'
                path.write_text('prompt eval time = 1 ms (1 ms per token, 80 tokens per second)\neval time = 1 ms (1 ms per token, 30 tokens per second)\n')
                os.utime(path, (NOW - history_age, NOW - history_age))
                value['historicalRates'] = [READ_LAST_RATES(path, NOW)]
            expected, unknown = exact_fields(activity, identities)
            entry = emit(value, 'healthy-complete', [variation, activity, age, cached],
                         [{'group': 'H1', 'exactFields': expected, 'unknownFields': unknown,
                           'callerRows': ['codex · gpu-wrapper · 1 connections'],
                           'observationRows': [['pair-llamacpp observation', 'observed'],
                               ['pair-ollama observation', 'listener-not-correlated-with-ollama-process'],
                               ['gpu-wrapper-101 observation', 'observed'], ['qwen35-vulkan observation', 'staged-not-probed'],
                               ['coordinator observation', 'intended-unit-active-observed']]
                               + ([['gpu-wrapper-202 observation', 'observed']] if variation == 'one-more-model' else [])}], cached=cached)
            entry['now'] = NOW + age
            entry['property'] = 'no-false-doubt'
            yield entry
