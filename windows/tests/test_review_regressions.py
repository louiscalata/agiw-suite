"""A3 review regressions: unknown evidence must survive machine and public projections."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from tests.test_machines import record, rows  # noqa: E402
from tests.test_pair_hands_off import gpu  # noqa: E402


class LaneEvidence(unittest.TestCase):
    def test_no_serving_lane_never_makes_a_machine_up(self):
        cases = (("loading", "loading", "down"), ("identity_mismatch", "identity_mismatch", "down"),
                 ("not_agiw", "not_agiw", "down"), ("not_agiw", "unreachable", "unknown"),
                 ("unreachable", "unreachable", "unknown"), ("future-status", "loading", "unknown"))
        for fast, deep, expected in cases:
            with self.subTest(fast=fast, deep=deep):
                lanes = [{"id": "fast", "status": fast}, {"id": "deep", "status": deep}]
                machine = rows(lanes=lanes)[0]
                self.assertEqual(machine["state"], expected)
                self.assertEqual(machine["reason"], "no-lane-serving" if expected == "down" else "lane-state-unknown")
                self.assertIsNone(machine["loadedCount"])
                self.assertNotIn("partly-serving", machine["reason"])
                for public in (probes.pc_status({"lanes": lanes}), probes.peer_presence({"lanes": lanes}, "pc")):
                    self.assertEqual(public["health"], expected)

    def test_degraded_peer_needs_a_serving_model_to_claim_up(self):
        for models in ([], [{"id": "m", "loadedState": "unknown"}], [{"id": "m", "loadedState": "inactive"}]):
            with self.subTest(models=models):
                machine = rows([record("second-pc", "windows", health="degraded", models=models)])[-1]
                self.assertEqual((machine["state"], machine["reason"], machine["health"]),
                                 ("unknown", "reported-service-unknown", "degraded"))
        machine = rows([record("second-pc", "windows", health="degraded", complete=False)])[-1]
        self.assertEqual((machine["state"], machine["reason"]), ("up", "reported-partly-serving"))
        self.assertIsNone(machine["loadedCount"])

    def test_unconfigured_lanes_are_not_public_models(self):
        undeclared = probes.probe_lane(probes.unconfigured_lane("deep"), lambda *args: self.fail("unconfigured fetch"))
        for lanes, ids in (([undeclared], []),
                           ([{"id": "fast", "status": "idle", "expectedModel": "real-model"}, undeclared], ["real-model"])):
            with self.subTest(ids=ids):
                snapshot = {"lanes": lanes}
                for public in (probes.pc_status(snapshot), probes.peer_presence(snapshot, "pc"),
                               probes.peer_link_status_doc(snapshot, "pc")):
                    self.assertEqual([m["id"] for m in public["models"]], ids)
                self.assertIn("lanes not configured: deep", probes.pc_status(snapshot)["limitations"])
                self.assertEqual(lanes[-1]["status"], "undeclared")


class IncompleteHandsOff(unittest.TestCase):
    def flag(self, gpus, notification=5, **kwargs):
        return probes.hands_off(gpus, gpu_live=True, lane_pids=frozenset({7708}),
                                notification_state=notification, **kwargs)

    def test_unknown_fullscreen_cannot_claim_only_agiw(self):
        flag = self.flag([gpu(90, [{"pid": 7708, "percent": 90}])], notification=None)
        self.assertEqual((flag["active"], flag["cause"], flag["agiwOnly"]), (True, "unknown", None))
        self.assertNotIn("AGIW's own lane work", flag["detail"])
        for public in (probes.pc_status({"handsOff": flag}), probes.peer_presence({"handsOff": flag}, "pc")):
            self.assertEqual(public["handsOff"]["unknown"], ["fullscreen-unknown"])
            self.assertIsNone(public["handsOff"]["agiwOnly"])

    def test_an_unread_gpu_cannot_claim_only_agiw(self):
        flag = self.flag([gpu(90, [{"pid": 7708, "percent": 90}]), gpu(None, index=100)])
        self.assertEqual((flag["active"], flag["cause"], flag["agiwOnly"]), (True, "unknown", None))
        self.assertEqual(flag["unknown"], ["gpu-load-unknown"])

    def test_unknown_busy_attribution_is_not_mixed(self):
        for known_pid in (7708, 9999):
            with self.subTest(known_pid=known_pid):
                flag = self.flag([gpu(90, [{"pid": known_pid, "percent": 90}]), gpu(80, index=100)])
                self.assertEqual((flag["active"], flag["cause"], flag["agiwOnly"]), (True, "unknown", None))

    def test_known_other_and_agiw_gpus_are_mixed(self):
        flag = self.flag([gpu(90, [{"pid": 7708, "percent": 90}]), gpu(80, [{"pid": 9999, "percent": 80}], index=100)])
        self.assertEqual((flag["cause"], flag["agiwOnly"]), ("mixed", False))

    def test_incomplete_inventory_keeps_busy_evidence_but_never_clear(self):
        for load, active in ((3, None), (90, True)):
            with self.subTest(load=load):
                flag = self.flag([gpu(load, [{"pid": 7708, "percent": load}])], gpu_complete=False)
                self.assertIs(flag["active"], active)
                self.assertIsNone(flag["agiwOnly"])
                self.assertIn("gpu-load-unknown", flag["unknown"])

    def test_incomplete_engine_shares_cannot_be_only_agiw(self):
        flag = self.flag([{**gpu(90, [{"pid": 7708, "percent": 90}]), "engineSharesComplete": False}])
        self.assertEqual((flag["cause"], flag["agiwOnly"]), ("unknown", None))

    def test_store_app_is_not_described_as_fullscreen(self):
        flag = self.flag([gpu(3)], notification=7)
        self.assertIs(flag["active"], True)
        self.assertEqual(flag["reasons"], ["store-app"])
        self.assertIn("a Windows Store app is running", flag["detail"])
        self.assertNotIn("full-screen", flag["detail"])


class PeerAndPairEvidence(unittest.TestCase):
    def test_stray_linux_share_report_remains_a_peer_not_the_linux_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = probes.peer_dir(root)
            directory.mkdir(parents=True)
            doc = {"schemaVersion": 1, "kind": "agiw-peer", "host": "anything", "platform": "linux",
                   "observedUnix": 999.0, "models": [], "modelsTotal": 0, "health": "down"}
            (directory / "anything.json").write_text(json.dumps(doc))
            peers, _ = probes.read_peers(root, "pc", 1000.0)
        self.assertEqual([peer["host"] for peer in peers], ["anything"])
        self.assertFalse(any(row["platform"] == "linux" for row in rows(peers)))
        relayed = rows([*peers, record("relayed-linux", "linux", age=4.0, relayed="mac")])
        self.assertFalse(any(row["platform"] == "linux" for row in relayed))

    def test_incomplete_pair_census_stays_unknown_when_published(self):
        component = probes.pair_component({"ok": True, "processes": [], "listeners": [], "uninspected": 2})
        for public in (probes.pair_public(component), probes.pc_status({"components": [component]})["pair"],
                       probes.peer_presence({"components": [component]}, "pc")["pair"]):
            self.assertEqual(public["state"], "unknown")
            self.assertEqual(public["uninspected"], 2)
            self.assertIsNone(public["lanExposed"])
            self.assertIsNone(public["processCount"])
            self.assertIsNone(public["engineCount"])

    def test_running_pair_with_partial_census_cannot_rule_out_lan_exposure(self):
        public = probes.pair_public(probes.pair_component({
            "ok": True, "processes": [{"pid": 1, "name": "PAIR.exe", "owner": "pair"}],
            "listeners": [{"pid": 1, "port": 8081, "local": "127.0.0.1"}], "uninspected": 2}))
        self.assertEqual((public["state"], public["processCount"], public["uninspected"]), ("running", None, 2))
        self.assertIsNone(public["lanExposed"])


if __name__ == "__main__":
    unittest.main()
