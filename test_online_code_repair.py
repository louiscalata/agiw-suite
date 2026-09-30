"""Focused owner-bound checks for the explicit Online Code Mode control."""

import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

import online_code_repair as repair
import telemetry


RUN = "bounded-preflight-20260924"
DIGEST = "a" * 64
JOB = "mac-test-1234567890"


def reply(value, code=0):
    raw = b"" if value is None else json.dumps(value).encode("utf-8")
    return repair.CommandResult(code, raw)


def entry_result(status="ready", evidence="fresh-launcher-receipt", code=0):
    return reply({"schemaVersion": 1, "operation": "readiness", "status": status,
                  "evidence": evidence}, code)


def idle():
    return reply({"schemaVersion": 1, "active": None})


def windows(pending=None, ready=True):
    return reply({"kind": "codemode.windows.status.v1", "pending": pending,
                  "readyForWork": ready, "inventory": {"ok": ready},
                  "inference": "NOT_RUN"}, 0 if ready and pending is None else 3)


def nisi(pending=False):
    return reply({"kind": "codemode.integrations.v1",
                  "nisi": {"status": "ADAPTER_PRESENT", "recoveryRequired": pending}})


def bridge(*, inventory="LISTED", recovery=False, binding="VERIFIED", drifted=None):
    return {"schemaVersion": 1, "kind": "agiw.nisi-v02.bridge-check.v1",
            "status": "RETURNED", "inventoryStatus": inventory,
            "runtimeCount": 1, "modelCount": 9,
            "recoveryRequired": recovery, "activationHostBinding": binding,
            "driftedHostFiles": list(drifted or []),
            "modelInference": "NOT_RUN", "workflowAcceptance": "NOT_RUN",
            "releaseAcceptance": "NOT_ESTABLISHED"}


def preflight_active():
    evidence = {"kind": "codemode.router.v1", "status": "NOT_RUN",
                "code": "NISI_RECOVERY_REQUIRED", "runId": RUN,
                "requestSha256": DIGEST, "recoveryRequired": True,
                "candidate": None, "selectedHost": None,
                "stages": {stage: {"status": "NOT_RUN"} for stage in repair.STAGES}}
    return {"schemaVersion": 1, "runId": RUN, "inputSha256": DIGEST,
            "stage": "incomplete", "sequence": 2,
            "checkpoint": {"runId": RUN, "inputSha256": DIGEST,
                           "stage": "incomplete", "sequence": 2,
                           "evidence": evidence}}


def active_status(active=None):
    return reply({"schemaVersion": 1, "active": active or preflight_active()})


def preflight_settled():
    return reply({"kind": "codemode.router.v1", "status": "NOT_RUN",
                  "code": "NISI_RECOVERY_REQUIRED", "runId": RUN,
                  "requestSha256": DIGEST, "recoveryRequired": False,
                  "reconciliation": {"kind": "codemode.router.preflight-reconcile.v1"}}, 3)


def prewarmed_script(path, body):
    """Write an owner executable and exec it once, untimed, before any deadline.

    macOS assesses a new executable on its first exec: measured 0.1-3 s on this
    Mac, against ~0.02 s for every later exec of the same file, so an unwarmed
    2 s deadline times the host, not the runner. The warm run exits before
    *body*, so it has no side effects.
    """
    path.write_text("#!/usr/bin/python3\nimport sys\n"
                    "if sys.argv[1:] == ['--prewarm']:\n    raise SystemExit(0)\n" + body)
    path.chmod(0o700)
    subprocess.run([str(path), "--prewarm"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=30, check=True)
    return path


class ScriptedRunner:
    def __init__(self, script, readiness_path=None, write_readiness=True):
        self.script = list(script)
        self.calls = []
        self.readiness_path = readiness_path
        self.write_readiness = write_readiness

    def __call__(self, args, data, timeout):
        self.calls.append((args, data, timeout))
        if not self.script:
            raise AssertionError("unexpected launcher operation")
        expected, result = self.script.pop(0)
        if args != expected:
            raise AssertionError(f"expected {expected}, got {args}")
        if (args in (repair.READINESS, repair.ENTRY_READINESS)
                and result.returncode == 0 and self.write_readiness
                and self.readiness_path is not None):
            self.readiness_path.write_text(json.dumps({
                "schemaVersion": 1, "status": "PREFLIGHT_COMPLETED",
                "observedAtUnix": time.time(), "source": "online-code-mode",
                "client": None, "chatId": None,
            }))
            self.readiness_path.chmod(0o600)
        return result


class OnlineCodeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.marker = Path(self.temporary.name) / "pending.json"
        self.receipt = Path(self.temporary.name) / "readiness.json"

    def test_default_owner_pauses_queue_calls_before_stalled_reader_can_repeat(self):
        with mock.patch.object(repair, "_windows_worker_reader_blocked", return_value=True), \
             mock.patch.object(repair, "launcher_runner",
                               side_effect=AssertionError("owner must not run")) as owner:
            controller = repair.OnlineCodeRepair()
            for args in (repair.WINDOWS_STATUS, repair.WINDOWS_RECONCILE,
                         repair.READINESS, repair.ENTRY_READINESS):
                with self.assertRaises(repair._Stop) as stopped:
                    controller._call(args)
                self.assertEqual(stopped.exception.status, "needs-action")
                self.assertIn("SharedChami worker status could not be read",
                              stopped.exception.message)
            owner.assert_not_called()
            self.assertEqual([step["result"] for step in controller.read()["steps"]],
                             ["paused"] * 4)
            self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
            repair._WINDOWS_WORKER_IO_LOCK.release()

    def test_default_owner_pauses_repeated_readiness_while_chami_ensure_child_is_uninterruptible(self):
        command = f"/usr/bin/python3 -I -S {repair.Path.home() / 'bin/chami-ensure'} --probe"
        listing = f"{os.getuid()} U {command}\n"
        controller = repair.OnlineCodeRepair()
        with mock.patch.object(telemetry, "_bounded_command", return_value=listing), \
             mock.patch.object(repair, "launcher_runner",
                               side_effect=AssertionError("must not launch a repeated SMB reader")) as owner:
            for _ in range(2):
                with self.assertRaises(repair._Stop) as stopped:
                    controller._call(repair.READINESS)
                self.assertIn("SharedChami worker status could not be read",
                              stopped.exception.message)
        owner.assert_not_called()
        self.assertEqual([step["result"] for step in controller.read()["steps"]],
                         ["paused", "paused"])
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        repair._WINDOWS_WORKER_IO_LOCK.release()

    def test_default_owner_releases_shared_gate_after_runner_timeout(self):
        with mock.patch.object(repair, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(repair, "launcher_runner",
                               side_effect=subprocess.TimeoutExpired(["owner"], 1)):
            controller = repair.OnlineCodeRepair()
            with self.assertRaises(subprocess.TimeoutExpired):
                controller._call(repair.WINDOWS_STATUS)
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        repair._WINDOWS_WORKER_IO_LOCK.release()

    def test_default_owner_does_not_overlap_passive_sharedchami_probe(self):
        controller = repair.OnlineCodeRepair()
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        try:
            with mock.patch.object(repair, "_windows_worker_reader_blocked",
                                   side_effect=AssertionError("must not proceed to process probe")), \
                 mock.patch.object(repair, "launcher_runner",
                                   side_effect=AssertionError("must not start another owner read")) as owner:
                with self.assertRaises(repair._Stop) as stopped:
                    controller._call(repair.WINDOWS_STATUS)
                self.assertEqual(stopped.exception.status, "needs-action")
                self.assertIn("already being read", stopped.exception.message)
                owner.assert_not_called()
        finally:
            repair._WINDOWS_WORKER_IO_LOCK.release()
        self.assertEqual(controller.read()["steps"], [
            {"name": "windows-preflight", "result": "paused"}])

    def test_button_and_passive_sampler_contend_without_overlapping_owner_reads(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        def owner(args, data, timeout):
            entered.set()
            release.wait(2.0)
            return windows()

        cache = {"checkedMonotonic": time.monotonic(), "heartbeatUnix": time.time() - 8,
                 "modelsAdvertised": ["gpt-oss-20b"], "modelCount": 1,
                 "probeRunning": False, "probeError": False,
                 "probePaused": False, "probeBusy": False,
                 "retryAfterMonotonic": 0.0}
        worker = threading.Thread(target=lambda: results.append(
            controller._call(repair.WINDOWS_STATUS)))
        try:
            with mock.patch.object(repair, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(repair, "launcher_runner", side_effect=owner):
                controller = repair.OnlineCodeRepair()
                worker.start()
                self.assertTrue(entered.wait(1.0))
                telemetry._windows_worker_refresh(cache)
                with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
                    projected, source = telemetry._windows_worker(time.time())
                self.assertTrue(cache["probeBusy"])
                self.assertFalse(cache["probePaused"])
                self.assertEqual(projected["state"], "advertised")
                self.assertEqual(projected["modelsAdvertised"], ["gpt-oss-20b"])
                self.assertIn("deferred", source["detail"])
                self.assertEqual(results, [])
                release.set()
                worker.join(timeout=2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results[0][0], 0)
        finally:
            release.set()
            worker.join(timeout=2.0)

    def test_default_owner_does_not_overlap_passive_sharedchami_probe(self):
        controller = repair.OnlineCodeRepair()
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        try:
            with mock.patch.object(repair, "_windows_worker_reader_blocked",
                                   side_effect=AssertionError("must not proceed to process probe")), \
                 mock.patch.object(repair, "launcher_runner",
                                   side_effect=AssertionError("must not start another owner read")) as owner:
                with self.assertRaises(repair._Stop) as stopped:
                    controller._call(repair.WINDOWS_STATUS)
                self.assertEqual(stopped.exception.status, "needs-action")
                self.assertIn("already being read", stopped.exception.message)
                owner.assert_not_called()
        finally:
            repair._WINDOWS_WORKER_IO_LOCK.release()
        self.assertEqual(controller.read()["steps"], [
            {"name": "windows-preflight", "result": "paused"}])

    def test_default_owner_runs_verified_windows_status_when_local_reader_clear(self):
        with mock.patch.object(repair, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(repair, "launcher_runner", return_value=windows()) as owner:
            controller = repair.OnlineCodeRepair()
            pending, ready = controller._windows_status()
        self.assertIsNone(pending)
        self.assertTrue(ready)
        owner.assert_called_once()

    def controller(self, script, *, write_readiness=True, sleep=None, monotonic=None,
                   bridge_probe=None):
        runner = ScriptedRunner(script, self.receipt, write_readiness)
        return repair.OnlineCodeRepair(runner, nisi_pending_path=self.marker,
                                       readiness_path=self.receipt, sleep=sleep,
                                       monotonic=monotonic,
                                       bridge_probe=bridge_probe or (lambda _: bridge())), runner

    def finish(self, controller, action="repair"):
        started = (controller.request_entry() if action == "entry" else
                   controller.request_fix(action.removeprefix("fix-")) if action.startswith("fix-") else
                   controller.request())
        self.assertEqual(started["status"], "running")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = controller.read()
            if state["status"] != "running":
                return state
            time.sleep(0.005)
        self.fail("background check did not finish")

    def test_idle_runs_one_capability_inventory_then_rechecks_all_owners(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready")
        self.assertIn("No model inference was run", state["message"])
        self.assertEqual([call[0] for call in runner.calls].count(repair.READINESS), 1)
        self.assertEqual(runner.script, [])
        self.assertEqual(controller.read(), state)
        self.assertEqual(state["steps"][-1]["name"], "nisi-v02-bridge")
        self.assertEqual(state["steps"][-1]["result"], "inventory-verified")

    def test_private_v02_partial_inventory_or_recovery_prevents_ready(self):
        script = [(repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
                  (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
                  (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
                  (repair.ROUTE_STATUS, idle())]
        for result in (bridge(inventory="PARTIAL"), bridge(recovery=True)):
            with self.subTest(result=result):
                controller, runner = self.controller(script, bridge_probe=lambda _, result=result: result)
                state = self.finish(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(state["steps"][-1]["result"], "needs-action")
                self.assertIn("No model inference was run", state["message"])
                self.assertEqual(runner.script, [])

    def test_private_v02_listed_inventory_cannot_hide_activation_binding_drift(self):
        script = [(repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
                  (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
                  (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
                  (repair.ROUTE_STATUS, idle())]
        for binding, phrase in (("DRIFT", "pipeline_integrations.py"),
                                ("UNKNOWN", "activation receipt")):
            with self.subTest(binding=binding):
                controller, runner = self.controller(
                    script, bridge_probe=lambda _, binding=binding: bridge(
                        binding=binding,
                        drifted=["pipeline_integrations.py"] if binding == "DRIFT" else []))
                state = self.finish(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(state["steps"][-1]["result"], "needs-action")
                self.assertIn(f"activationPin={binding}", state["steps"][-1]["evidence"])
                self.assertIn(phrase, state["message"])
                self.assertIn("No model inference was run", state["message"])
                self.assertEqual(runner.script, [])

    def test_private_v02_bridge_rejects_untrusted_drift_file_names(self):
        script = [(repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
                  (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
                  (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
                  (repair.ROUTE_STATUS, idle())]
        for names in (["../private"], ["pipeline_integrations.py", "pipeline_integrations.py"],
                      [{"name": "pipeline_integrations.py"}]):
            with self.subTest(names=names):
                result = bridge(binding="DRIFT")
                result["driftedHostFiles"] = names
                controller, runner = self.controller(script, bridge_probe=lambda _, result=result: result)
                state = self.finish(controller)
                self.assertEqual(state["status"], "error")
                self.assertIn("invalid host-file evidence", state["message"])
                self.assertEqual(runner.script, [])

    def test_initial_read_is_idle_without_action_needed_claim(self):
        controller, runner = self.controller([])
        self.assertEqual(controller.read()["status"], "idle")
        self.assertIsNone(controller.read()["action"])
        self.assertEqual(runner.calls, [])

    def test_universal_entry_runs_once_and_requires_fresh_receipt(self):
        controller, runner = self.controller([(repair.ENTRY_READINESS, entry_result())])
        state = self.finish(controller, "entry")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["action"], "readiness")
        self.assertIn("No task or model inference was run", state["message"])
        self.assertEqual(state["steps"], [{"name": "universal-entry", "result": "receipt-verified"}])
        self.assertEqual(runner.calls, [(repair.ENTRY_READINESS, None, 100)])

    def test_universal_entry_exit_zero_without_receipt_is_not_ready(self):
        controller, runner = self.controller([(repair.ENTRY_READINESS, entry_result())],
                                            write_readiness=False)
        state = self.finish(controller, "entry")
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["steps"][0]["result"], "receipt-unverified")
        self.assertEqual(len(runner.calls), 1)

    def test_universal_entry_degraded_and_unavailable_remain_distinct(self):
        for result, expected in ((entry_result("degraded", "launcher-preflight-exit", 3), "needs-action"),
                                 (entry_result("unavailable", "launcher-timeout", 3), "error")):
            with self.subTest(expected=expected):
                controller, runner = self.controller([(repair.ENTRY_READINESS, result)])
                self.assertEqual(self.finish(controller, "entry")["status"], expected)
                self.assertEqual(len(runner.calls), 1)

    def test_universal_entry_rejects_mismatched_or_private_output(self):
        cases = [entry_result("ready", "fresh-launcher-receipt", 3),
                 entry_result("ready", "private diagnostic", 0),
                 reply({"schemaVersion": 1, "operation": "status", "status": "ready",
                        "evidence": "fresh-launcher-receipt"}),
                 reply({"schemaVersion": 1, "operation": "readiness", "status": "ready",
                        "evidence": "fresh-launcher-receipt", "task": "private"}),
                 repair.CommandResult(0, b'{"schemaVersion":1,"schemaVersion":1}'),
                 reply(None)]
        for result in cases:
            with self.subTest(raw=result.stdout):
                controller, _ = self.controller([(repair.ENTRY_READINESS, result)])
                state = self.finish(controller, "entry")
                self.assertEqual(state["status"], "error")
                self.assertNotIn("private", json.dumps(state))

    def test_universal_entry_and_repair_share_one_worker_gate(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def waiting_runner(args, data, timeout):
            calls.append(args)
            entered.set()
            release.wait(1)
            return entry_result()
        controller = repair.OnlineCodeRepair(waiting_runner, readiness_path=self.receipt)
        first = controller.request_entry()
        self.assertTrue(entered.wait(1))
        blocked = controller.request()
        self.assertEqual(blocked, first)
        self.assertEqual(blocked["action"], "readiness")
        self.assertEqual(calls, [repair.ENTRY_READINESS])
        release.set()
        deadline = time.monotonic() + 2
        while controller.read()["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(controller.read()["status"], "needs-action")

    def test_windows_owner_shape_diagnostic_is_bounded_and_contains_no_raw_code(self):
        controller, _ = self.controller([(repair.WINDOWS_STATUS, windows(ready=False))])
        pending, ready = controller._windows_status()
        self.assertIsNone(pending)
        self.assertFalse(ready)
        evidence = controller.read()["steps"][0]["evidence"]
        self.assertIn("exit=3", evidence)
        self.assertIn("ready=false", evidence)
        self.assertIn("inventory=false", evidence)

        private = reply({"kind": "codemode.windows.status.v1", "pending": None,
                         "readyForWork": "private status", "inventory": {"ok": "private inventory", "reason": "private reason"},
                         "code": {"private": "secret"}}, 3)
        controller, _ = self.controller([(repair.WINDOWS_STATUS, private)])
        with self.assertRaises(repair._Stop):
            controller._windows_status()
        serialized = json.dumps(controller.read())
        self.assertIn("ready=invalid", serialized)
        self.assertNotIn("private", serialized)
        self.assertNotIn("secret", serialized)

    def test_universal_entry_runner_uses_fixed_executable_and_rejects_input(self):
        script = Path(self.temporary.name) / "entry"
        script.write_text("#!/usr/bin/python3\nimport sys\nprint(sys.argv[1:])\n")
        script.chmod(0o700)
        with mock.patch.object(repair, "ENTRY", script):
            result = repair.launcher_runner(repair.ENTRY_READINESS, None, 2)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), b"['readiness']")
            with self.assertRaisesRegex(ValueError, "accepts no input"):
                repair.launcher_runner(repair.ENTRY_READINESS, b'{}', 2)

    def test_launcher_cleans_nested_child_after_normal_exit_and_timeout(self):
        script = Path(self.temporary.name) / "launcher"
        pid_path = Path(self.temporary.name) / "nested.pid"
        child = "import time;time.sleep(30)"

        def live(pid):
            result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                                    capture_output=True, text=True, timeout=2)
            return result.returncode == 0 and result.stdout.strip()[:1] not in ("", "Z")

        real_popen = subprocess.Popen

        def ready_launcher(command, *args, **kwargs):
            process = real_popen(command, *args, **kwargs)
            if command[0] != str(script):
                return process
            # Start the runner's unchanged deadline only once this cleanup
            # fixture has a live descendant, independent of host exec latency.
            ready_until = time.monotonic() + 5
            try:
                while time.monotonic() < ready_until:
                    if pid_path.exists():
                        try:
                            nested_pid = int(pid_path.read_text())
                        except ValueError:
                            pass  # The launcher's pid write may be in progress.
                        else:
                            if live(nested_pid):
                                return process
                    time.sleep(0.01)
                self.fail("launcher fixture did not create a live nested child")
            except BaseException:
                try:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                finally:
                    try:
                        process.wait(timeout=2)
                    finally:
                        if process.stdout is not None:
                            process.stdout.close()
                raise

        for stall, startup_delay in ((False, 0), (True, 0.4)):
            with self.subTest(stall=stall, startup_delay=startup_delay):
                # A pid left by the first pass must not stand in for a stall launcher
                # that never started its nested child before the deadline.
                pid_path.unlink(missing_ok=True)
                prewarmed_script(script, "import subprocess,time\n"
                                 f"time.sleep({startup_delay})\n"
                                 f"child=subprocess.Popen(['/usr/bin/python3','-c',{child!r}],"
                                 "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                                 f"open({str(pid_path)!r},'w').write(str(child.pid))\n"
                                 + ("time.sleep(30)\n" if stall else "print('done')\n"))
                with mock.patch.object(repair, "LAUNCHER", script), \
                        mock.patch.object(repair.subprocess, "Popen", side_effect=ready_launcher):
                    if stall:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            repair.launcher_runner(repair.ROUTE_STATUS, None, 0.2)
                    else:
                        self.assertEqual(repair.launcher_runner(repair.ROUTE_STATUS, None, 2).stdout.strip(), b"done")
                nested_pid = int(pid_path.read_text())
                try:
                    deadline = time.monotonic() + 2
                    while live(nested_pid) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertFalse(live(nested_pid), "nested launcher child remained alive")
                finally:
                    if live(nested_pid):
                        os.kill(nested_pid, signal.SIGKILL)

    def test_universal_entry_rejects_symlinks_and_writable_source(self):
        source = Path(self.temporary.name) / "trusted"
        source.write_text("#!/usr/bin/python3\nprint('trusted')\n")
        source.chmod(0o700)
        link = Path(self.temporary.name) / "link"
        link.symlink_to(source)
        with mock.patch.object(repair, "ENTRY", link), self.assertRaises(OSError):
            repair.launcher_runner(repair.ENTRY_READINESS, None, 2)
        source.chmod(0o720)
        with mock.patch.object(repair, "ENTRY", source), self.assertRaises(ValueError):
            repair.launcher_runner(repair.ENTRY_READINESS, None, 2)
        source.chmod(0o700)
        alias = Path(self.temporary.name) / "alias"
        alias.symlink_to(Path(self.temporary.name), target_is_directory=True)
        with mock.patch.object(repair, "ENTRY", alias / "trusted"), self.assertRaises(OSError):
            repair.launcher_runner(repair.ENTRY_READINESS, None, 2)

    def test_universal_entry_executes_captured_bytes_after_path_replacement(self):
        source = Path(self.temporary.name) / "entry"
        source.write_text("#!/usr/bin/python3\nprint('captured')\n")
        source.chmod(0o700)
        real_reader = repair._entry_source
        def replace_after_capture():
            captured = real_reader()
            source.rename(source.with_name("old-entry"))
            source.write_text("#!/usr/bin/python3\nprint('replacement')\n")
            source.chmod(0o700)
            return captured
        with mock.patch.object(repair, "ENTRY", source), \
             mock.patch.object(repair, "_entry_source", side_effect=replace_after_capture):
            result = repair.launcher_runner(repair.ENTRY_READINESS, None, 2)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"captured")

    def test_readiness_receipt_read_is_pinned_to_private_directory(self):
        private = Path(self.temporary.name) / "private"
        private.mkdir(mode=0o700)
        receipt = private / "readiness.json"
        started_at = time.time()
        good = {"schemaVersion": 1, "status": "PREFLIGHT_COMPLETED",
                "observedAtUnix": time.time(), "source": "online-code-mode",
                "client": None, "chatId": None}
        receipt.write_text(json.dumps(good))
        receipt.chmod(0o600)
        real_open = os.open
        swapped = False
        def swap_parent(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "readiness.json" and "dir_fd" in kwargs and not swapped:
                swapped = True
                private.rename(private.with_name("old-private"))
                private.mkdir(mode=0o700)
                (private / "readiness.json").write_text('{"status":"NOT_RUN"}')
                (private / "readiness.json").chmod(0o600)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(repair.os, "open", side_effect=swap_parent):
            self.assertTrue(repair._new_readiness_receipt(receipt, started_at))
        self.assertTrue(swapped)

    def test_readiness_receipt_rejects_symlink(self):
        target = Path(self.temporary.name) / "target"
        target.write_text("{}")
        target.chmod(0o600)
        self.receipt.symlink_to(target)
        self.assertFalse(repair._new_readiness_receipt(self.receipt, time.time() - 1))

    def test_exit_zero_without_new_receipt_cannot_claim_ready(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ], write_readiness=False)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("no verified readiness receipt", state["message"])
        self.assertEqual(runner.calls[-1][0], repair.ROUTE_STATUS)

    def test_old_receipt_cannot_satisfy_a_new_click(self):
        self.receipt.write_text(json.dumps({
            "schemaVersion": 1, "status": "PREFLIGHT_COMPLETED",
            "observedAtUnix": time.time() - 30, "source": "online-code-mode",
            "client": None, "chatId": None,
        }))
        self.receipt.chmod(0o600)
        controller, _ = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ], write_readiness=False)
        self.assertEqual(self.finish(controller)["status"], "needs-action")

    def test_launcher_runner_enforces_output_cap_while_child_is_running(self):
        script = prewarmed_script(Path(self.temporary.name) / "large-output",
                                  "sys.stdout.buffer.write(b'x' * 262145)\n")
        with mock.patch.object(repair, "LAUNCHER", script):
            with self.assertRaisesRegex(ValueError, "output exceeded limit"):
                repair.launcher_runner(repair.ROUTE_STATUS, None, 2)
            with self.assertRaisesRegex(ValueError, "unsupported launcher operation"):
                repair.launcher_runner(("--route", "work"), None, 2)

    def test_cancel_between_spawn_and_registration_kills_private_group(self):
        script = Path(self.temporary.name) / "waiting-owner"
        script.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        owner = repair._OwnerChildren()
        spawned, release = threading.Event(), threading.Event()
        errors = []
        children = []
        real_popen = subprocess.Popen

        def delayed_spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            spawned.set()
            release.wait(2)
            return child

        def run():
            repair._OWNER_THREAD.children = owner
            try:
                repair.launcher_runner(repair.ROUTE_STATUS, None, 30)
            except repair._Cancelled as error:
                errors.append(error)
            finally:
                del repair._OWNER_THREAD.children

        worker = threading.Thread(target=run)
        try:
            with mock.patch.object(repair, "LAUNCHER", script), \
                 mock.patch.object(repair.subprocess, "Popen", side_effect=delayed_spawn):
                worker.start()
                self.assertTrue(spawned.wait(1))
                owner.cancel()
                release.set()
                worker.join(.7)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertEqual(children[0].poll(), -signal.SIGKILL)
        finally:
            owner.cancel()
            release.set()
            worker.join(1)

    def test_cancel_signal_failure_is_reported_and_direct_child_is_tried(self):
        controller = repair.OnlineCodeRepair(lambda *_: idle(),
                                              nisi_pending_path=self.marker)
        child = mock.Mock(pid=24680)
        controller._owner_children.register(child)
        with controller._lock:
            controller._state["status"] = "running"
        with mock.patch.object(repair.os, "killpg", side_effect=PermissionError):
            controller.cancel()
        child.kill.assert_called_once_with()
        self.assertTrue(controller._owner_children.signal_failed)
        self.assertIn("could not be confirmed terminated", controller.read()["message"])

    def test_launcher_cleanup_signal_failure_cannot_return_success(self):
        script = prewarmed_script(Path(self.temporary.name) / "successful-owner", "print('ready')\n")
        with mock.patch.object(repair, "LAUNCHER", script), \
             mock.patch.object(repair.os, "killpg", side_effect=PermissionError):
            with self.assertRaisesRegex(OSError, "could not be confirmed terminated"):
                repair.launcher_runner(repair.ROUTE_STATUS, None, 2)

    def test_cancel_live_route_owner_kills_child_and_joins_within_grace(self):
        script = Path(self.temporary.name) / "waiting-route"
        script.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        controller = repair.OnlineCodeRepair(nisi_pending_path=self.marker)
        try:
            with mock.patch.object(repair, "LAUNCHER", script):
                self.assertEqual(controller.request_fix("route")["status"], "running")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with controller._owner_children._lock:
                        children = list(controller._owner_children._children.values())
                    if children:
                        break
                    time.sleep(.005)
                self.assertEqual(len(children), 1)
                controller.cancel()
                self.assertTrue(controller.join(.45))
                self.assertEqual(children[0].poll(), -signal.SIGKILL)
                self.assertEqual(controller.read()["status"], "needs-action")
                self.assertEqual(controller.request_fix("route")["operationId"], 1)
        finally:
            controller.cancel()
            controller.join(1)

    def test_cancel_live_lm_studio_start_kills_child_and_joins_within_grace(self):
        script = Path(self.temporary.name) / "waiting-lms"
        script.write_text("#!/usr/bin/python3\nimport time\ntime.sleep(30)\n")
        script.chmod(0o700)
        controller = repair.OnlineCodeRepair(lambda *_: idle(),
                                              nisi_pending_path=self.marker)
        try:
            with mock.patch.object(repair, "_runtime_sources", return_value=(False, False)), \
                 mock.patch.object(repair, "_local_server_stopped", return_value=True), \
                 mock.patch.object(repair, "_loopback_port_free", return_value=True), \
                 mock.patch.object(repair, "_find_lms", return_value=str(script)):
                self.assertEqual(controller.request_fix("local")["status"], "running")
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    with controller._owner_children._lock:
                        children = list(controller._owner_children._children.values())
                    if children:
                        break
                    time.sleep(.005)
                self.assertEqual(len(children), 1)
                controller.cancel()
                self.assertTrue(controller.join(.45))
                self.assertEqual(children[0].poll(), -signal.SIGKILL)
                self.assertEqual(controller.read()["status"], "needs-action")
        finally:
            controller.cancel()
            controller.join(1)

    def test_exact_preflight_refusal_is_archived_with_bound_input(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, active_status()),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_PREFLIGHT, preflight_settled()),
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready")
        reconcile = next(call for call in runner.calls if call[0] == repair.ROUTE_PREFLIGHT)
        self.assertEqual(json.loads(reconcile[1]), {"runId": RUN, "inputSha256": DIGEST,
                                                   "confirmPreflightOnly": True})
        self.assertEqual([step["result"] for step in state["steps"] if step["name"] == "preflight-reconcile"],
                         ["archived"])
        self.assertEqual(runner.script, [])

    def test_backend_or_other_active_run_stops_before_any_repair(self):
        active = preflight_active()
        active["sequence"] = 7
        controller, runner = self.controller([(repair.ROUTE_STATUS, active_status(active))])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("owner review", state["message"])
        self.assertEqual(len(runner.calls), 1)

    def test_busy_router_stops_after_status(self):
        controller, runner = self.controller([(repair.ROUTE_STATUS,
            reply({"status": "NOT_RUN", "code": "ROUTER_OWNER_BUSY"}, 3))])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual([call[0] for call in runner.calls], [repair.ROUTE_STATUS])

    def test_per_run_router_status_refusals_need_action_not_error(self):
        """review:compat 1, spec R2.9 rule 6: the per-run router's exit-3 `--route status` codes
        describe router state (several quarantined runs, an install in progress, an owner repair),
        so the check stops with needs-action and changes nothing; an unknown code stays an error."""
        for code, result, words in (
                ("ROUTER_MULTIPLE_UNRESOLVED_RUNS", "multiple-unresolved", "More than one router run needs owner review"),
                ("ROUTER_INSTALL_IN_PROGRESS", "install-in-progress", "press the button again afterwards"),
                ("ROUTER_INSTALL_CHANGED", "install-in-progress", "press the button again afterwards"),
                # Permanent until the owner acts: never "press the button again afterwards".
                ("ROUTER_INSTALL_ROLLED_BACK", "install-refusing", "(ROUTER_INSTALL_ROLLED_BACK)"),
                ("ROUTER_INSTALL_FENCE_MISSING", "install-refusing", "finish the router install"),
                ("ROUTER_INSTALL_FENCE_INVALID", "install-refusing", "refuses every command (ROUTER_INSTALL_FENCE_INVALID)"),
                ("ROUTER_INSTALL_GENERATION_MISMATCH", "install-refusing", "or roll it back"),
                ("ROUTER_OWNER_LOCK_MISSING", "owner-action", "owner lock is missing"),
                ("ROUTER_OWNER_LOCK_REPLACED", "owner-action", "owner lock was replaced"),
                ("ROUTER_POLICY_BUSY", "owner-action", "concurrency policy"),
                ("ROUTER_BEGIN_UNCERTAIN", "owner-action", "exact reconcile gate")):
            with self.subTest(code=code):
                controller, runner = self.controller([(repair.ROUTE_STATUS,
                    reply({"status": "NOT_RUN", "code": code}, 3))])
                state = self.finish(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertIn(words, state["message"])
                if result == "install-refusing":
                    self.assertNotIn("press the button again", state["message"])
                self.assertIn({"name": "router-status", "result": result},
                              [{k: step.get(k) for k in ("name", "result")} for step in state["steps"]
                               if isinstance(step, dict)])
                self.assertEqual([call[0] for call in runner.calls], [repair.ROUTE_STATUS])
        for code, value in ((3, {"status": "NOT_RUN", "code": "ROUTER_STATE_MISSING"}),
                            (3, {"status": "NOT_RUN", "code": "ROUTER_INSTALL_../../x"}),
                            (2, {"status": "NOT_RUN", "code": "ROUTER_MULTIPLE_UNRESOLVED_RUNS"})):
            with self.subTest(code=value["code"], exit=code):
                controller, runner = self.controller([(repair.ROUTE_STATUS, reply(value, code))])
                state = self.finish(controller)
                self.assertEqual(state["status"], "error")
                self.assertEqual([call[0] for call in runner.calls], [repair.ROUTE_STATUS])

    def test_router_holds_tell_the_readers_the_monitor_may_hold_owner_lock(self):
        """Spec R2.9 legacy rule (iii): while Fix holds router owner.lock, an EX holder is this
        Monitor, never a live legacy route; the flag is raised before the flock is tried."""
        import telemetry
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "owner.lock"
            lock.write_bytes(b"")
            os.chmod(lock, 0o600)
            self.assertFalse(telemetry._monitor_holds_router_owner())
            with repair._hold_router_owner(lock) as held:
                self.assertEqual(held, "held")
                self.assertTrue(telemetry._monitor_holds_router_owner())
            self.assertFalse(telemetry._monitor_holds_router_owner())
            with mock.patch.object(repair, "ROUTER_OWNER_LOCK", lock):
                with repair.ShareControl().hold_router() as free:
                    self.assertTrue(free)
                    self.assertTrue(telemetry._monitor_holds_router_owner())
            self.assertFalse(telemetry._monitor_holds_router_owner())
            # p2-readers converge (Sol N3): the mark is up during the EX|NB attempt, but a failed
            # attempt holds nothing, so it is dropped before the caller handles "busy"; a live
            # legacy route is then discounted for one flock call, not for the busy branch.
            during = []
            real_flock = fcntl.flock

            def flock(fd, operation):
                if operation & fcntl.LOCK_EX:
                    during.append(telemetry._monitor_holds_router_owner())
                return real_flock(fd, operation)

            other = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(other, fcntl.LOCK_EX)
                with mock.patch.object(repair.fcntl, "flock", flock):
                    with repair._hold_router_owner(lock) as held:
                        self.assertEqual(held, "busy")
                        self.assertFalse(telemetry._monitor_holds_router_owner())
                    with mock.patch.object(repair, "ROUTER_OWNER_LOCK", lock):
                        with repair.ShareControl().hold_router() as free:
                            self.assertFalse(free)
                            self.assertFalse(telemetry._monitor_holds_router_owner())
            finally:
                os.close(other)
            self.assertEqual(during, [True, True])
            self.assertFalse(telemetry._monitor_holds_router_owner())

    def test_p2conv_huge_integers_never_raise_out_of_the_fix_readers(self):
        """Sol N7 (and the two sibling reads in this file): math.isfinite(10**400) raises
        OverflowError, which escaped the readiness receipt's and the Nisi marker's own error
        handling; a huge integer is now simply not a usable number."""
        huge = json.loads("1" + "0" * 400)
        good = {"schemaVersion": 1, "status": "PREFLIGHT_COMPLETED", "observedAtUnix": time.time(),
                "source": "online-code-mode", "client": None, "chatId": None}
        for value, expected in ((good["observedAtUnix"], True), (huge, False)):
            with self.subTest(observed=type(value).__name__):
                if self.receipt.exists():
                    self.receipt.unlink()
                self.receipt.write_text(json.dumps(dict(good, observedAtUnix=value)))
                self.receipt.chmod(0o600)
                self.assertIs(repair._new_readiness_receipt(self.receipt, time.time() - 5), expected)
        marker = {"kind": "codemode.nisi.pending.v1", "started_unix": huge, "input_sha256": "f" * 64}
        with self.assertRaises(repair._Unsafe):
            repair._parse_nisi_marker(json.dumps(marker).encode())
        marker["started_unix"] = time.time()
        self.assertEqual(repair._parse_nisi_marker(json.dumps(marker).encode())["input_sha256"], "f" * 64)

    def test_nisi_pending_blocks_preflight_reconciliation(self):
        controller, runner = self.controller([(repair.ROUTE_STATUS, active_status()),
                                             (repair.NISI_STATUS, nisi(True))])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual([call[0] for call in runner.calls], [repair.ROUTE_STATUS, repair.NISI_STATUS])

    def test_symlinked_nisi_marker_is_not_treated_as_clear(self):
        self.marker.symlink_to(Path(self.temporary.name) / "missing")
        controller, runner = self.controller([(repair.ROUTE_STATUS, active_status()),
                                             (repair.NISI_STATUS, nisi(False))])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(len(runner.calls), 2)

    def test_windows_pending_is_polled_once_and_never_resent(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows({"id": JOB})),
            (repair.WINDOWS_RECONCILE, reply({"status": "pending", "id": JOB}, 3)),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual([call[0] for call in runner.calls].count(repair.WINDOWS_RECONCILE), 1)
        reconcile = next(call for call in runner.calls if call[0] == repair.WINDOWS_RECONCILE)
        self.assertEqual(json.loads(reconcile[1]), {"expectedJobId": JOB})
        self.assertNotIn(repair.READINESS, [call[0] for call in runner.calls])

    def test_terminal_windows_error_is_archived_but_not_called_ready(self):
        private_error = "private response body must stay inside owner receipt"
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows({"id": JOB})),
            (repair.WINDOWS_RECONCILE, reply({"schema_version": 1, "id": JOB,
                                              "status": "error", "error": private_error})),
            (repair.WINDOWS_STATUS, windows()), (repair.NISI_STATUS, nisi()),
            (repair.READINESS, reply(None)), (repair.NISI_STATUS, nisi()),
            (repair.WINDOWS_STATUS, windows()), (repair.ROUTE_STATUS, idle()),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("ended in an error", state["message"])
        self.assertNotIn(private_error, json.dumps(state))
        self.assertEqual(runner.script, [])

    def test_mismatched_windows_result_does_not_claim_reconciliation(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows({"id": JOB})),
            (repair.WINDOWS_RECONCILE, reply({"schema_version": 1,
                                              "id": "mac-other-1234567890", "status": "success"})),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertNotIn(repair.READINESS, [call[0] for call in runner.calls])

    def test_owner_detected_job_change_stops_before_readiness(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows({"id": JOB})),
            (repair.WINDOWS_RECONCILE,
             reply({"status": "NOT_RUN", "code": "EXPECTED_JOB_ID_MISMATCH"}, 3)),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("job changed", state["message"])
        self.assertNotIn(repair.READINESS, [call[0] for call in runner.calls])

    def test_failed_preflight_reconcile_retains_unresolved_state(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, active_status()),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_PREFLIGHT, reply({"kind": "codemode.router.reconcile.v1",
                                             "status": "NOT_RUN", "code": "ROUTER_RECONCILE_STATE_INVALID"}, 3)),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("not archived", state["message"])
        self.assertNotIn(repair.READINESS, [call[0] for call in runner.calls])

    def test_degraded_inventory_still_rechecks_route_without_inference_claim(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None, 3)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ])
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("degraded", state["message"])
        self.assertEqual(runner.calls[-1][0], repair.ROUTE_STATUS)

    def test_mac_inventory_does_not_claim_unavailable_windows_route_ready(self):
        delays = []
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows(ready=False)),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows(ready=False)),
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows(ready=False)),
            (repair.WINDOWS_STATUS, windows(ready=False)),
        ], sleep=delays.append)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("Windows route is unavailable", state["message"])
        self.assertEqual([call[0] for call in runner.calls].count(repair.READINESS), 1)
        self.assertEqual([call[0] for call in runner.calls].count(repair.WINDOWS_RECONCILE), 0)
        self.assertEqual(delays, [2.0, 2.0])
        self.assertEqual([step["result"] for step in state["steps"] if step["name"] == "windows-status"][-3:],
                         ["unavailable", "unavailable", "unavailable"])

    def test_windows_heartbeat_recovers_during_read_only_grace(self):
        delays = []
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows(ready=False)),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows(ready=False)),
            (repair.ROUTE_STATUS, idle()),
            (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.ROUTE_STATUS, idle()),
        ], sleep=delays.append)
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(delays, [2.0])
        self.assertEqual([call[0] for call in runner.calls].count(repair.READINESS), 1)
        self.assertEqual([call[0] for call in runner.calls].count(repair.WINDOWS_RECONCILE), 0)
        self.assertEqual([step["result"] for step in state["steps"] if step["name"] == "windows-status"][-2:],
                         ["unavailable", "clear"])

    def test_second_click_while_running_does_not_start_another_worker(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def waiting_runner(args, data, timeout):
            calls.append(args)
            entered.set()
            release.wait(1)
            return idle()

        controller = repair.OnlineCodeRepair(waiting_runner, nisi_pending_path=self.marker)
        first = controller.request()
        self.assertTrue(entered.wait(1))
        second = controller.request()
        self.assertEqual(first["operationId"], second["operationId"])
        self.assertEqual(second["status"], "running")
        self.assertEqual(calls, [repair.ROUTE_STATUS])
        release.set()
        deadline = time.monotonic() + 2
        while controller.read()["status"] == "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(controller.read()["status"], "error")

    def test_fix_local_healthy_feeds_do_not_start_server_or_call_launcher(self):
        controller, runner = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", return_value=(True, True)), \
             mock.patch.object(repair, "_start_local_api", side_effect=AssertionError("unwanted start")):
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["action"], "fix-local")
        self.assertIn("No model inference was run", state["message"])
        self.assertEqual(runner.calls, [])

    def test_fix_local_api_live_activity_missing_does_not_restart(self):
        controller, runner = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", return_value=(True, False)), \
             mock.patch.object(repair, "_start_local_api", side_effect=AssertionError("unwanted start")):
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("activity feed is unavailable", state["message"])
        self.assertEqual(runner.calls, [])

    def test_fix_local_stopped_server_starts_only_on_free_loopback_port_and_verifies(self):
        controller, runner = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", side_effect=[(False, False), (False, False), (True, True)]) as feeds, \
             mock.patch.object(repair, "_local_server_stopped", return_value=True) as owner, \
             mock.patch.object(repair, "_loopback_port_free", return_value=True), \
             mock.patch.object(repair, "_start_local_api", return_value=True) as start:
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(feeds.call_count, 3)
        self.assertEqual(owner.call_count, 2)
        start.assert_called_once_with()
        self.assertIn({"name": "local-server-start", "result": "accepted"}, state["steps"])
        self.assertIn({"name": "local-api-after", "result": "live"}, state["steps"])
        self.assertIn("No model inference was run", state["message"])
        self.assertEqual(runner.calls, [])

    def test_fix_local_occupied_port_refuses_start(self):
        controller, _ = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", return_value=(False, True)), \
             mock.patch.object(repair, "_local_server_stopped", return_value=True), \
             mock.patch.object(repair, "_loopback_port_free", return_value=False), \
             mock.patch.object(repair, "_start_local_api", side_effect=AssertionError("unwanted start")):
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("port 1234 is occupied", state["message"])

    def test_fix_local_owner_running_elsewhere_refuses_second_server(self):
        controller, _ = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", return_value=(False, False)), \
             mock.patch.object(repair, "_local_server_stopped", return_value=False), \
             mock.patch.object(repair, "_start_local_api", side_effect=AssertionError("unwanted start")):
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("reports a running server", state["message"])

    def test_fix_local_rechecks_owner_and_port_before_start(self):
        for owner_checks, port_checks in (([True, False], [True]), ([True, True], [True, False])):
            with self.subTest(owner_checks=owner_checks, port_checks=port_checks):
                controller, _ = self.controller([])
                with mock.patch.object(repair, "_runtime_sources", return_value=(False, False)), \
                     mock.patch.object(repair, "_local_server_stopped", side_effect=owner_checks), \
                     mock.patch.object(repair, "_loopback_port_free", side_effect=port_checks), \
                     mock.patch.object(repair, "_start_local_api", side_effect=AssertionError("unwanted start")):
                    state = self.finish(controller, "fix-local")
                self.assertEqual(state["status"], "needs-action")
                self.assertIn("changed before the start", state["message"])

    def test_fix_local_failed_start_and_failed_postcheck_cannot_claim_ready(self):
        for started in (False, True):
            with self.subTest(started=started):
                now = [0.0]
                controller, _ = self.controller([], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
                                                monotonic=lambda: now[0])
                with mock.patch.object(repair, "_runtime_sources", return_value=(False, False)) as feeds, \
                     mock.patch.object(repair, "_local_server_stopped", return_value=True), \
                     mock.patch.object(repair, "_loopback_port_free", return_value=True), \
                     mock.patch.object(repair, "_start_local_api", return_value=started):
                    state = self.finish(controller, "fix-local")
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(feeds.call_count, 19 if started else 2)
                self.assertEqual(now[0], 8.0 if started else 0.0)
                self.assertNotIn("responsive", state["message"])

    def test_fix_local_late_feed_recovery_within_grace_verifies_without_second_start(self):
        now = [0.0]
        controller, runner = self.controller([], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
                                             monotonic=lambda: now[0])
        samples = [(False, False), (False, False)] + [(False, True)] * 5 + [(True, True)]
        with mock.patch.object(repair, "_runtime_sources", side_effect=samples) as feeds, \
             mock.patch.object(repair, "_local_server_stopped", return_value=True), \
             mock.patch.object(repair, "_loopback_port_free", return_value=True), \
             mock.patch.object(repair, "_start_local_api", return_value=True) as start:
            state = self.finish(controller, "fix-local")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(feeds.call_count, len(samples))
        self.assertGreater(now[0], 1.5)
        self.assertLess(now[0], 8.0)
        start.assert_called_once_with()
        self.assertEqual(runner.calls, [])

    def test_fix_both_runs_independent_route_check_when_local_needs_action(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ])
        with mock.patch.object(repair, "_runtime_sources", return_value=(False, False)), \
             mock.patch.object(repair, "_local_server_stopped", return_value=True), \
             mock.patch.object(repair, "_loopback_port_free", return_value=False):
            state = self.finish(controller, "fix-both")
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["action"], "fix-both")
        self.assertIn("Local runtime:", state["message"])
        self.assertIn("Route:", state["message"])
        self.assertEqual([call[0] for call in runner.calls].count(repair.READINESS), 1)

    def test_fix_both_preserves_local_success_when_route_response_is_invalid(self):
        controller, runner = self.controller([(repair.ROUTE_STATUS, object())])
        with mock.patch.object(repair, "_runtime_sources", return_value=(True, True)):
            state = self.finish(controller, "fix-both")
        self.assertEqual(state["status"], "error")
        self.assertIn("Local runtime: LM Studio inventory and activity feeds are responsive", state["message"])
        self.assertIn("Route: The route check failed unexpectedly", state["message"])
        self.assertNotIn("invalid runner response", state["message"])
        self.assertEqual(state["steps"][:2], [
            {"name": "local-api-before", "result": "live"},
            {"name": "local-activity-before", "result": "live"}])
        self.assertEqual([call[0] for call in runner.calls], [repair.ROUTE_STATUS])

    def test_fix_all_runs_local_nisi_and_route_once_and_journals_the_outcome(self):
        journal = Path(self.temporary.name) / "fix-journal.jsonl"
        controller = repair.OnlineCodeRepair(lambda *_: reply(None),
                                             fix_journal_path=journal,
                                             nisi_pending_path=self.marker)
        calls = []

        def local():
            calls.append("local")
            return "ready", "local responsive"

        def nisi():
            calls.append("nisi")
            raise repair._Stop("needs-action", "second resident model needed")

        def route(_started_at, *, fix=False):
            calls.append("route")
            self.assertTrue(fix)
            return "ready", "route responsive"

        with mock.patch.object(controller, "_fix_local_runtime", side_effect=local), \
             mock.patch.object(controller, "_fix_nisi", side_effect=nisi), \
             mock.patch.object(controller, "_check_and_repair", side_effect=route):
            state = self.finish(controller, "fix-all")
        self.assertEqual(calls, ["local", "nisi", "route"])
        self.assertEqual((state["status"], state["action"]), ("needs-action", "fix-all"))
        self.assertIn("Nisi Inference: second resident model needed", state["message"])
        self.assertEqual([step["result"] for step in state["steps"] if step["name"] == "fix-component"],
                         ["ready", "needs-action", "ready"])
        [line] = [json.loads(value) for value in journal.read_text().splitlines()]
        self.assertEqual((line["action"], line["status"]), ("fix-all", "needs-action"))

    def test_fix_all_preserves_all_component_failures_without_a_second_route_attempt(self):
        controller, _runner = self.controller([])
        with mock.patch.object(controller, "_fix_local_runtime", side_effect=ValueError("private")) as local, \
             mock.patch.object(controller, "_fix_nisi", side_effect=repair._Stop("needs-action", "owner busy")) as nisi, \
             mock.patch.object(controller, "_check_and_repair", side_effect=OSError("private")) as route:
            state = self.finish(controller, "fix-all")
        self.assertEqual(state["status"], "error")
        self.assertEqual((local.call_count, nisi.call_count, route.call_count), (1, 1, 1))
        self.assertIn("Local runtime:", state["message"])
        self.assertIn("Nisi Inference: owner busy", state["message"])
        self.assertIn("Route:", state["message"])
        self.assertNotIn("private", state["message"])

    def test_fix_route_reuses_exact_existing_owner_check(self):
        controller, runner = self.controller([
            (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
            (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
            (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
            (repair.ROUTE_STATUS, idle()),
        ])
        with mock.patch.object(repair, "_runtime_sources", side_effect=AssertionError("local not selected")):
            state = self.finish(controller, "fix-route")
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["action"], "fix-route")
        self.assertEqual([call[0] for call in runner.calls].count(repair.READINESS), 1)

    def test_fix_and_readiness_share_worker_gate(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_sources():
            entered.set()
            release.wait(1)
            return True, True
        controller, runner = self.controller([])
        with mock.patch.object(repair, "_runtime_sources", side_effect=blocked_sources):
            first = controller.request_fix("local")
            self.assertTrue(entered.wait(1))
            second = controller.request_entry()
            self.assertEqual(first["operationId"], second["operationId"])
            self.assertEqual(second["action"], "fix-local")
            release.set()
            deadline = time.monotonic() + 2
            while controller.read()["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(controller.read()["status"], "ready")
        self.assertEqual(runner.calls, [])

    def test_fixed_lmstudio_start_command_is_loopback_only(self):
        process = mock.Mock()
        process.wait.return_value = 0
        with mock.patch.object(repair, "_find_lms", return_value="/owner/lms"), \
             mock.patch.object(repair.subprocess, "Popen", return_value=process) as popen:
            self.assertTrue(repair._start_local_api())
        self.assertEqual(popen.call_args.args[0],
                         ["/owner/lms", "server", "start", "--port", "1234", "--bind", "127.0.0.1"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_owner_status_is_bounded_exact_json_and_fail_closed(self):
        with mock.patch.object(repair, "_find_lms", return_value="/owner/lms"), \
             mock.patch.object(repair, "_bounded_command", return_value='{"running":false}') as command:
            self.assertTrue(repair._local_server_stopped())
        self.assertEqual(command.call_args.args[0], ["/owner/lms", "server", "status", "--json"])
        self.assertEqual(command.call_args.kwargs, {"limit": 4096, "timeout": 3.0})
        for raw in ('{"running":true,"port":1234}', '{"running":"false"}',
                    '{"running":false,"running":false}',
                    '{"running":false,"unexpected":true}', '{"running":false,"port":70000}'):
            with self.subTest(raw=raw), \
                 mock.patch.object(repair, "_find_lms", return_value="/owner/lms"), \
                 mock.patch.object(repair, "_bounded_command", return_value=raw):
                if raw == '{"running":true,"port":1234}':
                    self.assertFalse(repair._local_server_stopped())
                else:
                    with self.assertRaises(repair._Stop):
                        repair._local_server_stopped()


class HeadlessSwitchTests(unittest.TestCase):
    """The monitor's PC headless button only runs pc-llm on/off, bounded and parsed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.argv = Path(self.temporary.name) / "argv.json"

    def fake(self, body, mode=0o700):
        script = Path(self.temporary.name) / "pc-llm"
        script.write_text("#!/usr/bin/python3\nimport json, sys, time\n"
                          f"open({str(self.argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n" + body)
        script.chmod(mode)
        return script

    def run_switch(self, action, script, **kwargs):
        controller = repair.OnlineCodeRepair(ScriptedRunner([]), pc_llm=script, **kwargs)
        self.addCleanup(controller.join, 1)
        self.addCleanup(controller.cancel)
        started = controller.request_headless(action)
        self.assertEqual((started["status"], started["action"]), ("running", f"headless-{action}"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = controller.read()
            if state["status"] != "running":
                return state
            time.sleep(0.01)
        self.fail("headless switch did not finish")

    def test_on_runs_exact_bounded_command_and_reports_expiry_and_probe_time(self):
        expires = time.time() + 4 * 3600
        script = self.fake(f"print(json.dumps({{'state': 'on', 'expiresAtUnix': {expires}, 'leaseId': 'SECRET', "
                           "'probe': {'elapsedSeconds': 3.7, 'jobId': 'mac-private'}}))\n")
        with mock.patch.object(repair, "_run_bounded", wraps=repair._run_bounded) as bounded:
            state = self.run_switch("on", script)
        self.assertEqual(json.loads(self.argv.read_text()), ["on", "--granted-by", "inference-monitor", "--hours", "4"])
        self.assertEqual(bounded.call_args.args[1:], (None, 150))
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["message"],
                         f"PC headless on until {time.strftime('%H:%M', time.localtime(expires))} (probe answered in 3.7 s).")
        self.assertEqual(state["steps"], [{"name": "pc-headless", "result": "on"}])
        self.assertNotIn("SECRET", json.dumps(state))

    def test_off_runs_only_off(self):
        script = self.fake("print(json.dumps({'state': 'off', 'reason': 'turned off'}))\n")
        with mock.patch.object(repair, "_run_bounded", wraps=repair._run_bounded) as bounded:
            state = self.run_switch("off", script)
        self.assertEqual(json.loads(self.argv.read_text()), ["off"])
        self.assertEqual(bounded.call_args.args[1:], (None, 15))
        self.assertEqual((state["status"], state["message"]), ("ready", "PC headless off."))
        self.assertEqual(state["steps"], [{"name": "pc-headless", "result": "off"}])

    def test_probe_failure_and_errors_report_pc_llm_message_without_paths(self):
        script = self.fake("print(json.dumps({'status': 'error', 'code': 'PC_PROBE_FAILED', "
                           "'message': 'the PC did not answer the probe correctly; switch stays off'}))\nsys.exit(2)\n")
        state = self.run_switch("on", script)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["message"], "the PC did not answer the probe correctly; switch stays off")
        self.assertEqual(state["steps"], [{"name": "pc-headless", "result": "probe-failed"}])
        script = self.fake("print(json.dumps({'status': 'error', 'code': 'PC_UNAVAILABLE', 'message': "
                           "'on/off failed: /Users/owner/bin/chami-dispatch and ~/x and C:\\\\SharedChami\\\\q\\n' + 'z' * 400}))\n"
                           "sys.exit(2)\n")
        state = self.run_switch("on", script)
        self.assertEqual(state["steps"], [{"name": "pc-headless", "result": "error"}])
        # "on/off" is a word, not a path; the real paths after it are still redacted.
        self.assertEqual(state["message"], "on/off failed: [path] and [path] and [path] " + "z" * 156)
        self.assertLessEqual(len(state["message"]), 200)
        for private in ("/Users", "owner", "~/", "SharedChami", "\n"):
            self.assertNotIn(private, state["message"])

    def test_pc_llm_message_keeps_queue_io_wording_and_still_redacts_paths(self):
        # chami-dispatch's share fault must read as an I/O fault, not as a redacted path.
        script = self.fake("print(json.dumps({'status': 'error', 'code': 'PC_UNAVAILABLE', 'message': "
                           "'worker not ready; job not enqueued: queue I/O unavailable or timed out'}))\nsys.exit(2)\n")
        state = self.run_switch("on", script)
        self.assertEqual(state["message"], "worker not ready; job not enqueued: queue I/O unavailable or timed out")
        for text in ("publication uncertain for mac-20260926-181500-abcdef: queue I/O unavailable or timed out",
                     "queue I/O.", "switch 'on/off' refused", "turn it On/Off"):
            with self.subTest(text=text):
                self.assertEqual(repair._pc_llm_message(text), text)
        for text, expected in (("read /Users/owner/x failed", "read [path] failed"),
                               ("see ~/x", "see [path]"), ("at C:\\SharedChami\\q", "at [path]"),
                               ("from bin/chami-dispatch", "from [path]"), ("I/O/Users/owner", "[path]"),
                               ("'a/b I/O'", "[path]")):
            with self.subTest(text=text):
                self.assertEqual(repair._pc_llm_message(text), expected)

    def test_unexpected_output_is_needs_action(self):
        for body in ("print('not json')\n", "print('{}')\nprint('{}')\n",
                     "print(json.dumps({'state': 'off'}))\n",  # off while on was asked
                     "print(json.dumps({'status': 'error', 'message': 'x'}))\n",  # exit 0 error
                     "sys.stdout.write('{\"state\":\"on\",\"state\":\"on\"}')\n"):
            with self.subTest(body=body):
                state = self.run_switch("on", self.fake(body))
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(state["steps"], [{"name": "pc-headless", "result": "error"}])
                self.assertIn("check the headless switch", state["message"])
        # A confirmed switch without a usable expiry or probe time still reads as on.
        state = self.run_switch("on", self.fake("print(json.dumps({'state': 'on', 'expiresAtUnix': 'later'}))\n"))
        self.assertEqual((state["status"], state["message"]), ("ready", "PC headless on."))

    def test_unsafe_or_missing_command_is_never_run(self):
        for script in (self.fake("print('{}')\n", mode=0o722), Path(self.temporary.name) / "missing"):
            with self.subTest(script=script.name), \
                 mock.patch.object(repair, "_run_bounded", side_effect=AssertionError("must not run")):
                state = self.run_switch("off", script)
            self.assertEqual(state["status"], "needs-action")
            self.assertIn("not installed as an owner executable", state["message"])
        controller = repair.OnlineCodeRepair(ScriptedRunner([]))
        with mock.patch.object(repair, "PC_LLM", self.fake("print('{}')\n")), \
             mock.patch.object(repair, "_run_bounded", side_effect=AssertionError("must not run")):
            controller.request_headless("off")
            controller.join(2)
        self.assertIn("not installed", controller.read()["message"])
        with self.assertRaises(ValueError):
            controller.request_headless("toggle")

    def test_timeout_is_bounded(self):
        script = self.fake("time.sleep(30)\n")
        with mock.patch.dict(repair.HEADLESS_COMMANDS, {"off": (("off",), 0.3)}):
            started = time.monotonic()
            state = self.run_switch("off", script)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("did not answer within 0.3 s", state["message"])

    def test_production_on_holds_sharedchami_and_refuses_beside_a_stuck_reader(self):
        script = self.fake("print(json.dumps({'state': 'on', 'expiresAtUnix': time.time() + 60}))\n")
        with mock.patch.object(repair, "PC_LLM", script), \
             mock.patch.object(repair, "_windows_worker_reader_blocked", return_value=True), \
             mock.patch.object(repair, "_run_bounded", side_effect=AssertionError("must not run")):
            controller = repair.OnlineCodeRepair()
            controller.request_headless("on")
            self.assertTrue(controller.join(8))
        state = controller.read()
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["steps"], [{"name": "windows-preflight", "result": "paused"}])
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        repair._WINDOWS_WORKER_IO_LOCK.release()
        held = []

        def reader_clear():
            held.append((telemetry._WINDOWS_WORKER_HOLD.is_set(),
                         not repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False)))
            return False
        with mock.patch.object(repair, "PC_LLM", script), \
             mock.patch.object(repair, "_windows_worker_reader_blocked", side_effect=reader_clear):
            controller = repair.OnlineCodeRepair()
            controller.request_headless("on")
            self.assertTrue(controller.join(8))
        self.assertEqual(held, [(True, True)])
        self.assertEqual(controller.read()["status"], "ready")
        self.assertFalse(telemetry._WINDOWS_WORKER_HOLD.is_set())
        self.assertTrue(repair._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        repair._WINDOWS_WORKER_IO_LOCK.release()

    def test_cancel_kills_a_running_probe(self):
        script = self.fake("time.sleep(30)\n")
        controller = repair.OnlineCodeRepair(ScriptedRunner([]), pc_llm=script)
        try:
            controller.request_headless("on")
            deadline = time.monotonic() + 2
            children = []
            while time.monotonic() < deadline and not children:
                with controller._owner_children._lock:
                    children = list(controller._owner_children._children.values())
                time.sleep(.005)
            self.assertEqual(len(children), 1)
            controller.cancel()
            self.assertTrue(controller.join(.45))
            self.assertEqual(children[0].poll(), -signal.SIGKILL)
            self.assertIn("probing the PC", controller.read()["message"])
        finally:
            controller.cancel()
            controller.join(1)


# Fix Nisi Inference ---------------------------------------------------------------

NOW = 1_790_500_000.0
MARKER_SHA = "81dbab4f" + "c" * 56
MARKER_AGE = 9 * 3600 + 12 * 60
GEMMA, QWEN = "google/gemma-4-26b-a4b-qat", "qwen/qwen3.8-27b"
SERVER_PID = 907
JEV_ON = {"kind": "chami.intake.typesafe.status.v1", "version": "0.1.0", "enabled": True,
          "code": None, "opt_in": {"CHAMI_TYPESAFE": "file", "TYPESAFE_API_KEY": "file"},
          "offline": False, "network_contacted": False, "next": "call classify with the task text"}


def private_file(path, raw=b"", mode=0o600):
    path.write_bytes(raw)
    path.chmod(mode)


def lms_row(model, *, status="idle", queued=0, kind="llm", identifier=None):
    return {"type": kind, "modelKey": model, "identifier": identifier or model,
            "status": status, "queued": queued, "parallel": 4}


def lsof_listing(*clients, unseen=(), listener="LM Studio", listen_name="127.0.0.1:1234"):
    """lsof -F pcnT text: LM Studio's listener, each client's end and the server's end."""
    lines = [f"p{SERVER_PID}", f"c{listener}", "f78", f"n{listen_name}", "TST=LISTEN", "TQR=0", "TQS=0"]
    for pid, command, port in clients:
        lines += [f"p{pid}", f"c{command}", f"f{port % 97}",
                  f"n127.0.0.1:{port}->127.0.0.1:1234", "TST=ESTABLISHED", "TQR=0", "TQS=0"]
    for port in [port for _pid, _command, port in clients] + list(unseen):
        lines += [f"p{SERVER_PID}", f"c{listener}", f"f{100 + port % 97}",
                  f"n127.0.0.1:1234->127.0.0.1:{port}", "TST=ESTABLISHED", "TQR=0", "TQS=0"]
    return "\n".join(lines) + "\n"


def nisi_status_reply(recovery, **changes):
    nisi = {"status": "ADAPTER_PRESENT", "root": "/nisi-runtime/test", "recoveryRequired": recovery,
            "ownership_scope": "online-code-mode Nisi wrapper only",
            "model_inference": "NOT_RUN", "workflow_acceptance": "NOT_RUN", **changes}
    return reply({"kind": "codemode.integrations.v1", "nisi": nisi})


def integration_failure(code):
    return reply({"kind": "codemode.integrations.v1", "status": "NOT_RUN", "code": code,
                  "accepted": False, "certification": "NOT_RUN"}, 3)


def launcher_recover(state):
    """What the launcher's recover() does: owner flock, rename the marker, acknowledge."""
    fd = os.open(state / "owner.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return integration_failure("NISI_OWNER_BUSY")
        marker = state / "pending.json"
        if marker.exists() or marker.is_symlink():
            marker.rename(state / f"recovered-{uuid.uuid4().hex}.json")
        return reply(dict(repair.NISI_RECOVERED))
    finally:
        os.close(fd)


class NisiLauncher:
    """Scripted owner commands over a temp Nisi state directory (never ~/.local/state)."""

    def __init__(self, state, *, route=None, jev=None, recover=None, nisi_status=None):
        self.state = state
        self.calls = []
        self.route = route if route is not None else idle()
        self.jev = jev if jev is not None else reply(JEV_ON)
        self.recover = recover
        self.nisi_status = nisi_status

    def count(self, args):
        return [call[0] for call in self.calls].count(args)

    def __call__(self, args, data, timeout):
        self.calls.append((args, data, timeout))
        if args == repair.ROUTE_STATUS:
            return self.route() if callable(self.route) else self.route
        if args == repair.NISI_STATUS:
            if self.nisi_status is not None:
                return self.nisi_status()
            return nisi_status_reply((self.state / "pending.json").exists())
        if args == repair.NISI_RECOVER:
            if data is not None or timeout != repair.NISI_RECOVER_TIMEOUT:
                raise AssertionError("recover must run without input under its 15 s cap")
            return (self.recover or launcher_recover)(self.state)
        if args == repair.JEV_STATUS:
            return self.jev() if callable(self.jev) else self.jev
        raise AssertionError(f"unexpected launcher operation {args}")


class FixNisiTests(unittest.TestCase):
    """Fix Nisi Inference: measured preconditions, one launcher recover, then status-only checks."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "codemode-nisi"
        self.state.mkdir(mode=0o700)
        self.pending = self.state / "pending.json"
        self.owner_lock = self.state / "owner.lock"
        private_file(self.owner_lock)
        self.router_lock = self.root / "router-owner.lock"
        private_file(self.router_lock)
        self.journal = self.root / "inference-monitor" / "fix-journal.jsonl"
        self.now = NOW
        self.sleeps = []
        self.lms = [lms_row(GEMMA), lms_row(QWEN), lms_row("text-embedding-nomic", kind="embedding")]
        self.lms_calls = 0
        self.sockets = lsof_listing()
        self.socket_calls = 0

    def reset_state(self):
        for path in self.state.iterdir():
            if path.name != "owner.lock":
                if path.is_dir() and not path.is_symlink():
                    path.rmdir()
                else:
                    path.unlink()

    def write_marker(self, age=MARKER_AGE, **extra):
        value = {"kind": "codemode.nisi.pending.v1", "started_unix": self.now - age,
                 "input_sha256": MARKER_SHA, **extra}
        raw = json.dumps(value).encode("utf-8")
        private_file(self.pending, raw)
        return raw

    def read_lms(self):
        self.lms_calls += 1
        return copy.deepcopy(self.lms)

    def read_sockets(self):
        self.socket_calls += 1
        return self.sockets

    def controller(self, launcher=None, **overrides):
        launcher = launcher or NisiLauncher(self.state)
        options = dict(nisi_pending_path=self.pending, readiness_path=self.root / "readiness.json",
                       sleep=self.sleeps.append, clock=lambda: self.now,
                       lms_ps=self.read_lms, loopback_sockets=self.read_sockets,
                       router_lock_path=self.router_lock, fix_journal_path=self.journal)
        options.update(overrides)
        controller = repair.OnlineCodeRepair(launcher, **options)
        self.addCleanup(controller.join, 1)
        self.addCleanup(controller.cancel)
        return controller, launcher

    def run_fix(self, controller):
        started = controller.request_fix("nisi")
        self.assertEqual((started["status"], started["action"]), ("running", "fix-nisi"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = controller.read()
            if state["status"] != "running":
                return state
            time.sleep(0.005)
        self.fail("Fix Nisi Inference did not finish")

    @staticmethod
    def steps(state):
        return [(step["name"], step["result"]) for step in state["steps"]]

    def step(self, state, name):
        matches = [step for step in state["steps"] if step["name"] == name]
        self.assertEqual(len(matches), 1, name)
        return matches[0]

    def assert_not_recovered(self, launcher, raw):
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)
        self.assertEqual(self.pending.read_bytes(), raw)
        self.assertEqual(list(self.state.glob("recovered-*")), [])

    def journal_lines(self):
        return [json.loads(line) for line in self.journal.read_text().splitlines()]

    def assert_lock_free(self, path):
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_happy_path_recovers_the_stale_marker_once_and_reports_ready(self):
        raw = self.write_marker()
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        self.assertEqual(self.steps(state), [
            ("route-status", "idle"), ("nisi-status", "recovery-required"), ("marker", "stale"),
            ("owner-lock", "free"), ("server-idle", "idle"), ("recover", "acknowledged"),
            ("pair", "resident"), ("jev", "opted-in"), ("verify", "clear"), ("journal", "written")])
        self.assertEqual([call[0] for call in launcher.calls], [
            repair.ROUTE_STATUS, repair.NISI_STATUS, repair.NISI_RECOVER,
            repair.JEV_STATUS, repair.NISI_STATUS])
        self.assertTrue(state["message"].startswith("Nisi Inference ready."))
        for text in ("age 9 h 12 min", "owner anonymous (legacy)", "input 81dbab4fcccc",
                     "No model inference was run."):
            self.assertIn(text, state["message"])
        marker = self.step(state, "marker")
        self.assertEqual((marker["ageSeconds"], marker["inputSha256"], marker["owner"]),
                         (str(MARKER_AGE), MARKER_SHA[:12], "anonymous (legacy)"))
        self.assertIn("kind=codemode.nisi.pending.v1", marker["evidence"])
        self.assertEqual(self.step(state, "owner-lock")["evidence"], "nisi=free; router=held")
        self.assertIn("sample2: lms=2-llm-idle; clients=0", self.step(state, "server-idle")["evidence"])
        pair = self.step(state, "pair")
        self.assertEqual((pair["author"], pair["reviewer"]), (GEMMA, QWEN))
        # The launcher moved exactly the measured marker; this control wrote nothing there.
        self.assertFalse(os.path.lexists(self.pending))
        recovered = list(self.state.glob("recovered-*.json"))
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].read_bytes(), raw)
        self.assertEqual(recovered[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.sleeps, [2.0])
        self.assertEqual((self.lms_calls, self.socket_calls), (3, 2))
        self.assert_lock_free(self.router_lock)
        self.assert_lock_free(self.owner_lock)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        [line] = self.journal_lines()
        self.assertEqual((line["kind"], line["action"], line["status"], line["operationId"]),
                         ("inference-monitor.fix-journal.v1", "fix-nisi", "ready", state["operationId"]))
        self.assertEqual([(s["name"], s["result"]) for s in line["steps"]], self.steps(state)[:-1])
        self.assertEqual(line["finishedUnix"], NOW)

    def test_router_form_marker_reports_its_run_id_as_owner(self):
        self.write_marker(runId="marketscout.brainstorm.20260926.qwen.answer", operation="answer")
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        marker = self.step(state, "marker")
        self.assertEqual((marker["runId"], marker["operation"], marker["owner"]),
                         ("marketscout.brainstorm.20260926.qwen.answer", "answer",
                          "marketscout.brainstorm.20260926.qwen.answer"))
        self.assertIn("owner marketscout.brainstorm.20260926.qwen.answer", state["message"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_active_busy_or_invalid_router_stops_before_any_nisi_step(self):
        raw = self.write_marker()

        def timed_out():
            raise subprocess.TimeoutExpired(["initiate-online-code-mode"], 12)

        def runner_failed():
            raise OSError("launcher missing")

        for label, route, result, status in (
                ("active", active_status(), "active", "needs-action"),
                ("busy", reply({"status": "NOT_RUN", "code": "ROUTER_OWNER_BUSY"}, 3), "busy", "needs-action"),
                # The per-run router (spec R2.9 rule 6): router state, never an invalid reply.
                ("multiple", reply({"status": "NOT_RUN", "code": "ROUTER_MULTIPLE_UNRESOLVED_RUNS"}, 3),
                 "multiple-unresolved", "needs-action"),
                ("installing", reply({"status": "NOT_RUN", "code": "ROUTER_INSTALL_IN_PROGRESS"}, 3),
                 "install-in-progress", "needs-action"),
                ("owner repair", reply({"status": "NOT_RUN", "code": "ROUTER_OWNER_LOCK_REPLACED"}, 3),
                 "owner-action", "needs-action"),
                ("extra key", reply({"schemaVersion": 1, "active": None, "extra": 1}), "unavailable", "error"),
                ("exit 3", reply({"schemaVersion": 1, "active": None}, 3), "unavailable", "error"),
                # The launcher's execv failure path prints plain text, not JSON.
                ("not json", repair.CommandResult(3, b"NOT_RUN: shared router unavailable\n"),
                 "unavailable", "error"),
                ("timeout", timed_out, "unavailable", "error"),
                ("runner error", runner_failed, "unavailable", "error")):
            with self.subTest(label=label):
                controller, launcher = self.controller(NisiLauncher(self.state, route=route))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], status)
                self.assertEqual(self.steps(state), [("route-status", result), ("journal", "written")])
                self.assertEqual([call[0] for call in launcher.calls], [repair.ROUTE_STATUS])
                self.assert_not_recovered(launcher, raw)
                self.assertIn("Nisi recovery was not attempted", state["message"])
                if result == "unavailable":
                    self.assertEqual(state["message"], "Router status is unavailable or invalid; "
                                                       "Nisi recovery was not attempted.")
                    self.assertEqual(self.journal_lines()[-1]["steps"], [
                        {"name": "route-status", "result": "unavailable"}])

    def test_active_router_run_is_named_and_the_advice_is_not_a_loop(self):
        self.write_marker()
        controller, launcher = self.controller(NisiLauncher(self.state, route=active_status()))
        state = self.run_fix(controller)
        self.assertEqual(self.step(state, "route-status")["runId"], RUN)
        # The router's reconcile gates refuse while pending.json exists, so the
        # message names the manual recover first, then the reconcile gates.
        self.assertEqual(state["message"], repair.ROUTE_ACTIVE_MESSAGE)
        message = state["message"]
        for text in ("never recovers Nisi under it", "Nisi recovery was not attempted",
                     "confirm the model server is idle",
                     "initiate-online-code-mode --nisi recover --confirm-server-idle",
                     "--route reconcile-review-unavailable", "--route reconcile-finish-refused",
                     "using the recovered file", "Otherwise resolve the run with its owner first."):
            self.assertIn(text, message)
        self.assertLess(message.index("--nisi recover"), message.index("reconcile-review-unavailable"))
        self.assertNotIn(repair.NISI_RECOVER, [call[0] for call in launcher.calls])

    def test_router_owner_lock_held_by_a_real_flock_stops_before_the_nisi_lock(self):
        raw = self.write_marker()
        fd = os.open(self.router_lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            controller, launcher = self.controller()
            state = self.run_fix(controller)
        finally:
            os.close(fd)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(self.steps(state)[-2:], [("owner-lock", "router-busy"), ("journal", "written")])
        self.assertIn("A route task started", state["message"])
        self.assertEqual((self.lms_calls, self.socket_calls), (0, 0))
        self.assert_not_recovered(launcher, raw)

    def router_lock_is_held(self):
        """A real LOCK_EX|LOCK_NB probe on a separate descriptor: True when another holder has it."""
        fd = os.open(self.router_lock, os.O_RDWR)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def test_router_owner_lock_is_really_held_through_the_idle_samples_and_the_recover(self):
        self.write_marker()
        observed = []

        def sockets():
            observed.append(("sockets", self.router_lock_is_held()))
            return self.sockets

        def recover(state):
            observed.append(("recover", self.router_lock_is_held()))
            return launcher_recover(state)

        def jev():
            observed.append(("jev", self.router_lock_is_held()))
            return reply(JEV_ON)

        self.assertFalse(self.router_lock_is_held())
        controller, launcher = self.controller(NisiLauncher(self.state, recover=recover, jev=jev),
                                               loopback_sockets=sockets)
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        # Held for both samples and the launcher's recover; released before the status reads.
        self.assertEqual(observed, [("sockets", True), ("sockets", True), ("recover", True), ("jev", False)])
        self.assertEqual(self.step(state, "owner-lock")["evidence"], "nisi=free; router=held")
        self.assert_lock_free(self.router_lock)

    def test_missing_or_linked_router_owner_lock_is_never_reported_as_held(self):
        target = self.root / "elsewhere.lock"
        private_file(target)
        for label, prepare, result, text in (
                ("missing", lambda: self.router_lock.unlink(), "router-absent",
                 "The router owner lock is missing"),
                ("linked", lambda: (self.router_lock.unlink(), self.router_lock.symlink_to(target)),
                 "router-busy", "A route task started")):
            with self.subTest(label=label):
                self.reset_state()
                raw = self.write_marker()
                prepare()
                self.lms_calls = self.socket_calls = 0
                controller, launcher = self.controller()
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2:], [("owner-lock", result), ("journal", "written")])
                self.assertIn(text, state["message"])
                self.assertIn("Nisi recovery was not attempted", state["message"])
                self.assertEqual((self.lms_calls, self.socket_calls), (0, 0))
                self.assert_not_recovered(launcher, raw)
                # The check never creates or replaces the router's lock.
                self.assertEqual(os.path.lexists(self.router_lock), label == "linked")
                self.assertEqual(target.read_bytes(), b"")
                if label == "linked":
                    self.router_lock.unlink()
                private_file(self.router_lock)
        # A scripted runner with no router lock path says so instead of claiming a hold.
        self.reset_state()
        self.write_marker()
        controller, _ = self.controller(router_lock_path=None)
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        self.assertEqual(self.step(state, "owner-lock")["evidence"], "nisi=free; router=not-configured")

    def test_nisi_owner_lock_held_by_a_real_flock_stops(self):
        raw = self.write_marker()
        fd = os.open(self.owner_lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            controller, launcher = self.controller()
            state = self.run_fix(controller)
        finally:
            os.close(fd)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["message"], "A Nisi call is still running; not recovering.")
        self.assertEqual(self.steps(state)[-2:], [("owner-lock", "busy"), ("journal", "written")])
        self.assertEqual((self.lms_calls, self.socket_calls), (0, 0))
        self.assert_not_recovered(launcher, raw)
        self.assert_lock_free(self.router_lock)

    def test_missing_linked_or_shared_owner_lock_stops(self):
        raw = self.write_marker()
        target = self.root / "elsewhere.lock"
        private_file(target)
        for case, result in (("missing", "missing"), ("symlink", "unsafe"), ("mode", "unsafe")):
            with self.subTest(case=case):
                if os.path.lexists(self.owner_lock):
                    self.owner_lock.unlink()
                if case == "symlink":
                    self.owner_lock.symlink_to(target)
                elif case == "mode":
                    private_file(self.owner_lock, mode=0o644)
                controller, launcher = self.controller(NisiLauncher(self.state, recover=lambda _: self.fail()))
                state = self.run_fix(controller)
                self.assertEqual(self.steps(state)[-2], ("owner-lock", result))
                self.assert_not_recovered(launcher, raw)

    def test_marker_younger_than_ten_minutes_is_never_recovered(self):
        self.assertEqual(repair.NISI_MARKER_MIN_AGE, 600)
        raw = self.write_marker(age=599)
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(self.steps(state)[-2], ("marker", "too-young"))
        self.assertIn("only 9 min old", state["message"])
        self.assertIn("at least 10 min old", state["message"])
        self.assertEqual(self.step(state, "marker")["ageSeconds"], "599")
        self.assert_not_recovered(launcher, raw)
        self.reset_state()
        self.write_marker(age=600)
        controller, launcher = self.controller()
        self.assertEqual(self.run_fix(controller)["status"], "ready")
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_future_dated_marker_is_not_recovered(self):
        raw = self.write_marker(age=-120)
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("marker", "future-dated"))
        self.assert_not_recovered(launcher, raw)

    def test_unsafe_marker_is_never_recovered(self):
        elsewhere = self.root / "elsewhere.json"
        valid = json.dumps({"kind": "codemode.nisi.pending.v1", "started_unix": NOW - MARKER_AGE,
                            "input_sha256": MARKER_SHA}).encode()

        def symlink():
            private_file(elsewhere, valid)
            self.pending.symlink_to(elsewhere)

        def hard_link():
            private_file(self.pending, valid)
            os.link(self.pending, self.state / "second-name.json")

        cases = {
            "is a symbolic link": symlink,
            "is larger than 4 KiB": lambda: private_file(self.pending, valid + b" " * 4096),
            "mode is not 0600": lambda: private_file(self.pending, valid, mode=0o644),
            "has another hard link": hard_link,
            "is not a regular file": lambda: self.pending.mkdir(mode=0o700),
            "is not valid JSON": lambda: private_file(self.pending, b"{not json"),
            "is not valid JSON (duplicate key)": lambda: private_file(
                self.pending, valid[:-1] + b', "kind": "codemode.nisi.pending.v1"}'),
            "is not a recognized form (kind)": lambda: private_file(
                self.pending, valid.replace(b"pending.v1", b"pending.v2")),
            "is not a recognized form (extra key)": lambda: private_file(
                self.pending, valid[:-1] + b', "runId": "x.answer"}'),
            "is not a recognized form (digest)": lambda: private_file(
                self.pending, valid.replace(MARKER_SHA.encode(), b"81dbab4f")),
            "is not a recognized form (bool time)": lambda: private_file(
                self.pending, json.dumps({"kind": "codemode.nisi.pending.v1", "started_unix": True,
                                          "input_sha256": MARKER_SHA}).encode()),
            "is not a recognized form (operation)": lambda: private_file(
                self.pending, valid[:-1] + b', "runId": "x.answer", "operation": "work"}'),
        }
        for reason, make in cases.items():
            with self.subTest(reason=reason):
                self.reset_state()
                make()
                before = {path.name for path in self.state.iterdir()}
                controller, launcher = self.controller(NisiLauncher(self.state, nisi_status=lambda: nisi_status_reply(True)))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("marker", "unsafe"))
                self.assertIn(reason.split(" (")[0], self.step(state, "marker")["evidence"])
                self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)
                self.assertEqual({path.name for path in self.state.iterdir()}, before)
                self.assertEqual((self.lms_calls, self.socket_calls), (0, 0))
        self.assertEqual(elsewhere.read_bytes(), valid)

    def test_marker_owned_by_another_user_is_unsafe(self):
        self.write_marker()
        dir_fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(repair.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(repair._Unsafe) as unsafe:
                    repair._read_private(dir_fd, "pending.json")
        finally:
            os.close(dir_fd)
        self.assertEqual(unsafe.exception.reason, "marker is not owned by this user")

    def test_state_directory_that_is_not_private_stops(self):
        raw = self.write_marker()
        self.state.chmod(0o755)
        self.addCleanup(self.state.chmod, 0o700)
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("marker", "unsafe"))
        self.assertIn("state directory is not private", self.step(state, "marker")["evidence"])
        self.assert_not_recovered(launcher, raw)

    def test_busy_or_unknown_lms_ps_stops_before_recover(self):
        def raising():
            raise OSError("lms unavailable")

        cases = (
            ("busy", [lms_row(GEMMA, status="generating"), lms_row(QWEN)], None),
            ("busy", [lms_row(GEMMA), lms_row(QWEN, queued=1)], None),
            ("busy", [lms_row(GEMMA, status="processingPrompt")], None),
            ("unknown", [{"type": "llm", "modelKey": GEMMA, "identifier": GEMMA, "queued": 0}], None),
            ("unknown", [lms_row(GEMMA, kind="vlm")], None),
            ("unknown", [lms_row(GEMMA, queued=True)], None),
            ("unknown", {"models": []}, None),
            ("unknown", None, raising),
        )
        for verdict, rows, probe in cases:
            with self.subTest(verdict=verdict, rows=rows):
                self.reset_state()
                raw = self.write_marker()
                self.lms = rows
                controller, launcher = self.controller(**({"lms_ps": probe} if probe else {}))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("server-idle", verdict))
                self.assertIn("busy or its state is unknown", state["message"])
                self.assert_not_recovered(launcher, raw)

    def test_foreign_unseen_or_unknown_loopback_clients_stop_before_recover(self):
        cases = (
            ("busy", lsof_listing((4242, "python3", 50123))),
            ("busy", lsof_listing((4242, "node", 50123), (os.getpid(), "Python", 50124))),
            ("busy", lsof_listing(unseen=(50125,))),
            ("busy", lsof_listing() + "p4242\ncpython3\nf9\nn127.0.0.1:50126->127.0.0.1:1234\nTST=SYN_SENT\n"),
            ("unknown", lsof_listing(listener="ollama")),
            ("unknown", lsof_listing(listen_name="127.0.0.1:12345")),
            ("unknown", ""),
            ("unknown", "p907\ncLM Studio\nf78\nn127.0.0.1:1234\n"),
            ("unknown", lsof_listing() + "x-unexpected\n"),
            ("unknown", lsof_listing() + "p4242\ncpython3\nf9\nn[::1]:50127->[::1]:1234\nTST=ESTABLISHED\n"),
            # A process other than LM Studio owning a server-side end of 127.0.0.1:1234.
            ("unknown", lsof_listing((os.getpid(), "Python", 50128))
             + "p4242\ncpython3\nf9\nn127.0.0.1:1234->127.0.0.1:50128\nTST=ESTABLISHED\n"),
        )
        for verdict, listing in cases:
            with self.subTest(verdict=verdict, listing=listing[-80:]):
                self.reset_state()
                raw = self.write_marker()
                self.sockets = listing
                controller, launcher = self.controller()
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("server-idle", verdict))
                self.assert_not_recovered(launcher, raw)

    def test_half_closed_or_abandoned_loopback_connections_stop_before_recover(self):
        def server_end(port, state):
            return f"p{SERVER_PID}\ncLM Studio\nf{150 + port % 97}\nn127.0.0.1:1234->127.0.0.1:{port}\nTST={state}\n"

        def client_end(pid, command, port, state):
            return f"p{pid}\nc{command}\nf{port % 97}\nn127.0.0.1:{port}->127.0.0.1:1234\nTST={state}\n"

        listen = lsof_listing()
        cases = (
            # A Nisi node child that half-closed on its timeout while LM Studio's end is still open.
            ("half-closed client", listen + client_end(5555, "node", 60000, "FIN_WAIT_2")
             + server_end(60000, "CLOSE_WAIT"), "clients=1; unseen-clients=0; half-closed=2"),
            # The client died: only LM Studio's CLOSE_WAIT end remains, maybe still generating.
            ("dead client", listen + server_end(60000, "CLOSE_WAIT"),
             "clients=0; unseen-clients=1; half-closed=1"),
            ("client reading after server closed", listen + client_end(5555, "node", 60001, "CLOSE_WAIT")
             + server_end(60001, "FIN_WAIT_2"), "clients=1; unseen-clients=0; half-closed=2"),
            ("closing", listen + client_end(5555, "node", 60002, "CLOSING")
             + server_end(60002, "CLOSING"), "clients=1; unseen-clients=0; half-closed=2"),
            ("last ack", listen + server_end(60003, "LAST_ACK"), "clients=0; unseen-clients=1; half-closed=1"),
            ("fin wait 1", listen + server_end(60004, "FIN_WAIT_1"), "clients=0; unseen-clients=1; half-closed=1"),
        )
        for label, listing, evidence in cases:
            with self.subTest(label=label):
                self.reset_state()
                raw = self.write_marker()
                self.sockets = listing
                controller, launcher = self.controller()
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("server-idle", "busy"))
                self.assertIn(f"sample1: lms=2-llm-idle; {evidence}", self.step(state, "server-idle")["evidence"])
                self.assert_not_recovered(launcher, raw)
        # Only LISTEN, TIME_WAIT and CLOSED sockets are ignored; this monitor's own
        # half-closed inventory read (LM Studio closed first) is not a client.
        for label, listing in (
                ("time wait", listen + server_end(60005, "TIME_WAIT")
                 + client_end(5555, "node", 60006, "TIME_WAIT")),
                ("closed", listen + client_end(5555, "node", 60007, "CLOSED")),
                ("own read", listen + client_end(os.getpid(), "Python", 60008, "CLOSE_WAIT")
                 + server_end(60008, "FIN_WAIT_2"))):
            with self.subTest(label=label):
                verdict = repair._server_client_verdict(repair._lsof_sockets(listing), os.getpid())
                self.assertEqual(verdict[0], "idle", verdict)

    def test_socket_listing_failure_is_unknown(self):
        raw = self.write_marker()

        def failing():
            raise TimeoutError("lsof timed out")

        controller, launcher = self.controller(loopback_sockets=failing)
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("server-idle", "unknown"))
        self.assertIn("sockets=unavailable", self.step(state, "server-idle")["evidence"])
        self.assert_not_recovered(launcher, raw)

    def test_second_sample_two_seconds_later_must_also_be_idle(self):
        raw = self.write_marker()
        listings = [lsof_listing(), lsof_listing((4242, "curl", 50200))]

        def sockets():
            return listings.pop(0)

        controller, launcher = self.controller(loopback_sockets=sockets)
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("server-idle", "busy"))
        evidence = self.step(state, "server-idle")["evidence"]
        self.assertIn("sample1: lms=2-llm-idle; clients=0", evidence)
        self.assertIn("sample2: lms=2-llm-idle; clients=1", evidence)
        self.assertEqual(self.sleeps, [2.0])
        self.assert_not_recovered(launcher, raw)

    def test_lm_studio_self_connection_and_this_monitors_inventory_read_are_not_clients(self):
        self.write_marker()
        self.sockets = lsof_listing((os.getpid(), "Python", 50301), (SERVER_PID, "LM Studio", 50302))
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        self.assertIn("monitor-reads=1", self.step(state, "server-idle")["evidence"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_unexpected_launcher_envelope_stops_and_is_never_retried(self):
        acknowledged = dict(repair.NISI_RECOVERED)
        cases = (
            ("invalid", reply({**acknowledged, "extra": True})),
            ("invalid", reply({**acknowledged, "remoteInferenceStopped": "STOPPED"})),
            ("invalid", reply(acknowledged, 3)),
            ("invalid", integration_failure("INTEGRATION_UNAVAILABLE_OR_INVALID")),
            ("invalid", repair.CommandResult(0, b"RECOVERY_ACKNOWLEDGED")),
            ("owner-busy", integration_failure("NISI_OWNER_BUSY")),
        )
        for result, response in cases:
            with self.subTest(result=result, response=response.stdout[:60]):
                self.reset_state()
                raw = self.write_marker()
                controller, launcher = self.controller(NisiLauncher(self.state, recover=lambda _: response))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("recover", result))
                self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)
                self.assertEqual(self.pending.read_bytes(), raw)
                self.assertNotIn(repair.JEV_STATUS, [call[0] for call in launcher.calls])

    def test_recover_timeout_is_an_unknown_outcome_and_is_not_retried(self):
        self.write_marker()

        def timed_out(_state):
            raise subprocess.TimeoutExpired(["initiate-online-code-mode"], repair.NISI_RECOVER_TIMEOUT)

        controller, launcher = self.controller(NisiLauncher(self.state, recover=timed_out))
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("recover", "no-answer"))
        self.assertIn("outcome is unknown and it was not retried", state["message"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_marker_still_present_after_acknowledgement_stops(self):
        raw = self.write_marker()
        controller, launcher = self.controller(
            NisiLauncher(self.state, recover=lambda _: reply(dict(repair.NISI_RECOVERED))))
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("recover", "marker-still-present"))
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)
        self.assertEqual(self.pending.read_bytes(), raw)

    def test_recovered_file_must_be_exactly_the_measured_marker(self):
        def extra_file(state):
            result = launcher_recover(state)
            private_file(state / f"recovered-{uuid.uuid4().hex}.json", b"{}")
            return result

        def different_content(state):
            (state / "pending.json").unlink()
            private_file(state / f"recovered-{uuid.uuid4().hex}.json", b'{"other": true}')
            return reply(dict(repair.NISI_RECOVERED))

        def same_bytes_new_inode(state):
            raw = (state / "pending.json").read_bytes()
            (state / "pending.json").unlink()
            private_file(state / f"recovered-{uuid.uuid4().hex}.json", raw)
            return reply(dict(repair.NISI_RECOVERED))

        def odd_name(state):
            (state / "pending.json").rename(state / "recovered-manual.json")
            return reply(dict(repair.NISI_RECOVERED))

        def shared_mode(state):
            result = launcher_recover(state)
            next(state.glob("recovered-*.json")).chmod(0o644)
            return result

        for result, recover in (("unconfirmed", extra_file), ("mismatch", different_content),
                                ("mismatch", same_bytes_new_inode), ("unconfirmed", odd_name),
                                ("unconfirmed", shared_mode)):
            with self.subTest(recover=recover.__name__):
                self.reset_state()
                self.write_marker()
                controller, launcher = self.controller(NisiLauncher(self.state, recover=recover))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.steps(state)[-2], ("recover", result))
                self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_marker_replaced_after_measurement_is_not_recovered(self):
        other = json.dumps({"kind": "codemode.nisi.pending.v1", "started_unix": NOW - MARKER_AGE,
                            "input_sha256": "d" * 64}).encode()
        # New content, or the same bytes under a new inode (another owner rewrote it).
        for label, content in (("different content", lambda raw: other), ("same bytes, new inode", lambda raw: raw)):
            with self.subTest(label=label):
                self.reset_state()
                raw = self.write_marker()
                self.lms_calls = 0
                before = self.pending.stat().st_ino

                def replacing_lms():
                    self.lms_calls += 1
                    if self.lms_calls == 2:
                        # Write the replacement beside it first so the inode must differ.
                        private_file(self.state / "replacement.json", content(raw))
                        os.replace(self.state / "replacement.json", self.pending)
                    return copy.deepcopy(self.lms)

                controller, launcher = self.controller(lms_ps=replacing_lms)
                state = self.run_fix(controller)
                self.assertEqual(self.steps(state)[-2], ("recover", "marker-changed"))
                self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)
                self.assertNotEqual(self.pending.stat().st_ino, before)
                self.assertEqual(self.pending.read_bytes(), content(raw))

    def test_marker_that_changes_while_it_is_read_is_unsafe(self):
        self.write_marker()
        real_fstat = os.fstat
        calls = []

        class Touched:
            """The second fstat of the open marker: same file, newer mtime (a concurrent write)."""

            def __init__(self, info):
                for name in ("st_mode", "st_uid", "st_nlink", "st_size", "st_dev", "st_ino", "st_ctime_ns"):
                    setattr(self, name, getattr(info, name))
                self.st_mtime_ns = info.st_mtime_ns + 1

        def fstat(fd):
            calls.append(fd)
            info = real_fstat(fd)
            return Touched(info) if len(calls) == 2 else info

        dir_fd = os.open(self.state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            with mock.patch.object(repair.os, "fstat", side_effect=fstat):
                with self.assertRaises(repair._Unsafe) as unsafe:
                    repair._read_private(dir_fd, "pending.json")
        finally:
            os.close(dir_fd)
        self.assertEqual(len(calls), 2)
        self.assertEqual(unsafe.exception.reason, "marker changed while it was read")

    def test_no_marker_skips_to_pair_jev_and_verify_without_recovering(self):
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready", state["message"])
        self.assertEqual(self.steps(state), [
            ("route-status", "idle"), ("nisi-status", "clear"), ("pair", "resident"),
            ("jev", "opted-in"), ("verify", "clear"), ("journal", "written")])
        self.assertEqual(state["message"], "Nisi Inference ready. No Nisi marker needed recovery. "
                                           "No model inference was run.")
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)
        self.assertEqual((self.lms_calls, self.socket_calls), (1, 0))
        self.assertEqual(self.sleeps, [])

    def test_owner_status_reporting_recovery_without_a_marker_stops(self):
        controller, launcher = self.controller(NisiLauncher(self.state, nisi_status=lambda: nisi_status_reply(True)))
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("marker", "missing"))
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)

    def test_nisi_status_is_read_closed(self):
        raw = self.write_marker()
        for response in (nisi_status_reply(True, extra="x"),
                         reply({"kind": "codemode.integrations.v1",
                                "nisi": {"status": "ADAPTER_PRESENT", "recoveryRequired": True}}),
                         nisi_status_reply(True, model_inference="RAN"),
                         nisi_status_reply(True, status="ADAPTER_MAYBE"),
                         nisi_status_reply(False, status="READY"),
                         nisi_status_reply("yes"),
                         reply({"kind": "codemode.integrations.v1", "nisi": None}),
                         integration_failure("INTEGRATION_UNAVAILABLE_OR_INVALID")):
            with self.subTest(response=response.stdout[:80]):
                controller, launcher = self.controller(
                    NisiLauncher(self.state, nisi_status=lambda: response))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "error")
                self.assertEqual(self.steps(state)[-2], ("nisi-status", "unavailable"))
                self.assert_not_recovered(launcher, raw)

    def test_missing_or_ambiguous_pair_is_a_named_gap_and_never_loads_a_model(self):
        cases = (
            ("missing", [lms_row(GEMMA)], "Nisi needs a second resident model"),
            ("missing", [], "Nisi needs a second resident model"),
            ("ambiguous", [lms_row(GEMMA), lms_row(QWEN), lms_row(QWEN, identifier=f"{QWEN}:2")],
             "ambiguous"),
            ("unavailable", {"models": []}, "loaded-model list is unavailable"),
        )
        for result, rows, gap in cases:
            with self.subTest(result=result):
                self.reset_state()
                self.write_marker()
                self.lms = rows
                # The server-idle samples see a valid idle list; only the pair read changes.
                reads = []

                def lms():
                    reads.append(1)
                    return copy.deepcopy(rows if len(reads) > 2 else [lms_row(GEMMA)])

                controller, launcher = self.controller(lms_ps=lms)
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.step(state, "pair")["result"], result)
                self.assertIn(gap, state["message"])
                self.assertIn("Recovered the Nisi marker", state["message"])
                self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)
                self.assertEqual(self.step(state, "verify")["result"], "clear")

    def test_pair_prefers_the_route_default_author_else_lexical_order(self):
        self.lms = [lms_row("zeta/model"), lms_row(QWEN), lms_row("alpha/model")]
        controller, _ = self.controller()
        pair = self.step(self.run_fix(controller), "pair")
        self.assertEqual((pair["author"], pair["reviewer"]), ("alpha/model", QWEN))
        self.lms = [lms_row(QWEN), lms_row(GEMMA), lms_row("alpha/model")]
        controller, _ = self.controller()
        pair = self.step(self.run_fix(controller), "pair")
        self.assertEqual((pair["author"], pair["reviewer"]), (GEMMA, "alpha/model"))

    def test_jev_not_opted_in_or_unavailable_is_a_named_gap(self):
        for jev, result, gap in (
                (reply({**JEV_ON, "enabled": False}), "not-opted-in", "Jev is not opted in"),
                (integration_failure("JEV_STATUS_UNAVAILABLE"), "unavailable", "Jev status is unavailable"),
                (reply({**JEV_ON, "network_contacted": True}), "unavailable", "Jev status is unavailable")):
            with self.subTest(result=result):
                controller, launcher = self.controller(NisiLauncher(self.state, jev=jev))
                state = self.run_fix(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertEqual(self.step(state, "jev")["result"], result)
                self.assertIn(gap, state["message"])
                self.assertEqual(launcher.count(repair.JEV_STATUS), 1)

    def test_missing_adapter_is_a_named_gap_never_ready(self):
        controller, launcher = self.controller(
            NisiLauncher(self.state, nisi_status=lambda: nisi_status_reply(False, status="NOT_RUN")))
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(self.steps(state)[-2], ("verify", "adapter-missing"))
        self.assertIn("the Nisi adapter is not installed", state["message"])
        self.assertNotIn("Nisi Inference ready", state["message"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)

    def test_dangling_marker_link_is_inspected_even_when_status_reports_no_recovery(self):
        # `--nisi status` follows the link (exists() is False), so only lexists sees it.
        self.pending.symlink_to(self.root / "gone.json")
        controller, launcher = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(self.steps(state), [
            ("route-status", "idle"), ("nisi-status", "recovery-required"), ("marker", "unsafe"),
            ("journal", "written")])
        self.assertIn("is a symbolic link", self.step(state, "marker")["evidence"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 0)
        self.assertTrue(self.pending.is_symlink())
        self.assertEqual((self.lms_calls, self.socket_calls), (0, 0))

    def test_verify_names_a_marker_that_is_still_required(self):
        self.write_marker()
        controller, launcher = self.controller(
            NisiLauncher(self.state, nisi_status=lambda: nisi_status_reply(True)))
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(self.steps(state)[-2], ("verify", "recovery-required"))
        self.assertIn("Nisi still reports recovery required", state["message"])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)

    def test_cancellation_during_the_idle_samples_never_runs_recover(self):
        raw = self.write_marker()
        holder = {}

        def cancelling_sleep(_seconds):
            holder["controller"].cancel()

        controller, launcher = self.controller(sleep=cancelling_sleep)
        holder["controller"] = controller
        controller.request_fix("nisi")
        self.assertTrue(controller.join(2))
        state = controller.read()
        self.assertEqual(state["status"], "needs-action")
        self.assertEqual(state["message"], repair.FIX_NISI_CANCELLED)
        self.assert_not_recovered(launcher, raw)
        [line] = self.journal_lines()
        self.assertEqual((line["status"], line["message"]), ("needs-action", repair.FIX_NISI_CANCELLED))
        self.assert_lock_free(self.router_lock)

    def test_cancel_during_a_probe_is_journaled_as_cancelled_not_as_a_result(self):
        holder = {}

        def cancelled_probe():
            # What _bounded_command raises when the monitor's cancel event fires mid-probe.
            holder["controller"].cancel()
            raise TimeoutError("process metadata cancelled")

        def cancelled_on_third_lms():
            self.lms_calls += 1
            if self.lms_calls == 3:
                cancelled_probe()
            return copy.deepcopy(self.lms)

        for label, overrides, absent, recovered in (
                ("socket listing", {"loopback_sockets": cancelled_probe}, "server-idle", False),
                ("pair read", {"lms_ps": cancelled_on_third_lms}, "pair", True)):
            with self.subTest(label=label):
                self.reset_state()
                if self.journal.exists():
                    self.journal.unlink()
                raw = self.write_marker()
                self.lms_calls = 0
                controller, launcher = self.controller(**overrides)
                holder["controller"] = controller
                controller.request_fix("nisi")
                self.assertTrue(controller.join(2))
                state = controller.read()
                self.assertEqual((state["status"], state["message"]), ("needs-action", repair.FIX_NISI_CANCELLED))
                self.assertNotIn(absent, [name for name, _ in self.steps(state)])
                self.assertEqual(self.steps(state)[-1], ("journal", "written"))
                [line] = self.journal_lines()
                self.assertEqual((line["status"], line["message"]), ("needs-action", repair.FIX_NISI_CANCELLED))
                self.assertNotIn(absent, [step["name"] for step in line["steps"]])
                self.assertEqual(launcher.count(repair.NISI_RECOVER), int(recovered))
                self.assertEqual(os.path.lexists(self.pending), not recovered)
                if not recovered:
                    self.assertEqual(self.pending.read_bytes(), raw)
                self.assertNotIn(repair.JEV_STATUS, [call[0] for call in launcher.calls])

    def test_cancel_interrupts_the_real_two_second_pause_within_grace(self):
        raw = self.write_marker()
        sampled = threading.Event()

        def lms():
            sampled.set()
            return copy.deepcopy(self.lms)

        controller, launcher = self.controller(sleep=None, lms_ps=lms)
        controller.request_fix("nisi")
        self.assertTrue(sampled.wait(2))
        time.sleep(0.05)
        started = time.monotonic()
        controller.cancel()
        self.assertTrue(controller.join(.45))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(controller.read()["message"], repair.FIX_NISI_CANCELLED)
        self.assert_not_recovered(launcher, raw)

    def test_cancel_during_recover_records_an_interrupted_recover(self):
        raw = self.write_marker()
        holder = {}

        def cancelled_recover(_state):
            holder["controller"].cancel()
            raise repair._Cancelled()

        controller, launcher = self.controller(NisiLauncher(self.state, recover=cancelled_recover))
        holder["controller"] = controller
        controller.request_fix("nisi")
        self.assertTrue(controller.join(2))
        state = controller.read()
        self.assertEqual((state["status"], state["message"]), ("needs-action", repair.FIX_NISI_CANCELLED))
        self.assertEqual(self.steps(state)[-2:], [("recover", "interrupted"), ("journal", "written")])
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)
        self.assertEqual(self.pending.read_bytes(), raw)

    def test_cancel_kills_a_live_launcher_recover_through_the_bounded_runner(self):
        raw = self.write_marker()
        status = {"kind": "codemode.integrations.v1",
                  "nisi": {"status": "ADAPTER_PRESENT", "root": "/nisi", "recoveryRequired": True,
                           "ownership_scope": "online-code-mode Nisi wrapper only",
                           "model_inference": "NOT_RUN", "workflow_acceptance": "NOT_RUN"}}
        script = self.root / "launcher"
        script.write_text("#!/usr/bin/python3\nimport json, sys, time\nargs = sys.argv[1:]\n"
                          "if args == ['--route', 'status']:\n"
                          "    print(json.dumps({'schemaVersion': 1, 'active': None}))\n"
                          "elif args == ['--nisi', 'status']:\n"
                          f"    print({json.dumps(status)!r})\n"
                          "elif args == ['--nisi', 'recover', '--confirm-server-idle']:\n"
                          "    time.sleep(30)\n"
                          "else:\n"
                          "    sys.exit(2)\n")
        script.chmod(0o700)
        # Production runner, but every host probe and path is injected (temp only).
        controller = repair.OnlineCodeRepair(
            nisi_pending_path=self.pending, readiness_path=self.root / "readiness.json",
            sleep=self.sleeps.append, clock=lambda: self.now, lms_ps=self.read_lms,
            loopback_sockets=self.read_sockets, router_lock_path=self.router_lock,
            fix_journal_path=self.journal)
        try:
            with mock.patch.object(repair, "LAUNCHER", script):
                controller.request_fix("nisi")
                deadline = time.monotonic() + 5
                children = []
                while time.monotonic() < deadline:
                    with controller._owner_children._lock:
                        children = list(controller._owner_children._children.values())
                    if children and ("server-idle", "idle") in self.steps(controller.read()):
                        break
                    time.sleep(.005)
                self.assertEqual(len(children), 1)
                controller.cancel()
                self.assertTrue(controller.join(.45))
                self.assertEqual(children[0].poll(), -signal.SIGKILL)
            state = controller.read()
            self.assertEqual(state["message"], repair.FIX_NISI_CANCELLED)
            self.assertEqual(self.steps(state)[-2:], [("recover", "interrupted"), ("journal", "written")])
            self.assertEqual(self.pending.read_bytes(), raw)
            self.assert_lock_free(self.router_lock)
        finally:
            controller.cancel()
            controller.join(1)

    def test_one_operation_at_a_time_and_recover_never_runs_twice(self):
        self.write_marker()
        entered, release = threading.Event(), threading.Event()

        def blocking_lms():
            entered.set()
            release.wait(2)
            return copy.deepcopy(self.lms)

        launcher = NisiLauncher(self.state)
        controller, _ = self.controller(launcher, lms_ps=blocking_lms)
        first = controller.request_fix("nisi")
        self.assertTrue(entered.wait(2))
        for again in (controller.request_fix("nisi"), controller.request_fix("route"),
                      controller.request(), controller.request_entry()):
            self.assertEqual((again["operationId"], again["status"], again["action"]),
                             (first["operationId"], "running", "fix-nisi"))
        release.set()
        self.assertTrue(controller.join(3))
        self.assertEqual(controller.read()["status"], "ready")
        second = self.run_fix(controller)
        self.assertEqual(second["operationId"], first["operationId"] + 1)
        self.assertEqual(self.step(second, "nisi-status")["result"], "clear")
        self.assertEqual(launcher.count(repair.NISI_RECOVER), 1)
        self.assertEqual([line["operationId"] for line in self.journal_lines()],
                         [first["operationId"], second["operationId"]])

    def test_journal_is_private_bounded_and_its_failure_does_not_change_the_outcome(self):
        path = self.root / "journal" / "fix.jsonl"
        record = {"kind": "inference-monitor.fix-journal.v1", "action": "fix-nisi", "status": "ready",
                  "steps": [{"name": "route-status", "result": "idle"}]}
        for index in range(200):
            self.assertTrue(repair._append_journal(path, {**record, "operationId": index}, max_bytes=4096))
        self.assertLessEqual(path.stat().st_size, 4096)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(lines[-1]["operationId"], 199)
        self.assertEqual([line["operationId"] for line in lines],
                         list(range(200 - len(lines), 200)))
        self.assertEqual([p.name for p in path.parent.iterdir()], ["fix.jsonl"])
        huge = repair._append_journal(path, {**record, "operationId": 200, "message": "x" * 20000},
                                      max_bytes=65536)
        self.assertTrue(huge)
        self.assertTrue(json.loads(path.read_text().splitlines()[-1])["truncated"])
        # A linked or shared journal is refused, and the fix outcome stands.
        target = self.root / "target.jsonl"
        private_file(target, b"keep\n")
        self.journal.parent.mkdir(mode=0o700)
        self.journal.symlink_to(target)
        controller, _ = self.controller()
        state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(self.steps(state)[-1], ("journal", "failed"))
        self.assertEqual(target.read_bytes(), b"keep\n")
        self.journal.unlink()
        private_file(self.journal, b"", mode=0o644)
        self.assertFalse(repair._append_journal(self.journal, record))

    def test_a_raising_journal_or_clock_never_strands_the_operation_running(self):
        controller, _ = self.controller(clock=lambda: float("nan"))
        with mock.patch.object(repair, "_append_journal", side_effect=RuntimeError("disk")):
            state = self.run_fix(controller)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(self.steps(state)[-1], ("journal", "failed"))

    def test_launcher_runner_accepts_only_the_exact_recover_and_jev_status(self):
        script = prewarmed_script(self.root / "launcher", "import json\nprint(json.dumps(sys.argv[1:]))\n")
        with mock.patch.object(repair, "LAUNCHER", script):
            for args in (("--nisi", "recover"), ("--nisi", "recover", "--confirm-server-idle", "--force"),
                         ("--nisi", "work"), ("--jev", "classify")):
                with self.assertRaisesRegex(ValueError, "unsupported launcher operation"):
                    repair.launcher_runner(args, None, 2)
            for args in (repair.NISI_RECOVER, repair.JEV_STATUS):
                with self.assertRaisesRegex(ValueError, "accepts no input"):
                    repair.launcher_runner(args, b"{}", 2)
                self.assertEqual(json.loads(repair.launcher_runner(args, None, 2).stdout), list(args))
        self.assertEqual(repair.NISI_RECOVER, ("--nisi", "recover", "--confirm-server-idle"))

    def test_production_probes_are_fixed_bounded_commands_and_scripted_runners_fail_closed(self):
        calls = []

        def bounded(arguments, **kwargs):
            calls.append((arguments, kwargs))
            return "[]" if arguments[-1] == "--json" else ""

        with mock.patch.object(repair, "_bounded_command", side_effect=bounded), \
             mock.patch.object(repair, "_find_lms", return_value="/owner/.lmstudio/bin/lms"):
            self.assertEqual(repair.lms_ps_listing(), [])
            self.assertEqual(repair.loopback_api_sockets(), "")
        self.assertEqual(calls[0], (["/owner/.lmstudio/bin/lms", "ps", "--json"],
                                    {"limit": 524288, "timeout": 5.0}))
        self.assertEqual(calls[1], (["/usr/sbin/lsof", "-nP", "-iTCP@127.0.0.1:1234", "-F", "pcnT"],
                                    {"limit": 65536, "timeout": 5.0, "ok_codes": (0, 1)}))
        production = repair.OnlineCodeRepair()
        self.assertIs(production._lms_ps, repair.lms_ps_listing)
        self.assertIs(production._loopback_sockets, repair.loopback_api_sockets)
        self.assertEqual(production._router_lock_path, repair.ROUTER_OWNER_LOCK)
        self.assertEqual(production._fix_journal_path, repair.FIX_JOURNAL)
        # A scripted runner never reads host probes it was not given.
        raw = self.write_marker()
        controller, launcher = self.controller(lms_ps=None, loopback_sockets=None)
        state = self.run_fix(controller)
        self.assertEqual(self.steps(state)[-2], ("server-idle", "unknown"))
        self.assert_not_recovered(launcher, raw)
        scripted = repair.OnlineCodeRepair(NisiLauncher(self.state))
        self.assertEqual((scripted._lms_ps, scripted._loopback_sockets, scripted._router_lock_path,
                          scripted._fix_journal_path), (None, None, None, None))

    def test_socket_listing_parser_rejects_malformed_fields(self):
        for text in ("n127.0.0.1:1234\n", "p907\nf1\nn127.0.0.1:1234\nn127.0.0.1:1235\nTST=LISTEN\n",
                     "p907\nf1\nn127.0.0.1:1234\nTST=LISTEN\nTST=LISTEN\n", "pabc\n",
                     "p907\nf1\nTST=LISTEN\n", "p907\nf1\nn127.0.0.1:1234\nTST=BOGUS\n", None):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    repair._lsof_sockets(text)
        self.assertEqual(repair._lsof_sockets("p907\ncLM Studio\nf78\nn127.0.0.1:1234\nTST=LISTEN\nTQR=0\n"),
                         [{"pid": 907, "command": "LM Studio", "name": "127.0.0.1:1234", "state": "LISTEN"}])

    def test_fix_scope_nisi_is_explicit(self):
        self.assertIn("nisi", repair.FIX_SCOPES)
        self.assertIn("all", repair.FIX_SCOPES)
        controller, _ = self.controller()
        for scope in ("NISI", "nisi+jev", "ALL"):
            with self.assertRaises(ValueError):
                controller.request_fix(scope)
        self.assertEqual(controller.read()["status"], "idle")


if __name__ == "__main__":
    unittest.main()
