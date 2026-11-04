"""PR 07: successful slow lanes, partial inventory and post-expiry rollups."""
import copy
import datetime as dt
import json
import time
import unittest
from unittest.mock import patch

import inference_collect
from inference_stats import expire_map, machine_node, make_node
from server import SnapshotStore


def stamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def inventory(rows):
    return {'sampledAt': 100, 'models': rows, 'sources': [
        {'id': 'lmstudio-api', 'state': 'live', 'route': 'direct',
         'endpoint': '127.0.0.1:4321', 'modelsComplete': True,
         'verifiedListener': {'kind': 'lmstudio', 'pid': 123, 'executable': '/fixture/lms'}}]}


def mac_row(loaded, **extra):
    return dict(id='qwen', modelKey='qwen', host='mac', loaded=loaded, ageSeconds=0, context=8192, **extra)


class CollectionReviewFixes(unittest.TestCase):
    def test_slow_successful_lanes_keep_qwen_inside_shared_deadline(self):
        def slow_get(url, timeout=.4):
            time.sleep(min(.15, timeout))
            if timeout < .15:
                raise inference_collect.ProbeError('timeout')
            if url.endswith('/metrics'):
                body = 'llamacpp:tokens_predicted_total 50\n'
            elif url.endswith('/slots'):
                body = json.dumps([{'id': 0, 'is_processing': False}])
            elif url.endswith('/props'):
                body = json.dumps({'model_alias': 'qwen3.8-27b' if ':1236/' in url else 'gpt-oss-20b'})
            elif url.endswith('/api/v0/models'):
                body = json.dumps({'data': [{'id': 'studio', 'state': 'loaded'}]})
            elif url.endswith('/v1/models'):
                body = json.dumps({'data': [{'id': 'qwen3.8-27b'}]})
            else:
                body = json.dumps({'model': 'veritas'})
            return 200, body, 150

        for host in ('mac', 'pc'):
            with self.subTest(host=host), patch('inference_collect.loopback_get', side_effect=slow_get):
                started = time.monotonic()
                document = inference_collect.collect_map(
                    host, targets=inference_collect.direct_targets(), veritas_url='http://127.0.0.1:9/health')
                self.assertLess(time.monotonic() - started, .8)
                nodes = {node['id']: node for node in document['nodes']}
                for node_id in ('pc:router', 'pc:gpt-oss-20b', 'pc:qwen3.8-27b'):
                    self.assertEqual(nodes[node_id]['health'], 'ok')
                self.assertEqual(nodes['pc:qwen3.8-27b']['modelId'], 'qwen3.8-27b')
                if host == 'mac':
                    self.assertEqual(nodes['mac:lmstudio']['modelId'], 'studio')
                    self.assertEqual(nodes['mac:veritas']['modelId'], 'veritas')

    def test_unknown_inventory_rows_do_not_hide_a_loaded_model(self):
        for unknown in (None, 1, 'true'):
            with self.subTest(loaded=unknown), patch('inference_collect.time.time', return_value=100):
                data = inventory([{'host': 'mac', 'loaded': unknown}, mac_row(True)])
                with patch('inference_collect.loopback_get', side_effect=AssertionError('unexpected reprobe')):
                    document = inference_collect.attach(data, 'mac', targets=[], veritas_url='')
                node = next(node for node in document['nodes'] if node['id'] == 'mac:lmstudio')
                self.assertEqual((node['health'], node['reason'], node['modelId'], node['contextLimit']),
                                 ('ok', None, 'qwen', 8192))
                self.assertEqual(node['endpoint'], 'http://127.0.0.1:4321')

    def test_unknown_inventory_without_loaded_model_is_not_unloaded(self):
        for known in ([], [mac_row(False)]):
            with self.subTest(known=known), patch('inference_collect.time.time', return_value=100):
                node = inference_collect._snapshot_lmstudio(inventory([{'host': 'mac', 'loaded': None}] + known))
                self.assertEqual((node['health'], node['reason'], node['modelId']),
                                 ('not_measured', 'load_state_unreported', None))

    def test_boolean_inventory_keeps_freshness_and_unloaded_checks(self):
        with patch('inference_collect.time.time', return_value=100):
            node = inference_collect._snapshot_lmstudio(inventory([mac_row(False)]))
            self.assertEqual((node['health'], node['reason']), ('degraded', 'model_not_loaded'))
            row = mac_row(True)
            row['ageSeconds'] = 4
            node = inference_collect._snapshot_lmstudio(inventory([{'host': 'mac', 'loaded': None}, row]))
            self.assertEqual((node['health'], node['reason']), ('not_measured', 'inventory_unverified'))

    def test_expiry_recomputes_mac_rollup_before_machine_timestamp_expires(self):
        child = make_node(id='mac:lmstudio', machine='mac', role='lmstudio', label='LM Studio', health='ok',
                          observed_at=stamp(100), prompt_tokens_per_second=5, generation_tokens_per_second=10)
        parent = machine_node('mac', 'Mac', [child], observed_at=stamp(102),
                              vram={'usedMiB': 100, 'totalMiB': 200})
        document = {'observedAt': stamp(102), 'nodes': [parent, child]}
        saved = copy.deepcopy(document)
        store = SnapshotStore()
        store.snapshot = {'sampledAt': 102, 'inference': document}
        with patch('server.time.time', return_value=104):
            result = store.read()['inference']['nodes'][0]
        self.assertEqual((result['health'], result['reason']), ('not_measured', 'stale'))
        self.assertIsNone(result['promptTokensPerSecond'])
        self.assertIsNone(result['generationTokensPerSecond'])
        self.assertEqual((result['observedAt'], result['vramUsedMiB'], result['vramTotalMiB']), (stamp(102), 100, 200))
        self.assertEqual(store.snapshot['inference'], saved)

    def test_expiry_sums_only_fresh_children_and_excludes_clients(self):
        for machine in ('mac', 'pc'):
            with self.subTest(machine=machine):
                stale = make_node(id=machine + ':old', machine=machine, role='llama', label='Old', health='down',
                                  observed_at=stamp(60), prompt_tokens_per_second=50, generation_tokens_per_second=100)
                fresh = make_node(id=machine + ':fresh', machine=machine, role='llama', label='Fresh', health='ok',
                                  observed_at=stamp(100), prompt_tokens_per_second=2, generation_tokens_per_second=3)
                client = make_node(id=machine + ':client', machine=machine, role='client', label='Client', health='down',
                                   observed_at=stamp(100), generation_tokens_per_second=999)
                parent = machine_node(machine, machine, [stale, fresh], observed_at=stamp(100))
                document = {'observedAt': stamp(100), 'nodes': [parent, stale, fresh, client]}
                expire_map(document, 101)
                self.assertEqual((parent['health'], parent['reason']), ('ok', None))
                self.assertEqual((parent['promptTokensPerSecond'], parent['generationTokensPerSecond']), (2, 3))
                expire_map(document, 131)
                self.assertEqual((parent['health'], parent['reason']), ('not_measured', 'stale'))
                self.assertIsNone(parent['promptTokensPerSecond'])
                self.assertIsNone(parent['generationTokensPerSecond'])
