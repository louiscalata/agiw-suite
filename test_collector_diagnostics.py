"""Collector failure contracts through in-memory stores and handlers; no sockets."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import inference_collect
from inference_stats import MAP_SCHEMA
import server

sys.path.insert(0, str(Path(__file__).resolve().parent / 'windows'))
from agiw_win import server as win_server


def sample(at=100):
    return {'schemaVersion': 1, 'host': 'mac', 'sampledAt': at, 'fullSampledAt': at,
            'models': [], 'sources': [], 'machines': [],
            'inference': {'schema': MAP_SCHEMA, 'observedAt': '1970-01-01T00:01:40+00:00',
                          'nodes': [], 'edges': []}}


class StopAfterOne:
    def __init__(self):
        self.called = False

    def is_set(self):
        before = self.called
        self.called = True
        return before

    def wait(self, _):
        return True


class CollectorDiagnosticsTests(unittest.TestCase):
    def test_failure_publication_keeps_all_original_evidence_clocks_and_history(self):
        for module in (server, win_server):
            with self.subTest(platform=module.__name__):
                store = module.SnapshotStore()
                store.publish(sample())
                before = copy.deepcopy(store.snapshot)
                with patch.object(module.time, 'time', return_value=120):
                    store.publish_error('collect_snapshot', TimeoutError('/private/fixture?token=secret'))
                    after, sequence = store.wait_for_change(before['sequence'], 0)
                self.assertEqual(after['sampledAt'], before['sampledAt'])
                self.assertEqual(after['fullSampledAt'], before['fullSampledAt'])
                self.assertEqual(after['history'], before['history'])
                self.assertEqual(after['inference']['observedAt'], before['inference']['observedAt'])
                self.assertGreater(sequence, before['sequence'])
                diagnostic = after['collectorDiagnostics'][0]
                self.assertEqual(diagnostic, {'stage': 'collect_snapshot', 'code': 'timeout',
                                             'failedAt': 120, 'lastSuccessfulSampleAt': 100})
                self.assertEqual(after['inference']['collectorDiagnostics'], [diagnostic])
                self.assertNotIn('private', json.dumps(after))
                self.assertFalse(after['activityKnown'])

    def test_failures_before_first_sample_have_api_diagnostics_without_a_fake_sample(self):
        for module in (server, win_server):
            store = module.SnapshotStore()
            with patch.object(module.time, 'time', return_value=120):
                store.publish_error('collect_snapshot', ValueError('private configuration'))
            self.assertIsNone(store.read())
            for path in ('/api/snapshot', '/api/inference'):
                with self.subTest(platform=module.__name__, path=path):
                    request = object.__new__(module.Handler)
                    request.path = path
                    request.same_origin = lambda: True
                    request.server = SimpleNamespace(store=store)
                    replies = []
                    request.reply = lambda status, payload, mime: replies.append((status, json.loads(payload)))
                    if module is server:
                        request.headers = {'Host': '127.0.0.1:8765'}
                        request.server.server_address = ('127.0.0.1', 8765)
                    request.do_GET()
                    status, value = replies[0]
                    self.assertEqual(status, 503)
                    self.assertEqual(value['collectorDiagnostics'][0]['code'], 'invalid_value')
                    self.assertIsNone(value['collectorDiagnostics'][0]['lastSuccessfulSampleAt'])
                    self.assertNotIn('sampledAt', value)

    def test_diagnostics_are_bounded_and_clear_on_a_successful_full_sample(self):
        for module in (server, win_server):
            with self.subTest(platform=module.__name__):
                store = module.SnapshotStore()
                for index in range(20):
                    store.publish_error('stage_%d' % index, RuntimeError('private details'))
                self.assertEqual(len(store.diagnostics()), 8)
                store.publish_error('stage_19', PermissionError('private path'))
                self.assertEqual(len(store.diagnostics()), 8)
                self.assertEqual(store.diagnostics()[-1]['code'], 'permission_denied')
                store.publish(sample(121))
                self.assertEqual(store.diagnostics(), [])
                self.assertEqual(store.read()['collectorDiagnostics'], [])

    def test_mac_sample_failure_names_its_stage_and_still_ticks_memory(self):
        store = server.SnapshotStore()
        store.publish(sample())
        feed = SimpleNamespace(fingerprints=lambda: {})
        with patch.object(server, 'collect_snapshot', side_effect=KeyError('private_field')), \
                patch.object(server, 'memory_sample', return_value=({}, {})) as memory, \
                patch.object(server, 'LocalCallers'):
            server.sample(store, StopAfterOne(), feed=feed, guard=object())
        memory.assert_called_once()
        diagnostic = store.diagnostics()[0]
        self.assertEqual((diagnostic['stage'], diagnostic['code']), ('collect_snapshot', 'missing_field'))
        self.assertEqual(store.snapshot['sampledAt'], 100)

    def test_windows_sample_failure_does_not_renew_old_sample(self):
        store = win_server.SnapshotStore()
        store.publish(sample())
        collector = SimpleNamespace(collect=lambda: (_ for _ in ()).throw(OSError('private share')))
        with patch('sys.stderr'):
            win_server.sample(store, collector, StopAfterOne())
        self.assertEqual(store.snapshot['sampledAt'], 100)
        self.assertEqual(store.diagnostics()[0]['stage'], 'collect_snapshot')
        self.assertEqual(store.diagnostics()[0]['code'], 'io_error')

    def test_failed_inference_retains_history_without_copying_old_rates_into_new_machine(self):
        previous = sample()['inference']
        previous['nodes'] = [{'id': 'lane', 'observedAt': previous['observedAt'],
                              'generationTokensPerSecond': 42}]
        current = sample(120)
        with patch.object(inference_collect, 'collect_map', side_effect=TimeoutError('private endpoint')), \
                patch.object(inference_collect.remote_machines, 'project_snapshot'), \
                patch.object(inference_collect, 'apply_rates') as apply_rates:
            document = inference_collect.attach(current, 'pc', previous=previous, now=120)
        apply_rates.assert_not_called()
        self.assertEqual(document['observedAt'], previous['observedAt'])
        self.assertEqual(document['nodes'], previous['nodes'])
        self.assertEqual(document['error'], 'collector_failed')
        self.assertEqual(current['collectorDiagnostics'][0], {
            'stage': 'inference', 'code': 'timeout', 'failedAt': 120, 'lastSuccessfulSampleAt': 100})
        self.assertNotIn('private', json.dumps(document))

    def test_store_asset_is_allowlisted_on_both_platforms(self):
        for module in (server, win_server):
            self.assertEqual(module.ASSETS['/monitor-store.mjs'],
                             ('monitor-store.mjs', 'text/javascript; charset=utf-8'))


if __name__ == '__main__':
    unittest.main()
