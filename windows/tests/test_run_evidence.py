"""Real Windows journal collection with passive adapters replaced by fixtures."""
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import collect, probes
from agiw_win.server import SnapshotStore

NOW = 1_800_000_000.0


def windows_collector_read():
    with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
        root = Path(directory)
        run = root / 'runs' / '20261018-120000-000'
        run.mkdir(parents=True)
        manifest = run / 'manifest.json'
        manifest.write_text(json.dumps({'run_id': 'fixture', 'metrics': [
            {'kind': 'local', 'stage': 'draft', 'model': 'fixture/model',
             'tok': 1, 'sec': 1, 'finish': 'stop'}]}))
        os.utime(manifest, (NOW - 1, NOW - 1))
        for state in ('pending', 'claimed', 'completed'):
            (root / 'bin' / 'online-code-route-queue' / state).mkdir(parents=True)
        for name, value in {'lane_specs': ([], None), 'read_peers': ([], None),
                            'configured_mac_name': None, 'share_health': ({'state': 'unknown'}, None),
                            'nisi_component': {'id': 'nisi', 'state': 'unknown'}}.items():
            stack.enter_context(mock.patch.object(probes, name, return_value=value))
        processes, census = mock.Mock(), mock.Mock()
        processes.sample.return_value = ({}, [], {'id': 'lane-processes', 'state': 'unavailable', 'detail': 'Fixture unavailable'})
        census.sample.return_value = None
        collector = collect.Collector(root, root / 'bin', runs_root=root / 'runs',
            clock=lambda: NOW, host='windows', build={}, lane_processes=processes, pair_census=census,
            notification_fn=lambda: None, clients_fn=lambda: [],
            fetch=mock.Mock(side_effect=AssertionError('No endpoint request is allowed')),
            gpu_fn=lambda: ([], None), adapters_fn=lambda: [], memory_fn=lambda: ({'level': 'unknown'}, None))
        stack.callback(collector.close)
        store = SnapshotStore()
        with mock.patch('time.time', return_value=NOW):
            store.publish(collector.collect())
            return store.read()


class WindowsRunEvidenceTests(unittest.TestCase):
    def test_real_collector_preserves_job_counts_and_router_scope(self):
        snapshot = windows_collector_read()
        self.assertTrue(snapshot['activity']['complete'])
        self.assertEqual(len(snapshot['windowsJobs']['recent']), 1)
        self.assertEqual(snapshot['activity']['sampledAt'], NOW)
        evidence = snapshot['activity']['currentEvidence']
        self.assertEqual(evidence['scope'], 'not-applicable')
        self.assertIsNone(evidence['count'])
        self.assertIn('router journal producer is not connected', evidence['reason'])

    def test_legacy_collector_gets_scope_reason_without_false_staleness(self):
        snapshot = windows_collector_read()
        del snapshot['activity']['sampledAt']
        del snapshot['activity']['currentEvidence']
        store = SnapshotStore()
        store.snapshot = snapshot
        with mock.patch('time.time', return_value=NOW):
            evidence = store.read()['activity']
        self.assertTrue(evidence['complete'])
        self.assertIn('router journal producer is not connected', evidence['currentEvidence']['reason'])


if __name__ == '__main__':
    if '--snapshot' in sys.argv:
        print(json.dumps(windows_collector_read()))
    else:
        unittest.main()
