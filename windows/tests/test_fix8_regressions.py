"""Pass 8 regressions: discovery, activity expiry, installer scope and peer reasons."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agiw_win import collect, probes
from agiw_win.server import SnapshotStore
from tests import test_collector_safety as safety

WINDOWS = Path(__file__).resolve().parents[1]


class DiscoveryGuard(unittest.TestCase):
    def test_documented_discovery_imports_windows_tests_without_root_shadowing(self):
        # Discover every module by the documented CLI; filter execution to avoid recursion
        # and re-running socket/native fixtures. Import errors still fail discovery.
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests',
                                 '-k', 'test_discovered_probes_are_windows'], cwd=WINDOWS,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Ran 1 test', result.stderr)

    def test_discovered_probes_are_windows(self):
        import importlib
        module = sys.modules.get('test_probes') or importlib.import_module('tests.test_probes')
        self.assertEqual(Path(module.__file__).resolve(), WINDOWS / 'tests/test_probes.py')


class ActivityExpiry(unittest.TestCase):
    setUp = safety.CollectorSafety.setUp
    collector = safety.CollectorSafety.collector
    processes = staticmethod(safety.CollectorSafety.processes)

    def test_activity_expires_independently_of_slow_probe_and_retains_history(self):
        collector = self.collector(processes=self.processes(), interval=20)
        collector._lane_ownership['fast'] = 'allowed'
        collector._lane_owners['fast'] = {'pid': 77, 'created': 1}
        for state in ('idle', 'busy'):
            with self.subTest(state=state), mock.patch.object(collect, 'LanePoller') as poller:
                collector._pollers.clear()
                original = {**probes.probe_lane(safety.FAST, safety.CollectorSafety.fetch),
                            'status': state, 'phase': state, 'slotsBusy': int(state == 'busy'),
                            'liveTokensPerSecond': 25.0}
                poller.return_value.read.return_value = (1000.0, original)
                self.assertEqual(collector._lane(safety.FAST, 1005.0)['status'], state)
                for age in (5.004, 8, 10, 11):
                    lane = collector._lane(safety.FAST, 1000 + age)
                    self.assertEqual(lane['status'], 'slots_unknown')
                    self.assertIsNone(lane['slotsBusy'])
                    self.assertIsNone(lane['phase'])
                    self.assertIsNone(lane['liveTokensPerSecond'])
                    self.assertEqual(lane['lastObservation']['status'], state)
                    self.assertEqual(lane['lastObservation']['observedAt'], 1000)
                self.assertEqual(original['status'], state)

    def test_snapshot_emits_evidence_loss_within_ten_seconds(self):
        store = SnapshotStore()
        row = {'host': 'windows', 'id': 'fast', 'name': 'Fast', 'source': 'llama-slots',
               'state': 'idle', 'loaded': True, 'ageSeconds': 0.0}
        data = {'sampledAt': 1000, 'sources': [{'id': 'llama-slots', 'state': 'live'}], 'models': [row]}
        store.publish(data)
        store.publish({**data, 'sampledAt': 1006, 'models': [{**row, 'ageSeconds': 6.0}]})
        snap = store.read()
        self.assertFalse(snap['activityKnown'])
        self.assertIsNone(snap['history'][-1]['active'])
        self.assertEqual(snap['history'][0]['active'], 0)
        self.assertEqual(snap['events'][0]['kind'], 'evidence-loss')
        self.assertLessEqual(snap['events'][0]['at'] - 1000, 10)


class InstallerContracts(unittest.TestCase):
    def test_ownership_checks_complete_arguments_and_working_directory(self):
        source = (WINDOWS / 'install.ps1').read_text()
        ownership = source.split('$existing = $shell.CreateShortcut($link)', 1)[1].split('throw ', 1)[0]
        self.assertIn('$existing.Arguments -cne $expectedArguments', ownership)
        self.assertIn('$existing.WorkingDirectory -ine $Destination', ownership)
        self.assertNotIn('.Contains(', ownership)
        self.assertIn("$expectedArguments = '\"{0}\" /quiet' -f $launcher", source)
        self.assertIn('$shortcut.Arguments = $expectedArguments', source)

    def test_post_creation_preflight_failure_is_inside_cleanup_scope(self):
        source = (WINDOWS / 'install.ps1').read_text()
        outer = source[source.index('$guard = New-Object AgiwInstallGuard'):]
        cleanup = outer.rsplit('} finally {', 1)[1]
        self.assertIn('$guard.RemoveEmptyCreatedDirectories()', cleanup)
        self.assertLess(cleanup.index('RemoveEmptyCreatedDirectories'), cleanup.index('$guard.Dispose()'))


class PeerReasons(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'llm-lab/status/mac-status.json'
        self.path.parent.mkdir(parents=True)

    def test_capped_scan_has_its_own_reason(self):
        directory = probes.peer_dir(self.root)
        directory.mkdir()
        for name in ('one', 'two'):
            (directory / name).write_text('unused')
        with mock.patch.object(probes, 'PEER_SCAN_LIMIT', 1):
            peers, source = probes.read_peers(self.root, 'pc', 1000)
        self.assertEqual(source['reason'], 'scan-truncated')
        self.assertEqual(source['refusals'], {'scan-truncated': 1})
        self.assertEqual(collect.mac_from_presence(peers, 'mac', peers_source=source)['reason'], 'scan-truncated')

    def test_legacy_failures_reach_configured_mac_and_machine_view(self):
        future = {'schemaVersion': 1, 'host': 'mac', 'models': [],
                  'observedAt': dt.datetime.fromtimestamp(1006, dt.timezone.utc).isoformat()}
        for content, reason in [('bad json', 'report-refused'), ('[]', 'report-refused'),
                                (json.dumps(future), 'clock-ahead')]:
            with self.subTest(reason=reason, content=content):
                self.path.write_text(content)
                self.check_reason(reason)
        with mock.patch.object(probes, 'read_json', side_effect=PermissionError('fixture')):
            self.check_reason('report-unreadable')

    def check_reason(self, reason):
        peers, source = probes.read_peers(self.root, 'pc', 1000)
        self.assertEqual(peers, [])
        self.assertEqual(source['reason'], reason)
        self.assertEqual(source['refusalHosts']['mac'], reason)
        self.assertEqual(collect.mac_from_presence(peers, 'mac.local', peers_source=source)['reason'], reason)
        rows = probes.machine_rows(host='pc', sampled_at=1000, lanes=[], hands=None,
                                  version='test', peers=peers, mac_name='mac', peers_source=source)
        self.assertEqual(rows[1]['reason'], reason)
