"""Two-sided fixture oracles. Expected values come from inputs, never the publisher.

Real route/job projections, stores, JSON publication and the app renderer are in
the path. All file/process readers are fixture replacements; no service is called.
"""
import copy
import errno
import itertools
import json
from pathlib import Path
from unittest import mock

import activity
import telemetry
from live_feed import LiveFeed
from fix10_dimensions import scenario

NOW = 1_800_000_000.0


def replace_source(data, source):
    data['sources'] = [source if s['id'] == source['id'] else s for s in data['sources']]


def route_projection(state, event_age):
    at = None if event_age is None else NOW - event_age
    record = dict(runId='route', startedUnix=at, stage='backend_draft',
                  checkpoint=dict(recordedUnix=at))
    row = dict(runId='route', state='waiting' if state == 'queued' else 'running',
               stage='backend_draft', host='mac', client='codex', live=True,
               layout='per-run', recoveryRequired=False, recordedUnix=at,
               startedUnix=at, note=dict(resource='mac-pair'), _record=record,
               valid=True, readable=True, pid=None, lockHeld=True, lockBound=True, collision=False)
    router = dict(primary=row, rows=[row], install=dict(inProgress=False), error=None, truncated=False, lanes={}, admission=None, collisions=[])
    with mock.patch.object(telemetry, '_safe_file', side_effect=FileNotFoundError), mock.patch.object(telemetry, '_router_namespace_initialized', return_value=True):
        pipeline, source = telemetry._pipeline(NOW, router)
    if state == 'running':
        telemetry._mark_live_route(pipeline, source, dict(state='processing', routeId='route'))
    run = dict(runId='route', source='router-active', activity=state, status='unresolved',
               ageSeconds=event_age, host='mac', client='codex', layout='per-run')
    with mock.patch.object(activity, '_active_rows', return_value=([run], False, 'ok')), \
            mock.patch.object(activity, '_receipt_files', return_value=[]), \
            mock.patch.object(activity, '_archive_files', return_value=[]):
        projected = activity.collect_activity(NOW)
    return pipeline, source, projected


def job_projection(event_age, running=False):
    jid = 'mac-20260925-080000-' + 'a' * 32
    raw = json.dumps(dict(v=1, event='enqueued' if running else 'result', id=jid,
        unix=NOW-event_age, status='success', model='model-fast', client='codex', lane='fast',
        elapsedSeconds=1, timeout=600, pid=123, predictedPerSecond=12.5,
        promptPerSecond=25, completionTokens=12, promptTokens=25))
    with mock.patch.object(telemetry, '_read_windows_journal', return_value=raw.encode()), \
            mock.patch.object(telemetry, '_pid_alive', return_value=True):
        return telemetry._windows_jobs(NOW)


def dimensions(healthy):
    # Every single benign mutation, idle/busy, both editions and just below the
    # tightest full-feed/model deadline. Unrelated incomplete features aren't
    # fabricated: the exact-field contract enumerates the supplied observations.
    variations = ('base', 'more-model', 'more-job', 'alias', 'recent-success', 'idle-lane', 'boundary')
    for host, busy, variation in itertools.product(('mac', 'windows'), (False, True), variations):
        data = healthy(host, active=busy)
        if variation == 'more-model':
            extra = copy.deepcopy(data['models'][0])
            extra.update(id='third-model', name='Third model')
            data['models'].append(extra)
        if variation == 'more-job':
            data['windowsJobs']['recent'].append(dict(data['windowsJobs']['recent'][0], id='done-2'))
        if variation == 'alias':
            data['models'][0].update(id='alias/served-model', name='Served alias')
        if variation == 'recent-success':
            data['windowsJobs']['recent'][0]['ageSeconds'] = 60
        if variation == 'idle-lane':
            data['models'][0].update(state='idle')
            data['models'][0]['metadata']['slotsBusy'] = 0
            data['windowsWorker']['lanes']['fast']['slotsBusy'] = 0
        elapsed = 2.999 if variation == 'boundary' else 0
        working = sum(row['state'] in ('busy', 'generating') for row in data['models'])
        yield scenario(data, 'healthy-exact', dict(host=host, busy=busy, variation=variation),
            loaded=len(data['models']), working=working, answered=len(data['windowsJobs']['recent']),
            slotsBusy=sum(l['slotsBusy'] for l in data['windowsWorker']['lanes'].values()),
            rendererNow=NOW+elapsed)

    # Event/start age may be absent or arbitrarily old while lock observation is
    # current. Test both sides of its independent inclusive 30-second deadline.
    for state, event_age, observation, elapsed in itertools.product(
            ('queued', 'running'), (None, 0, 29.999, 60, 1800),
            (0, 29.999, 30, 30.001, -.001, None), (0, .002, 2.5)):
        data = healthy('mac')
        pipeline, source, projected = route_projection(state, event_age)
        # Zero exercises the producer's own new observation stamp, not a test
        # repair. Other values exercise a cached observation's independent age.
        if observation != 0:
            source['observationAgeSeconds'] = observation
            projected['sources'][0]['observationAgeSeconds'] = observation
        data['pipeline'], data['activity'] = pipeline, projected
        replace_source(data, source)
        replace_source(data, projected['sources'][0])
        current = observation is not None and 0 <= observation and observation+elapsed <= 30
        yield scenario(data, 'route-semantics', dict(state=state, eventAge=event_age,
            observation=observation, elapsed=elapsed), current=current,
            state=state, eventAge=event_age, rendererNow=NOW+elapsed)

    for running, event_age, observation, elapsed in itertools.product(
            (False, True), (1, 60, 599), (0, 29.999, 30.001, -.001, None), (0, .002, 2.5)):
        data = healthy('mac')
        jobs, source = job_projection(event_age, running)
        if observation != 0:
            source['observationAgeSeconds'] = observation
        data['windowsJobs'] = jobs
        replace_source(data, source)
        current = observation is not None and 0 <= observation and observation+elapsed <= 30
        yield scenario(data, 'journal-semantics', dict(running=running, eventAge=event_age,
            observation=observation, elapsed=elapsed), current=current, running=running,
            eventAge=event_age, rendererNow=NOW+elapsed)

    # The worker's heartbeat is valid for 60 seconds, independently of the
    # 30-second shared status file. Keep both clocks on each side of the boundary.
    for sid, age, elapsed in itertools.product(('windows-worker', 'windows-telemetry'),
            (0, 29.999, 30, 30.001, 59.999, 60, 60.001), (0, .002, 2.5)):
        data = healthy('mac')
        source = next(s for s in data['sources'] if s['id'] == sid)
        source['ageSeconds'] = age
        data['windowsWorker' if sid == 'windows-worker' else 'windowsLive']['ageSeconds'] = age
        limit = 60 if sid == 'windows-worker' else 30
        yield scenario(data, 'source-boundary', dict(source=sid, age=age, elapsed=elapsed),
            current=age+elapsed <= limit, retained=age <= limit, rendererNow=NOW+elapsed)

    for sid, (count, event_age), mode in itertools.product(
            ('monitor-receipts', 'router-archive'), ((0, None), (1, 0), (1, 60), (1, 1800), (2, 60)),
            ('complete', 'denied', 'capped')):
        data = healthy('mac')
        paths = activity._Listing(Path(f'row-{i}.json') for i in range(count))
        paths.scan_cut = mode == 'capped'
        if mode == 'denied' and not paths:
            paths = []  # a failed directory read has no successful listing envelope
        def read(path):
            if mode == 'denied':
                raise PermissionError(errno.EACCES, 'fixture denied read')
            return dict(runId=path.stem, source='monitor-receipt' if sid == 'monitor-receipts' else 'router-archive',
                ageSeconds=event_age, recordedAt=NOW-event_age, status='RESPONSE_VALIDATED')
        with mock.patch.object(activity, '_receipt_files', return_value=paths if sid == 'monitor-receipts' else activity._Listing()), \
                mock.patch.object(activity, '_archive_files', return_value=paths if sid == 'router-archive' else activity._Listing()), \
                mock.patch.object(activity, '_read_archive', side_effect=read), \
                mock.patch.object(activity, '_project_monitor_receipt', side_effect=lambda row, at: row), \
                mock.patch.object(activity, '_project_archive', side_effect=lambda row, at: row), \
                mock.patch.object(activity, '_active_rows', return_value=([], False, 'ok')):
            projected = activity.collect_activity(NOW)
        data['activity'] = projected
        for source in projected['sources']:
            replace_source(data, source)
        yield scenario(data, 'history-read', dict(source=sid, count=count, eventAge=event_age, mode=mode),
            current=mode == 'complete', routes=count, rendererNow=NOW)

    for host, busy, rows, elapsed, paused in itertools.product(
            ('mac', 'windows'), (False, True), ('empty', 'success', 'running'), (0, 2.5, 60), (False, True)):
        if not paused and elapsed <= 3:
            continue
        data = healthy(host, active=busy)
        if rows == 'empty':
            data['windowsJobs']['recent'] = []
        if rows == 'running':
            data['windowsJobs']['inFlight'] = [dict(id='open', ageSeconds=1,
                timeoutSeconds=600, client='codex', lane='fast')]
            if host == 'mac':
                data['activity']['runs'] = [dict(runId='route', source='router-active',
                    activity='running', ageSeconds=60, client='codex')]
        yield scenario(data, 'frozen-capture', dict(host=host, busy=busy, rows=rows,
            elapsed=elapsed, paused=paused), paused=paused, rendererNow=NOW+elapsed,
            capture=NOW)

    for sid, observed_age, elapsed in itertools.product(
            ('pipeline-router', 'router-active', 'windows-jobs'), (29, 29.999, 30), (0, .002, 1, 2.5)):
        data = healthy('mac')
        pipeline, source, projected = route_projection('running', 60)
        data['pipeline'], data['activity'] = pipeline, projected
        replace_source(data, source)
        replace_source(data, projected['sources'][0])
        jobs, source = job_projection(60, running=True)
        data['windowsJobs'] = jobs
        replace_source(data, source)
        next(s for s in data['sources'] if s['id'] == sid)['observationAgeSeconds'] = observed_age
        # The sampler publishes activity without its nested source list; source
        # envelopes live once at snapshot.sources (avoid fixture-only aliases).
        data['activity'].pop('sources', None)
        data = LiveFeed(lambda: {}).overlay(data, [], now=NOW+elapsed)
        yield scenario(data, 'fast-overlay', dict(source=sid, age=observed_age, elapsed=elapsed),
            current=observed_age+elapsed <= 30, age=observed_age+elapsed, rendererNow=NOW+elapsed)

    jid = 'mac-20260925-080000-' + 'b' * 32
    valid = json.dumps(dict(v=1, event='result', id=jid, unix=NOW-60,
        status='success', model='model-fast', elapsedSeconds=1)).encode()
    for mode, raw in [('empty', b''), ('valid', valid), ('malformed', b'{'),
                     ('mixed', valid+b'\n{'), ('invalid-event', valid.replace(b'result', b'invalid')),
                     ('invalid-result', valid.replace(b'success', b'invalid'))]:
        data = healthy('mac')
        with mock.patch.object(telemetry, '_read_windows_journal', return_value=raw):
            jobs, source = telemetry._windows_jobs(NOW)
        data['windowsJobs'] = jobs
        replace_source(data, source)
        yield scenario(data, 'journal-read', dict(mode=mode), current=mode in ('empty', 'valid'),
            answered=1 if mode == 'valid' else 0, rendererNow=NOW)
