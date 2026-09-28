import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import live_feed
import server


def snapshot(sampled, **extra):
    base = {"schemaVersion": 1, "sampledAt": sampled, "fullSampledAt": sampled, "observedAt": "x",
            "models": [{"host": "mac", "id": "m", "name": "m", "state": "idle", "loaded": True,
                        "source": "lms-ps", "ageSeconds": 0.0}],
            "sources": [{"id": "lms-ps", "state": "live", "ageSeconds": 0.0},
                        {"id": "windows-jobs", "state": "live", "ageSeconds": 0.0}],
            "windowsWorker": {"state": "advertised", "ageSeconds": 4.0,
                              "headless": {"state": "on", "expiresInSeconds": 100}},
            "windowsJobs": {"schemaVersion": 1, "inFlight": [], "recent": [], "lastSuccess": None},
            "activity": {"runs": [{"runId": "r", "ageSeconds": 10.0}]}}
    base.update(extra)
    return base


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_tracks_content_changes_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jobs.jsonl"
            self.assertIsNone(live_feed.fingerprint(path))
            path.write_text("a\n")
            first = live_feed.fingerprint(path)
            self.assertEqual(first, live_feed.fingerprint(path))
            with path.open("a") as handle:
                handle.write("b\n")
            self.assertNotEqual(first, live_feed.fingerprint(path))

    def test_router_group_watches_both_journal_layouts(self):
        """Router concurrency (spec 6.12): a per-run record, note or run lock appearing or being
        replaced moves active/, notes/ or locks/; the fast path re-reads the router on either layout."""
        import telemetry
        root = Path("/fixture/router")
        with mock.patch.object(telemetry, "_ROUTER_ROOT", root):
            watched = live_feed._paths()["router"]
        for name in ("archive", "checkpoints", "active", "notes", "locks", "policy.json", "install-fence.json"):
            self.assertIn(root / name, watched)
        self.assertIn(telemetry._ACTIVE_PATH, watched)

    def test_changed_reports_only_the_groups_whose_files_moved(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "a", Path(tmp) / "b"
            a.write_text("1")
            b.write_text("1")
            feed = live_feed.LiveFeed(lambda: {"jobs": [a], "router": [b]})
            groups, prints = feed.changed()
            self.assertEqual(sorted(groups), ["jobs", "router"])
            feed.set_baseline(prints)
            self.assertEqual(feed.changed()[0], [])
            a.write_text("22")
            self.assertEqual(feed.changed()[0], ["jobs"])


class ShiftAgesTests(unittest.TestCase):
    def test_ages_grow_and_expiries_shrink_everywhere_but_booleans_and_nulls_stay(self):
        data = {"models": [{"ageSeconds": 1.0}, {"ageSeconds": None}], "flag": {"ageSeconds": True},
                "worker": {"headless": {"expiresInSeconds": 100}}, "deep": [[{"ageSeconds": 2}]]}
        live_feed.shift_ages(data, 0.5)
        self.assertEqual(data["models"][0]["ageSeconds"], 1.5)
        self.assertIsNone(data["models"][1]["ageSeconds"])
        self.assertIs(data["flag"]["ageSeconds"], True)
        self.assertEqual(data["worker"]["headless"]["expiresInSeconds"], 99)
        self.assertEqual(data["deep"][0][0]["ageSeconds"], 2.5)


class OverlayTests(unittest.TestCase):
    def test_overlay_rereads_only_changed_groups_and_keeps_absolute_freshness(self):
        base = snapshot(1000.0)
        fresh_jobs = {"schemaVersion": 1, "inFlight": [{"id": "j", "ageSeconds": 0.1}], "recent": [], "lastSuccess": None}
        with mock.patch.object(live_feed.telemetry, "_windows_jobs",
                               return_value=(fresh_jobs, {"id": "windows-jobs", "state": "live", "ageSeconds": 0.0, "detail": "new"})), \
                mock.patch.object(live_feed.telemetry, "_windows_headless") as headless, \
                mock.patch.object(live_feed.telemetry, "_pipeline") as pipeline:
            out = live_feed.LiveFeed(lambda: {}).overlay(base, ["jobs"], now=1000.4)
        headless.assert_not_called()
        pipeline.assert_not_called()
        self.assertEqual(out["windowsJobs"], fresh_jobs)
        self.assertEqual(out["sampledAt"], 1000.4)
        self.assertEqual(out["fullSampledAt"], 1000.0)
        self.assertAlmostEqual(out["models"][0]["ageSeconds"], 0.4)
        self.assertAlmostEqual(out["windowsWorker"]["ageSeconds"], 4.4)
        self.assertEqual(out["windowsWorker"]["headless"]["expiresInSeconds"], 99)
        self.assertEqual([s["detail"] for s in out["sources"] if s["id"] == "windows-jobs"], ["new"])
        self.assertEqual(len([s for s in out["sources"] if s["id"] == "windows-jobs"]), 1)
        self.assertEqual(out["liveGroups"], ["jobs"])
        self.assertEqual(base["sampledAt"], 1000.0, "the stored snapshot must not be mutated")

    def test_overlay_refuses_a_future_snapshot(self):
        with self.assertRaises(ValueError):
            live_feed.LiveFeed(lambda: {}).overlay(snapshot(2000.0), [], now=1999.0)


class StoreTests(unittest.TestCase):
    def test_wait_for_change_wakes_on_publish_and_times_out_otherwise(self):
        store = server.SnapshotStore()
        self.assertEqual(store.wait_for_change(None, 0.05), (None, None))
        store.publish(snapshot(time.time()))
        data, seq = store.wait_for_change(None, 0.05)
        self.assertEqual(seq, 1)
        self.assertEqual(store.wait_for_change(seq, 0.05), (None, 1))
        threading.Timer(0.05, lambda: store.publish(snapshot(time.time()), partial=True)).start()
        started = time.monotonic()
        data, seq = store.wait_for_change(1, 2.0)
        self.assertEqual(seq, 2)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_partial_publish_adds_no_history_point(self):
        store = server.SnapshotStore()
        store.publish(snapshot(time.time()))
        store.publish(snapshot(time.time()), partial=True)
        self.assertEqual(len(store.read()["history"]), 1)
        store.publish(snapshot(time.time()))
        self.assertEqual(len(store.read()["history"]), 2)


class SamplerTests(unittest.TestCase):
    def test_fast_path_skips_overlays_when_the_full_sample_is_stale(self):
        store = server.SnapshotStore()
        stale = snapshot(time.time() - 10)
        store.publish(stale)
        feed = mock.Mock()
        feed.changed.return_value = (["jobs"], {"jobs": (1,)})
        stop = threading.Event()
        with mock.patch.object(server, "collect_snapshot", side_effect=RuntimeError("sampler down")):
            thread = threading.Thread(target=server.sample, args=(store, stop, feed), kwargs={"tick": 0.02, "full_interval": 5.0})
            thread.start()
            time.sleep(0.2)
            stop.set()
            thread.join(2)
        feed.overlay.assert_not_called()
        self.assertEqual(store.read()["sequence"], 1)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.store = server.SnapshotStore()
        self.store.publish(snapshot(time.time()))
        with mock.patch.object(server, "OnlineCodeRepair", return_value=None):
            self.srv = server.MonitorServer(("127.0.0.1", 0), self.store)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.stopping.set()
        self.srv.shutdown()
        self.srv.server_close()

    def open_stream(self, host=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(f"GET /api/stream HTTP/1.1\r\nHost: {host or f'127.0.0.1:{self.port}'}\r\n\r\n".encode())
        return sock

    @staticmethod
    def read_until(sock, marker, timeout=5.0):
        # Overall deadline: heartbeats keep the socket busy, so a per-recv timeout alone never fires.
        deadline = time.monotonic() + timeout
        buffer = b""
        while marker not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"{marker!r} not received within {timeout}s; got {buffer[-200:]!r}")
            sock.settimeout(remaining)
            part = sock.recv(65536)
            if not part:
                break
            buffer += part
        return buffer

    def test_stream_pushes_the_current_and_every_new_snapshot_and_outlives_the_deadline(self):
        sock = self.open_stream()
        try:
            first = self.read_until(sock, b"event: snapshot", 5)
            self.assertIn(b"200", first.split(b"\r\n")[0])
            self.assertIn(b"text/event-stream", first)
            time.sleep(2.5)  # past the ordinary 2 s connection deadline
            started = time.monotonic()
            self.store.publish(snapshot(time.time(), marker="second"), partial=True)
            pushed = self.read_until(sock, b'"marker":"second"', 5)
            self.assertIn(b'"marker":"second"', pushed)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            sock.close()

    def test_stream_rejects_foreign_hosts_and_caps_concurrent_streams(self):
        bad = self.open_stream(host="evil.example")
        try:
            self.assertIn(b"403", self.read_until(bad, b"\r\n", 5))
        finally:
            bad.close()
        streams = [self.open_stream() for _ in range(self.srv.max_streams)]
        try:
            for sock in streams:
                self.read_until(sock, b"event: snapshot", 5)
            extra = self.open_stream()
            try:
                self.assertIn(b"503", self.read_until(extra, b"\r\n", 5))
            finally:
                extra.close()
        finally:
            for sock in streams:
                sock.close()


if __name__ == "__main__":
    unittest.main()
