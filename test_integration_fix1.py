"""Integrated review replays. All observations and files belong to local fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import activity
import server
import telemetry
from windows.agiw_win import probes
from windows.agiw_win.collect import merge_mac_lan_observation

NOW = 1_800_000_000.0


def source(sid, **fields):
    return dict(id=sid, state='live', ageSeconds=0, observationAgeSeconds=0, **fields)


def publish(pipeline, **fields):
    data = dict(sampledAt=NOW, models=[], sources=[source('pipeline-router')],
                pipeline=pipeline, onlineCodeMode=dict(state='ready', active=False, taskState='idle'))
    data.update(fields)
    store = server.SnapshotStore()
    store.publish(data)
    return store.read()


class IntegratedReviewReplays(unittest.TestCase):
    def test_idle_scan_uses_observation_age_not_absent_checkpoint(self):
        router = dict(primary=None, rows=[], install=dict(inProgress=False), error=None,
                      truncated=False, lanes={}, admission=None, collisions=[])
        with mock.patch.object(telemetry, '_safe_file', side_effect=FileNotFoundError), \
                mock.patch.object(telemetry, '_router_namespace_initialized', return_value=True):
            pipeline, status = telemetry._pipeline(NOW, router)
        self.assertIsNone(pipeline['ageSeconds'])
        for age, complete, idle in ((0, True, True), (30, True, True), (30.001, True, False),
                                    (None, True, False), (0, False, False)):
            with self.subTest(age=age, complete=complete):
                observed = {**status, 'observationAgeSeconds': age, 'complete': complete}
                data = publish(copy.deepcopy(pipeline), sources=[observed])
                self.assertEqual(data['pipeline']['status'], 'idle' if idle else 'unknown')
                if idle:
                    self.assertEqual(data['onlineCodeMode']['state'], 'ready')

    def test_bounded_primary_survives_but_bounded_idle_does_not(self):
        for state, cut in ((state, cut) for state in ('running', 'queued', 'idle') for cut in (False, True)):
            with self.subTest(state=state, cut=cut):
                pipeline = dict(status=state, runId=None if state == 'idle' else 'route',
                                ageSeconds=None, pipelines=[], pipelinesComplete=not cut, runsTruncated=cut)
                data = publish(pipeline, onlineCodeMode=dict(state='processing', active=True, taskState=state))
                self.assertEqual(data['pipeline']['status'], 'unknown' if cut and state == 'idle' else state)
                if state != 'idle':
                    self.assertIs(data['onlineCodeMode']['active'], True)
                if cut:
                    self.assertIs(data['onlineCodeMode'].get('runCountsComplete'), False)

    def test_matching_non_directories_and_linked_runs_root_are_incomplete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / 'runs'
            runs.mkdir()
            target = root / 'target'
            target.mkdir()
            entry = runs / '20261018T000000-run'
            for kind in ('empty', 'file', 'symlink'):
                with self.subTest(kind=kind):
                    if kind == 'file': entry.write_text('hidden run')
                    elif kind == 'symlink': entry.symlink_to(target, target_is_directory=True)
                    try:
                        jobs, status = probes.codemode_jobs(runs, [], NOW)
                        self.assertEqual(jobs['complete'], kind == 'empty')
                        self.assertEqual(status['state'] == 'live', kind == 'empty')
                    finally:
                        entry.unlink(missing_ok=True)
            link = root / 'linked-runs'
            link.symlink_to(runs, target_is_directory=True)
            jobs, status = probes.codemode_jobs(link, [], NOW)
            self.assertFalse(jobs['complete'])
            self.assertEqual(status['state'], 'unavailable')

    def test_missing_queue_directories_still_cannot_certify_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for present in (False, True):
                if present:
                    for state in ('pending', 'claimed', 'completed'): (root / state).mkdir()
                queue, _ = probes.route_queue(root, NOW)
                self.assertEqual(queue.get('pipelinesComplete') is True, present)
                self.assertEqual(queue.get('status') == 'idle', present)
                if not present:
                    self.assertFalse((queue.get('queue') or {}).get('complete', False))

    def test_metric_finishes_are_validated_without_losing_valid_neighbors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / '20261018T000000-run'
            run.mkdir()
            path = run / 'manifest.json'
            good = dict(kind='local', stage='draft', model='fixture-model', tok=1, sec=1, finish='stop')
            for finish in ('stop', 'banana'):
                with self.subTest(finish=finish):
                    path.write_text(json.dumps(dict(metrics=[good, {**good, 'finish': finish}])))
                    os.utime(path, (NOW, NOW))
                    jobs, _ = probes.codemode_jobs(root, [], NOW)
                    self.assertEqual(jobs['complete'], finish == 'stop')
                    self.assertTrue(any(row['state'] == 'success' for row in jobs['recent']))
                    if finish == 'banana': self.assertFalse(any(row['state'] == 'error' for row in jobs['recent']))

    def test_explicit_json_depth_guard_and_normal_json(self):
        self.assertEqual(probes.loads_strict('[{"ok":true}]'), [{'ok': True}])
        with self.assertRaises(ValueError): probes.loads_strict('[' * 100000 + ']' * 100000)

    def test_empty_archive_is_live_even_beside_a_receipt_and_active_route(self):
        receipt = dict(runId='saved', source='monitor-receipt', ageSeconds=1, status='RESPONSE_VALIDATED')
        with mock.patch.object(activity, '_active_rows', return_value=([dict(runId='live', source='router-active', activity='running', ageSeconds=None, layout='per-run')], False, 'ok')), \
                mock.patch.object(activity, '_receipt_files', return_value=activity._Listing([Path('receipt.json')])), \
                mock.patch.object(activity, '_archive_files', return_value=activity._Listing()), \
                mock.patch.object(activity, '_read_archive', return_value=receipt), \
                mock.patch.object(activity, '_project_monitor_receipt', return_value=receipt):
            result = activity.collect_activity(NOW)
        self.assertEqual(next(s for s in result['sources'] if s['id'] == 'router-archive')['state'], 'live')

    def test_failed_history_read_bounds_helper_and_publication(self):
        for failed in (False, True):
            listing = activity._Listing()
            listing.read_failed = failed
            self.assertEqual(activity._history_bound((listing,), [], NOW),
                             dict(truncated=failed, missingFromAgeSeconds=0.0 if failed else None))

    def test_dropped_journal_prefix_never_proves_no_live_jobs(self):
        enqueue = dict(v=1, id='mac-20260926-181500-aaaaaaaaaaaa', event='enqueued', unix=NOW-20,
                       model='fixture-model', timeout=600, pid=123)
        done = dict(v=1, id='mac-20260926-181500-bbbbbbbbbbbb', event='result', unix=NOW-1,
                    status='success', model='fixture-model', elapsedSeconds=1, padding='x'*200)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'jobs.jsonl'
            for repeats in (1, 1600):
                path.write_bytes((json.dumps(enqueue)+'\n'+(json.dumps(done)+'\n')*repeats).encode())
                path.chmod(0o600)
                with mock.patch.object(telemetry, '_WINDOWS_JOBS_PATH', path), mock.patch.object(telemetry, '_pid_alive', return_value=True):
                    jobs, _ = telemetry._windows_jobs(NOW)
                self.assertEqual(jobs['complete'], repeats == 1)
                self.assertEqual(jobs['liveAvailable'], repeats == 1)
                self.assertEqual(jobs['unsettled'], 1 if repeats == 1 else None)

    def test_positive_hands_off_is_independent_of_missing_attribution(self):
        for busy, gpu_live, notification in ((True, True, None), (False, False, 3), (False, True, 5), (False, False, None)):
            with self.subTest(busy=busy, gpu_live=gpu_live, notification=notification):
                hands = probes.hands_off([dict(index=0, name='GPU', utilizationPercent=90 if busy else 0)],
                    gpu_live=gpu_live, gpu_complete=gpu_live, lane_pids=None, notification_state=notification)
                data = probes.enforce_snapshot_evidence(dict(sources=[source('hands-off'), source('pc-gpu', complete=gpu_live)], handsOff=hands))
                self.assertEqual(data['handsOff']['active'], hands['active'])
                if busy: self.assertIs(data['handsOff']['active'], True)

    def test_cached_lan_evidence_keeps_its_own_clock_and_identity(self):
        mac = dict(state='linked', configuredName='mac.local', linkAgeSeconds=2.4, ageSeconds=2.4,
                   verified=False, address=None, latencyMs=None)
        for age in (0, 45, 45.04, -5, -5.04):
            for name in ('mac', 'other'):
                with self.subTest(age=age, name=name):
                    result, status = merge_mac_lan_observation(mac, dict(state='unreachable', name=name,
                        observedAt=NOW-age, reason='refused'), NOW)
                    current = -5 <= age <= 45 and name == 'mac'
                    self.assertEqual(result.get('lanFailure'), 'refused' if current else None)
                    self.assertEqual(result.get('lanFailureAgeSeconds'), max(0.0, age) if current else None)
                    self.assertEqual(status['state'], 'error' if current else 'unavailable')
                    self.assertEqual((result['state'], result['ageSeconds'], result['verified']), ('linked', 2.4, False))
        self.assertNotIn('lanFailure', mac)
        for at in (None, 'bad', True, 10**1000):
            result, status = merge_mac_lan_observation(mac, dict(state='unreachable',name='mac',reason='refused',observedAt=at), NOW)
            self.assertNotIn('lanFailure', result)
            self.assertEqual(status['state'], 'unavailable')


if __name__ == '__main__': unittest.main()
