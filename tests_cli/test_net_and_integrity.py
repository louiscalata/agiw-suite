"""Node network permission (live only, guarded in-process) and a strict, race-free Nisi pin check."""

import json
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agiw import fleet_jev
from agiw import fleet_local_pilot as pilot
from agiw.windows_worker import Worker

ENDPOINT = "http://127.0.0.1:1237/v1/chat/completions"
VENDOR = pilot.PILOT / "vendor/nisi-0.3.1"


class NetFlagTests(unittest.TestCase):
    def test_flags_by_node_major_and_mode(self):
        self.assertEqual(pilot.net_flags(26, "live"), ["--allow-net"])
        self.assertEqual(pilot.net_flags(25, "live"), ["--allow-net"])
        self.assertEqual(pilot.net_flags(26, "fixture"), [])
        self.assertEqual(pilot.net_flags(24, "live"), [])
        with self.assertRaises(pilot.Refused):
            pilot.net_flags(20, "live")

    def test_node_major_parsing_fails_closed(self):
        pilot._NODE_MAJOR.clear()
        with mock.patch.object(pilot.subprocess, "check_output", return_value="v26.13.0\n"):
            self.assertEqual(pilot.node_major("/fake/node-a"), 26)
        with mock.patch.object(pilot.subprocess, "check_output", return_value="garbage"):
            with self.assertRaises(pilot.Refused):
                pilot.node_major("/fake/node-b")
        with mock.patch.object(pilot.subprocess, "check_output", side_effect=OSError):
            with self.assertRaises(pilot.Refused):
                pilot.node_major("/fake/node-c")
        pilot._NODE_MAJOR.clear()

    def run_execute(self, mode, major, route=None, child=None):
        seen = {}

        def run(cmd, input=None, **kw):
            seen["cmd"] = cmd
            if route is not None:
                self.assertEqual(json.loads(input)["route"], route)
            output = child or {"report": {"outcome": "BLOCKED"}}
            return SimpleNamespace(stdout=json.dumps(output), stderr="", returncode=1)

        ev = pilot.WorkerEvidence(0, True, True, True, False)
        with (
            mock.patch.object(pilot, "integrity", return_value=pilot.PIN),
            mock.patch.object(pilot, "node_binary", return_value="node"),
            mock.patch.object(pilot, "node_major", return_value=major),
            mock.patch.object(pilot, "readiness", return_value=ev),
            mock.patch.object(pilot.time, "time", return_value=1.0),
            mock.patch.object(pilot.subprocess, "run", side_effect=run),
        ):
            with tempfile.TemporaryDirectory() as td:
                data = pilot.execute(mode, ENDPOINT, "m", report_store=td, route=route)
        return seen["cmd"], data

    def test_judged_route_reaches_child_cfg(self):
        answers = {"plan": {"choice": "reason", "confidence": 1}, "underspecified": {"noul": 0}}
        judge = mock.Mock(return_value={"model": "jev-test", "answers": answers})
        jev = fleet_jev.preanalyze("nisi-smoke.mjs", judge)
        jev.pop("status")
        route = {"worker": "m5-macbook-pro", "reason": "jev-preanalyzed", "preanalysis": "jev", "jev": jev}
        plan = {k: jev[k] for k in ("plan", "confidence", "underspecified", "jevModel")}
        plan["requestSha256"] = "a" * 64
        child = {"report": {"outcome": "BLOCKED"}, "jevPlan": dict(plan, extra="discard")}

        def execute(mode, *args, **kwargs):
            self.assertEqual(kwargs["route"], route)
            return self.run_execute(mode, 26, kwargs["route"], child)[1]

        with tempfile.TemporaryDirectory() as td:
            worker = Worker(route["worker"], "m", ENDPOINT, td, execute=execute, commit=lambda: None)
            receipt = worker.run_job({"job_id": "test", "program": "nisi-smoke.mjs", "route": route})
            saved = json.loads(Path(receipt["receiptPath"]).read_text())
            self.assertEqual(saved["jevPlan"], plan)

        child = {"report": None, "code": "JEV_PLAN_REFUSED"}
        _, data = self.run_execute("live", 26, route, child)
        self.assertEqual(data["report"], {"outcome": "BLOCKED", "code": "JEV_PLAN_REFUSED"})
        for bad in (None, dict(plan, confidence=True), dict(plan, requestSha256="invalid")):
            with self.subTest(bad=bad), self.assertRaisesRegex(pilot.Refused, "^JEV_PLAN_INVALID$"):
                self.run_execute("live", 26, route, {"report": {}, "jevPlan": bad})

    def test_live_on_node26_gets_bare_allow_net_and_guard(self):
        cmd, data = self.run_execute("live", 26)
        self.assertIn("--allow-net", cmd)
        self.assertFalse(any(a.startswith("--allow-net=") for a in cmd))
        self.assertEqual(data["netPermission"], "allow-net+guard:127.0.0.1:1237")

    def test_fixture_and_node24_get_no_net_flag(self):
        self.assertNotIn("--allow-net", self.run_execute("fixture", 26)[0])
        cmd, data = self.run_execute("live", 24)
        self.assertNotIn("--allow-net", cmd)
        self.assertEqual(data["netPermission"], "none+guard:127.0.0.1:1237")


class IntegrityTests(unittest.TestCase):
    def fake_git(self, head=None, status="", fail=False):
        calls = []

        def check_output(argv, **kw):
            calls.append((argv, kw["env"]))
            if fail:
                raise subprocess.CalledProcessError(128, argv, stderr="fatal: detected dubious ownership in /x/y")
            return (head or pilot.PIN) + "\n" if "rev-parse" in argv else status

        return calls, check_output

    def check(self, **kw):
        calls, fake = self.fake_git(**kw)
        with (
            mock.patch.object(pilot, "git_binary", return_value="git"),
            mock.patch.object(pilot.subprocess, "check_output", side_effect=fake),
            mock.patch.object(pilot.os, "name", "posix"),
        ):
            try:
                return pilot.integrity(), calls
            except pilot.Refused as exc:
                return str(exc), calls

    def test_clean_pin_passes_without_index_locks(self):
        result, calls = self.check()
        self.assertEqual(result, pilot.PIN)
        self.assertEqual(calls[1][1]["GIT_OPTIONAL_LOCKS"], "0")
        self.assertIn("--untracked-files=all", calls[1][0])
        self.assertNotIn("core.autocrlf=true", calls[1][0])

    def test_real_dirt_is_refused_with_entries(self):
        result, _ = self.check(status=" M workflow/engine.mjs\n?? stray.txt\n?? a\n?? b")
        self.assertTrue(result.startswith("NISI_CHECKOUT_DIRTY: "))
        self.assertIn("workflow/engine.mjs", result)
        self.assertIn("(+1 more)", result)

    def test_pin_mismatch_and_git_failure_are_distinct(self):
        self.assertEqual(self.check(head="0" * 40)[0], "NISI_PIN_MISMATCH")
        result = self.check(fail=True)[0]
        self.assertTrue(result.startswith("NISI_GIT_FAILED: "))
        self.assertNotIn("/x/y", result)

    def test_integrity_runs_only_under_the_pilot_lock(self):
        with (
            mock.patch.object(pilot, "try_lock", side_effect=pilot.Refused("PREVIEW_ALREADY_RUNNING")),
            mock.patch.object(pilot, "integrity") as integrity,
            mock.patch.object(pilot, "node_binary", return_value="node"),
            mock.patch.object(pilot, "node_major", return_value=26),
        ):
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(pilot.Refused):
                    pilot.execute("fixture", ENDPOINT, "fixture-only", report_store=td)
        integrity.assert_not_called()

    @unittest.skipUnless((VENDOR / ".git").exists(), "pinned Nisi checkout missing")
    def test_real_checkout_clean_then_untracked_file_refused(self):
        self.assertEqual(pilot.integrity(), pilot.PIN)
        stray = VENDOR / (f"stray-{uuid.uuid4().hex}.tmp")
        stray.write_text("x")
        try:
            with self.assertRaises(pilot.Refused) as cm:
                pilot.integrity()
            self.assertTrue(str(cm.exception).startswith("NISI_CHECKOUT_DIRTY"))
        finally:
            stray.unlink()
        self.assertEqual(pilot.integrity(), pilot.PIN)


if __name__ == "__main__":
    unittest.main()
