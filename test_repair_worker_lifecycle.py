"""Repair lifecycle contracts, using deferred workers and no owner or model I/O."""

from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import online_code_repair as repair
import server


class DeferredWorker:
    def __init__(self, *, target, args, name, daemon):
        self.target, self.args = target, args
        self.name, self.daemon = name, daemon
        self.active = False

    def start(self):
        self.active = True

    def complete(self):
        try:
            self.target(*self.args)
        finally:
            self.active = False

    def join(self, timeout):
        pass

    def is_alive(self):
        return self.active


REQUESTS = (
    ("request", (), "check-and-repair", "Checking router and host owner state."),
    ("request_entry", (), "readiness", "Invoking the computer-wide readiness entry."),
    ("request_fix", ("local",), "fix-local", "Checking the selected inference scope."),
    ("request_fix", ("route",), "fix-route", "Checking the selected inference scope."),
    ("request_fix", ("both",), "fix-both", "Checking the selected inference scope."),
    ("request_fix", ("nisi",), "fix-nisi",
     "Checking the router, Nisi owner and model server before any Nisi recovery."),
    ("request_fix", ("all",), "fix-all",
     "Checking local runtime, Nisi Inference and the Windows route under their owner guards."),
    ("request_headless", ("on",), "headless-on", "Probing the PC before turning headless on."),
    ("request_headless", ("off",), "headless-off", "Turning PC headless off."),
)


class LifecycleFixture(unittest.TestCase):
    def setUp(self):
        self.workers = []

        def worker(**kwargs):
            result = DeferredWorker(**kwargs)
            self.workers.append(result)
            return result

        patcher = mock.patch.object(repair.threading, "Thread", side_effect=worker)
        self.thread = patcher.start()
        self.addCleanup(patcher.stop)
        self.runner = mock.Mock(side_effect=AssertionError("owner calls are forbidden"))
        self.addCleanup(self.runner.assert_not_called)

    def controller(self, **kwargs):
        return repair.OnlineCodeRepair(self.runner, l2_open=kwargs.pop("l2_open", True), **kwargs)

    def dispatch(self, controller, path, body=None, *, l2_open=True, capability="fixture-only"):
        handler = server.Handler.__new__(server.Handler)
        handler.server = SimpleNamespace(
            control_capability="fixture-only", server_address=("127.0.0.1", 12345),
            online_code_repair=controller, l2_open=l2_open, action_diagnosis=None)
        raw = json.dumps(body).encode()
        handler.path, handler.rfile = path, BytesIO(raw)
        handler.headers = Message()
        for key, value in {"Host": "127.0.0.1:12345", "Origin": "http://127.0.0.1:12345",
                           "Content-Type": "application/json", "Content-Length": str(len(raw)),
                           "X-AGIW-Control": capability}.items():
            handler.headers[key] = value
        handler.reply = mock.Mock()
        with mock.patch.object(server, "mac_online_code_controls_enabled", return_value=True):
            (handler.do_GET if body is None else handler.do_POST)()
        return handler.reply.call_args.args


class RepairWorkerCharacterizationTests(LifecycleFixture):
    def test_idle_payload_bytes_and_detached_reads(self):
        controller = self.controller()
        self.assertEqual(json.dumps(controller.read()).encode(),
                         b'{"status": "idle", "message": "No check requested yet.", '
                         b'"steps": [], "operationId": 0, "action": null}')
        changed = controller.read()
        changed["steps"].append({"name": "invented", "result": "ignored"})
        self.assertEqual(controller.read()["steps"], [])
        self.assertTrue(controller.join(0))
        self.thread.assert_not_called()

    def test_every_action_preserves_running_payload_and_one_shared_gate(self):
        for method, args, action, message in REQUESTS:
            with self.subTest(action=action):
                controller = self.controller()
                accepted = getattr(controller, method)(*args)
                expected = {"status": "running", "message": message, "steps": [],
                            "operationId": 1, "action": action}
                self.assertEqual(json.dumps(accepted), json.dumps(expected))
                for other, arguments, _, _ in REQUESTS:
                    self.assertEqual(getattr(controller, other)(*arguments), expected)
                self.assertEqual(self.workers[-1].args, (1, action))
                self.assertEqual((self.workers[-1].name, self.workers[-1].daemon),
                                 ("online-code-repair", True))
                accepted["steps"].append({"name": "invented", "result": "ignored"})
                self.assertEqual(controller.read(), expected)
        self.assertEqual(self.thread.call_count, len(REQUESTS))

    def test_closed_l2_payload_and_no_worker(self):
        controller = self.controller(l2_open=False)
        expected = {
            "status": "needs-action", "reasonCode": "l2_closed", "readOnly": True,
            "message": "Owner actions are locked while L2 is closed. No cached machine observations are available; readiness is unknown. Complete and verify the L2 handover, then explicitly start the monitor with --l2-open before requesting this action again.",
            "diagnosis": "Owner actions are locked while L2 is closed. No cached machine observations are available; readiness is unknown.",
            "nextStep": "Complete and verify the L2 handover, then explicitly start the monitor with --l2-open before requesting this action again.",
            "machines": [], "steps": [], "operationId": 0, "action": "fix-all"}
        self.assertEqual(json.dumps(controller.request_fix("all")), json.dumps(expected))
        self.assertEqual(controller.read(), expected)
        self.thread.assert_not_called()

    def test_outcomes_keep_ordered_steps_and_increment_ids_on_retry(self):
        controller = self.controller()
        for operation_id, status in enumerate(("ready", "needs-action", "error"), 1):
            message = f"fixture {status}"

            def stage():
                controller._step("first", "unknown", evidence="fixture")
                controller._step("second", "not-run")
                return status, message

            with mock.patch.object(controller, "_fix_local_runtime", side_effect=stage):
                self.assertEqual(controller.request_fix("local")["operationId"], operation_id)
                self.workers[-1].complete()
            self.assertTrue(controller.join(0))
            self.assertEqual(json.dumps(controller.read()), json.dumps({
                "status": status, "message": message,
                "steps": [{"name": "first", "result": "unknown", "evidence": "fixture"},
                          {"name": "second", "result": "not-run"}],
                "operationId": operation_id, "action": "fix-local"}))

    def test_cancellation_messages_and_latch_survive_late_completion(self):
        general = "Monitor stopped before this check completed. No model inference was run."
        messages = {
            "headless-on": "Monitor stopped while pc-llm was probing the PC; check the headless switch before retrying.",
            "fix-nisi": "Monitor stopped before Fix Nisi Inference completed. If its recover step had started, check Nisi status before retrying. No model inference was run.",
            "fix-all": "Monitor stopped before Fix inference completed. Inspect the recorded steps, Nisi status and Windows owner state before retrying; a probe or recovery may have started."}
        for method, args, action, _ in REQUESTS:
            for cleanup_ok in (False, True):
                with self.subTest(action=action, cleanup_ok=cleanup_ok):
                    controller = self.controller()
                    getattr(controller, method)(*args)
                    controller._step("before-stop", "unknown")
                    controller._owner_children.signal_failed = not cleanup_ok
                    controller.cancel()
                    message = messages.get(action, general)
                    if not cleanup_ok:
                        message = (message + " An owner process group could not be confirmed terminated."
                                   if action in messages else
                                   "Monitor stopped; an owner process group could not be confirmed terminated. Inspect owner processes before retrying. No model inference was run.")
                    expected = {"status": "needs-action", "message": message,
                                "steps": [{"name": "before-stop", "result": "unknown"}],
                                "operationId": 1, "action": action}
                    self.assertEqual(json.dumps(controller.read()), json.dumps(expected))
                    self.workers[-1].complete()
                    for other, arguments, _, _ in REQUESTS:
                        self.assertEqual(getattr(controller, other)(*arguments), expected)

    def test_cancel_retains_idle_or_finished_outcome(self):
        for finish in (False, True):
            controller = self.controller()
            if finish:
                with mock.patch.object(controller, "_fix_local_runtime", return_value=("ready", "fixture")):
                    controller.request_fix("local")
                    self.workers[-1].complete()
            expected = controller.read()
            count = self.thread.call_count
            controller.cancel()
            self.assertEqual(controller.request(), expected)
            self.assertEqual(self.thread.call_count, count)

    def test_fix_journal_bytes_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "fix.jsonl"
            controller = self.controller(fix_journal_path=journal, clock=lambda: 1234.56789)
            for scope in ("nisi", "all"):
                stage_name = "_fix_nisi" if scope == "nisi" else "_fix_all"
                with mock.patch.object(controller, stage_name, return_value=("ready", "fixture ready")):
                    controller.request_fix(scope)
                    controller._step("fixture", "clear", evidence="sanitized")
                    self.workers[-1].complete()
                self.assertEqual(controller.read()["steps"][-1], {"name": "journal", "result": "written"})
            self.assertEqual(journal.read_bytes(),
                             b'{"kind":"inference-monitor.fix-journal.v1","action":"fix-nisi","operationId":1,"finishedUnix":1234.568,"status":"ready","message":"fixture ready","steps":[{"name":"fixture","result":"clear","evidence":"sanitized"}]}\n'
                             b'{"kind":"inference-monitor.fix-journal.v1","action":"fix-all","operationId":2,"finishedUnix":1234.568,"status":"ready","message":"fixture ready","steps":[{"name":"fixture","result":"clear","evidence":"sanitized"}]}\n')
            self.assertEqual(journal.stat().st_mode & 0o777, 0o600)

    def test_http_aliases_codes_and_payload_bytes_without_sockets(self):
        controller = self.controller()
        for path in ("/api/online-code-mode/repair", "/api/inference/fix", "/api/eci-tuning/repair"):
            for scope in ("local", "route", "nisi"):
                self.assertEqual(self.dispatch(controller, path, {"scope": scope, "reviewed": True}),
                                 (202, json.dumps(controller.read()).encode(), "application/json"))
            self.assertEqual(self.dispatch(controller, path),
                             (200, json.dumps(controller.read()).encode(), "application/json"))
            for invalid in ({"scope": "ALL", "reviewed": True}, {"scope": "local"},
                            {"scope": "both", "reviewed": True}, {"scope": "all", "reviewed": True},
                            {"scope": "local", "reviewed": False}, {"scope": "nisi", "reviewed": 1},
                            {"action": "check-and-repair", "reviewed": True}):
                self.assertEqual(self.dispatch(controller, path, invalid)[0], 400)
            self.assertEqual(self.dispatch(controller, path, {"scope": "local", "reviewed": True}, capability="")[0], 403)
            code, raw, mime = self.dispatch(controller, path, {"scope": "local", "reviewed": True}, l2_open=False)
            self.assertEqual((code, mime), (423, "application/json"))
            self.assertEqual(json.loads(raw)["reasonCode"], "l2_closed")

    def test_start_exception_propagates_before_any_work(self):
        controller = self.controller()
        failure = RuntimeError("private start diagnostic")
        with mock.patch.object(DeferredWorker, "start", side_effect=failure), \
             mock.patch.object(controller, "_work") as work:
            with self.assertRaises(RuntimeError) as caught:
                controller.request()
        self.assertIs(caught.exception, failure)
        work.assert_not_called()


class RepairWorkerStartFailureTests(LifecycleFixture):
    def fail_start(self, controller, method="request", args=()):
        with mock.patch.object(DeferredWorker, "start", side_effect=RuntimeError("private diagnostic")):
            with self.assertRaises(RuntimeError):
                getattr(controller, method)(*args)

    def test_start_failure_records_an_honest_outcome_for_every_action(self):
        for method, args, action, _ in REQUESTS:
            with self.subTest(action=action):
                controller = self.controller()
                self.fail_start(controller, method, args)
                self.assertEqual(json.dumps(controller.read()), json.dumps({
                    "status": "error", "message": "Repair worker could not start; no maintenance action was run.",
                    "steps": [], "operationId": 1, "action": action}))

    def test_start_failure_leaves_join_safe(self):
        controller = self.controller()
        # A real unstarted Thread raises from join(); retaining it is unsafe.
        worker = mock.Mock()
        worker.start.side_effect = RuntimeError("private diagnostic")
        worker.join.side_effect = RuntimeError("cannot join thread before it is started")
        self.thread.side_effect = None
        self.thread.return_value = worker
        with self.assertRaises(RuntimeError):
            controller.request()
        self.assertTrue(controller.join(0))
        worker.join.assert_not_called()

    def test_start_failure_releases_monitor_busy(self):
        controller = self.controller()
        owner = SimpleNamespace(online_code_repair=controller,
                                model_control=SimpleNamespace(read=lambda: {"status": "idle"}))
        self.fail_start(controller)
        self.assertIsNone(server.monitor_busy(owner))

    def test_start_failure_allows_next_explicit_request_without_journaling_unrun_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "fix.jsonl"
            controller = self.controller(fix_journal_path=journal)
            self.fail_start(controller, "request_fix", ("all",))
            self.assertFalse(journal.exists())
            with mock.patch.object(controller, "_fix_local_runtime", return_value=("ready", "fixture")) as stage:
                accepted = controller.request_fix("local")
                self.assertEqual((accepted["operationId"], accepted["status"], accepted["action"]),
                                 (2, "running", "fix-local"))
                self.assertEqual(len(self.workers), 2)
                stage.assert_not_called()
                self.workers[-1].complete()
                stage.assert_called_once_with()
            self.assertEqual(controller.read()["status"], "ready")

    def test_cancel_after_start_failure_retains_error_and_closes_admission(self):
        controller = self.controller()
        self.fail_start(controller)
        failed = controller.read()
        controller.cancel()
        self.assertEqual(controller.request(), failed)
        self.assertEqual(failed["status"], "error")
        self.assertEqual(self.thread.call_count, 1)


if __name__ == "__main__":
    unittest.main()
