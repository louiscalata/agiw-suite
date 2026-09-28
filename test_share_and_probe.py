"""Fix Route: SharedChami recovery and the end-to-end Windows inference probe."""

import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

import online_code_repair as repair
import windows_probe
from test_online_code_repair import (ScriptedRunner, bridge, idle, nisi, reply,
                                     windows)

PROBE_JOB = "mac-20260925-081500-" + "d" * 32
SHARE_ENV = {"AGIW_SHARE_HOSTS": "10.222.33.10,10.222.33.20,pc.example.invalid",
             "AGIW_SHARE_USERNAME": "suiteuser"}


class FakeShare:
    def __init__(self, *, mounted=True, stuck=0, dispatch="ok", open_jobs=0, owner_idle=True,
                 host="10.222.33.10", unmount_ok=True, mount_ok=True, after_mount="ok", dispatch_sequence=None,
                 router_free=True):
        self.router_free = router_free
        self.state = {"mounted": mounted, "stuck": stuck, "dispatch": dispatch}
        self.sequence = list(dispatch_sequence or [])
        self.open = open_jobs
        self.idle = owner_idle
        self.host = host
        self.unmount_ok, self.mount_ok, self.after_mount = unmount_ok, mount_ok, after_mount
        self.events = []

    def mounted(self):
        return self.state["mounted"]

    def stuck_readers(self):
        return self.state["stuck"]

    def dispatch_status(self):
        self.events.append("dispatch")
        if self.sequence:
            return self.sequence.pop(0)
        return self.state["dispatch"]

    def open_jobs(self):
        return self.open

    @contextmanager
    def hold_router(self):
        self.events.append("router")
        try:
            yield self.router_free
        finally:
            self.events.append("router-release")

    @contextmanager
    def hold_owner(self):
        self.events.append("hold")
        try:
            yield self.idle
        finally:
            self.events.append("release")

    def reachable_host(self):
        self.events.append("reach")
        return self.host

    def force_unmount(self):
        self.events.append("unmount")
        if self.unmount_ok:
            self.state.update(mounted=False, stuck=0)
        return self.unmount_ok

    def mount(self, host):
        self.events.append(f"mount:{host}")
        if self.mount_ok:
            self.state.update(mounted=True, dispatch=self.after_mount)
        return self.mount_ok

    def settle(self, seconds):
        self.events.append("settle")


def success_probe(**extra):
    calls = []

    def probe():
        calls.append(1)
        return {"status": "success", "jobId": PROBE_JOB, "model": "gpt-oss-20b",
                "elapsedSeconds": 6.25, "answered": True, **extra}
    return probe, calls


FULL_ROUTE = [
    (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, windows()),
    (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
    (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, windows()),
    (repair.ROUTE_STATUS, idle()),
]


class FixRouteShareAndProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.marker = Path(self.temporary.name) / "pending.json"
        self.receipt = Path(self.temporary.name) / "readiness.json"

    def controller(self, script, share, probe, binding="VERIFIED"):
        runner = ScriptedRunner(script, self.receipt)
        return repair.OnlineCodeRepair(runner, nisi_pending_path=self.marker,
                                       readiness_path=self.receipt,
                                       bridge_probe=lambda _: bridge(binding=binding),
                                       share=share, inference_probe=probe), runner

    def finish(self, controller, action="fix-route"):
        started = (controller.request_fix(action.removeprefix("fix-")) if action.startswith("fix-")
                   else controller.request())
        self.assertEqual(started["status"], "running")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = controller.read()
            if state["status"] != "running":
                return state
            time.sleep(0.005)
        self.fail("fix did not finish")

    def steps(self, state):
        return [(s["name"], s["result"]) for s in state["steps"]]

    def test_healthy_share_then_verified_inference_is_ready(self):
        share = FakeShare()
        probe, calls = success_probe()
        controller, runner = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready", state)
        self.assertIn("Windows inference verified end to end: gpt-oss-20b answered in 6.2 s", state["message"])
        self.assertNotIn("No model inference was run", state["message"])
        self.assertIn(("share", "healthy"), self.steps(state))
        self.assertIn(("windows-inference", "verified"), self.steps(state))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("unmount", share.events)
        self.assertEqual(runner.script, [])

    def test_missing_share_config_reports_needs_action_without_probing_or_remounting(self):
        with mock.patch.dict(os.environ, {"AGIW_SHARE_HOSTS": "", "AGIW_SHARE_USERNAME": ""}):
            share = repair.ShareControl()
        probe, calls = success_probe()
        controller, runner = self.controller([(repair.ROUTE_STATUS, idle())], share, probe)
        with mock.patch.object(repair, "_bounded_command") as bounded, \
             mock.patch.object(repair, "_run_bounded") as run, \
             mock.patch.object(repair.socket, "create_connection") as connect:
            state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("AGIW_SHARE_HOSTS", state["message"])
        self.assertIn("AGIW_SHARE_USERNAME", state["message"])
        self.assertIn(("share", "unhealthy"), self.steps(state))
        self.assertEqual(calls, [])
        self.assertEqual(runner.script, [])
        bounded.assert_not_called()
        run.assert_not_called()
        connect.assert_not_called()

    def test_wedged_share_is_force_unmounted_and_remounted_before_owner_calls(self):
        share = FakeShare(stuck=3, dispatch="timeout")
        probe, _ = success_probe()
        script = [(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())] + FULL_ROUTE[1:]
        controller, runner = self.controller(script, share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready", state)
        self.assertEqual([e for e in share.events if e in ("reach", "unmount") or e.startswith("mount:")],
                         ["reach", "unmount", "mount:10.222.33.10"])
        names = self.steps(state)
        self.assertLess(names.index(("share-mount", "mounted")), names.index(("windows-status", "clear")))
        self.assertIn(("share-verify", "healthy"), names)

    def test_unmounted_share_is_mounted_without_force_unmount(self):
        share = FakeShare(mounted=False, dispatch="trusted SharedChami mount unavailable")
        probe, _ = success_probe()
        script = [(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())] + FULL_ROUTE[1:]
        controller, _ = self.controller(script, share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready", state)
        self.assertNotIn("unmount", share.events)
        self.assertIn("mount:10.222.33.10", share.events)

    def test_open_job_defers_recovery_without_touching_the_mount(self):
        for kwargs in ({"open_jobs": 1}, {"open_jobs": None}, {"owner_idle": False}):
            share = FakeShare(stuck=2, dispatch="timeout", **kwargs)
            probe, calls = success_probe()
            controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())],
                                            share, probe)
            state = self.finish(controller)
            self.assertEqual(state["status"], "needs-action")
            self.assertIn("Nothing was unmounted", state["message"])
            self.assertNotIn("unmount", share.events)
            self.assertFalse(any(e.startswith("mount:") for e in share.events))
            self.assertEqual(calls, [])

    def test_nisi_recovery_pending_blocks_share_recovery(self):
        share = FakeShare(stuck=2, dispatch="timeout")
        probe, _ = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi(True))],
                                        share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertNotIn("unmount", share.events)

    def test_unreachable_pc_leaves_share_untouched(self):
        share = FakeShare(stuck=1, dispatch="timeout", host=None)
        probe, _ = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())],
                                        share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("does not answer on the LAN", state["message"])
        self.assertNotIn("unmount", share.events)

    def test_failed_unmount_never_attempts_mount(self):
        share = FakeShare(stuck=1, dispatch="timeout", unmount_ok=False)
        probe, _ = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())],
                                        share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertFalse(any(e.startswith("mount:") for e in share.events))

    def test_unconfirmed_unmount_never_attempts_second_mount(self):
        class UnconfirmedShare(FakeShare):
            def __init__(self, after_unmount):
                super().__init__(stuck=1, dispatch="timeout")
                self.after_unmount = after_unmount

            def force_unmount(self):
                self.events.append("unmount")
                self.state["mounted"] = self.after_unmount
                return True

        for after_unmount in (None, True):
            with self.subTest(after_unmount=after_unmount):
                share = UnconfirmedShare(after_unmount)
                probe, calls = success_probe()
                controller, _ = self.controller([(repair.ROUTE_STATUS, idle()),
                                                  (repair.NISI_STATUS, nisi())], share, probe)
                state = self.finish(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertIn("unmount could not be confirmed", state["message"])
                self.assertIn(("share-unmount", "unverified"), self.steps(state))
                self.assertFalse(any(event.startswith("mount:") for event in share.events))
                self.assertEqual(calls, [])

    def test_remount_that_still_cannot_read_queue_needs_action(self):
        share = FakeShare(mounted=False, dispatch="trusted SharedChami mount unavailable",
                          after_mount="queue I/O unavailable or timed out")
        probe, calls = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())],
                                        share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn(("share-verify", "failed"), self.steps(state))
        self.assertEqual(calls, [])

    def test_degraded_or_unknown_worker_state_is_not_a_share_fault(self):
        share = FakeShare(dispatch="pc-state")
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        self.finish(controller)
        self.assertNotIn("unmount", share.events)
        self.assertNotIn("reach", share.events)

    def test_stale_heartbeat_is_not_treated_as_a_share_fault(self):
        share = FakeShare(dispatch="heartbeat stale or future-dated")
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        self.finish(controller)
        self.assertNotIn("unmount", share.events)
        self.assertNotIn("reach", share.events)

    def test_probe_timeout_is_unresolved_and_not_resent(self):
        share = FakeShare()
        calls = []

        def probe():
            calls.append(1)
            return {"status": "unresolved", "code": "TIMEOUT", "jobId": PROBE_JOB}
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("not resent", state["message"])
        self.assertIn({"name": "windows-inference", "result": "unresolved",
                       "evidence": "code=TIMEOUT", "jobId": PROBE_JOB}, state["steps"])
        self.assertEqual(len(calls), 1)

    def test_open_job_skips_probe(self):
        share = FakeShare()
        share.open = 1
        probe, calls = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("no probe was sent", state["message"])
        self.assertEqual(calls, [])

    def test_worker_error_and_unsent_probe_need_action(self):
        for evidence, text in (({"status": "error", "jobId": PROBE_JOB, "model": "gpt-oss-20b"}, "returned an error"),
                               ({"status": "not-run", "code": "WORKER_NOT_READY"}, "No job was published")):
            controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), lambda e=evidence: e)
            state = self.finish(controller)
            self.assertEqual(state["status"], "needs-action")
            self.assertIn(text, state["message"])

    def test_router_install_refusal_of_the_probe_transport_is_a_not_run_with_its_own_words(self):
        """Spec R2.9 rule 6 (Sol #12): a transport that refused to load under the router's install
        fence is a probe not-run: needs-action, no job published, one probe, and the message tells
        a transition (press again later) from a fence the owner must repair."""
        for code, words in (("ROUTER_INSTALL_IN_PROGRESS", "router install in progress. No job was published."),
                            ("ROUTER_INSTALL_CHANGED", "router install in progress. No job was published."),
                            ("ROUTER_INSTALL_FENCE_MISSING", "refused to load (ROUTER_INSTALL_FENCE_MISSING)"),
                            ("ROUTER_INSTALL_GENERATION_MISMATCH", "finish or roll back the router install")):
            with self.subTest(code=code):
                calls = []
                probe = lambda c=code: calls.append(c) or {"status": "not-run", "code": c}
                controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe)
                state = self.finish(controller)
                self.assertEqual(state["status"], "needs-action")
                self.assertIn(words, state["message"])
                self.assertIn("No job was published", state["message"])
                self.assertIn({"name": "windows-inference", "result": "not-run", "evidence": f"code={code}"},
                              state["steps"])
                self.assertEqual(calls, [code])

    def test_p2conv_a_huge_elapsed_never_hides_the_install_refusal(self):
        """Sol N7: elapsedSeconds is read before the code; math.isfinite(10**400) raised
        OverflowError, so an install refusal carrying one went to the generic error path.  It is
        bounded first: the refusal keeps its own words, and a success with it is not a verified
        answer."""
        huge = json.loads("1" + "0" * 400)
        probe = lambda: {"status": "not-run", "code": "ROUTER_INSTALL_IN_PROGRESS", "elapsedSeconds": huge}
        controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("router install in progress. No job was published.", state["message"])
        probe = lambda: {"status": "success", "model": "qwen", "elapsedSeconds": huge, "answered": True}
        controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertNotIn("verified end to end", state["message"])

    def test_probe_evidence_is_sanitized(self):
        probe = lambda: {"status": "success", "jobId": "../../x", "model": "evil\nmodel; rm",
                         "elapsedSeconds": float("nan"), "answered": True}
        controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        text = json.dumps(state)
        self.assertNotIn("rm", text.replace("form", ""))
        self.assertNotIn("../../x", text)

    def test_nisi_drift_still_needs_action_but_reports_verified_inference(self):
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe, binding="DRIFT")
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertTrue(state["message"].startswith("Windows inference verified end to end"))
        self.assertIn("activation pin changed", state["message"])

    def test_top_button_never_recovers_share_or_probes(self):
        share = FakeShare(stuck=5, dispatch="timeout")
        probe, calls = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller, "repair")
        self.assertEqual(state["status"], "ready")
        self.assertIn("No model inference was run", state["message"])
        self.assertEqual(share.events, [])
        self.assertEqual(calls, [])


    def test_one_contended_sample_is_not_a_wedge(self):
        share = FakeShare(dispatch_sequence=["queue I/O unavailable or timed out", "ok"])
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "ready", state)
        self.assertNotIn("unmount", share.events)
        self.assertIn(("share", "healthy"), self.steps(state))

    def test_io_that_works_is_healthy_even_with_old_stuck_readers(self):
        share = FakeShare(stuck=4)
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertNotIn("unmount", share.events)
        self.assertIn({"name": "share", "result": "healthy", "evidence": "stuck-readers=4"}, state["steps"])

    def test_gates_are_checked_while_the_owner_lock_is_held(self):
        share = FakeShare(stuck=2, dispatch="timeout")
        probe, _ = success_probe()
        script = [(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())] + FULL_ROUTE[1:]
        controller, _ = self.controller(script, share, probe)
        self.finish(controller)
        order = [e for e in share.events if e in ("router", "hold", "unmount", "release", "router-release")
                 or e.startswith("mount:")]
        self.assertEqual(order[:6], ["router", "hold", "unmount", "mount:10.222.33.10", "release", "router-release"])

    def test_remount_with_stopped_worker_counts_as_share_recovered(self):
        share = FakeShare(mounted=False, dispatch="trusted SharedChami mount unavailable",
                          after_mount="worker is not running")
        probe, calls = success_probe()
        script = [(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi()),
                  (repair.WINDOWS_STATUS, windows(ready=False))]
        controller, _ = self.controller(script, share, probe)
        state = self.finish(controller)
        self.assertIn(("share-verify", "healthy"), self.steps(state))
        self.assertNotIn(("share-verify", "failed"), self.steps(state))

    def test_unexpected_probe_answer_is_not_verified(self):
        probe, _ = success_probe(answered=False)
        controller, _ = self.controller(list(FULL_ROUTE), FakeShare(), probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertNotIn("verified", state["message"])
        self.assertIn("not the expected READY", state["message"])
        self.assertIn(("windows-inference", "returned"), self.steps(state))


    def test_busy_router_defers_recovery_and_probe(self):
        share = FakeShare(stuck=2, dispatch="timeout", router_free=False)
        probe, calls = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())], share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertNotIn("unmount", share.events)
        self.assertIn("router=busy", json.dumps(state["steps"]))
        share = FakeShare(router_free=False)
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn({"name": "windows-inference", "result": "deferred", "evidence": "router=busy"}, state["steps"])
        self.assertEqual(calls, [])

    def test_errno_from_a_dead_session_is_a_share_fault(self):
        share = FakeShare(dispatch="other")
        probe, _ = success_probe()
        script = [(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())] + FULL_ROUTE[1:]
        controller, _ = self.controller(script, share, probe)
        state = self.finish(controller)
        self.assertIn("unmount", share.events)
        self.assertEqual(state["status"], "ready", state)

    def test_pre_existing_stuck_readers_are_tolerated_after_a_verified_read(self):
        import telemetry
        self.addCleanup(telemetry.set_windows_worker_tolerance, 0)
        share = FakeShare(stuck=3)
        probe, _ = success_probe()
        controller, _ = self.controller(list(FULL_ROUTE), share, probe)
        self.finish(controller)
        for count, blocked in ((3, False), (4, True), (None, True), (1, False), (2, True)):
            with mock.patch.object(telemetry, "_windows_worker_stuck_readers", return_value=count):
                self.assertEqual(telemetry._windows_worker_reader_blocked(), blocked, count)

    def test_router_busy_never_takes_the_windows_owner_lock(self):
        share = FakeShare(stuck=2, dispatch="timeout", router_free=False)
        probe, _ = success_probe()
        controller, _ = self.controller([(repair.ROUTE_STATUS, idle()), (repair.NISI_STATUS, nisi())], share, probe)
        self.finish(controller)
        self.assertIn("router", share.events)
        self.assertNotIn("hold", share.events)


    def test_degraded_worker_message_names_the_pc_side_fix(self):
        degraded = reply({"kind": "codemode.windows.status.v1", "pending": None, "readyForWork": False,
                          "inventory": {"ok": False, "reason": "worker status degraded"},
                          "code": "WINDOWS_EXACT_LANES_UNAVAILABLE"}, 3)
        script = [(repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, degraded),
                  (repair.NISI_STATUS, nisi()), (repair.READINESS, reply(None)),
                  (repair.NISI_STATUS, nisi()), (repair.WINDOWS_STATUS, degraded),
                  (repair.ROUTE_STATUS, idle()), (repair.WINDOWS_STATUS, degraded),
                  (repair.WINDOWS_STATUS, degraded)]
        probe, calls = success_probe()
        controller, _ = self.controller(script, FakeShare(dispatch="pc-state"), probe)
        controller._sleep = lambda _: None
        state = self.finish(controller)
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("none of its model servers answer", state["message"])
        self.assertIn("No model inference was run", state["message"])
        self.assertEqual(calls, [])


class ShareControlTests(unittest.TestCase):
    def test_mount_table_trust_matches_dispatcher_hosts(self):
        good = "//suiteuser:@pc.example.invalid/SharedChami on /Volumes/SharedChami (smbfs, nodev, nosuid)\n"
        guest = good.replace("suiteuser", "guest")
        with mock.patch.dict(os.environ, SHARE_ENV):
            control = repair.ShareControl()
        for listing, expected in ((good, True),
                                  (good.replace("suiteuser:@", "suiteuser@"), True),
                                  (good.replace("pc.example.invalid", "10.222.33.10"), True),
                                  (good.replace("pc.example.invalid", "evil.example"), None),
                                  (guest, None),
                                  (good + guest, None),
                                  (good + good, None),
                                  (good.replace("/Volumes/SharedChami (", "/Volumes/Other ("), False)):
            with mock.patch.object(repair, "_bounded_command", return_value=listing):
                self.assertEqual(control.mounted(), expected, listing)

    def test_guest_mount_stops_before_network_or_remount(self):
        with mock.patch.dict(os.environ, SHARE_ENV):
            share = repair.ShareControl()
        probe, calls = success_probe()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            receipt = root / "readiness.json"
            controller = repair.OnlineCodeRepair(ScriptedRunner([(repair.ROUTE_STATUS, idle())], receipt),
                                                 nisi_pending_path=root / "pending.json",
                                                 readiness_path=receipt,
                                                 bridge_probe=lambda _: bridge(binding="VERIFIED"),
                                                 share=share, inference_probe=probe)
            listing = "//guest:@pc.example.invalid/SharedChami on /Volumes/SharedChami (smbfs, nodev)\n"
            with mock.patch.object(repair, "_bounded_command", return_value=listing), \
                 mock.patch.object(repair, "_windows_worker_stuck_readers", return_value=0), \
                 mock.patch.object(repair, "_run_bounded") as run, \
                 mock.patch.object(repair.socket, "create_connection") as connect:
                self.assertEqual(controller.request_fix("route")["status"], "running")
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    state = controller.read()
                    if state["status"] != "running":
                        break
                    time.sleep(0.005)
                else:
                    self.fail("fix did not finish")
        self.assertEqual(state["status"], "needs-action")
        self.assertIn("different SMB account", state["message"])
        self.assertEqual(calls, [])
        run.assert_not_called()
        connect.assert_not_called()

    def test_mount_refuses_untrusted_host_and_uses_fixed_commands(self):
        with mock.patch.dict(os.environ, SHARE_ENV):
            control = repair.ShareControl()
        with self.assertRaises(ValueError):
            control.mount('x" & do shell script "id')
        with self.assertRaises(ValueError):
            control.mount("pc.example.invalid")  # inventory-only name is not a remount target
        with mock.patch.object(repair, "_run_bounded", return_value=repair.CommandResult(0, b"")) as run:
            self.assertTrue(control.mount("10.222.33.10"))
            self.assertTrue(control.force_unmount())
        self.assertEqual(run.call_args_list[0].args[0],
                         ["/usr/bin/osascript", "-e", 'mount volume "smb://suiteuser:@10.222.33.10/SharedChami"'])
        self.assertEqual(run.call_args_list[1].args[0], ["/sbin/umount", "-f", "/Volumes/SharedChami"])

    def test_missing_or_malformed_share_config_cannot_touch_mount_or_network(self):
        for hosts, username in (("", ""), ("10.222.33.10", ""),
                                ("10.222.33.10", "guest"),
                                ("10.222.33.10", 'bad"name'),
                                ('10.222.33.10,x".invalid', "suiteuser"),
                                ('10.222.33.10,10.222.33.20,bad".invalid', "suiteuser"),
                                ("127.0.0.1", "suiteuser"),
                                ("8.8.8.8", "suiteuser"),
                                ("192.0.2.10", "suiteuser"),
                                ("169.254.1.1", "suiteuser")):
            with mock.patch.dict(os.environ, {"AGIW_SHARE_HOSTS": hosts,
                                           "AGIW_SHARE_USERNAME": username}):
                control = repair.ShareControl()
            with mock.patch.object(repair, "_bounded_command") as bounded, \
                 mock.patch.object(repair, "_run_bounded") as run, \
                 mock.patch.object(repair.socket, "create_connection") as connect:
                self.assertIsNone(control.mounted())
                self.assertIsNone(control.reachable_host())
                self.assertFalse(control.force_unmount())
                with self.assertRaises(ValueError):
                    control.mount("10.222.33.10")
                bounded.assert_not_called()
                run.assert_not_called()
                connect.assert_not_called()

    def test_hold_owner_ignores_retained_pending_but_respects_a_live_owner(self):
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            with mock.patch.object(repair, "WINDOWS_OWNER_STATE", state):
                control = repair.ShareControl()
                with control.hold_owner() as held:
                    self.assertFalse(held)  # no lock file: cannot exclude an owner
                lock = state / "owner.lock"
                lock.write_bytes(b"")
                (state / "pending.json").write_text("{}")
                with control.hold_owner() as held:
                    self.assertTrue(held)  # retained record, nobody waiting
                    other = os.open(lock, os.O_RDWR)
                    try:
                        with self.assertRaises(OSError):
                            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(other)
                fd = os.open(lock, os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    with control.hold_owner() as held:
                        self.assertFalse(held)
                finally:
                    os.close(fd)

    def test_dispatch_status_classifies_without_leaking_reasons(self):
        control = repair.ShareControl()
        cases = [({"ok": True}, "ok"),
                 ({"ok": False, "reason": "BridgeError: trusted SharedChami mount unavailable"},
                  "trusted SharedChami mount unavailable"),
                 ({"ok": False, "reason": "queue I/O unavailable or timed out"}, "queue I/O unavailable or timed out"),
                 ({"ok": False, "reason": "/Users/secret path"}, "other"),
                 ({"ok": False, "reason": "OSError: [Errno 6] Device not configured: '/Volumes/SharedChami'"}, "other"),
                 ({"ok": False, "reason": "worker status degraded"}, "pc-state"),
                 ({"ok": False, "reason": "heartbeat stale or future-dated"}, "pc-state"),
                 ({"ok": False, "reason": None}, "other"),
                 ({"ok": False, "reason": "BridgeError: JSONDecodeError: Expecting value: line 1"}, "pc-state"),
                 ({"ok": False, "reason": "BridgeError: queue evidence exceeds bounded limit"}, "pc-state"),
                 ({"ok": False, "reason": "BridgeError: duplicate JSON evidence key"}, "pc-state")]
        for value, expected in cases:
            with mock.patch.object(repair, "_run_bounded",
                                   return_value=repair.CommandResult(2, json.dumps(value).encode())):
                self.assertEqual(control.dispatch_status(), expected)
        with mock.patch.object(repair, "_run_bounded",
                               side_effect=repair.subprocess.TimeoutExpired("x", 10)):
            self.assertEqual(control.dispatch_status(), "timeout")

    def test_probe_runner_timeout_maps_to_timeout(self):
        with mock.patch.object(repair, "_run_bounded",
                               side_effect=repair.subprocess.TimeoutExpired("x", 1)) as run:
            self.assertEqual(repair.windows_inference_probe(), {"status": "timeout"})
        self.assertGreaterEqual(run.call_args.args[2], repair.PROBE_MODEL_SECONDS + 30)


    def test_kill_group_treats_macos_eperm_on_a_reaped_leader_as_stopped(self):
        import subprocess as sp
        child = sp.Popen(["/bin/sleep", "30"], start_new_session=True)
        os_kill = repair.os.killpg
        calls = []

        def fake_killpg(pid, sig):
            calls.append(pid)
            if len(calls) == 1:
                os_kill(pid, sig)
                raise PermissionError(1, "Operation not permitted")
            return os_kill(pid, sig)
        with mock.patch.object(repair.os, "killpg", side_effect=fake_killpg):
            self.assertTrue(repair._kill_private_group(child))
        self.assertIsNotNone(child.poll())

    def test_router_and_owner_locks_are_separate_and_non_blocking(self):
        import fcntl
        import os
        with tempfile.TemporaryDirectory() as temp:
            router = Path(temp) / "router.lock"
            router.write_bytes(b"")
            with mock.patch.object(repair, "ROUTER_OWNER_LOCK", router):
                control = repair.ShareControl()
                fd = os.open(router, os.O_RDWR)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    with control.hold_router() as free:
                        self.assertFalse(free)
                finally:
                    os.close(fd)
                with control.hold_router() as free:
                    self.assertTrue(free)


class WindowsProbeScriptTests(unittest.TestCase):
    def run_probe(self, owner_cls, argv=("windows_probe.py", "60")):
        class TransportError(RuntimeError):
            def __init__(self, code, message, job_id=None):
                super().__init__(message)
                self.code, self.job_id = code, job_id
        module = types.SimpleNamespace(Owner=owner_cls(TransportError), TransportError=TransportError)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"windows_queue_transport": module}), redirect_stdout(out):
            code = windows_probe.main(list(argv))
        return code, json.loads(out.getvalue())

    @staticmethod
    def owner(models=("Qwen3.8-27B Q4_K_M", "gpt-oss-20b"), result=None, raise_code=None, job=None):
        def factory(TransportError):
            class Owner:
                requests = []

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def status(self):
                    return {"ok": True, "models": list(models)}

                def request(self, prompt, model, timeout):
                    Owner.requests.append((prompt, model, timeout))
                    if raise_code:
                        raise TransportError(raise_code, "x", job)
                    return result or {"status": "success", "id": PROBE_JOB, "model": model,
                                      "output": "READY", "elapsed_seconds": 4.5}
            return Owner
        return factory

    def test_router_concurrency_transport_codes_are_named_not_generic(self):
        """Spec R2.9 rule 6: lane tokens, the job binding and the owner identity refuse with
        their own codes (no job published), and the Fix controller accepts each of them."""
        for code in ("LANE_BUSY", "LANE_UNKNOWN", "PUBLICATION_INTERRUPTED", "PUBLICATION_BINDING_FAILED",
                     "PUBLICATION_BUDGET_EXHAUSTED", "OWNER_LOCK_REPLACED", "PENDING_CHANGED"):
            with self.subTest(code=code):
                exit_code, value = self.run_probe(self.owner(raise_code=code))
                self.assertEqual(exit_code, 3)
                self.assertEqual(value, {"status": "not-run", "code": code, "jobId": None})
                self.assertIn(code, repair.PROBE_CODES)

    def test_a_transport_refusing_to_load_mid_install_is_a_probe_not_run(self):
        """Spec R2.9 rule 6: the transport is a router generation file; during an install or a
        rollback it refuses to load (ROUTER_INSTALL_*), which is a not-run, not an error."""
        class FenceError(RuntimeError):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        class Refusing:
            def __init__(self, code):
                self.refusal = code

            def __getattr__(self, name):
                raise FenceError(self.refusal)

        for code, expected in (("ROUTER_INSTALL_IN_PROGRESS", "ROUTER_INSTALL_IN_PROGRESS"),
                               ("ROUTER_INSTALL_ROLLED_BACK", "ROUTER_INSTALL_ROLLED_BACK"),
                               ("SOMETHING_ELSE", "OWNER_UNAVAILABLE")):
            with self.subTest(code=code):
                out = io.StringIO()
                with mock.patch.dict(sys.modules, {"windows_queue_transport": Refusing(code)}), \
                        redirect_stdout(out):
                    exit_code = windows_probe.main(["windows_probe.py", "60"])
                self.assertEqual(exit_code, 3)
                self.assertEqual(json.loads(out.getvalue()), {"status": "not-run", "code": expected})
                self.assertIn(expected, repair.PROBE_CODES)

    def test_success_prefers_small_model_and_hides_output(self):
        code, value = self.run_probe(self.owner(result={"status": "success", "id": PROBE_JOB,
                                                        "model": "gpt-oss-20b", "output": "READY secret",
                                                        "elapsed_seconds": 4.5}))
        self.assertEqual(code, 0)
        self.assertEqual(value, {"status": "success", "jobId": PROBE_JOB, "model": "gpt-oss-20b",
                                 "elapsedSeconds": 4.5, "answered": True})

    def test_timeout_reports_unresolved_job(self):
        code, value = self.run_probe(self.owner(raise_code="TIMEOUT", job=PROBE_JOB))
        self.assertEqual(code, 3)
        self.assertEqual(value, {"status": "unresolved", "code": "TIMEOUT", "jobId": PROBE_JOB})

    def test_busy_owner_is_not_run(self):
        code, value = self.run_probe(self.owner(raise_code="OWNER_BUSY"))
        self.assertEqual(value["status"], "not-run")
        self.assertEqual(value["code"], "OWNER_BUSY")

    def test_invalid_timeout_is_refused(self):
        code, value = self.run_probe(self.owner(), ("windows_probe.py", "9999"))
        self.assertEqual(value, {"status": "not-run", "code": "INVALID_TIMEOUT"})


    def test_no_fallback_to_a_slow_model(self):
        owner = self.owner(models=("Qwen3.8-27B Q4_K_M",))
        code, value = self.run_probe(owner)
        self.assertEqual(value, {"status": "not-run", "code": "WORKER_NOT_READY"})

    def test_post_publish_failure_without_job_id_reports_the_pending_job(self):
        def factory(TransportError):
            class Owner:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def status(self):
                    return {"ok": True, "models": ["gpt-oss-20b"]}

                def request(self, prompt, model, timeout):
                    raise OSError("archive write failed")

                def pending(self):
                    return {"id": PROBE_JOB, "model": model if False else "gpt-oss-20b"}
            return Owner
        code, value = self.run_probe(factory)
        self.assertEqual(value, {"status": "unresolved", "code": "PROBE_FAILED", "jobId": PROBE_JOB})


if __name__ == "__main__":
    unittest.main()
