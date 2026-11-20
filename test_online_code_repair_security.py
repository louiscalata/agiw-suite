"""Mocked recovery, evidence and journal boundary regressions."""

import test_support.posix_only  # noqa: F401  (skips this module on Windows)

# isort: split
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import online_code_repair

JOB = "mac-security-1234567890"
REAL_WRITE = os.write


def source_module(source, name="_repair_security_source"):
    """Load a source control without changing the imported product module."""
    module = types.ModuleType(name)
    module.__file__ = str(Path(online_code_repair.__file__))
    module.__package__ = ""
    sys.modules[name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    except BaseException:
        del sys.modules[name]
        raise
    return module


repair = (
    source_module(Path(os.environ["REPAIR_SECURITY_SOURCE"]).read_text())
    if "REPAIR_SECURITY_SOURCE" in os.environ
    else online_code_repair
)


def proof():
    return {"status": "success", "jobId": JOB, "model": "fixture-model", "elapsedSeconds": 1.25, "answered": True}


def windows(*, ready=True, inventory=True, pending=None):
    return {
        "kind": "codemode.windows.status.v1",
        "pending": pending,
        "readyForWork": ready,
        "inventory": {"ok": inventory},
        "inference": "NOT_RUN",
    }


def command(value, code=0):
    return repair.CommandResult(code, json.dumps(value).encode())


class RepairSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        for target in ("launcher_runner", "_run_bounded", "_runtime_sources"):
            patcher = mock.patch.object(repair, target, side_effect=AssertionError("unexpected external operation"))
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("kill", "killpg"):
            patcher = mock.patch.object(repair.os, target, side_effect=AssertionError("unexpected signal"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(repair.subprocess, "Popen", side_effect=AssertionError("unexpected process"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def controller(self, result=None, **options):
        result = command(windows()) if result is None else result
        return repair.OnlineCodeRepair(lambda *_: result, l2_open=True, **options)

    def assert_unverified(self, evidence):
        controller = self.controller(inference_probe=lambda: evidence)
        with self.assertRaises(repair._Stop):
            controller._prove_windows_inference()
        self.assertNotIn("verified", [step["result"] for step in controller.read()["steps"]])
        return controller

    def test_rechecks_router_inside_recovery_hold(self):
        controller = self.controller()
        held = False
        observed = []

        @contextmanager
        def hold(_path):
            nonlocal held
            held = True
            try:
                yield "held"
            finally:
                held = False

        def route():
            observed.append(held)
            if held:
                raise repair._Stop("needs-action", "route state changed")

        with (
            mock.patch.object(controller, "_fix_nisi_route_idle", side_effect=route),
            mock.patch.object(controller, "_fix_nisi_owner_status", return_value=(True, True)),
            mock.patch.object(
                controller, "_fix_nisi_marker", return_value={"age": 600, "owner": "fixture", "digest": "a" * 64}
            ),
            mock.patch.object(controller, "_fix_nisi_owner_lock") as owner,
            mock.patch.object(controller, "_fix_nisi_server_idle") as server,
            mock.patch.object(controller, "_fix_nisi_recover") as recover,
            mock.patch.object(controller, "_fix_nisi_pair", return_value=None),
            mock.patch.object(controller, "_fix_nisi_jev", return_value=None),
            mock.patch.object(controller, "_fix_nisi_verify", return_value=None),
            mock.patch.object(repair, "_hold_router_owner", side_effect=hold),
        ):
            with self.assertRaises(repair._Stop):
                controller._fix_nisi()
        self.assertEqual(observed, [False, True])
        owner.assert_not_called()
        server.assert_not_called()
        recover.assert_not_called()

    def test_ready_requires_positive_inventory(self):
        for inventory in (None, {}, {"ok": False}, {"ok": None}, {"ok": 1}, {"ok": "true"}):
            with self.subTest(inventory=inventory):
                value = windows()
                value["inventory"] = inventory
                controller = self.controller(command(value))
                pending, ready = controller._windows_status()
                self.assertIsNone(pending)
                self.assertFalse(ready)
                self.assertEqual(controller.read()["steps"][-1]["result"], "unavailable")
        value = windows()
        del value["pending"]
        self.assertFalse(self.controller(command(value))._windows_status()[1])
        self.assertEqual(self.controller()._windows_status(), (None, True))

    def test_ready_requires_coherent_exit_pending_and_diagnostics(self):
        for change, code in (
            ({}, 3),
            ({"pending": {"id": JOB}}, 0),
            ({"code": "WINDOWS_UNRESOLVED_JOB"}, 0),
            ({"code": "unexpected"}, 0),
            ({"inventory": {"ok": True, "reason": "worker is not running"}}, 0),
        ):
            with self.subTest(change=change, code=code):
                controller = self.controller(command({**windows(), **change}, code))
                _pending, ready = controller._windows_status()
                self.assertFalse(ready)

    def test_unready_inventory_preserves_pending_identity(self):
        value = windows(ready=False, inventory=False, pending={"id": JOB})
        controller = self.controller(command(value, 3))
        self.assertEqual(controller._windows_status(), ({"id": JOB}, False))
        self.assertEqual(controller.read()["steps"][-1]["jobId"], JOB)

    def test_probe_success_requires_every_receipt_field(self):
        for field in proof():
            with self.subTest(field=field):
                evidence = proof()
                del evidence[field]
                self.assert_unverified(evidence)
        controller = self.controller(inference_probe=proof)
        self.assertIn("verified end to end", controller._prove_windows_inference())
        self.assertEqual(controller.read()["steps"][-1]["jobId"], JOB)

    def test_probe_requires_valid_job_and_model(self):
        for field, invalid in (
            ("jobId", (None, "", 3, "mac-short", JOB + "\n", "../job")),
            ("model", (None, "", 3, "model\nsecret", "x" * 97)),
        ):
            for value in invalid:
                with self.subTest(field=field, value=value):
                    self.assert_unverified({**proof(), field: value})

    def test_probe_requires_finite_bounded_elapsed_and_boolean_answer(self):
        for elapsed in (None, True, False, "1.25", -1, 3601, float("nan"), float("inf"), 10**400):
            with self.subTest(elapsed=elapsed):
                self.assert_unverified({**proof(), "elapsedSeconds": elapsed})
        for answered in (None, 1, 0, "true", [], {}):
            with self.subTest(answered=answered):
                controller = self.assert_unverified({**proof(), "answered": answered})
                self.assertEqual(controller.read()["steps"][-1]["result"], "unverified")
        for elapsed in (0, 3600):
            controller = self.controller(inference_probe=lambda elapsed=elapsed: {**proof(), "elapsedSeconds": elapsed})
            self.assertIn("verified end to end", controller._prove_windows_inference())
        controller = self.assert_unverified({**proof(), "answered": False})
        self.assertEqual(controller.read()["steps"][-1]["result"], "returned")

    def test_success_cannot_carry_failure_code(self):
        for code in ("TIMEOUT", "PUBLICATION_UNCERTAIN", "PROBE_FAILED", "unknown", None):
            with self.subTest(code=code):
                self.assert_unverified({**proof(), "code": code})

    def test_invalid_success_does_not_claim_no_publication(self):
        controller = self.controller(inference_probe=lambda: {**proof(), "jobId": None})
        with self.assertRaises(repair._Stop) as stop:
            controller._prove_windows_inference()
        self.assertNotIn("No job was published", stop.exception.message)
        self.assertNotIn("was not sent", stop.exception.message)

    def test_probe_transport_requires_exit_and_unique_json_keys(self):
        raw = json.dumps(proof())
        duplicate = raw.replace('"status": "success"', '"status": "unresolved", "status": "success"')
        for result in (command(proof(), 3), repair.CommandResult(0, duplicate.encode())):
            with self.subTest(result=result), mock.patch.object(repair, "_run_bounded", return_value=result):
                self.assert_unverified(repair.windows_inference_probe())
        with mock.patch.object(repair, "_run_bounded", return_value=command(proof())):
            self.assertEqual(repair.windows_inference_probe(), proof())

    def test_unreadable_probe_result_preserves_dispatch_uncertainty(self):
        for result in (repair.CommandResult(0, b"not json"), command(proof(), 3)):
            with self.subTest(result=result), mock.patch.object(repair, "_run_bounded", return_value=result):
                controller = self.controller(inference_probe=repair.windows_inference_probe)
                with self.assertRaises(repair._Stop) as stop:
                    controller._prove_windows_inference()
                self.assertNotIn("No job was published", stop.exception.message)
                self.assertNotIn("was not sent", stop.exception.message)
                self.assertEqual(controller.read()["steps"][-1]["result"], "unresolved")

    def test_cancel_transition_reports_possible_route_probe(self):
        for action in ("fix-route", "fix-both"):
            for stopped in (False, True):
                with self.subTest(action=action, stopped=stopped):
                    controller = self.controller()
                    controller._state.update(status="running", action=action, operationId=1)
                    with mock.patch.object(controller._owner_children, "cancel", return_value=stopped):
                        controller.cancel()
                    message = controller.read()["message"]
                    self.assertNotIn("No model inference was run", message)
                    self.assertIn("Windows", message)
                    if not stopped:
                        self.assertIn("could not be confirmed terminated", message)

    def test_worker_cancellation_reports_possible_route_probe(self):
        for action, method in (("fix-route", "_check_and_repair"), ("fix-both", "_fix_both")):
            with self.subTest(action=action):
                controller = self.controller()
                controller._state.update(status="running", action=action, operationId=1)
                with mock.patch.object(controller, method, side_effect=repair._Cancelled):
                    controller._work(1, action)
                self.assertNotIn("No model inference was run", controller.read()["message"])
                self.assertIn("Windows", controller.read()["message"])

    def test_probe_dispatch_is_recorded_before_callback_can_cancel(self):
        controller = self.controller()
        controller._state.update(status="running", action="check-and-repair", operationId=1)

        def probe():
            with mock.patch.object(controller._owner_children, "cancel", return_value=True):
                controller.cancel()
            raise repair._Cancelled

        controller._inference_probe = probe
        with self.assertRaises(repair._Cancelled):
            controller._prove_windows_inference()
        self.assertNotIn("No model inference was run", controller.read()["message"])
        self.assertIn("Windows", controller.read()["message"])

    def test_cancelled_owner_cannot_start_probe(self):
        probe = mock.Mock(return_value=proof())
        controller = self.controller(inference_probe=probe)
        controller._owner_children.cancelled.set()
        with self.assertRaises(repair._Cancelled):
            controller._prove_windows_inference()
        probe.assert_not_called()

    def private_file(self, path, data=b""):
        path.write_bytes(data)
        path.chmod(0o600)

    def test_journal_short_writes_complete_append_and_rotation(self):
        for rotate in (False, True):
            with self.subTest(rotate=rotate):
                path = self.root / f"journal-{rotate}.jsonl"
                self.private_file(path, b'{"old":1}\n' * 8)
                lengths = []

                def write(fd, data, lengths=lengths):
                    lengths.append(len(data))
                    return REAL_WRITE(fd, data[:3])

                with mock.patch.object(repair.os, "write", side_effect=write):
                    self.assertTrue(repair._append_journal(path, {"status": "ready"}, max_bytes=64 if rotate else 4096))
                self.assertGreater(len(lengths), 1)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(rows[-1], {"status": "ready"})
                self.assertTrue(path.read_bytes().endswith(b"\n"))
                self.assertFalse(list(self.root.glob(f".{path.name}.*.tmp")))

    def test_journal_zero_or_error_is_never_acknowledged(self):
        initial = b'{"old":1}\n' * 8
        for rotate in (False, True):
            for fail_after, failure in ((0, "zero"), (1, "zero"), (1, "error")):
                with self.subTest(rotate=rotate, fail_after=fail_after, failure=failure):
                    path = self.root / f"failure-{rotate}-{fail_after}-{failure}.jsonl"
                    self.private_file(path, initial)
                    writes = 0

                    def write(fd, data, fail_after=fail_after, failure=failure):
                        nonlocal writes
                        writes += 1
                        if writes > fail_after:
                            if failure == "error":
                                raise OSError("fixture write failure")
                            return 0
                        return REAL_WRITE(fd, data[:3])

                    with mock.patch.object(repair.os, "write", side_effect=write):
                        self.assertFalse(
                            repair._append_journal(path, {"status": "ready"}, max_bytes=64 if rotate else 4096)
                        )
                    if rotate or fail_after == 0:
                        self.assertEqual(path.read_bytes(), initial)
                    self.assertFalse(list(self.root.glob(f".{path.name}.*.tmp")))

    def test_unterminated_journal_tail_blocks_later_append_and_rotation(self):
        path = self.root / "incomplete.jsonl"
        self.private_file(path, b'{"old":1}\n' * 8)
        writes = 0

        def write(fd, data):
            nonlocal writes
            writes += 1
            return REAL_WRITE(fd, data[:3]) if writes == 1 else 0

        with mock.patch.object(repair.os, "write", side_effect=write):
            self.assertFalse(repair._append_journal(path, {"status": "ready"}))
        partial = path.read_bytes()
        self.assertFalse(partial.endswith(b"\n"))
        for cap in (4096, 64):
            with self.subTest(cap=cap):
                self.assertFalse(repair._append_journal(path, {"status": "ready"}, max_bytes=cap))
                self.assertEqual(path.read_bytes(), partial)
                self.assertFalse(list(self.root.glob(f".{path.name}.*.tmp")))

    def test_lock_guards_reject_path_replacement_after_flock(self):
        for guard, refused in ((repair.ShareControl._hold_lock, False), (repair._hold_router_owner, "busy")):
            for change in ("replace", "unlink", "hardlink", "symlink"):
                with self.subTest(guard=guard.__qualname__, change=change):
                    path = self.root / f"owner-{change}.lock"
                    alias = self.root / f"alias-{change}.lock"
                    self.private_file(path)

                    def flock(_fd, operation, change=change, path=path, alias=alias):
                        if operation != repair.fcntl.LOCK_EX | repair.fcntl.LOCK_NB:
                            return
                        if change in ("replace", "symlink"):
                            path.rename(alias)
                            if change == "replace":
                                self.private_file(path)
                            else:
                                path.symlink_to(alias)
                        elif change == "unlink":
                            path.unlink()
                        else:
                            os.link(path, alias)

                    mark = mock.Mock()
                    with (
                        mock.patch.object(repair.fcntl, "flock", side_effect=flock),
                        mock.patch.object(
                            repair,
                            "monitor_router_hold",
                            return_value=mock.MagicMock(__enter__=lambda _, mark=mark: mark),
                        ),
                    ):
                        with guard(path) as held:
                            self.assertEqual(held, refused)
                    if guard is repair._hold_router_owner:
                        mark.release.assert_called_once_with()
                    for candidate in (path, alias):
                        if candidate.exists() or candidate.is_symlink():
                            candidate.unlink()

    def test_nisi_owner_probe_rejects_replacement_during_flock(self):
        path = self.root / "owner.lock"
        self.private_file(path)
        controller = self.controller(nisi_pending_path=self.root / "pending.json")

        def flock(_fd, operation):
            if operation == repair.fcntl.LOCK_EX | repair.fcntl.LOCK_NB:
                path.rename(self.root / "old.lock")
                self.private_file(path)

        with mock.patch.object(repair.fcntl, "flock", side_effect=flock):
            with self.assertRaises(repair._Stop):
                controller._fix_nisi_owner_lock(router="held")
        self.assertEqual(controller.read()["steps"][-1]["result"], "changed")

    def test_lock_identity_requires_regular_single_link_same_device_and_inode(self):
        regular = {"st_mode": stat.S_IFREG | 0o600, "st_nlink": 1, "st_dev": 10, "st_ino": 20}
        for side in ("opened", "current"):
            for field, changed in (("st_mode", stat.S_IFIFO | 0o600), ("st_nlink", 2), ("st_dev", 11), ("st_ino", 21)):
                with self.subTest(side=side, field=field):
                    opened = types.SimpleNamespace(**regular)
                    current = types.SimpleNamespace(**regular)
                    setattr(opened if side == "opened" else current, field, changed)
                    with (
                        mock.patch.object(repair.os, "fstat", return_value=opened),
                        mock.patch.object(repair.os, "stat", return_value=current),
                    ):
                        self.assertFalse(repair._lock_matches_path(41, self.root / "fixture.lock"))
        with mock.patch.object(repair.os, "fstat", side_effect=OSError("fixture lookup failure")):
            self.assertFalse(repair._lock_matches_path(41, self.root / "fixture.lock"))

    def test_stable_private_lock_is_held_without_following_links(self):
        path = self.root / "stable.lock"
        self.private_file(path)
        with mock.patch.object(repair.fcntl, "flock"):
            with repair.ShareControl._hold_lock(path) as held:
                self.assertIs(held, True)
            with repair._hold_router_owner(path) as held:
                self.assertEqual(held, "held")


if __name__ == "__main__":
    unittest.main()
