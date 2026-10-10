"""End to end: fake llama-server lanes on real sockets -> collector -> store -> HTTP + SSE."""
import http.client
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402
from agiw_win.collect import Collector  # noqa: E402
from agiw_win.server import MonitorServer, SnapshotStore, activity_is_known, find_web_root, sample  # noqa: E402


def lane_server(model, state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = {"/health": {"status": "ok"}, "/v1/models": {"data": [{"id": model}]},
                    "/slots": [{"id": 0, "is_processing": state["busy"], "n_ctx": 8192,
                                "next_token": [{"n_decoded": 4 if state["busy"] else 0}]},
                               {"id": 1, "is_processing": False, "n_ctx": 8192}]}.get(self.path)
            payload = json.dumps(body).encode()
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.fast_state, self.deep_state = {"busy": False}, {"busy": False}
        self.fast = lane_server("openai/gpt-oss-20b", self.fast_state)
        self.deep = lane_server("qwen3.8-27b", self.deep_state)
        self.tmp = tempfile.TemporaryDirectory()
        share = Path(self.tmp.name)
        (share / "windows-llm-pipeline").mkdir()
        (share / "windows-llm-pipeline" / "config.json").write_text(json.dumps({"runtime": {"lanes": {
            "fast": {"port": self.fast.server_address[1], "alias": "openai/gpt-oss-20b", "device": "CUDA0"},
            "deep": {"port": self.deep.server_address[1], "alias": "qwen3.8-27b", "device": "none"}}}}))
        (share / "llm-lab").mkdir()
        (share / "llm-lab" / "config.json").write_text("{}")
        gib = 2 ** 30
        self.collector = Collector(
            share, share / "bin", lane_interval=0.05, lane_wait=3.0,
            gpu_fn=lambda: ([{"index": 0, "name": "RTX", "utilizationPercent": 10.0, "memoryUsedMiB": 100.0,
                              "memoryTotalMiB": 12288.0, "temperatureC": 40.0, "powerW": 50.0, "vendor": "nvidia"}],
                            probes._source("pc-gpu", "PC GPU", "live", "ok")),
            adapters_fn=lambda: [{"name": "AMD Radeon RX 5700 XT", "memoryBytes": 8 * gib}],
            memory_fn=lambda: probes.memory_block(lambda: {"totalPhys": 64 * gib, "availPhys": 32 * gib,
                                                           "commitLimit": 80 * gib, "commitAvail": 50 * gib}))
        self.store = SnapshotStore()
        self.server = MonitorServer(("127.0.0.1", 0), self.store, find_web_root(Path(__file__).resolve().parents[1]))
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        for s in (self.fast, self.deep):
            s.shutdown()
            s.server_close()
        self.collector.close()
        self.tmp.cleanup()

    def get(self, path, host=None, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if origin:
            headers["Origin"] = origin
        conn.request("GET", path, headers=headers)
        response = conn.getresponse()
        return response.status, response.read(), response

    def test_snapshot_round_trip(self):
        self.assertEqual(self.get("/api/snapshot")[0], 503)
        self.store.publish(self.collector.collect())
        status, body, _ = self.get("/api/snapshot")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["host"], "windows")
        self.assertTrue(data["activityKnown"])
        self.assertEqual({m["id"]: m["state"] for m in data["models"]},
                         {"openai/gpt-oss-20b": "idle", "qwen3.8-27b": "idle"})
        worker = data["windowsWorker"]
        self.assertEqual(worker["state"], "advertised")
        self.assertEqual(worker["lanes"]["fast"]["slotsTotal"], 2)
        self.assertEqual(data["pcAdapters"][0]["name"], "AMD Radeon RX 5700 XT")
        self.assertEqual(data["memory"]["level"], "ok")
        self.assertEqual(data["share"]["state"], "ready")

    def test_activity_transition_is_recorded(self):
        self.store.publish(self.collector.collect())
        self.deep_state["busy"] = True
        time.sleep(0.4)
        self.store.publish(self.collector.collect())
        data = self.store.read()
        deep = next(m for m in data["models"] if m["id"] == "qwen3.8-27b")
        self.assertEqual(deep["state"], "generating")
        self.assertEqual(data["events"][0]["state"], "generating")
        self.assertEqual(data["history"][-1]["active"], 1)

    def test_lane_down(self):
        self.fast.shutdown()
        self.fast.server_close()
        time.sleep(1.0)
        data = self.collector.collect()
        fast = next(m for m in data["models"] if m["id"] == "openai/gpt-oss-20b")
        self.assertEqual((fast["state"], fast["loaded"]), ("unloaded", False))
        self.assertFalse(activity_is_known(data["models"], {s["id"]: s["state"] for s in data["sources"]}))

    def test_rejects_foreign_host_and_origin(self):
        self.store.publish(self.collector.collect())
        self.assertEqual(self.get("/api/snapshot", host="evil.example")[0], 403)
        self.assertEqual(self.get("/api/snapshot", origin="http://evil.example")[0], 403)

    def test_assets_and_health(self):
        status, body, response = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body.lower())
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))
        self.assertEqual(self.get("/usage-format.mjs")[0], 200)
        self.assertEqual(json.loads(self.get("/api/health")[1])["service"], "agiw-win-observer")
        self.assertEqual(self.get("/etc/passwd")[0], 404)
        self.assertEqual(json.loads(self.get("/api/models/control")[1])["supported"], False)

    def test_stream_delivers_snapshot(self):
        stop = threading.Event()
        threading.Thread(target=sample, args=(self.store, self.collector, stop, 0.2), daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.port}"})
            response = conn.getresponse()
            self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
            buffer = b""
            deadline = time.time() + 4
            while b"event: snapshot" not in buffer and time.time() < deadline:
                buffer += response.read1(65536)
            self.assertIn(b"event: snapshot", buffer)
            conn.close()
        finally:
            stop.set()

    def test_post_is_read_only(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        origin = f"http://127.0.0.1:{self.port}"
        conn.request("POST", "/api/models/control", body=b"{}", headers={"Host": f"127.0.0.1:{self.port}",
                                                                          "Origin": origin, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 501)


if __name__ == "__main__":
    unittest.main()
