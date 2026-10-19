"""Sol round-seven regressions. All inputs are local fixtures; no live transport."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect, linsys, probes
from test_linsys import Fixture
from test_coordinator_units import ABSENT, unit, V8_RUNTIME, V8_SHA
from test_probes import listener


class CoordinatorCandidates(Fixture):
    def test_extra_interpreter_flags_remain_unverified_candidates(self):
        script = f"/home/fixture/.local/share/agiw-runtimes/{V8_RUNTIME}/coordinator_v8.py"
        self.process(202, "python3", ["-I", "-B", script, "serve", "--release-sha256=" + V8_SHA],
                     exe="/usr/bin/python3")
        processes = linsys.read_processes(self.root)
        self.assertTrue(processes[0].get("coordinator"))
        self.assertNotIn("coordinatorRuntime", processes[0])
        for active in (False, True):
            with self.subTest(active=active):
                observed = list(processes)
                if active:
                    observed.append({"pid": 101, "coordinator": True, "coordinatorRuntime": V8_RUNTIME,
                                     "coordinatorReleaseSha256": V8_SHA})
                state = probes.coordinator_status(observed, units={"user": ABSENT,
                    "system": unit(active="active", sub="running", enabled="enabled") if active else ABSENT})
                self.assertEqual((state["state"], state["health"]), ("unknown", "unknown"))
                self.assertEqual(state["reason"], "process-unit-state-conflict")
                self.assertTrue(state["processPresent"])


class ProjectionAndAllowance(unittest.TestCase):
    def test_named_wrapper_failure_survives_ssh_projection(self):
        snapshot = collect.unknown_snapshot(now=100, host="fixture")
        snapshot["lanes"] = [{"id": "gpu-wrapper-101", "status": "unreachable", "health": "unknown",
                              "reason": "timeout", "endpoint": "private", "prompt": "private"}]
        projected = collect.project_status(snapshot)
        self.assertEqual(projected.get("lanes"), [{"id": "gpu-wrapper-101", "status": "unreachable",
            "health": "unknown", "reason": "timeout", "activity": "unknown", "slotsBusy": None,
            "slotsTotal": None, "liveTokensPerSecond": None}])
        self.assertNotIn("private", json.dumps(projected))

    def test_lane_projection_rejects_private_reasons_and_contradictory_health(self):
        snapshot = collect.unknown_snapshot(now=100)
        for status, health in (("unreachable", "up"), ("unknown", "down")):
            snapshot["lanes"] = [{"id": "gpu-wrapper-101", "status": status,
                                  "health": health, "reason": "fixture-secret"}]
            row = collect.project_status(snapshot)["lanes"][0]
            self.assertEqual(row["health"], "unknown")
            self.assertEqual(row["reason"], "not-observed")

    def test_busy_slots_allowance_accepts_half_second_response(self):
        process = {"pid": 101, "exe": probes.WRAPPER_EXE, "startTimeTicks": 1234, "port": 49152,
                   "flags": {"host": "127.0.0.1", "alias": "fixture-model"}}
        timeouts = {}
        def fetch(url, timeout, cap):
            timeouts[url.rsplit("/", 1)[-1]] = timeout
            if url.endswith("/slots"):
                if timeout < .5:
                    raise TimeoutError("fixture response takes .5 seconds")
                return [{"id": 0, "id_task": 9, "is_processing": True, "n_decoded": 10}]
            return {"status": "ok"} if url.endswith("/health") else {"data": [{"id": "fixture-model"}]}
        row = probes.probe_wrapper([process], fetch, network=listener(process, 49152))[0]
        self.assertEqual((row["status"], row["phase"], row["slotsBusy"]), ("busy", "generating", 1))
        self.assertGreaterEqual(timeouts["slots"], .5)
        self.assertLessEqual(timeouts["slots"], 1.0)
        self.assertLessEqual(timeouts["health"], .35)
        self.assertLessEqual(timeouts["models"], .35)

    def test_safe_space_bearing_caller_name_is_retained(self):
        result = collect.network_status({"state": "up", "listeners": [], "callersState": "known",
            "callers": [{"name": "Claude Code", "lane": "gpu-wrapper", "connections": 1}]})
        self.assertEqual(result["callers"][0]["name"], "Claude Code")
        self.assertEqual(result["callersState"], "known")
