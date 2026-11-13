"""Run evidence regressions using production collectors and substituted adapters."""
import copy
import unittest
from unittest import mock

import activity
import server
import telemetry
import invariant_pipeline
from windows.tests.test_run_evidence import NOW, windows_collector_read
from windows.agiw_win.server import SnapshotStore as WindowsStore




class Stage7Regressions(unittest.TestCase):
    def test_real_windows_collector_preserves_exact_job_evidence(self):
        snapshot = windows_collector_read()
        evidence = snapshot['activity']
        self.assertTrue(evidence['complete'])
        self.assertEqual(len(snapshot['windowsJobs']['recent']), 1)
        self.assertEqual(evidence['sampledAt'], NOW)
        self.assertEqual(evidence['currentEvidence']['scope'], 'not-applicable')
        self.assertIsNone(evidence['currentEvidence']['count'])
        self.assertIn('router journal producer is not connected', evidence['currentEvidence']['reason'])

    def test_slow_earlier_stages_do_not_backdate_router_observation(self):
        original = telemetry.collect_snapshot
        def delayed_fixture():
            clock = [NOW]
            def delay():
                clock[0] += 10
                return telemetry._lms_server_status.return_value
            telemetry._lms_server_status.side_effect = delay
            with mock.patch.object(telemetry.time, 'time', side_effect=lambda: clock[0]):
                snapshot = original()
            self.assertEqual(snapshot['sampledAt'], NOW)
            telemetry.observe_router.assert_called_once_with(NOW + 10)
            return snapshot
        with mock.patch.object(telemetry, 'collect_snapshot', side_effect=delayed_fixture):
            invariant_pipeline.collect()

    def test_inventory_clock_uses_router_observation_not_sample_start(self):
        snapshot = {'sampledAt': NOW - 10, 'pipeline': {'pipelinesComplete': True, 'pipelines': []},
                    'sources': [{'id': 'pipeline-router', 'state': 'live', 'ageSeconds': 0}]}
        with mock.patch('telemetry.last_router_observation', return_value={'observedAt': NOW, 'rows': []}):
            inventory = server.router_run_inventory(snapshot)
        self.assertEqual(inventory['observedAt'], NOW)
        self.assertTrue(activity.current_run_evidence(inventory, NOW)['complete'])

    def test_inventory_and_store_share_clock_skew_boundary(self):
        for offset, complete in ((-3.001, False), (-3, True), (0, True), (1, True), (1.001, False)):
            with self.subTest(offset=offset):
                inventory = {'observedAt': NOW + offset, 'complete': True,
                             'counts': {'running': 0, 'queued': 0, 'unresolved': 0}}
                evidence = activity.current_run_evidence(inventory, NOW)
                self.assertEqual(evidence['complete'], complete)
                for store_class in (server.SnapshotStore, WindowsStore):
                    store = store_class()
                    store.snapshot = {'sampledAt': NOW, 'models': [], 'sources': [],
                                      'activity': {'runs': [], 'sampledAt': NOW + offset,
                                                   'complete': True, 'currentEvidence': copy.deepcopy(evidence)}}
                    with mock.patch('time.time', return_value=NOW):
                        result = store.read()['activity']
                    self.assertEqual(result['complete'], complete)
                    self.assertEqual(result['currentEvidence']['complete'], complete)
