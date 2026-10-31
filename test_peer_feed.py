import hmac
import json
import os
import pwd
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import peer_feed
import peer_link
from machine_state import machines_summary


def presence(age=2.0, platform="windows", **extra):
    now = time.time() - age
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": "winpc01", "platform": platform,
            "observedAt": peer_link._iso(now), "observedUnix": now, "health": "ok",
            "models": [{"id": "gpt-oss-20b", "loadedState": "loaded", "activity": "idle", "slotsBusy": 0,
                        "slotsTotal": 2, "liveTokensPerSecond": 104.27, "secretish": "/C:/Users/x"}],
            "modelsTotal": 1,
            "gpus": [{"name": "RTX 3080 Ti", "utilizationPercent": 4.0, "path": "C:\\nope"}],
            "memory": {"level": "ok", "availablePercent": 55.6, "commit": 1}, **extra}


class PerHostFeeds(unittest.TestCase):
    def test_invalid_mac_share_schema_is_not_hidden_by_unverified_trust(self):
        now = time.time()
        for platform in ("mac", "macos", "linux"):
            for version in (999, True, None):
                doc = presence(age=0, platform=platform, host="other-host", schemaVersion=version)
                doc.pop("models")
                for via in ("share", "lan"):
                    with self.subTest(platform=platform, version=version, via=via):
                        entry = peer_feed.summarize_document(doc, via, now)
                        self.assertIsNone(entry["feed"])
                        self.assertEqual(entry["source"]["reasonCode"], "schema_invalid")

    def test_mac_share_health_is_only_an_unverified_diagnostic(self):
        now = time.time()
        for platform in ("mac", "macos"):
            for health in ("ok", "degraded", "down"):
                with self.subTest(platform=platform, health=health):
                    doc = presence(age=0, platform=platform, host="other-mac", health=health)
                    entry = peer_feed.summarize_document(doc, "share", now)
                    rows = machines_summary({"sampledAt": now, "models": [], "sources": []}, [entry])
                    row = next(row for row in rows if row["host"] == "other-mac")
                    self.assertEqual((row["state"], row["reasonCode"]), ("unknown", "unverified"))
                    self.assertEqual(row["trust"], "unverified")
                    self.assertEqual(row["observedAt"], doc["observedAt"])
                    self.assertIsNone(row["loadedCount"])
                    self.assertIsNone(row["busyCount"])
                    self.assertIsNone(entry["feed"])
                    self.assertEqual(entry["source"]["via"], "share")

    def test_authenticated_mac_health_and_counts_are_accepted(self):
        now = time.time()
        for via in ("lan", "ssh"):
            for health, state in (("ok", "up"), ("degraded", "up"), ("down", "down")):
                with self.subTest(via=via, health=health):
                    entry = peer_feed.summarize_document(
                        presence(age=0, platform="macos", host="other-mac", health=health), via, now)
                    rows = machines_summary({"sampledAt": now, "models": [], "sources": []}, [entry])
                    row = next(row for row in rows if row["host"] == "other-mac")
                    self.assertEqual((row["state"], row["trust"]), (state, "authenticated"))
                    self.assertEqual(row["loadedCount"], 1 if state == "up" else None)
                    self.assertEqual(row["busyCount"], 0 if state == "up" else None)

    def test_identity_fault_survives_unsigned_share_ok_and_down(self):
        for health in ("ok", "down"):
            with self.subTest(health=health), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                (state / "control-token").write_text("fixture")
                node = "00000000-0000-4000-8000-000000000001"
                peer = {"node": node, "paired": True, "host": "windows", "state": "connected",
                        "status": presence(host="pc1")}
                with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": [peer]})):
                    peer_feed.from_peer_link_hosts(time.time(), state)
                    peer["status"] = presence(host="pc2")
                    share = peer_feed.summarize_document(presence(host="pc1", health=health), "share", time.time())
                    rows = peer_feed.peer_feeds(time.time(),
                        peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state), share_reader=lambda _: [share])
                windows = [row for row in rows if row["source"]["platform"] == "windows"]
                self.assertEqual(len(windows), 1)
                self.assertIsNone(windows[0]["feed"])
                self.assertEqual(windows[0]["source"]["reasonCode"], "host_key_mismatch")
                self.assertEqual(windows[0]["source"]["node"], node)

    def test_first_label_claim_cannot_reject_other_uuid_in_either_order(self):
        nodes = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
        for order in (nodes, list(reversed(nodes))):
            with self.subTest(order=order), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                (state / "control-token").write_text("fixture")
                peers = [{"node": node, "paired": True, "host": "windows", "state": "connected",
                          "status": presence(host="pc-b")} for node in order]
                with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": peers})):
                    rows = peer_feed.from_peer_link_hosts(time.time(), state)
                self.assertEqual({row["source"]["host"] for row in rows}, set(nodes))
                self.assertTrue(all(row["feed"] is not None for row in rows))
                self.assertTrue(all(row["source"]["label"] == "pc-b (reported)" for row in rows))

    def test_cold_offline_pair_and_share_is_one_uncertain_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            node = "00000000-0000-4000-8000-000000000001"
            peer = {"node": node, "paired": True, "host": "windows", "name": "pc1", "state": "offline"}
            share = peer_feed.summarize_document(presence(host="pc1"), "share", time.time())
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": [peer]})):
                rows = peer_feed.peer_feeds(time.time(),
                    peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state), share_reader=lambda _: [share])
            windows = [row for row in rows if row["source"]["platform"] == "windows"]
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0]["source"]["node"], node)
            self.assertIsNone(windows[0]["feed"])
            self.assertEqual(windows[0]["source"]["reasonCode"], "not_connected")
            self.assertEqual(windows[0]["source"]["diagnostics"][0]["correlation"], "unverified")

    def test_invalid_modern_and_legacy_fallback_one_machine_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agiw-peers").mkdir()
            (root / "agiw-peers/pc1.json").write_text(json.dumps(presence(host="pc1", health="bad")))
            (root / "windows-status.json").write_text(json.dumps(presence(host="windows", kind=None)))
            shares = peer_feed.from_share_hosts(time.time(), root, root)
            rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda _: [], share_reader=lambda _: shares)
            windows = [row for row in rows if row["source"]["platform"] == "windows"]
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0]["feed"]["health"], "ok")
            self.assertEqual(windows[0]["source"]["diagnostics"][0]["reasonCode"], "schema_invalid")
            self.assertEqual(windows[0]["source"]["diagnostics"][0]["host"], "pc1")

    def test_cold_pair_modern_error_and_legacy_share_stay_one_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agiw-peers").mkdir()
            (root / "agiw-peers/pc1.json").write_text(json.dumps(presence(host="pc1", health="bad")))
            (root / "windows-status.json").write_text(json.dumps(presence(host="windows", kind=None, health="down")))
            node = "00000000-0000-4000-8000-000000000001"
            paired = peer_feed._feed_failure("windows", node, "not_connected")
            paired["source"]["node"] = node
            shares = peer_feed.from_share_hosts(time.time(), root, root)
            rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda _: [paired], share_reader=lambda _: shares)
            windows = [row for row in rows if row["source"]["platform"] == "windows"]
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0]["source"]["node"], node)
            self.assertIsNone(windows[0]["feed"])
            self.assertEqual(windows[0]["source"]["reasonCode"], "not_connected")
            diagnostics = windows[0]["source"]["diagnostics"]
            self.assertEqual(len(diagnostics), 2)
            self.assertEqual(diagnostics[0]["reportedHealth"], "down")
            self.assertEqual(diagnostics[1]["reasonCode"], "schema_invalid")
            self.assertTrue(all(item["correlation"] == "unverified" for item in diagnostics))

    def test_multiple_cold_pairs_keep_bounded_ambiguous_share_diagnostics(self):
        now = time.time()
        nodes = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
        pairs = [peer_feed._feed_failure("windows", node, "not_connected") for node in nodes]
        for row, node in zip(pairs, nodes):
            row["source"]["node"] = node
        shares = [peer_feed.summarize_document(presence(host=f"pc{index}"), "share", now)
                  for index in range(peer_feed.MAX_FEED_DIAGNOSTICS + 4)]
        rows = peer_feed.peer_feeds(now, peer_reader=lambda _: pairs, share_reader=lambda _: shares)
        windows = [row for row in rows if row["source"]["platform"] == "windows"]
        self.assertEqual({row["source"]["node"] for row in windows}, set(nodes))
        self.assertTrue(all("diagnostics" not in row["source"] for row in pairs))
        for row in windows:
            self.assertIsNone(row["feed"])
            self.assertEqual(len(row["source"]["diagnostics"]), peer_feed.MAX_FEED_DIAGNOSTICS)
            self.assertTrue(row["source"]["diagnosticsTruncated"])
            self.assertTrue(all(item["candidateNodes"] == nodes and item["correlation"] == "unverified"
                                for item in row["source"]["diagnostics"]))

    def test_projected_hands_off_beats_opposite_unsigned_share_flag(self):
        for hands_off in (True, False):
            with self.subTest(hands_off=hands_off), tempfile.TemporaryDirectory() as directory:
                now = time.time()
                state = Path(directory)
                (state / "control-token").write_text("fixture")
                node = "00000000-0000-4000-8000-000000000001"
                projected = peer_link.project_status(presence(host="pc1", handsOff=hands_off))
                peer = {"node": node, "paired": True, "host": "windows", "state": "connected", "status": projected}
                share = peer_feed.summarize_document(presence(host="pc1", handsOff=not hands_off), "share", now)
                with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": [peer]})):
                    rows = peer_feed.peer_feeds(now,
                        peer_reader=lambda stamp: peer_feed.from_peer_link_hosts(stamp, state), share_reader=lambda _: [share])
                windows = [row for row in rows if row["source"]["platform"] == "windows"]
                self.assertEqual(len(windows), 1)
                self.assertIs(windows[0]["feed"]["handsOff"], hands_off)
                self.assertEqual(windows[0]["feed"]["trust"], "authenticated")

    def test_linux_share_cannot_replace_authenticated_failure(self):
        now = time.time()
        for health in ("ok", "down"):
            share = peer_feed.summarize_document(presence(host="linux1", platform="linux", health=health), "share", now)
            failure = peer_feed._feed_failure("linux", "linux1", "host_key_mismatch")
            rows = peer_feed.peer_feeds(now, peer_reader=lambda _: [failure], share_reader=lambda _: [share])
            linux = [r for r in rows if r["source"]["platform"] == "linux"]
            self.assertEqual(len(linux), 1)
            self.assertIsNone(linux[0]["feed"])
            self.assertEqual(linux[0]["source"]["reasonCode"], "host_key_mismatch")

    def test_linux_share_does_not_hide_transport_exception(self):
        now = time.time()
        share = peer_feed.summarize_document(presence(host="linux1", platform="linux"), "share", now)
        def failed(_):
            raise peer_feed.FeedUnavailable("timeout")
        rows = peer_feed.peer_feeds(now, peer_reader=failed, share_reader=lambda _: [share])
        linux = next(r for r in rows if r["source"]["platform"] == "linux")
        self.assertIsNone(linux["feed"])
        self.assertEqual(linux["source"]["reasonCode"], "timeout")

    def test_invalid_modern_health_keeps_valid_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "agiw-peers").mkdir()
            (root / "agiw-peers/pc1.json").write_text(json.dumps(presence(host="pc1", health="bad")))
            (root / "windows-status.json").write_text(json.dumps(presence(host="windows", kind=None)))
            rows = peer_feed.from_share_hosts(time.time(), root, root)
            rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda _: [], share_reader=lambda _: rows)
            self.assertEqual([r["feed"]["host"] for r in rows if r["feed"]], ["windows"])

    def test_paired_uuid_prevents_same_platform_hostname_spoof(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            nodes = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
            peers = [{"node": node, "paired": True, "host": "windows", "name": "desktop",
                      "state": "connected", "status": presence(host=host)}
                     for node, host in zip(nodes, ("pc-a", "pc-b"))]
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": peers})):
                first = peer_feed.from_peer_link_hosts(time.time(), state)
                self.assertEqual([r["source"].get("node") for r in first], nodes)
                peers[0]["status"] = presence(host="pc-b")
                rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state),
                                           share_reader=lambda _: [])
            windows = [r for r in rows if r["source"]["platform"] == "windows"]
            self.assertEqual(len(windows), 2)
            by_node = {r["source"]["node"]: r for r in windows}
            self.assertIsNone(by_node[nodes[0]]["feed"])
            self.assertEqual(by_node[nodes[0]]["source"]["reasonCode"], "host_key_mismatch")
            self.assertEqual(by_node[nodes[1]]["feed"]["reportedHost"], "pc-b")

    def test_initial_hostname_collision_keeps_both_paired_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            nodes = ["00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"]
            peers = [{"node": node, "paired": True, "host": "windows", "state": "connected",
                      "status": presence(host="pc-b")} for node in nodes]
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": peers})):
                rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state),
                                           share_reader=lambda _: [])
            windows = [r for r in rows if r["source"]["platform"] == "windows"]
            self.assertEqual({r["source"]["node"] for r in windows}, set(nodes))
            self.assertEqual(len({r["source"]["host"] for r in windows}), 2)
            self.assertEqual(sum(r["feed"] is not None for r in windows), 2)
            self.assertEqual({r["source"]["host"] for r in windows}, set(nodes))

    def test_disconnected_unmapped_name_never_authenticates_share_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            node = "00000000-0000-4000-8000-000000000001"
            peer = {"node": node, "paired": True, "host": "windows", "name": "pc1", "state": "offline"}
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": [peer]})):
                share = peer_feed.summarize_document(presence(host="pc1"), "share", time.time())
                rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state),
                                           share_reader=lambda _: [share])
            paired = next(r for r in rows if r["source"].get("node") == node)
            self.assertIsNone(paired["feed"])
            self.assertEqual(paired["source"]["host"], node)
            self.assertFalse(any(r["feed"] for r in rows))
            self.assertEqual(paired["source"]["diagnostics"][0]["host"], "pc1")

    def test_unpair_removes_authenticated_hostname_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            node = "00000000-0000-4000-8000-000000000001"
            peer = {"node": node, "paired": True, "host": "windows", "name": "desktop",
                    "state": "connected", "status": presence(host="pc1")}
            listing = {"peers": [peer]}
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, listing)):
                peer_feed.from_peer_link_hosts(time.time(), state)
                listing["peers"] = []
                peer_feed.from_peer_link_hosts(time.time(), state)
                listing["peers"] = [peer]
                peer.update(state="offline", status=None)
                rows = peer_feed.from_peer_link_hosts(time.time(), state)
            self.assertEqual(rows[0]["source"]["host"], node)
            self.assertIsNone(rows[0]["feed"])

    def test_warm_disconnect_keeps_share_uncorrelated_with_reported_label(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            node = "00000000-0000-4000-8000-000000000001"
            peer = {"node": node, "paired": True, "host": "windows", "name": "desktop",
                    "state": "connected", "status": presence(host="pc1")}
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, {"peers": [peer]})):
                peer_feed.from_peer_link_hosts(time.time(), state)
                peer.update(state="offline", status=None)
                share = peer_feed.summarize_document(presence(host="pc1"), "share", time.time())
                rows = peer_feed.peer_feeds(time.time(), peer_reader=lambda now: peer_feed.from_peer_link_hosts(now, state),
                                           share_reader=lambda _: [share])
            windows = [r for r in rows if r["source"]["platform"] == "windows"]
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0]["source"]["node"], node)
            self.assertIsNone(windows[0]["feed"])
            self.assertEqual(windows[0]["source"]["reportedHost"], "pc1")
            self.assertEqual(windows[0]["source"]["label"], "pc1 (reported)")
            self.assertEqual(windows[0]["source"]["reasonCode"], "not_connected")
            self.assertEqual(windows[0]["source"]["diagnostics"][0]["correlation"], "unverified")

    def test_platforms_identity_and_trust(self):
        for platform, canonical in (("windows", "windows"), ("mac", "mac"), ("macos", "mac"), ("linux", "linux")):
            for via, trust in (("lan", "authenticated"), ("share", "share-reported")):
                result = peer_feed.summarize_document(presence(platform=platform, host="node"), via, time.time(),
                                                      platform=canonical, host="node")
                self.assertEqual(result["source"]["platform"], canonical)
                if canonical != "windows" and via == "share":
                    self.assertIsNone(result["feed"])
                    self.assertEqual(result["source"]["reasonCode"], "unverified")
                    continue
                self.assertEqual(result["feed"]["host"], "node")
                self.assertEqual(result["feed"]["trust"], trust)
                self.assertEqual(result["feed"]["reasonCode"], "ok")

    def test_faults_never_return_live_inventory(self):
        now = time.time()
        cases = [(b"not json", "non_json"), (b" " * 65537, "oversize"),
                 ({}, "schema_invalid"), (presence(age=90), "stale"),
                 (presence(age=-90), "clock_skew"), (presence(platform="linux"), "host_key_mismatch")]
        for doc, reason in cases:
            with self.subTest(reason=reason):
                result = peer_feed.summarize_document(doc, "lan", now, platform="windows")
                self.assertIsNone(result["feed"])
                self.assertEqual(result["source"]["reasonCode"], reason)
                self.assertEqual(result["source"]["state"], "unavailable")

    def test_share_all_hosts_and_bad_file_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "llm-lab" / "status"
            peers = root / "agiw-peers"
            peers.mkdir(parents=True)
            for name, platform in (("pc1", "windows"), ("pc2", "windows"), ("linux1", "linux"), ("mac1", "macos")):
                (peers / (name + ".json")).write_text(json.dumps(presence(host=name, platform=platform)))
            (peers / "wrong.json").write_text(json.dumps(presence(host="someone-else")))
            rows = peer_feed.from_share_hosts(time.time(), root, base)
            good = [row["feed"] for row in rows if row["feed"]]
            self.assertEqual({row["host"] for row in good}, {"pc1", "pc2"})
            for host in ("linux1", "mac1"):
                self.assertEqual(next(row["source"]["reasonCode"] for row in rows
                                      if row["source"]["host"] == host), "unverified")
            self.assertEqual(next(row["source"]["reasonCode"] for row in rows if row["source"]["host"] == "wrong"),
                             "host_key_mismatch")

    def test_transport_faults_are_named_and_do_not_become_down(self):
        for error, code in ((TimeoutError(), "timeout"), (ConnectionRefusedError(), "refused"),
                            (FileNotFoundError(), "share_unmounted"),
                            (peer_feed.FeedUnavailable("asleep"), "asleep")):
            def failed(now):
                raise error
            rows = peer_feed.peer_feeds(time.time(), peer_reader=failed, share_reader=failed)
            self.assertEqual({r["source"]["platform"] for r in rows}, {"windows"})
            self.assertTrue(all(r["feed"] is None and r["source"]["reasonCode"] == code for r in rows))

    def test_lan_all_platforms_isolated_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "control-token").write_text("fixture")
            listing = {"peers": [
                {"paired": True, "host": platform, "state": "connected", "status": presence(host=name, platform=platform)}
                for name, platform in (("pc1", "windows"), ("pc2", "windows"), ("linux1", "linux"), ("mac1", "mac"))]}
            listing["peers"].extend([
                {"paired": True, "host": "linux", "state": "connected", "status": presence(platform="windows")},
                {"paired": False, "host": "linux", "state": "connected", "status": presence(platform="linux")},
                {"paired": True, "host": "linux", "state": "connected", "status": {}}])
            for index, row in enumerate(listing["peers"]):
                row["node"] = f"00000000-0000-4000-8000-{index:012d}"
            with mock.patch.object(peer_feed, "_control_get", return_value=(200, listing)):
                rows = peer_feed.from_peer_link_hosts(time.time(), state)
            self.assertEqual({r["feed"]["reportedHost"] for r in rows if r["feed"]}, {"pc1", "pc2", "linux1", "mac1"})
            self.assertEqual([r["source"]["reasonCode"] for r in rows if not r["feed"]],
                             ["host_key_mismatch", "schema_invalid"])

    def test_freshness_boundary_and_partial_inventory(self):
        now = 1800000000.0
        for age, good in ((30.0, True), (30.049, False), (-2.0, True), (-2.01, False)):
            row = peer_feed.summarize_document(presence(platform="linux", observedAt=peer_link._iso(now - age)), "lan", now)
            self.assertEqual(row["feed"] is not None, good)
        row = peer_feed.summarize_document(presence(platform="linux", modelsTotal=2), "lan", time.time())
        self.assertFalse(row["feed"]["modelsComplete"])

    def test_share_hang_does_not_queue_threads(self):
        release = threading.Event()
        entered = threading.Event()
        reader = peer_feed._HostShareReader()
        def hang(now):
            entered.set()
            release.wait(2)
            return []
        with mock.patch.object(peer_feed, "from_share_hosts", side_effect=hang) as read, \
                mock.patch.object(peer_feed, "SHARE_READ_DEADLINE", 0.02):
            try:
                for _ in range(2):
                    with self.assertRaises(peer_feed.FeedUnavailable) as error:
                        reader.read(time.time())
                    self.assertEqual(error.exception.reason_code, "timeout")
                self.assertTrue(entered.is_set())
                self.assertEqual(read.call_count, 1)
            finally:
                release.set()
                reader.thread.join(2)

    def test_share_legacy_bad_file_retains_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "llm-lab" / "status"
            root.mkdir(parents=True)
            (root / "windows-status.json").write_text(" " * 65537)
            rows = peer_feed.from_share_hosts(time.time(), root, base)
            self.assertEqual(rows[0]["source"]["reasonCode"], "oversize")
            self.assertEqual(rows[0]["source"]["platform"], "windows")

    def test_hands_off_preserves_only_explicit_boolean(self):
        for value in (True, False, "true", 1, None):
            result = peer_feed.summarize_document(presence(handsOff=value, gaming=True), "share", time.time())
            self.assertEqual(result["feed"].get("handsOff"), value if type(value) is bool else None)
            self.assertNotIn("gaming", result["feed"])

    def test_stale_source_retains_original_observation(self):
        doc = presence(age=90)
        result = peer_feed.summarize_document(doc, "share", time.time())
        self.assertIsNone(result["feed"])
        self.assertEqual(result["source"]["observedAt"], doc["observedAt"])

    def test_own_mac_presence_is_excluded_without_hiding_other_macs(self):
        now = time.time()
        rows = [peer_feed.summarize_document(presence(host=name, platform=platform), "share", now)
                for name, platform in (("local-mac", "macos"), ("other-mac", "mac"), ("local-mac", "linux"))]
        with mock.patch.object(peer_feed, "host_id", return_value="local-mac"):
            result = peer_feed.peer_feeds(now, peer_reader=lambda _: [], share_reader=lambda _: rows)
        identities = {(r["source"]["platform"], r["source"]["host"]) for r in result}
        self.assertNotIn(("mac", "local-mac"), identities)
        self.assertIn(("mac", "other-mac"), identities)
        self.assertIn(("linux", "local-mac"), identities)

    def test_legacy_windows_document_is_fallback_not_a_duplicate_machine(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "llm-lab" / "status"
            peers = root / "agiw-peers"
            peers.mkdir(parents=True)
            (peers / "pc1.json").write_text(json.dumps(presence(host="pc1")))
            (root / "windows-status.json").write_text(json.dumps(presence(host="windows", kind=None)))
            rows = peer_feed.from_share_hosts(time.time(), root, base)
            self.assertEqual([r["feed"]["host"] for r in rows if r["feed"]], ["pc1"])
            (peers / "pc1.json").unlink()
            rows = peer_feed.from_share_hosts(time.time(), root, base)
            self.assertEqual([r["feed"]["host"] for r in rows if r["feed"]], ["windows"])

    def test_unattributed_bad_document_keeps_failure_on_missing_platform(self):
        now = time.time()
        good = peer_feed.summarize_document(presence(host="pc1"), "share", now)
        bad = peer_feed.summarize_document(b"not json", "share", now, host="unattributed-node")
        rows = peer_feed.peer_feeds(now, peer_reader=lambda _: [], share_reader=lambda _: [good, bad])
        self.assertNotIn("linux", {r["source"]["platform"] for r in rows})
        self.assertEqual(bad["source"]["reasonCode"], "non_json")
        self.assertIn(bad, rows)
        self.assertIn(good, rows)

    def test_lan_host_suppresses_only_legacy_windows_fallback(self):
        now = time.time()
        lan = peer_feed.summarize_document(presence(host="pc-one", models=[
            {"id": "model", "loadedState": "loaded", "activity": "busy"}]), "lan", now)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "llm-lab" / "status"
            peers = root / "agiw-peers"
            peers.mkdir(parents=True)
            (root / "windows-status.json").write_text(json.dumps(presence(host="windows", kind=None,
                models=[{"id": "model", "loadedState": "loaded", "activity": "busy"}])))
            share = peer_feed.from_share_hosts(now, root, base)
            self.assertIs(share[0]["source"].get("legacy"), True)
            rows = peer_feed.peer_feeds(now, peer_reader=lambda _: [lan], share_reader=lambda _: share)
            self.assertEqual([r["feed"]["host"] for r in rows if r["feed"]], ["pc-one"])
            (peers / "windows.json").write_text(json.dumps(presence(host="windows")))
            share = peer_feed.from_share_hosts(now, root, base)
            self.assertFalse(share[0]["source"].get("legacy", False))
            rows = peer_feed.peer_feeds(now, peer_reader=lambda _: [lan], share_reader=lambda _: share)
            self.assertEqual({r["feed"]["host"] for r in rows if r["feed"]}, {"pc-one", "windows"})

    def test_merge_keeps_hosts_separate_and_authenticated_preferred(self):
        now = time.time()
        lan = peer_feed.summarize_document(presence(host="pc1"), "lan", now)
        share = [peer_feed.summarize_document(presence(host=name), "share", now) for name in ("pc1", "pc2")]
        rows = peer_feed.peer_feeds(now, peer_reader=lambda _: [lan], share_reader=lambda _: share)
        peers = {r["source"]["host"]: r for r in rows if r["feed"]}
        self.assertEqual(set(peers), {"pc1", "pc2"})
        self.assertEqual(peers["pc1"]["source"]["trust"], "authenticated")
        self.assertEqual(peers["pc2"]["source"]["trust"], "share-reported")


class Share(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "llm-lab" / "status"
        (self.root / "agiw-peers").mkdir(parents=True)

    def put(self, name, doc):
        (self.root / name).write_text(json.dumps(doc))

    def test_fresh_presence_is_live_and_only_known_fields_pass(self):
        self.put("agiw-peers/winpc01.json", presence())
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual((feed["via"], feed["host"], feed["models"][0]["slotsTotal"]), ("share", "winpc01", 2))
        self.assertNotIn("secretish", feed["models"][0])
        self.assertNotIn("path", feed["gpus"][0])
        self.assertNotIn("commit", feed["memory"])
        self.assertEqual(feed["models"][0]["liveTokensPerSecond"], 104.3)
        self.assertIs(feed["modelsComplete"], True)

    def test_partial_presence_keeps_valid_unique_models_without_claiming_a_complete_inventory(self):
        doc = presence(models=[{"id": "first", "loadedState": "loaded"},
                               {"id": "first", "loadedState": "inactive"},
                               {"id": "", "loadedState": "loaded"},
                               {"id": "second", "loadedState": "inactive"}], modelsTotal=4)
        self.put("agiw-peers/winpc01.json", doc)
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual([(m["id"], m["loadedState"]) for m in feed["models"]],
                         [("first", "loaded"), ("second", "inactive")])
        self.assertIs(feed["modelsComplete"], False)

    def test_schema_version_is_a_strict_integer(self):
        for version in (True, 1.0, "1", None, 2):
            with self.subTest(version=version):
                self.put("agiw-peers/winpc01.json", presence(schemaVersion=version))
                self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_freshness_uses_the_unrounded_age(self):
        now = 1800000000.0
        for age, fresh in ((30.0, True), (30.049, False)):
            with self.subTest(age=age):
                self.put("agiw-peers/winpc01.json",
                         presence(observedAt=peer_link._iso(now - age), observedUnix=now - age))
                feed = peer_feed.from_share(now, self.root, self.base)
                self.assertEqual(feed is not None, fresh)
                if feed:
                    self.assertEqual(feed["ageSeconds"], 30.0)

    def test_freshest_report_uses_unrounded_observation_time(self):
        now = 1800000000.0
        self.put("agiw-peers/older.json", presence(host="older", observedAt=peer_link._iso(now - 2.04)))
        self.put("agiw-peers/newer.json", presence(host="newer", observedAt=peer_link._iso(now - 2.01)))
        with mock.patch.object(peer_feed, "_peer_names", return_value=["older.json", "newer.json"]):
            feed = peer_feed.from_share(now, self.root, self.base)
        self.assertEqual((feed["host"], feed["ageSeconds"]), ("newer", 2.0))

    def test_stale_or_mac_or_broken_files_are_not_live(self):
        self.put("agiw-peers/old.json", presence(age=45))
        self.put("agiw-peers/mac.json", presence(platform="mac"))
        self.put("agiw-peers/bad.json", {"schemaVersion": 1})
        (self.root / "agiw-peers" / "nan.json").write_text('{"schemaVersion": NaN}')
        (self.root / "agiw-peers" / "big.json").write_text(" " * 70000)
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_windows_status_json_is_the_fallback(self):
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        self.put("windows-status.json", doc)
        self.assertEqual(peer_feed.from_share(time.time(), self.root, self.base)["models"][0]["id"], "gpt-oss-20b")

    def test_symlinked_record_or_folder_is_refused(self):
        real = self.root.parent / "elsewhere.json"
        real.write_text(json.dumps(presence()))
        os.symlink(real, self.root / "agiw-peers" / "link.json")
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_symlinked_ancestor_folder_is_refused(self):
        self.put("agiw-peers/winpc01.json", presence())
        moved = self.base / "real-llm-lab"
        (self.base / "llm-lab").rename(moved)
        os.symlink(moved, self.base / "llm-lab")
        with self.assertRaises(peer_feed.ShareRefused):
            peer_feed.from_share(time.time(), self.root, self.base)

    def test_infinite_numbers_and_odd_memory_values_are_dropped(self):
        doc = presence()
        self.put("agiw-peers/winpc01.json", doc)
        raw = (self.root / "agiw-peers" / "winpc01.json").read_text()
        raw = raw.replace('"utilizationPercent": 4.0', '"utilizationPercent": 1e999')
        raw = raw.replace('"availablePercent": 55.6', '"availablePercent": {"nested": 1}')
        (self.root / "agiw-peers" / "winpc01.json").write_text(raw)
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertNotIn("utilizationPercent", feed["gpus"][0])
        self.assertEqual(feed["memory"], {"level": "ok"})
        json.dumps(feed, allow_nan=False)  # the snapshot must stay serializable

    def test_future_dated_record_is_a_clock_mismatch_not_fresh(self):
        self.put("agiw-peers/winpc01.json", presence(age=-45))
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_missing_share_is_none(self):
        self.assertIsNone(peer_feed.from_share(time.time(), Path("/nonexistent/llm-lab/status")))


class Feed(unittest.TestCase):
    def test_model_completeness_requires_valid_unique_rows_and_the_uncapped_total(self):
        good = {"id": "first", "loadedState": "loaded"}
        many = [{"id": f"m{i}", "loadedState": "inactive"} for i in range(33)]
        cases = (([good], 1, True), ([], 0, True), (many[:32], 32, True), (many, 33, False),
                 (many[:32], 33, False), ([good, good], 2, False), ([good, None], 2, False),
                 ([good, {"id": "", "loadedState": "loaded"}], 2, False),
                 ([good, {"id": "bad", "loadedState": "resident"}], 2, False),
                 (None, 0, False), ({}, 0, False), ([good], True, False), ([good], 1.0, False),
                 ([good], "1", False), ([good], None, False), ([good], 0, False))
        for models, total, complete in cases:
            with self.subTest(models=models, total=total):
                summary = peer_feed._summary(presence(models=models, modelsTotal=total), "share", time.time())
                self.assertIs(summary["modelsComplete"], complete)
                self.assertLessEqual(len(summary["models"]), peer_link.MAX_MODELS)
        doc = presence()
        del doc["modelsTotal"]
        self.assertIs(peer_feed._summary(doc, "share", time.time())["modelsComplete"], False)
        del doc["models"]
        self.assertIs(peer_feed._summary(doc, "share", time.time())["modelsComplete"], False)

    def test_legacy_status_completeness_does_not_require_models_total(self):
        doc = presence()
        del doc["kind"], doc["modelsTotal"]
        doc["host"] = "windows"
        self.assertIs(peer_feed._summary(doc, "share", time.time())["modelsComplete"], True)
        doc["models"] += doc["models"]
        summary = peer_feed._summary(doc, "share", time.time())
        self.assertIs(summary["modelsComplete"], False)
        self.assertEqual(len(summary["models"]), 1)

    def test_lan_freshness_uses_the_unrounded_age(self):
        token_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, token_dir)
        (token_dir / "control-token").write_text("a" * 64)
        now = 1800000000.0
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected"}]}
        with mock.patch.object(peer_link, "default_state_dir", return_value=token_dir), \
                mock.patch.object(peer_feed, "_control_get", return_value=(200, listing)):
            for age, fresh in ((30.0, True), (30.049, False)):
                with self.subTest(age=age):
                    listing["peers"][0]["status"] = presence(observedAt=peer_link._iso(now - age))
                    self.assertEqual(peer_feed.from_peer_link(now) is not None, fresh)

    def test_stale_lan_result_falls_back_to_a_fresh_share(self):
        token_dir = Path(tempfile.mkdtemp())
        (token_dir / "control-token").write_text("a" * 64)
        stale = presence(age=45)
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected", "status": stale}]}
        with mock.patch.object(peer_link, "default_state_dir", return_value=token_dir), \
                mock.patch.object(peer_feed, "_control_get", return_value=(200, listing)):
            self.assertIsNone(peer_feed.from_peer_link(time.time()))
            listing["peers"][0]["status"] = presence(age=2)
            self.assertEqual(peer_feed.from_peer_link(time.time())["via"], "lan")

    def test_no_peer_link_setup_means_no_lan_attempt_and_nothing_created(self):
        empty = Path(tempfile.mkdtemp()) / "peer-link"
        with mock.patch.object(peer_link, "default_state_dir", return_value=empty), \
                mock.patch.object(peer_feed, "_control_get", side_effect=AssertionError("must not be called")), \
                mock.patch.object(peer_link, "control_request", side_effect=AssertionError("must not be called")):
            self.assertIsNone(peer_feed.from_peer_link(time.time()))
        self.assertFalse(empty.exists())

    def test_lan_beats_share(self):
        lan = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "lan", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", return_value=lan), \
                mock.patch.object(peer_feed, "from_share", side_effect=AssertionError("share read not needed")):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual((feed["via"], source["state"]), ("lan", "live"))
        self.assertIn("LAN", source["detail"])

    def test_share_used_when_lan_fails_and_says_unsigned(self):
        share = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "share", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", side_effect=ConnectionRefusedError()), \
                mock.patch.object(peer_feed, "from_share", return_value=share):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual(feed["via"], "share")
        # The map's words (REPORTED VIA SHARE) and the PC's: an unsigned share file is reported, never "linked",
        # and its source is recorded, never live (review finding D2).
        self.assertEqual(source["state"], "recorded")
        self.assertEqual(source["detail"], "Unsigned report from winpc01 in SharedChami status files "
                                           "(not verified; LAN link not connected: ConnectionRefusedError)")
        self.assertNotIn("Linked", source["detail"])

    def test_hung_share_is_reported_not_waited_on(self):
        release = threading.Event()
        reader = peer_feed._ShareReader()
        with mock.patch.object(peer_feed, "from_share", side_effect=lambda now: release.wait(5)), \
                mock.patch.object(peer_feed, "SHARE_READ_DEADLINE", 0.2):
            started = time.monotonic()
            self.assertEqual(reader.read(time.time())[0], "stuck")
            self.assertEqual(reader.read(time.time())[0], "stuck")  # no second thread piles up
            self.assertLess(time.monotonic() - started, 1.0)
        release.set()

    def test_nothing_fresh_is_unavailable_with_reason(self):
        with mock.patch.object(peer_feed, "from_peer_link", return_value=None), \
                mock.patch.object(peer_feed, "from_share", return_value=None):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertIsNone(feed)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("no fresh PC status", source["detail"])


class MacPresence(unittest.TestCase):
    def test_incomplete_inventory_diagnostic_does_not_claim_sources_are_unavailable(self):
        now = time.time()
        snap = {"sampledAt": now, "activityKnown": False,
                "sources": [{"id": "lmstudio-api", "state": "live", "modelsComplete": False}],
                "models": [{"id": "vendor/idle", "host": "mac", "loaded": False}]}
        with self.assertRaises(peer_feed.InventoryUnknown) as refused:
            peer_feed.share_presence(snap, "louis-mac", now=now)
        self.assertEqual(peer_feed._failure(refused.exception), (
            "unavailable", "Mac model inventory incomplete, so no model count is published; "
            "the last file ages out after 30 s"))

    def test_inventory_diagnostics_keep_fixed_causes_without_private_identifiers(self):
        for cause in ("Mac model instance inventory incomplete", "Mac model loaded state unknown",
                      "Mac model inventory contains an unpublishable identifier", "private /Users/alice"):
            with self.subTest(cause=cause):
                state, detail = peer_feed._failure(peer_feed.InventoryUnknown(cause))
                self.assertEqual(state, "unavailable")
                expected = "Mac model inventory unknown" if cause.startswith("private") else cause
                self.assertEqual(detail, expected + ", so no model count is published; "
                                 "the last file ages out after 30 s")

    def test_unpublishable_model_id_withholds_presence_instead_of_a_complete_zero(self):
        now = time.time()
        for model_id in ("Users/alice", "/home/alice", None):
            for loaded in (True, False):
                with self.subTest(model_id=model_id, loaded=loaded):
                    snap = {"sampledAt": now, "activityKnown": False,
                            "sources": [{"id": "lmstudio-api", "state": "live", "modelsComplete": True}],
                            "models": [{"id": model_id, "host": "mac", "loaded": loaded,
                                        "source": "lmstudio-api"},
                                       {"id": "vendor/idle", "host": "mac", "loaded": False,
                                        "source": "lmstudio-api"}]}
                    with self.assertRaises(peer_feed.InventoryUnknown):
                        peer_feed.share_presence(snap, "louis-mac", now=now)

    def test_incomplete_live_inventory_is_withheld_from_the_pc(self):
        now = time.time()
        for source_id in ("lmstudio-api", "lms-ps"):
            for known in (False, True):
                with self.subTest(source=source_id, known=known):
                    snap = {"sampledAt": now, "activityKnown": known,
                            "sources": [{"id": source_id, "state": "live", "route": "direct", "modelsComplete": False}],
                            "models": [{"id": "inactive", "host": "mac", "loaded": False}]}
                    with self.assertRaises(peer_feed.InventoryUnknown):
                        peer_feed.share_presence(snap, "louis-mac", now=now)
                    snap["sources"][0]["modelsComplete"] = True
                    doc = peer_feed.share_presence(snap, "louis-mac", now=now)
                    report = pc_probes()._validate_peer(doc, now)
                    self.assertEqual(report["modelsComplete"], source_id == "lmstudio-api")
                    self.assertTrue(report["loadedListComplete"])
                    self.assertEqual(sum(row["loadedState"] == "loaded" for row in report["models"]), 0)

    def test_incomplete_instance_correlation_is_withheld_from_the_pc(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        now = time.time()
        for identifiers in ([f"alias-{index}" for index in range(17)], ["alias-0", "invalid id"]):
            with self.subTest(identifiers=identifiers):
                api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
                    {"id": identifier} for identifier in identifiers]}]}, now)
                cli = _parse_lms({"models": [{"identifier": "alias-0", "status": "idle"}]}, now)
                models = {row["id"]: row for row in api}
                _merge_cli_rows(models, cli)
                snap = {"sampledAt": now, "activityKnown": False, "models": list(models.values()),
                        "sources": [{"id": source, "state": "live", "modelsComplete": True, "route": "direct"}
                                    for source in ("lmstudio-api", "lms-ps")]}
                with self.assertRaises(peer_feed.InventoryUnknown):
                    peer_feed.share_presence(snap, "louis-mac", now=now)

    def test_unknown_loaded_state_is_withheld_from_the_pc(self):
        now = time.time()
        snap = {"sampledAt": now, "activityKnown": False,
                "models": [{"id": "uncertain", "host": "mac", "loaded": None}],
                "sources": [{"id": "lmstudio-api", "state": "live", "modelsComplete": True}]}
        with self.assertRaises(peer_feed.InventoryUnknown):
            peer_feed.share_presence(snap, "louis-mac", now=now)

    def test_publishes_one_canonical_model_with_instance_alias_metadata(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        now = time.time()
        for same_id in (False, True):
            for activity in ("idle", "busy", "generating"):
                with self.subTest(same_id=same_id, activity=activity):
                    identifiers = ["provider/model" if same_id else "alias-1", "alias-2"]
                    api = _parse_api({"models": [{"key": "provider/model", "loaded_instances": [
                        {"id": identifier} for identifier in identifiers]}]}, now)
                    cli = _parse_lms({"models": [{"identifier": identifier, "modelKey": "provider/model",
                        "status": activity if index else "idle"} for index, identifier in enumerate(identifiers)]}, now)
                    models = {row["id"]: row for row in api}
                    _merge_cli_rows(models, cli)
                    snap = {"sampledAt": now, "activityKnown": True, "models": list(models.values()),
                            "sources": [{"id": source, "state": "live", "modelsComplete": True, "route": "direct"}
                                        for source in ("lmstudio-api", "lms-ps")]}
                    doc = peer_feed.share_presence(snap, "louis-mac", now=now)
                    self.assertEqual(doc["modelsTotal"], 1)
                    self.assertEqual([(row["id"], row["loadedState"], row["activity"]) for row in doc["models"]],
                                     [("provider/model", "loaded", activity)])
                    self.assertEqual(doc["models"][0]["aliases"], [item for item in identifiers if item != "provider/model"])
                    report = pc_probes()._validate_peer(doc, now)
                    self.assertTrue(report["modelsComplete"])
                    self.assertEqual(sum(row["loadedState"] == "loaded" for row in report["models"]),
                                     machines_summary(snap, [])[0]["loadedCount"])

    def test_canonical_presence_keeps_colliding_instance_observations(self):
        from telemetry import _merge_cli_rows, _parse_api, _parse_lms
        now = time.time()
        api = _parse_api({"models": [
            {"key": "provider/model", "loaded_instances": [{"id": "other/model"}]},
            {"key": "other/model", "loaded_instances": []}]}, now)
        cli = _parse_lms({"models": [{"identifier": "other/model", "modelKey": "provider/model",
                                      "status": "generating"}]}, now)
        models = {row["id"]: row for row in api}
        observations = _merge_cli_rows(models, cli, isolate_collisions=True)
        snap = {"sampledAt": now, "activityKnown": True, "models": list(models.values()),
                "localInstanceObservations": observations,
                "sources": [{"id": source, "state": "live", "modelsComplete": True, "route": "direct"}
                            for source in ("lmstudio-api", "lms-ps")]}
        doc = peer_feed.share_presence(snap, "louis-mac", now=now)
        self.assertEqual(doc["modelsTotal"], 2)
        self.assertEqual([(row["id"], row["loadedState"], row["activity"]) for row in doc["models"]],
                         [("provider/model", "loaded", "generating"), ("other/model", "inactive", "unknown")])
        self.assertEqual(doc["models"][0]["aliases"], ["other/model"])

    def test_maps_snapshot_rows_and_validates(self):
        snap = {"sampledAt": time.time(), "activityKnown": True, "memory": {"level": "watch", "availablePercent": 57},
                "models": [{"id": "qwen", "host": "mac", "loaded": True, "state": "generating"},
                           {"id": "gemma", "host": "mac", "loaded": False, "state": "unloaded"},
                           {"id": "pc-model", "host": "windows", "loaded": True, "state": "idle"}]}
        doc = peer_feed.mac_presence(snap, "louis-mac")
        peer_link.validate_status(doc, "mac", time.time())
        self.assertEqual([(m["id"], m["loadedState"], m["activity"]) for m in doc["models"]],
                         [("qwen", "loaded", "generating"), ("gemma", "inactive", "unknown")])

    def test_no_snapshot_is_an_error_not_an_empty_status(self):
        with self.assertRaises(ValueError):
            peer_feed.mac_presence(None, "louis-mac")


HERE = Path(__file__).resolve().parent
WINDOWS = HERE / "windows"


def pc_probes():
    """The PC edition's own producer (windows/agiw_win/probes.py), imported read-only for interop checks."""
    if not (WINDOWS / "agiw_win" / "probes.py").is_file():
        raise unittest.SkipTest("PC edition sources are not in this checkout")
    before, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    sys.path.insert(0, str(WINDOWS))
    try:
        from agiw_win import probes
    finally:
        sys.path.remove(str(WINDOWS))
        sys.dont_write_bytecode = before
    return probes


class PcMemoryLevels(unittest.TestCase):
    """Review finding A1: every level the PC's producer emits, "tight" above all, reaches the Mac's reader."""

    def test_pc_tight_memory_survives_producer_to_reader(self):
        probes = pc_probes()
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        root = base / "llm-lab" / "status"
        (root / "agiw-peers").mkdir(parents=True)
        gib = 1 << 30
        # (available %, commit %, level) through the PC's own probes.memory_level thresholds.
        for available, commit, expected in ((50, None, "ok"), (15, None, "watch"), (10, None, "tight"),
                                            (3, None, "critical"), (50, 97, "tight")):
            reading = {"totalPhys": 100 * gib, "availPhys": available * gib}
            if commit is not None:
                reading.update(commitLimit=100 * gib, commitAvail=(100 - commit) * gib)
            block, _ = probes.memory_block(lambda: reading, lambda: [])
            self.assertEqual(block["level"], expected)
            now = time.time()
            doc = probes.peer_presence({"lanes": [{"id": "fast", "status": "idle", "phase": "idle", "port": 1235,
                                                   "expectedModel": "openai/gpt-oss-20b", "servedModel": "gpt-oss-20b",
                                                   "slotsBusy": 0, "slotsTotal": 2}],
                                        "pcGpus": [], "memory": block, "observerVersion": "0.1.0",
                                        "observedAt": peer_feed._iso(now), "sampledAt": now, "dashboardPort": 8767},
                                       "winpc01")
            (root / "agiw-peers" / "winpc01.json").write_text(json.dumps(doc))
            want = {"level": expected, "availablePercent": float(available)}
            # The share file, read as the Mac reads it.
            self.assertEqual(peer_feed.from_share(time.time(), root, base)["memory"], want, expected)
            # The paired LAN link: what leaves the PC (project_status), then the same reader.
            lan = peer_feed._summary(peer_link.validate_status(peer_link.project_status(doc), "windows", time.time()),
                                     "lan", time.time())
            self.assertEqual(lan["memory"], want, expected)

    def test_reader_and_publisher_share_one_level_vocabulary(self):
        import mem_guard
        self.assertEqual(set(peer_feed.MEMORY_LEVELS), set(mem_guard.LEVELS) | {"unknown"})
        doc = presence(memory={"level": "warn", "availablePercent": 9})
        self.assertEqual(peer_feed._summary(peer_link.validate_status(doc, "windows"), "share", time.time())["memory"],
                         {"availablePercent": 9.0})  # a word no producer emits is dropped, the number kept


class ShareBounds(unittest.TestCase):
    """Review finding D1: agiw-peers is enumerated from its own descriptor within a fixed budget, never followed."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "llm-lab" / "status"
        self.peers = self.root / "agiw-peers"
        self.peers.mkdir(parents=True)
        (self.peers / "winpc01.json").write_text(json.dumps(presence()))

    def fill(self, count):
        for index in range(count):
            (self.peers / f".junk-{index:04d}.tmp").write_bytes(b"")

    def test_normal_folder_reads_as_before(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT - 1)  # exactly the budget, the record included
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual((feed["via"], feed["host"]), ("share", "winpc01"))

    def test_over_budget_folder_is_unavailable_not_partial(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT)  # one entry over the budget
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        (self.root / "windows-status.json").write_text(json.dumps(doc))
        with self.assertRaises(peer_feed.ShareRefused) as caught:
            peer_feed.from_share(time.time(), self.root, self.base)
        self.assertIn("too many entries", str(caught.exception))
        self.assertNotIn(str(self.base), str(caught.exception))
        # The refusal reaches the Windows source row through the share reader, with nothing shown as the PC.
        with mock.patch.object(peer_feed, "from_peer_link", return_value=None), \
                mock.patch.object(peer_feed, "SHARE_STATUS", self.root), \
                mock.patch.object(peer_feed, "SHARE_ANCHOR", self.base):
            feed, source = peer_feed.windows_feed(time.time())
        self.assertIsNone(feed)
        self.assertEqual(source["state"], "unavailable")
        self.assertIn("too many entries", source["detail"])

    def test_symlinked_agiw_peers_is_refused_whole(self):
        elsewhere = self.base / "elsewhere"
        self.peers.rename(elsewhere)
        os.symlink(elsewhere, self.peers)
        # Even a fresh windows-status.json beside it is not used: a tampered folder means no share answer at all.
        doc = presence()
        doc.pop("kind")
        doc["host"] = "windows"
        (self.root / "windows-status.json").write_text(json.dumps(doc))
        with self.assertRaises(peer_feed.ShareRefused) as caught:
            peer_feed.from_share(time.time(), self.root, self.base)
        self.assertIn("symbolic link", str(caught.exception))
        self.assertEqual(sorted(p.name for p in elsewhere.iterdir()), ["winpc01.json"])

    def test_folder_swapped_while_read_is_refused(self):
        real_read = peer_feed._read_at
        swapped = []

        def read_then_swap(directory, name):
            data = real_read(directory, name)
            if not swapped and name.endswith(".json") and name != "windows-status.json":
                self.peers.rename(self.root / "agiw-peers-old")
                self.peers.mkdir()
                swapped.append(name)
            return data

        with mock.patch.object(peer_feed, "_read_at", side_effect=read_then_swap):
            with self.assertRaises(peer_feed.ShareRefused) as caught:
                peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual(swapped, ["winpc01.json"])
        self.assertIn("replaced while it was read", str(caught.exception))

    def test_scan_stops_at_the_budget(self):
        self.fill(peer_feed.PEER_SCAN_LIMIT * 4)
        listed = []
        real_scandir = os.scandir

        class Counting:
            def __init__(self, inner):
                self.inner = inner

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()

            def __iter__(self):
                for entry in self.inner:
                    listed.append(entry.name)
                    yield entry

        with mock.patch.object(peer_feed.os, "scandir", side_effect=lambda target: Counting(real_scandir(target))):
            with self.assertRaises(peer_feed.ShareRefused):
                peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual(len(listed), peer_feed.PEER_SCAN_LIMIT + 1)


class ShareNeverLive(unittest.TestCase):
    """Review finding D2: an unsigned share report is recorded, never live; the paired LAN feed may stay live."""

    def test_share_source_is_recorded_and_lan_source_is_the_peers_report(self):
        share = peer_feed._summary(peer_link.validate_status(presence(age=0), "windows"), "share", time.time())
        for lan in (None, peer_feed.LinkUnavailable("control token malformed (the peer link replaces it)")):
            effect = {"return_value": None} if lan is None else {"side_effect": lan}
            with mock.patch.object(peer_feed, "from_peer_link", **effect), \
                    mock.patch.object(peer_feed, "from_share", return_value=share):
                feed, source = peer_feed.windows_feed(time.time())
            self.assertEqual((feed["via"], source["state"]), ("share", "recorded"))
            self.assertNotIn("live", source["detail"].lower())
            self.assertTrue(source["detail"].startswith("Unsigned report from winpc01"), source["detail"])
        self.assertIn("LAN link not connected: control token malformed", source["detail"])
        lan_feed = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "lan", time.time())
        with mock.patch.object(peer_feed, "from_peer_link", return_value=lan_feed):
            _feed, source = peer_feed.windows_feed(time.time())
        self.assertEqual(source["state"], "live")
        self.assertEqual(source["detail"], "Reported by winpc01 over the paired LAN link "
                                           "(signed by the paired peer; its own report of its state)")


VALID_TOKEN = "0123456789abcdef" * 4


class Listener:
    """A loopback listener on the peer-link control paths that records every request it receives. With a token it
    answers as the real peer link does (server proof first, then checks the client proof); without one it is a
    squatter whose hello proof is wrong."""

    def __init__(self, token=None, listing=None):
        self.token, self.listing, self.requests, self.issued = token, listing, [], None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, doc=None):
                body = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append((self.command, self.path, dict(self.headers.items())))
                if self.path == "/control/hello":
                    nc, ns = self.headers.get("X-Agiw-Client-Nonce", ""), secrets.token_hex(16)
                    outer.issued = (nc, ns)
                    proof = peer_link.control_server_proof(outer.token, nc, ns) if outer.token else "0" * 64
                    return self.reply(200, {"ns": ns, "proof": proof})
                if self.path == "/control/peers" and outer.token and outer.issued:
                    nc, ns = outer.issued
                    expected = peer_link.control_client_proof(outer.token, ns, nc, "GET", "/control/peers", b"")
                    if (self.headers.get("X-Agiw-Control-Nonce") == ns
                            and hmac.compare_digest(self.headers.get("X-Agiw-Control-Proof", ""), expected)):
                        return self.reply(200, outer.listing)
                self.reply(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def tree(path):
    """Everything a status read could change in a state folder: names, modes, owners, inodes, change times,
    contents (or link targets) and, on macOS, extended access lists."""
    paths = [str(path)]
    for dirpath, dirnames, filenames in os.walk(path):
        paths += [os.path.join(dirpath, name) for name in dirnames + filenames]
    out = {}
    for item in paths:
        info = os.lstat(item)
        if stat.S_ISREG(info.st_mode):
            with open(item, "rb") as handle:
                content = handle.read()
        elif stat.S_ISLNK(info.st_mode):
            content = os.readlink(item)
        else:
            content = None
        acl = ""
        if sys.platform == "darwin":
            acl = "\n".join(subprocess.run(["/bin/ls", "-led", item], capture_output=True, text=True,
                                           timeout=10).stdout.splitlines()[1:])
        out[os.path.relpath(item, path)] = (info.st_mode, info.st_uid, info.st_ino, info.st_ctime_ns,
                                            info.st_mtime_ns, content, acl)
    return out


ACL_TOOL = "/bin/" + "chmod"  # macOS chmod +a / -N: adds the test's folder ACL and removes it in cleanup


class ObserverToken(unittest.TestCase):
    """Review finding A2: observing the paired PC never changes the peer-link state folder, and nothing derived from
    the control token is sent before the listener proves it holds that token."""

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        self.dir = base / "peer-link"
        self.dir.mkdir()
        os.chmod(self.dir, 0o700)
        (self.dir / "trusted").mkdir(mode=0o700)
        self.put("identity.json", b'{"nodeUuid": "6f1c3a52-6a0e-4c43-8f55-2b1e0c9a7d10"}')
        self.http_calls = []
        real_http = peer_link._http

        def recording_http(*args, **kwargs):
            self.http_calls.append(args[:5])
            return real_http(*args, **kwargs)

        never = AssertionError("the observer must never repair or rotate peer-link state")
        for name in ("load_control_token", "_restrict_dir", "_write_private", "control_request",
                     "_win_restrict_dir", "_win_retire_exposed"):
            patcher = mock.patch.object(peer_link, name, side_effect=never)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(peer_link, "_http", side_effect=recording_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, name, data, mode=0o600):
        path = self.dir / name
        path.write_bytes(data)
        os.chmod(path, mode)
        return path

    def listener(self, **kwargs):
        listener = Listener(**kwargs)
        self.addCleanup(listener.close)
        return listener

    def status_read(self, listener):
        """One snapshot's Windows read (LAN then share), then a direct LAN read against the fake listener."""
        share = peer_feed._summary(peer_link.validate_status(presence(), "windows"), "share", time.time())
        with mock.patch.object(peer_link, "default_state_dir", return_value=self.dir), \
                mock.patch.object(peer_feed, "from_share", return_value=share):
            feed, source = peer_feed.windows_feed(time.time())
        direct = None
        try:
            peer_feed.from_peer_link(time.time(), self.dir, listener.port)
        except peer_feed.LinkUnavailable as exc:
            direct = str(exc)
        return feed, source, direct

    def test_token_problems_change_nothing_and_send_nothing(self):
        real_fstat = os.fstat

        def foreign(target):
            def fstat(fd):
                info = real_fstat(fd)
                if info.st_ino != target:
                    return info
                return os.stat_result((info.st_mode, info.st_ino, info.st_dev, info.st_nlink, info.st_uid + 1,
                                       info.st_gid, info.st_size, int(info.st_atime), int(info.st_mtime),
                                       int(info.st_ctime)))
            return mock.patch.object(peer_feed.os, "fstat", side_effect=fstat)

        def open_folder():
            self.put("control-token", VALID_TOKEN.encode())
            os.chmod(self.dir, 0o757)

        def group_folder():  # not writable by others, but a running peer link would still chmod it to 0700
            self.put("control-token", VALID_TOKEN.encode())
            os.chmod(self.dir, 0o750)

        cases = [
            ("malformed", lambda: self.put("control-token", b"not-a-token\n"), None, "malformed"),
            ("upper-case hex", lambda: self.put("control-token", VALID_TOKEN.upper().encode()), None, "malformed"),
            ("loose", lambda: self.put("control-token", VALID_TOKEN.encode(), 0o644), None, "another user"),
            ("not text", lambda: self.put("control-token", b"\xff\xfe" * 40), None, "unreadable"),
            ("linked", lambda: os.symlink(self.put("real-token", VALID_TOKEN.encode()).name, self.dir / "control-token"),
             None, "unreadable"),
            ("foreign", lambda: self.put("control-token", VALID_TOKEN.encode()), foreign, "another user"),
            ("open folder", open_folder, None, "writable by other users"),
            ("group-readable folder", group_folder, None, "owner-only"),
        ]
        if sys.platform == "darwin":
            me = pwd.getpwuid(os.getuid()).pw_name

            def with_acl():
                self.put("control-token", VALID_TOKEN.encode())
                if subprocess.run([ACL_TOOL, "+a", f"user:{me} allow list", str(self.dir)],
                                  capture_output=True, timeout=10).returncode != 0:
                    self.skipTest("this volume does not take an extended ACL")
                self.addCleanup(subprocess.run, [ACL_TOOL, "-N", str(self.dir)], capture_output=True, timeout=10)
            cases.append(("folder ACL", with_acl, None, "access list"))
        for name, arrange, patch, reason in cases:
            with self.subTest(name):
                for entry in list(self.dir.iterdir()):
                    if entry.name not in ("trusted", "identity.json"):
                        entry.unlink()
                os.chmod(self.dir, 0o700)
                arrange()
                listener = self.listener()
                before = tree(self.dir)
                self.http_calls.clear()
                if patch is not None:
                    with patch(os.lstat(self.dir / "control-token").st_ino):
                        feed, source, direct = self.status_read(listener)
                else:
                    feed, source, direct = self.status_read(listener)
                self.assertEqual(tree(self.dir), before, "the state folder changed")
                self.assertEqual(listener.requests, [], "something was sent")
                self.assertEqual(self.http_calls, [], "a control exchange was attempted")
                # The share fallback still answers, and the source row says why the LAN feed did not.
                self.assertEqual((feed["via"], source["state"]), ("share", "recorded"))
                self.assertIn(reason, source["detail"])
                self.assertIn(reason, direct or "")
                self.assertNotIn(str(self.dir), source["detail"])
                self.assertNotIn(VALID_TOKEN, json.dumps(source))

    def test_case_variant_token_is_left_alone(self):
        self.put("Control-Token", VALID_TOKEN.encode())
        listener = self.listener()
        before = tree(self.dir)
        _feed, source, direct = self.status_read(listener)
        self.assertEqual(tree(self.dir), before)
        self.assertEqual((listener.requests, self.http_calls), ([], []))
        if (self.dir / "control-token").exists():  # a case-insensitive volume opens the variant for the real name
            self.assertIn("case-variant", direct)
            self.assertIn("case-variant", source["detail"])

    def test_squatter_without_the_proof_gets_nothing_token_derived(self):
        self.put("control-token", VALID_TOKEN.encode())
        squatter = self.listener()
        before = tree(self.dir)
        with self.assertRaises(peer_feed.LinkUnavailable) as caught:
            peer_feed.from_peer_link(time.time(), self.dir, squatter.port)
        self.assertIn("did not prove", str(caught.exception))
        self.assertEqual(tree(self.dir), before)
        self.assertEqual([(method, path) for method, path, _ in squatter.requests], [("GET", "/control/hello")])
        headers = {key.lower(): value for key, value in squatter.requests[0][2].items()}
        self.assertNotIn("x-agiw-control-proof", headers)
        self.assertNotIn("x-agiw-control-nonce", headers)
        self.assertRegex(headers["x-agiw-client-nonce"], r"^[0-9a-f]{32}$")
        self.assertNotIn(VALID_TOKEN, json.dumps(squatter.requests))

    def test_genuine_peer_link_answers_through_the_mutual_handshake(self):
        self.put("control-token", VALID_TOKEN.encode() + b"\n")
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected", "status": presence(age=1)}]}
        link = self.listener(token=VALID_TOKEN, listing=listing)
        before = tree(self.dir)
        feed = peer_feed.from_peer_link(time.time(), self.dir, link.port)
        self.assertEqual((feed["via"], feed["host"]), ("lan", "winpc01"))
        self.assertEqual(tree(self.dir), before)
        self.assertEqual([path for _, path, _ in link.requests], ["/control/hello", "/control/peers"])
        self.assertNotIn(VALID_TOKEN, json.dumps(link.requests))


@unittest.skipIf(os.name == "nt", "the state folder rules checked here are POSIX modes")
class ObserverWithRunningPeerLink(unittest.TestCase):
    """Review finding A2 against the real peer link, not a fake listener. A running peer link checks its folder and
    token on every control request, the observer's hello included (load_control_token -> _restrict_dir sets the
    folder to 0700, strips its ACL and rotates a bad token). The observer must not reach it while that check would
    change anything; when it does reach it, the folder's mode, access list and files stay as they are."""

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, True)
        self.dir = base / "peer-link"
        self.link = peer_link.PeerLink(self.dir, "mac", "m", lambda: presence(platform="mac"), status_port=0,
                                       beacon_port=0, bind="127.0.0.1", broadcast=(), allow_loopback=True).start()
        self.addCleanup(self.link.stop)
        self.addCleanup(self.reset)  # cleanups run last-in first-out: the folder is writable again before rmtree
        self.token = (self.dir / "control-token").read_bytes()
        # Every check the running peer link makes, counted from here on; the observer never calls it itself.
        patcher = mock.patch.object(peer_link, "load_control_token", wraps=peer_link.load_control_token)
        self.checks = patcher.start()
        self.addCleanup(patcher.stop)
        self.http_calls = []
        real_http = peer_link._http

        def recording_http(*args, **kwargs):
            self.http_calls.append(args[3])
            return real_http(*args, **kwargs)

        patcher = mock.patch.object(peer_link, "_http", side_effect=recording_http)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset(self):
        os.chmod(self.dir, 0o700)
        if sys.platform == "darwin":
            subprocess.run([ACL_TOOL, "-N", str(self.dir)], capture_output=True, timeout=10)
        token = self.dir / "control-token"
        token.write_bytes(self.token)
        os.chmod(token, 0o600)

    def read(self):
        return peer_feed.from_peer_link(time.time(), self.dir, self.link.status_port)

    def test_state_the_peer_link_would_repair_is_never_contacted(self):
        token = self.dir / "control-token"
        cases = [(oct(mode), lambda mode=mode: os.chmod(self.dir, mode), "owner-only")
                 for mode in (0o750, 0o740, 0o710, 0o500, 0o1700)]
        cases += [("0757", lambda: os.chmod(self.dir, 0o757), "writable by other users"),
                  ("loose token", lambda: os.chmod(token, 0o640), "another user"),
                  ("malformed token", lambda: token.write_bytes(b"not-a-token\n"), "malformed")]
        if sys.platform == "darwin":
            me = pwd.getpwuid(os.getuid()).pw_name

            def with_acl():
                if subprocess.run([ACL_TOOL, "+a", f"user:{me} allow list", str(self.dir)],
                                  capture_output=True, timeout=10).returncode != 0:
                    self.skipTest("this volume does not take an extended ACL")
            cases.append(("folder ACL", with_acl, "access list"))
        for name, arrange, reason in cases:
            with self.subTest(name):
                self.reset()
                arrange()
                before = tree(self.dir)
                self.checks.reset_mock()
                self.http_calls.clear()
                with self.assertRaises(peer_feed.LinkUnavailable) as caught:
                    self.read()
                self.assertIn(reason, str(caught.exception))
                self.assertEqual(tree(self.dir), before, "the state folder changed")
                self.assertEqual((self.checks.call_count, self.http_calls), (0, []), "the peer link was contacted")

    def test_contact_leaves_mode_access_list_and_files_as_they_are(self):
        def without_change_times(snapshot):
            return {name: row[:3] + row[4:] for name, row in snapshot.items()}

        before = tree(self.dir)
        self.assertIsNone(self.read())  # nothing paired: the full handshake ran and the listing was empty
        self.assertEqual(self.http_calls, ["/control/hello", "/control/peers"])
        self.assertGreaterEqual(self.checks.call_count, 1, "the real peer link's own check did not run")
        after = tree(self.dir)
        # The peer link's re-check may re-stamp the folder's change time (chmod 0700 on a folder already 0700);
        # mode, owner, inodes, modification times, contents and access lists stay exactly as they were.
        self.assertEqual(without_change_times(after), without_change_times(before))
        self.assertEqual(stat.S_IMODE(os.lstat(self.dir).st_mode), 0o700)
        # Once the peer link has re-checked the folder, a further read changes nothing at all.
        self.assertIsNone(self.read())
        self.assertEqual(tree(self.dir), after)


if __name__ == "__main__":
    unittest.main()
