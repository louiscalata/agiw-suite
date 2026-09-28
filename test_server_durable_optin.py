"""Synthetic server wiring checks; no model CLI or live Monitor endpoint."""
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


import server as server_source
from durable_model_journal import DurableModelJournal


class ServerDurableOptInTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='monitor-durable-optin-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal_path = self.root / 'model-operation.json'
        self.path_patch = patch.object(server_source, 'MODEL_JOURNAL_PATH', self.journal_path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def monitor(self, *, durable=False):
        result = server_source.MonitorServer(('127.0.0.1', 0), server_source.SnapshotStore(),
                                             durable_model_control=durable)
        self.addCleanup(result.server_close)
        return result

    def test_default_has_no_journal_and_does_not_create_one(self):
        monitor = self.monitor()
        state = monitor.model_control.read()
        self.assertNotIn('durableJournal', state)
        self.assertEqual(state['status'], 'idle')
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(self.journal_path.with_name(self.journal_path.name + '.lock').exists())

    def test_optin_missing_journal_fails_closed_without_provisioning(self):
        monitor = self.monitor(durable=True)
        state = monitor.model_control.read()
        self.assertTrue(state['durableJournal']['enabled'])
        self.assertTrue(state['durableJournal']['blocked'])
        self.assertEqual(state['settlement'], 'unconfirmed')
        self.assertFalse(state['cleanupConfirmed'])
        self.assertFalse(self.journal_path.exists())
        self.assertFalse(self.journal_path.with_name(self.journal_path.name + '.lock').exists())

    def test_optin_reads_only_explicitly_provisioned_ready_journal(self):
        journal = DurableModelJournal(self.journal_path)
        journal.provision_new()  # fixture-only clean install, never server startup
        before = hashlib.sha256(self.journal_path.read_bytes()).hexdigest()
        monitor = self.monitor(durable=True)
        state = monitor.model_control.read()
        self.assertTrue(state['durableJournal']['enabled'])
        self.assertFalse(state['durableJournal']['blocked'])
        self.assertEqual(state['durableJournal']['generation'], 0)
        self.assertEqual(state['status'], 'idle')
        self.assertEqual(hashlib.sha256(self.journal_path.read_bytes()).hexdigest(), before)

    def test_optin_preserves_pending_restart_latch(self):
        journal = DurableModelJournal(self.journal_path)
        journal.provision_new()
        journal.begin(operation_id='a' * 32, action='unload', model_id='publisher/model',
                      model_key='publisher/model', started_at=time.time())
        before = hashlib.sha256(self.journal_path.read_bytes()).hexdigest()
        monitor = self.monitor(durable=True)
        state = monitor.model_control.read()
        self.assertEqual(state['status'], 'failed')
        self.assertEqual(state['settlement'], 'unconfirmed')
        self.assertEqual(state['operationId'], 'a' * 32)
        self.assertFalse(state['workerActive'])
        self.assertEqual(hashlib.sha256(self.journal_path.read_bytes()).hexdigest(), before)

    def test_optin_corrupt_journal_remains_blocked(self):
        journal = DurableModelJournal(self.journal_path)
        journal.provision_new()
        self.journal_path.write_text('{', encoding='utf-8')
        self.journal_path.chmod(0o600)
        monitor = self.monitor(durable=True)
        self.assertTrue(monitor.model_control.read()['durableJournal']['blocked'])

    def test_non_boolean_optin_is_refused_before_socket_open(self):
        with self.assertRaisesRegex(ValueError, 'boolean'):
            self.monitor(durable='yes')

    def test_cli_flag_selects_durable_constructor_without_startup_actions(self):
        class Control:
            def __init__(self):
                self.calls = []
            def cancel(self):
                self.calls.append('cancel')
            def join(self, timeout):
                self.calls.append(('join', timeout))
        class FakeServer:
            server_address = ('127.0.0.1', 12345)
            online_code_repair = None
            auto_unloader = None
            stopping = threading.Event()
            model_control = Control()
            def serve_forever(self, **_):
                pass
            def server_close(self):
                pass
        class NoThread:
            def __init__(self, *_, **__):
                pass
            def start(self):
                pass
        fake = FakeServer()
        with patch.object(server_source, 'MonitorServer', return_value=fake) as ctor, \
             patch.object(server_source, 'memory_guard', return_value=None), \
             patch.object(server_source.threading, 'Thread', NoThread), \
             patch.object(server_source, 'write_endpoint'), \
             patch.object(server_source, 'stop_windows_worker_probe'), \
             patch.object(server_source.signal, 'signal'), \
             patch.object(sys, 'argv', ['server.py', '--durable-model-control']), \
             redirect_stdout(io.StringIO()):
            server_source.main()
        self.assertEqual(ctor.call_args.kwargs, {'durable_model_control': True})
        self.assertEqual(fake.model_control.calls, ['cancel', ('join', .45)])


if __name__ == '__main__':
    unittest.main()
