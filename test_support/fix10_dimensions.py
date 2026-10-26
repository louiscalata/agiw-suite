"""Additional generator axes: time at the renderer, collector bounds and lane deadlines.

All probes are fixture readers. The real active-row collector, lane cache, publishers,
JSON writer and browser renderer remain in the path; no service or socket is used.
"""
import itertools
import math
from pathlib import Path
import tempfile
from unittest import mock

import activity
from agiw_win import collect, probes

NOW = 1_800_000_000.0
ROUTE_SOURCES = ('router-active', 'monitor-receipts', 'router-archive')


def scenario(data, dimension, inputs, **expected):
    data['fixtureScenario'] = dict(dimension=dimension, inputs=inputs, expected=expected)
    return data, ((dimension, 'generated'),), inputs


def clock_cases(healthy):
    # Includes exact limits, small tolerated clock skew, invalid/missing ages and
    # stale full snapshots. Independent readers must retain their own good evidence.
    for mask, age, elapsed, running, jobs in itertools.product(
            range(1, 8), (0, 27, 29, 30, 30.004, -.001, None, 'missing'),
            (0, 1, 2.5, 3, 3.004, -.5, -1.001), (False, True), (False, True)):
        data = healthy('mac')
        selected = [sid for i, sid in enumerate(ROUTE_SOURCES) if mask & (1 << i)]
        for source in data['sources']:
            if source['id'] in selected:
                if age == 'missing':
                    source.pop('ageSeconds')  # supported legacy source envelope
                else:
                    source['ageSeconds'] = age
        data['windowsJobs']['liveAvailable'] = jobs
        data['activity']['runs'] = [dict(runId='route', source='router-active',
            activity='running' if running else 'unknown', status='unresolved', ageSeconds=1)]
        valid_age = age == 'missing' or type(age) in (int, float) and 0 <= age <= 30
        aged_current = valid_age and (0 if age == 'missing' else age) + max(0, elapsed) <= 30
        feed_current = -1 <= elapsed <= 3
        yield scenario(data, 'route-clock', dict(sources=selected, age=age, elapsed=elapsed,
            running=running, jobs=jobs), routesAvailable=feed_current and aged_current,
            routeRunning=running and feed_current and ('router-active' not in selected or aged_current),
            rendererNow=NOW + elapsed)


def cap_cases(healthy):
    # _read_actives and _live_rows stand in for the record/lock readers; both
    # _active_rows and collect_activity perform their real sorting and bounds.
    for count, layout, listing, history, jobs in itertools.product(
            (0, 1, 11, 12, 13, 24), ('note', 'per-run', 'mixed'),
            ('ok', 'truncated'), (0, 1, 13), (False, True)):
        data = healthy('mac')
        records, live = [], {}
        for i in range(count):
            kind = 'per-run' if layout == 'per-run' or layout == 'mixed' and i % 2 else 'note'
            rid = f'route-{i}'
            live[rid, kind] = dict(live=True, state='running', inputSha256='a' * 64,
                note=dict(sinceUnix=NOW-i/100), recordedUnix=NOW-i/100)
            if kind == 'per-run':
                records.append(dict(runId=rid, _layout=kind, stage='backend_draft', inputSha256='a' * 64,
                    checkpoint=dict(recordedUnix=NOW-i/100)))
        receipts = [dict(runId=f'done-{i}', source='monitor-receipt', recordedAt=NOW-i-1,
                         ageSeconds=i+1, status='RESPONSE_VALIDATED') for i in range(history)]
        with mock.patch.object(activity, '_read_actives', return_value=(records, False, listing)), \
                mock.patch.object(activity, '_live_rows', return_value=live), \
                mock.patch.object(activity, '_receipt_files', return_value=[Path(f'done-{i}.json') for i in range(history)]), \
                mock.patch.object(activity, '_archive_files', return_value=[]), \
                mock.patch.object(activity, '_receipt_runs', return_value=receipts):
            data['activity'] = activity.collect_activity(NOW)
        # Source ages are a separate axis above. Keep all readers available here
        # so a dropped row cannot hide behind an unavailable-reader label.
        data['windowsJobs'].update(liveAvailable=jobs, recent=[])
        yield scenario(data, 'route-cap', dict(count=count, layout=layout, listing=listing,
            history=history, jobs=jobs), truncated=count+history > 12 or listing == 'truncated',
            visibleRunning=min(count, 12), missingActive=count > 12 or listing == 'truncated')


def lane_cases(healthy):
    # Decimal deadlines cannot be recovered from a rounded display age. Exercise
    # both idle/busy and both timestamps to expose cancellation at Unix magnitudes.
    ages = (0, 4.999, 5, 5.001, 5.004, 11.799, 11.8, 11.804, 12.8, 12.804, -.001)
    configurations = list(itertools.product((1000.0, NOW), ages, (False, True), (1.0, 2.0)))
    for observed, busy, interval in itertools.product((1000.0, NOW), (False, True), (1.0, 2.0)):
        for window, direction in itertools.product((5.0, collect.LANE_FRESH_SECONDS + interval-1), (-math.inf, math.inf)):
            configurations.append((observed, math.nextafter(observed+window, direction)-observed, busy, interval))
    with tempfile.TemporaryDirectory() as tmp:
        collector = collect.Collector(Path(tmp), build={}, clock=lambda: NOW)
        try:
            for observed, age, busy, interval in configurations:
                sampled = observed + age
                collector.clock = lambda: sampled
                collector.lane_interval = interval
                data = healthy('windows', active=busy)
                data.update(sampledAt=sampled, fullSampledAt=sampled,
                    laneFreshSeconds=collect.LANE_FRESH_SECONDS + max(0, interval-1),
                    laneActivityFreshSeconds=collect.LANE_ACTIVITY_FRESH_SECONDS)
                lanes = []
                for i, raw in enumerate(data['lanes']):
                    spec = dict(id=raw['id'], port=raw['port'], alias=raw['expectedModel'], device='CPU')
                    collector._lane_ownership[raw['id']] = 'allowed'
                    collector._lane_owners[raw['id']] = dict(pid=i+1, created=1)
                    collector._pollers.clear()
                    with mock.patch.object(collect, 'LanePoller') as poller:
                        poller.return_value.read.return_value = (observed, {**raw, 'liveTokensPerSecond': 12.5})
                        lane = collector._lane(spec, sampled)
                    lanes.append(lane)
                data['lanes'] = lanes
                data['models'] = [probes.lane_model_row(lane, dict(id=lane['id'], port=lane['port'],
                    alias=lane['expectedModel'], device='CPU'), sampled) for lane in lanes]
                for row, lane in zip(data['models'], lanes):
                    row['ageSeconds'] = lane['ageSeconds']
                data['windowsWorker']['lanes'] = {lane['id']: dict(status=lane['status'],
                    up={'up': True, 'down': False, 'unknown': None}[probes.lane_health(lane)['state']],
                    model=lane['expectedModel'], ageSeconds=lane['ageSeconds'],
                    slotsBusy=lane['slotsBusy'], slotsTotal=lane['slotsTotal']) for lane in lanes}
                for src in data['sources']:
                    if src['id'] == 'llama-slots':
                        src['state'] = 'live' if all(l['status'] in ('idle', 'busy') for l in lanes) else 'error'
                    if src['id'] == 'lane-inventory':
                        src['state'] = 'live' if all(l['status'] in probes.LANE_UP for l in lanes) else 'unavailable'
                # Compare timestamps to their deadline, not rounded elapsed values.
                inventory = observed <= sampled <= observed + data['laneFreshSeconds']
                active = observed <= sampled <= observed + 5.0
                yield scenario(data, 'lane-window', dict(observed=observed, age=age, busy=busy, interval=interval),
                    inventory=inventory, activity=active, rendererNow=sampled)
        finally:
            collector.close()


def dimensions(healthy):
    yield from clock_cases(healthy)
    yield from cap_cases(healthy)
    yield from lane_cases(healthy)
