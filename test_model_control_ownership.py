"""Owned model operations; every runner and process here is a fake."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from model_control import ControlError, ModelControl, _Operation, _run_cli
from test_model_control import MutableStore, inventory, unloaded, memory


class FakeProcess:
    pid = 765432
    def __init__(self):
        self.stdout = io.BytesIO()
        self.returncode = None
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        return self.returncode


class OwnershipTests(unittest.TestCase):
    def test_cancel_before_worker_dispatch_never_calls_runner(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        @contextmanager
        def guard():
            entered.set()
            self.assertTrue(release.wait(2))
            yield
        control = ModelControl(MutableStore(inventory()), runner=lambda *args: calls.append(args))
        accepted = control.request('unload', 'publisher/model', action_guard=guard)
        self.assertTrue(entered.wait(1))
        self.assertTrue(control.cancel(accepted['operationId'])['cancellationRequested'])
        self.assertFalse(control.join(.01, operation_id=accepted['operationId']))
        with self.assertRaises(ControlError):
            control.request('unload', 'publisher/model')
        release.set()
        self.assertTrue(control.join(1))
        self.assertEqual(calls, [])
        self.assertEqual(control.read()['settlement'], 'not-started')
        self.assertFalse(control.read()['workerActive'])

    def test_cancellation_before_popen_has_no_child(self):
        operation = _Operation('before-popen')
        def find():
            operation.cancel()
            return '/fake/lms'
        with patch('model_control._find_lms', side_effect=find), \
             patch('model_control.subprocess.Popen') as popen:
            with self.assertRaisesRegex(ControlError, 'cancelled'):
                _run_cli('unload', 'publisher/model', operation=operation)
        popen.assert_not_called()
        self.assertFalse(operation.attempted)

    def test_cancellation_during_popen_kills_child_on_registration(self):
        entered, release = threading.Event(), threading.Event()
        process = FakeProcess()
        def popen(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(2))
            return process
        def kill(pid, signal):
            self.assertEqual(pid, process.pid)
            process.returncode = -9
        control = ModelControl(MutableStore(inventory()))
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', side_effect=popen), \
             patch('model_control.selectors.DefaultSelector'), \
             patch('model_control.os.killpg', side_effect=kill) as killed:
            op = control.request('unload', 'publisher/model')['operationId']
            self.assertTrue(entered.wait(1))
            control.cancel(op)
            killed.assert_not_called()
            release.set()
            self.assertTrue(control.join(1, operation_id=op))
            killed.assert_called_once()
        self.assertEqual(control.read()['settlement'], 'unconfirmed')
        self.assertTrue(control.read()['cleanupConfirmed'])
        self.assertTrue(process.stdout.closed)

    def test_cancellation_during_cli_signals_tracked_process(self):
        selecting = threading.Event()
        process = FakeProcess()
        def select(timeout):
            selecting.set()
            time.sleep(.005)
            return []
        def kill(pid, signal):
            process.returncode = -9
        control = ModelControl(MutableStore(inventory()))
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', return_value=process), \
             patch('model_control.selectors.DefaultSelector') as selector, \
             patch('model_control.os.killpg', side_effect=kill) as killed:
            selector.return_value.select.side_effect = select
            op = control.request('unload', 'publisher/model')['operationId']
            self.assertTrue(selecting.wait(1))
            control.cancel(op)
            self.assertTrue(control.join(1))
            killed.assert_called_once()
        self.assertEqual(control.read()['status'], 'failed')
        self.assertEqual(control.read()['settlement'], 'unconfirmed')

    def test_cancel_after_command_keeps_admission_closed_until_positive_reconciliation(self):
        store = MutableStore(inventory())
        called = threading.Event()
        control = ModelControl(store, runner=lambda *args: called.set())
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(called.wait(1))
        control.cancel(op)
        self.assertTrue(control.join(1))
        self.assertEqual(control.read()['settlement'], 'unconfirmed')
        with self.assertRaises(ControlError):
            control.request('unload', 'publisher/model')
        with self.assertRaises(ControlError):
            control.reconcile(op)  # opposite state cannot certify settlement
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        reconciled = control.reconcile(op)
        self.assertEqual(reconciled['settlement'], 'confirmed')
        self.assertEqual(reconciled['status'], 'failed')  # preserve cancellation history
        self.assertTrue(reconciled['cancellationRequested'])

    def test_cancel_waiting_custom_runner_does_not_claim_worker_stopped(self):
        entered, release = threading.Event(), threading.Event()
        def runner(*args):
            entered.set()
            self.assertTrue(release.wait(2))
        control = ModelControl(MutableStore(inventory()), runner=runner)
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(entered.wait(1))
        control.cancel(op)
        self.assertFalse(control.join(.01))
        self.assertTrue(control.read()['workerActive'])
        with self.assertRaises(ControlError):
            control.request('unload', 'publisher/model')
        release.set()
        self.assertTrue(control.join(1))
        self.assertEqual(control.read()['settlement'], 'unconfirmed')

    def test_stale_and_wrong_operation_reconciliation_refused(self):
        control = ModelControl(MutableStore(inventory()), runner=lambda *args: None)
        op = control.request('unload', 'publisher/model')['operationId']
        control.cancel(op)
        self.assertTrue(control.join(1))
        # Dispatch scheduling may cancel before the runner; this case requires
        # the unconfirmed state and is covered deterministically in other tests.
        for method in (control.cancel, control.reconcile):
            with self.assertRaises(ControlError):
                method('wrong-operation')
        with self.assertRaises(ControlError):
            control.join(.01, operation_id='wrong-operation')
        for timeout in (-1, float('nan'), float('inf'), True):
            with self.assertRaises(ValueError):
                control.join(timeout)

    def test_timeout_blocks_second_operation_and_stale_positive_evidence(self):
        store = MutableStore(inventory())
        def runner(*args):
            raise ControlError(504, 'LM Studio command timed out.')
        control = ModelControl(store, runner=runner)
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(control.join(1))
        self.assertEqual(control.read()['settlement'], 'unconfirmed')
        with self.assertRaises(ControlError):
            control.request('unload', 'publisher/model')
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api', sampled=time.time()-10)
        with self.assertRaises(ControlError):
            control.reconcile(op)
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        store.data['models'][0]['ageSeconds'] = 10
        with self.assertRaises(ControlError):
            control.reconcile(op)

    def test_worker_start_failure_leaves_no_running_operation(self):
        control = ModelControl(MutableStore(inventory()), runner=lambda *args: self.fail('runner called'))
        with patch('model_control.threading.Thread.start', side_effect=RuntimeError('no thread')):
            with self.assertRaises(RuntimeError):
                control.request('unload', 'publisher/model')
        self.assertEqual(control.read()['settlement'], 'not-started')
        self.assertFalse(control.read()['workerActive'])
        self.assertTrue(control.join(0))

    def test_cancel_after_confirmation_before_guard_exit_remains_unconfirmed(self):
        store = MutableStore(inventory())
        exiting, release = threading.Event(), threading.Event()
        @contextmanager
        def guard():
            yield
            exiting.set()
            self.assertTrue(release.wait(2))
        control = ModelControl(store, runner=lambda *args: None)
        with patch('model_control._observed', return_value=True):
            op = control.request('unload', 'publisher/model', action_guard=guard)['operationId']
            self.assertTrue(exiting.wait(1))
            control.cancel(op)
            release.set()
            self.assertTrue(control.join(1))
        self.assertEqual(control.read()['status'], 'failed')
        self.assertEqual(control.read()['settlement'], 'unconfirmed')

    def test_load_reconciliation_requires_exact_fresh_single_instance(self):
        store = MutableStore(unloaded(mem=memory()))
        called = threading.Event()
        control = ModelControl(store, runner=lambda *args: called.set())
        op = control.request('load', 'publisher/model')['operationId']
        self.assertTrue(called.wait(1))
        control.cancel(op)
        self.assertTrue(control.join(1))
        store.data = inventory()
        store.data['models'][0]['loadedInstanceIds'] = ['publisher/model', 'renamed']
        with self.assertRaises(ControlError):
            control.reconcile(op)
        store.data = inventory()
        store.data['models'].append(dict(store.data['models'][0]))
        with self.assertRaises(ControlError):
            control.reconcile(op)
        store.data = inventory()
        self.assertEqual(control.reconcile(op)['settlement'], 'confirmed')

    def test_failed_popen_never_marks_dispatch(self):
        control = ModelControl(MutableStore(inventory()))
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', side_effect=OSError('no launch')):
            control.request('unload', 'publisher/model')
            self.assertTrue(control.join(1))
        self.assertEqual(control.read()['settlement'], 'not-started')

    def test_selector_setup_failure_reaps_registered_child(self):
        process = FakeProcess()
        operation = _Operation('setup-failure')
        def kill(pid, signal):
            process.returncode = -9
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', return_value=process), \
             patch('model_control.selectors.DefaultSelector', side_effect=OSError('setup failed')), \
             patch('model_control.os.killpg', side_effect=kill) as killed:
            with self.assertRaises(OSError):
                _run_cli('unload', 'publisher/model', operation=operation)
            killed.assert_called_once()
        self.assertTrue(operation.attempted)
        self.assertIsNone(operation.process)
        self.assertTrue(process.stdout.closed)

    def test_failed_child_cleanup_retains_uncertainty_and_child_identity(self):
        process = FakeProcess()
        store = MutableStore(inventory())
        control = ModelControl(store)
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', return_value=process), \
             patch('model_control.selectors.DefaultSelector', side_effect=OSError('setup failed')), \
             patch('model_control.os.killpg', side_effect=OSError('cannot stop')):
            op = control.request('unload', 'publisher/model')['operationId']
            self.assertTrue(control.join(1))
        self.assertFalse(control.read()['cleanupConfirmed'])
        self.assertIs(control._operation.process, process)
        self.assertEqual(control.read()['settlement'], 'unconfirmed')
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        with self.assertRaises(ControlError):
            control.reconcile(op)
        with self.assertRaises(ControlError):
            control.request('unload', 'publisher/model')

    def test_cancel_thread_never_signals_or_reaps(self):
        operation = _Operation('single-owner')
        process = FakeProcess()
        operation.register(process)
        with patch('model_control.os.killpg') as kill, \
             patch.object(process, 'poll') as poll, patch.object(process, 'wait') as wait:
            operation.cancel()
        kill.assert_not_called()
        poll.assert_not_called()
        wait.assert_not_called()
        self.assertTrue(operation.cancelled.is_set())

    def test_cancellation_racing_reap_never_signals_reusable_pgid(self):
        operation = _Operation('cancel-at-reap')
        process = FakeProcess()
        def reaped(timeout=None):
            process.returncode = 0
            operation.cancel()
            return 0
        with patch('model_control._find_lms', return_value='/fake/lms'), \
             patch('model_control.subprocess.Popen', return_value=process), \
             patch('model_control.selectors.DefaultSelector') as selector, \
             patch('model_control.os.read', return_value=b''), \
             patch.object(process.stdout, 'fileno', return_value=99), \
             patch.object(process, 'wait', side_effect=reaped), \
             patch('model_control.os.killpg') as kill:
            selector.return_value.select.return_value = [object()]
            with self.assertRaisesRegex(ControlError, 'cancelled'):
                _run_cli('unload', 'publisher/model', operation=operation)
        kill.assert_not_called()
        self.assertFalse(operation.group_signal_sent)
        self.assertIsNone(operation.process)

    def test_reconcile_reads_store_without_holding_controller_lock(self):
        store = MutableStore(inventory())
        def runner(*args):
            raise ControlError(504, 'timeout')
        control = ModelControl(store, runner=runner)
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(control.join(1))
        class ReentrantStore:
            def read(self):
                self_status = control.read()  # deadlocks if reconcile owns lock
                self_outer.assertEqual(self_status['operationId'], op)
                return inventory(loaded=False, state='unloaded', source='lmstudio-api')
        self_outer = self
        control.store = ReentrantStore()
        done = threading.Event()
        errors = []
        def reconcile():
            try:
                control.reconcile(op)
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()
        threading.Thread(target=reconcile, daemon=True).start()
        self.assertTrue(done.wait(1), 'store read must not hold controller lock')
        self.assertEqual(errors, [])

    def test_reconcile_rechecks_status_after_unlocked_store_read(self):
        store = MutableStore(inventory())
        def runner(*args):
            raise ControlError(504, 'timeout')
        control = ModelControl(store, runner=runner)
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(control.join(1))
        class ChangingStore:
            def read(self):
                with control.lock:
                    control.status['message'] = 'changed while snapshot was read'
                return inventory(loaded=False, state='unloaded', source='lmstudio-api')
        control.store = ChangingStore()
        with self.assertRaisesRegex(ControlError, 'changed during reconciliation'):
            control.reconcile(op)

    def test_missing_unloaded_row_is_not_settlement_proof(self):
        store = MutableStore(inventory())
        def runner(*args):
            raise ControlError(504, 'timeout')
        control = ModelControl(store, runner=runner)
        op = control.request('unload', 'publisher/model')['operationId']
        self.assertTrue(control.join(1))
        store.data = inventory(loaded=False, state='unloaded', source='lmstudio-api')
        store.data['models'] = []
        with self.assertRaises(ControlError):
            control.reconcile(op)


class RealSubprocessTests(unittest.TestCase):
    """Local short-lived Python scripts only; never the LM Studio executable."""
    def test_cancellation_signals_group_member_after_leader_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = root / 'child.json'
            script = root / 'local-cli'
            script.write_text('#!' + sys.executable + '\n' +
                              'import subprocess,sys,json,os\n' +
                              "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])\n" +
                              f"open({str(receipt)!r},'w').write(json.dumps({{'child':child.pid,'leader':os.getpid()}}))\n")
            script.chmod(0o700)
            control = ModelControl(MutableStore(inventory()))
            with patch('model_control._find_lms', return_value=str(script)):
                op = control.request('unload', 'publisher/model')['operationId']
                deadline = time.monotonic() + 2
                while not receipt.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(receipt.exists())
                data = json.loads(receipt.read_text())
                # Observe without waitpid/poll: do not reap the CLI leader.
                leader_state = ''
                while time.monotonic() < deadline:
                    leader_state = subprocess.check_output(
                        ['/bin/ps', '-o', 'stat=', '-p', str(data['leader'])], text=True).strip()
                    if leader_state.startswith('Z'):
                        break
                    time.sleep(.01)
                self.assertTrue(leader_state.startswith('Z'), leader_state)
                control.cancel(op)
                self.assertTrue(control.join(.45, operation_id=op))
            state = control.read()
            self.assertEqual(state['settlement'], 'unconfirmed')
            self.assertTrue(state['cleanupConfirmed'])
            self.assertEqual(state['cleanupScope'], 'cli-leader')
            self.assertTrue(state['groupSignalSent'])
            deadline = time.monotonic() + 1
            while True:
                child = subprocess.run(['/bin/ps', '-o', 'stat=', '-p', str(data['child'])],
                                       capture_output=True, text=True)
                if child.returncode != 0 or child.stdout.strip().startswith('Z'):
                    break
                self.assertLess(time.monotonic(), deadline, 'group member did not exit after signal')
                time.sleep(.01)

    def test_cancel_after_output_eof_interrupts_bounded_leader_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / 'local-cli'
            script.write_text('#!' + sys.executable + '\nimport os,time\nos.close(1)\nos.close(2)\ntime.sleep(5)\n')
            script.chmod(0o700)
            control = ModelControl(MutableStore(inventory()))
            with patch('model_control._find_lms', return_value=str(script)):
                op = control.request('unload', 'publisher/model')['operationId']
                time.sleep(.15)
                control.cancel(op)
                self.assertTrue(control.join(.45, operation_id=op))
            self.assertTrue(control.read()['cleanupConfirmed'])
            self.assertEqual(control.read()['settlement'], 'unconfirmed')

if __name__ == '__main__':
    unittest.main()
