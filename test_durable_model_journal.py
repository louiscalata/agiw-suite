"""Crash/restart safety checks using fake runners and synthetic inventory only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from durable_model_journal import DurableModelJournal, JournalError
from model_control import ControlError, ModelControl


MODEL = "publisher/model"


def inventory(*, loaded=True, sampled=None):
    return {
        "sampledAt": time.time() if sampled is None else sampled,
        "sources": [{"id": "lmstudio-api", "state": "live"},
                    {"id": "lms-ps", "state": "live"}],
        "models": [{"id": MODEL, "name": MODEL, "host": "mac", "modelKey": MODEL,
                    "source": "lms-ps" if loaded else "lmstudio-api",
                    "instanceId": MODEL if loaded else None,
                    "loadedInstanceIds": [MODEL] if loaded else [],
                    "loaded": loaded, "state": "idle" if loaded else "unloaded",
                    "queued": 0, "ageSeconds": 0}],
    }


def aliased_load_inventory():
    result = inventory(loaded=True)
    result["models"] = [
        {**result["models"][0], "source": "lmstudio-api", "instanceId": None,
         "loadedInstanceIds": ["instance-1"]},
        {**result["models"][0], "id": "instance-1", "source": "lms-ps",
         "instanceId": "instance-1", "loadedInstanceIds": None},
    ]
    return result


class Store:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot if snapshot is not None else inventory()

    def read(self):
        return self.snapshot


class DurableJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "model-operation.json"
        self.journal = DurableModelJournal(self.path)

    def provision(self):
        self.journal.provision_new()

    def pending(self, *, cleanup=False):
        self.provision()
        begun = self.journal.begin(operation_id="a" * 32, action="unload",
                                   model_id=MODEL, model_key=MODEL,
                                   started_at=time.time())
        if cleanup:
            self.journal.finish(operation_id="a" * 32, action="unload",
                                model_id=MODEL, model_key=MODEL,
                                generation=begun["generation"], finished_at=time.time(),
                                cleanup_confirmed=True)
        return begun

    def test_missing_journal_fails_closed_without_runner(self):
        calls = []
        control = ModelControl(Store(), runner=lambda *args: calls.append(args), journal_path=self.path)
        self.assertTrue(control.read()["durableJournal"]["blocked"])
        self.assertEqual(control.read()["settlement"], "unconfirmed")
        self.assertFalse(control.read()["cleanupConfirmed"])
        with self.assertRaises(ControlError):
            control.request("unload", MODEL)
        self.assertEqual(calls, [])
        self.assertFalse(self.path.exists(), "constructor must not silently provision a journal")

    def test_corrupt_duplicate_and_symlinked_journals_fail_closed(self):
        self.provision()
        for raw in ('{', '{"schema":1,"schema":1}', '{"schema":NaN}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                self.path.chmod(0o600)
                control = ModelControl(Store(), runner=lambda *_: self.fail("runner called"),
                                       journal_path=self.path)
                with self.assertRaises(ControlError):
                    control.request("unload", MODEL)
                self.assertTrue(control.read()["durableJournal"]["blocked"])
                self.assertFalse(control.read()["cleanupConfirmed"])
        self.path.unlink()
        target = Path(self.temp.name) / "target"
        target.write_text("{}")
        target.chmod(0o600)
        self.path.symlink_to(target)
        control = ModelControl(Store(), runner=lambda *_: self.fail("runner called"),
                               journal_path=self.path)
        with self.assertRaises(ControlError):
            control.request("unload", MODEL)

    def test_missing_lockfile_fails_closed_even_with_ready_journal(self):
        self.provision()
        self.journal.lock_path.unlink()
        control = ModelControl(Store(), runner=lambda *_: self.fail("runner called"),
                               journal_path=self.path)
        self.assertTrue(control.read()["durableJournal"]["blocked"])
        self.assertFalse(control.read()["cleanupConfirmed"])
        with self.assertRaises(ControlError):
            control.request("unload", MODEL)

    def test_rejected_provision_does_not_recreate_a_missing_lock(self):
        self.provision()
        self.journal.lock_path.unlink()
        with self.assertRaisesRegex(JournalError, "Existing journal"):
            self.journal.provision_new()
        self.assertFalse(self.journal.lock_path.exists())
        with self.assertRaises(JournalError):
            self.journal.read()
        control = ModelControl(Store(), runner=lambda *_: self.fail("runner called"),
                               journal_path=self.path)
        self.assertTrue(control.read()["durableJournal"]["blocked"])
        with self.assertRaises(ControlError):
            control.request("unload", MODEL)

    def test_startup_journal_failure_never_reports_cleanup_confirmed(self):
        # There is no restored operation owner when a journal cannot be read.
        # The status field must not turn that absence into affirmative cleanup.
        self.provision()
        self.path.write_text("{damaged")
        self.path.chmod(0o600)
        control = ModelControl(Store(), journal_path=self.path)
        state = control.read()
        self.assertEqual(state["settlement"], "unconfirmed")
        self.assertTrue(state["durableJournal"]["blocked"])
        self.assertFalse(state["cleanupConfirmed"])
        self.assertIsNone(state["operationId"])

    def test_provision_is_explicit_and_cannot_replace_existing_state(self):
        self.provision()
        with self.assertRaises(JournalError):
            self.journal.provision_new()
        self.pending_with_existing = self.journal.begin(
            operation_id="b" * 32, action="unload", model_id=MODEL,
            model_key=MODEL, started_at=time.time())
        with self.assertRaises(JournalError):
            self.journal.provision_new()
        self.assertEqual(self.journal.read()["phase"], "pending")

    def test_crash_after_admission_retains_exact_identity_and_blocks_even_with_desired_inventory(self):
        begun = self.pending(cleanup=False)
        store = Store(inventory(loaded=False))
        control = ModelControl(store, runner=lambda *_: self.fail("runner called"),
                               journal_path=self.path)
        state = control.read()
        self.assertEqual((state["operationId"], state["action"], state["modelId"]),
                         ("a" * 32, "unload", MODEL))
        self.assertEqual(state["settlement"], "unconfirmed")
        self.assertFalse(state["cleanupConfirmed"])
        time.sleep(.012)
        store.snapshot = inventory(loaded=False)
        with self.assertRaisesRegex(ControlError, "cleanup"):
            control.reconcile("a" * 32)
        with self.assertRaises(ControlError):
            control.request("unload", MODEL)
        self.assertEqual(self.journal.read(), begun)

    def test_real_process_exit_during_fake_dispatch_keeps_write_ahead_pending(self):
        self.provision()
        script = ("import os,time\n"
                  "from model_control import ModelControl\n"
                  "from test_durable_model_journal import Store,MODEL\n"
                  f"control=ModelControl(Store(),runner=lambda *_: os._exit(42),journal_path={str(self.path)!r})\n"
                  "control.request('unload',MODEL)\n"
                  "time.sleep(3)\n")
        child = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).parent,
                               capture_output=True, text=True, timeout=5,
                               env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
        self.assertEqual(child.returncode, 42, child.stderr)
        saved = self.journal.read()
        self.assertEqual(saved["phase"], "pending")
        self.assertEqual(saved["operation"]["action"], "unload")
        self.assertEqual(saved["operation"]["modelId"], MODEL)
        self.assertFalse(saved["operation"]["cleanupConfirmed"])
        restarted = ModelControl(Store(inventory(loaded=False)),
                                 runner=lambda *_: self.fail("runner called"), journal_path=self.path)
        time.sleep(.012)
        restarted.store.snapshot = inventory(loaded=False)
        with self.assertRaises(ControlError):
            restarted.reconcile(saved["operation"]["operationId"])
        with self.assertRaises(ControlError):
            restarted.request("unload", MODEL)

    def test_restart_requires_cleanup_and_a_new_exact_positive_observation(self):
        begun = self.pending(cleanup=True)
        store = Store(inventory(loaded=True))
        control = ModelControl(store, runner=lambda *_: None, journal_path=self.path)
        with self.assertRaises(ControlError):
            control.reconcile("wrong")
        with self.assertRaises(ControlError):
            control.reconcile("a" * 32)  # opposite state is insufficient
        store.snapshot = inventory(loaded=False, sampled=begun["operation"]["startedAt"] - 1)
        with self.assertRaises(ControlError):
            control.reconcile("a" * 32)  # stale observation is insufficient
        time.sleep(.012)
        store.snapshot = inventory(loaded=False)
        reconciled = control.reconcile("a" * 32)
        self.assertEqual(reconciled["settlement"], "confirmed")
        saved = self.journal.read()
        self.assertEqual(saved["phase"], "confirmed")
        self.assertEqual(saved["operation"]["operationId"], "a" * 32)
        self.assertGreater(saved["operation"]["observedAt"], saved["operation"]["finishedAt"])
        after_restart = ModelControl(Store(), runner=lambda *_: None, journal_path=self.path)
        self.assertEqual(after_restart.read()["status"], "idle")

    def test_snapshot_from_before_restart_cannot_clear_latch_even_if_currently_fresh(self):
        self.pending(cleanup=True)
        before_restart = inventory(loaded=False)
        control = ModelControl(Store(before_restart), runner=lambda *_: None, journal_path=self.path)
        self.assertLess(time.time() - before_restart["sampledAt"], 3)
        with self.assertRaisesRegex(ControlError, "post-restart"):
            control.reconcile("a" * 32)
        self.assertEqual(control.read()["settlement"], "unconfirmed")
        self.assertFalse(control.read()["durableJournal"]["blocked"])
        time.sleep(.012)
        control.store.snapshot = inventory(loaded=False)
        self.assertEqual(control.reconcile("a" * 32)["settlement"], "confirmed")

    def test_recovered_load_requires_one_exact_instance_not_ambiguous_inventory(self):
        self.provision()
        begun = self.journal.begin(operation_id="c" * 32, action="load",
                                   model_id=MODEL, model_key=MODEL,
                                   started_at=time.time())
        self.journal.finish(operation_id="c" * 32, action="load", model_id=MODEL,
                            model_key=MODEL, generation=begun["generation"],
                            finished_at=time.time(), cleanup_confirmed=True)
        store = Store(inventory(loaded=True))
        control = ModelControl(store, runner=lambda *_: None, journal_path=self.path)
        time.sleep(.012)
        store.snapshot = inventory(loaded=True)
        store.snapshot["models"][0]["loadedInstanceIds"] = [MODEL, "other-instance"]
        with self.assertRaises(ControlError):
            control.reconcile("c" * 32)
        store.snapshot = inventory(loaded=True)
        store.snapshot["models"][0]["modelKey"] = "wrong/model"
        with self.assertRaises(ControlError):
            control.reconcile("c" * 32)
        store.snapshot = inventory(loaded=True)
        self.assertEqual(control.reconcile("c" * 32)["settlement"], "confirmed")

    def test_recovered_load_accepts_a_distinct_instance_id_only_when_api_and_cli_agree(self):
        self.provision()
        begun = self.journal.begin(operation_id="d" * 32, action="load",
                                   model_id=MODEL, model_key=MODEL,
                                   started_at=time.time())
        self.journal.finish(operation_id="d" * 32, action="load", model_id=MODEL,
                            model_key=MODEL, generation=begun["generation"],
                            finished_at=time.time(), cleanup_confirmed=True)
        store = Store(aliased_load_inventory())
        control = ModelControl(store, runner=lambda *_: self.fail("runner called"), journal_path=self.path)
        time.sleep(.012)
        store.snapshot = aliased_load_inventory()
        store.snapshot["models"][1]["instanceId"] = "different-instance"
        with self.assertRaises(ControlError):
            control.reconcile("d" * 32)
        store.snapshot = aliased_load_inventory()
        store.snapshot["models"][0]["loadedInstanceIds"] = ["instance-1", "instance-2"]
        with self.assertRaises(ControlError):
            control.reconcile("d" * 32)
        store.snapshot = aliased_load_inventory()
        store.snapshot["models"].append(
            {**store.snapshot["models"][1], "id": "instance-2", "instanceId": "instance-2"})
        with self.assertRaises(ControlError):
            control.reconcile("d" * 32)
        store.snapshot = aliased_load_inventory()
        self.assertEqual(control.reconcile("d" * 32)["settlement"], "confirmed")
        self.assertEqual(self.journal.read()["operation"]["operationId"], "d" * 32)

    def test_wrong_identity_or_generation_cannot_finish_or_reconcile(self):
        begun = self.pending(cleanup=False)
        params = dict(operation_id="a" * 32, action="unload", model_id=MODEL,
                      model_key=MODEL, generation=begun["generation"])
        for changed in ({"operation_id": "b" * 32}, {"action": "load"},
                        {"model_id": "other/model"}, {"model_key": "other/model"},
                        {"generation": 99}):
            with self.subTest(changed=changed), self.assertRaises(JournalError):
                self.journal.finish(**{**params, **changed}, finished_at=time.time(),
                                    cleanup_confirmed=True)
        self.assertEqual(self.journal.read(), begun)

    def test_two_monitors_cannot_both_admit_from_ready_state(self):
        self.provision()
        started, release = threading.Event(), threading.Event()
        calls = []
        def runner(*args):
            calls.append(args)
            started.set()
            release.wait(2)
        store = Store()
        first = ModelControl(store, runner=runner, journal_path=self.path)
        second = ModelControl(store, runner=lambda *_: self.fail("second runner called"),
                              journal_path=self.path)
        accepted = first.request("unload", MODEL)
        self.assertTrue(started.wait(1))
        with self.assertRaises(ControlError):
            second.request("unload", MODEL)
        self.assertEqual(self.journal.read()["operation"]["operationId"], accepted["operationId"])
        first.cancel(accepted["operationId"])
        release.set()
        self.assertTrue(first.join(1))
        self.assertEqual(calls, [("unload", MODEL)])

    def test_failed_pre_dispatch_journal_write_never_calls_runner(self):
        self.provision()
        calls = []
        control = ModelControl(Store(), runner=lambda *args: calls.append(args),
                               journal_path=self.path)
        with patch.object(control.journal, "_write_locked", side_effect=JournalError("disk fault")):
            with self.assertRaises(ControlError):
                control.request("unload", MODEL)
        self.assertEqual(calls, [])
        self.assertTrue(control.read()["durableJournal"]["blocked"])
        self.assertEqual(self.journal.read()["phase"], "ready")

    def test_failed_final_write_leaves_pending_across_restart(self):
        self.provision()
        control = ModelControl(Store(), runner=lambda *_: None, journal_path=self.path)
        with patch.object(control.journal, "finish", side_effect=JournalError("disk fault")):
            op = control.request("unload", MODEL)["operationId"]
            control.cancel(op)
            self.assertTrue(control.join(1))
        self.assertEqual(control.read()["settlement"], "unconfirmed")
        self.assertTrue(control.read()["durableJournal"]["blocked"])
        restarted = ModelControl(Store(inventory(loaded=False)), runner=lambda *_: self.fail("runner called"),
                                 journal_path=self.path)
        self.assertEqual(restarted.read()["operationId"], op)
        with self.assertRaises(ControlError):
            restarted.request("unload", MODEL)
        with self.assertRaises(ControlError):
            restarted.reconcile(op)

    def test_success_requires_inventory_and_persists_confirmation_before_next_admission(self):
        self.provision()
        store = Store()
        fake_cli = Path(self.temp.name) / "fake-lms"
        fake_cli.write_text("#!" + sys.executable + "\nprint('fake local command')\n")
        fake_cli.chmod(0o700)
        control = ModelControl(store, journal_path=self.path)
        with patch("model_control._find_lms", return_value=str(fake_cli)):
            op = control.request("unload", MODEL)["operationId"]
            deadline = time.monotonic() + 1
            while "waiting for fresh inventory" not in control.read()["message"] and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertIn("waiting for fresh inventory", control.read()["message"])
            time.sleep(.012)
            store.snapshot = inventory(loaded=False)
            self.assertTrue(control.join(1))
        self.assertEqual(control.read()["settlement"], "confirmed")
        self.assertEqual(self.journal.read()["phase"], "confirmed")
        restarted = ModelControl(Store(), runner=lambda *_: None, journal_path=self.path)
        self.assertEqual(restarted.read()["status"], "idle")

    def test_opaque_injected_runner_return_is_not_owner_cleanup_evidence(self):
        self.provision()
        store = Store()
        entered = threading.Event()
        control = ModelControl(store, runner=lambda *_: entered.set(), journal_path=self.path)
        control.request("unload", MODEL)
        self.assertTrue(entered.wait(1))
        deadline = time.monotonic() + 1
        while "waiting for fresh inventory" not in control.read()["message"] and time.monotonic() < deadline:
            time.sleep(.005)
        store.snapshot = inventory(loaded=False)
        self.assertTrue(control.join(1))
        self.assertEqual(control.read()["settlement"], "unconfirmed")
        self.assertEqual(self.journal.read()["phase"], "pending")
        self.assertFalse(self.journal.read()["operation"]["cleanupConfirmed"])


if __name__ == "__main__":
    unittest.main()
