"""Fix pass 7: refused or incomplete evidence never becomes a known absence."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes
from agiw_win.jsonutil import loads_strict

SPEC = {"id": "fast", "port": 1235, "alias": "fixture"}


def fetch_health(health):
    def fetch(url, *_):
        if url.endswith('/health'):
            return health
        if url.endswith('/v1/models'):
            return {"data": [{"id": "fixture"}]}
        return [{"id": 0, "is_processing": False}]
    return fetch


class Evidence(unittest.TestCase):
    def test_rejected_health_json_does_not_become_idle(self):
        raw = '{"status":"loading","extra":' + '[' * 65 + '0' + ']' * 65 + '}'
        with self.assertRaises(ValueError):
            loads_strict(raw)
        response = mock.Mock(status=200)
        response.read.return_value = raw.encode()
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(probes.http.client, 'HTTPConnection', return_value=connection):
            health = probes.http_get_json('http://127.0.0.1:1235/health')
        lane = probes.probe_lane(SPEC, fetch_health(health))
        self.assertEqual(lane['status'], 'health_unknown')
        self.assertIsNone(lane['slotsBusy'])
        self.assertEqual(probes.lane_health(lane)['state'], 'unknown')

    def test_health_requires_a_recognised_status(self):
        for health in (None, [], {}, {'status': 5}, {'status': {'x': 1}}, {'status': 'error'}):
            with self.subTest(health=health):
                lane = probes.probe_lane(SPEC, fetch_health(health))
                self.assertEqual(probes.lane_health(lane)['state'], 'unknown')
        for status in ('loading', 'loading model'):
            self.assertEqual(probes.probe_lane(SPEC, fetch_health({'status': status}))['status'], 'loading')

    def test_malformed_model_identity_is_unknown(self):
        for row in (None, {'name': 'x'}, {'id': 5}, {'id': ''}):
            fetch = mock.Mock(side_effect=[{'status': 'ok'}, {'data': [row]}])
            self.assertEqual(probes.probe_lane(SPEC, fetch)['status'], 'no_identity')
            self.assertEqual(fetch.call_count, 2)

    def test_owner_refusal_at_every_endpoint_is_unknown(self):
        for position in range(3):
            replies = [{'status': 'ok'}, {'data': [{'id': 'fixture'}]}, []]
            replies[position] = probes.OwnerUnverified('refused')
            lane = probes.probe_lane(SPEC, mock.Mock(side_effect=replies))
            self.assertEqual(lane['status'], 'unknown')
            self.assertIn('ownership', lane['detail'])
            self.assertIsNone(lane['slotsBusy'])

    def test_undeclared_lanes_leave_both_public_inventories_incomplete(self):
        absent = probes.probe_lane(probes.unconfigured_lane('deep'))
        serving = probes.probe_lane(SPEC, fetch_health({'status': 'ok'}))
        for lanes in ([absent, absent], [serving, absent]):
            status = probes.pc_status({'lanes': lanes})
            presence = probes.peer_presence({'lanes': lanes}, 'pc')
            self.assertFalse(status['modelsComplete'])
            self.assertEqual(presence['modelsTotal'], 2)
            self.assertLess(len(presence['models']), presence['modelsTotal'])

    def test_slots_unknown_is_sufficient_for_nisi_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gate = root / 'nisi-gate-v0.2' / 'nisi_gate.mjs'
            gate.parent.mkdir()
            gate.write_text('')
            lanes = [{'id': lane, 'status': 'slots_unknown', 'expectedModel': lane} for lane in ('fast', 'deep')]
            self.assertEqual(probes.nisi_component(lanes, root)['state'], 'ready')

    def test_unknown_lane_keeps_aggregate_health_unknown(self):
        for status in probes.LANE_NO_EVIDENCE | {'other'}:
            self.assertEqual(probes.lanes_health([{'status': 'idle'}, {'status': status}]), 'unknown')
        self.assertEqual(probes.lanes_health([{'status': 'idle'}, {'status': 'loading'}]), 'degraded')

    def test_consumers_failure_and_partial_residency_stay_unknown(self):
        status = lambda: {'totalPhys': 1000, 'availPhys': 50}
        consumers = lambda: [{'lane': True, 'residentBytes': 100, 'processCount': 2}]
        lanes = [{'status': 'idle'}, {'status': 'unreachable'}]
        block, _ = probes.memory_block(status, consumers, lanes=lanes)
        self.assertNotIn('loadedModelCount', block['consumers'][0])
        self.assertTrue(block['consumersKnown'])
        block, _ = probes.memory_block(status, mock.Mock(side_effect=OSError('denied')), lanes=lanes)
        self.assertFalse(block['consumersKnown'])

    def test_inactive_session_is_not_proof_of_no_fullscreen(self):
        block = probes.hands_off([{'utilizationPercent': 3}], gpu_live=True,
                                 lane_pids=frozenset(), notification_state=1)
        self.assertIsNone(block['active'])
        self.assertIn('fullscreen-unknown', block['unknown'])

    def test_pair_partial_census_publishes_only_lower_bounds(self):
        census = {'ok': True, 'uninspected': 2, 'processes': [{'pid': 10, 'name': 'PAIR.exe', 'owner': 'pair'}]}
        row = probes.pair_component(census)
        public = probes.pair_public(row)
        self.assertIsNone(public['processCount'])
        self.assertIsNone(public['engineCount'])
        self.assertEqual(public['processCountAtLeast'], 1)
        self.assertIn('at least 1', row['detail'])
        census['uninspected'] = 0
        self.assertEqual(probes.pair_component(census)['processCount'], 1)


class Files(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))

    def test_alias_is_never_silently_changed(self):
        path = self.root / 'windows-llm-pipeline' / 'config.json'
        for alias in (' fixture ', 'a' * 81, 'fi\nxture'):
            self.write(path, {'runtime': {'lanes': {'fast': {'port': 1235, 'alias': alias}}}})
            specs, _ = probes.lane_specs(self.root)
            self.assertTrue(all(s.get('undeclared') for s in specs))

    def test_unreadable_config_has_an_unknown_reason(self):
        with mock.patch.object(probes, 'read_json', side_effect=PermissionError('denied')):
            specs, _ = probes.lane_specs(self.root)
        lanes = [probes.probe_lane(s) for s in specs]
        self.assertTrue(all('config unreadable' in lane['detail'] for lane in lanes))
        self.assertIn('lane config unreadable; lane unknown: fast, deep', probes.pc_status({'lanes': lanes})['limitations'])
        self.write(self.root / 'windows-llm-pipeline' / 'config.json', {'runtime': {'lanes': {}}})
        specs, _ = probes.lane_specs(self.root)
        self.assertTrue(all(spec['configReason'] == 'missing' for spec in specs))

    def test_mac_config_absent_and_unreadable_are_distinct(self):
        self.assertEqual(probes.configured_mac_identity(self.root), (None, 'unconfigured'))
        path = self.root / 'llm-lab' / 'mac-reach' / 'hosts.json'
        for value in ([], {'mac': None}, {'mac': {'hostname': 'bad name'}}):
            self.write(path, value)
            self.assertEqual(probes.configured_mac_identity(self.root), (None, 'config-unreadable'))
        with mock.patch.object(probes, 'read_json', side_effect=PermissionError('denied')):
            self.assertEqual(probes.configured_mac_identity(self.root), (None, 'config-unreadable'))
        self.write(path, {})
        self.assertEqual(probes.configured_mac_identity(self.root), (None, 'unconfigured'))

    def test_unreadable_share_does_not_say_missing(self):
        with mock.patch.object(Path, 'iterdir', side_effect=PermissionError('denied')):
            block, source = probes.share_health(self.root)
        self.assertEqual(block['state'], 'unknown')
        self.assertIn('unreadable', source['detail'])

    def test_queue_missing_required_folder_cannot_be_idle(self):
        (self.root / 'pending').mkdir()
        block, source = probes.route_queue(self.root)
        self.assertIsNone(block['status'])
        self.assertEqual(source['state'], 'unavailable')

    def test_queue_caps_publish_unknown_counts_and_age(self):
        for state in ('pending', 'claimed', 'completed'):
            (self.root / state).mkdir()
            for index in range(3):
                (self.root / state / f'{index}.json').write_text('{}')
        with mock.patch.object(probes, 'QUEUE_MAX_ENTRIES', 2):
            block, source = probes.route_queue(self.root)
        self.assertIsNone(block['status'])
        for state in ('pending', 'claimed', 'completed'):
            self.assertIsNone(block['queue'][state])
            self.assertTrue(block['queue']['truncated'][state])
        self.assertIsNone(block['queue']['oldestPendingSeconds'])
        self.assertIsNone(block['queue']['staleClaimed'])
        self.assertIn('2+ completed', source['detail'])

    def manifest(self, metrics):
        self.write(self.root / '20261018-120000-fixture' / 'manifest.json', {'run_id': 'fixture', 'metrics': [{'model':'fixture-model','stage':'draft','tok':1,'sec':1,'finish':'stop',**metric} for metric in metrics]})
        os.utime(self.root / '20261018-120000-fixture' / 'manifest.json', (1000, 1000))
        return probes.codemode_jobs(self.root, [SPEC], 1000)

    def test_unknown_finish_does_not_become_error(self):
        jobs, _ = self.manifest([{'kind': 'local', 'finish': None}])
        self.assertFalse(jobs['complete'])
        self.assertEqual(jobs['recent'], [])
        for finish in ('tool_calls', 'unexpected'):
            jobs, _ = self.manifest([{'kind': 'local', 'finish': finish}])
            self.assertEqual(jobs['recent'][0]['state'], 'unknown')
        for finish in ('length', 'error', 'content_filter'):
            jobs, _ = self.manifest([{'kind': 'local', 'finish': finish}])
            self.assertEqual(jobs['recent'][0]['state'], 'error')

    def test_only_loopback_metrics_are_attributed_to_pc_lanes(self):
        for host in ('192.0.2.1', 'example.invalid', '127.0.0.1', 'localhost', '[::1]'):
            jobs, _ = self.manifest([{'kind': 'local', 'endpoint': f'http://{host}:1235/v1'}])
            self.assertEqual(jobs['recent'][0]['lane'], 'fast' if host in ('127.0.0.1', 'localhost', '[::1]') else None)

    def test_metric_limit_and_duplicate_stage_names_are_explicit(self):
        jobs, source = self.manifest([{'kind': 'local', 'stage': 'verify'}] * 11)
        self.assertEqual(jobs['availability']['state'], 'unknown')
        self.assertTrue(jobs['truncated'])
        self.assertEqual(len({row['id'] for row in jobs['recent']}), 8)
        self.assertIn('stage limit', source['detail'])

    def test_run_without_manifest_keeps_history_incomplete(self):
        (self.root / '20261018-000000-running').mkdir()
        jobs, source = probes.codemode_jobs(self.root, [], 1000)
        self.assertEqual(jobs['availability']['state'], 'unknown')
        self.assertIn('1 runs without manifest', source['detail'])

    def peer(self, observed=1000):
        return {'schemaVersion': 1, 'kind': 'agiw-peer', 'host': 'mac', 'platform': 'macos',
                'observedUnix': observed, 'models': [], 'modelsTotal': 0}

    def test_future_dated_peer_has_a_clock_reason(self):
        self.write(probes.peer_dir(self.root) / 'mac.json', self.peer(1006))
        peers, source = probes.read_peers(self.root, 'pc', 1000)
        self.assertEqual(peers, [])
        self.assertEqual(source['reason'], 'clock-ahead')
        self.assertEqual(source['refusalHosts'], {'mac': 'clock-ahead'})
        rows = probes.machine_rows(host='pc', sampled_at=1000, lanes=[], hands=None,
                                  version='test', peers=peers, mac_name='mac', peers_source=source)
        self.assertEqual(rows[1]['reason'], 'clock-ahead')

    def test_peer_refusals_do_not_claim_no_publication(self):
        directory = probes.peer_dir(self.root)
        directory.mkdir(parents=True)
        for content in ('not json', '{"schemaVersion":NaN}', '[' * 65 + '0' + ']' * 65):
            (directory / 'mac.json').write_text(content)
            peers, source = probes.read_peers(self.root, 'pc', 1000)
            self.assertEqual(peers, [])
            self.assertEqual(source['reason'], 'report-refused')
            self.assertNotIn('No AGIW peer has published', source['detail'])

    def test_peer_folder_read_failure_or_link_is_named(self):
        for patcher in (mock.patch.object(probes.os, 'scandir', side_effect=PermissionError('denied')),
                        mock.patch.object(probes, 'is_link_or_junction', return_value=True)):
            with patcher:
                peers, source = probes.read_peers(self.root, 'pc', 1000)
            self.assertEqual(source['reason'], 'peers-unreadable')
            self.assertEqual(peers, [])

    def test_peer_file_count_limit_is_disclosed(self):
        for host in ('one', 'two'):
            self.write(probes.peer_dir(self.root) / f'{host}.json', {**self.peer(), 'host': host})
        with mock.patch.object(probes, 'PEER_MAX_FILES', 1):
            _, source = probes.read_peers(self.root, 'pc', 1000)
        self.assertEqual(source['refusals'], {'scan-truncated': 1})
        self.assertIn('scan limited', source['detail'])

    def test_shared_strict_loader_refuses_duplicates_overflow_and_deep_peer_link(self):
        for value in ('1e400', '{"x":1,"x":2}', '[' * 65 + '0' + ']' * 65):
            with self.assertRaises(ValueError):
                loads_strict(value)
        path = self.root / 'peers.json'
        path.write_text('{"ignored":' + '[' * 65 + '0' + ']' * 65 + '}')
        with mock.patch.object(probes, 'is_link_or_junction', return_value=False), self.assertRaises(ValueError):
            probes._read_peer_link_file(path.resolve())


if __name__ == '__main__':
    unittest.main()
