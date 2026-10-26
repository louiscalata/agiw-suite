"""PLAN A3 with critic correction 3: a PAIR row from process and listener ownership, and the handsOff flag."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, winsys  # noqa: E402
from agiw_win.collect import Collector, merge_adapters  # noqa: E402

LISTEN = winsys.MIB_TCP_STATE_LISTEN
PC_CENSUS = {  # the 10-16 native snapshot's layout: PAIR.exe + children, the proxy on ::, the engine on loopback
    "ok": True, "uninspected": 3,
    "processes": [{"pid": 5588, "name": "PAIR.exe", "owner": "pair"}, {"pid": 18468, "name": "nvpair-proxy.exe", "owner": "pair"},
                  {"pid": 19768, "name": "llama-server.exe", "owner": "pair"}],
    "listeners": [{"pid": 18468, "local": "::", "port": 1234}, {"pid": 18468, "local": "::", "port": 8080},
                  {"pid": 18468, "local": "::", "port": 11434}, {"pid": 19768, "local": "127.0.0.1", "port": 8081},
                  {"pid": 5612, "local": "0.0.0.0", "port": 22}, {"pid": 7708, "local": "127.0.0.1", "port": 1235}]}


class PairComponent(unittest.TestCase):
    def test_running_pair_with_its_engine_and_lan_listeners(self):
        row = probes.pair_component(PC_CENSUS)
        self.assertEqual((row["id"], row["state"], row["processCount"], row["engineCount"]), ("pair", "running", None, None))
        self.assertEqual(row["listeners"], [{"port": 1234, "bind": "all", "role": "pair"}, {"port": 8080, "bind": "all", "role": "pair"},
                                            {"port": 8081, "bind": "loopback", "role": "engine"},
                                            {"port": 11434, "bind": "all", "role": "pair"}])
        self.assertTrue(row["lanExposed"])
        self.assertIn("open to the LAN on :1234, :8080, :11434", row["detail"])
        self.assertIn("not AGIW lanes", row["detail"])
        # Unrelated listeners (sshd :22, the AGIW fast lane :1235) are not PAIR's.
        self.assertEqual(row["unattributedPorts"], [])
        self.assertEqual(row["evidence"], "process+listener")

    def test_pair_ports_held_by_unrecognised_processes_are_unattributed_not_pair(self):
        census = {"ok": True, "processes": [], "uninspected": 0,
                  "listeners": [{"pid": 900, "local": "::", "port": 1234}, {"pid": 901, "local": "192.0.2.71", "port": 14320}]}
        row = probes.pair_component(census)
        self.assertEqual((row["state"], row["processCount"], row["lanExposed"]), ("unknown", None, None))
        self.assertEqual(row["unattributedPorts"], [{"port": 1234, "bind": "all"}, {"port": 14320, "bind": "address"}])
        self.assertIn("held by unidentified processes: :1234, :14320", row["detail"])

    def test_not_seen_discloses_uninspected_processes(self):
        row = probes.pair_component({"ok": True, "processes": [], "listeners": [], "uninspected": 2})
        self.assertEqual(row["state"], "unknown")
        self.assertEqual(row["detail"], "No NVIDIA PAIR process seen (2 processes could not be inspected)")

    def test_an_unreadable_census_is_unknown_never_not_running(self):
        for census in (None, {}, {"ok": False, "error": "OSError"}, [], "census"):
            with self.subTest(census=census):
                row = probes.pair_component(census)
                self.assertEqual((row["state"], row["processCount"], row["lanExposed"]), ("unknown", None, None))

    def test_malformed_rows_are_skipped(self):
        census = {"ok": True, "processes": [{"pid": "5588", "owner": "pair"}, {"pid": 1, "owner": None}, None],
                  "listeners": [{"pid": 1, "port": "1234"}, None, {"pid": True, "port": 8080}]}
        row = probes.pair_component(census)
        self.assertEqual((row["state"], row["listeners"], row["unattributedPorts"]), ("not-seen", [], []))

    def test_public_projection_has_ports_and_counts_only(self):
        public = probes.pair_public(probes.pair_component(PC_CENSUS))
        self.assertEqual(public, {"state": "running", "processCount": None, "engineCount": None, "processCountAtLeast": 3, "engineCountAtLeast": 1, "lanExposed": True, "uninspected": 3, "unattributedPorts": [],
                                  "listeners": [{"port": 1234, "bind": "all"}, {"port": 8080, "bind": "all"},
                                                {"port": 8081, "bind": "loopback"}, {"port": 11434, "bind": "all"}]})
        self.assertNotIn("pid", json.dumps(public))
        self.assertIsNone(probes.pair_public({"id": "nisi"}))


class PairCensus(unittest.TestCase):
    def test_census_reads_processes_and_listeners_and_caches(self):
        images = mock.Mock(return_value=([{"pid": 1, "name": "PAIR.exe", "owner": "pair"},
                                          {"pid": 2, "name": "code.exe", "owner": None}], 1))
        table = mock.Mock(return_value=[{"state": LISTEN, "local": "::", "localPort": 1234, "remote": "::", "remotePort": 0, "pid": 1},
                                        {"state": 5, "local": "127.0.0.1", "localPort": 5000, "remote": "127.0.0.1",
                                         "remotePort": 1234, "pid": 2}])
        now = [100.0]
        census = winsys.PairCensus(images_fn=images, table_fn=table, clock=lambda: now[0], ttl=10.0)
        first = census.sample()
        self.assertEqual(first, {"ok": True, "processes": [{"pid": 1, "name": "PAIR.exe", "owner": "pair"}],
                                 "listeners": [{"pid": 1, "local": "::", "port": 1234}], "uninspected": 1})
        now[0] = 109.9
        self.assertIs(census.sample(), first)
        now[0] = 110.0
        census.sample()
        self.assertEqual((images.call_count, table.call_count), (2, 2))

    def test_a_failed_census_is_not_ok(self):
        census = winsys.PairCensus(images_fn=mock.Mock(side_effect=OSError("denied")), table_fn=lambda: [], clock=lambda: 0.0)
        self.assertEqual(census.sample(), {"ok": False, "error": "OSError"})
        self.assertEqual(probes.pair_component(census.sample())["state"], "unknown")

    def test_off_windows_the_system_reads_refuse(self):
        if winsys.IS_WINDOWS:
            self.skipTest("POSIX behaviour")
        with self.assertRaises(OSError):
            winsys.process_images()
        self.assertIsNone(winsys.notification_state())


class GpuEngineShares(unittest.TestCase):
    ADAPTERS = [{"name": "NVIDIA GeForce RTX 3080 Ti", "vendor": "nvidia", "luid": "000000000000D1A4", "memoryTotalMiB": 12288},
                {"name": "AMD Radeon RX 5700 XT", "vendor": "amd", "luid": "000000000000E2B5", "memoryTotalMiB": 8176}]

    def test_busiest_engine_is_split_by_process(self):
        engine = [("pid_7708_luid_0x00000000_0x0000D1A4_phys_0_eng_1_engtype_Cuda", 70.0),
                  ("pid_4444_luid_0x00000000_0x0000D1A4_phys_0_eng_1_engtype_Cuda", 5.0),
                  ("pid_4444_luid_0x00000000_0x0000D1A4_phys_0_eng_0_engtype_3D", 30.0),
                  ("pid_5555_luid_0x00000000_0x0000E2B5_phys_0_eng_0_engtype_3D", 50.0),
                  ("pid_6666_luid_0x00000000_0x0000E2B5_phys_0_eng_0_engtype_3D", 0.0)]
        memory = [("luid_0x00000000_0x0000D1A4_phys_0", 2 ** 30)]
        rows = winsys.aggregate_gpu_counters(engine, memory, self.ADAPTERS)
        self.assertEqual((rows[0]["utilizationPercent"], rows[0]["busiestEngine"], rows[0]["memoryUsedMiB"]), (75.0, "Cuda", 1024))
        self.assertEqual(rows[0]["engineShares"], [{"pid": 7708, "percent": 70.0}, {"pid": 4444, "percent": 5.0}])
        self.assertEqual((rows[1]["utilizationPercent"], rows[1]["busiestEngine"]), (50.0, "3D"))
        self.assertEqual(rows[1]["engineShares"], [{"pid": 5555, "percent": 50.0}])

    def test_no_counters_means_no_shares(self):
        rows = winsys.aggregate_gpu_counters([], [], self.ADAPTERS)
        self.assertEqual([(r["utilizationPercent"], r["engineShares"]) for r in rows], [(None, None), (None, None)])

    def test_merged_nvidia_rows_keep_the_counter_split(self):
        smi = [{"index": 0, "name": "NVIDIA GeForce RTX 3080 Ti", "utilizationPercent": 88.0, "memoryUsedMiB": 11000.0,
                "memoryTotalMiB": 12288.0, "temperatureC": 70.0, "powerW": 300.0, "vendor": "nvidia"}]
        adapters = [{**self.ADAPTERS[0], "utilizationPercent": 80.0, "memoryUsedMiB": 11000, "busiestEngine": "Cuda",
                     "engineShares": [{"pid": 7708, "percent": 80.0}]},
                    {**self.ADAPTERS[1], "utilizationPercent": 4.4, "memoryUsedMiB": 792, "busiestEngine": "3D",
                     "engineShares": []}]
        rows = merge_adapters(smi, adapters)
        self.assertEqual((rows[0]["utilizationPercent"], rows[0]["source"], rows[0]["engineShares"]),
                         (88.0, "nvidia-smi", [{"pid": 7708, "percent": 80.0}]))
        self.assertEqual((rows[1]["source"], rows[1]["busiestEngine"], rows[1]["engineShares"]), ("windows-gpu-counters", "3D", []))


def gpu(util, shares=None, name="RTX", engine="Cuda", index=0):
    return {"index": index, "name": name, "utilizationPercent": util, "memoryUsedMiB": 1000.0, "memoryTotalMiB": 12288.0,
            "temperatureC": 50.0, "powerW": 100.0, "vendor": "nvidia", "busiestEngine": engine, "engineShares": shares}


class HandsOff(unittest.TestCase):
    LANES = frozenset({7708})

    def flag(self, gpus, notification=5, gpu_live=True, lane_pids=LANES):
        return probes.hands_off(gpus, gpu_live=gpu_live, lane_pids=lane_pids, notification_state=notification)

    def test_clear_needs_every_gpu_at_or_below_forty_and_no_fullscreen(self):
        for state in (5, 6):
            with self.subTest(notification=state):
                result = self.flag([gpu(40.0), gpu(4.4, index=100)], notification=state)
                self.assertEqual((result["active"], result["state"], result["reasons"], result["cause"]),
                                 (False, "clear", [], None))
                self.assertIsNone(result["agiwOnly"])

    def test_above_forty_is_hands_off_and_names_the_cause(self):
        cases = [([{"pid": 7708, "percent": 90.0}], "agiw-lanes", True),
                 ([{"pid": 4444, "percent": 90.0}], "other-processes", False),
                 ([{"pid": 7708, "percent": 50.0}, {"pid": 4444, "percent": 40.0}], "mixed", False),
                 ([{"pid": 7708, "percent": 85.0}, {"pid": 4444, "percent": 5.0}], "agiw-lanes", True),
                 (None, "unknown", None), ([], "unknown", None)]
        for shares, cause, agiw_only in cases:
            with self.subTest(shares=shares):
                result = self.flag([gpu(90.0, shares)])
                self.assertEqual((result["active"], result["state"], result["reasons"]), (True, "hands-off", ["gpu-busy"]))
                self.assertEqual((result["cause"], result["agiwOnly"]), (cause, agiw_only))
                self.assertEqual(result["gpus"][0]["cause"], cause)
        detail = self.flag([gpu(90.0, [{"pid": 7708, "percent": 90.0}])])["detail"]
        self.assertEqual(detail, "Hands off: RTX at 90% (AGIW lanes 90%, other 0%) (AGIW's own lane work)")

    def test_forty_point_one_is_above_the_threshold(self):
        self.assertEqual(self.flag([gpu(40.1, [{"pid": 4444, "percent": 40.1}])])["active"], True)

    def test_unknown_lane_pids_cannot_attribute(self):
        result = self.flag([gpu(90.0, [{"pid": 7708, "percent": 90.0}])], lane_pids=None)
        self.assertEqual((result["active"], result["cause"], result["gpus"][0]["agiwPercent"]), (True, "unknown", None))

    def test_a_game_or_fullscreen_app_is_hands_off_even_with_idle_gpus(self):
        for state, reason, cause in ((3, "fullscreen-game", "game"), (2, "fullscreen-app", "fullscreen"),
                                     (7, "store-app", "fullscreen"), (4, "presentation-mode", "fullscreen")):
            with self.subTest(notification=state):
                result = self.flag([gpu(3.0)], notification=state)
                self.assertEqual((result["active"], result["reasons"], result["cause"]), (True, [reason], cause))
        self.assertIn("a full-screen game is running", self.flag([gpu(3.0)], notification=3)["detail"])

    def test_other_process_3d_load_is_marked_as_graphics(self):
        result = self.flag([gpu(95.0, [{"pid": 4444, "percent": 95.0}], engine="3D")])
        self.assertTrue(result["gpus"][0]["graphics"])
        self.assertNotIn("graphics", self.flag([gpu(95.0, [{"pid": 7708, "percent": 95.0}], engine="3D")])["gpus"][0])

    def test_unread_inputs_are_unknown_never_clear(self):
        cases = [dict(gpus=[], notification=5), dict(gpus=None, notification=5), dict(gpus=[gpu(10.0)], gpu_live=False),
                 dict(gpus=[gpu(10.0), gpu(None, index=100)], notification=5), dict(gpus=[gpu(10.0)], notification=None),
                 dict(gpus=[gpu(10.0)], notification=0), dict(gpus=[gpu(10.0)], notification=True),
                 dict(gpus=[gpu(10.0)], notification="5"), dict(gpus=["gpu"], notification=5)]
        for case in cases:
            with self.subTest(case=case):
                result = self.flag(case["gpus"], notification=case.get("notification", 5), gpu_live=case.get("gpu_live", True))
                self.assertEqual((result["active"], result["state"]), (None, "unknown"))
                self.assertTrue(result["unknown"])
                self.assertTrue(result["detail"].startswith("Unknown: "))

    def test_a_busy_gpu_wins_over_an_unread_one(self):
        result = self.flag([gpu(90.0, [{"pid": 4444, "percent": 90.0}]), gpu(None, index=100)], notification=None)
        self.assertEqual((result["active"], result["cause"]), (True, "other-processes"))
        self.assertEqual(result["unknown"], ["gpu-load-unknown", "fullscreen-unknown"])

    def test_malformed_shares_void_the_split(self):
        for shares in ([{"pid": "7708", "percent": 90.0}], [{"pid": 7708, "percent": float("nan")}], ["x"],
                       [{"pid": 7708, "percent": 0.0}]):
            with self.subTest(shares=shares):
                self.assertEqual(self.flag([gpu(90.0, shares)])["cause"], "unknown")

    def test_public_projection_carries_no_process_identity(self):
        result = self.flag([gpu(90.0, [{"pid": 7708, "percent": 85.0}, {"pid": 4444, "percent": 5.0}])], notification=3)
        public = probes.hands_off_public(result)
        self.assertEqual(set(public), {"active", "state", "reasons", "unknown", "cause", "agiwOnly", "thresholdPercent", "gpus"})
        self.assertEqual((public["active"], public["cause"], public["reasons"]), (True, "game", ["gpu-busy", "fullscreen-game"]))
        self.assertNotIn("7708", json.dumps(public))
        self.assertNotIn("engineShares", json.dumps(public))
        for bad in (None, {}, {"active": "yes"}, []):
            with self.subTest(bad=bad):
                self.assertEqual(probes.hands_off_public(bad)["state"], "unknown")


class HandsOffInTheSample(unittest.TestCase):
    def collect(self, gpus, notification, servers):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "windows-llm-pipeline").mkdir()
            (root / "windows-llm-pipeline" / "config.json").write_text(json.dumps({"runtime": {"lanes": {
                "fast": {"port": 1235, "alias": "openai/gpt-oss-20b"}, "deep": {"port": 1236, "alias": "qwen3.8-27b"}}}}))
            processes = mock.Mock()
            processes.sample.return_value = (servers, [], {"id": "lane-processes", "label": "Lane processes",
                                                           "state": "live", "ageSeconds": 0.0, "detail": "x"})
            census = mock.Mock()
            census.sample.return_value = PC_CENSUS
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: 1000.0,
                                  fetch=mock.Mock(side_effect=ConnectionRefusedError), lane_processes=processes,
                                  gpu_fn=lambda: (gpus, probes._source("pc-gpu", "PC GPU", "live", "test")),
                                  adapters_fn=lambda: [{"name": "RTX"}], memory_fn=lambda: ({"level": "ok"}, None),
                                  pair_census=census, notification_fn=lambda: notification, lane_wait=2.0)
            self.addCleanup(collector.close)
            # Exercise Windows' inventory-completeness path without constructing native PDH/DXGI readers.
            collector.gpu_counters = mock.Mock(sample=mock.Mock(return_value=[]))
            with mock.patch.object(winsys, "IS_WINDOWS", True):
                return collector.collect()

    def test_snapshot_presence_and_status_carry_hands_off_and_pair(self):
        snapshot = self.collect([gpu(92.0, [{"pid": 7708, "percent": 92.0}])], 5,
                                {"fast": {"pid": 7708, "name": "llama-server.exe", "owner": "agiw", "ownerKnown": True, "created": 1, "cpuPercent": 1.0,
                                          "workingSetBytes": 1},
                                 "deep": {"pid": 8808, "name": "llama-server.exe", "owner": "agiw", "ownerKnown": True, "created": 1}})
        hands = snapshot["handsOff"]
        self.assertEqual((hands["active"], hands["cause"], hands["agiwOnly"]), (True, "agiw-lanes", True))
        self.assertEqual(snapshot["machines"][0]["handsOff"], True)
        source = next(s for s in snapshot["sources"] if s["id"] == "hands-off")
        self.assertEqual(source["state"], "live")
        pair = next(c for c in snapshot["components"] if c["id"] == "pair")
        self.assertEqual(pair["state"], "running")
        presence = probes.peer_presence(snapshot, "test-pc")
        status = probes.pc_status(snapshot)
        probes.validate_status(status)
        for doc in (presence, status):
            self.assertEqual((doc["handsOff"]["active"], doc["handsOff"]["cause"]), (True, "agiw-lanes"))
            self.assertEqual(doc["pair"]["state"], "running")
            text = json.dumps(doc)
            for private in ("7708", "18468", "nvpair-proxy", "engineShares"):
                self.assertNotIn(private, text)
        self.assertIn({"name": "NVIDIA PAIR", "status": "running"}, status["components"])

    def test_no_lane_process_reading_leaves_the_cause_unknown(self):
        snapshot = self.collect([gpu(92.0, [{"pid": 7708, "percent": 92.0}])], 5, {})
        # Missing owner evidence cannot attribute GPU work to another process.
        self.assertEqual(snapshot["handsOff"]["cause"], "unknown")

    def test_a_crashing_fullscreen_read_is_unknown_not_clear(self):
        def boom():
            raise OSError("shell32")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: 1000.0,
                                  lane_processes=None, gpu_fn=lambda: ([gpu(3.0)], probes._source("pc-gpu", "PC GPU", "live", "t")),
                                  adapters_fn=lambda: [{"name": "RTX"}], memory_fn=lambda: ({"level": "ok"}, None), notification_fn=boom,
                                  pair_census=mock.Mock(sample=mock.Mock(return_value={"ok": False})))
            self.addCleanup(collector.close)
            collector.gpu_counters = mock.Mock(sample=mock.Mock(return_value=[]))
            with mock.patch.object(winsys, "IS_WINDOWS", True):
                snapshot = collector.collect()
        self.assertEqual((snapshot["handsOff"]["active"], snapshot["handsOff"]["unknown"]), (None, ["fullscreen-unknown"]))
        self.assertEqual(next(s for s in snapshot["sources"] if s["id"] == "hands-off")["state"], "unavailable")
        # No PAIR census off Windows: unknown, never "not running".
        self.assertEqual(next(c for c in snapshot["components"] if c["id"] == "pair")["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
