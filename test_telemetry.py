import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import telemetry

_NO_WINDOWS_WORKER = (
    {"state": "unavailable", "ageSeconds": None, "modelsAdvertised": [],
     "modelCount": 0, "detail": "No recent registered Windows worker heartbeat"},
    {"id": "windows-worker", "label": "Windows worker", "state": "unavailable",
     "ageSeconds": None, "detail": "No recent registered Windows worker heartbeat"},
)


class AFMExecutableStatusTests(unittest.TestCase):
    def test_owned_executable_is_metadata_only_and_never_invoked(self):
        with tempfile.TemporaryDirectory() as temp:
            adapter = Path(temp) / "afm"
            adapter.write_bytes(b"test adapter fixture")
            adapter.chmod(0o700)
            with mock.patch.object(telemetry.subprocess, "Popen",
                                   side_effect=AssertionError("AFM must not run")):
                result = telemetry._afm_status(adapter)
            self.assertEqual(result["state"], "executable")
            self.assertEqual(result["callability"], "permission-granted")
            self.assertEqual(result["inference"], "NOT_TESTED")
            self.assertEqual(result["role"], "optional-advisory-intake")
            adapter.chmod(0o600)
            self.assertEqual(telemetry._afm_status(adapter)["state"], "not-executable")
            adapter.unlink()
            self.assertEqual(telemetry._afm_status(adapter)["state"], "missing")

    def test_symlink_and_foreign_owner_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = root / "afm"
            target = root / "target"
            target.write_bytes(b"fixture")
            target.chmod(0o700)
            adapter.symlink_to(target)
            self.assertEqual(telemetry._afm_status(adapter)["state"], "untrusted")
            adapter.unlink()
            adapter.write_bytes(b"fixture")
            adapter.chmod(0o700)
            with mock.patch.object(telemetry.os, "getuid", return_value=os.getuid() + 1):
                self.assertEqual(telemetry._afm_status(adapter)["state"], "untrusted")


class TelemetryParsingTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName == 'test_online_mode_process_probe_rejects_read_only_or_wrong_command':
            return  # This case checks the real installed script path in ps output.
        # This class exercises the pre-fence owner.lock journal. Point its code
        # provenance at a legacy fixture so an installed router_fence.py on the
        # test host cannot make the isolated temporary journal appear broken.
        legacy_script = Path(__file__).resolve().parent / '_legacy_router_fixture/pipeline_router.py'
        patcher = mock.patch.object(telemetry, '_ROUTER_SCRIPT', legacy_script)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _wait_for_windows_probe(cache):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with telemetry._WINDOWS_WORKER_LOCK:
                if not cache.get("probeRunning"):
                    return
            time.sleep(0.01)
        raise AssertionError("Windows heartbeat probe did not finish")

    def test_bounded_command_timeout_stops_nested_reader(self):
        # A timed-out dispatcher must not leave its SMB I/O child alive.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "child-progress"
            pid_file = root / "child-pid"
            child = (
                "from pathlib import Path\n"
                "import sys,time\n"
                "p=Path(sys.argv[1]); n=0\n"
                "while True:\n"
                " p.write_text(str(n)); n+=1; time.sleep(.02)\n"
            )
            parent = root / "parent.py"
            parent.write_text(
                "import os,subprocess,sys,time\n"
                f"child={child!r}\n"
                "p=subprocess.Popen([sys.executable,'-c',child,sys.argv[1]])\n"
                "deadline=time.monotonic()+1\n"
                "while not os.path.exists(sys.argv[1]) and time.monotonic()<deadline:\n"
                " time.sleep(.01)\n"
                "open(sys.argv[2],'w').write(str(p.pid))\n"
                "time.sleep(10)\n"
            )
            child_pid = None
            try:
                with self.assertRaises(TimeoutError):
                    telemetry._bounded_command(
                        [sys.executable, str(parent), str(marker), str(pid_file)],
                        timeout=1.5)
                self.assertTrue(pid_file.exists())
                self.assertTrue(marker.exists())
                child_pid = int(pid_file.read_text())
                before = marker.read_text() if marker.exists() else None
                time.sleep(0.15)
                after = marker.read_text() if marker.exists() else None
                self.assertEqual(after, before)
            finally:
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @staticmethod
    def _router_journal(root, active=None):
        root.mkdir(mode=0o700)
        (root / "archive").mkdir(mode=0o700)
        (root / "checkpoints").mkdir(mode=0o700)
        (root / "owner.lock").write_text("")
        (root / "owner.lock").chmod(0o600)
        if active is not None:
            (root / "active.json").write_text(json.dumps(active))
            (root / "active.json").chmod(0o600)

    @staticmethod
    @contextlib.contextmanager
    def _owner_lock_held(root):
        """A legacy route holds owner.lock EX for its whole run (spec R2.9 legacy rule (i))."""
        fd = os.open(str(root / "owner.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _active_route(now):
        return {"schemaVersion": 1, "runId": "bound-route", "inputSha256": "a" * 64,
                "stage": "backend_draft", "sequence": 1, "startedUnix": now - 15,
                "checkpoint": {"schemaVersion": 1, "runId": "bound-route",
                               "inputSha256": "a" * 64, "sequence": 1,
                               "stage": "backend_draft", "recordedUnix": now - 5,
                               "evidence": {"prompt": "PRIVATE_ROUTER_TASK"}}}

    def test_online_mode_inactive_requires_initialized_empty_journal(self):
        now = 2_000_000_000.0
        observed = "2033-05-18T03:33:20Z"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_LAUNCHER_ROOT", Path(temp) / "launcher"), \
                 mock.patch.object(telemetry, "_READINESS_PATH", Path(temp) / "launcher/readiness.json"):
                missing = telemetry._online_code_mode(now, observed)
                self._router_journal(root)
                idle = telemetry._online_code_mode(now, observed)
        self.assertEqual(missing["state"], "unknown")
        self.assertIsNone(missing["active"])
        self.assertEqual(idle["state"], "inactive")
        self.assertIs(idle["active"], False)
        self.assertFalse(idle["blinking"])
        self.assertEqual(idle["observedAt"], observed)

    def test_online_mode_recent_readiness_is_steady_green_without_task_attribution(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            launcher = Path(temp) / "launcher"
            self._router_journal(root)
            launcher.mkdir(mode=0o700)
            receipt = launcher / "readiness.json"
            record = {"schemaVersion": 1, "status": "PREFLIGHT_COMPLETED",
                      "observedAtUnix": now - 12, "source": "online-code-mode",
                      "client": None, "chatId": None}
            receipt.write_text(json.dumps(record))
            receipt.chmod(0o600)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_LAUNCHER_ROOT", launcher), \
                 mock.patch.object(telemetry, "_READINESS_PATH", receipt):
                ready = telemetry._online_code_mode(now, "now")
                expired = telemetry._online_code_mode(now + 601, "later")
                record["client"] = "invented-client"
                receipt.write_text(json.dumps(record))
                invalid = telemetry._online_code_mode(now, "now")
        self.assertEqual(ready["state"], "ready")
        self.assertIs(ready["active"], False)
        self.assertFalse(ready["blinking"])
        self.assertIsNone(ready["client"])
        self.assertIsNone(ready["chatId"])
        self.assertIsNone(ready["routeId"])
        self.assertIn("12s ago", ready["evidence"])
        self.assertEqual(expired["state"], "inactive")
        self.assertIs(expired["active"], False)
        self.assertIn("readiness is unverified", expired["evidence"])
        self.assertEqual(invalid["state"], "inactive")
        self.assertIs(invalid["active"], False)
        self.assertIn("invalid or unreadable", invalid["evidence"])

    def test_online_mode_processing_requires_fresh_bound_pointer_and_live_owner(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            self._router_journal(root, self._active_route(now))
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 16)), \
                 self._owner_lock_held(root):
                mode = telemetry._online_code_mode(now, "2033-05-18T03:33:20Z")
        self.assertEqual(mode["state"], "processing")
        self.assertIs(mode["active"], True)
        self.assertTrue(mode["blinking"])
        self.assertEqual(mode["routeId"], "bound-route")
        self.assertIsNone(mode["client"])
        self.assertIsNone(mode["chatId"])
        self.assertNotIn("PRIVATE_ROUTER_TASK", json.dumps(mode))
        self.assertIn("client and chat binding unavailable", mode["evidence"])

    def test_online_mode_incomplete_checkpoint_never_blinks_with_live_owner(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            record = self._active_route(now)
            record["stage"] = "incomplete"
            record["checkpoint"]["stage"] = "incomplete"
            record["checkpoint"]["evidence"] = {"recoveryRequired": True}
            self._router_journal(root, record)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 16)) as owner:
                mode = telemetry._online_code_mode(now, "now")
                owner.assert_not_called()
                record["checkpoint"]["recordedUnix"] = now - 301
                (root / "active.json").write_text(json.dumps(record))
                stale = telemetry._online_code_mode(now, "later")
                owner.assert_not_called()
        self.assertEqual(mode["state"], "unknown")
        self.assertIsNone(mode["active"])
        self.assertEqual(mode["taskState"], "unfinished")
        self.assertFalse(mode["blinking"])
        self.assertIn("incomplete checkpoint", mode["evidence"])
        self.assertEqual(stale["state"], "unknown")
        self.assertIsNone(stale["active"])
        self.assertEqual(stale["taskState"], "unfinished")
        self.assertFalse(stale["blinking"])
        self.assertIn("stale", stale["evidence"])

    def test_online_mode_checkpoint_alone_never_claims_processing(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            record = self._active_route(now)
            self._router_journal(root, record)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=None):
                without_owner = telemetry._online_code_mode(now, "now")
            record["checkpoint"]["recordedUnix"] = now - 600
            (root / "active.json").write_text(json.dumps(record))
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 16)) as owner:
                stale = telemetry._online_code_mode(now, "now")
                owner.assert_not_called()
        for mode in (without_owner, stale):
            self.assertEqual(mode["state"], "unknown")
            self.assertIsNone(mode["active"])
            self.assertFalse(mode["blinking"])

    def test_online_mode_stalled_owner_stops_blinking_after_freshness_limit(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            record = self._active_route(now)
            record["startedUnix"] = now - 400
            record["checkpoint"]["recordedUnix"] = now - 299
            self._router_journal(root, record)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 410)) as owner, \
                 self._owner_lock_held(root):
                recent = telemetry._online_code_mode(now, "now")
                record["checkpoint"]["recordedUnix"] = now - 301
                (root / "active.json").write_text(json.dumps(record))
                stalled = telemetry._online_code_mode(now, "now")
        self.assertEqual(recent["state"], "processing")
        self.assertTrue(recent["blinking"])
        # R2.9 rule 3: liveness is (i)-(iv), not the checkpoint's age.  A route still holding
        # the owner lock with its exact work process is live but stalled: it stops blinking
        # and says so, and is never shown as a dead, unfinished run.
        self.assertEqual((stalled["state"], stalled["active"], stalled["taskState"]),
                         ("processing", True, "processing"))
        self.assertFalse(stalled["blinking"])
        self.assertIn("no checkpoint for 301s", stalled["evidence"])
        self.assertIn("may be stalled", stalled["evidence"])
        self.assertEqual(owner.call_count, 4)

    def test_online_mode_rejects_other_route_process_and_changed_record(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            record = self._active_route(now)
            self._router_journal(root, record)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(322, now - 1)):
                later_process = telemetry._online_code_mode(now, "now")
            original = telemetry._safe_file
            active_reads = 0
            def swapped(path, limit):
                nonlocal active_reads
                value = original(path, limit)
                if path == root / "active.json":
                    active_reads += 1
                    if active_reads == 2:
                        value["stage"] = "backend_review"
                return value
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 16)), \
                 mock.patch.object(telemetry, "_safe_file", side_effect=swapped), \
                 self._owner_lock_held(root):
                changed = telemetry._online_code_mode(now, "now")
        self.assertEqual(later_process["state"], "unknown")
        self.assertEqual(changed["state"], "unknown")
        self.assertIn("changed", changed["evidence"])

    def test_online_mode_rejects_owner_exit_during_observation(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            self._router_journal(root, self._active_route(now))
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process",
                                   side_effect=[(321, now - 16), None]), \
                 self._owner_lock_held(root):
                mode = telemetry._online_code_mode(now, "now")
        self.assertEqual(mode["state"], "unknown")
        self.assertFalse(mode["blinking"])
        self.assertIn("owner process changed", mode["evidence"])

    def test_online_mode_rejects_invalid_checkpoint_binding(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            record = self._active_route(now)
            record["checkpoint"]["runId"] = "other-route"
            self._router_journal(root, record)
            with mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_router_owner_process", return_value=(321, now - 16)) as owner:
                mode = telemetry._online_code_mode(now, "now")
                owner.assert_not_called()
        self.assertEqual(mode["state"], "unknown")
        self.assertIsNone(mode["client"])
        self.assertIsNone(mode["chatId"])

    def test_online_mode_process_probe_rejects_read_only_or_wrong_command(self):
        lock_path = str(telemetry._ROUTER_ROOT / "owner.lock")
        owner_line = f"p123\nf3\nau\nn{lock_path}\n"
        ps_line = ("  123 Wed Sep 23 23:14:44 2026     /usr/bin/python3 -I -B "
                   f"{telemetry._ROUTER_SCRIPT} work\n")
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(telemetry, "_bounded_command",
                               side_effect=[owner_line, ps_line]):
            self.assertEqual(telemetry._router_owner_process()[0], 123)
        # What ps really shows for /usr/bin/python3 (Xcode) and Homebrew python3.
        for interpreter in (
                "/Applications/Xcode-27.0.app/Contents/Developer/Library/Frameworks/Python3.framework/"
                "Versions/3.9/Resources/Python.app/Contents/MacOS/Python",
                "/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/"
                "Resources/Python.app/Contents/MacOS/Python"):
            framework_line = ps_line.replace("/usr/bin/python3", interpreter)
            with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command", side_effect=[owner_line, framework_line]):
                self.assertEqual(telemetry._router_owner_process()[0], 123)
        for impostor in ("/tmp/Python", "/tmp/evil/Python.app/Contents/MacOS/Python"):
            with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command",
                                   side_effect=[owner_line, ps_line.replace("/usr/bin/python3", impostor)]):
                self.assertIsNone(telemetry._router_owner_process())
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(telemetry, "_bounded_command",
                               return_value=owner_line.replace("au", "ar")):
            self.assertIsNone(telemetry._router_owner_process())
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(telemetry, "_bounded_command",
                               side_effect=[owner_line, ps_line.replace(" work", " status")]):
            self.assertIsNone(telemetry._router_owner_process())

    def test_router_owner_lsof_probe_pauses_while_shared_queue_reader_is_stalled(self):
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=True), \
             mock.patch.object(telemetry, "_bounded_command") as bounded:
            self.assertIsNone(telemetry._router_owner_process())
            bounded.assert_not_called()

    @staticmethod
    def _canary_record(root, events, enabled=True):
        root.mkdir(mode=0o700)
        config = {"mode": "review", "targets": ["registered-task"]}

        def digest(value):
            raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
            return hashlib.sha256(raw).hexdigest()

        record = {"schema": 1, "config": config, "configHash": digest(config),
                  "enabled": enabled, "events": events}
        record["integrityHash"] = digest(record)
        path = root / "state.json"
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        return path

    def test_canary_counts_are_recorded_only_and_private_fields_never_escape(self):
        now = 2_000_000_000.0
        secret = "PRIVATE_CANARY_PROMPT"

        def event(name, phase, **extra):
            return {"eventId": name, "state": phase, "createdAt": int(now - 3600),
                    "payload": {"request": {"task": secret}, "sourceSha256": "a" * 64},
                    "claimToken": "PRIVATE_CLAIM_TOKEN", **extra}

        events = {
            "ready": event("ready", "READY"),
            "claimed": event("claimed", "CLAIMED", claimAt=int(now - 1200)),
            "sent": event("sent", "SENT", claimAt=int(now - 900)),
            "received": event("received", "RECEIVED", claimAt=int(now - 800)),
            "verified": event("verified", "VERIFIED", claimAt=int(now - 700)),
            "held": event("held", "HELD", heldAt=int(now - 60), heldBlocksTarget=True),
            "resolved": event("resolved", "HELD", heldAt=int(now - 120), heldBlocksTarget=False),
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nisi-canary"
            path = self._canary_record(root, events)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path):
                summary, source = telemetry._canary(now)
        self.assertEqual(source["state"], "recorded")
        self.assertEqual(summary["enabled"], True)
        self.assertEqual(summary["stateCounts"], {
            "READY": 1, "CLAIMED": 1, "SENT": 1, "RECEIVED": 1,
            "VERIFIED": 1, "HELD": 2,
        })
        self.assertEqual(summary["outstanding"], 4)
        self.assertEqual(summary["latestRecordedTransitionAgeSeconds"], 60.0)
        self.assertIn("Scheduler and target activity unverified", source["detail"])
        public = json.dumps(summary) + json.dumps(source)
        for private in (secret, "PRIVATE_CLAIM_TOKEN", "a" * 64, "registered-task"):
            self.assertNotIn(private, public)

    def test_canary_enabled_stale_ledger_is_not_a_live_scheduler_signal(self):
        now = 2_000_000_000.0
        events = {"old": {"eventId": "old", "state": "READY", "createdAt": int(now - 86400)}}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nisi-canary"
            path = self._canary_record(root, events)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path):
                summary, source = telemetry._canary(now)
        self.assertEqual(source["state"], "recorded")
        self.assertEqual(source["ageSeconds"], 86400.0)
        self.assertEqual(summary["outstanding"], 0)
        self.assertNotIn("live", json.dumps(source))

    def test_canary_disabled_ledger_and_absent_ledger_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nisi-canary"
            path = self._canary_record(root, {}, enabled=False)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path):
                disabled, recorded = telemetry._canary(time.time())
            self.assertEqual(recorded["state"], "recorded")
            self.assertEqual(disabled["enabled"], False)
            self.assertEqual(disabled["outstanding"], 0)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", root / "missing.json"):
                unknown, missing = telemetry._canary(time.time())
            self.assertEqual(missing["state"], "unavailable")
            self.assertIsNone(unknown["enabled"])
            self.assertIsNone(unknown["stateCounts"])

    def test_canary_corruption_and_unsafe_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nisi-canary"
            path = self._canary_record(root, {})
            record = json.loads(path.read_text())
            record["enabled"] = False  # Integrity hash no longer matches.
            path.write_text(json.dumps(record))
            path.chmod(0o600)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path):
                summary, source = telemetry._canary(time.time())
            self.assertEqual(source["state"], "error")
            self.assertIsNone(summary["enabled"])
            root.chmod(0o755)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path):
                summary, source = telemetry._canary(time.time())
            self.assertEqual(source["state"], "error")
            root.chmod(0o700)
            link = root / "linked-state.json"
            link.symlink_to(path)
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", link):
                summary, source = telemetry._canary(time.time())
            self.assertEqual(source["state"], "error")

    def test_snapshot_includes_only_canary_aggregate_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "nisi-canary"
            path = self._canary_record(root, {
                "job": {"eventId": "job", "state": "CLAIMED",
                        "createdAt": int(time.time() - 120), "claimAt": int(time.time() - 60),
                        "claimToken": "SECRET_DISPATCH_TOKEN",
                        "payload": {"request": "SECRET_TASK_TEXT", "targetThreadId": "SECRET_TARGET"}},
            })
            with mock.patch.object(telemetry, "_CANARY_ROOT", root), \
                 mock.patch.object(telemetry, "_CANARY_PATH", path), \
                 mock.patch.object(telemetry, "_bounded_http", return_value={"models": []}), \
                 mock.patch.object(telemetry, "_bounded_lms", return_value={"models": []}), \
                 mock.patch.object(telemetry, "_windows_worker", return_value=_NO_WINDOWS_WORKER), \
                 mock.patch.object(telemetry, "_pipeline", return_value=(
                     {"runId": None, "status": "idle", "stage": None, "recoveryRequired": False,
                      "ageSeconds": None, "steps": [], "usage": {}},
                     {"id": "pipeline-router", "label": "Pipeline", "state": "recorded",
                      "ageSeconds": None, "detail": "No unresolved route"})):
                snapshot = telemetry.collect_snapshot()
        self.assertEqual(snapshot["canary"]["stateCounts"]["CLAIMED"], 1)
        self.assertEqual(snapshot["canary"]["outstanding"], 1)
        source = next(item for item in snapshot["sources"] if item["id"] == "canary-ledger")
        self.assertEqual(source["state"], "recorded")
        public = json.dumps(snapshot)
        for private in ("SECRET_DISPATCH_TOKEN", "SECRET_TASK_TEXT", "SECRET_TARGET"):
            self.assertNotIn(private, public)

    def test_lms_exact_activity_states_and_unknown_enum(self):
        payload = {"models": [
            {"identifier": "provider/idle", "displayName": "Idle", "status": "idle", "queued": 0},
            {"identifier": "provider/generating", "status": "generating", "queued": 0},
            {"identifier": "provider/prompt", "status": "processingPrompt", "queued": 0},
            {"identifier": "provider/queued", "status": "idle", "queued": 2},
            {"identifier": "provider/future", "status": "warming-up", "queued": 0},
        ]}
        rows = {row["id"]: row for row in telemetry._parse_lms(payload, time.time())}
        self.assertEqual(rows["provider/idle"]["state"], "idle")
        self.assertEqual(rows["provider/generating"]["state"], "generating")
        self.assertEqual(rows["provider/prompt"]["state"], "busy")
        self.assertEqual(rows["provider/queued"]["state"], "idle")
        self.assertEqual(rows["provider/queued"]["queued"], 2)
        self.assertEqual(rows["provider/future"]["state"], "loaded")
        self.assertTrue(all(row["loaded"] is True for row in rows.values()))

    def test_api_inventory_distinguishes_loaded_and_unloaded(self):
        rows = telemetry._parse_api({"models": [
            {"key": "loaded", "display_name": "Loaded", "loaded_instances": [{"id": "i"}],
             "size_bytes": 12, "max_context_length": 32},
            {"key": "downloaded", "loaded_instances": []},
            {"key": "missing-load-field"},
            {"key": "invalid-load-field", "loaded_instances": "unknown"},
            {"key": "malicious", "display_name": "prompt: do not leak", "loaded_instances": [],
             "prompt": "private source content"},
        ]}, time.time())
        self.assertEqual([row["state"] for row in rows],
                         ["loaded", "unloaded", "unknown", "unknown", "unloaded"])
        self.assertIsNone(rows[2]["loaded"])
        self.assertIsNone(rows[3]["loaded"])
        self.assertEqual(rows[0]["sizeBytes"], 12)
        self.assertEqual(rows[0]["context"], 32)
        self.assertNotIn("prompt", json.dumps(rows))
        self.assertNotIn("private source content", json.dumps(rows))

    def test_api_model_details_are_allowlisted_and_survive_cli_merge(self):
        api = telemetry._parse_api({"models": [{
            "key": "google/gemma-3-4b", "display_name": "Gemma 3 4B", "type": "llm",
            "publisher": "google", "architecture": "gemma3", "params_string": "4B",
            "format": "mlx", "quantization": {"name": "4bit", "bits_per_weight": 4},
            "capabilities": {"vision": True, "trained_for_tool_use": False,
                             "reasoning": {"allowed_options": ["off", "low", "on"],
                                           "private_prompt": "SECRET_REASONING_PROMPT"}},
            "loaded_instances": [{"id": "google/gemma-3-4b",
                                  "config": {"context_length": 8192, "parallel": 2,
                                             "reasoning_budget_message": "SECRET_MESSAGE"},
                                  "remaining_ttl_seconds": 300}],
            "description": "SECRET_DESCRIPTION", "prompt": "SECRET_PROMPT",
        }]}, time.time())[0]
        self.assertEqual(api["name"], "Gemma 3 4B")
        self.assertEqual(api["metadata"]["quantization"], "4bit")
        self.assertEqual(api["metadata"]["capabilities"]["reasoningOptions"],
                         ["off", "low", "on"])
        self.assertEqual(api["metadata"]["loadedInstances"][0]["parallel"], 2)
        self.assertEqual(api["modelKey"], "google/gemma-3-4b")
        self.assertEqual(api["loadedInstanceIds"], ["google/gemma-3-4b"])
        cli = telemetry._parse_lms({"models": [{"identifier": "google/gemma-3-4b"}]},
                                   time.time())[0]
        merged = {api["id"]: api}
        telemetry._merge_cli_rows(merged, [cli])
        row = merged[api["id"]]
        self.assertEqual(row["name"], "Gemma 3 4B")
        self.assertEqual(row["metadata"], api["metadata"])
        self.assertEqual(row["modelKey"], "google/gemma-3-4b")
        self.assertEqual(row["loadedInstanceIds"], ["google/gemma-3-4b"])
        public = json.dumps(row)
        for private in ("SECRET_REASONING_PROMPT", "SECRET_MESSAGE",
                        "SECRET_DESCRIPTION", "SECRET_PROMPT"):
            self.assertNotIn(private, public)

    def test_cli_model_key_and_instance_identifier_are_projected_without_config_text(self):
        api = telemetry._parse_api({"models": [{
            "key": "provider/model", "loaded_instances": [{
                "identifier": "alias-1", "config": {"reasoning_budget_message": "SECRET"},
            }],
        }]}, time.time())[0]
        cli = telemetry._parse_lms({"models": [{"identifier": "alias-1",
                                                 "modelKey": "provider/model"}]}, time.time())[0]
        self.assertEqual(api["loadedInstanceIds"], ["alias-1"])
        self.assertEqual(cli["modelKey"], "provider/model")
        self.assertEqual(cli["instanceId"], "alias-1")
        merged = {api["id"]: api}
        telemetry._merge_cli_rows(merged, [cli])
        self.assertIsNone(merged["alias-1"]["loadedInstanceIds"])
        self.assertEqual(merged["alias-1"]["metadata"], api["metadata"])
        self.assertNotIn("SECRET", json.dumps(api))

    def test_incomplete_instance_projection_cannot_confirm_an_unload(self):
        row = telemetry._parse_api({"models": [{
            "key": "provider/model", "loaded_instances": [
                {"id": "valid"}, {"id": "invalid id"}],
        }]}, time.time())[0]
        self.assertEqual(row["metadata"]["loadedInstanceCount"], 2)
        self.assertIsNone(row["loadedInstanceIds"])
        many = telemetry._parse_api({"models": [{
            "key": "provider/model", "loaded_instances": [
                {"id": f"instance-{i}"} for i in range(17)],
        }]}, time.time())[0]
        self.assertEqual(many["metadata"]["loadedInstanceCount"], 17)
        self.assertIsNone(many["loadedInstanceIds"])
        fallback = telemetry._parse_lms({"models": [{"modelKey": "provider/model"}]},
                                        time.time())[0]
        self.assertIsNone(fallback["instanceId"])

    def test_registered_windows_worker_is_inventory_only_and_expires(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "chami-dispatch"
            command.write_text("test fixture")
            command.chmod(0o700)
            payload = {"ok": True, "age": 5.0,
                       "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE,
                       "models": ["Qwen3.8-27B Q4_K_M", "gpt-oss-20b"],
                       "task": "PRIVATE_WINDOWS_TASK"}
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                 mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command",
                                   return_value=json.dumps(payload)):
                observed = telemetry._windows_worker_probe()
            self.assertIsNotNone(observed)
            self.assertEqual(observed[1:4], (["Qwen3.8-27B Q4_K_M", "gpt-oss-20b"], 2, None))
            # A pre-1.2 worker sends neither a version nor GPUs.
            self.assertEqual(observed[4], {"workerVersion": None, "gpus": None})
            cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                     "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
                 mock.patch.object(telemetry, "_windows_worker_probe", return_value=observed):
                first, _ = telemetry._windows_worker(now)
                self.assertEqual(first["state"], "unknown")
                self._wait_for_windows_probe(cache)
                worker, source = telemetry._windows_worker(now)
            self.assertEqual(worker["state"], "advertised")
            self.assertEqual(source["state"], "recorded")
            self.assertNotIn("PRIVATE_WINDOWS_TASK", json.dumps((worker, source)))
            cache["checkedMonotonic"] = time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
                 mock.patch.object(telemetry, "_windows_worker_probe", return_value=None):
                telemetry._windows_worker(now + 61)
                self._wait_for_windows_probe(cache)
                expired, source = telemetry._windows_worker(now + 61)
            self.assertEqual(expired["state"], "unknown")
            self.assertEqual(source["state"], "error")
            self.assertIn("availability unknown", source["detail"])
            self.assertNotIn("No recent", source["detail"])

    def test_windows_worker_slow_success_does_not_block_sampling_or_duplicate_probe(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def slow_probe():
            calls.append(True)
            entered.set()
            if not release.wait(2.0):
                raise AssertionError("test probe was not released")
            return now - 5, ["Qwen3.8-27B"], 1, None

        try:
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
                 mock.patch.object(telemetry, "_windows_worker_probe", side_effect=slow_probe):
                start = time.monotonic()
                first, _ = telemetry._windows_worker(now)
                self.assertLess(time.monotonic() - start, 0.2)
                self.assertTrue(entered.wait(1.0))
                first_again, _ = telemetry._windows_worker(now + 1)
                self.assertEqual(first["state"], "unknown")
                self.assertEqual(first_again["state"], "unknown")
                self.assertEqual(len(calls), 1)
                release.set()
                self._wait_for_windows_probe(cache)
                available, source = telemetry._windows_worker(now + 2)
                self.assertEqual(available["state"], "advertised")
                self.assertEqual(available["ageSeconds"], 7.0)
                self.assertEqual(available["modelsAdvertised"], ["Qwen3.8-27B"])
                self.assertEqual(source["state"], "recorded")
                self.assertEqual(len(calls), 1)
        finally:
            release.set()
            self._wait_for_windows_probe(cache)

    def test_windows_worker_timeout_retains_cache_until_heartbeat_expires(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": now - 6,
                 "modelsAdvertised": ["gpt-oss-20b"], "modelCount": 1,
                 "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_windows_worker_probe", return_value=None):
            retained, _ = telemetry._windows_worker(now)
            self._wait_for_windows_probe(cache)
            expired, source = telemetry._windows_worker(now + 61)
        self.assertEqual(retained["state"], "advertised")
        self.assertEqual(retained["modelCount"], 1)
        self.assertEqual(expired["state"], "unknown")
        self.assertEqual(expired["modelsAdvertised"], [])
        self.assertEqual(source["state"], "error")
        self.assertIn("availability unknown", source["detail"])
        self.assertNotIn("No recent", source["detail"])

    def test_windows_worker_busy_defers_refresh_without_losing_freshness(self):
        now = time.time()
        heartbeat = now - 12
        cache = {"checkedMonotonic": time.monotonic(), "heartbeatUnix": heartbeat,
                 "modelsAdvertised": ["gpt-oss-20b"], "modelCount": 1,
                 "probeRunning": False, "probeError": False,
                 "probePaused": False, "probeBusy": False,
                 "retryAfterMonotonic": 0.0}
        self.assertTrue(telemetry._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        try:
            telemetry._windows_worker_refresh(cache)
        finally:
            telemetry._WINDOWS_WORKER_IO_LOCK.release()

        self.assertEqual(cache["heartbeatUnix"], heartbeat)
        self.assertEqual(cache["modelsAdvertised"], ["gpt-oss-20b"])
        self.assertTrue(cache["probeBusy"])
        self.assertFalse(cache["probePaused"])
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, source = telemetry._windows_worker(now)
            self.assertEqual(worker["state"], "advertised")
            self.assertEqual(worker["ageSeconds"], 12)
            self.assertEqual(worker["modelsAdvertised"], ["gpt-oss-20b"])
            self.assertEqual(source["state"], "recorded")
            self.assertIn("deferred", source["detail"])
            self.assertNotIn("unhealthy", source["detail"])

            stale, stale_source = telemetry._windows_worker(now + telemetry._WINDOWS_WORKER_MAX_AGE + 1)
            self.assertEqual(stale["state"], "unknown")
            self.assertEqual(stale["ageSeconds"], telemetry._WINDOWS_WORKER_MAX_AGE + 13)
            self.assertEqual(stale_source["state"], "unavailable")
            self.assertIn("deferred", stale_source["detail"])

    def test_concurrent_windows_samples_start_only_one_probe(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": now - 2,
                 "modelsAdvertised": ["gpt-oss-20b"], "modelCount": 1,
                 "probeRunning": False}
        release = threading.Event()
        entered = threading.Event()
        calls = []

        def probe():
            calls.append(True)
            entered.set()
            release.wait(2.0)
            return None

        try:
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
                 mock.patch.object(telemetry, "_windows_worker_probe", side_effect=probe):
                results = []
                workers = [threading.Thread(target=lambda: results.append(
                    telemetry._windows_worker(now)[0]["state"])) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=1.0)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(entered.wait(1.0))
                self.assertEqual(results, ["advertised"] * 8)
                self.assertEqual(len(calls), 1)
        finally:
            release.set()
            self._wait_for_windows_probe(cache)

    def test_windows_worker_dispatch_timeout_is_bounded_and_inconclusive(self):
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "chami-dispatch"
            command.write_text("test fixture")
            command.chmod(0o700)
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                 mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command",
                                   side_effect=TimeoutError("SMB stalled")) as bounded:
                self.assertIsNone(telemetry._windows_worker_probe())
            self.assertEqual(bounded.call_args.kwargs["timeout"],
                             telemetry._WINDOWS_WORKER_COMMAND_TIMEOUT)
            self.assertGreater(telemetry._WINDOWS_WORKER_COMMAND_TIMEOUT, 3.0)

    def test_windows_worker_skips_probe_while_another_owner_read_holds_sharedchami_gate(self):
        self.assertTrue(telemetry._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        try:
            with mock.patch.object(telemetry, "_windows_worker_reader_blocked",
                                   side_effect=AssertionError("must not inspect or dispatch")), \
                 mock.patch.object(telemetry, "_bounded_command",
                                   side_effect=AssertionError("must not read SMB")):
                with self.assertRaises(telemetry._WindowsWorkerBusy):
                    telemetry._windows_worker_probe()
        finally:
            telemetry._WINDOWS_WORKER_IO_LOCK.release()

    def test_windows_worker_pauses_before_dispatch_when_smb_reader_is_uninterruptible(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        reader = f"{telemetry._WINDOWS_WORKER_CMD} _io-child"
        listing = f"{os.getuid()} U /usr/bin/python3 -I -S {reader}\n"
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_bounded_command", return_value=listing) as bounded:
            telemetry._windows_worker(now)
            self._wait_for_windows_probe(cache)
            worker, source = telemetry._windows_worker(now + 1)
            self.assertEqual(worker["state"], "unknown")
            self.assertEqual(source["state"], "error")
            self.assertIn("paused", worker["detail"])
            self.assertIn("uninterruptible", worker["detail"])
            self.assertTrue(telemetry._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
            telemetry._WINDOWS_WORKER_IO_LOCK.release()
            self.assertTrue(all(call.args[0][0] == "/bin/ps" for call in bounded.call_args_list))

    def test_windows_worker_releases_shared_gate_when_reader_guard_raises(self):
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked",
                               side_effect=RuntimeError("guard failure")):
            with self.assertRaises(RuntimeError):
                telemetry._windows_worker_probe()
        self.assertTrue(telemetry._WINDOWS_WORKER_IO_LOCK.acquire(blocking=False))
        telemetry._WINDOWS_WORKER_IO_LOCK.release()

    def test_windows_worker_guard_matches_only_exact_chami_ensure_probe_children(self):
        path = telemetry._SHAREDCHAMI_ENSURE
        matched = f"/usr/bin/python3 -I -S {path} --probe"
        matched_readonly = f"/usr/bin/python3 -I -S {path} --probe-read-only"
        unrelated = f"/usr/bin/python3 -I -S {path}.other --probe"
        for state, command, expected in (("U", matched, True),
                                         ("U", matched_readonly, True),
                                         ("S", matched, False),
                                         ("U", unrelated, False)):
            with self.subTest(state=state, command=command), \
                 mock.patch.object(telemetry, "_bounded_command",
                                   return_value=f"{os.getuid()} {state} {command}\n"):
                self.assertEqual(telemetry._windows_worker_reader_blocked(), expected)

    def test_windows_worker_reader_inspection_is_local_and_fails_closed(self):
        reader = f"{telemetry._WINDOWS_WORKER_CMD} _io-child"
        with mock.patch.object(telemetry, "_bounded_command",
                               return_value=f"{os.getuid()} S /usr/bin/python3 -I -S {reader}\n"):
            self.assertFalse(telemetry._windows_worker_reader_blocked())
        with mock.patch.object(telemetry, "_bounded_command", side_effect=TimeoutError("ps stalled")):
            self.assertTrue(telemetry._windows_worker_reader_blocked())
        with mock.patch.object(telemetry, "_bounded_command", return_value="bad record"):
            self.assertTrue(telemetry._windows_worker_reader_blocked())

    def test_windows_worker_failed_probe_backs_off_without_repeating_queue_read(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_windows_worker_probe", return_value=None) as probe:
            telemetry._windows_worker(now)
            self._wait_for_windows_probe(cache)
            cache["checkedMonotonic"] = time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS
            worker, source = telemetry._windows_worker(now + 12)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(worker["state"], "unknown")
            self.assertEqual(source["state"], "error")
            self.assertGreater(cache["retryAfterMonotonic"], time.monotonic())

    def test_windows_probe_shutdown_cancels_nested_reader_promptly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "child-progress"
            command = root / "chami-dispatch"
            child = (
                "from pathlib import Path\n"
                "import time\n"
                f"p=Path({str(marker)!r}); n=0\n"
                "while True:\n"
                " p.write_text(str(n)); n+=1; time.sleep(.02)\n"
            )
            command.write_text(
                "#!/usr/bin/env python3\n"
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                "time.sleep(10)\n"
            )
            command.chmod(0o700)
            cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                     "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
            try:
                telemetry._WINDOWS_WORKER_CANCEL.clear()
                with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                     mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                     mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
                    telemetry._windows_worker(time.time())
                    deadline = time.monotonic() + 2.0
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    start = time.monotonic()
                    self.assertTrue(telemetry.stop_windows_worker_probe(timeout=.8))
                    self.assertLess(time.monotonic() - start, .8)
                    before = marker.read_text()
                    time.sleep(.12)
                    self.assertEqual(marker.read_text(), before)
                    self.assertFalse(cache["probeRunning"])
            finally:
                telemetry.stop_windows_worker_probe(timeout=.8)
                telemetry._WINDOWS_WORKER_CANCEL.clear()

    def test_unexpected_windows_probe_error_is_generic_and_observable(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_windows_worker_probe",
                               side_effect=RuntimeError("PRIVATE WINDOWS PATH")):
            telemetry._windows_worker(now)
            self._wait_for_windows_probe(cache)
            worker, source = telemetry._windows_worker(now + 1)
        self.assertEqual(worker["state"], "unknown")
        self.assertEqual(source["state"], "error")
        self.assertIn("probe inconclusive", source["detail"])
        self.assertNotIn("PRIVATE WINDOWS PATH", json.dumps((worker, source)))

    def test_windows_probe_thread_start_failure_is_inconclusive(self):
        now = time.time()
        cache = {"checkedMonotonic": float("-inf"), "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("private")):
            worker, source = telemetry._windows_worker(now)
        self.assertEqual(worker["state"], "unknown")
        self.assertEqual(source["state"], "error")
        self.assertIn("availability unknown", source["detail"])
        self.assertNotIn("private", json.dumps((worker, source)))

    def test_windows_worker_allows_read_beyond_old_700ms_deadline(self):
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "chami-dispatch"
            command.write_text(
                "#!/usr/bin/env python3\n"
                "import json, time\n"
                "time.sleep(0.9)\n"
                "print(json.dumps({'ok': True, 'age': 3.0, "
                f"'evidence_scope': {telemetry._WINDOWS_WORKER_SCOPE!r}, "
                "'models': ['gpt-oss-20b']}))\n"
            )
            command.chmod(0o700)
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                 mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False):
                result = telemetry._windows_worker_probe()
            self.assertIsNotNone(result)
            self.assertEqual(result[1:4], (["gpt-oss-20b"], 1, None))

    @staticmethod
    def _route_record(run_id, stage, started, recorded, evidence):
        """A whole legacy router record (the closed envelope the router writes)."""
        return {"schemaVersion": 1, "runId": run_id, "inputSha256": "a" * 64, "stage": stage, "sequence": 1,
                "startedUnix": started,
                "checkpoint": {"schemaVersion": 1, "runId": run_id, "inputSha256": "a" * 64, "sequence": 1,
                               "stage": stage, "recordedUnix": recorded, "evidence": evidence}}

    def test_pipeline_stale_record_is_not_active_and_text_is_projected_away(self):
        with tempfile.TemporaryDirectory() as temp:
            active = Path(temp) / "active.json"
            active.write_text(json.dumps(self._route_record(
                "safe-run-1", "backend_draft", time.time() - 3600, time.time() - 3600,
                {"recoveryRequired": False, "prompt": "private prompt text"})))
            active.chmod(0o600)
            with mock.patch.object(telemetry, "_ACTIVE_PATH", active), \
                 mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"):
                pipeline, source = telemetry._pipeline(time.time())
        self.assertEqual(pipeline["status"], "stale")
        self.assertEqual(pipeline["steps"][0]["state"], "unknown")
        self.assertEqual(source["state"], "stale")
        serialized = json.dumps(pipeline) + json.dumps(source)
        self.assertNotIn("private prompt text", serialized)
        self.assertIsNone(pipeline["usage"]["inputTokens"])
        self.assertIsNone(pipeline["usage"]["totalTokens"])

    def _pending_pipeline(self, marker, *, mode=0o600, raw=None):
        """Fix Nisi + Jev summary (27 Sep): _pipeline with no router run and one pending marker."""
        with tempfile.TemporaryDirectory() as temp:
            pending = Path(temp) / "pending.json"
            pending.write_text(raw if raw is not None else json.dumps(marker))
            pending.chmod(mode)
            with mock.patch.object(telemetry, "_ACTIVE_PATH", Path(temp) / "absent-active.json"), \
                 mock.patch.object(telemetry, "_PENDING_PATH", pending), \
                 mock.patch.object(telemetry, "_router_namespace_initialized", return_value=True):
                return telemetry._pipeline(1_000_000.0)

    def test_pending_marker_age_and_owner_are_projected_without_its_digest(self):
        digest = "81dbab4f" + "0" * 56
        legacy = {"kind": "codemode.nisi.pending.v1", "started_unix": 1_000_000.0 - 33_000, "input_sha256": digest}
        pipeline, source = self._pending_pipeline(legacy)
        self.assertEqual(pipeline["status"], "recovery-required")
        self.assertTrue(pipeline["recoveryRequired"])
        self.assertTrue(pipeline["pendingMarkerObserved"])
        self.assertEqual(pipeline["pendingMarkerAgeSeconds"], 33_000)
        self.assertEqual(pipeline["pendingMarkerOwner"], "anonymous (legacy)")
        self.assertNotIn(digest[:12], json.dumps(pipeline) + json.dumps(source))
        router = {**legacy, "started_unix": 1_000_000 - 900, "runId": "marketscout.brainstorm.20260926.qwen", "operation": "answer"}
        pipeline, _ = self._pending_pipeline(router)
        self.assertEqual((pipeline["pendingMarkerAgeSeconds"], pipeline["pendingMarkerOwner"]),
                         (900, "marketscout.brainstorm.20260926.qwen"))

    def test_unrecognized_or_future_pending_marker_still_requires_recovery_but_reports_no_numbers(self):
        base = {"kind": "codemode.nisi.pending.v1", "started_unix": 1_000_000.0 - 600, "input_sha256": "a" * 64}
        for marker in ({**base, "started_unix": 1_000_000.0 + 3600},          # future-dated
                       {**base, "kind": "other"},                             # wrong kind
                       {**base, "extra": 1},                                  # unknown key
                       {**base, "started_unix": "600"},                       # not a number
                       {**base, "runId": "bad run id!", "operation": "answer"},  # malformed owner
                       {**base, "runId": "ok-run"}):                          # router form missing a key
            with self.subTest(marker=marker):
                pipeline, _ = self._pending_pipeline(marker)
                self.assertTrue(pipeline["pendingMarkerObserved"])
                self.assertEqual(pipeline["status"], "recovery-required")
                self.assertIsNone(pipeline["pendingMarkerAgeSeconds"])
                self.assertIsNone(pipeline["pendingMarkerOwner"])
        # An unreadable (not private) marker is an error, never an age or an owner.
        pipeline, source = self._pending_pipeline(base, mode=0o644)
        self.assertEqual(source["state"], "error")
        self.assertIsNone(pipeline["pendingMarkerAgeSeconds"])
        self.assertFalse(pipeline["pendingMarkerObserved"])

    def test_no_pending_marker_leaves_its_facts_unknown(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(telemetry, "_ACTIVE_PATH", Path(temp) / "absent-active.json"), \
             mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"), \
             mock.patch.object(telemetry, "_router_namespace_initialized", return_value=True):
            pipeline, _ = telemetry._pipeline(time.time())
        self.assertEqual(pipeline["status"], "idle")
        self.assertEqual((pipeline["pendingMarkerAgeSeconds"], pipeline["pendingMarkerOwner"]), (None, None))

    def test_router_freshness_uses_checkpoint_time_and_never_infers_prior_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            active = Path(temp) / "active.json"
            active.write_text(json.dumps(self._route_record(
                "safe-run-2", "backend_review", time.time() - 7200, time.time() - 10, {"recoveryRequired": True})))
            active.chmod(0o600)
            with mock.patch.object(telemetry, "_ACTIVE_PATH", active), \
                 mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"):
                pipeline, source = telemetry._pipeline(time.time())
        self.assertEqual(source["state"], "live")
        self.assertLess(pipeline["ageSeconds"], 30)
        self.assertEqual(pipeline["status"], "recovery-required")
        self.assertEqual(pipeline["steps"][0]["state"], "pending")
        self.assertEqual(pipeline["steps"][4]["state"], "blocked")
        self.assertFalse(any(step["state"] == "complete" for step in pipeline["steps"]))

    def test_cli_context_is_kept_and_api_context_only_fills_missing_value(self):
        api = telemetry._parse_api({"models": [{"key": "provider/model",
                                                 "loaded_instances": [{"id": "i"}],
                                                 "max_context_length": 128}]}, time.time())[0]
        cli = telemetry._parse_lms({"models": [{"identifier": "provider/model",
                                                 "contextLength": 4096}]}, time.time())[0]
        merged = {api["id"]: api}
        telemetry._merge_cli_rows(merged, [cli])
        self.assertEqual(merged["provider/model"]["context"], 4096)
        cli_missing = telemetry._parse_lms({"models": [{"identifier": "provider/model"}]}, time.time())[0]
        merged = {api["id"]: api}
        telemetry._merge_cli_rows(merged, [cli_missing])
        self.assertEqual(merged["provider/model"]["context"], 128)

    def test_fixed_lms_path_is_fallback_when_path_lookup_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            fixed = home / ".lmstudio/bin/lms"
            fixed.parent.mkdir(parents=True)
            fixed.write_text("stub")
            fixed.chmod(0o700)
            with mock.patch.object(telemetry.shutil, "which", return_value=None), \
                 mock.patch.object(telemetry, "_HOME", home):
                self.assertEqual(telemetry._find_lms(), str(fixed))

    def test_malformed_pipeline_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            active = Path(temp) / "active.json"
            active.write_text("not-json")
            active.chmod(0o600)
            with mock.patch.object(telemetry, "_ACTIVE_PATH", active), \
                 mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"):
                pipeline, source = telemetry._pipeline(time.time())
        self.assertEqual(pipeline["status"], "unknown")
        self.assertEqual(source["state"], "error")
        self.assertIsNone(pipeline["usage"]["outputTokens"])

    def test_absent_active_record_in_initialized_namespace_is_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "router"
            root.mkdir(mode=0o700)
            (root / "archive").mkdir(mode=0o700)
            (root / "checkpoints").mkdir(mode=0o700)
            (root / "owner.lock").write_text("")
            (root / "owner.lock").chmod(0o600)
            with mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"):
                pipeline, source = telemetry._pipeline(time.time())
        self.assertIsNone(pipeline["runId"])
        self.assertEqual(pipeline["status"], "idle")
        self.assertEqual(source["state"], "live")

    def test_absent_router_namespace_is_unknown_not_idle(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing-router"
            with mock.patch.object(telemetry, "_ACTIVE_PATH", root / "active.json"), \
                 mock.patch.object(telemetry, "_ROUTER_ROOT", root), \
                 mock.patch.object(telemetry, "_PENDING_PATH", Path(temp) / "absent.json"):
                pipeline, source = telemetry._pipeline(time.time())
        self.assertIsNone(pipeline["runId"])
        self.assertEqual(pipeline["status"], "unknown")
        self.assertEqual(source["state"], "unavailable")

    def test_private_router_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "target.json"
            target.write_text("{}")
            target.chmod(0o600)
            link = Path(temp) / "active.json"
            link.symlink_to(target)
            with self.assertRaises(ValueError):
                telemetry._safe_file(link, 4096)

    def test_snapshot_falls_back_to_cli_and_drops_untrusted_fields(self):
        cli = {"models": [{"identifier": "model-a", "displayName": "Model A",
                           "status": "idle", "queued": 0,
                           "prompt": "private prompt", "source": "private source"}]}
        with mock.patch.object(telemetry, "_bounded_http", side_effect=OSError("offline")), \
             mock.patch.object(telemetry, "_bounded_lms", return_value=cli), \
             mock.patch.object(telemetry, "_windows_worker", return_value=_NO_WINDOWS_WORKER), \
             mock.patch.object(telemetry, "_pipeline", return_value=(
                 {"runId": None, "status": "idle", "stage": None, "recoveryRequired": False,
                  "ageSeconds": None, "steps": [],
                  "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None},
                  "authorModel": None, "reviewerModel": None},
                 {"id": "pipeline-router", "label": "Pipeline", "state": "unavailable",
                  "ageSeconds": None, "detail": "Unavailable"})):
            result = telemetry.collect_snapshot()
        self.assertEqual(result["models"][0]["state"], "idle")
        self.assertEqual(result["models"][0]["loaded"], True)
        self.assertNotIn("private prompt", json.dumps(result))
        self.assertNotIn("private source", json.dumps(result))
        self.assertIsNone(result["pipeline"]["usage"]["totalTokens"])
        self.assertEqual(result["sources"][0]["state"], "error")

    def test_activity_is_downgraded_when_cli_source_fails(self):
        api = {"models": [{"key": "model-a", "loaded_instances": [{"id": "x"}]}]}
        with mock.patch.object(telemetry, "_bounded_http", return_value=api), \
             mock.patch.object(telemetry, "_bounded_lms", side_effect=TimeoutError()), \
             mock.patch.object(telemetry, "_windows_worker", return_value=_NO_WINDOWS_WORKER), \
             mock.patch.object(telemetry, "_pipeline", return_value=(
                 {"runId": None, "status": "idle", "stage": None, "recoveryRequired": False,
                  "ageSeconds": None, "steps": [],
                  "usage": {"inputTokens": None, "outputTokens": None, "totalTokens": None},
                  "authorModel": None, "reviewerModel": None},
                 {"id": "pipeline-router", "label": "Pipeline", "state": "unavailable",
                  "ageSeconds": None, "detail": "Unavailable"})):
            result = telemetry.collect_snapshot()
        self.assertEqual(result["models"][0]["state"], "loaded")
        self.assertIsNone(result["models"][0]["queued"])
        self.assertEqual(result["sources"][1]["state"], "unavailable")

    def test_private_v02_projection_is_separate_and_reader_failure_preserves_runtime(self):
        status = {"schemaVersion": 1, "integration": "private-local-orchestration-route",
                  "runtimeIntegrity": "VERIFIED", "hostBinding": "DRIFT",
                  "liveInference": "UNKNOWN", "workflowAcceptance": "UNKNOWN",
                  "releaseAcceptance": "NOT_ESTABLISHED"}
        source = {"id": "nisi-v02-runtime", "label": "Nisi Inference private runtime",
                  "state": "error", "ageSeconds": 120, "detail": "Host bridge changed"}
        with mock.patch.object(telemetry, "_bounded_http", return_value={"models": []}), \
             mock.patch.object(telemetry, "_bounded_lms", return_value={"models": []}), \
             mock.patch.object(telemetry, "_windows_worker", return_value=_NO_WINDOWS_WORKER), \
             mock.patch.object(telemetry, "collect_nisi_v02", return_value=(status, source)):
            observed = telemetry.collect_snapshot()
        self.assertEqual(observed["nisiV02"], status)
        self.assertIn(source, observed["sources"])
        self.assertEqual(observed["components"][0]["label"], "Nisi route adapter")

        with mock.patch.object(telemetry, "_bounded_http", return_value={"models": []}), \
             mock.patch.object(telemetry, "_bounded_lms", return_value={"models": []}), \
             mock.patch.object(telemetry, "_windows_worker", return_value=_NO_WINDOWS_WORKER), \
             mock.patch.object(telemetry, "collect_nisi_v02", side_effect=RuntimeError("private failure")):
            failed = telemetry.collect_snapshot()
        self.assertEqual(failed["nisiV02"]["runtimeIntegrity"], "UNKNOWN")
        self.assertEqual(failed["nisiV02"]["liveInference"], "UNKNOWN")
        self.assertEqual(next(s for s in failed["sources"] if s["id"] == "nisi-v02-runtime")["state"], "error")
        self.assertEqual(failed["models"], [])


class WindowsJobsJournalTests(unittest.TestCase):
    NOW = 2_000_000_000.0
    JOB_A = "mac-20260925-080000-" + "a" * 32
    JOB_B = "mac-20260925-080100-" + "b" * 32
    JOB_C = "mac-20260925-080200-" + "c" * 32

    def _journal(self, records, mode=0o600, raw_extra=b""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "jobs.jsonl"
        body = b"".join(json.dumps(r).encode() + b"\n" for r in records) + raw_extra
        path.write_bytes(body)
        os.chmod(path, mode)
        patcher = mock.patch.object(telemetry, "_WINDOWS_JOBS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def rec(self, event, job, at, **fields):
        return {"v": 1, "event": event, "id": job, "unix": self.NOW - at, **fields}

    def test_absent_journal_is_unavailable_not_error(self):
        with mock.patch.object(telemetry, "_WINDOWS_JOBS_PATH", Path("/nonexistent/jobs.jsonl")):
            jobs, source = telemetry._windows_jobs(self.NOW)
        self.assertEqual(jobs["journal"], "absent")
        self.assertEqual(jobs["inFlight"], [])
        self.assertEqual(source["id"], "windows-jobs")
        self.assertEqual(source["state"], "unavailable")

    def test_in_flight_then_result_then_expiry(self):
        self._journal([
            self.rec("enqueued", self.JOB_A, 20, model="gpt-oss-20b", timeout=60, promptChars=9),
            self.rec("enqueued", self.JOB_B, 50, model="Qwen3.8-27B Q4_K_M", timeout=60, promptChars=9),
            self.rec("result", self.JOB_B, 10, status="success", model="Qwen3.8-27B Q4_K_M", elapsedSeconds=14.1),
            self.rec("enqueued", self.JOB_C, 500, model="gpt-oss-20b", timeout=60, promptChars=9),
        ])
        jobs, source = telemetry._windows_jobs(self.NOW)
        self.assertEqual([j["id"] for j in jobs["inFlight"]], [self.JOB_A])
        self.assertEqual(jobs["inFlight"][0]["timeoutSeconds"], 60)
        self.assertEqual(jobs["inFlight"][0]["ageSeconds"], 20.0)
        states = {j["id"]: j["state"] for j in jobs["recent"]}
        self.assertEqual(states, {self.JOB_B: "success", self.JOB_C: "unresolved"})
        self.assertEqual(jobs["lastSuccess"]["model"], "Qwen3.8-27B Q4_K_M")
        self.assertEqual(jobs["lastSuccess"]["elapsedSeconds"], 14.1)
        self.assertEqual(source["state"], "live")
        json.dumps(jobs, allow_nan=False)

    def test_result_after_unresolved_wins(self):
        self._journal([
            self.rec("enqueued", self.JOB_A, 300, timeout=60),
            self.rec("unresolved", self.JOB_A, 200),
            self.rec("result", self.JOB_A, 100, status="error", elapsedSeconds=61.0),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual(jobs["recent"][0]["state"], "error")
        self.assertIsNone(jobs["lastSuccess"])

    def test_invalid_records_are_ignored_without_leaking_values(self):
        self._journal([
            {"v": 2, "event": "enqueued", "id": self.JOB_A, "unix": self.NOW - 1},
            {"v": 1, "event": "enqueued", "id": "../../etc/passwd", "unix": self.NOW - 1},
            {"v": 1, "event": "shell", "id": self.JOB_A, "unix": self.NOW - 1},
            {"v": 1, "event": "enqueued", "id": self.JOB_A, "unix": self.NOW + 999},
            self.rec("enqueued", self.JOB_B, 5, model="bad\u0000model; rm -rf", timeout=10**9),
            self.rec("result", self.JOB_C, 5, status="success", model="gpt-oss-20b", elapsedSeconds=float("inf")) | {"elapsedSeconds": 1e309},
        ], raw_extra=b'{"v":1,"event":"result","id":"' + self.JOB_C.encode() + b'","unix":NaN}\n{"torn line')
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual([j["id"] for j in jobs["inFlight"]], [self.JOB_B])
        self.assertIsNone(jobs["inFlight"][0]["model"])
        self.assertEqual(jobs["inFlight"][0]["timeoutSeconds"], 600)
        text = json.dumps(jobs, allow_nan=False)
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("passwd", text)

    def test_group_or_world_readable_or_linked_journal_is_refused(self):
        self._journal([self.rec("enqueued", self.JOB_A, 5, timeout=60)], mode=0o644)
        jobs, source = telemetry._windows_jobs(self.NOW)
        self.assertEqual(jobs["journal"], "invalid")
        self.assertEqual(source["state"], "error")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "real"
            target.write_text(json.dumps(self.rec("enqueued", self.JOB_A, 5, timeout=60)) + "\n")
            os.chmod(target, 0o600)
            link = Path(temp) / "jobs.jsonl"
            link.symlink_to(target)
            with mock.patch.object(telemetry, "_WINDOWS_JOBS_PATH", link):
                jobs, source = telemetry._windows_jobs(self.NOW)
        self.assertEqual(jobs["journal"], "invalid")

    def test_tail_read_drops_partial_first_record(self):
        filler = [self.rec("enqueued", "mac-20260925-070000-" + f"{i:032x}", 1000 + i, timeout=60)
                  for i in range(3000)]
        self._journal(filler + [self.rec("enqueued", self.JOB_A, 1, timeout=60)])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual([j["id"] for j in jobs["inFlight"]], [self.JOB_A])

    def test_jobs_stay_visible_while_heartbeat_reads_are_paused(self):
        self.NOW = time.time()  # collect_snapshot samples the real clock
        self._journal([self.rec("enqueued", self.JOB_A, 3, model="gpt-oss-20b", timeout=60)])
        reader = f"{telemetry._WINDOWS_WORKER_CMD} _io-child"
        listing = (f"{os.getuid()} U /usr/bin/python3 -I -S {reader}\n") * 3
        # Python 3.9 on macOS starts time.monotonic() near zero per process.
        cache = {"checkedMonotonic": time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS - 1,
                 "heartbeatUnix": None,
                 "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_bounded_command", return_value=listing), \
             mock.patch.object(telemetry, "_bounded_http", side_effect=TimeoutError()), \
             mock.patch.object(telemetry, "_bounded_lms", side_effect=TimeoutError()), \
             mock.patch.object(telemetry, "_pipeline", return_value=({}, {"id": "router", "state": "unavailable"})), \
             mock.patch.object(telemetry, "_online_code_mode", return_value={}), \
             mock.patch.object(telemetry, "_canary", return_value=({}, {"id": "canary", "state": "unavailable"})), \
             mock.patch.object(telemetry, "collect_nisi_v02", return_value=({}, {"id": "nisi-v02-runtime", "state": "unavailable"})):
            telemetry.collect_snapshot()
            deadline = time.monotonic() + 5
            while cache.get("probeRunning") and time.monotonic() < deadline:
                time.sleep(0.01)
            snapshot = telemetry.collect_snapshot()
        self.assertEqual(snapshot["windowsWorker"]["stuckReaders"], 3)
        self.assertIn("3 stuck SharedChami readers", snapshot["windowsWorker"]["detail"])
        self.assertIn("open Fix inference > Route pipeline", snapshot["windowsWorker"]["detail"])
        self.assertEqual(len(snapshot["windowsJobs"]["inFlight"]), 1)
        self.assertEqual(snapshot["sources"][-1]["id"], "windows-jobs")
        self.assertEqual(snapshot["sources"][-1]["state"], "live")
        json.dumps(snapshot, allow_nan=False)

    def test_job_whose_waiting_client_exited_is_not_in_flight(self):
        child = __import__("subprocess").Popen(["/usr/bin/true"])
        child.wait()
        self._journal([
            self.rec("enqueued", self.JOB_A, 5, timeout=60, pid=child.pid),
            self.rec("enqueued", self.JOB_B, 5, timeout=60, pid=os.getpid()),
            self.rec("enqueued", self.JOB_C, 5, timeout=60),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual(sorted(j["id"] for j in jobs["inFlight"]), sorted([self.JOB_B, self.JOB_C]))
        self.assertEqual([j["state"] for j in jobs["recent"] if j["id"] == self.JOB_A], ["unresolved"])
        # The PC may still be running JOB_A, so the safety gates still count it.
        self.assertEqual(jobs["unsettled"], 3)

    def test_hold_blocks_new_passive_reads_and_waits_for_the_running_one(self):
        cache = {"checkedMonotonic": time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS - 1,
                 "heartbeatUnix": None, "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
        release = threading.Event()
        calls = []

        def slow_probe():
            calls.append(1)
            release.wait(2)
            return None
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_windows_worker_probe", side_effect=slow_probe):
            telemetry._windows_worker(time.time())
            threading.Timer(0.2, release.set).start()
            started = time.monotonic()
            with telemetry.hold_windows_worker_probe() as idle:
                waited = time.monotonic() - started
                cache["checkedMonotonic"] = time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS - 1
                cache["retryAfterMonotonic"] = 0.0
                telemetry._windows_worker(time.time())
                self.assertTrue(idle)
                self.assertFalse(cache.get("probeRunning"))
            self.assertGreaterEqual(waited, 0.15)
            self.assertEqual(len(calls), 1)
            self.assertFalse(telemetry._WINDOWS_WORKER_HOLD.is_set())

    def test_pause_wording_matches_the_observed_reason(self):
        cases = ((telemetry._WindowsWorkerBusy(), "another monitor owner check is reading SharedChami", 0, False),
                 (telemetry._WindowsWorkerPaused(), "cannot be ruled out", None, False),
                 (telemetry._WindowsWorkerPaused(2), "2 stuck SharedChami readers", 2, True))
        for exc, expected, stuck, recommends_fix in cases:
            cache = {"checkedMonotonic": time.monotonic(), "heartbeatUnix": None,
                     "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
            with mock.patch.object(telemetry, "_windows_worker_probe", side_effect=exc):
                telemetry._windows_worker_refresh(cache)
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
                worker, _ = telemetry._windows_worker(time.time())
            self.assertIn(expected, worker["detail"])
            self.assertEqual(worker["stuckReaders"], stuck)
            self.assertEqual("Fix inference > Route pipeline" in worker["detail"], recommends_fix)

    def test_abandoned_job_settles_at_enqueue_and_never_outranks_a_later_success(self):
        child = __import__("subprocess").Popen(["/usr/bin/true"])
        child.wait()
        self._journal([
            self.rec("enqueued", self.JOB_A, 100, timeout=300, pid=child.pid),
            self.rec("enqueued", self.JOB_B, 60, model="gpt-oss-20b", timeout=60),
            self.rec("result", self.JOB_B, 50, status="success", model="gpt-oss-20b", elapsedSeconds=6.0),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual([(j["id"], j["state"], j["ageSeconds"]) for j in jobs["recent"]],
                         [(self.JOB_B, "success", 50.0), (self.JOB_A, "unresolved", 100.0)])

    def test_held_heartbeat_reports_deferred_not_unverified(self):
        cache = {"checkedMonotonic": time.monotonic() - telemetry._WINDOWS_WORKER_POLL_SECONDS - 1,
                 "heartbeatUnix": time.time() - 61, "modelsAdvertised": ["gpt-oss-20b"], "modelCount": 1,
                 "probeRunning": False}
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache), \
             mock.patch.object(telemetry, "_windows_worker_probe", side_effect=AssertionError("no read while held")):
            with telemetry.hold_windows_worker_probe():
                worker, _ = telemetry._windows_worker(time.time())
        self.assertIn("deferred", worker["detail"])
        self.assertFalse(cache.get("probeRunning"))

    def test_fresh_degraded_worker_is_reported_not_inconclusive(self):
        payload = {"ok": False, "reason": "worker status degraded", "age": 12.0,
                   "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": [], "endpoints": {}}
        cache = {"checkedMonotonic": time.monotonic(), "heartbeatUnix": None,
                 "modelsAdvertised": ["old"], "modelCount": 1, "probeRunning": False}
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "chami-dispatch"
            command.write_text("#!/bin/sh\n")
            command.chmod(0o700)
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                 mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command", return_value=json.dumps(payload)) as bounded:
                telemetry._windows_worker_refresh(cache)
        self.assertEqual(bounded.call_args.kwargs["ok_codes"], (0, 2))
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, source = telemetry._windows_worker(time.time())
        self.assertEqual(worker["state"], "degraded")
        self.assertEqual(worker["modelsAdvertised"], [])
        self.assertIn("no model lane answers", worker["detail"])
        self.assertEqual(source["state"], "error")
        payload["reason"] = "queue I/O unavailable or timed out"
        cache2 = dict(cache, workerCondition=None, heartbeatUnix=None)
        with mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
             mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", Path("/nonexistent")):
            self.assertIsNone(telemetry._windows_worker_probe_locked())


    def test_job_rows_carry_validated_client_lane_and_speed(self):
        self._journal([
            self.rec("enqueued", self.JOB_A, 40, model="gpt-oss-20b", timeout=60, client="Claude", maxTokens=64),
            self.rec("result", self.JOB_A, 30, status="success", model="gpt-oss-20b", elapsedSeconds=3.5,
                     lane="amd", servedModel="openai/gpt-oss-20b", client="Claude", completionTokens=12,
                     predictedPerSecond=41.5),
            self.rec("enqueued", self.JOB_B, 20, model="Qwen3.8-27B Q4_K_M", timeout=60, client="codex"),
            self.rec("result", self.JOB_B, 10, status="success", model="Qwen3.8-27B Q4_K_M", elapsedSeconds=9.0,
                     lane="bionic", completionTokens=True, predictedPerSecond=1e7),
            self.rec("enqueued", self.JOB_C, 5, model="gpt-oss-20b", timeout=60, client="rm -rf /; x"),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual([row["id"] for row in jobs["inFlight"]], [self.JOB_C])
        self.assertEqual({k: jobs["inFlight"][0][k] for k in ("client", "lane", "predictedPerSecond", "completionTokens")},
                         {"client": None, "lane": "fast", "predictedPerSecond": None, "completionTokens": None})
        # In-flight rows know their lane from the requested model, so the map can light the lane live.
        rows = {row["id"]: row for row in jobs["recent"]}
        self.assertEqual((rows[self.JOB_A]["client"], rows[self.JOB_A]["lane"],
                          rows[self.JOB_A]["predictedPerSecond"], rows[self.JOB_A]["completionTokens"]),
                         ("claude", "fast", 41.5, 12))
        self.assertEqual((rows[self.JOB_B]["client"], rows[self.JOB_B]["lane"],
                          rows[self.JOB_B]["predictedPerSecond"], rows[self.JOB_B]["completionTokens"]),
                         ("codex", "deep", None, None))
        self.assertEqual(jobs["lastSuccess"]["lane"], "deep")
        self.assertEqual(jobs["clientsRecent"], {"codex": 1, "claude": 1})
        text = json.dumps(jobs, allow_nan=False)
        self.assertNotIn("rm -rf", text)
        self.assertNotIn("servedModel", text)

    def test_result_rows_carry_prompt_speed_size_and_known_flags_within_bounds(self):
        # UI pass 26 Sep: the dispatcher journals promptPerSecond, promptTokens and flags on result events.
        self._journal([
            self.rec("enqueued", self.JOB_A, 40, model="gpt-oss-20b", timeout=60, client="codex"),
            self.rec("result", self.JOB_A, 30, status="success", model="gpt-oss-20b", elapsedSeconds=1.5, lane="amd",
                     predictedPerSecond=104.0, promptPerSecond=212.5, promptTokens=830, completionTokens=96,
                     flags=["hit-token-limit", "rm -rf /", "hit-token-limit", 7]),
            self.rec("enqueued", self.JOB_B, 20, model="Qwen3.8-27B Q4_K_M", timeout=60, client="claude"),
            self.rec("result", self.JOB_B, 10, status="success", model="Qwen3.8-27B Q4_K_M", elapsedSeconds=9.0,
                     lane="bionic", promptPerSecond=2e6, promptTokens=2**31, flags="hit-token-limit"),
            self.rec("enqueued", self.JOB_C, 5, model="gpt-oss-20b", timeout=60, client="codex",
                     flags=["hit-token-limit"], promptPerSecond=50.0),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        rows = {row["id"]: row for row in jobs["recent"]}
        self.assertEqual((rows[self.JOB_A]["promptPerSecond"], rows[self.JOB_A]["promptTokens"], rows[self.JOB_A]["flags"]),
                         (212.5, 830, ["hit-token-limit"]))
        # Out of range (over 1e6 per second, over 2^31 - 1 tokens) or not a list: dropped, never clamped.
        self.assertEqual((rows[self.JOB_B]["promptPerSecond"], rows[self.JOB_B]["promptTokens"], rows[self.JOB_B]["flags"]),
                         (None, None, []))
        # Only a result carries this evidence; an enqueue record cannot plant it.
        self.assertEqual({k: jobs["inFlight"][0][k] for k in ("promptPerSecond", "promptTokens", "flags")},
                         {"promptPerSecond": None, "promptTokens": None, "flags": []})
        self.assertEqual(jobs["lastSuccess"]["id"], self.JOB_B)
        self.assertIsNot(rows[self.JOB_A]["flags"], rows[self.JOB_B]["flags"])
        self._journal([
            self.rec("enqueued", self.JOB_A, 40, timeout=60),
            self.rec("result", self.JOB_A, 30, status="success", model="gpt-oss-20b", elapsedSeconds=1.0,
                     promptPerSecond=True, promptTokens=-1, flags=[["hit-token-limit"]]),
        ])
        row = telemetry._windows_jobs(self.NOW)[0]["recent"][0]
        self.assertEqual((row["promptPerSecond"], row["promptTokens"], row["flags"]), (None, None, []))
        self.assertNotIn("rm -rf", json.dumps(jobs, allow_nan=False))

    def test_cancelled_result_settles_the_job_and_cancel_requested_is_informational(self):
        # Worker 1.2 (26 Sep): chami-dispatch journals 'cancel-requested' when it asks the worker to stop a
        # job, and the worker's 'cancelled' result (status only, plus elapsed time) then settles it.
        self._journal([
            self.rec("enqueued", self.JOB_A, 50, model="Qwen3.8-27B Q4_K_M", timeout=600, client="claude"),
            self.rec("cancel-requested", self.JOB_A, 40),
            self.rec("result", self.JOB_A, 38, status="cancelled", elapsedSeconds=11.5,
                     predictedPerSecond=40.0, promptPerSecond=90.0, flags=["hit-token-limit"]),
            # A late unresolved record never reopens or rewrites a cancelled job.
            self.rec("unresolved", self.JOB_A, 30),
            self.rec("enqueued", self.JOB_B, 20, model="gpt-oss-20b", timeout=600, client="codex"),
            self.rec("cancel-requested", self.JOB_B, 10),
            # A cancel request for a job this journal never saw enqueued creates no row.
            self.rec("cancel-requested", self.JOB_C, 5),
        ])
        jobs, source = telemetry._windows_jobs(self.NOW)
        rows = {row["id"]: row for row in jobs["recent"]}
        self.assertEqual(list(rows), [self.JOB_A])
        self.assertEqual((rows[self.JOB_A]["state"], rows[self.JOB_A]["elapsedSeconds"], rows[self.JOB_A]["client"],
                          rows[self.JOB_A]["lane"]), ("cancelled", 11.5, "claude", "deep"))
        # A cancelled job has no answer, so nothing it sent vouches for a speed, size or flag.
        self.assertEqual((rows[self.JOB_A]["predictedPerSecond"], rows[self.JOB_A]["promptPerSecond"],
                          rows[self.JOB_A]["flags"]), (None, None, []))
        self.assertIsNone(jobs["lastSuccess"])
        # The request alone settles nothing: JOB_B is still in flight (and unsettled), marked as asked to stop.
        self.assertEqual([(row["id"], row["cancelRequested"]) for row in jobs["inFlight"]], [(self.JOB_B, True)])
        self.assertEqual(jobs["unsettled"], 1)
        self.assertEqual(source["state"], "live")
        # Once its cancelled result arrives, nothing is unsettled.
        self._journal([
            self.rec("enqueued", self.JOB_B, 20, model="gpt-oss-20b", timeout=600, client="codex"),
            self.rec("cancel-requested", self.JOB_B, 10),
            self.rec("result", self.JOB_B, 8, status="cancelled", elapsedSeconds=0.0),
        ])
        jobs, source = telemetry._windows_jobs(self.NOW)
        self.assertEqual((jobs["inFlight"], jobs["unsettled"], jobs["recent"][0]["state"]), ([], 0, "cancelled"))
        # An unknown result status is still ignored, so the job stays open.
        self._journal([self.rec("enqueued", self.JOB_B, 20, timeout=600),
                       self.rec("result", self.JOB_B, 8, status="aborted")])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        self.assertEqual((len(jobs["inFlight"]), jobs["inFlight"][0]["cancelRequested"], jobs["unsettled"]), (1, False, 1))

    def test_unknown_lane_and_missing_client_count_as_unknown(self):
        self._journal([
            self.rec("enqueued", self.JOB_A, 40, timeout=60),
            self.rec("result", self.JOB_A, 30, status="error", elapsedSeconds=3.0, lane="gpu9"),
            self.rec("enqueued", self.JOB_B, 20, timeout=60, client=["claude"]),
            self.rec("result", self.JOB_B, 10, status="success", model="gpt-oss-20b", elapsedSeconds=2.0,
                     lane=["amd"], client="x" * 41),
        ])
        jobs, _ = telemetry._windows_jobs(self.NOW)
        # An unrecognised lane value is ignored; the lane then follows the model when one is known.
        self.assertEqual([(row["client"], row["lane"]) for row in jobs["recent"]], [(None, "fast"), (None, None)])
        self.assertEqual(jobs["clientsRecent"], {"unknown": 2})
        with mock.patch.object(telemetry, "_WINDOWS_JOBS_PATH", Path("/nonexistent/jobs.jsonl")):
            self.assertEqual(telemetry._windows_jobs(self.NOW)[0]["clientsRecent"], {})


class WindowsLanesAndHeadlessTests(unittest.TestCase):
    LANES = {"bionic": {"up": True, "alias": "Qwen3.8-27B", "kind": "qwen", "slots": {"busy": 1, "total": 2}},
             "amd": {"up": False, "alias": "gpt-oss-20b", "kind": "gpt-oss", "slots": None}}

    def test_lanes_are_mapped_to_monitor_names_and_revalidated(self):
        self.assertEqual(telemetry._windows_lanes(self.LANES), {
            "fast": {"up": False, "model": "gpt-oss-20b", "kind": "gpt-oss", "slotsBusy": None, "slotsTotal": None},
            "deep": {"up": True, "model": "Qwen3.8-27B", "kind": "qwen", "slotsBusy": 1, "slotsTotal": 2}})
        bad = []
        for lane, change in (("amd", {"up": 1}), ("amd", {"kind": "llama"}), ("bionic", {"alias": "a;rm -rf"}),
                             ("bionic", {"slots": {"busy": 3, "total": 2}}), ("bionic", {"slots": {"busy": True, "total": 2}}),
                             ("bionic", {"slots": {"busy": 0, "total": 65}}), ("bionic", {"slots": [1, 2]})):
            lanes = json.loads(json.dumps(self.LANES))
            lanes[lane].update(change)
            bad.append(lanes)
        bad += [None, [], {}, {"amd": self.LANES["amd"]}, dict(self.LANES, gpu=self.LANES["amd"]),
                dict(self.LANES, amd="up")]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(telemetry._windows_lanes(value))

    def _probe(self, payload):
        with tempfile.TemporaryDirectory() as temp:
            command = Path(temp) / "chami-dispatch"
            command.write_text("#!/bin/sh\n")
            command.chmod(0o700)
            cache = {"checkedMonotonic": time.monotonic(), "heartbeatUnix": None,
                     "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}
            with mock.patch.object(telemetry, "_WINDOWS_WORKER_CMD", command), \
                 mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False), \
                 mock.patch.object(telemetry, "_bounded_command", return_value=json.dumps(payload)):
                telemetry._windows_worker_refresh(cache)
        return cache

    def test_fresh_heartbeat_publishes_lanes_and_hides_them_when_stale_or_absent_on_degraded(self):
        now = time.time()
        payload = {"ok": True, "age": 4.0, "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE,
                   "models": ["gpt-oss-20b"], "lanes": self.LANES}
        cache = self._probe(payload)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(now)
            stale, _ = telemetry._windows_worker(now + telemetry._WINDOWS_WORKER_MAX_AGE + 5)
        self.assertEqual(worker["state"], "advertised")
        self.assertEqual(worker["lanes"]["deep"]["slotsBusy"], 1)
        self.assertEqual(worker["lanes"]["fast"]["up"], False)
        self.assertIsNone(stale["lanes"])
        # What chami-dispatch validate_state prints today for a degraded worker: it adds
        # "lanes" only when the worker is ready, so a degraded heartbeat carries none.
        degraded = {"ok": False, "reason": "worker status degraded", "age": 4.0,
                    "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": [], "endpoints": {}}
        cache = self._probe(degraded)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual(worker["state"], "degraded")
        self.assertIsNone(worker["lanes"])
        # Pending dispatcher change (lanes on a fresh degraded heartbeat): they pass through as sent.
        cache = self._probe(dict(degraded, lanes=self.LANES))
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual(worker["state"], "degraded")
        self.assertEqual(sorted(worker["lanes"]), ["deep", "fast"])
        cache = self._probe(dict(payload, lanes={"amd": {"up": "yes"}}))
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual(worker["state"], "advertised")
        self.assertIsNone(worker["lanes"])
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE",
                               {"checkedMonotonic": time.monotonic(), "heartbeatUnix": None,
                                "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}):
            self.assertIsNone(telemetry._windows_worker(time.time())[0]["lanes"])

    def test_rejected_or_dispatcher_flagged_lane_detail_is_published_as_lanes_error(self):
        # Follow-up 26 Sep: detail that was sent but rejected is "malformed", not "no lane detail".
        payload = {"ok": True, "age": 4.0, "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": ["gpt-oss-20b"]}
        cases = ((dict(payload, lanes=self.LANES), None, True),
                 (payload, None, False),
                 (dict(payload, lanes=None), None, False),
                 (dict(payload, lanesError="malformed lane detail"), "malformed lane detail", False),
                 (dict(payload, lanes={"amd": {"up": "yes"}}), "malformed lane detail", False),
                 # The dispatcher's own wording never reaches the snapshot; its flag wins over any lanes.
                 (dict(payload, lanes=self.LANES, lanesError="PRIVATE dispatcher text"), "malformed lane detail", False))
        for record, error, has_lanes in cases:
            with self.subTest(record=record):
                now = time.time()
                cache = self._probe(record)
                with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
                    worker, _ = telemetry._windows_worker(now)
                    stale, _ = telemetry._windows_worker(now + telemetry._WINDOWS_WORKER_MAX_AGE + 5)
                self.assertEqual(worker["state"], "advertised")
                self.assertEqual(worker["lanesError"], error)
                self.assertEqual(worker["lanes"] is not None, has_lanes)
                self.assertIsNone(stale["lanesError"])
                self.assertNotIn("PRIVATE", json.dumps(worker))
        degraded = {"ok": False, "reason": "worker status degraded", "age": 4.0,
                    "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": [], "endpoints": {}, "lanes": {"amd": 1}}
        cache = self._probe(degraded)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual((worker["state"], worker["lanes"], worker["lanesError"]), ("degraded", None, "malformed lane detail"))
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE",
                               {"checkedMonotonic": time.monotonic(), "heartbeatUnix": None,
                                "modelsAdvertised": [], "modelCount": 0, "probeRunning": False}):
            self.assertIsNone(telemetry._windows_worker(time.time())[0]["lanesError"])

    GPU = {"index": 0, "name": "NVIDIA GeForce RTX 4070", "utilizationPercent": 32, "memoryUsedMiB": 11980,
           "memoryTotalMiB": 12282, "temperatureC": 54, "powerW": 118.5}

    def test_worker_version_and_gpus_pass_through_revalidated_and_only_while_fresh(self):
        # Worker 1.2 (26 Sep): chami-dispatch status relays worker_version and gpus (null when unavailable).
        payload = {"ok": True, "age": 4.0, "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": ["gpt-oss-20b"],
                   "worker_version": "1.2", "gpus": [dict(self.GPU, index=1, name="Second GPU"), self.GPU]}
        now = time.time()
        cache = self._probe(payload)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(now)
            stale, _ = telemetry._windows_worker(now + telemetry._WINDOWS_WORKER_MAX_AGE + 5)
        self.assertEqual(worker["workerVersion"], "1.2")
        self.assertEqual([row["index"] for row in worker["gpus"]], [0, 1], "rows sorted by index")
        self.assertEqual(worker["gpus"][0], self.GPU)
        self.assertEqual((stale["workerVersion"], stale["gpus"]), (None, None))
        # The published rows are copies, never the cache's own objects.
        worker["gpus"][0]["utilizationPercent"] = 99
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            self.assertEqual(telemetry._windows_worker(now)[0]["gpus"][0]["utilizationPercent"], 32)
        # A degraded heartbeat carries them too (the dispatcher relays them for ready and degraded).
        degraded = {"ok": False, "reason": "worker status degraded", "age": 4.0, "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE,
                    "models": [], "endpoints": {}, "worker_version": "1.2", "gpus": [self.GPU]}
        cache = self._probe(degraded)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual((worker["state"], worker["workerVersion"], worker["gpus"][0]["name"]), ("degraded", "1.2", "NVIDIA GeForce RTX 4070"))
        # null (no GPU sample), a pre-1.2 worker and the dispatcher's own error flag all read as unknown.
        without = {k: v for k, v in payload.items() if k not in ("gpus", "worker_version")}
        for record in (dict(payload, gpus=None), without, dict(without, gpusError="malformed gpu detail")):
            with self.subTest(record=record):
                cache = self._probe(record)
                with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
                    worker, _ = telemetry._windows_worker(time.time())
                self.assertEqual(worker["state"], "advertised")
                self.assertIsNone(worker["gpus"])
                self.assertEqual(worker["workerVersion"], record.get("worker_version"))

    def test_gpu_rows_and_worker_version_are_checked_with_the_dispatcher_bounds(self):
        gpu = self.GPU
        self.assertEqual(telemetry._windows_gpus([gpu]), [gpu])
        self.assertEqual(telemetry._windows_gpus([dict(gpu, extra="PRIVATE")]), [gpu], "unknown keys are dropped")
        bad = [None, [], {}, [gpu] * 2, [dict(gpu, index=i) for i in range(5)], ["gpu"],
               [dict(gpu, index=-1)], [dict(gpu, index=64)], [dict(gpu, index=True)], [dict(gpu, index=1.0)],
               [dict(gpu, name="")], [dict(gpu, name=" padded")], [dict(gpu, name="x" * 65)], [dict(gpu, name="tab\there")],
               [dict(gpu, name="caf\u00e9")], [dict(gpu, name=7)],
               [dict(gpu, utilizationPercent=101)], [dict(gpu, utilizationPercent=-1)], [dict(gpu, utilizationPercent=True)],
               [dict(gpu, utilizationPercent=float("nan"))], [dict(gpu, utilizationPercent="32")],
               [dict(gpu, memoryUsedMiB=12283)], [dict(gpu, memoryTotalMiB=0)], [dict(gpu, memoryTotalMiB=1048577)],
               [dict(gpu, temperatureC=151)], [dict(gpu, powerW=2001)], [{k: v for k, v in gpu.items() if k != "powerW"}]]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(telemetry._windows_gpus(value))
        self.assertEqual(telemetry._windows_hardware({"worker_version": "1.2", "gpus": [gpu]}), {"workerVersion": "1.2", "gpus": [gpu]})
        for version in ("", "x" * 17, "1.2; rm -rf /", 1.2, None, ["1.2"]):
            with self.subTest(version=version):
                self.assertIsNone(telemetry._windows_hardware({"worker_version": version})["workerVersion"])
        # One bad row drops the whole list, and nothing from it reaches the snapshot.
        payload = {"ok": True, "age": 4.0, "evidence_scope": telemetry._WINDOWS_WORKER_SCOPE, "models": ["gpt-oss-20b"],
                   "worker_version": "PRIVATE version text", "gpus": [gpu, dict(gpu, index=1, name="PRIVATE\nname")]}
        cache = self._probe(payload)
        with mock.patch.object(telemetry, "_WINDOWS_WORKER_CACHE", cache):
            worker, _ = telemetry._windows_worker(time.time())
        self.assertEqual((worker["state"], worker["workerVersion"], worker["gpus"]), ("advertised", None, None))
        self.assertNotIn("PRIVATE", json.dumps(worker))

    def _mode(self, value, mode=0o600):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "mode.json"
        if value is not None:
            _private_write(path, value if isinstance(value, str) else json.dumps(value), mode)
        patcher = mock.patch.object(telemetry, "_WINDOWS_HEADLESS_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def test_headless_switch_follows_pc_llm_read_mode_and_fails_closed(self):
        now = 2_000_000_000.0
        on = {"schemaVersion": 1, "kind": "codemode.online-mode.v1", "state": "on", "leaseId": "a" * 32,
              "grantedBy": "inference-monitor", "grantedAtUnix": now - 60, "expiresAtUnix": now + 3 * 3600 + 1799.6,
              "probe": {"jobId": "mac-x", "elapsedSeconds": 3.7}}
        self._mode(on)
        self.assertEqual(telemetry._windows_headless(now),
                         {"state": "on", "reason": None, "expiresInSeconds": 3 * 3600 + 1800,
                          "grantedBy": "inference-monitor"})
        cases = ((None, None, "never turned on"),
                 (dict(on, state="off"), None, "turned off"),
                 (dict(on, kind="other"), None, "mode file malformed"),
                 (dict(on, schemaVersion=True), None, "mode file malformed"),
                 (dict(on, expiresAtUnix="soon"), None, "mode file malformed"),
                 (dict(on, grantedAtUnix=True), None, "mode file malformed"),
                 ('{"schemaVersion":1,"kind":"codemode.online-mode.v1","state":"on","grantedAtUnix":1,'
                  '"expiresAtUnix":NaN}', None, "mode file unreadable"),
                 ("{not json", None, "mode file unreadable"),
                 (dict(on, grantedAtUnix=now + 61), None, "mode file future-dated"),
                 (dict(on, expiresAtUnix=now), None, "expired"),
                 (on, 0o640, "mode file unsafe"))
        for value, mode, reason in cases:
            with self.subTest(reason=reason, value=value):
                self._mode(value, mode or 0o600)
                self.assertEqual(telemetry._windows_headless(now),
                                 {"state": "off", "reason": reason, "expiresInSeconds": None, "grantedBy": None})
        self._mode(dict(on, grantedBy="../../x"))
        self.assertIsNone(telemetry._windows_headless(now)["grantedBy"])
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "real.json"
            _private_write(target, json.dumps(on))
            link = Path(temp) / "mode.json"
            link.symlink_to(target)
            hard = Path(temp) / "hard.json"
            os.link(target, hard)
            for path in (link, target):
                with self.subTest(path=path.name), mock.patch.object(telemetry, "_WINDOWS_HEADLESS_PATH", path):
                    self.assertEqual(telemetry._windows_headless(now)["reason"], "mode file unsafe")

    def test_snapshot_exposes_headless_state_without_private_fields(self):
        now = time.time()
        self._mode({"schemaVersion": 1, "kind": "codemode.online-mode.v1", "state": "on", "leaseId": "SECRETLEASE",
                    "grantedBy": "cli", "grantedAtUnix": now, "expiresAtUnix": now + 600,
                    "probe": {"jobId": "mac-PRIVATE"}})
        with mock.patch.object(telemetry, "_bounded_http", side_effect=TimeoutError()), \
             mock.patch.object(telemetry, "_bounded_lms", side_effect=TimeoutError()), \
             mock.patch.object(telemetry, "_pipeline", return_value=({}, {"id": "router", "state": "unavailable"})), \
             mock.patch.object(telemetry, "_online_code_mode", return_value={}), \
             mock.patch.object(telemetry, "_windows_worker", return_value=({"state": "unknown", "lanes": None},
                                                                        {"id": "windows-worker"})), \
             mock.patch.object(telemetry, "_windows_jobs", side_effect=RuntimeError("private")), \
             mock.patch.object(telemetry, "_canary", return_value=({}, {"id": "canary", "state": "unavailable"})), \
             mock.patch.object(telemetry, "collect_nisi_v02", return_value=({}, {"id": "nisi-v02-runtime", "state": "unavailable"})):
            snapshot = telemetry.collect_snapshot()
        headless = snapshot["windowsWorker"]["headless"]
        self.assertEqual((headless["state"], headless["grantedBy"]), ("on", "cli"))
        self.assertTrue(595 <= headless["expiresInSeconds"] <= 600)
        self.assertEqual(snapshot["windowsJobs"]["clientsRecent"], {})
        text = json.dumps(snapshot, allow_nan=False)
        self.assertNotIn("SECRETLEASE", text)
        self.assertNotIn("PRIVATE", text)


_HUGE = "1" + "0" * 400     # a JSON integer too large for a float (math.isfinite raises on it)


def _private_write(path, text, mode=0o600):
    """Create a file with an explicit mode (no permission change afterwards)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


class RouteComponentTests(unittest.TestCase):
    def rows(self, *ids):
        return {i: {"id": i, "loaded": True} for i in ids}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.env = root / "typesafe.env"
        self.router = root / "router"
        os.mkdir(self.router, 0o700)
        os.mkdir(self.router / "archive", 0o700)
        for name, value in (("_JEV_ENV_PATH", self.env), ("_ROUTER_ROOT", self.router)):
            patcher = mock.patch.object(telemetry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        telemetry._JEV_CACHE.update(key=None, when=None, model=None)

    def components(self, pipeline=None, nisi=None, rows=None):
        verified = {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"}
        result = telemetry._route_components(pipeline or {}, verified if nisi is None else nisi,
                                             rows if rows is not None else {}, time.time())
        return {c["id"]: c for c in result}

    def test_pair_resident_is_ready_with_roles(self):
        c = self.components(rows=self.rows("qwen/qwen3.8-27b", "google/gemma-4-26b-a4b-qat",
                                           "text-embedding-nomic-embed-text-v1.5"))
        self.assertEqual(c["nisi"]["state"], "ready")
        self.assertEqual(c["nisi"]["authorModel"], "google/gemma-4-26b-a4b-qat")
        self.assertEqual(c["nisi"]["reviewerModel"], "qwen/qwen3.8-27b")
        self.assertIn("chat google/gemma-4-26b-a4b-qat", c["nisi"]["detail"])
        c = self.components(rows=self.rows("b-model", "a-model"))
        self.assertEqual((c["nisi"]["authorModel"], c["nisi"]["reviewerModel"]), ("a-model", "b-model"))

    def test_one_model_says_edits_need_a_second(self):
        c = self.components(rows=self.rows("google/gemma-4-26b-a4b-qat"))
        self.assertEqual(c["nisi"]["state"], "partial")
        self.assertIn("need a second model", c["nisi"]["detail"])

    def test_pending_marker_and_drift_take_precedence(self):
        self.assertEqual(self.components(pipeline={"pendingMarkerObserved": True},
                                         rows=self.rows("a", "b"))["nisi"]["state"], "unresolved")
        self.assertEqual(self.components(nisi={"runtimeIntegrity": "VERIFIED", "hostBinding": "DRIFT"})
                         ["nisi"]["state"], "needs-action")

    def test_jev_opt_in_and_last_judgment_without_reading_the_key(self):
        self.assertEqual(self.components()["jev"]["state"], "unavailable")
        _private_write(self.env, "TYPESAFE_API_KEY=secret-value")
        record = {"finishedUnix": time.time() - 120,
                  "result": {"stages": {"intake": {"kind": "chami.intake.typesafe.v1",
                                                   "status": "JUDGED", "model": "jev-1.13.0"}}}}
        _private_write(self.router / "archive" / "run-1.json", json.dumps(record))
        jev = self.components()["jev"]
        self.assertEqual(jev["state"], "configured")
        self.assertIn("last judged a route 2 min ago (jev-1.13.0)", jev["detail"])
        self.assertNotIn("secret", json.dumps(jev))
        self.env.unlink()
        _private_write(self.env, "x", mode=0o644)
        if os.stat(self.env).st_mode & 0o044:
            self.assertEqual(self.components()["jev"]["state"], "unavailable")


class LiveRouteTests(unittest.TestCase):
    def pipeline(self, status="recovery-required"):
        return ({"runId": "run-1", "status": status, "stage": "backend_review", "recoveryRequired": True,
                 "steps": [{"id": "backend_review", "state": "blocked"}]},
                {"id": "pipeline-router", "state": "live", "detail": "x"})

    def test_verified_live_owner_turns_the_record_into_a_running_route(self):
        pipeline, source = self.pipeline()
        telemetry._mark_live_route(pipeline, source, {"state": "processing", "routeId": "run-1"})
        self.assertEqual((pipeline["status"], pipeline["recoveryRequired"]), ("running", False))
        self.assertEqual(pipeline["steps"][0]["state"], "active")

    def test_unverified_or_other_run_keeps_recovery(self):
        for mode in ({"state": "unknown", "routeId": "run-1"}, {"state": "processing", "routeId": "run-2"},
                     {"state": "processing"}):
            pipeline, source = self.pipeline()
            telemetry._mark_live_route(pipeline, source, mode)
            self.assertEqual(pipeline["status"], "recovery-required")
            self.assertTrue(pipeline["recoveryRequired"])
        pipeline, source = self.pipeline(status="stale")
        telemetry._mark_live_route(pipeline, source, {"state": "processing", "routeId": "run-1"})
        self.assertEqual(pipeline["status"], "stale")


# --- Router-concurrency P2: dual-read readers (spec 6.12, R2.9) --------------------------------
# Fixtures are written by the router's own state writers, loaded read-only: the per-run layout
# by the dev build of pipeline_router_state.py (RouterOwner.claim / begin / checkpoint / note,
# router_fence.write), the legacy layout by the single-run writer that is installed today (the
# baseline copy).  Holders that must be real router processes run a small driver named
# pipeline_router.py as `python -I -B <driver> work`, the argv the readers attribute.
_ROUTER_WORK = Path.home() / "pending-review/router-concurrency"
_INSTALLED_SCRIPTS = Path.home() / ".codex/skills/local-llm-orchestrator/scripts"
_ROUTER_WRITERS = {
    "per-run": [Path(os.environ["MONITOR_ROUTER_DEV_SCRIPTS"])] if os.environ.get("MONITOR_ROUTER_DEV_SCRIPTS")
    else [_ROUTER_WORK / "dev/scripts", _INSTALLED_SCRIPTS],
    "legacy": [Path(os.environ["MONITOR_ROUTER_LEGACY_SCRIPTS"])] if os.environ.get("MONITOR_ROUTER_LEGACY_SCRIPTS")
    else [_ROUTER_WORK / "baseline/scripts", _INSTALLED_SCRIPTS],
}
_DRIVER = r'''
import json, os, runpy, time
from pathlib import Path
cfg = json.loads(os.environ["MONITOR_ROUTER_FIXTURE"])
module = runpy.run_path(cfg["writer"])
owner = module["RouterOwner"](Path(cfg["root"]))
owner.__enter__()
if cfg["layout"] == "legacy":
    owner.begin(cfg["runId"], cfg["digest"])
else:
    owner.claim(cfg["runId"], cfg["digest"], until=time.monotonic() + 5, meta=cfg.get("meta"))
    if cfg.get("begin", True):
        owner.begin(cfg["runId"], cfg["digest"])
if cfg.get("stage"):
    owner.checkpoint(cfg["stage"], {"selectedHost": "mac", "prompt": "PRIVATE_FIXTURE_TEXT"})
if cfg.get("note"):
    phase, resource, step = cfg["note"]
    owner.note(phase, resource, step, time.monotonic() + 60)
Path(cfg["ready"]).write_text(str(os.getpid()))
deadline = time.monotonic() + 60
while not Path(cfg["stop"]).exists() and time.monotonic() < deadline:
    time.sleep(0.02)
os._exit(0)     # no finish: the record stays behind, as after a crash (the lock is released)
'''


def _router_writer(kind):
    """The state writer for ``kind`` ('per-run' has RouterOwner.claim; 'legacy' writes active.json)."""
    for directory in _ROUTER_WRITERS[kind]:
        path = directory / "pipeline_router_state.py"
        try:
            text = path.read_text()
        except OSError:
            continue
        if ("def claim(" in text) == (kind == "per-run") and (
                kind == "legacy" or (directory / "router_fence.py").is_file()):
            return path
    return None


def _require_router_writer(test, kind):
    """The writer for ``kind``, or a FAILURE: the R2.9 dual-read tests are the evidence the owner
    attests before `install.py accept-monitor`, so a suite that silently skipped all of them must
    never read "OK".  Skipping them needs MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1, said out loud."""
    path = _router_writer(kind)
    if path is None:
        message = f"no {kind} router state writer found in {[str(d) for d in _ROUTER_WRITERS[kind]]}"
        if os.environ.get("MONITOR_ALLOW_ROUTER_FIXTURE_SKIP") == "1":
            test.skipTest(message)
        test.fail(message + "; the R2.9 dual-read tests cannot run (MONITOR_ALLOW_ROUTER_FIXTURE_SKIP=1 skips them)")
    return path


class RouterDualReadTests(unittest.TestCase):
    """Spec R2.9: both journal layouts, closed and bounded; liveness by lock, never by openers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        # lsof prints resolved names, so the fixture lives under its real path.
        self.base = Path(os.path.realpath(self.temp.name))
        self.root = self.base / "router"
        self.driver = self.base / "pipeline_router.py"
        self.driver.write_text(_DRIVER)
        self.pending = self.base / "nisi-pending.json"
        for name, value in (("_ROUTER_ROOT", self.root), ("_ACTIVE_PATH", self.root / "active.json"),
                            ("_ROUTER_SCRIPT", self.driver), ("_PENDING_PATH", self.pending),
                            ("_LAUNCHER_ROOT", self.base / "launcher"),
                            ("_READINESS_PATH", self.base / "launcher/readiness.json")):
            patcher = mock.patch.object(telemetry, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(telemetry, "_windows_worker_reader_blocked", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        with telemetry._ROUTER_RECORD_CACHE_LOCK:
            telemetry._ROUTER_RECORD_CACHE.clear()
        self.addCleanup(telemetry._LAST_ROUTER_OBSERVATION.update, sampledAt=None, value=None)
        self.owners = []
        self.addCleanup(self._release_owners)

    def _release_owners(self):
        for owner in reversed(self.owners):
            owner.__exit__(None, None, None)

    def writer(self, kind):
        path = _require_router_writer(self, kind)
        return runpy.run_path(str(path)), path

    def per_run_layout(self, *, policy="multi", fence="installed"):
        state, path = self.writer("per-run")
        with state["RouterOwner"](self.root):
            pass                                  # the router's own layout: dirs 0700, owner.lock 0600
        if fence:
            runpy.run_path(str(path.with_name("router_fence.py")))["write"](
                self.root, fence, "c" * 64, "20260927T120000Z")
        if policy:
            self.set_policy(state, policy)
        return state

    def set_policy(self, state, policy):
        state["_atomic"](self.root / "policy.json", {"schemaVersion": 1, "kind": "codemode.router.policy.v1",
                                                     "concurrency": policy}, 1024)

    def run_in_process(self, state, run_id, digest, *, begin=True, stage=None, evidence=None, note=None,
                       keep=True, meta=None):
        """A run of this test process (L0 + its L1 held on its own descriptors)."""
        owner = state["RouterOwner"](self.root)
        owner.__enter__()
        owner.claim(run_id, digest, until=time.monotonic() + 2, meta=meta)
        if begin:
            owner.begin(run_id, digest)
        if stage:
            owner.checkpoint(stage, evidence if evidence is not None else {"selectedHost": "mac"})
        if note:
            owner.note(note[0], note[1], note[2], time.monotonic() + 45)
        if keep:
            self.owners.append(owner)
        else:
            owner.__exit__(None, None, None)      # no finish: an unresolved (quarantined) record
        return owner

    @contextlib.contextmanager
    def holder(self, kind, run_id, digest, **config):
        """A real router work process (`python -I -B pipeline_router.py work`) holding the run."""
        _state, writer = self.writer(kind)
        ready, stop = self.base / f"{run_id}.ready", self.base / f"{run_id}.stop"
        config = dict({"writer": str(writer), "root": str(self.root), "layout": kind, "runId": run_id,
                       "digest": digest, "ready": str(ready), "stop": str(stop)}, **config)
        env = dict(os.environ, MONITOR_ROUTER_FIXTURE=json.dumps(config))
        process = subprocess.Popen([sys.executable, "-I", "-B", str(self.driver), "work"], env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if process.poll() is not None or time.monotonic() > deadline:
                    self.fail("router fixture did not start: " + process.stderr.read().decode()[-2000:])
                time.sleep(0.02)
            yield process.pid
        finally:
            stop.write_text("")
            try:
                process.wait(timeout=15)
            finally:
                process.stderr.close()

    def observe(self):
        now = time.time()
        router = telemetry._router_observation(now)
        mode = telemetry._online_code_mode(now, "now", router)
        pipeline, source = telemetry._pipeline(now, router)
        telemetry._mark_live_route(pipeline, source, mode)
        return router, mode, pipeline, source

    @staticmethod
    def row(router, run_id):
        return next(row for row in router["rows"] if row["runId"] == run_id)

    def write_marker(self, run_id=None, operation="draft"):
        marker = {"kind": "codemode.nisi.pending.v1", "started_unix": time.time() - 5, "input_sha256": "f" * 64}
        if run_id is not None:
            marker.update(runId=run_id, operation=operation)
        self.pending.write_text(json.dumps(marker))
        os.chmod(self.pending, 0o600)

    def assert_no_lock_left(self):
        """Every probe was released at once: the router can take each lock EX|NB right now."""
        paths = [self.root / "owner.lock"] + sorted((self.root / "locks").glob("*.lock"))
        for path in paths:
            fd = os.open(str(path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    # -- the three contract tests install.py requires (R2.9 rule 1) ----------------------------

    def test_dual_read_legacy_live_holder_without_locks_dir(self):
        """A genuinely old-style live holder: the installed single-run writer holds owner.lock EX
        for the whole run, and there is no locks/ (or active/, notes/) directory at all."""
        with self.holder("legacy", "legacy-live", "a" * 64, stage="backend_draft") as pid:
            self.assertTrue((self.root / "active.json").is_file())
            for name in ("locks", "active", "notes", "install-fence.json", "policy.json"):
                self.assertFalse((self.root / name).exists(), name)
            router, mode, pipeline, source = self.observe()
            row = self.row(router, "legacy-live")
            self.assertEqual((row["layout"], row["live"], row["state"], row["pid"]),
                             ("legacy", True, "running", pid))
            self.assertEqual((mode["state"], mode["active"], mode["routeId"], mode["taskState"]),
                             ("processing", True, "legacy-live", "processing"))
            self.assertIsNone(mode["client"])
            self.assertEqual(mode["runCounts"], {"running": 1, "queued": 0, "unresolved": 0})
            self.assertEqual(mode["admission"]["source"], "legacy-router")
            self.assertEqual((pipeline["status"], pipeline["runId"]), ("running", "legacy-live"))
            self.assertEqual([p["status"] for p in pipeline["pipelines"]], ["running"])
            self.assertNotIn("PRIVATE_FIXTURE_TEXT", json.dumps([mode, pipeline, source]))
            # R2.9 rule 8 (Astra 6): the live legacy route's own overlapping Nisi call.  Today's
            # single-run router writes the anonymous 3-key marker; it is this route's call, so
            # the route stays running and the marker is not a recovery.
            self.write_marker(None)
            _router, _mode, pipeline, source = self.observe()
            self.assertEqual((pipeline["status"], pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"],
                              pipeline["pendingMarkerAttributedTo"], pipeline["pendingMarkerOwner"]),
                             ("running", False, False, None, "anonymous (legacy)"))
            self.assertEqual(source["state"], "live")
            # After P1 the same route's chat call writes the 5-key form: attributed by exact runId.
            self.write_marker("legacy-live.answer", operation="answer")
            _router, _mode, pipeline, source = self.observe()
            components = {c["id"]: c for c in telemetry._route_components(
                pipeline, {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"}, {}, time.time())}
            self.assertEqual((pipeline["status"], pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"],
                              pipeline["pendingMarkerAttributedTo"]), ("running", False, False, "legacy-live"))
            self.assertEqual(components["nisi"]["state"], "in-use")
            self.assertIn("Nisi call in flight for live route legacy-live", source["detail"])
            self.pending.unlink()
            # Openers are not a test: a waiter-like process that merely opens owner.lock changes nothing.
            opener = os.open(str(self.root / "owner.lock"), os.O_RDONLY)
            try:
                self.assertEqual(self.observe()[1]["state"], "processing")
            finally:
                os.close(opener)
            # (iii) While this Monitor may hold owner.lock, the EX holder is not provably the route.
            with telemetry.monitor_router_hold():
                held = self.observe()[1]
            self.assertEqual((held["state"], held["taskState"]), ("unknown", "unfinished"))
            # (ii) A held per-run lock means the EX holder can be a new-code run under single.
            (self.root / "locks").mkdir(mode=0o700)
            other = self.root / "locks" / "someone.lock"
            fd = os.open(str(other), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                blocked = self.observe()[1]
            finally:
                os.close(fd)
            self.assertEqual((blocked["state"], blocked["taskState"]), ("unknown", "unfinished"))
            self.assertIn("per-run router lock is held", blocked["evidence"])
            self.assertEqual(self.observe()[1]["state"], "processing")
            self.assert_no_lock_left_except_owner()
        # The holder died without finishing: a quarantined legacy run, never idle.
        router, mode, pipeline, _ = self.observe()
        self.assertEqual(self.row(router, "legacy-live")["state"], "unresolved")
        self.assertEqual((mode["state"], mode["taskState"], mode["routeId"]), ("unknown", "unfinished", "legacy-live"))
        self.assertEqual(pipeline["status"], "unresolved")
        self.assert_no_lock_left()

    def test_dual_read_legacy_record_needs_the_owner_lock_held_not_just_a_process(self):
        """Legacy rule (i): even an exact work process with a fitting start is not proof while no one
        holds owner.lock EX (it may be a waiter, a status reader or a finished run's successor)."""
        legacy, _ = self.writer("legacy")
        with legacy["RouterOwner"](self.root) as owner:
            owner.begin("legacy-a", "a" * 64)
            owner.checkpoint("backend_draft", {"selectedHost": "mac"})
        started = json.loads((self.root / "active.json").read_text())["startedUnix"]
        with mock.patch.object(telemetry, "_router_owner_process", return_value=(4242, started - 1)) as process:
            router, mode, _pipeline, _ = self.observe()
            self.assertEqual((self.row(router, "legacy-a")["live"], mode["taskState"]), (False, "unfinished"))
            self.assertIn("no process holds the router owner lock", mode["evidence"])
            process.assert_not_called()
            fd = os.open(str(self.root / "owner.lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                router, mode, _pipeline, _ = self.observe()
                self.assertEqual((self.row(router, "legacy-a")["pid"], mode["state"]), (4242, "processing"))
                # Rule (iv): a work process that started more than 660 s before the record is an
                # earlier route's, never this one's owner.
                process.return_value = (4242, started - 661)
                router, mode, _pipeline, _ = self.observe()
                self.assertEqual((self.row(router, "legacy-a")["live"], mode["taskState"]), (False, "unfinished"))
                process.return_value = (4242, started - 659)
                self.assertEqual(self.observe()[1]["state"], "processing")
            finally:
                os.close(fd)

    def assert_no_lock_left_except_owner(self):
        for path in sorted((self.root / "locks").glob("*.lock")):
            fd = os.open(str(path), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

    def test_dual_read_per_run_live_holder(self):
        """New layout: a live run is its L1 held (SH|NB probe fails) on the inode its record binds;
        the process is attributed separately; N runs project as separate rows."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-dead", "d" * 64, stage="incomplete",
                            evidence={"recoveryRequired": True, "code": "NISI_OWNER_BUSY",
                                      "prompt": "PRIVATE_FIXTURE_TEXT"}, keep=False)
        with self.holder("per-run", "run-live", "a" * 64, stage="backend_draft",
                         meta={"client": "claude", "host": "mac", "operation": "work"},
                         note=["running", "mac-pair", "backend"]) as pid:
            self.run_in_process(state, "run-queued", "b" * 64, begin=False,
                                meta={"client": "codex", "host": "windows", "operation": "work"},
                                note=("waiting", "pc-route", "pre-begin"))
            router, mode, pipeline, source = self.observe()
            live = self.row(router, "run-live")
            record = json.loads((self.root / "active" / "run-live.json").read_text())
            self.assertEqual(record["runLock"]["ino"], os.stat(self.root / "locks" / "run-live.lock").st_ino)
            self.assertEqual((live["layout"], live["lock"], live["live"], live["state"], live["pid"]),
                             ("per-run", "held", True, "running", pid))
            self.assertEqual((live["client"], live["host"]), ("claude", "mac"))
            queued = self.row(router, "run-queued")
            self.assertEqual((queued["layout"], queued["live"], queued["state"]), ("note", True, "waiting"))
            dead = self.row(router, "run-dead")
            self.assertEqual((dead["live"], dead["state"], dead["code"], dead["recoveryRequired"]),
                             (False, "unresolved", "NISI_OWNER_BUSY", True))
            # One object for older consumers: the newest running run, never the queued one.
            self.assertEqual((mode["state"], mode["routeId"], mode["client"]), ("processing", "run-live", "claude"))
            self.assertIn(f"pid {pid}", mode["evidence"])
            self.assertEqual(mode["runCounts"], {"running": 1, "queued": 1, "unresolved": 1})
            self.assertEqual([r["runId"] for r in mode["activeRuns"]], ["run-live", "run-dead"])
            self.assertEqual([(r["runId"], r["resource"], r["client"]) for r in mode["queuedRuns"]],
                             [("run-queued", "pc-route", "codex")])
            self.assertGreater(mode["queuedRuns"][0]["secondsLeft"], 30)
            self.assertEqual(mode["lanes"]["mac-pair"], {"capacity": 1, "holders": ["run-live"], "waiting": []})
            self.assertEqual(mode["lanes"]["pc-route"], {"capacity": 1, "holders": [], "waiting": ["run-queued"]})
            self.assertEqual(mode["lanes"]["pc-fast"]["capacity"], 2)
            self.assertEqual((mode["admission"]["policy"], mode["admission"]["source"]), ("multi", "file"))
            self.assertEqual((mode["install"]["state"], mode["install"]["inProgress"]), ("installed", False))
            self.assertEqual((pipeline["status"], pipeline["runId"], pipeline["stage"]),
                             ("running", "run-live", "backend_draft"))
            self.assertEqual({p["runId"]: p["status"] for p in pipeline["pipelines"]},
                             {"run-live": "running", "run-queued": "waiting", "run-dead": "unresolved"})
            self.assertNotIn("PRIVATE_FIXTURE_TEXT", json.dumps([mode, pipeline, source]))
        # The run's process is gone: its lock is free, its record stays (quarantined), and the stale
        # note it left is ignored.
        self.assertTrue((self.root / "notes" / "run-live.json").exists())
        router, mode, _pipeline, _ = self.observe()
        self.assertEqual((self.row(router, "run-live")["state"], self.row(router, "run-live")["note"]),
                         ("unresolved", None))
        self.assertEqual((mode["state"], mode["routeId"]), ("processing", "run-queued"))
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 1, "unresolved": 2})

    def test_dual_read_marker_attribution_is_exact(self):
        """A 5-key Nisi marker belongs to a verified-live run only by exact runId after stripping
        exactly one suffix; anything else keeps today's recovery-required behaviour."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-x", "a" * 64, stage="backend_draft")
        self.run_in_process(state, "src.jev", "b" * 64, stage="feedback_review_intent")
        self.run_in_process(state, "dead-run", "c" * 64, stage="backend_review", keep=False)
        for marker, attributed in (("run-x.draft", "run-x"), ("run-x.review", "run-x"),
                                   ("run-x.mac-return", "run-x"), ("run-x.answer", "run-x"),
                                   ("src.jev.review", "src.jev"),
                                   ("run-x2.draft", None),            # a longer id, never by prefix
                                   ("run.draft", None),               # a shorter id
                                   ("run-x.draft.review", None),      # one suffix only
                                   ("run-xdraft", None), ("run-x", None),
                                   ("dead-run.review", None)):         # its run is not live
            with self.subTest(marker=marker):
                self.write_marker(marker)
                router, mode, pipeline, source = self.observe()
                components = {c["id"]: c for c in telemetry._route_components(
                    pipeline, {"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"}, {}, time.time())}
                self.assertEqual(pipeline["pendingMarkerAttributedTo"], attributed)
                self.assertTrue(pipeline["pendingMarkerObserved"])
                self.assertEqual(pipeline["pendingMarkerOwner"], marker)
                # The newest live run is the single object, whoever owns the marker.
                self.assertEqual((pipeline["status"], pipeline["runId"]), ("running", "src.jev"))
                if attributed:
                    self.assertEqual((pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]), (False, False))
                    self.assertEqual(components["nisi"]["state"], "in-use")
                    self.assertIn(attributed, source["detail"])
                else:
                    self.assertEqual((pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]), (True, True))
                    self.assertEqual(components["nisi"]["state"], "unresolved")
        # The anonymous 3-key marker is never attributed; beside per-run routes (which write the
        # 5-key form) it belongs to no live route and stays recovery-required.
        self.write_marker(None)
        _router, _mode, pipeline, _ = self.observe()
        self.assertIsNone(pipeline["pendingMarkerAttributedTo"])
        self.assertEqual((pipeline["status"], pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]),
                         ("running", True, True))
        self.assertEqual(pipeline["pendingMarkerOwner"], "anonymous (legacy)")

    # -- the rest of R2.9 rule 8 ---------------------------------------------------------------

    def test_dual_read_record_being_published_is_retried_then_unreadable(self):
        """R2.11: a record linked twice (a publish that died between link and unlink) reads as
        "changed during observation": retried, then that one row is unreadable; nothing fails."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-a", "a" * 64, stage="backend_draft", keep=False)
        self.run_in_process(state, "run-b", "b" * 64, stage="backend_draft", keep=False)
        record = self.root / "active" / "run-a.json"
        os.link(record, record.with_name("run-a.json.tmp-4242"))
        with mock.patch.object(telemetry.time, "sleep") as sleep:
            router, mode, pipeline, source = self.observe()
        self.assertEqual(sleep.call_count, telemetry._ROUTER_READ_RETRIES - 1)
        self.assertEqual((self.row(router, "run-a")["state"], self.row(router, "run-a")["readable"]),
                         ("unreadable", False))
        self.assertEqual(self.row(router, "run-b")["state"], "unresolved")
        self.assertEqual(mode["taskState"], "unfinished")
        self.assertEqual(sorted(r["runId"] for r in mode["activeRuns"]), ["run-a", "run-b"])
        self.assertNotIn("run-a.json.tmp-4242", json.dumps([mode, pipeline]))
        os.unlink(record.with_name("run-a.json.tmp-4242"))           # the next L1 holder heals it
        self.assertEqual(self.row(self.observe()[0], "run-a")["state"], "unresolved")

    def test_dual_read_multiple_unresolved_runs_of_both_layouts(self):
        """Several quarantined runs (the router's ROUTER_MULTIPLE_UNRESOLVED_RUNS) project as
        separate rows; the newest is the single object; none is live."""
        legacy, _ = self.writer("legacy")
        with legacy["RouterOwner"](self.root) as owner:
            owner.begin("legacy-old", "e" * 64)
            owner.checkpoint("backend_review", {"recoveryRequired": True})
        state = self.per_run_layout(policy=None, fence=None)
        self.run_in_process(state, "run-new", "a" * 64, stage="incomplete",
                            evidence={"recoveryRequired": True, "code": "WINDOWS_OWNER_UNAVAILABLE"}, keep=False)
        router, mode, pipeline, _ = self.observe()
        self.assertEqual(router["layout"], "mixed")
        self.assertEqual({row["runId"]: (row["layout"], row["state"]) for row in router["rows"]},
                         {"legacy-old": ("legacy", "unresolved"), "run-new": ("per-run", "unresolved")})
        self.assertEqual((mode["state"], mode["taskState"], mode["routeId"]), ("unknown", "unfinished", "run-new"))
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 0, "unresolved": 2})
        self.assertEqual(pipeline["runId"], "run-new")
        self.assertEqual(len(pipeline["pipelines"]), 2)
        self.assert_no_lock_left()

    def test_dual_read_archived_uncleared_and_stale_note(self):
        """C10: archive written, active record not yet cleared -> archived-uncleared; a note whose
        run lock is free (C1) gives no row at all."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-c10", "a" * 64, stage="final_validation_intent", keep=False)
        (self.root / "archive" / "run-c10.json").write_text("{}")
        os.chmod(self.root / "archive" / "run-c10.json", 0o600)
        ghost = state["RouterOwner"](self.root)
        ghost.__enter__()
        ghost.claim("run-ghost", "b" * 64, until=time.monotonic() + 2)
        ghost.note("waiting", "mac-pair", "pre-begin", time.monotonic() + 30)
        ghost._l1, l1 = None, ghost._l1              # keep its note on disk, then release its locks
        os.close(l1)
        ghost.__exit__(None, None, None)
        self.assertTrue((self.root / "notes" / "run-ghost.json").exists())
        router, mode, _pipeline, _ = self.observe()
        self.assertEqual([row["runId"] for row in router["rows"]], ["run-c10"])
        self.assertEqual(self.row(router, "run-c10")["state"], "archived-uncleared")
        self.assertIn("reconcile-archived", mode["evidence"])

    def test_dual_read_replaced_or_missing_run_lock_is_unverified_never_live(self):
        """C14/R2.4: liveness is the L1 the record bound.  A lock file replaced under a live run
        (another inode) or deleted proves nothing: the row is unverified, never live or idle."""
        state = self.per_run_layout(policy="multi")
        owner = self.run_in_process(state, "run-r", "a" * 64, stage="backend_draft")
        lock = self.root / "locks" / "run-r.lock"
        os.unlink(lock)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)   # an impostor lock file, held
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            router, mode, _pipeline, _ = self.observe()
            row = self.row(router, "run-r")
            self.assertEqual((row["lock"], row["live"], row["state"]), ("replaced", False, "unverified"))
            self.assertEqual((mode["state"], mode["taskState"]), ("unknown", "unfinished"))
            self.assertIn("liveness cannot be proven", mode["evidence"])
        finally:
            os.close(fd)
        os.unlink(lock)
        router, mode, _pipeline, _ = self.observe()
        self.assertEqual((self.row(router, "run-r")["lock"], self.row(router, "run-r")["state"]),
                         ("missing", "unverified"))
        self.assertFalse(lock.exists(), "the reader never re-creates a lock file")
        self.assertIsNotNone(owner)

    def test_dual_read_fence_states_and_the_install_barrier(self):
        """R2.1/R2.9: installing and rolling-back (or the installer's second link on owner.lock)
        are "router install in progress", never an unsafe journal; rolled-back counts as absent
        for legacy liveness; an invalid fence never proves a legacy route live."""
        state, path = self.writer("per-run")
        fence = runpy.run_path(str(path.with_name("router_fence.py")))
        with self.holder("legacy", "legacy-live", "a" * 64, stage="backend_draft"):
            for fence_state, in_progress, live in (("installing", True, False), ("rolling-back", True, False),
                                                   ("installed", False, True), ("rolled-back", False, True)):
                with self.subTest(fence=fence_state):
                    fence["write"](self.root, fence_state, "c" * 64, "20260927T120000Z")
                    router, mode, pipeline, _ = self.observe()
                    self.assertEqual((router["install"]["state"], router["install"]["inProgress"]),
                                     (fence_state, in_progress))
                    self.assertEqual(self.row(router, "legacy-live")["live"], live)
                    if in_progress:
                        self.assertEqual(mode["taskState"], "install-in-progress")
                        self.assertIn("install in progress", mode["evidence"])
                        self.assertEqual(pipeline["status"], "installing")
                    else:
                        self.assertEqual(mode["state"], "processing")
            (self.root / "install-fence.json").write_text('{"schemaVersion": 1}')
            os.chmod(self.root / "install-fence.json", 0o600)
            router, mode, _pipeline, _ = self.observe()
            self.assertEqual(router["install"]["state"], "invalid")
            self.assertEqual((self.row(router, "legacy-live")["live"], mode["taskState"]), (False, "unfinished"))
            os.unlink(self.root / "install-fence.json")
            # The installer's barrier: owner.lock gets a second link for the whole transition.
            os.link(self.root / "owner.lock", self.root / "owner.lock.install-barrier")
            router, mode, pipeline, source = self.observe()
            self.assertEqual((router["install"]["barrier"], router["install"]["inProgress"]), (True, True))
            self.assertEqual((mode["state"], mode["taskState"]), ("unknown", "install-in-progress"))
            self.assertEqual((pipeline["status"], source["state"]), ("installing", "unavailable"))
            self.assertNotIn("unsafe", mode["evidence"].lower())
            os.unlink(self.root / "owner.lock.install-barrier")
            self.assertEqual(self.observe()[1]["state"], "processing")

    def test_dual_read_single_policy_reports_draining_until_shared_holders_leave(self):
        """R2.2: a switch to single does not convert runs admitted shared; the drain state comes
        from momentary owner.lock probes alone, and the notes only name the shared holders."""
        state = self.per_run_layout(policy="multi")
        with self.holder("per-run", "run-shared", "a" * 64, stage="backend_draft"):
            self.assertEqual(self.observe()[1]["admission"]["drainState"], "not-applicable")
            self.set_policy(state, "single")
            admission = self.observe()[1]["admission"]
            self.assertEqual((admission["policy"], admission["drainState"], admission["draining"],
                              admission["sharedHolders"]), ("single", "draining", True, ["run-shared"]))
            with telemetry.monitor_router_hold():      # never probe owner.lock while Fix may hold it
                self.assertEqual(self.observe()[1]["admission"]["drainState"], "unknown")
        admission = self.observe()[1]["admission"]
        self.assertEqual((admission["drainState"], admission["draining"]), ("drained", False))
        (self.root / "policy.json").write_text('{"concurrency": "multi"}')
        admission = self.observe()[1]["admission"]
        self.assertEqual((admission["policy"], admission["source"]), (None, "invalid"))

    def test_dual_read_probes_never_block_the_router(self):
        """The readers only take momentary NB probes and release them at once: a router run that
        claims, begins, checkpoints and finishes over and over while the Monitor observes in a loop
        is never refused, and afterwards every lock is free."""
        state = self.per_run_layout(policy="single")
        stop = threading.Event()
        failures = []

        def monitor():
            while not stop.is_set():
                try:
                    telemetry._router_observation(time.time())
                except Exception as exc:          # pragma: no cover - reported below
                    failures.append(repr(exc))

        thread = threading.Thread(target=monitor)
        # Lock probes only: process attribution (lsof/ps) is not what this test is about.
        patcher = mock.patch.object(telemetry, "_router_openers", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread.start()
        try:
            for index in range(25):
                run_id, digest = f"run-{index}", hashlib.sha256(str(index).encode()).hexdigest()
                with state["RouterOwner"](self.root) as owner:
                    owner.claim(run_id, digest, until=time.monotonic() + 0.3)
                    owner.begin(run_id, digest)
                    owner.checkpoint("backend_draft", {"selectedHost": "mac"})
                    owner.finish({"kind": "codemode.router.v1", "status": "NOT_RUN", "runId": run_id,
                                  "requestSha256": digest, "accepted": False, "advisoryOnly": True}, 3)
        finally:
            stop.set()
            thread.join(10)
        self.assertEqual(failures, [])
        self.assert_no_lock_left()
        self.assertEqual(self.observe()[0]["rows"], [])

    def test_observe_router_never_raises_and_is_remembered_for_activity(self):
        with mock.patch.object(telemetry, "_router_observation", side_effect=RuntimeError("boom")):
            router = telemetry.observe_router(time.time())
        self.assertEqual((router["rows"], router["error"]), ([], "Router run observation failed"))
        self.assertIs(telemetry.last_router_observation(), router)
        mode = telemetry._online_code_mode(time.time(), "now", router)
        self.assertEqual((mode["state"], mode["activeRuns"], mode["queuedRuns"]), ("unknown", [], []))
        with mock.patch.object(telemetry.time, "time", return_value=time.time() + 60):
            self.assertIsNone(telemetry.last_router_observation())   # only this sample's observation

    def test_dual_read_bounds_listing_and_full_reads(self):
        """At most 64 names per directory and 4 full reads per sample; older rows stay listed with
        their liveness but no checkpoint, and a truncated listing says so."""
        state = self.per_run_layout(policy="multi")
        for index in range(6):
            self.run_in_process(state, f"run-{index}", hashlib.sha256(bytes([index])).hexdigest(),
                                stage="backend_draft", keep=False)
            os.utime(self.root / "active" / f"run-{index}.json", (1000 + index, 1000 + index))
        with mock.patch.object(telemetry, "_ROUTER_LISTED", 5), \
             mock.patch.object(telemetry, "_safe_file", wraps=telemetry._safe_file) as reads:
            router, mode, _pipeline, _ = self.observe()
        record_reads = [c for c in reads.call_args_list if "/active/" in str(c.args[0])]
        self.assertEqual(len(record_reads), telemetry._ROUTER_FULL_READS)
        self.assertTrue(router["truncated"])
        self.assertEqual(len(router["rows"]), 5)
        staged = [row["runId"] for row in router["rows"] if row["stage"]]
        self.assertEqual(sorted(staged), ["run-2", "run-3", "run-4", "run-5"])
        self.assertTrue(mode["runsTruncated"])

    # -- converge round (2026-09-27): each review finding reproduced, fixed and pinned ----------

    def rewrite(self, path, change=None, *, raw=None):
        """Replace a journal file with new private bytes (a fresh inode at nlink 1)."""
        if raw is None:
            value = json.loads(path.read_text())
            change(value)
            raw = json.dumps(value)
        if os.path.lexists(path):
            os.unlink(path)
        _private_write(path, raw.replace('"__HUGE__"', _HUGE))

    def activity_runs(self, router, now=None):
        """activity.collect_activity over this journal, fed by ``router`` as this sample's observation."""
        import activity
        now = time.time() if now is None else now
        with telemetry._LAST_ROUTER_OBSERVATION_LOCK:
            telemetry._LAST_ROUTER_OBSERVATION.update(sampledAt=now, value=router)
        with mock.patch.object(activity, "_ACTIVE", self.root / "active.json"), \
             mock.patch.object(activity, "_ROOT", self.root), \
             mock.patch.object(activity, "_receipt_files", return_value=[]), \
             mock.patch.object(activity, "_archive_files", return_value=[]):
            return activity.collect_activity(now)

    def test_converge_hostile_numbers_never_fail_the_observation(self):
        """Sol #3: an integer too large for a float (JSON allows any length) makes math.isfinite
        raise OverflowError.  In a record, a note, the fence, a Nisi marker or the readiness receipt
        it now only invalidates that one file: every other run is still observed."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-ok", "a" * 64, stage="backend_draft", keep=False)
        self.run_in_process(state, "run-huge", "b" * 64, stage="backend_draft", keep=False)
        self.run_in_process(state, "run-note", "c" * 64, begin=False, note=("waiting", "mac-pair", "pre-begin"))
        self.rewrite(self.root / "active" / "run-huge.json", lambda value: value.update(startedUnix="__HUGE__"))
        self.rewrite(self.root / "notes" / "run-note.json", lambda value: value.update(sinceUnix="__HUGE__"))
        self.write_marker("run-ok.draft")
        self.rewrite(self.pending, lambda value: value.update(started_unix="__HUGE__"))
        router, mode, pipeline, _source = self.observe()
        self.assertIsNone(router["error"])
        self.assertEqual({row["runId"]: row["state"] for row in router["rows"]},
                         {"run-ok": "unresolved", "run-huge": "unreadable", "run-note": "admitting"})
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 1, "unresolved": 2})
        self.assertEqual((pipeline["pendingMarkerObserved"], pipeline["recoveryRequired"],
                          pipeline["pendingMarkerAgeSeconds"]), (True, True, None))
        self.rewrite(self.root / "install-fence.json", lambda value: value.update(sinceUnix="__HUGE__"))
        self.assertEqual(telemetry.observe_router(time.time())["install"]["state"], "invalid")
        os.mkdir(self.base / "launcher", 0o700)
        self.rewrite(self.base / "launcher/readiness.json", raw=json.dumps({
            "schemaVersion": 1, "status": "PREFLIGHT_COMPLETED", "observedAtUnix": "__HUGE__",
            "source": "online-code-mode", "client": None, "chatId": None}))
        self.assertEqual(telemetry._launcher_readiness(time.time()), ("invalid", None))
        self.assertFalse(telemetry._finite(True))
        self.assertTrue(telemetry._finite(2 ** 53 - 1))
        self.assertFalse(telemetry._finite(2 ** 53))

    def test_converge_legacy_live_route_past_the_freshness_limit_is_live_and_stalled(self):
        """Sol #4 / review 6: R2.9 legacy liveness is rules (i)-(iv); the checkpoint's age is not
        one of them.  A route still holding owner.lock with its exact work process 301 s after its
        last checkpoint is live but stalled (it stops blinking), never an unresolved run."""
        with self.holder("legacy", "legacy-live", "a" * 64, stage="backend_draft") as pid:
            later = time.time() + 301
            router = telemetry._router_observation(later)
            mode = telemetry._online_code_mode(later, "later", router)
            pipeline, source = telemetry._pipeline(later, router)
            telemetry._mark_live_route(pipeline, source, mode)
        row = self.row(router, "legacy-live")
        self.assertEqual((row["live"], row["state"], row["stalled"], row["pid"]), (True, "running", True, pid))
        self.assertEqual((mode["state"], mode["active"], mode["taskState"], mode["blinking"]),
                         ("processing", True, "processing", False))
        self.assertIn("may be stalled", mode["evidence"])
        self.assertEqual(mode["runCounts"], {"running": 1, "queued": 0, "unresolved": 0})
        self.assertEqual((pipeline["status"], pipeline["recoveryRequired"], source["state"]), ("running", False, "live"))

    def test_converge_legacy_record_changing_under_observation_is_not_unresolved(self):
        """Review 5 (and mutant ME): owner.lock released between the first probe and the final
        re-probe is verdict 'unknown': the row is 'changing', re-checked next sample, never counted
        or shown as an unresolved run (and never live)."""
        legacy, _ = self.writer("legacy")
        with legacy["RouterOwner"](self.root) as owner:
            owner.begin("legacy-a", "a" * 64)
            owner.checkpoint("backend_draft", {"selectedHost": "mac"})
        started = json.loads((self.root / "active.json").read_text())["startedUnix"]
        real, probes = telemetry._probe_lock, []

        def probe(path, **kwargs):
            if path.name == "owner.lock":
                probes.append(path)
                if len(probes) == 2:
                    return "free"
            return real(path, **kwargs)

        fd = os.open(str(self.root / "owner.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.object(telemetry, "_router_owner_process", return_value=(4242, started - 1)), \
                 mock.patch.object(telemetry, "_probe_lock", side_effect=probe):
                router, mode, pipeline, _source = self.observe()
        finally:
            os.close(fd)
        self.assertEqual(len(probes), 2)
        row = self.row(router, "legacy-a")
        self.assertEqual((row["live"], row["state"], row["_verdict"]), (False, "changing", "unknown"))
        self.assertIn("released during observation", row["evidence"])
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 0, "unresolved": 0})
        self.assertEqual([(r["runId"], r["state"]) for r in mode["activeRuns"]], [("legacy-a", "changing")])
        self.assertEqual((mode["state"], mode["taskState"]), ("unknown", "unknown"))
        self.assertEqual([p["status"] for p in pipeline["pipelines"]], ["changing"])
        runs = self.activity_runs(router)["runs"]
        self.assertEqual([(r["runId"], r["status"], r["activity"]) for r in runs], [("legacy-a", "changing", "unknown")])

    def test_converge_run_finishing_during_the_sample_leaves_no_row(self):
        """Review 4 / Sol #11: a live run that archives, clears its record and releases its lock
        between the record read and the lock probe finished cleanly: no 'archived-uncleared' row
        telling the owner to run a gate.  A listed record that vanishes before its read is dropped
        the same way."""
        state = self.per_run_layout(policy="multi")
        owner = state["RouterOwner"](self.root)
        owner.__enter__()
        owner.claim("run-g", "b" * 64, until=time.monotonic() + 2)
        owner.begin("run-g", "b" * 64)
        owner.checkpoint("final_validation_intent", {"selectedHost": "mac"})
        real = telemetry._router_record

        def record(path, info):
            status, value = real(path, info)
            if path.name == "run-g.json" and owner._run is not None:
                owner.finish({"kind": "codemode.router.v1", "status": "RESPONSE_VALIDATED", "runId": "run-g",
                              "requestSha256": "b" * 64, "accepted": False, "advisoryOnly": True}, 0)
                owner.__exit__(None, None, None)
            return status, value

        with mock.patch.object(telemetry, "_router_record", side_effect=record):
            router, mode, _pipeline, _source = self.observe()
        self.assertFalse((self.root / "active" / "run-g.json").exists())
        self.assertTrue((self.root / "archive" / "run-g.json").exists())
        self.assertEqual(router["rows"], [])
        self.assertNotEqual(mode["taskState"], "unfinished")
        self.run_in_process(state, "run-v", "c" * 64, stage="backend_draft", keep=False)
        # p2-readers converge (Sol #11, R2.9 rule 2): a listed record gone before its read, with
        # no archive, is looked for again (<= 3 x 20 ms), then shown as one unreadable row;
        # only a run whose archive exists finished cleanly and leaves no row.
        with mock.patch.object(telemetry, "_router_record", return_value=("absent", None)), \
             mock.patch.object(telemetry.time, "sleep") as sleep:
            rows = telemetry._router_observation(time.time())["rows"]
        self.assertEqual(sleep.call_count, telemetry._ROUTER_READ_RETRIES - 1)
        self.assertEqual([(row["runId"], row["state"], row["readable"]) for row in rows],
                         [("run-v", "unreadable", False)])
        _private_write(self.root / "archive" / "run-v.json", "{}")
        with mock.patch.object(telemetry, "_router_record", return_value=("absent", None)), \
             mock.patch.object(telemetry.time, "sleep") as sleep:
            self.assertEqual(telemetry._router_observation(time.time())["rows"], [])
        sleep.assert_not_called()
        os.unlink(self.root / "archive" / "run-v.json")
        self.assertEqual([row["runId"] for row in telemetry._router_observation(time.time())["rows"]], ["run-v"])

    def test_converge_a_live_row_past_the_read_budget_is_still_inode_checked(self):
        """Sol #1: rows past the 4 full reads had no runLock, so a held lock was live without the
        inode rule.  A held lock past the budget now has its record read too (bounded, cached):
        the original lock is live and bound, an impostor on a replaced lock is never live, and if
        even that budget is spent the row is live with its identity marked unverified."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-old", "0" * 64, stage="backend_draft")
        for index in range(1, 5):
            self.run_in_process(state, f"run-{index}", f"{index}" * 64, stage="backend_draft", keep=False)
        for index, run_id in enumerate(("run-old", "run-1", "run-2", "run-3", "run-4")):
            os.utime(self.root / "active" / f"{run_id}.json", (1000 + index, 1000 + index))
        with mock.patch.object(telemetry, "_safe_file", wraps=telemetry._safe_file) as reads:
            router, mode, _pipeline, _source = self.observe()
        self.assertEqual(len([c for c in reads.call_args_list if "/active/" in str(c.args[0])]),
                         telemetry._ROUTER_FULL_READS + 1)
        row = self.row(router, "run-old")
        self.assertEqual((row["lock"], row["live"], row["lockBound"], row["stage"]),
                         ("held", True, True, "backend_draft"))
        self.assertNotIn("identity cannot be checked", mode["evidence"])
        # p2-readers converge (Sol High #1/#6): with even that budget spent, a held lock whose
        # record was not read proves nothing about this run: unverified (run lock held), never
        # live or running, never idle.
        with mock.patch.object(telemetry, "_ROUTER_ATTRIBUTED", 0):
            router, mode, _pipeline, _source = self.observe()
        row = self.row(router, "run-old")
        self.assertEqual((row["live"], row["lockBound"], row["lockHeld"], row["read"], row["state"]),
                         (False, False, True, False, "unverified"))
        self.assertEqual(mode["runCounts"]["running"], 0)
        self.assertEqual({r["runId"]: (r["state"], r["lockHeld"]) for r in mode["activeRuns"]}["run-old"],
                         ("unverified", True))
        with mock.patch.object(telemetry, "_ROUTER_ATTRIBUTED", 0), mock.patch.object(telemetry, "_ROUTER_FULL_READS", 0):
            for index in range(1, 5):
                os.unlink(self.root / "active" / f"run-{index}.json")
            router, mode, _pipeline, _source = self.observe()
        self.assertEqual((router["primary"]["runId"], mode["state"], mode["taskState"]), ("run-old", "unknown", "unfinished"))
        self.assertIn("identity cannot be checked", mode["evidence"])
        lock = self.root / "locks" / "run-old.lock"
        os.unlink(lock)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)        # an impostor, held
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            router, mode, _pipeline, _source = self.observe()
        finally:
            os.close(fd)
        row = self.row(router, "run-old")
        self.assertEqual((row["lock"], row["live"], row["state"]), ("replaced", False, "unverified"))
        self.assertEqual(mode["runCounts"]["running"], 0)

    def test_converge_an_invalid_record_never_drives_the_pipeline_or_activity(self):
        """Sol #6: a readable JSON object outside the closed envelope is 'unreadable' everywhere:
        the pipeline does not take its stage or time, and activity does not show it as a run."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-x", "a" * 64, stage="backend_draft", keep=False)
        self.rewrite(self.root / "active" / "run-x.json", lambda value: value.update(extra=1))
        router, mode, pipeline, source = self.observe()
        self.assertEqual(self.row(router, "run-x")["state"], "unreadable")
        self.assertIn("could not be read whole", mode["evidence"])
        self.assertEqual((pipeline["status"], pipeline["stage"], pipeline["ageSeconds"], source["state"]),
                         ("unknown", None, None, "error"))
        runs = self.activity_runs(router)["runs"]
        self.assertEqual([(r["runId"], r["status"], r["stage"]) for r in runs], [("run-x", "unreadable", None)])

    def test_converge_a_queued_primary_is_listed_never_promoted_to_running(self):
        """Sol #8 (and mutant ML): a live run whose note says waiting / admitting is queued: the
        pipeline says so and _mark_live_route never turns it into ROUTE RUNNING, with or without a
        foreign marker; a begun run waiting for a lane is waiting and counted queued."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-q", "a" * 64, begin=False, note=("waiting", "mac-pair", "pre-begin"))
        router, mode, pipeline, source = self.observe()
        self.assertEqual((mode["state"], mode["routeId"]), ("processing", "run-q"))
        # p2-readers converge (orchestrator note, brief item 4): a queued-only primary never reads
        # as processing and never blinks; the pipeline names what it waits for.
        self.assertEqual((mode["taskState"], mode["blinking"], mode["runCounts"]["running"]), ("queued", False, 0))
        self.assertEqual((pipeline["queuePhase"], pipeline["queueResource"]), ("waiting", "mac-pair"))
        self.assertEqual((pipeline["status"], pipeline["runId"], pipeline.get("liveOwnerVerified")),
                         ("queued", "run-q", None))
        self.assertEqual(source["state"], "live")
        self.assertEqual([p["status"] for p in pipeline["pipelines"]], ["waiting"])
        self.write_marker("other-run.draft")
        _router, _mode, pipeline, _source = self.observe()
        self.assertEqual((pipeline["status"], pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]),
                         ("queued", True, True))
        self.pending.unlink()
        self.run_in_process(state, "run-w", "b" * 64, stage="backend_draft", note=("waiting", "pc-route", "backend"))
        router, mode, pipeline, _source = self.observe()
        self.assertEqual((self.row(router, "run-w")["state"], self.row(router, "run-w")["layout"]), ("waiting", "per-run"))
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 2, "unresolved": 0})
        self.assertEqual(pipeline["status"], "queued")

    def test_converge_a_marker_must_be_whole_to_be_attributed(self):
        """Sol #5: only the marker pipeline_integrations writes (operation draft/review/answer, a
        sha256 digest, a finite start not in the future) is attributed to a live run; an unreadable
        marker beside a live route stays an error instead of being overwritten with 'live'."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-x", "a" * 64, stage="backend_draft")
        good = {"kind": "codemode.nisi.pending.v1", "started_unix": time.time() - 5, "input_sha256": "f" * 64,
                "runId": "run-x.draft", "operation": "draft"}
        for change in ({"operation": 7}, {"operation": "work"}, {"input_sha256": []}, {"input_sha256": "F" * 64},
                       {"started_unix": time.time() + 3600}, {"started_unix": "__HUGE__"}, {"started_unix": True}):
            with self.subTest(change=change):
                self.rewrite(self.pending, raw=json.dumps(dict(good, **change)))
                _router, _mode, pipeline, _source = self.observe()
                self.assertIsNone(pipeline["pendingMarkerAttributedTo"])
                self.assertEqual((pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]), (True, True))
        self.rewrite(self.pending, raw=json.dumps(good))
        self.assertEqual(self.observe()[2]["pendingMarkerAttributedTo"], "run-x")
        self.rewrite(self.pending, raw="{not json")
        _router, _mode, pipeline, source = self.observe()
        self.assertEqual((pipeline["status"], pipeline["pendingMarkerUnreadable"], source["state"]),
                         ("running", True, "error"))
        self.assertIn("Nisi marker invalid or unreadable", source["detail"])

    def test_converge_the_install_fence_is_read_closed(self):
        """Sol #10 / review 8 (and mutant MJ): a fence the router's own reader refuses (a stamp that
        is not YYYYmmddTHHMMSSZ, a sinceUnix that is not a finite number) is 'invalid': no route is
        admitted.  No fence while the installed code carries router_fence.py is 'missing', never
        the single-run router, and a legacy record under it is never live."""
        self.per_run_layout(policy="single")
        fence = self.root / "install-fence.json"
        good = json.loads(fence.read_text())
        self.assertEqual(telemetry._router_install_state(self.root)["state"], "installed")
        for change in ({"stamp": 17}, {"stamp": "not-a-stamp"}, {"stamp": None}, {"stamp": "20260927T1200Z"},
                       {"sinceUnix": "soon"}, {"sinceUnix": "__HUGE__"}, {"sinceUnix": True}):
            with self.subTest(change=change):
                self.rewrite(fence, raw=json.dumps(dict(good, **change)))
                router = telemetry._router_observation(time.time())
                self.assertEqual(router["install"]["state"], "invalid")
                self.assertEqual((router["admission"]["policy"], router["admission"]["source"]),
                                 (None, "fence-invalid"))
        os.unlink(fence)
        self.assertEqual(telemetry._router_observation(time.time())["admission"]["source"], "legacy-router")
        (self.base / "router_fence.py").write_text("")
        router, mode, _pipeline, _source = self.observe()
        self.assertEqual((router["install"]["state"], router["admission"]["source"]), ("missing", "fence-missing"))
        self.assertEqual((mode["state"], mode["taskState"]), ("inactive", "idle"))
        self.assertIn("install fence is missing", mode["evidence"])
        with self.holder("legacy", "legacy-live", "a" * 64, stage="backend_draft"):
            router, mode, _pipeline, _source = self.observe()
        row = self.row(router, "legacy-live")
        self.assertEqual((row["live"], row["state"], mode["taskState"]), (False, "unresolved", "unfinished"))
        self.assertIn("install fence is missing or invalid", row["evidence"])

    def test_converge_the_install_barrier_needs_its_name_and_the_second_link(self):
        """Mutant MD: the barrier name alone (another file) or a second link under another name is
        never "install in progress"; the second link alone leaves owner.lock unsafe to probe."""
        self.per_run_layout(policy="multi")
        barrier = self.root / "owner.lock.install-barrier"
        _private_write(barrier, "")
        install = telemetry._router_install_state(self.root)
        self.assertEqual((install["barrier"], install["inProgress"]), (False, False))
        os.unlink(barrier)
        os.link(self.root / "owner.lock", self.root / "owner.lock.other")
        install = telemetry._router_install_state(self.root)
        self.assertEqual((install["barrier"], install["inProgress"]), (False, False))
        self.assertEqual(telemetry._probe_lock(self.root / "owner.lock"), "unsafe")
        os.link(self.root / "owner.lock", barrier)
        os.unlink(self.root / "owner.lock.other")
        self.assertEqual(telemetry._router_install_state(self.root)["inProgress"], True)

    def test_converge_any_run_lock_live_fails_closed(self):
        """Mutants MA, MB, MC (R2.9 legacy rule (ii)): every locks/*.lock is probed; a listing cut
        short, an unsafe lock file, or a locks/ that cannot be read or listed means a per-run lock
        may be live, so the legacy route is never proven live."""
        locks = self.root / "locks"
        self.assertFalse(telemetry._any_run_lock_live(locks))
        os.makedirs(locks, mode=0o700)
        for name in ("a.lock", "b.lock", "c.lock"):
            _private_write(locks / name, "")
        _private_write(locks / "readme.txt", "", mode=0o644)          # not a lock file: ignored
        self.assertFalse(telemetry._any_run_lock_live(locks))
        with mock.patch.object(telemetry, "_ROUTER_SCAN", 2):
            self.assertTrue(telemetry._any_run_lock_live(locks))
        _private_write(locks / "d.lock", "", mode=0o644)
        self.assertTrue(telemetry._any_run_lock_live(locks))
        os.unlink(locks / "d.lock")
        real = Path.lstat

        def lstat(path, *args, **kwargs):
            if path == locks:
                raise PermissionError(13, "denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", lstat):
            self.assertTrue(telemetry._any_run_lock_live(locks))
        with mock.patch.object(telemetry.os, "scandir", side_effect=PermissionError(13, "denied")):
            self.assertTrue(telemetry._any_run_lock_live(locks))
        fd = os.open(str(locks / "b.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertTrue(telemetry._any_run_lock_live(locks))
        finally:
            os.close(fd)
        self.assertFalse(telemetry._any_run_lock_live(locks))

    def test_converge_drain_state_comes_from_owner_lock_probes(self):
        """Mutants MH, MI (R2.2.3): under single, an EX holder of owner.lock means no shared holder
        exists (drained); a shared holder means draining; owner.lock replaced (not the inode the
        fence bound) is never probed: unknown."""
        self.per_run_layout(policy="single")
        self.assertEqual(self.observe()[1]["admission"]["drainState"], "drained")
        fd = os.open(str(self.root / "owner.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.observe()[1]["admission"]["drainState"], "drained")
            fcntl.flock(fd, fcntl.LOCK_UN)
            fcntl.flock(fd, fcntl.LOCK_SH)
            admission = self.observe()[1]["admission"]
            self.assertEqual((admission["drainState"], admission["draining"]), ("draining", True))
        finally:
            os.close(fd)
        os.unlink(self.root / "owner.lock")
        _private_write(self.root / "owner.lock", "")
        router = telemetry._router_observation(time.time())
        self.assertIs(router["install"]["ownerLockBound"], False)
        self.assertEqual(router["admission"]["drainState"], "unknown")

    def test_converge_run_attribution_needs_the_note_pid_and_pc_lanes_hold_the_pc_route(self):
        """Mutants MF, MG: among two exact work processes that have a run's lock open, only the
        note's pid is the owner (without a note: ambiguous, unknown); a run holding or waiting for
        a PC lane token holds the PC route."""
        lock_name = str(self.root / "locks" / "run-a.lock")
        argv = ["/usr/bin/python3", "-I", "-B", str(self.driver), "work"]
        row = {"runId": "run-a", "live": True, "layout": "per-run", "startedUnix": 1000.0, "note": None,
               "_notePid": 222, "pid": None}
        with mock.patch.object(telemetry, "_router_openers", return_value=[(111, "r", lock_name), (222, "r", lock_name)]), \
             mock.patch.object(telemetry, "_process_table", return_value={111: (999.0, argv), 222: (999.0, argv)}):
            telemetry._run_owners(self.root, [row])
            self.assertEqual(row["pid"], 222)
            row.update(_notePid=None, pid=None)
            telemetry._run_owners(self.root, [row])
            self.assertIsNone(row["pid"])
        lanes = telemetry._router_lanes([
            {"live": True, "runId": "run-f", "note": {"resource": "pc-lane-fast", "phase": "running"}},
            {"live": True, "runId": "run-d", "note": {"resource": "pc-lane-deep", "phase": "waiting"}},
            {"live": False, "runId": "run-x", "note": {"resource": "pc-route", "phase": "running"}}])
        self.assertEqual((lanes["pc-fast"]["holders"], lanes["pc-deep"]["waiting"], lanes["pc-route"]["holders"]),
                         (["run-f"], ["run-d"], ["run-f", "run-d"]))

    def test_converge_legacy_and_per_run_records_of_one_run_are_one_row(self):
        """Mutant MM: a legacy active.json naming a run that also has active/<id>.json is the same
        run: one row (the per-run one), in telemetry and in activity."""
        state = self.per_run_layout(policy=None, fence=None)
        self.run_in_process(state, "run-dup", "a" * 64, stage="backend_review", keep=False)
        record = json.loads((self.root / "active" / "run-dup.json").read_text())
        record.pop("runLock")
        _private_write(self.root / "active.json", json.dumps(record))
        router, mode, _pipeline, _source = self.observe()
        self.assertTrue(router["legacyPresent"])
        self.assertEqual([(r["runId"], r["layout"]) for r in router["rows"]], [("run-dup", "per-run")])
        self.assertEqual(mode["runCounts"], {"running": 0, "queued": 0, "unresolved": 1})
        runs = self.activity_runs(router)["runs"]
        self.assertEqual([(r["runId"], r["layout"]) for r in runs], [("run-dup", "per-run")])

    # -- p2-readers converge (2026-09-27): the Claude reviewer's and Sol's round-2 findings ----------

    def test_p2conv_a_held_lock_never_makes_an_unreadable_record_live(self):
        """Sol High (round-1 #1/#6 and N2): a held run lock proves a run live only on the inode its
        valid record binds.  Beside a record outside the envelope, or bad JSON, the row is
        'unreadable' with its lock held: never running, never counted running, never a lane holder,
        and activity never promotes it, not even from an observation taken while it was whole."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-x", "a" * 64, stage="backend_draft", note=("running", "mac-pair", "backend"))
        before, _mode, _pipeline, _source = self.observe()
        self.assertEqual((self.row(before, "run-x")["state"], self.row(before, "run-x")["live"]), ("running", True))
        for label, change, raw in (("envelope", lambda value: value.update(extra=1), None), ("json", None, "{not json")):
            with self.subTest(record=label):
                self.rewrite(self.root / "active" / "run-x.json", change, raw=raw)
                router, mode, pipeline, source = self.observe()
                row = self.row(router, "run-x")
                self.assertEqual((row["state"], row["live"], row["lockHeld"], row["lock"], row["readable"], row["note"]),
                                 ("unreadable", False, True, "held", False, None))
                self.assertEqual(mode["runCounts"], {"running": 0, "queued": 0, "unresolved": 1})
                self.assertEqual((mode["state"], mode["taskState"], mode["blinking"]), ("unknown", "unfinished", False))
                self.assertIn("its run lock is held, so it may still be live", mode["evidence"])
                self.assertEqual((mode["activeRuns"][0]["state"], mode["activeRuns"][0]["lockHeld"]), ("unreadable", True))
                self.assertEqual((pipeline["status"], source["state"]), ("unknown", "error"))
                self.assertEqual(router["lanes"]["mac-pair"]["holders"], [])
                for observation in (router, before):
                    runs = self.activity_runs(observation)["runs"]
                    self.assertEqual([(r["runId"], r["status"], r["activity"]) for r in runs],
                                     [("run-x", "unreadable", "unknown")])

    def test_p2conv_activity_lists_a_live_route_older_than_its_four_record_reads(self):
        """Claude reviewer (Low): activity read only the 4 newest records, so a live run in a long
        PC call (older record) vanished from Live activity while the tab said Processing.  Every
        run the observation verified live is listed."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-live", "0" * 64, stage="backend_draft", note=("running", "pc-route", "backend"),
                            meta={"client": "claude", "host": "windows", "operation": "work"})
        for index in range(1, 5):
            self.run_in_process(state, f"run-dead{index}", f"{index}" * 64, stage="incomplete",
                                evidence={"recoveryRequired": True, "code": "NISI_OWNER_BUSY"}, keep=False)
        now = time.time()
        os.utime(self.root / "active" / "run-live.json", (now - 100, now - 100))
        router, mode, _pipeline, _source = self.observe()
        self.assertEqual(mode["runCounts"], {"running": 1, "queued": 0, "unresolved": 4})
        result = self.activity_runs(router)
        rows = {r["runId"]: r for r in result["runs"]}
        self.assertEqual(len(rows), 5)
        self.assertEqual((rows["run-live"]["status"], rows["run-live"]["activity"], rows["run-live"]["stage"],
                          rows["run-live"]["client"], rows["run-live"]["host"], rows["run-live"]["layout"]),
                         ("running", "running", "backend_draft", "claude", "windows", "per-run"))
        self.assertIn("1 verified live", result["sources"][0]["detail"])
        self.assertEqual(len({r["traceKey"] for r in result["runs"]}), 5)

    def test_p2conv_a_deeply_nested_record_is_one_unreadable_row_everywhere(self):
        """Claude reviewer (Medium): json.loads raises RecursionError on a deeply nested record (a
        2 MB file, under the 3 MiB cap).  activity caught only OSError/ValueError/TypeError, so its
        catch-all emptied every active row, the live run included.  It is now one unreadable row."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-live", "a" * 64, stage="backend_draft", note=("running", "mac-pair", "backend"))
        self.run_in_process(state, "run-dead", "b" * 64, stage="backend_review", keep=False)
        _private_write(self.root / "active" / "run-deep.json", '{"x":' + "[" * 1000000 + "]" * 1000000 + "}")
        router, mode, _pipeline, _source = self.observe()
        # Telemetry isolates it as one row (it has no run lock file either, so 'unverified').
        self.assertEqual({row["runId"]: (row["state"], row["readable"]) for row in router["rows"]},
                         {"run-live": ("running", True), "run-dead": ("unresolved", True),
                          "run-deep": ("unverified", False)})
        result = self.activity_runs(router)
        self.assertEqual({r["runId"]: (r["status"], r["activity"]) for r in result["runs"]},
                         {"run-live": ("running", "running"), "run-dead": ("unresolved", "unknown"),
                          "run-deep": ("unreadable", "unknown")})
        self.assertEqual(result["sources"][0]["state"], "live")

    def test_p2conv_one_run_id_in_both_layouts_never_hides_a_live_legacy_route(self):
        """Sol N1: an unreadable active/R.json suppressed the legacy active.json of R, so R's L0
        proof never ran and a live legacy route was hidden.  Now both rows show, the legacy one is
        verified by its own rules, and the collision is named."""
        with self.holder("legacy", "run-c", "a" * 64, stage="backend_draft") as pid:
            os.mkdir(self.root / "active", 0o700)
            _private_write(self.root / "active" / "run-c.json", "{not json")
            router, mode, _pipeline, _source = self.observe()
            rows = {(r["runId"], r["layout"]): r for r in router["rows"]}
            # The per-run row has no run lock file either: unverified, never live.
            self.assertEqual({key: (r["state"], r["live"], r["readable"], r["collision"]) for key, r in rows.items()},
                             {("run-c", "legacy"): ("running", True, True, True),
                              ("run-c", "per-run"): ("unverified", False, False, True)})
            self.assertEqual(rows[("run-c", "legacy")]["pid"], pid)
            self.assertEqual((router["collisions"], mode["runIdCollisions"]), (["run-c"], ["run-c"]))
            self.assertEqual((mode["state"], mode["routeId"]), ("processing", "run-c"))
            self.assertEqual(mode["runCounts"], {"running": 1, "queued": 0, "unresolved": 1})
            self.assertIn("both journal layouts", mode["evidence"])
            runs = self.activity_runs(router)["runs"]
            self.assertEqual(sorted((r["layout"], r["status"], r["activity"]) for r in runs),
                             [("legacy", "running", "running"), ("per-run", "unreadable", "unknown")])
            self.assertEqual(len({r["traceKey"] for r in runs}), 2)

    def test_p2conv_a_foreign_input_under_one_run_id_is_a_collision_not_one_row(self):
        """Sol N1 (the fold rule): a legacy record is one run with active/R.json only when that
        per-run record is valid, has the same input and the legacy route is not live."""
        state = self.per_run_layout(policy=None, fence=None)
        self.run_in_process(state, "run-dup", "a" * 64, stage="backend_review", keep=False)
        record = json.loads((self.root / "active" / "run-dup.json").read_text())
        record.pop("runLock")
        record["inputSha256"] = record["checkpoint"]["inputSha256"] = "b" * 64
        _private_write(self.root / "active.json", json.dumps(record))
        router, mode, _pipeline, _source = self.observe()
        self.assertEqual(sorted((r["layout"], r["state"], r["collision"]) for r in router["rows"]),
                         [("legacy", "unresolved", True), ("per-run", "unresolved", True)])
        self.assertEqual(mode["runCounts"]["unresolved"], 2)
        self.assertIn("both journal layouts", mode["evidence"])
        self.assertEqual(sorted(r["layout"] for r in self.activity_runs(router)["runs"]), ["legacy", "per-run"])
        # Beside an unreadable active/R.json the legacy record (not live here) is its own row in
        # activity too, never folded away.
        self.rewrite(self.root / "active" / "run-dup.json", raw="{not json")
        router = self.observe()[0]
        self.assertEqual(sorted((r["layout"], r["state"], r["collision"]) for r in router["rows"]),
                         [("legacy", "unresolved", True), ("per-run", "unreadable", True)])
        self.assertEqual(sorted((r["layout"], r["status"]) for r in self.activity_runs(router)["runs"]),
                         [("legacy", "unresolved"), ("per-run", "unreadable")])

    def test_p2conv_a_live_legacy_route_is_never_folded_into_its_per_run_twin(self):
        """Sol N1 (the fold rule, live side): even a valid active/R.json with the same input never
        hides R's legacy route while the legacy rules prove it live: both rows, collision named."""
        with self.holder("legacy", "run-t", "a" * 64, stage="backend_draft") as pid:
            record = json.loads((self.root / "active.json").read_text())
            os.mkdir(self.root / "active", 0o700)
            os.mkdir(self.root / "locks", 0o700)
            _private_write(self.root / "locks" / "run-t.lock", "")
            lock = os.stat(self.root / "locks" / "run-t.lock")
            _private_write(self.root / "active" / "run-t.json",
                           json.dumps(dict(record, runLock={"dev": lock.st_dev, "ino": lock.st_ino})))
            router, mode, _pipeline, _source = self.observe()
        rows = {r["layout"]: r for r in router["rows"]}
        self.assertEqual({layout: (r["state"], r["live"], r["valid"], r["collision"]) for layout, r in rows.items()},
                         {"legacy": ("running", True, True, True), "per-run": ("unresolved", False, True, True)})
        self.assertEqual((rows["legacy"]["pid"], mode["state"], mode["routeId"]), (pid, "processing", "run-t"))
        self.assertIn("both journal layouts", mode["evidence"])

    def test_p2conv_a_listed_record_proves_nothing_unless_locks_is_a_private_directory(self):
        """Sol N4: O_NOFOLLOW guards only the lock file's own name.  A locks/ that is a symlink (or
        not private) makes every per-run lock 'unsafe' and the observation an error."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-l", "a" * 64, stage="backend_draft")
        self.assertEqual(self.row(self.observe()[0], "run-l")["state"], "running")
        os.rename(self.root / "locks", self.base / "locks-real")
        os.symlink(self.base / "locks-real", self.root / "locks")
        try:
            router, mode, _pipeline, _source = self.observe()
        finally:
            os.unlink(self.root / "locks")
            os.rename(self.base / "locks-real", self.root / "locks")
        row = self.row(router, "run-l")
        self.assertEqual((row["lock"], row["live"], row["state"]), ("unsafe", False, "unverified"))
        self.assertEqual(router["error"], "Router run directories are unsafe or unreadable")
        self.assertNotEqual(mode["state"], "processing")
        self.assertEqual(telemetry._private_dir(self.root / "locks"), "ok")

    def test_p2conv_notes_are_read_to_their_enumerated_schema(self):
        """Sol N5 (and reviewer mutant R9): a note is used only when every field is one the spec
        enumerates (host may also be 'auto', which the router records for an auto request) and it
        names its own run; otherwise it is ignored: a begun run reads running, a record-less one is
        still in admission (begin writes the record first)."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-n", "a" * 64, stage="backend_draft", note=("waiting", "pc-route", "backend"),
                            meta={"client": "codex", "host": "auto", "operation": "work"})
        row = self.row(self.observe()[0], "run-n")
        self.assertEqual((row["state"], row["host"], row["client"]), ("waiting", "auto", "codex"))
        note = self.root / "notes" / "run-n.json"
        good = json.loads(note.read_text())
        for change in ({"operation": "xyz"}, {"client": "cursor"}, {"host": "moon"}, {"step": "somewhere"},
                       {"step": 7}, {"runId": "run-other"}):
            with self.subTest(change=change):
                self.rewrite(note, raw=json.dumps(dict(good, **change)))
                row = self.row(self.observe()[0], "run-n")
                self.assertEqual((row["note"], row["state"], row["live"]), (None, "running", True))
        self.run_in_process(state, "run-p", "b" * 64, begin=False, note=("waiting", "mac-pair", "pre-begin"))
        self.rewrite(self.root / "notes" / "run-p.json", lambda value: value.update(step="elsewhere"))
        row = self.row(self.observe()[0], "run-p")
        self.assertEqual((row["layout"], row["note"], row["state"], row["live"]), ("note", None, "admitting", True))

    def test_p2conv_note_must_bind_the_record_input_before_attributing_a_run(self):
        """A stale or foreign note under the same run ID cannot change a begun run's phase,
        client, host or lane. Its held L1 still proves the record live."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-bound", "a" * 64, stage="backend_draft",
                            note=("waiting", "pc-route", "backend"),
                            meta={"client": "claude", "host": "windows", "operation": "work"})
        note_path = self.root / "notes" / "run-bound.json"
        self.rewrite(note_path, lambda note: note.update(inputSha256="b" * 64))

        router, mode, _pipeline, _source = self.observe()
        row = self.row(router, "run-bound")
        self.assertEqual((row["live"], row["state"], row["note"]), (True, "running", None))
        self.assertEqual((row["client"], row["host"]), (None, "mac"))
        self.assertEqual((mode["runCounts"], mode["lanes"]["pc-route"]["holders"]),
                         ({"running": 1, "queued": 0, "unresolved": 0}, []))

    def test_p2conv_multi_policy_says_a_caller_override_is_not_observable(self):
        """Sol round-1 #9: CODEMODE_ROUTER_CONCURRENCY=off|single forces single for the caller whose
        environment has it.  That is each router process's environment, never the Monitor's: the
        Monitor's own variable changes nothing, and under multi the admission says the override is
        not observable."""
        state = self.per_run_layout(policy="multi")
        with mock.patch.dict(os.environ, {"CODEMODE_ROUTER_CONCURRENCY": "off"}):
            admission = self.observe()[1]["admission"]
        self.assertEqual((admission["policy"], admission["source"], admission["callerOverride"]),
                         ("multi", "file", "not-observable"))
        self.set_policy(state, "single")
        self.assertIsNone(self.observe()[1]["admission"]["callerOverride"])

    def test_p2conv_legacy_owner_is_exactly_one_work_process_with_no_run_lock_open(self):
        """Reviewer mutants R3, R4 (R2.9 legacy rule (iv)): a process that also has a locks/ file
        open is never the legacy owner, and two matching processes are ambiguous (None)."""
        root = telemetry._real_root(self.root)
        owner, lock = str(root / "owner.lock"), str(root / "locks" / "x.lock")
        argv = ["/usr/bin/python3", "-I", "-B", str(self.driver), "work"]
        with mock.patch.object(telemetry, "_process_table", return_value={111: (1000.0, argv), 222: (1000.0, argv)}):
            with mock.patch.object(telemetry, "_router_openers",
                                   return_value=[(111, "u", owner), (111, "r", lock), (222, "u", owner)]):
                self.assertEqual(telemetry._router_owner_process(1001.0), (222, 1000.0))
            with mock.patch.object(telemetry, "_router_openers", return_value=[(111, "u", owner), (222, "u", owner)]):
                self.assertIsNone(telemetry._router_owner_process(1001.0))

    def test_p2conv_run_attribution_needs_the_work_argv_and_the_start_window(self):
        """Reviewer mutants R5, R6: a gate or any other opener of a run's lock, or a work process
        that started outside the window of the run's record, is never 'its work process'."""
        lock_name = str(telemetry._real_root(self.root) / "locks" / "run-a.lock")
        argv = ["/usr/bin/python3", "-I", "-B", str(self.driver), "work"]
        for table, expected in (({111: (999.0, argv)}, 111),
                                ({111: (999.0, ["/usr/bin/python3", "-I", "-B", "gate.py", "work"])}, None),
                                ({111: (1000.0 - 700, argv)}, None),
                                ({111: (1005.0, argv)}, None)):
            with self.subTest(table=table), \
                 mock.patch.object(telemetry, "_router_openers", return_value=[(111, "r", lock_name)]), \
                 mock.patch.object(telemetry, "_process_table", return_value=table):
                row = {"runId": "run-a", "live": True, "layout": "per-run", "startedUnix": 1000.0, "note": None,
                       "_notePid": None, "pid": None}
                telemetry._run_owners(self.root, [row])
                self.assertEqual(row["pid"], expected)

    def test_p2conv_a_lock_path_replaced_during_the_probe_is_unknown(self):
        """Reviewer mutant R7: the probe re-checks the lock path after flock; another file there by
        then means the result describes nothing: 'unknown'."""
        locks = self.root / "locks"
        os.makedirs(locks, mode=0o700)
        _private_write(locks / "a.lock", "")
        _private_write(locks / "b.lock", "")
        self.assertEqual(telemetry._probe_lock(locks / "a.lock"), "free")
        real, other = Path.lstat, (locks / "b.lock").lstat()

        def lstat(path, *args, **kwargs):
            return other if path == locks / "a.lock" else real(path, *args, **kwargs)

        with mock.patch.object(Path, "lstat", lstat):
            self.assertEqual(telemetry._probe_lock(locks / "a.lock"), "unknown")

    def test_p2conv_unsafe_run_directories_are_an_error_in_telemetry_and_activity(self):
        """Reviewer mutant R8 and Sol N6: active/, notes/ or locks/ with group or other bits (or
        another owner) is an observation error; activity no longer returns an empty listing that
        reads "No unresolved router pointer"."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-a", "a" * 64, stage="backend_draft", keep=False)
        self.assertIsNone(self.observe()[0]["error"])
        for name, bits in (("active", 0o750), ("notes", 0o701), ("locks", 0o770)):
            with self.subTest(directory=name):
                os.chmod(self.root / name, bits)
                try:
                    router = telemetry._router_observation(time.time())
                    self.assertEqual(router["error"], "Router run directories are unsafe or unreadable")
                    if name == "active":
                        source = self.activity_runs(router)["sources"][0]
                        self.assertEqual(source["state"], "error")
                        self.assertIn("unsafe", source["detail"])
                        self.assertNotIn("No unresolved router pointer", source["detail"])
                finally:
                    os.chmod(self.root / name, 0o700)
        with mock.patch.object(telemetry.os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(telemetry._router_listing(self.root / "active", ".json")[2], "unsafe")
        self.assertIsNone(telemetry._router_observation(time.time())["error"])

    def test_p2conv_legacy_archived_uncleared_and_invalid_timing(self):
        """Reviewer mutants R10, R13: a legacy record whose archive exists is archived-uncleared;
        a legacy record whose start is after its checkpoint is never live, even with owner.lock
        held EX and a matching owner process."""
        legacy, _ = self.writer("legacy")
        with legacy["RouterOwner"](self.root) as owner:
            owner.begin("legacy-c10", "a" * 64)
            owner.checkpoint("final_validation_intent", {"selectedHost": "mac"})
        os.makedirs(self.root / "archive", mode=0o700, exist_ok=True)
        _private_write(self.root / "archive" / "legacy-c10.json", "{}")
        self.assertEqual(self.row(self.observe()[0], "legacy-c10")["state"], "archived-uncleared")
        os.unlink(self.root / "archive" / "legacy-c10.json")
        self.rewrite(self.root / "active.json",
                     lambda value: value.update(startedUnix=value["checkpoint"]["recordedUnix"] + 50))
        started = json.loads((self.root / "active.json").read_text())["startedUnix"]
        fd = os.open(str(self.root / "owner.lock"), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.object(telemetry, "_router_owner_process", return_value=(4242, started - 1)):
                router, mode, _pipeline, _source = self.observe()
        finally:
            os.close(fd)
        row = self.row(router, "legacy-c10")
        self.assertEqual((row["live"], row["state"]), (False, "unresolved"))
        self.assertIn("invalid timing", row["evidence"])
        self.assertEqual(mode["taskState"], "unfinished")

    def test_p2conv_a_marker_with_any_other_key_set_is_never_attributed(self):
        """Reviewer mutant R14: only the exact 5-key marker is attributed to a live run."""
        state = self.per_run_layout(policy="multi")
        self.run_in_process(state, "run-x", "a" * 64, stage="backend_draft")
        good = {"kind": "codemode.nisi.pending.v1", "started_unix": time.time() - 5, "input_sha256": "f" * 64,
                "runId": "run-x.draft", "operation": "draft"}
        self.rewrite(self.pending, raw=json.dumps(good))
        self.assertEqual(self.observe()[2]["pendingMarkerAttributedTo"], "run-x")
        self.rewrite(self.pending, raw=json.dumps(dict(good, extra=1)))
        pipeline = self.observe()[2]
        self.assertIsNone(pipeline["pendingMarkerAttributedTo"])
        self.assertEqual((pipeline["recoveryRequired"], pipeline["pendingMarkerForeign"]), (True, True))

    def test_p2conv_an_unreadable_install_fence_is_invalid_and_never_proves_a_legacy_route(self):
        """Reviewer mutant R15: a fence the router cannot read (bad JSON, mode 0644, a second link)
        is 'invalid', like one outside its schema: every entrypoint refuses FENCE_INVALID, so a
        legacy record under it is never live and admission is not the single-run router."""
        self.per_run_layout(policy="single")
        fence = self.root / "install-fence.json"
        good = fence.read_text()
        for label in ("json", "mode", "links"):
            with self.subTest(fence=label):
                os.unlink(fence)
                _private_write(fence, "{not json" if label == "json" else good, mode=0o644 if label == "mode" else 0o600)
                if label == "links":
                    os.link(fence, self.base / "fence-second-link")
                try:
                    with mock.patch.object(telemetry.time, "sleep"):
                        self.assertEqual(telemetry._router_install_state(self.root)["state"], "invalid")
                finally:
                    if label == "links":
                        os.unlink(self.base / "fence-second-link")
        os.unlink(fence)
        _private_write(fence, "{not json")
        with self.holder("legacy", "legacy-live", "a" * 64, stage="backend_draft"):
            router, mode, _pipeline, _source = self.observe()
        self.assertEqual((self.row(router, "legacy-live")["live"], mode["admission"]["source"]),
                         (False, "fence-invalid"))

    def test_p2conv_a_note_or_run_lock_that_is_not_private_proves_nothing(self):
        """Reviewer mutants R16, R17: a stale note whose lock file is not a private regular file
        (0644 here, even held) gives no row; a run's lock file that is unsafe or could not be probed
        makes its row 'unverified', never 'unresolved' (liveness cannot be proven)."""
        state = self.per_run_layout(policy="multi")
        ghost = state["RouterOwner"](self.root)
        ghost.__enter__()
        ghost.claim("run-s", "b" * 64, until=time.monotonic() + 2)
        ghost.note("waiting", "mac-pair", "pre-begin", time.monotonic() + 30)
        ghost._l1, l1 = None, ghost._l1              # keep its note on disk, then release its locks
        os.close(l1)
        ghost.__exit__(None, None, None)
        lock = self.root / "locks" / "run-s.lock"
        os.unlink(lock)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(telemetry._probe_lock(lock), "unsafe")
            self.assertEqual(self.observe()[0]["rows"], [])
        finally:
            os.close(fd)
        self.run_in_process(state, "run-u", "a" * 64, stage="backend_draft", keep=False)
        os.chmod(self.root / "locks" / "run-u.lock", 0o644)
        row = self.row(self.observe()[0], "run-u")
        self.assertEqual((row["lock"], row["state"], row["live"]), ("unsafe", "unverified", False))
        with mock.patch.object(telemetry, "_probe_lock", return_value="unknown"):
            row = self.row(telemetry._router_observation(time.time()), "run-u")
        self.assertEqual((row["lock"], row["state"]), ("unknown", "unverified"))


if __name__ == "__main__":
    unittest.main()
