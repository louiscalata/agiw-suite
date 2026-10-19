"""Sol A3 review: no false clear, no inferred owner, and no unsigned relay authority."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes, winsys
from agiw_win.collect import merge_adapters
from tests import test_collector_safety as safety
from tests.test_collector_safety import NVIDIA, FAST, LIVE
from tests.test_machines import rows
from tests.test_pair_hands_off import gpu


class GpuEvidence(unittest.TestCase):
    def test_both_sources_survive_and_either_busy_reading_sets_hands_off(self):
        for smi, pdh in ((3, 92), (92, 3), (None, 92)):
            with self.subTest(smi=smi, pdh=pdh):
                adapter = {"name": NVIDIA["name"], "vendor": "nvidia", "luid": "a", "utilizationPercent": pdh,
                           "busiestEngine": "3D", "engineShares": [{"pid": 77, "percent": pdh}]}
                merged = merge_adapters([{**NVIDIA, "utilizationPercent": smi}], [adapter])
                self.assertEqual(merged[0]["utilizationReadings"],
                                 {"nvidia-smi": smi, "windows-gpu-counters": pdh})
                flag = probes.hands_off(merged, gpu_live=True, lane_pids=frozenset({77}), notification_state=5)
                for public in (flag, probes.pc_status({"handsOff": flag})["handsOff"],
                               probes.peer_presence({"handsOff": flag}, "pc")["handsOff"]):
                    self.assertIs(public["active"], True)
                    self.assertEqual(public["gpus"][0]["utilizationPercent"], 92)
                    self.assertEqual(public["gpus"][0]["utilizationReadings"], merged[0]["utilizationReadings"])
                if smi == 92:
                    self.assertIsNone(flag["agiwOnly"], "PDH cannot explain a higher SMI reading")

    def test_ninth_adapter_participates_before_detail_is_bounded(self):
        for last, expected in ((90, True), (None, None), (3, False)):
            with self.subTest(last=last):
                flag = probes.hands_off([gpu(3, index=i) for i in range(8)] + [gpu(last, index=8)],
                                        gpu_live=True, lane_pids=frozenset(), notification_state=5)
                self.assertIs(flag["active"], expected)
                self.assertLessEqual(len(flag["gpus"]), 8)
                if last == 90:
                    self.assertIn(8, [row["index"] for row in flag["gpus"]])
                    self.assertIn("90%", flag["detail"])

    def test_truncated_process_shares_cannot_claim_only_agiw(self):
        shares = [{"pid": 77, "percent": 1} for _ in range(64)] + [{"pid": 999, "percent": 90}]
        flag = probes.hands_off([gpu(90, shares)], gpu_live=True, lane_pids=frozenset({77}), notification_state=5)
        self.assertIs(flag["active"], True)
        self.assertEqual(flag["cause"], "unknown")
        self.assertIsNone(flag["agiwOnly"])


class OwnerEvidence(unittest.TestCase):
    setUp = safety.CollectorSafety.setUp
    collector = safety.CollectorSafety.collector
    configure = safety.CollectorSafety.configure
    fetch = staticmethod(safety.CollectorSafety.fetch)
    processes = staticmethod(safety.CollectorSafety.processes)

    def test_readable_unrecognized_owner_is_never_probed(self):
        self.configure()
        fetch = mock.Mock(side_effect=self.fetch)
        collector = self.collector(processes=self.processes(None), fetch=fetch,
                                   gpus=[gpu(92, [{"pid": 77, "percent": 92}])])
        snapshot = collector.collect()
        self.assertEqual(snapshot["lanes"][0]["status"], "unknown")
        self.assertEqual(snapshot["handsOff"]["cause"], "unknown")
        self.assertIsNone(snapshot["handsOff"]["agiwOnly"])
        fetch.assert_not_called()

    def test_missing_listener_or_native_reader_never_authorizes_http(self):
        self.configure()
        for processes in (None, mock.Mock()):
            with self.subTest(processes=processes):
                if processes is not None:
                    processes.sample.return_value = ({}, [], LIVE)
                fetch = mock.Mock(side_effect=self.fetch)
                collector = self.collector(processes=processes, fetch=fetch)
                collector.lane_processes = processes
                self.assertEqual(collector.collect()["lanes"][0]["status"], "unknown")
                fetch.assert_not_called()

    def test_ownership_is_rechecked_before_each_endpoint(self):
        self.configure()
        processes = self.processes()
        calls = []

        def fetch(url, timeout, limit):
            calls.append(url)
            processes.sample.return_value = self.processes(None).sample.return_value
            return self.fetch(url, timeout, limit)

        collector = self.collector(processes=processes, fetch=fetch)
        snapshot = collector.collect()
        self.assertEqual(calls, [f"http://127.0.0.1:{FAST['port']}/health"])
        self.assertNotIn(snapshot["lanes"][0]["status"], probes.LANE_UP)


class NativeOwnerPins(unittest.TestCase):
    def test_only_an_exact_locally_pinned_process_is_a_lane_owner(self):
        image = r"C:\Local\AGIW\llama-server.exe"
        identity = {"pid": 77, "created": 123, "image": image}
        info = {**identity, "name": "llama-server.exe", "owner": None, "cpuTime": 0, "workingSetBytes": 1}
        table = [{"state": winsys.MIB_TCP_STATE_LISTEN, "local": "127.0.0.1", "localPort": 1235,
                  "remote": "0.0.0.0", "remotePort": 0, "pid": 77}]
        for changes, expected in (({}, "agiw"), ({"pid": 78}, None), ({"created": 124}, None),
                                  ({"image": r"C:\Other\llama-server.exe"}, None), ({"owner": "pair"}, "pair")):
            with self.subTest(changes=changes):
                process = winsys.LaneProcesses(table_fn=lambda: table, info_fn=lambda _: {**info, **changes},
                                               identities={"fast": identity})
                servers, _, _ = process.sample({1235: "fast"})
                self.assertEqual(servers["fast"]["owner"], expected)
                self.assertNotIn(image, json.dumps(servers))
        unpinned = winsys.LaneProcesses(table_fn=lambda: table, info_fn=lambda _: info)
        self.assertIsNone(unpinned.sample({1235: "fast"})[0]["fast"]["owner"])

    def test_unattributed_listener_is_not_counted_as_an_agiw_process(self):
        self.assertEqual(probes.lane_server_pids([{"process": {"pid": 77, "owner": None, "ownerKnown": True}}]),
                         frozenset())


class PublicEvidence(unittest.TestCase):
    def test_unattributed_pair_port_preserves_unknown_and_port_evidence(self):
        component = probes.pair_component({"ok": True, "processes": [], "uninspected": 0,
                                           "listeners": [{"pid": 999, "local": "0.0.0.0", "port": 14318}]})
        for public in (component, probes.pair_public(component), probes.pc_status({"components": [component]})["pair"],
                       probes.peer_presence({"components": [component]}, "pc")["pair"]):
            self.assertEqual(public["state"], "unknown")
            self.assertIsNone(public["lanExposed"])
            self.assertEqual(public["unattributedPorts"], [{"port": 14318, "bind": "all"}])

    def test_self_asserted_relay_and_verified_fields_never_set_canonical_linux(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = probes.peer_dir(root)
            folder.mkdir(parents=True)
            for host in ("evil", "linux-node-1"):
                for health in ("down", "ok"):
                    with self.subTest(host=host, health=health):
                        doc = {"schemaVersion": 1, "kind": "agiw-peer", "host": host, "platform": "linux",
                               "relayedBy": "mac", "verified": True, "observedUnix": 999.0,
                               "models": [{"id": "m", "loadedState": "loaded"}], "modelsTotal": 1, "health": health}
                        (folder / f"{host}.json").write_text(json.dumps(doc))
                        peers, source = probes.read_peers(root, "pc", 1000.0)
                        linux = rows(peers)[2]
                        self.assertEqual((linux["id"], linux["state"], linux["reason"]),
                                         ("linux", "unknown", "relay-unverified"))
                        for field in ("loadedCount", "relayedBy", "observedUnix", "ageSeconds"):
                            self.assertIsNone(linux[field])
                        self.assertNotIn("relayed by mac", source["detail"])

    def test_ready_nisi_never_publishes_private_alias_or_version_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gate = root / "nisi-gate-v0.2"
            gate.mkdir()
            (gate / "nisi_gate.mjs").touch()
            private = r"C:\Users\private-owner\models\private-model.gguf"
            package = root / "nisi-v0.2" / "node_modules" / "nisi"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"version": private}))
            lanes = [{"id": lane, "status": "idle", "expectedModel": private} for lane in ("fast", "deep")]
            component = probes.nisi_component(lanes, root)
            self.assertEqual(component["state"], "ready")
            self.assertEqual(component["authorModel"], "private-model.gguf")
            snapshot = {"lanes": lanes, "components": [component]}
            for public in (probes.pc_status(snapshot), probes.peer_link_status_doc(snapshot, "pc")):
                text = json.dumps(public)
                self.assertNotIn("private-owner", text)
                self.assertNotIn("models\\", text)


if __name__ == "__main__":
    unittest.main()
