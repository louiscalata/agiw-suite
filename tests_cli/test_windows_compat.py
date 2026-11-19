"""Windows compatibility of the fleet pilot and receipt checker, simulated on any host."""

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agiw import fleet_local_pilot as pilot
from agiw import windows_worker_receipt_check as rc
from agiw.fleet_routing_plan import WorkerEvidence


class PilotWindowsTests(unittest.TestCase):
    def test_module_imports_without_fcntl(self):
        path = Path(pilot.__file__)
        name = "agiw._fleet_local_pilot_nofcntl"
        spec = importlib.util.spec_from_file_location(name, str(path))
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "agiw"
        with mock.patch.dict(sys.modules, {"fcntl": None, "msvcrt": None}):
            spec.loader.exec_module(module)
        self.assertIsNone(module.fcntl)
        self.assertIsNone(module.msvcrt)
        with tempfile.TemporaryFile("a+") as handle:
            with self.assertRaises(module.Refused) as cm:
                module.try_lock(handle)
        self.assertEqual(str(cm.exception), "LOCK_UNAVAILABLE")

    def test_msvcrt_lock_used_when_fcntl_missing(self):
        calls = []
        fake = SimpleNamespace(LK_NBLCK=2, locking=lambda fd, mode, n: calls.append((mode, n)))
        with mock.patch.object(pilot, "fcntl", None), mock.patch.object(pilot, "msvcrt", fake):
            with tempfile.TemporaryFile("a+") as handle:
                self.assertEqual(pilot.try_lock(handle), "msvcrt")
        self.assertEqual(calls, [(2, 1)])

    def test_msvcrt_lock_contention_fails_closed(self):
        def locked(fd, mode, n):
            raise OSError(36, "Resource deadlock avoided")

        fake = SimpleNamespace(LK_NBLCK=2, locking=locked)
        with mock.patch.object(pilot, "fcntl", None), mock.patch.object(pilot, "msvcrt", fake):
            with tempfile.TemporaryFile("a+") as handle:
                with self.assertRaises(pilot.Refused) as cm:
                    pilot.try_lock(handle)
        self.assertEqual(str(cm.exception), "PREVIEW_ALREADY_RUNNING")

    def test_git_missing_fails_closed(self):
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(pilot.shutil, "which", return_value=None):
            os.environ.pop("AGIW_GIT", None)
            with self.assertRaises(pilot.Refused) as cm:
                pilot.integrity()
        self.assertEqual(str(cm.exception), "GIT_NOT_FOUND")

    def test_integrity_uses_discovered_windows_git_path(self):
        git = r"C:\Program Files\Git\cmd\git.exe"
        seen = []

        def check_output(argv, **kwargs):
            seen.append((argv, kwargs["env"]))
            return pilot.PIN + "\n" if "rev-parse" in argv else ""

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGIW_GIT", None)
            with (
                mock.patch.object(pilot.shutil, "which", side_effect=lambda t: git if t == "git" else None),
                mock.patch.object(pilot.subprocess, "check_output", side_effect=check_output),
                mock.patch.object(pilot.os, "name", "nt"),
            ):
                self.assertEqual(pilot.integrity(), pilot.PIN)
        self.assertEqual([argv[0] for argv, _ in seen], [git, git])
        self.assertEqual(seen[0][0][1:3], ["-c", "core.autocrlf=true"])
        self.assertEqual(seen[0][0][3:5], ["-C", str(pilot.PILOT / "vendor/nisi-0.3.1")])
        self.assertEqual(seen[0][1]["GIT_OPTIONAL_LOCKS"], "0")
        env = seen[0][1]
        self.assertNotIn("/usr/bin", env["PATH"])
        self.assertIn("SYSTEMROOT", env)
        self.assertIn(os.path.dirname(git), env["PATH"].split(os.pathsep))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)

    def test_posix_clean_env_unchanged(self):
        with mock.patch.object(pilot.os, "name", "posix"):
            self.assertEqual(pilot.clean_env()["PATH"], "/usr/bin:/bin:/usr/local/bin")

    def test_route_accepts_dual_gpu_pc(self):
        ev = WorkerEvidence(time.time(), True, True, True, False)
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(pilot.route(ev, worker_id="dual-gpu-pc")["worker"], "dual-gpu-pc")


class ReceiptCheckWindowsTests(unittest.TestCase):
    def stat(self, ctime):
        return SimpleNamespace(st_dev=1, st_ino=2, st_size=3, st_mtime_ns=4, st_ctime_ns=ctime)

    def test_ctime_ignored_on_windows_only(self):
        a, b = self.stat(10), self.stat(99)
        with mock.patch.object(rc.os, "name", "nt"):
            self.assertEqual(rc.identity(a), rc.identity(b))
        with mock.patch.object(rc.os, "name", "posix"):
            self.assertNotEqual(rc.identity(a), rc.identity(b))
        self.assertNotEqual(
            rc.identity(self.stat(1), windows=True),
            rc.identity(SimpleNamespace(st_dev=1, st_ino=2, st_size=4, st_mtime_ns=4, st_ctime_ns=1), windows=True),
        )

    def test_read_regular_tolerates_ctime_drift_on_windows(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "r.json"
            p.write_text("{}")
            real_fstat = os.fstat

            def drifting_fstat(fd):
                s = real_fstat(fd)
                return SimpleNamespace(
                    st_mode=s.st_mode,
                    st_dev=s.st_dev,
                    st_ino=s.st_ino,
                    st_size=s.st_size,
                    st_mtime_ns=s.st_mtime_ns,
                    st_ctime_ns=s.st_ctime_ns + 12345,
                )

            with mock.patch.object(rc.os, "fstat", side_effect=drifting_fstat):
                with mock.patch.object(rc.os, "name", "nt"):
                    self.assertEqual(rc.read_regular(str(p), rc.LIMIT), b"{}")
                with mock.patch.object(rc.os, "name", "posix"):
                    with self.assertRaises(ValueError):
                        rc.read_regular(str(p), rc.LIMIT)

    def job_receipt(self, **over):
        now = time.time()
        r = {
            "kind": rc.JOB_KIND,
            "schema_version": 2,
            "job_id": "job-1",
            "worker_id": "dual-gpu-pc",
            "workerCommit": None,
            "route": {"worker": "dual-gpu-pc", "preanalysis": "local", "reason": "strict-local"},
            "model": "m",
            "outcome": "COMPLETED",
            "reportStored": True,
            "runId": "0017efb7-de84-4fc5-95ba-3ea152847ffd",
            "reportSha256": "a" * 64,
            "reportPath": "C:/AGIW/report-store/x.json",
            "started_unix": now - 2,
            "finished_unix": now - 1,
        }
        r.update(over)
        return r

    def run_cli(self, receipt, *extra):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td).resolve() / "receipt.json"
            p.write_text(json.dumps(receipt))
            out = io.StringIO()
            with redirect_stdout(out):
                code = rc.main(["--mode", "job", "--receipt", str(p), "--expected-model", "m", *extra])
            return code, json.loads(out.getvalue())

    def test_cli_job_mode_valid(self):
        code, result = self.run_cli(self.job_receipt(), "--expected-worker", "dual-gpu-pc", "--job-id", "job-1")
        self.assertEqual(code, 0)
        self.assertTrue(result["valid"])
        self.assertEqual(result["report_path"], "C:/AGIW/report-store/x.json")

    def test_cli_job_mode_rejects_wrong_worker_and_unstored_report(self):
        code, result = self.run_cli(self.job_receipt(), "--expected-worker", "m5-macbook-pro")
        self.assertEqual((code, result["reason"]), (1, "worker-mismatch"))
        code, result = self.run_cli(self.job_receipt(reportStored=False), "--expected-worker", "dual-gpu-pc")
        self.assertEqual((code, result["reason"]), (1, "report-not-stored"))

    def test_cli_job_mode_requires_expected_worker(self):
        with (
            self.assertRaises(SystemExit) as cm,
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            self.run_cli(self.job_receipt())
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
