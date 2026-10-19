"""Mac share evidence in complete Windows samples, using only a temporary share."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402
from agiw_win.server import Handler  # noqa: E402


class NoLaneProcesses:
    def sample(self, _ports):
        return {}, None, None


class MacPeerEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.share = Path(temporary.name)
        self.now = 1000.0
        self.peer_path = probes.peer_dir(self.share) / "test-mac.json"
        self.peer_path.parent.mkdir(parents=True)
        self.hosts_path = self.share / "llm-lab" / "mac-reach" / "hosts.json"
        self.hosts_path.parent.mkdir(parents=True)
        self.hosts_path.write_text(json.dumps({"mac": {"hostname": "test-mac.local"}}), encoding="utf-8")

        def no_fetch(*_args):
            raise AssertionError("An undeclared lane must not make an HTTP request")

        self.collector = Collector(
            self.share, self.share / "bin", host="test-pc", clock=lambda: self.now,
            fetch=no_fetch, gpu_fn=lambda: ([], probes._source("pc-gpu", "PC GPU", "unavailable", "Test")),
            adapters_fn=lambda: [], memory_fn=lambda: ({"level": "unknown"}, None),
            runs_root=self.share / "runs", lane_processes=NoLaneProcesses(),
        )
        self.addCleanup(self.collector.close)

    def report(self, *states):
        self.peer_path.write_text(json.dumps({
            "schemaVersion": 1, "kind": "agiw-peer", "host": "test-mac",
            "platform": "macos", "edition": "mac", "observedUnix": 1000.0,
            "modelsTotal": len(states),
            "models": [{"id": f"model-{index}", "loadedState": state}
                       for index, state in enumerate(states)],
        }), encoding="utf-8")

    def test_unknown_or_partial_residency_never_becomes_a_known_total(self):
        for states in [("unknown",), ("loaded", "unknown"), ("inactive", "unknown"), ("malformed",)]:
            with self.subTest(states=states):
                self.report(*states)
                snapshot = self.collector.collect()
                self.assertEqual(snapshot["host"], "windows")
                self.assertEqual(snapshot["macPeer"]["state"], "linked")
                self.assertIsNone(snapshot["macPeer"]["loadedCount"])

    def test_an_empty_share_inventory_does_not_prove_zero_loaded(self):
        self.report()
        mac = self.collector.collect()["macPeer"]
        self.assertEqual(mac["state"], "linked")
        self.assertEqual(mac["models"], [])
        self.assertIsNone(mac["loadedCount"])

    def test_complete_residency_retains_loaded_and_zero_loaded_counts(self):
        for states, count in [(("loaded", "inactive"), 1), (("inactive",), 0)]:
            with self.subTest(states=states):
                self.report(*states)
                mac = self.collector.collect()["macPeer"]
                self.assertEqual(mac["loadedCount"], count)
                self.assertEqual(mac["models"][0]["state"], "loaded" if count else "not-loaded")

    def test_loaded_list_can_be_complete_without_a_downloaded_inventory(self):
        for states in ((), ("loaded",), ("inactive",)):
            with self.subTest(states=states):
                self.report(*states)
                doc = json.loads(self.peer_path.read_text())
                doc.pop("modelsTotal")
                doc["loadedListComplete"] = True
                self.peer_path.write_text(json.dumps(doc))
                snapshot = self.collector.collect()
                self.assertFalse(snapshot["peers"][0]["modelsComplete"])
                self.assertTrue(snapshot["peers"][0]["loadedListComplete"])
                self.assertEqual(snapshot["macPeer"]["loadedCount"], states.count("loaded"))

    def test_a_share_report_does_not_claim_why_the_lan_probe_did_not_answer(self):
        self.report("loaded")
        mac = self.collector.collect()["macPeer"]
        self.assertFalse(mac["verified"])
        self.assertEqual(mac["via"], "sharedchami")
        self.assertIsNone(mac["latencyMs"])
        self.assertIsNone(mac["address"])
        self.assertIn("does not probe the Mac over the LAN", mac["detail"])
        self.assertNotIn("not open to the LAN", mac["detail"])

    def test_share_residency_expires_after_thirty_seconds(self):
        self.report("loaded")
        self.now = 1030.0
        self.assertEqual(self.collector.collect()["macPeer"]["state"], "linked")
        self.now = 1030.2
        snapshot = self.collector.collect()
        self.assertEqual(snapshot["sampledAt"], self.now)
        self.assertEqual(snapshot["macPeer"]["state"], "unknown")
        self.assertFalse(snapshot["peers"][0]["fresh"])
        self.assertEqual(snapshot["macPeer"]["models"], [])

    def test_the_mac_view_is_presence_only_and_sends_nothing(self):
        # no_fetch raises on any HTTP request, so every collect() here also proves the observer contacts nothing.
        for share_present in (False, True):
            if share_present:
                self.report("loaded")
            with self.subTest(share_present=share_present):
                snapshot = self.collector.collect()
                mac = snapshot["macPeer"]
                self.assertEqual(snapshot["sampledAt"], self.now)
                self.assertEqual(mac["state"], "linked" if share_present else "unknown")
                self.assertEqual(mac["reason"], None if share_present else "no-report")
                self.assertEqual([model["id"] for model in mac["models"]], ["model-0"] if share_present else [])
                self.assertEqual((mac["address"], mac["latencyMs"], mac["verified"]), (None, None, False))
                self.assertNotIn("mac-peer", [s["id"] for s in snapshot["sources"]], "the LAN probe source is gone")
                presence = next(s for s in snapshot["sources"] if s["id"] == "mac-presence")
                self.assertEqual(presence["state"], "live" if share_present else "unavailable")
                handler = Mock()
                Handler.json_reply(handler, 200, snapshot)
                status, payload, mime = handler.reply.call_args.args
                self.assertEqual((status, mime), (200, "application/json"))
                self.assertEqual(json.loads(payload)["sampledAt"], self.now)

    def test_windows_model_control_remains_unavailable_with_plain_explanation(self):
        snapshot = self.collector.collect()
        self.assertEqual(snapshot["host"], "windows")
        self.assertEqual(snapshot["modelControl"], {
            "supported": False,
            "reason": "This Windows dashboard monitors models only. Load and unload are unavailable.",
        })

    def test_configured_name_names_the_mac_with_or_without_its_report(self):
        for host, expected, reason in (("test-mac", "linked", None), ("other-mac", "unknown", "no-report")):
            with self.subTest(host=host):
                self.report("loaded")
                doc = json.loads(self.peer_path.read_text(encoding="utf-8"))
                doc["host"] = host
                self.peer_path.unlink()
                self.peer_path = self.peer_path.with_name(host + ".json")
                self.peer_path.write_text(json.dumps(doc), encoding="utf-8")
                snapshot = self.collector.collect()
                self.assertEqual([p["host"] for p in snapshot["peers"]], [host])
                mac = snapshot["macPeer"]
                self.assertEqual((mac["name"], mac["configuredName"]), ("test-mac.local", "test-mac.local"))
                self.assertEqual((mac["state"], mac["reason"]), (expected, reason))
                self.assertEqual([m["id"] for m in mac["models"]], ["model-0"] if host == "test-mac" else [])

    def test_no_configured_name_never_links_even_a_single_fresh_mac(self):
        self.hosts_path.unlink()
        self.report("loaded")
        snapshot = self.collector.collect()
        self.assertEqual(len(snapshot["peers"]), 1)
        self.assertTrue(snapshot["peers"][0]["fresh"])
        self.assertEqual(snapshot["macPeer"]["state"], "unknown")
        self.assertEqual(snapshot["macPeer"]["models"], [])

    def test_report_host_matching_ignores_case_and_domain(self):
        self.report("loaded")
        doc = json.loads(self.peer_path.read_text(encoding="utf-8"))
        doc["host"] = "TEST-MAC.example"
        self.peer_path.unlink()
        self.peer_path = self.peer_path.with_name("TEST-MAC.example.json")
        self.peer_path.write_text(json.dumps(doc), encoding="utf-8")
        self.assertEqual(self.collector.collect()["macPeer"]["state"], "linked")

    def test_unknown_mac_states_name_their_reason(self):
        self.report("loaded")
        self.now = 1031.0
        mac = self.collector.collect()["macPeer"]
        self.assertEqual((mac["state"], mac["reason"]), ("unknown", "stale"))
        self.assertIn("31 s old", mac["detail"])
        self.hosts_path.unlink()
        mac = self.collector.collect()["macPeer"]
        self.assertEqual((mac["state"], mac["reason"], mac["name"]), ("unknown", "unconfigured", None))

    def test_freshness_expires_before_display_age_rounding(self):
        self.report("loaded")
        self.now = 1030.049
        snapshot = self.collector.collect()
        self.assertEqual(snapshot["peers"][0]["ageSeconds"], self.now - 1000.0)
        self.assertFalse(snapshot["peers"][0]["fresh"])
        self.assertEqual(snapshot["macPeer"]["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
