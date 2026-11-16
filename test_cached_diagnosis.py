"""Cached diagnosis age tests use only in-memory observations and clocks."""
import copy
import datetime as dt
from email.message import Message
import io
import json
from types import SimpleNamespace
import unittest
from unittest import mock

import online_code_repair as repair
import server


class CachedDiagnosisTests(unittest.TestCase):
    def sample(self, **clocks):
        return {'sampledAt': 100, 'machines': [
            {'platform': 'mac', 'state': 'up', 'reasonCode': 'observed'},
            {'platform': 'windows', 'state': 'down', 'reasonCode': 'unreachable'},
        ], **clocks}

    def test_diagnosis_and_readiness_age_out_both_positive_and_negative_states(self):
        for now, reason in ((103, None), (103.001, 'stale'), (99, 'clock_skew')):
            with self.subTest(now=now):
                sample = self.sample()
                before = copy.deepcopy(sample)
                with mock.patch.object(repair.time, 'time', return_value=now):
                    diagnosis = repair.read_only_diagnosis('fix-local', sample)
                readiness = server.cached_readiness(sample, now)
                for result in (diagnosis, readiness):
                    self.assertEqual([row['state'] for row in result['machines']],
                                     ['unknown', 'unknown'] if reason else ['up', 'down'])
                    self.assertEqual([row['reasonCode'] for row in result['machines']],
                                     [reason, reason] if reason else ['observed', 'unreachable'])
                    self.assertEqual(result['sampledAt'], 100)
                    self.assertAlmostEqual(result['ageSeconds'], now - 100)
                self.assertIn('mac: unknown (' + reason + ')' if reason else 'mac: up (observed)',
                              diagnosis['diagnosis'])
                if reason:
                    for result in (diagnosis, readiness):
                        self.assertEqual([row['lastState'] for row in result['machines']], ['up', 'down'])
                        self.assertEqual([row['lastReasonCode'] for row in result['machines']],
                                         ['observed', 'unreachable'])
                self.assertEqual(sample, before, 'diagnosis must not mutate cached evidence')

    def test_fast_overlay_cannot_renew_the_full_sample(self):
        sample = self.sample(fullSampledAt=90)
        result = repair.read_only_diagnosis(None, sample, now=100)
        self.assertEqual((result['sampledAt'], result['ageSeconds']), (90, 10))
        self.assertTrue(all(row['state'] == 'unknown' and row['reasonCode'] == 'stale'
                            for row in result['machines']))
        self.assertEqual(server.cached_readiness(sample, 100)['machines'], result['machines'])

    def test_missing_or_invalid_full_sample_clock_keeps_states_unknown(self):
        samples = [self.sample(fullSampledAt=value) for value in (None, True, '100', float('nan'), float('inf'))]
        missing = self.sample(); missing.pop('sampledAt'); samples.append(missing)
        for sample in samples:
            with self.subTest(clock=sample.get('fullSampledAt')):
                with mock.patch.object(repair.time, 'time', return_value=100):
                    result = repair.read_only_diagnosis(None, sample)
                readiness = server.cached_readiness(sample, 100)
                self.assertTrue(all(row['state'] == 'unknown' and row['reasonCode'] == 'not_observed'
                                    for row in result['machines']))
                self.assertEqual(readiness['machines'], result['machines'])
                self.assertIn('mac: unknown (not_observed)', result['diagnosis'])
                for response in (result, readiness):
                    self.assertIsNone(response['sampledAt'])
                    self.assertIsNone(response['ageSeconds'])

    def test_closed_owner_uses_its_injected_clock(self):
        owner = repair.OnlineCodeRepair(snapshot_read=lambda: self.sample(), clock=lambda: 3700)
        result = owner.request()
        self.assertIn('mac: unknown (stale)', result['diagnosis'])
        self.assertEqual(result['ageSeconds'], 3600)

    def test_closed_http_owner_passes_current_time_without_running_an_action(self):
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(l2_open=False, store=SimpleNamespace(read=lambda: self.sample()))
        action = mock.Mock(side_effect=AssertionError('must stay read-only'))
        with mock.patch.object(server.time, 'time', return_value=3700):
            result = handler.owner_action('fix-local', action)
        self.assertIn('mac: unknown (stale)', result['diagnosis'])
        self.assertEqual(result['ageSeconds'], 3600)
        action.assert_not_called()

    def test_direct_read_reages_the_requested_action_and_uses_the_current_store(self):
        clock = mock.Mock(return_value=101)
        sample = self.sample()
        owner = repair.OnlineCodeRepair(runner=mock.Mock(), snapshot_read=lambda: sample, clock=clock)
        posted = owner.request_fix('local')
        self.assertIn('mac: up (observed)', posted['diagnosis'])
        self.assertEqual(posted['ageSeconds'], 1)
        clock.return_value = 3700
        aged = owner.read()
        self.assertIn('mac: unknown (stale)', aged['diagnosis'])
        self.assertEqual((aged['action'], aged['ageSeconds']), ('fix-local', 3600))
        self.assertEqual((aged['machines'][0]['lastState'], aged['machines'][0]['lastReasonCode']),
                         ('up', 'observed'))
        sample = self.sample(sampledAt=3700)
        sample['machines'][0].update(state='down', reasonCode='unreachable')
        renewed = owner.read()
        self.assertIn('mac: down (unreachable)', renewed['diagnosis'])
        self.assertEqual(renewed['ageSeconds'], 0)
        self.assertEqual(posted['machines'][0]['state'], 'up', 'reads must not mutate prior responses')

    def test_post_then_entry_get_ages_real_store_and_retains_last_observation(self):
        store = server.SnapshotStore()
        sample = self.sample()
        for row in sample['machines']:
            row['observedAt'] = dt.datetime.fromtimestamp(100, dt.timezone.utc).isoformat()
        store.snapshot = copy.deepcopy(sample)
        owner = mock.Mock(side_effect=AssertionError('owner actions must not run'))
        state = SimpleNamespace(l2_open=False, store=store, online_code_repair=owner,
                                action_diagnosis=None, control_capability='fixture-only',
                                server_address=('127.0.0.1', 8431))
        handler = server.Handler.__new__(server.Handler)
        handler.server = state
        handler.path = '/api/online-code-mode/repair'
        raw = json.dumps({'scope': 'local', 'reviewed': True}).encode()
        handler.rfile = io.BytesIO(raw)
        handler.headers = Message()
        for key, value in {'Host': '127.0.0.1:8431', 'Origin': 'http://127.0.0.1:8431',
                           'Content-Type': 'application/json', 'Content-Length': str(len(raw)),
                           'X-AGIW-Control': 'fixture-only'}.items():
            handler.headers[key] = value
        handler.reply = mock.Mock()
        with mock.patch.object(server, 'mac_online_code_controls_enabled', return_value=True), \
             mock.patch.object(server.time, 'time', return_value=101) as clock:
            handler.do_POST()
            status, payload, _ = handler.reply.call_args.args
            self.assertEqual(status, 423)
            posted = json.loads(payload)
            self.assertIn('mac: up (observed)', posted['diagnosis'])
            self.assertEqual(posted['ageSeconds'], 1)
            handler.path = '/api/online-code-mode/entry'
            clock.return_value = 3700
            handler.do_GET()
            status, payload, _ = handler.reply.call_args.args
            self.assertEqual(status, 200)
            aged = json.loads(payload)
            self.assertIn('mac: unknown (stale)', aged['diagnosis'])
            self.assertEqual((aged['action'], aged['ageSeconds']), ('fix-local', 3600))
            self.assertEqual([row['lastState'] for row in aged['machines']], ['up', 'down'])
            self.assertEqual([row['lastReasonCode'] for row in aged['machines']],
                             ['observed', 'unreachable'])
            self.assertEqual(store.snapshot, sample, 'GET must not rewrite stored evidence')
            store.snapshot['sampledAt'] = 3700
            for row in store.snapshot['machines']:
                row['observedAt'] = dt.datetime.fromtimestamp(3700, dt.timezone.utc).isoformat()
            store.snapshot['machines'][0].update(state='down', reasonCode='unreachable')
            handler.do_GET()
            renewed = json.loads(handler.reply.call_args.args[1])
            self.assertIn('mac: down (unreachable)', renewed['diagnosis'])
            self.assertEqual((renewed['action'], renewed['ageSeconds']), ('fix-local', 0))
        self.assertEqual(owner.mock_calls, [])

    def test_reaging_preserves_existing_last_observed_fields(self):
        sample = self.sample()
        sample['machines'][0].update(state='unknown', reasonCode='stale',
                                     lastState='up', lastReasonCode='observed')
        for now, reason in ((3700, 'stale'), (99.999, 'clock_skew')):
            with self.subTest(reason=reason), mock.patch.object(repair.time, 'time', return_value=now):
                result = repair.read_only_diagnosis('fix-local', sample)
                self.assertIn('mac: unknown (' + reason + ')', result['diagnosis'])
                self.assertEqual((result['machines'][0]['lastState'],
                                  result['machines'][0]['lastReasonCode']), ('up', 'observed'))

    def test_same_host_diagnosis_rejects_even_small_future_skew(self):
        with mock.patch.object(repair.time, 'time', return_value=99.999):
            result = repair.read_only_diagnosis('fix-local', self.sample())
        self.assertIn('mac: unknown (clock_skew)', result['diagnosis'])
        self.assertEqual(result['machines'][0]['lastState'], 'up')
