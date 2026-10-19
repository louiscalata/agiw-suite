"""Fix pass 7: enrollment trust, process sampling and incomplete collector evidence."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import buildinfo, collect, probes, winsys
from agiw_win.server import SnapshotStore
from tests import test_collector_safety as safety
_import_path = sys.path[:]
try:
    sys.path.append(str(Path(__file__).resolve().parents[2]))
    import peer_feed
finally:
    sys.path[:] = _import_path


class EnrollmentPermissions(unittest.TestCase):
    def test_system_profile_with_read_only_capability_is_accepted(self):
        winsys._check_enrollment_permissions("S-1-5-18", [("S-1-15-3-65536-123", 0x100020, False)], "user", True)

    def test_untrusted_owner_or_writers_are_refused(self):
        for owner, entries in (("other", []), ("user", None),
                               ("user", [("other", 2, False)]), ("user", [("other", 2, True)])):
            with self.subTest(owner=owner, entries=entries), self.assertRaises(PermissionError):
                winsys._check_enrollment_permissions(owner, entries, "user", True)

    def test_creator_owner_and_owner_rights_only_follow_trusted_ownership(self):
        for sid in ("S-1-3-4", "S-1-3-0"):
            winsys._check_enrollment_permissions("user", [(sid, 0x1f01ff, sid == "S-1-3-0")], "user", True)
            with self.assertRaises(PermissionError):
                winsys._check_enrollment_permissions("other", [(sid, 0x1f01ff, True)], "user", True)

    def test_ancestors_allow_siblings_but_not_replacing_the_path(self):
        winsys._check_enrollment_permissions("S-1-5-18", [("other", 2, False)], "user", False)
        with self.assertRaises(PermissionError):
            winsys._check_enrollment_permissions("S-1-5-18", [("other", 0x40, False)], "user", False)

    def test_enrollment_refusal_is_logged_without_silently_parking(self):
        with mock.patch.object(winsys, "_read_local_enrollment", side_effect=PermissionError("parent writable")), \
                self.assertLogs("agiw_win.winsys", level="WARNING") as log:
            self.assertEqual(winsys.local_lane_identities(Path("unused")), {})
        self.assertIn("parent writable", log.output[0])

    def test_build_and_enrollment_use_strict_json_for_ignored_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build.json"
            for ignored in ("[" * 65 + "0" + "]" * 65, "1e400"):
                with self.subTest(ignored=ignored[:10]):
                    path.write_text('{"extra":' + ignored + '}')
                    with self.assertRaises(ValueError):
                        buildinfo._read(path)
            path.write_text('{"schemaVersion":1,"schemaVersion":1}')
            with self.assertRaises(ValueError):
                buildinfo._read(path)
        raw = '{"schema":1,"lanes":{},"ignored":' + '[' * 65 + '0' + ']' * 65 + '}'
        with mock.patch.object(winsys, "_read_local_enrollment", return_value=raw), \
                self.assertLogs("agiw_win.winsys", level="WARNING") as log:
            self.assertEqual(winsys.local_lane_identities(Path("unused")), {})
        self.assertIn("nested too deeply", log.output[0])


def tcp(state, local, port, remote, remote_port, pid):
    return {"state": state, "local": local, "localPort": port, "remote": remote, "remotePort": remote_port, "pid": pid}


class OwnershipRechecks(unittest.TestCase):
    def test_ipv4_http_owner_is_not_the_ipv6_loopback_listener(self):
        rows = [tcp(winsys.MIB_TCP_STATE_LISTEN, "::1", 1235, "::", 0, 200),
                tcp(winsys.MIB_TCP_STATE_LISTEN, "0.0.0.0", 1235, "0.0.0.0", 0, 100)]
        lp = winsys.LaneProcesses(table_fn=lambda: rows, info_fn=lambda pid: {
            "pid": pid, "name": "llama-server.exe", "created": 1, "cpuTime": 0, "workingSetBytes": 1})
        self.assertEqual(lp.sample({1235: "fast"})[0]["fast"]["pid"], 100)
        rows.pop()
        self.assertEqual(lp.sample({1235: "fast"})[0], {})

    def test_endpoint_rechecks_do_not_reset_cpu_or_other_lane_callers(self):
        now, cpu = [100.0], [0]
        rows = [tcp(2, "127.0.0.1", 1235, "0.0.0.0", 0, 10), tcp(2, "127.0.0.1", 1236, "0.0.0.0", 0, 20),
                tcp(5, "127.0.0.1", 1236, "127.0.0.1", 50000, 20), tcp(5, "127.0.0.1", 50000, "127.0.0.1", 1236, 30)]
        lp = winsys.LaneProcesses(table_fn=lambda: rows, clock=lambda: now[0], cpus=1,
                                 info_fn=lambda pid: {"pid": pid, "name": "llama-server.exe", "created": 1,
                                                      "cpuTime": cpu[0], "workingSetBytes": 1})
        ports = {1235: "fast", 1236: "deep"}
        lp.sample(ports)
        before, callers_before = dict(lp._previous), dict(lp._first_seen)
        now[0], cpu[0] = 101.9, 10_000_000
        lp.ownership({1235: "fast"})
        self.assertEqual((lp._previous, lp._first_seen), (before, callers_before))
        now[0] = 102.0
        servers, callers, _ = lp.sample(ports)
        self.assertEqual(servers["fast"]["cpuPercent"], 50.0)
        self.assertEqual(callers[0]["connectedSeconds"], 2.0)


class CollectorEvidence(unittest.TestCase):
    setUp = safety.CollectorSafety.setUp
    collector = safety.CollectorSafety.collector
    configure = safety.CollectorSafety.configure
    processes = staticmethod(safety.CollectorSafety.processes)

    def test_undeclared_lanes_make_callers_and_gpu_attribution_unknown(self):
        for declared in (False, True):
            with self.subTest(one_lane_declared=declared):
                if declared:
                    self.configure()
                processes = self.processes()
                if not declared:
                    processes.sample.return_value = ({}, [], dict(safety.LIVE))
                snapshot = self.collector(processes=processes, fetch=safety.CollectorSafety.fetch,
                    adapters=[safety.ADAPTERS[0]], gpus=[{**safety.NVIDIA, "utilizationPercent": 92.0,
                        "engineShares": [{"pid": 77, "percent": 92.0}], "engineSharesComplete": True}]).collect()
                self.assertIsNone(snapshot["localCallers"])
                self.assertTrue(all(lane["callers"] is None for lane in snapshot["lanes"]))
                self.assertEqual(snapshot["handsOff"]["cause"], "unknown")
                self.assertIsNone(snapshot["handsOff"]["agiwOnly"])

    def test_lane_inventory_outlives_activity_until_its_probe_deadline(self):
        self.configure()
        collector = self.collector(processes=self.processes())
        collector._lane_ownership["fast"] = "allowed"
        collector._lane_owners["fast"] = {"pid": 77, "created": 1}
        lane = probes.probe_lane(safety.FAST, safety.CollectorSafety.fetch)
        lane["liveTokensPerSecond"] = 25.0
        with mock.patch.object(collect, "LanePoller") as poller:
            poller.return_value.read.return_value = (1000.0, lane)
            self.assertEqual(collector._lane(safety.FAST, 1005.0)["liveTokensPerSecond"], 25.0)
            self.assertIsNone(collector._lane(safety.FAST, 1005.001)["liveTokensPerSecond"])
            self.assertEqual(collector._lane(safety.FAST, 1008.0)["status"], "slots_unknown")
            self.assertIsNone(collector._lane(safety.FAST, 1008.0)["liveTokensPerSecond"])
            published = probes.pc_status({"lanes": [collector._lane(safety.FAST, 1008.0)]})
            self.assertIsNone(published["models"][0]["liveTokensPerSecond"])
            self.assertEqual(collector._lane(safety.FAST, 1020.0)["status"], "stale")

    def test_snapshot_inventory_history_outlives_activity(self):
        store = SnapshotStore()
        data = {"sampledAt": 1000.0, "sources": [{"id": "llama-slots", "state": "live"}], "models": [
            {"host": "windows", "id": "fast", "name": "Fast", "source": "llama-slots", "state": "idle",
             "loaded": True, "ageSeconds": 8.0}]}
        store.publish(data)
        self.assertIs(store.read()["activityKnown"], False)
        self.assertEqual(store.read()["history"][-1]["loaded"], 1)

    def test_peer_refusal_and_config_failure_reach_mac_presence(self):
        source = {"reason": "report-refused", "refusalHosts": {"mac": "clock-ahead"}, "detail": "clock-ahead: 1"}
        self.assertEqual(collect.mac_from_presence([], "mac.local", peers_source=source)["reason"], "clock-ahead")
        self.assertEqual(collect.mac_from_presence([], None, config_reason="config-unreadable")["reason"], "config-unreadable")
        self.assertEqual(collect.mac_from_presence([], "mac", peers_source={"reason": "no-report"})["reason"], "no-report")

    def test_undeclared_inventory_stays_incomplete_through_mac_summary(self):
        snapshot = self.collector(processes=self.processes()).collect()
        status = probes.pc_status(snapshot)
        self.assertEqual(status["models"], [])
        self.assertIs(status["modelsComplete"], False)
        summary = peer_feed._summary(status, "share", snapshot["sampledAt"])
        self.assertIs(summary["modelsComplete"], False)
        presence = probes.peer_presence(snapshot, "test-pc")
        self.assertEqual(presence["modelsTotal"], 2)
        self.assertIs(peer_feed._summary(presence, "share", snapshot["sampledAt"])["modelsComplete"], False)

    @unittest.skipUnless(shutil.which("node"), "Node required for the Python-to-dashboard round trip")
    def test_undeclared_inventory_round_trip_never_renders_no_models(self):
        snapshot = self.collector(processes=self.processes()).collect()
        summary = peer_feed._summary(probes.pc_status(snapshot), "share", snapshot["sampledAt"])
        module = (Path(__file__).resolve().parents[2] / "web/map-layout.mjs").as_uri()
        script = ("import {pcReportView} from " + json.dumps(module) + ";"
                  "let raw='';for await(const chunk of process.stdin)raw+=chunk;"
                  "process.stdout.write(JSON.stringify(pcReportView(JSON.parse(raw),{feedFresh:true})));" )
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", script],
                                input=json.dumps(summary), capture_output=True, text=True, check=True, timeout=10)
        view = json.loads(result.stdout)
        self.assertIn("unknown", view["words"].lower())
        self.assertNotIn("no model loaded", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
