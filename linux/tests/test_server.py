import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import server
from agiw_linux.collect import project_status, unknown_snapshot


class Output(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / "state"
        self.collector = mock.Mock()
        self.collector.collect.side_effect = lambda: unknown_snapshot(now=100, host="fixture")

    def heartbeat(self, at=100):
        value = project_status(unknown_snapshot(now=at, host="fixture"))
        server.write_status(self.state, value)
        return value

    def test_fresh_cache_avoids_probes_and_preserves_time(self):
        self.heartbeat(95)
        value = json.loads(server.once(self.collector, state_dir=self.state, clock=lambda: 100))
        self.collector.collect.assert_not_called()
        self.assertEqual(value["observedUnix"], 95)

    def test_stale_missing_future_or_invalid_cache_samples_twice(self):
        for at in (89, 101, None):
            with self.subTest(at=at):
                if at is not None:
                    self.heartbeat(at)
                else:
                    (self.state / "linux-status.json").write_text('{"schemaVersion":2}')
                sleep = mock.Mock()
                before = self.collector.collect.call_count
                value = json.loads(server.once(self.collector, state_dir=self.state, clock=lambda: 100, sleep=sleep))
                self.assertEqual(self.collector.collect.call_count - before, 2)
                sleep.assert_called_once_with(1.0)
                self.assertEqual(value["components"][-1]["state"], "unknown")
                self.assertEqual(value["components"][-1]["reason"], "heartbeat-not-fresh-live-fallback")

    def test_cache_reprojection_drops_extra_fields(self):
        value = self.heartbeat()
        value["private"] = "must not leave node"
        server.write_status(self.state, value)
        self.assertNotIn("private", json.loads(server.once(self.collector, state_dir=self.state, clock=lambda: 100)))

    def test_cache_requires_matching_timestamp_and_valid_rows(self):
        for change in ({"observedAt": "bad"}, {"observedAt": 123}, {"host": 123}, {"memory": ["bad"]},
                       {"gpus": "bad"}, {"components": {}}, {"models": [None]}, {"platform": "windows"},
                       {"schemaVersion": True}, {"models": [{"id": "m", "loadedState": "loaded"}] * 2}):
            value = self.heartbeat()
            value.update(change)
            server.write_status(self.state, value)
            self.assertIsNone(server.cached_status(self.state / "linux-status.json", 100))

    def test_cache_requires_private_owner_file_and_parent(self):
        self.heartbeat()
        path = self.state / "linux-status.json"
        path.chmod(0o666)
        self.assertIsNone(server.cached_status(path, 100))
        path.chmod(0o600)
        self.state.chmod(0o755)
        self.assertIsNone(server.cached_status(path, 100))
        self.state.chmod(0o700)
        os.link(path, self.state / "duplicate")
        self.assertIsNone(server.cached_status(path, 100))

    def test_boolean_model_count_cannot_claim_completeness(self):
        value = self.heartbeat()
        value.update(models=[{"id": "m", "loadedState": "loaded"}], modelsTotal=True, modelsComplete=True)
        server.write_status(self.state, value)
        self.assertFalse(server.cached_status(self.state / "linux-status.json", 100)["modelsComplete"])

    def test_once_does_not_create_state_directory(self):
        server.once(self.collector, state_dir=self.state, clock=lambda: 100, sleep=lambda _: None)
        self.assertFalse(self.state.exists())

    def test_output_cap_fails_closed(self):
        self.collector.collect.side_effect = ValueError("oversize")
        value = server.once(self.collector, state_dir=self.state, clock=lambda: 100, sleep=lambda _: None)
        self.assertLessEqual(len(value), 65536)
        self.assertEqual(json.loads(value)["health"], "unknown")
        with self.assertRaises(ValueError):
            server.encode({"large": "x" * 65536})

    def test_real_deadline_stops_blocked_probe_before_four_seconds(self):
        self.collector.collect.side_effect = lambda: time.sleep(10)
        start = time.monotonic()
        value = json.loads(server.once(self.collector, state_dir=self.state))
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual(value["reason"], "sample-deadline")
        self.assertIsNone(value["modelsTotal"])

    def test_deadline_is_not_swallowed_by_oserror_guards(self):
        with self.assertRaises(server.SampleDeadline):
            with server.deadline(.01):
                try:
                    time.sleep(.1)
                except OSError:
                    self.fail("deadline swallowed")

    def test_atomic_mode_and_no_extra_files(self):
        value = self.heartbeat()
        path = self.state / "linux-status.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        value["health"] = "up"
        server.write_status(self.state, value)
        self.assertEqual(json.loads(path.read_text())["health"], "up")
        self.assertEqual([p.name for p in self.state.iterdir()], ["linux-status.json"])

    def test_parent_and_target_links_are_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.state.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.heartbeat()
        self.assertEqual(list(outside.iterdir()), [])
        self.state.unlink()
        self.state.mkdir(mode=0o700)
        (self.state / "linux-status.json").symlink_to(outside / "victim")
        with self.assertRaises(OSError):
            self.heartbeat()
        self.assertFalse((outside / "victim").exists())

    def test_public_state_directory_refused(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        with self.assertRaises(PermissionError):
            self.heartbeat()

    def test_parent_swap_cannot_redirect_publication(self):
        self.state.mkdir(mode=0o700)
        moved, outside = self.root / "old", self.root / "outside"
        outside.mkdir()
        real_replace = os.replace
        def replace(*args, **kwargs):
            self.state.rename(moved)
            self.state.symlink_to(outside, target_is_directory=True)
            return real_replace(*args, **kwargs)
        with mock.patch.object(server.os, "replace", side_effect=replace):
            self.heartbeat()
        self.assertTrue((moved / "linux-status.json").is_file())
        self.assertEqual(list(outside.iterdir()), [])

    def test_isolated_entrypoint_reads_fixture_only(self):
        value = self.heartbeat(time.time())
        env = dict(os.environ, XDG_STATE_HOME=str(self.root))
        self.state.rename(self.root / "agiw")
        entry = Path(__file__).resolve().parents[1] / "agiw-linux-status"
        run = subprocess.run([sys.executable, "-I", "-B", str(entry), "--once"], env=env,
                             capture_output=True, timeout=4)
        self.assertEqual(run.returncode, 0, run.stderr.decode())
        self.assertEqual(json.loads(run.stdout)["observedUnix"], value["observedUnix"])
        self.assertLessEqual(len(run.stdout), 65536)

    def test_serve_default_publishes_without_listener(self):
        stop = threading.Event()
        original = server.write_status
        def write(*args):
            original(*args)
            stop.set()
        with mock.patch.object(server, "MonitorServer") as listener, \
                mock.patch.object(server, "write_status", side_effect=write), \
                mock.patch.object(server, "RELEASE_COMMIT", "a" * 40):
            server.serve(self.collector, state_dir=self.state, stop=stop, clock=lambda: 100)
        listener.assert_not_called()
        value = json.loads((self.state / "linux-status.json").read_text())
        self.assertEqual(value["components"][-1]["state"], "up")
        self.assertEqual(value["releaseCommit"], "a" * 40)

    def test_running_process_keeps_original_release_after_current_repoints(self):
        import shutil
        first, second = self.root / ("a" * 40), self.root / ("b" * 40)
        shutil.copytree(Path(server.__file__).parent, first / "agiw_linux")
        second.mkdir()
        current = self.root / "current"
        current.symlink_to(first, target_is_directory=True)
        program = '''
import json, sys
from pathlib import Path
current, second = map(Path, sys.argv[1:])
sys.path.insert(0, str(current))
from agiw_linux import server
before = server.RELEASE_COMMIT
current.unlink()
current.symlink_to(second, target_is_directory=True)
print(json.dumps([before, server.RELEASE_COMMIT]))
'''
        run = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(current), str(second)],
                             capture_output=True, timeout=4, check=True)
        self.assertEqual(json.loads(run.stdout), ["a" * 40, "a" * 40])


class Transport(unittest.TestCase):
    def test_stalled_output_pipe_has_a_deadline(self):
        read, write = os.pipe()
        try:
            os.set_blocking(write, False)
            while True:
                try:
                    os.write(write, b"x" * 65536)
                except BlockingIOError:
                    break
            start = time.monotonic()
            self.assertFalse(server.write_output(b"{}\n", write, start + .05))
            self.assertLess(time.monotonic() - start, .3)
        finally:
            os.close(read)
            os.close(write)

    def test_output_roundtrip_is_exact(self):
        read, write = os.pipe()
        try:
            self.assertTrue(server.write_output(b"{}\n", write, time.monotonic() + .1))
            self.assertEqual(os.read(read, 100), b"{}\n")
        finally:
            os.close(read)
            os.close(write)

    def test_loopback_http_roundtrip(self):
        import http.client
        store = server.SnapshotStore()
        store.publish(unknown_snapshot())
        try:
            monitor = server.MonitorServer(("127.0.0.1", 0), store)
        except PermissionError as error:
            self.skipTest(f"sandbox socket bind refused: {error}")
        thread = threading.Thread(target=monitor.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", monitor.server_address[1], timeout=1)
            client.request("GET", "/api/status")
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["platform"], "linux")
            client.close()
        finally:
            monitor.shutdown()
            monitor.server_close()
            thread.join(1)

    def test_nonloopback_rejected_before_binding(self):
        with self.assertRaises(ValueError):
            server.MonitorServer(("0.0.0.0", 0), server.SnapshotStore())

    def test_stale_and_future_samples_lose_known_counts(self):
        store = server.SnapshotStore(clock=lambda: 100)
        for at in (89, 101):
            snapshot = unknown_snapshot(now=at)
            snapshot.update(health="up", modelsComplete=True)
            store.publish(snapshot)
            self.assertEqual(store.read()["health"], "unknown")
            self.assertFalse(store.read()["modelsComplete"])

    def test_handler_routes_and_origin_without_socket(self):
        store = server.SnapshotStore(clock=lambda: 100)
        store.publish(unknown_snapshot(now=100))
        handler = object.__new__(server.Handler)
        handler.server = mock.Mock(server_address=("127.0.0.1", 8766), store=store)
        handler.headers = {"Host": "127.0.0.1:8766"}
        handler.path = "/api/status"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        handler.do_GET()
        handler.send_response.assert_called_with(200)
        self.assertEqual(json.loads(handler.wfile.getvalue())["platform"], "linux")
        handler.headers["Origin"] = "http://evil.invalid"
        handler.do_GET()
        handler.send_error.assert_called_with(403)
        handler.headers.pop("Origin")
        handler.path = "/repair"
        handler.do_GET()
        handler.send_error.assert_called_with(404)
        handler.do_POST()
        handler.send_error.assert_called_with(405, "Read-only observer")


class XDG(unittest.TestCase):
    def test_peer_link_linux_default_and_absolute_override(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import peer_link
        with mock.patch.object(peer_link.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": "/fixture/state"}):
            self.assertEqual(peer_link.default_state_dir(), Path("/fixture/state/agiw/peer-link"))
        with mock.patch.object(peer_link.sys, "platform", "linux"), \
                mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}):
            self.assertEqual(peer_link.default_state_dir(), Path.home() / ".local/state/agiw/peer-link")

    def test_observer_relative_xdg_falls_back(self):
        with mock.patch.dict(os.environ, {"XDG_STATE_HOME": "relative"}):
            self.assertEqual(server.default_state_dir(), Path.home() / ".local/state/agiw")


if __name__ == "__main__":
    unittest.main()
