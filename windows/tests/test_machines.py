"""PLAN A3: machines[] (this PC and the Mac). Linux is retired and is not a machine row."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402
sys.path.append(str(Path(__file__).resolve().parents[2]))  # the shared root modules, as the installed layout has them
import machine_state  # noqa: E402
import remote_machines  # noqa: E402

NOW = 2_000_000_000.0
UP_LANES = [{"id": "fast", "status": "idle"}, {"id": "deep", "status": "busy"}]


def record(host, platform, age=2.0, health="ok", models=None, relayed=None, source="agiw-peers", complete=True):
    return {"host": host, "platform": platform, "edition": platform, "version": "1", "health": health,
            "models": models if models is not None else [{"id": "m", "loadedState": "loaded", "activity": "idle"}],
            "modelsComplete": complete, "ageSeconds": age, "fresh": age <= 30, "source": source,
            "observedUnix": NOW - age, "relayedBy": relayed, "verified": False}


def rows(peers=(), mac_name="target-mac.local", lanes=UP_LANES, hands=None, paired=None, sampled=NOW):
    return probes.machine_rows(host="test-pc", sampled_at=sampled, lanes=lanes, hands=hands, version="0.2.0",
                               peers=list(peers), mac_name=mac_name, paired=paired)


def machine_state_time(text):
    import datetime as _dt
    return _dt.datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


class MachineRows(unittest.TestCase):
    def test_shared_registry_projection_preserves_this_pc_and_replaces_only_remote_reports(self):
        config = {"schemaVersion": 1, "machines": [{"id": "worker", "label": "Registry worker",
                  "replaces": "legacy-pc", "lanes": [{"id": "fast", "label": "Fast", "localPort": 21001}]}]}
        nodes = remote_machines.collect(config, now=NOW,
            fetch=lambda _: (200, '{"data":[{"id":"vendor/model"}]}', 1))
        local = rows()[0]
        snapshot = {"machines": [local, {"host": "old-pc", "platform": "windows", "state": "down",
                                      "trust": "share-reported"}], "inference": {"nodes": nodes}}
        remote_machines.project_snapshot(snapshot)
        self.assertEqual([row["host"] for row in snapshot["machines"]], ["test-pc", "registry:worker"])
        self.assertIs(snapshot["machines"][0], local)
        registry = snapshot["machines"][1]
        self.assertEqual((registry["state"], registry["lanes"][0]["modelId"]), ("up", "vendor/model"))
        self.assertFalse(registry["modelsComplete"], "configured lane health is not whole-machine inventory")
        json.dumps(snapshot, allow_nan=False)

    def test_shared_registry_unknown_and_missing_registry_keep_their_evidence(self):
        legacy = {"host": "old-pc", "platform": "windows", "state": "down", "trust": "share-reported"}
        snapshot = {"machines": [dict(legacy)]}
        remote_machines.project_snapshot(snapshot)
        self.assertEqual(snapshot, {"machines": [legacy]})
        config = {"schemaVersion": 1, "machines": [{"id": "worker", "label": "Registry worker",
                  "replaces": "legacy-pc", "lanes": [{"id": "fast", "label": "Fast", "localPort": 21001}]}]}
        snapshot["inference"] = {"nodes": remote_machines.unavailable_nodes(config, now=NOW)}
        remote_machines.project_snapshot(snapshot)
        self.assertEqual(len(snapshot["machines"]), 1)
        registry = snapshot["machines"][0]
        self.assertEqual((registry["state"], registry["lanes"][0]["reason"]), ("unknown", "collector_failed"))
        self.assertIsNone(registry["lanes"][0]["modelId"])

    def test_expected_machines_are_this_pc_and_the_mac(self):
        out = rows()
        self.assertEqual([(r["platform"], r["role"]) for r in out],
                         [("windows", "this-machine"), ("mac", "peer")])  # the shared schema says "mac"
        self.assertEqual([(r["state"], r["reason"]) for r in out],
                         [("up", "serving"), ("unknown", "no-report")])
        self.assertEqual(out[1]["name"], "target-mac.local")
        self.assertNotIn("linux", [row["platform"] for row in out])
        for row in out[1:]:
            self.assertEqual((row["health"], row["loadedCount"], row["ageSeconds"]), ("unknown", None, None))

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
        self.assertEqual(unconfigured["reasonCode"], "not_configured")  # not "never observed": nothing is set up

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

    def test_unsigned_linux_relay_does_not_become_a_machine(self):
        for age in (6.5, 31.0):
            out = rows([record("linux-node-1", "linux", age=age, relayed="mac")])
            self.assertEqual([row["platform"] for row in out], ["windows", "mac"])
            self.assertTrue(all(row.get("relayedBy") is None for row in out))
            self.assertIsNone(out[1]["loadedCount"])

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


class SharedRowSchema(unittest.TestCase):
    """machines[] rows also speak the shared (Mac) row schema that the shared pages read."""
    SHARED = {"id", "host", "platform", "state", "reasonCode", "trust", "observedAt", "ageSeconds", "freshForSeconds",
              "modelsComplete", "loadedCount", "busyCount", "promptTokensPerSecond", "generationTokensPerSecond",
              "handsOff"}

    def check(self, row):
        self.assertLessEqual(self.SHARED, set(row), sorted(self.SHARED - set(row)))
        self.assertIn(row["platform"], ("windows", "mac"))
        if row["state"] == "up":
            self.assertIn(row["reasonCode"], ("observed", "reported_degraded"))
        elif row["state"] == "down":
            self.assertEqual(row["reasonCode"], "reported_down")
        else:
            self.assertIn(row["reasonCode"], machine_state.UNKNOWN_REASONS)
        self.assertEqual(row["freshForSeconds"], 3.0 if row["role"] == "this-machine" else 30.0)
        if row["state"] != "up":
            self.assertEqual((row["loadedCount"], row["busyCount"], row["modelsComplete"]), (None, None, False))
        for key in ("promptTokensPerSecond", "generationTokensPerSecond"):
            self.assertIsNone(row[key])
        for key in ("loadedCount", "busyCount"):
            self.assertTrue(row[key] is None or (type(row[key]) is int and row[key] >= 0), (key, row[key]))
        # A count is claimed only with a complete list; never a complete list without a count.
        self.assertEqual(row["modelsComplete"], row["loadedCount"] is not None)
        if row["busyCount"] is not None:
            self.assertIsNotNone(row["loadedCount"])
        if row["state"] != "unknown":
            self.assertIsInstance(row["observedAt"], str)  # a live row must be able to age

    def test_every_row_kind_carries_the_shared_fields(self):
        for peers in ([], [record("target-mac", "macos")], [record("target-mac", "macos", age=60.0)],
                      [record("target-mac", "macos", health="down")], [record("target-mac", "macos", health="degraded")],
                      [record("other-pc", "windows"), record("target-mac", "macos")],
                      [record("target-mac", "macos", complete=False)]):
            for mac_name in ("target-mac.local", None):
                for lanes in (UP_LANES, [], [{"id": "fast", "status": "down"}], [{"id": "f", "status": "slots_unknown"}]):
                    with self.subTest(peers=len(peers), mac=mac_name, lanes=len(lanes)):
                        for row in rows(peers, mac_name=mac_name, lanes=lanes):
                            self.check(row)

    def test_the_degraded_pc_is_up_but_not_plain_observed(self):
        degraded = rows(lanes=[{"id": "fast", "status": "idle"}, {"id": "deep", "status": "loading"}])[0]
        self.assertEqual((degraded["state"], degraded["reason"], degraded["reasonCode"]),
                         ("up", "partly-serving", "reported_degraded"))
        peer = rows([record("target-mac", "macos", health="degraded")])[1]
        self.assertEqual((peer["state"], peer["reasonCode"]), ("up", "reported_degraded"))

    def test_counts_follow_what_is_known(self):
        pc = rows()[0]  # one idle lane and one busy lane, both answering
        self.assertEqual((pc["loadedCount"], pc["busyCount"], pc["modelsComplete"]), (2, 1, True))
        slots_unknown = rows(lanes=[{"id": "fast", "status": "idle"}, {"id": "deep", "status": "slots_unknown"}])[0]
        self.assertEqual((slots_unknown["loadedCount"], slots_unknown["busyCount"], slots_unknown["modelsComplete"]),
                         (2, None, True))
        down = rows(lanes=[{"id": "fast", "status": "idle"}, {"id": "deep", "status": "down"}])[0]
        self.assertEqual((down["loadedCount"], down["busyCount"], down["modelsComplete"]), (None, None, False))
        mac = rows([record("target-mac", "macos", models=[
            {"id": "a", "loadedState": "loaded", "activity": "generating"},
            {"id": "b", "loadedState": "loaded", "activity": "idle"},
            {"id": "c", "loadedState": "inactive", "activity": "unknown"}])])[1]
        self.assertEqual((mac["loadedCount"], mac["busyCount"], mac["modelsComplete"]), (2, 1, True))
        unknown_activity = rows([record("target-mac", "macos", models=[
            {"id": "a", "loadedState": "loaded", "activity": "unknown"}])])[1]
        self.assertEqual((unknown_activity["loadedCount"], unknown_activity["busyCount"]), (1, None))

    def test_every_reason_word_the_pc_can_emit_has_a_code(self):
        words = {reason for _, reason in probes.MACHINE_REASONS.values()}
        words |= {"health-unknown", "reported-service-unknown", "stale", "no-report", "unconfigured", "config-unreadable",
                  "clock-ahead", "report-refused", "peers-unreadable", "report-unreadable", "scan-truncated"}
        for word in sorted(words):
            with self.subTest(reason=word):
                self.assertIn(word, probes.MACHINE_REASON_CODES)
                code = probes.MACHINE_REASON_CODES[word]
                self.assertTrue(code in machine_state.UNKNOWN_REASONS or code in (
                    "observed", "reported_degraded", "reported_down"), code)

    def test_a_stale_mac_report_keeps_its_stale_reason_when_the_peer_source_is_unavailable(self):
        snapshot = {"sources": [{"id": "lane-inventory", "state": "live"},
                                {"id": "agiw-peers", "state": "unavailable"}],
                    "machines": rows([record("target-mac", "macos", age=7200.0)]), "lanes": [], "models": []}
        mac = probes.enforce_snapshot_evidence(snapshot)["machines"][1]
        self.assertEqual((mac["state"], mac["reason"], mac["reasonCode"]), ("unknown", "stale", "stale"))
        self.assertIsNone(mac["handsOff"])

    def test_observed_at_keeps_the_milliseconds(self):
        out = rows([record("target-mac", "macos", age=2.0)], sampled=NOW + 0.75)
        self.assertRegex(out[0]["observedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertRegex(out[1]["observedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        # Whole-second floor would put the row up to 1 s further in the past; the milliseconds put it back.
        parsed = machine_state_time(out[0]["observedAt"])
        self.assertAlmostEqual(parsed, NOW + 0.75, delta=0.001)

    def test_a_demoted_row_does_not_keep_saying_observed(self):
        snapshot = {"sources": [{"id": "lane-inventory", "state": "unavailable"},
                                {"id": "agiw-peers", "state": "unavailable"}],
                    "machines": rows([record("target-mac", "macos")]), "lanes": [], "models": [], "handsOff": {"active": True}}
        out = probes.enforce_snapshot_evidence(snapshot)["machines"]
        for row in out:
            self.assertEqual((row["state"], row["reason"], row["reasonCode"]), ("unknown", "evidence-incomplete", "unavailable"))
            self.assertEqual((row["loadedCount"], row["busyCount"], row["modelsComplete"]), (None, None, False))
            self.assertIsNone(row["promptTokensPerSecond"])
        self.assertIsNone(out[1]["handsOff"])

    def test_the_pcs_own_row_goes_unknown_after_three_seconds_without_a_sample(self):
        out = rows([record("target-mac", "macos")])
        self.assertEqual(out[0]["freshForSeconds"], 3.0)
        later = machine_state.expire_machines(json.loads(json.dumps(out)), NOW + 4.0)
        self.assertEqual((later[0]["state"], later[0]["reasonCode"], later[0]["busyCount"]), ("unknown", "stale", None))
        self.assertEqual(later[1]["state"], "up")  # the Mac's report still has its 30 s window

    def test_the_mac_row_ages_at_read_time_like_the_macs_own(self):
        out = rows([record("target-mac", "macos")])
        later = NOW + 45.0  # past the 30 s window with no new sample
        aged = machine_state.expire_machines(json.loads(json.dumps(out)), later)
        for row in aged:
            self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "stale"), row["id"])
            self.assertEqual((row["loadedCount"], row["busyCount"], row["modelsComplete"]), (None, None, False))
        # Inside the window nothing changes.
        fresh = machine_state.expire_machines(json.loads(json.dumps(out)), NOW + 2.0)
        self.assertEqual([r["state"] for r in fresh], [r["state"] for r in out])


class WindowsStoreAging(unittest.TestCase):
    """The Windows store ages machines[] and activityKnown on read and stream, like the Mac store."""

    def snapshot(self, sampled):
        return {"sampledAt": sampled, "fullSampledAt": sampled, "activityKnown": True,
                "machines": rows(sampled=sampled), "inference": None}

    def store(self, sampled):
        from agiw_win import server
        store = server.SnapshotStore()
        store.snapshot = self.snapshot(sampled)
        return server, store

    def test_read_ages_a_stale_sample(self):
        import time
        server, store = self.store(time.time() - 120.0)
        data = store.read()
        self.assertIs(data["activityKnown"], False)
        self.assertTrue(all(row["state"] == "unknown" and row["reasonCode"] == "stale" for row in data["machines"][:1]))
        self.assertEqual(data["machines"][0]["loadedCount"], None)
        # The PC's own words agree with the aged state; they no longer say the lanes are serving.
        local = data["machines"][0]
        self.assertEqual((local["reason"], local["health"]), ("stale", "unknown"))
        self.assertNotIn("answers as declared", local["detail"])
        # The stored snapshot is not changed by reading.
        self.assertEqual(store.snapshot["machines"][0]["state"], "up")

    def test_read_keeps_a_fresh_sample(self):
        import time
        server, store = self.store(time.time())
        data = store.read()
        self.assertIs(data["activityKnown"], True)
        self.assertEqual(data["machines"][0]["state"], "up")


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
                         [("windows", "unknown", "lane-state-unknown"), ("mac", "up", "reported-serving")])
        self.assertNotIn("linux", [m["platform"] for m in machines])
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
