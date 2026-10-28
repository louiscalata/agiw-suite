"""machines[] is this PC and the Mac. A Linux share file is not a machine row, and unknown is never down."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402

NOW = 2_000_000_000.0
UP_LANES = [{"id": "fast", "status": "idle"}, {"id": "deep", "status": "busy"}]


def record(host, platform, age=2.0, health="ok", models=None, relayed=None, source="agiw-peers", complete=True):
    return {"host": host, "platform": platform, "edition": platform, "version": "1", "health": health,
            "models": models if models is not None else [{"id": "m", "loadedState": "loaded", "activity": "idle"}],
            "modelsComplete": complete, "ageSeconds": age, "fresh": age <= 30, "source": source,
            "observedUnix": NOW - age, "relayedBy": relayed, "verified": False}


def rows(peers=(), mac_name="target-mac.local", lanes=UP_LANES, hands=None, paired=None):
    return probes.machine_rows(host="test-pc", sampled_at=NOW, lanes=lanes, hands=hands, version="0.2.0",
                               peers=list(peers), mac_name=mac_name, paired=paired)


class MachineRows(unittest.TestCase):
    def test_pc_and_mac_appear_and_a_missing_linux_node_does_not(self):
        out = rows()
        self.assertEqual([(r["platform"], r["role"]) for r in out],
                         [("windows", "this-machine"), ("macos", "peer")])
        self.assertEqual([(r["state"], r["reason"]) for r in out],
                         [("up", "serving"), ("unknown", "no-report")])
        self.assertEqual(out[1]["name"], "target-mac.local")
        self.assertFalse(any(row["platform"] == "linux" for row in out))
        self.assertEqual((out[1]["health"], out[1]["loadedCount"], out[1]["ageSeconds"]), ("unknown", None, None))

    def test_this_pc_state_follows_lane_evidence_never_unknown_as_down(self):
        cases = [(UP_LANES, "up", "serving", 2),
                 ([{"status": "idle"}, {"status": "unreachable"}], "unknown", "lane-state-unknown", None),
                 ([{"status": "unreachable"}, {"status": "unreachable"}], "unknown", "lane-state-unknown", None),
                 ([{"status": "undeclared"}, {"status": "undeclared"}], "unknown", "lane-state-unknown", None),
                 ([{"status": "unreachable"}, {"status": "stale"}], "unknown", "lane-state-unknown", None),
                 ([], "unknown", "lane-state-unknown", None)]
        for lanes, state, reason, loaded in cases:
            with self.subTest(lanes=lanes):
                pc = rows(lanes=lanes)[0]
                self.assertEqual((pc["state"], pc["reason"], pc["loadedCount"], pc["trust"]), (state, reason, loaded, "local"))
        self.assertIs(rows(hands={"active": True})[0]["handsOff"], True)
        self.assertIsNone(rows(hands=None)[0]["handsOff"])

    def test_the_mac_row_is_attributed_only_to_the_configured_name(self):
        out = rows([record("other-mac", "macos"), record("TARGET-MAC", "macos", age=4.0)])
        mac = out[1]
        self.assertEqual((mac["id"], mac["state"], mac["reason"], mac["trust"], mac["loadedCount"]),
                         ("TARGET-MAC", "up", "reported-serving", "share-reported", 1))
        self.assertIn("unsigned, not verified", mac["detail"])
        self.assertEqual(len(out), 2, "an unattributed Mac report gets no machine row")
        unconfigured = rows([record("target-mac", "macos")], mac_name=None)[1]
        self.assertEqual((unconfigured["state"], unconfigured["reason"]), ("unknown", "unconfigured"))

    def test_down_only_from_a_fresh_report_and_stale_is_not_observed_since(self):
        self.assertEqual(rows([record("target-mac", "macos", health="down")])[1][("state")], "down")
        self.assertEqual(rows([record("target-mac", "macos", health="down")])[1]["reason"], "reported-down")
        stale = rows([record("target-mac", "macos", age=284_451.0, health="down")])[1]
        self.assertEqual((stale["state"], stale["reason"], stale["health"], stale["loadedCount"]),
                         ("unknown", "stale", "unknown", None))
        self.assertEqual(stale["detail"], "Not observed since 2033-05-14T20:32:29Z (79.0 h); state unknown")
        for health in ("weird", None, "unknown"):
            with self.subTest(health=health):
                self.assertEqual(rows([record("target-mac", "macos", health=health)])[1]["state"], "unknown")

    def test_unsigned_linux_share_does_not_become_a_machine_row(self):
        for age in (6.5, 31.0):
            with self.subTest(age=age):
                out = rows([record("linux-node-1", "linux", age=age, relayed="mac")])
                self.assertEqual([row["platform"] for row in out], ["windows", "macos"])

    def test_incomplete_inventories_make_no_count_claim(self):
        for models, complete in (([{"id": "a", "loadedState": "unknown"}], True), ([{"id": "a", "loadedState": "loaded"}], False)):
            with self.subTest(models=models, complete=complete):
                self.assertIsNone(rows([record("target-mac", "macos", models=models, complete=complete)])[1]["loadedCount"])

    def test_other_pcs_are_listed_after_the_three_and_the_pairing_link_annotates(self):
        paired = [{"node": "x", "host": "mac", "paired": True, "state": "connected"},
                  {"node": "y", "host": "windows", "paired": True, "state": "connected"}]
        out = rows([record("second-pc", "windows"), record("target-mac", "macos")], paired=paired)
        self.assertEqual([r["id"] for r in out], ["test-pc", "target-mac", "second-pc"])
        self.assertEqual(out[1]["peerLink"], "connected")
        self.assertIsNone(out[2]["peerLink"], "a host-kind link cannot be pinned to one of several PCs")

    def test_rows_are_json_safe(self):
        out = rows([record("target-mac", "macos"), record("linux-node-1", "linux", relayed="mac")], hands={"active": None})
        json.dumps(out, allow_nan=False)


class RelayedPeers(unittest.TestCase):
    def doc(self, **over):
        value = {"schemaVersion": 1, "kind": "agiw-peer", "host": "linux-node-1", "platform": "linux", "edition": "linux",
                 "observedUnix": NOW - 3, "relayedBy": "mac", "health": "ok", "modelsTotal": 1,
                 "models": [{"id": "qwen35", "loadedState": "inactive"}]}
        value.update(over)
        return value

    def test_only_the_mac_may_relay_and_only_linux(self):
        self.assertEqual(probes._validate_peer(self.doc(), NOW)["relayedBy"], "mac")
        for bad in ({"relayedBy": "linux"}, {"relayedBy": "windows"}, {"relayedBy": ""}, {"relayedBy": True},
                    {"relayedBy": ["mac"]}, {"platform": "macos"}, {"platform": "windows"}):
            with self.subTest(bad=bad):
                self.assertIsNone(probes._validate_peer(self.doc(**bad), NOW))
        direct = probes._validate_peer(self.doc(relayedBy=None), NOW)
        self.assertIsNone(direct["relayedBy"])

    def test_a_relay_cannot_refresh_the_observation(self):
        old = probes._validate_peer(self.doc(observedUnix=NOW - 120), NOW)
        self.assertEqual((old["fresh"], old["ageSeconds"], old["observedUnix"]), (False, 120.0, NOW - 120))
        self.assertIsNone(probes._validate_peer(self.doc(observedUnix=NOW + 6), NOW), "future-dated relays are refused")

    def test_read_peers_lists_a_relayed_file_with_its_relayer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probes.write_json_atomic(probes.peer_dir(root) / "linux-node-1.json", self.doc(verified=True),
                                     probes.PEER_MAX_BYTES, stop_at=root)
            peers, source = probes.read_peers(root, "test-pc", NOW)
        self.assertEqual([(p["host"], p["platform"], p["relayedBy"], p["fresh"]) for p in peers],
                         [("linux-node-1", "linux", "mac", True)])
        self.assertIs(peers[0]["verified"], False, "a share file cannot claim to be verified")
        self.assertIn("linux-node-1 claims relay by mac", source["detail"])


class MachinesInTheSnapshot(unittest.TestCase):
    def test_snapshot_carries_machines_from_the_share(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "llm-lab" / "mac-reach").mkdir(parents=True)
            (root / "llm-lab" / "mac-reach" / "hosts.json").write_text(json.dumps({"mac": {"hostname": "test-mac.local"}}))
            probes.write_json_atomic(probes.peer_dir(root) / "test-mac.json", {
                "schemaVersion": 1, "kind": "agiw-peer", "host": "test-mac", "platform": "macos", "observedUnix": NOW - 1,
                "health": "ok", "modelsTotal": 0, "models": []}, probes.PEER_MAX_BYTES, stop_at=root)
            probes.write_json_atomic(probes.peer_dir(root) / "linux-node-1.json", RelayedPeers.doc(RelayedPeers()),
                                     probes.PEER_MAX_BYTES, stop_at=root)
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: NOW,
                                  fetch=mock.Mock(side_effect=AssertionError("no request")), lane_processes=None,
                                  gpu_fn=lambda: ([], None), adapters_fn=lambda: [], memory_fn=lambda: ({"level": "ok"}, None),
                                  notification_fn=lambda: None)
            self.addCleanup(collector.close)
            snapshot = collector.collect()
        machines = snapshot["machines"]
        self.assertEqual([(m["platform"], m["state"], m["reason"]) for m in machines],
                         [("windows", "unknown", "lane-state-unknown"), ("macos", "up", "reported-serving")])
        self.assertFalse(any(m["platform"] == "linux" for m in machines))
        self.assertIsNone(machines[1]["loadedCount"], "an empty inventory is not proof of zero loaded")
        json.dumps(snapshot, allow_nan=False, default=str)

    def test_a_crashing_machine_builder_keeps_the_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            collector = Collector(root, root / "bin", runs_root=root / "runs", host="test-pc", clock=lambda: NOW,
                                  lane_processes=None, gpu_fn=lambda: ([], None), adapters_fn=lambda: [],
                                  memory_fn=lambda: ({"level": "ok"}, None), notification_fn=lambda: None)
            self.addCleanup(collector.close)
            with mock.patch.object(probes, "machine_rows", side_effect=RuntimeError("boom")):
                snapshot = collector.collect()
        self.assertEqual(snapshot["machines"], [])
        self.assertEqual(next(s for s in snapshot["sources"] if s["id"] == "machines")["state"], "error")
        self.assertEqual(snapshot["host"], "windows")


if __name__ == "__main__":
    unittest.main()
