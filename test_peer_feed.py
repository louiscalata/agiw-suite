import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import peer_feed
import peer_link


def presence(age=2.0, platform="windows", **extra):
    now = time.time() - age
    return {"schemaVersion": 1, "kind": "agiw-peer", "host": "louisaurorar12", "platform": platform,
            "observedAt": peer_link._iso(now), "observedUnix": now, "health": "ok",
            "models": [{"id": "gpt-oss-20b", "loadedState": "loaded", "activity": "idle", "slotsBusy": 0,
                        "slotsTotal": 2, "liveTokensPerSecond": 104.27, "secretish": "/C:/Users/x"}],
            "gpus": [{"name": "RTX 3080 Ti", "utilizationPercent": 4.0, "path": "C:\\nope"}],
            "memory": {"level": "ok", "availablePercent": 55.6, "commit": 1}, **extra}


class Share(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.root = self.base / "llm-lab" / "status"
        (self.root / "agiw-peers").mkdir(parents=True)

    def put(self, name, doc):
        (self.root / name).write_text(json.dumps(doc))

    def test_fresh_presence_is_live_and_only_known_fields_pass(self):
        self.put("agiw-peers/louisaurorar12.json", presence())
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertEqual((feed["via"], feed["host"], feed["models"][0]["slotsTotal"]), ("share", "louisaurorar12", 2))
        self.assertNotIn("secretish", feed["models"][0])
        self.assertNotIn("path", feed["gpus"][0])
        self.assertNotIn("commit", feed["memory"])
        self.assertEqual(feed["models"][0]["liveTokensPerSecond"], 104.3)

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
        self.put("agiw-peers/louisaurorar12.json", presence())
        moved = self.base / "real-llm-lab"
        (self.base / "llm-lab").rename(moved)
        os.symlink(moved, self.base / "llm-lab")
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_infinite_numbers_and_odd_memory_values_are_dropped(self):
        doc = presence()
        self.put("agiw-peers/louisaurorar12.json", doc)
        raw = (self.root / "agiw-peers" / "louisaurorar12.json").read_text()
        raw = raw.replace('"utilizationPercent": 4.0', '"utilizationPercent": 1e999')
        raw = raw.replace('"availablePercent": 55.6', '"availablePercent": {"nested": 1}')
        (self.root / "agiw-peers" / "louisaurorar12.json").write_text(raw)
        feed = peer_feed.from_share(time.time(), self.root, self.base)
        self.assertNotIn("utilizationPercent", feed["gpus"][0])
        self.assertEqual(feed["memory"], {"level": "ok"})
        json.dumps(feed, allow_nan=False)  # the snapshot must stay serializable

    def test_future_dated_record_is_a_clock_mismatch_not_fresh(self):
        self.put("agiw-peers/louisaurorar12.json", presence(age=-45))
        self.assertIsNone(peer_feed.from_share(time.time(), self.root, self.base))

    def test_missing_share_is_none(self):
        self.assertIsNone(peer_feed.from_share(time.time(), Path("/nonexistent/llm-lab/status")))


class Feed(unittest.TestCase):
    def test_stale_lan_result_falls_back_to_a_fresh_share(self):
        token_dir = Path(tempfile.mkdtemp())
        (token_dir / "control-token").write_text("a" * 64)
        stale = presence(age=45)
        listing = {"peers": [{"paired": True, "host": "windows", "state": "connected", "status": stale}]}
        with mock.patch.object(peer_link, "default_state_dir", return_value=token_dir), \
                mock.patch.object(peer_link, "control_request", return_value=(200, listing)):
            self.assertIsNone(peer_feed.from_peer_link(time.time()))
            listing["peers"][0]["status"] = presence(age=2)
            self.assertEqual(peer_feed.from_peer_link(time.time())["via"], "lan")

    def test_no_peer_link_setup_means_no_lan_attempt_and_nothing_created(self):
        empty = Path(tempfile.mkdtemp()) / "peer-link"
        with mock.patch.object(peer_link, "default_state_dir", return_value=empty), \
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
        # The map's words (REPORTED VIA SHARE) and the PC's: an unsigned share file is reported, never "linked".
        self.assertEqual(source["detail"], "Reported via SharedChami status files from louisaurorar12 "
                                           "(unsigned, not verified; LAN link not connected)")
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


if __name__ == "__main__":
    unittest.main()
