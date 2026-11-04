"""PR 07: Windows inference observations expire on copies at both read boundaries."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win.server import SnapshotStore
from inference_stats import machine_node, make_node


class InferenceExpiryTests(unittest.TestCase):
    def store(self):
        child = make_node(id='pc:qwen3.8-27b', machine='pc', role='llama', label='Qwen', health='ok',
                          observed_at='1970-01-01T00:01:40Z', model_id='qwen3.8-27b',
                          prompt_tokens_per_second=2, generation_tokens_per_second=3)
        parent = machine_node('pc', 'PC', [child], observed_at='1970-01-01T00:01:42Z')
        store = SnapshotStore()
        store.snapshot = {'inference': {'observedAt': parent['observedAt'], 'nodes': [parent, child]},
                          'models': [{'loaded': True}], 'unrelated': 'unchanged'}
        store.sequence = 7
        return store

    def check_copy(self, path):
        store = self.store()
        original = copy.deepcopy(store.snapshot)
        with patch('agiw_win.server.time.time', return_value=129):
            fresh = store.read() if path == 'read' else store.wait_for_change(6, 0)[0]
            self.assertEqual(store.wait_for_change(7, 0), (None, 7))
        self.assertEqual(fresh['inference']['nodes'][1]['modelId'], 'qwen3.8-27b')
        with patch('agiw_win.server.time.time', return_value=131):
            if path == 'read':
                expired = store.read()
            else:
                expired, sequence = store.wait_for_change(7, 0)
                self.assertEqual(sequence, 7)
        self.assertIsNotNone(expired)
        parent, child = expired['inference']['nodes']
        for node in (parent, child):
            self.assertEqual((node['health'], node['reason']), ('not_measured', 'stale'))
            self.assertIsNone(node['promptTokensPerSecond'])
            self.assertIsNone(node['generationTokensPerSecond'])
        self.assertIsNone(child['modelId'])
        self.assertEqual(parent['observedAt'], original['inference']['nodes'][0]['observedAt'])
        self.assertEqual(expired['models'], original['models'])
        self.assertEqual(store.snapshot, original)
        expired['models'].clear()
        self.assertEqual(store.snapshot, original)

    def test_read_expires_only_the_returned_inference_copy(self):
        self.check_copy('read')

    def test_wait_for_change_expires_only_the_returned_inference_copy(self):
        self.check_copy('wait')

    def test_empty_store_and_missing_map_keep_existing_behavior(self):
        store = SnapshotStore()
        self.assertIsNone(store.read())
        self.assertEqual(store.wait_for_change(None, 0), (None, None))
        store.snapshot = {'other': 'data'}
        self.assertEqual(store.read(), {'other': 'data'})
        self.assertEqual(store.wait_for_change(-1, 0), ({'other': 'data'}, 0))
