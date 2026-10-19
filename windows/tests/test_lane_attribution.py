"""PLAN A3: lanes fail closed, only lane-port owners are the "Local LLM server", and PAIR is never an AGIW lane."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, winsys  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402

GIB = 2 ** 30
LISTEN, ESTAB = winsys.MIB_TCP_STATE_LISTEN, winsys.MIB_TCP_STATE_ESTAB
PAIR_ENGINE = r"C:\Users\x\AppData\Local\Nvidia Corporation\Personal AI Router\engine-bin\llamacpp\llama-server.exe"
LANE_SERVER = r"C:\Users\x\.lmstudio\extensions\backends\llama.cpp-win-x86_64-nvidia-cuda12-avx2-2.31.2\llama-server.exe"


def lane_fetch(model):
    def fetch(url, _timeout, _limit):
        if url.endswith("/health"):
            return {"status": "ok"}
        if url.endswith("/v1/models"):
            return {"data": [{"id": model, "meta": {"size": 5 * GIB}}]}
        if url.endswith("/slots"):
            return [{"id": 0, "is_processing": False}]
        raise AssertionError(url)
    return fetch


class FailClosedLanes(unittest.TestCase):
    def test_unconfigured_lanes_are_never_probed_and_never_down(self):
        fetch = mock.Mock(side_effect=AssertionError("an unconfigured lane must not be probed"))
        lanes = [probes.probe_lane(probes.unconfigured_lane(lane_id), fetch) for lane_id in probes.LANE_IDS]
        fetch.assert_not_called()
        self.assertEqual([(lane["status"], lane["port"], lane["endpoint"]) for lane in lanes],
                         [("undeclared", None, None), ("undeclared", None, None)])
        # Unknown is never published as down.
        self.assertEqual(probes.lanes_health(lanes), "unknown")
        status = probes.pc_status({"observedAt": "2026-10-17T00:00:00Z", "observerVersion": "0.2.0", "lanes": lanes})
        probes.validate_status(status)
        self.assertEqual(status["health"], "unknown")
        self.assertEqual(status["models"], [])
        self.assertIn("lanes not configured: fast, deep", status["limitations"])

    def test_a_port_is_never_invented_for_a_lane(self):
        # No code path may fall back to PAIR's :1234 (or any port) when the config is unusable.
        for config in (None, "{", [], {"runtime": {"lanes": {}}}, {"runtime": {"lanes": {"fast": {"port": "x", "alias": "a"}}}}):
            with self.subTest(config=config), tempfile.TemporaryDirectory() as tmp:
                if config is not None:
                    path = Path(tmp) / "windows-llm-pipeline"
                    path.mkdir()
                    (path / "config.json").write_text(config if isinstance(config, str) else json.dumps(config))
                specs, source = probes.lane_specs(Path(tmp))
                self.assertEqual([spec["port"] for spec in specs], [None, None])
                self.assertTrue(all(spec["undeclared"] for spec in specs))
                self.assertIn(source["state"], ("unavailable", "error"))

    def test_collector_fallback_after_a_crashing_config_read_also_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            processes = mock.Mock()
            processes.sample.return_value = ({}, [], {"id": "lane-processes", "state": "live", "detail": "x"})
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: 1000.0,
                                  fetch=mock.Mock(side_effect=AssertionError("no lane may be probed")),
                                  gpu_fn=lambda: ([], None), adapters_fn=lambda: [], lane_processes=processes,
                                  memory_fn=lambda: ({"level": "unknown"}, None), notification_fn=lambda: None)
            self.addCleanup(collector.close)
            with mock.patch.object(probes, "lane_specs", side_effect=RuntimeError("boom")):
                snapshot = collector.collect()
        self.assertEqual([lane["status"] for lane in snapshot["lanes"]], ["undeclared", "undeclared"])
        processes.sample.assert_called_once_with({})
        self.assertEqual(snapshot["machines"][0]["state"], "unknown")
        self.assertEqual(snapshot["machines"][0]["reason"], "lane-state-unknown")


class LanesHealth(unittest.TestCase):
    def test_down_needs_affirmative_non_serving_evidence(self):
        cases = {("idle", "busy"): "ok", ("slots_unknown", "idle"): "ok", ("idle", "unreachable"): "unknown",
                 ("unreachable", "unreachable"): "unknown", ("unreachable", "stale"): "unknown",
                 ("unreachable", "undeclared"): "unknown", ("unknown", "unknown"): "unknown",
                 ("loading", "unreachable"): "unknown", ("identity_mismatch", "unreachable"): "unknown",
                 ("not_agiw", "unreachable"): "unknown", ("idle", "not_agiw"): "degraded",
                 ("loading", "loading"): "down", ("identity_mismatch", "not_agiw"): "down"}
        for statuses, expected in cases.items():
            with self.subTest(statuses=statuses):
                self.assertEqual(probes.lanes_health([{"status": s} for s in statuses]), expected)
        self.assertEqual(probes.lanes_health([]), "unknown")
        self.assertEqual(probes.lanes_health([{"status": None}, {"status": "unreachable"}]), "unknown")


class LanePidAttribution(unittest.TestCase):
    def test_only_lane_port_owners_form_the_local_llm_server(self):
        samples = [("llama-server.exe", 8 * GIB, 101, None),   # owns a lane port
                   ("llama-server.exe", 6 * GIB, 202, "pair"),  # PAIR's engine
                   ("llama-server.exe", 3 * GIB, 303, None),   # someone else's server
                   ("PAIR.exe", GIB, 404, "pair"), ("browser.exe", 2 * GIB, 505, None)]
        rows = probes.group_processes(samples, limit=8, lane_pids=frozenset({101}))
        by_label = {row["label"]: row for row in rows}
        self.assertEqual(by_label["Local LLM server"], {"label": "Local LLM server", "residentBytes": 8 * GIB,
                                                        "processCount": 1, "gpuAllocBytes": None, "lane": True})
        self.assertEqual(by_label["NVIDIA PAIR"], {"label": "NVIDIA PAIR", "residentBytes": 7 * GIB, "processCount": 2,
                                                   "gpuAllocBytes": None, "owner": "pair"})
        self.assertEqual(by_label["llama-server.exe"]["residentBytes"], 3 * GIB)
        self.assertNotIn("lane", by_label["llama-server.exe"])

    def test_without_lane_pids_nothing_is_the_lane_server(self):
        rows = probes.group_processes([("llama-server.exe", GIB, 7, None)], limit=3)
        self.assertEqual(rows, [{"label": "llama-server.exe", "residentBytes": GIB, "processCount": 1, "gpuAllocBytes": None}])
        block, _ = probes.memory_block(lambda: {"totalPhys": 100 * GIB, "availPhys": 8 * GIB}, lambda: rows,
                                       lanes=[{"status": "idle", "modelBytes": 5 * GIB}])
        self.assertEqual([row["label"] for row in block["consumers"]], ["llama-server.exe"])
        self.assertFalse(any("loadedModelCount" in row for row in block["consumers"]))

    def test_the_lane_group_is_kept_beyond_the_limit(self):
        samples = [(f"app-{i}.exe", (20 - i) * GIB, 1000 + i, None) for i in range(6)] + [("server.exe", GIB, 9, None)]
        rows = probes.group_processes(samples, limit=3, lane_pids=frozenset({9}))
        self.assertEqual([row["label"] for row in rows], ["app-0.exe", "app-1.exe", "app-2.exe", "Local LLM server"])

    def test_lane_server_pids_skip_pair_owned_missing_and_invalid_processes(self):
        lanes = [{"process": {"pid": 11, "owner": "agiw", "ownerKnown": True}}, {"process": {"pid": 12, "owner": "pair"}}, {"process": None}, {},
                 {"process": {"pid": True}}, {"process": {"pid": 4}}, {"process": {"pid": "13"}}, "lane",
                 {"process": {"pid": 14, "ownerKnown": False}}]
        self.assertEqual(probes.lane_server_pids(lanes), frozenset({11}))
        self.assertEqual(probes.lane_server_pids(None), frozenset())

    def test_top_processes_classifies_pair_by_path_and_keeps_only_names(self):
        # Forward slashes for the ordinary process so Path(...).name splits it on every OS the tests run on.
        paths = {1: LANE_SERVER, 2: PAIR_ENGINE, 3: r"C:\Program Files\PAIR\PAIR.exe", 4: "C:/Apps/game.exe"}

        def enumerate_processes(pids, _size, needed):
            for index, pid in enumerate(paths):
                pids._obj[index] = pid
            needed._obj.value = len(paths) * probes.ctypes.sizeof(probes.ctypes.c_ulong)
            return True

        def memory(handle, counters, _size):
            counters._obj.WorkingSetSize = handle * GIB
            return True

        def name(handle, _flags, buffer, _size):
            buffer.value = paths[handle]
            return True
        psapi = mock.Mock(EnumProcesses=enumerate_processes, GetProcessMemoryInfo=memory)
        kernel = mock.Mock(OpenProcess=lambda _query, _inherit, pid: pid, QueryFullProcessImageNameW=name)
        with mock.patch.object(probes, "IS_WINDOWS", True), \
                mock.patch.object(probes.ctypes, "windll", mock.Mock(psapi=psapi, kernel32=kernel), create=True):
            rows = probes.top_processes(limit=8, lane_pids=frozenset({1}))
        self.assertEqual({row["label"]: row["residentBytes"] for row in rows},
                         {"Local LLM server": GIB, "NVIDIA PAIR": 5 * GIB, "game.exe": 4 * GIB})
        self.assertNotIn("Personal AI Router", json.dumps(rows))
        self.assertNotIn("Program Files", json.dumps(rows))


class ImageOwner(unittest.TestCase):
    def test_pair_install_folders_and_names(self):
        for path in (PAIR_ENGINE, r"C:\Program Files\PAIR\PAIR.exe", r"c:\program files (x86)\pair\helper.exe",
                     r"D:\Tools\NVPAIR-PROXY.EXE", "C:/Users/x/AppData/Local/Nvidia Corporation/Personal AI Router/x.exe"):
            with self.subTest(path=path):
                self.assertEqual(winsys.image_owner(path), "pair")
        for path in (LANE_SERVER, r"C:\Games\pairs-solitaire.exe", r"C:\Program Files\PAIRING\x.exe", "", None, 7):
            with self.subTest(path=path):
                self.assertIsNone(winsys.image_owner(path))
        self.assertIs(probes.image_owner, winsys.image_owner)


def row(state, local, lport, remote, rport, pid):
    return {"state": state, "local": local, "localPort": lport, "remote": remote, "remotePort": rport, "pid": pid}


class PairOwnedLanePorts(unittest.TestCase):
    def test_lane_processes_carry_the_pair_owner(self):
        table = [row(LISTEN, "127.0.0.1", 1235, "0.0.0.0", 0, 11), row(LISTEN, "::", 1234, "::", 0, 22)]
        info = {11: {"pid": 11, "name": "llama-server.exe", "owner": None, "created": 1, "cpuTime": 1, "workingSetBytes": GIB},
                22: {"pid": 22, "name": "nvpair-proxy.exe", "owner": "pair", "created": 1, "cpuTime": 1, "workingSetBytes": GIB}}
        lp = winsys.LaneProcesses(table_fn=lambda: table, info_fn=info.get, clock=lambda: 0.0, cpus=4, self_pid=1)
        servers, _, _ = lp.sample({1235: "fast", 1234: "deep"})
        self.assertEqual((servers["fast"]["owner"], servers["deep"]["owner"]), (None, "pair"))

    def test_a_pair_owned_lane_port_is_not_an_agiw_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "windows-llm-pipeline").mkdir()
            (root / "windows-llm-pipeline" / "config.json").write_text(json.dumps({"runtime": {"lanes": {
                "fast": {"port": 1235, "alias": "openai/gpt-oss-20b"},
                "deep": {"port": 1234, "alias": "qwen3.8-27b"}}}}))
            processes = mock.Mock()
            processes.sample.return_value = (
                {"fast": {"pid": 11, "name": "llama-server.exe", "owner": "agiw", "ownerKnown": True, "created": 1, "cpuPercent": 1.0, "workingSetBytes": GIB},
                 "deep": {"pid": 22, "name": "nvpair-proxy.exe", "owner": "pair", "cpuPercent": 0.0, "workingSetBytes": GIB}},
                [], {"id": "lane-processes", "label": "Lane processes", "state": "live", "ageSeconds": 0.0, "detail": "x"})
            seen = []
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: 1000.0,
                                  gpu_fn=lambda: ([], None), adapters_fn=lambda: [], lane_processes=processes,
                                  notification_fn=lambda: 5)
            self.addCleanup(collector.close)

            def lane(spec, _now):
                return {**probes.probe_lane(spec, lane_fetch(spec["alias"])), "ageSeconds": 0.0}
            consumers = [{"label": "Local LLM server", "residentBytes": GIB, "processCount": 1, "gpuAllocBytes": None,
                          "lane": True}]
            with mock.patch.object(collector, "_lane", side_effect=lane), \
                    mock.patch.object(probes, "_memory_status", return_value={"totalPhys": 100 * GIB, "availPhys": 8 * GIB}), \
                    mock.patch.object(probes, "top_processes", side_effect=lambda **kw: seen.append(kw) or consumers):
                snapshot = collector.collect()
        deep = next(lane for lane in snapshot["lanes"] if lane["id"] == "deep")
        self.assertEqual(deep["status"], "not_agiw")
        self.assertIn("NVIDIA PAIR", deep["detail"])
        self.assertEqual((deep["slotsBusy"], deep["phase"], deep["liveTokensPerSecond"]), (None, None, None))
        worker = snapshot["windowsWorker"]
        self.assertFalse(worker["lanes"]["deep"]["up"])
        self.assertIn("port held by NVIDIA PAIR on deep", worker["lanesError"])
        row_ = next(r for r in snapshot["models"] if r["metadata"]["lane"] == "deep")
        self.assertEqual((row_["state"], row_["loaded"], row_["source"]), ("unknown", None, "lane-processes"))
        # Only the fast lane's PID is the lane server; PAIR's proxy never is.
        self.assertEqual(seen, [{"limit": 8, "lane_pids": frozenset({11})}])
        self.assertNotIn("loadedModelCount", snapshot["memory"]["consumers"][0])
        status = probes.pc_status(snapshot)
        self.assertEqual(status["health"], "degraded")
        self.assertIn("lane port held by NVIDIA PAIR, not counted: deep", status["limitations"])


if __name__ == "__main__":
    unittest.main()
