"""End to end: fake llama-server lanes on real sockets -> collector -> store -> HTTP + SSE."""
import http.client
import json
import os
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

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
            share, share / "bin", lane_interval=0.05, lane_wait=3.0, runs_root=share / "code-runs",
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
        self.assertEqual((fast["state"], fast["loaded"]), ("unknown", None))
        self.assertEqual(data["windowsWorker"]["lanes"]["fast"]["up"], False)
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

    def test_windows_status_matches_the_mac_reader(self):
        data = self.collector.collect()
        status = probes.pc_status(data)
        probes.validate_status(status)
        self.assertEqual((status["schemaVersion"], status["host"], status["health"]), (1, "windows", "ok"))
        # The Mac reader reconciles on chami-dispatch inventory names, so those are the ids.
        self.assertEqual({m["id"] for m in status["models"]}, {"gpt-oss-20b", "Qwen3.8-27B Q4_K_M"})
        self.assertEqual({m["aliases"][0] for m in status["models"]}, {"openai/gpt-oss-20b", "qwen3.8-27b"})
        self.assertTrue(all(m["loadedState"] == "loaded" and m["activity"] == "idle" for m in status["models"]))
        self.assertNotIn("clients", status)
        target = Path(self.tmp.name) / "llm-lab" / "status" / probes.STATUS_FILE_NAME
        probes.write_json_atomic(target, status, probes.STATUS_MAX_BYTES)
        self.assertEqual(target.name, "windows-status.json")
        self.assertLess(target.stat().st_size, probes.STATUS_MAX_BYTES)

    def test_dormant_pairing_reader_is_separate_from_the_share_and_published_status(self):
        reported = [{"node": "22222222-2222-4222-8222-222222222222", "name": "test-mac",
                     "host": "mac", "paired": True, "state": "connecting", "status": None}]
        source = probes._source("peer-link", "Peer link", "live", "Pairing service reports 1 paired, 0 connected")
        with mock.patch.object(probes, "read_peer_link", return_value=(reported, source)) as read:
            off = self.collector.collect()
            read.assert_not_called()
            self.assertNotIn("pairedPeers", off)
            self.assertFalse(any(s["id"] == "peer-link" for s in off["sources"]))
            self.collector.peer_link_reader = True
            on = self.collector.collect()
            read.assert_called_once_with(on["sampledAt"], enabled=True)
        self.assertEqual(on["pairedPeers"], reported)
        self.assertIn(source, on["sources"])
        self.assertEqual(on["macPeer"], off["macPeer"])
        self.assertEqual(on["peers"], off["peers"])
        published = probes.pc_status(on)
        probes.validate_status(published)
        self.assertNotIn("pairedPeers", published)
        self.assertEqual([m["id"] for m in published["models"]], ["gpt-oss-20b", "Qwen3.8-27B Q4_K_M"])

    def test_pairing_reader_failure_preserves_the_observer_sample(self):
        self.collector.peer_link_reader = True
        with mock.patch.object(probes, "read_peer_link", side_effect=RuntimeError("private path/PIN")):
            data = self.collector.collect()
        self.assertEqual(data["pairedPeers"], [])
        source = next(s for s in data["sources"] if s["id"] == "peer-link")
        self.assertEqual(source["state"], "error")
        self.assertNotIn("private", json.dumps(source))
        self.assertEqual(data["host"], "windows")
        probes.validate_status(probes.pc_status(data))

    def test_status_validation_refuses_what_the_reader_rejects(self):
        status = probes.pc_status(self.collector.collect())
        for broken in ({**status, "host": "mac"}, {**status, "models": [{**status["models"][0], "loadedState": "busy"}]},
                       {**status, "models": [{**status["models"][0], "id": ""}]}, {**status, "models": status["models"] * 17}):
            with self.assertRaises(ValueError):
                probes.validate_status(broken)
        with self.assertRaises(ValueError):
            probes.write_json_atomic(Path(self.tmp.name) / "x.json", {"pad": "x" * 70000}, probes.STATUS_MAX_BYTES)

    def test_presence_round_trip_links_a_second_instance(self):
        self.configure_mac(hostname="otherpc")
        data = self.collector.collect()
        share = Path(self.tmp.name)
        probes.write_json_atomic(probes.peer_dir(share) / "otherpc.json",
                                 {**probes.peer_presence(data, "otherpc"), "platform": "macos"}, probes.PEER_MAX_BYTES)
        again = self.collector.collect()
        self.assertEqual([p["host"] for p in again["peers"]], ["otherpc"])
        self.assertTrue(again["peers"][0]["fresh"])
        # The Mac LAN probe is off in this test, so the share link is what connects the Mac node.
        self.assertEqual(again["macPeer"]["state"], "linked")
        self.assertEqual(again["macPeer"]["loadedCount"], 2)

    def test_a_stalled_poller_is_stale_not_live(self):
        # Codex finding 4: an old idle result must not keep the lane looking live.
        self.collector.collect()
        poller = next(p for k, p in self.collector._pollers.items() if k[0] == "deep")
        poller.stop()
        at, lane = poller.read()
        poller._latest = (at - 10, lane)
        data = self.collector.collect()
        deep = data["windowsWorker"]["lanes"]["deep"]
        self.assertEqual((deep["status"], deep["up"]), ("stale", False))
        self.assertNotEqual(next(s for s in data["sources"] if s["id"] == "llama-slots")["state"], "live")
        self.assertFalse(activity_is_known(data["models"], {s["id"]: s["state"] for s in data["sources"]}))

    def test_lane_expiry_uses_unrounded_poll_time(self):
        from contextlib import ExitStack
        observed = 1000.0
        self.collector.clock = lambda: observed
        self.fast_state["busy"] = True
        self.collector.collect()
        with ExitStack() as patches:
            for poller in self.collector._pollers.values():
                _, lane = poller.read()
                frozen = {**lane, "liveTokensPerSecond": 12.5 if lane["id"] == "fast" else None}
                patches.enter_context(mock.patch.object(poller, "read", return_value=(observed, frozen)))
            for age in (2.999, 3.0, 3.004, 3.005):
                with self.subTest(age=age):
                    self.collector.clock = lambda: observed + age
                    snap = self.collector.collect()
                    self.store.publish(snap)
                    snap = self.store.read()
                    lane = snap["windowsWorker"]["lanes"]["fast"]
                    model = next(m for m in snap["models"] if m["id"] == "openai/gpt-oss-20b")
                    sources = {s["id"]: s["state"] for s in snap["sources"]}
                    self.assertEqual(lane["ageSeconds"], 3.0)
                    if age <= 3:
                        self.assertEqual((lane["status"], lane["up"], lane["slotsBusy"], lane["slotsTotal"]),
                                         ("busy", True, 1, 2))
                        self.assertEqual((model["state"], model["loaded"]), ("generating", True))
                        self.assertEqual(model["metadata"]["liveTokensPerSecond"], 12.5)
                        self.assertEqual((sources["llama-slots"], sources["lane-inventory"]), ("live", "live"))
                        self.assertTrue(snap["activityKnown"])
                    else:
                        self.assertEqual((lane["status"], lane["up"], lane["slotsBusy"], lane["slotsTotal"]),
                                         ("stale", False, None, 2))
                        self.assertEqual((model["state"], model["loaded"]), ("unknown", None))
                        self.assertIsNone(model["metadata"]["liveTokensPerSecond"])
                        self.assertEqual((sources["llama-slots"], sources["lane-inventory"]),
                                         ("unavailable", "unavailable"))
                        self.assertFalse(snap["activityKnown"])
                        self.assertFalse(activity_is_known(snap["models"], sources))

    def test_lane_inventory_rejects_stale_status_at_a_rounded_three_seconds(self):
        self.collector.collect()
        cached = {poller.spec["id"]: poller.read()[1] for poller in self.collector._pollers.values()}
        for status, expected in (("stale", "unavailable"), ("unreachable", "live")):
            with self.subTest(status=status):
                def lane_result(spec, _sampled):
                    return {**cached[spec["id"]], "status": status, "ageSeconds": 3.0,
                            "phase": None, "slotsBusy": None, "liveTokensPerSecond": None}
                with mock.patch.object(self.collector, "_lane", side_effect=lane_result):
                    snap = self.collector.collect()
                self.store.publish(snap)
                snap = self.store.read()
                sources = {s["id"]: s["state"] for s in snap["sources"]}
                self.assertEqual(sources["lane-inventory"], expected)
                self.assertEqual(sources["llama-slots"], "unavailable")
                self.assertFalse(snap["activityKnown"])
                self.assertTrue(all(m["loaded"] is None for m in snap["models"]))

    def test_one_broken_probe_does_not_lose_the_sample(self):
        # Codex finding 1 (general): a crashing probe becomes an error source.
        def broken():
            raise RuntimeError("boom")
        self.collector.memory_fn = broken
        data = self.collector.collect()
        self.assertEqual(data["memory"]["level"], "unknown")
        self.assertTrue(any(s["id"] == "mem-guard" and s["state"] == "error" for s in data["sources"]))

    def test_port_in_use_fails_clearly(self):
        # Codex finding 2: never move to another port.
        from agiw_win.server import PortUnavailable, bind
        with self.assertRaises(PortUnavailable):
            bind(SnapshotStore(), self.server.web_root, self.port, retry_seconds=0.2)

    def test_slow_client_is_cut_off(self):
        # Codex finding 3: a client that never finishes its request is closed by the deadline.
        import socket as s_
        conn = s_.create_connection(("127.0.0.1", self.port), timeout=6)
        conn.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n")
        started = time.time()
        try:
            data = conn.recv(10)
        except OSError:
            data = b""
        self.assertEqual(data, b"")
        self.assertLess(time.time() - started, 5.5)
        conn.close()

    def test_removed_lane_does_not_keep_old_evidence(self):
        # Codex round 2, finding 4: an undeclared lane must not be served from its old poller.
        self.collector.collect()
        share = Path(self.tmp.name)
        config = share / "windows-llm-pipeline" / "config.json"
        data = json.loads(config.read_text())
        del data["runtime"]["lanes"]["deep"]
        config.write_text(json.dumps(data))
        snap = self.collector.collect()
        self.assertEqual(snap["windowsWorker"]["lanes"]["deep"]["status"], "undeclared")
        self.assertFalse(any(k[0] == "deep" for k in self.collector._pollers))

    def test_failed_tcp_read_is_unknown_not_empty(self):
        # Codex round 2, finding 11.
        class Broken:
            def sample(self, _ports):
                return {}, [], {"id": "lane-processes", "label": "Lane processes", "state": "unavailable",
                                "ageSeconds": None, "detail": "no table"}
        self.collector.lane_processes = Broken()
        snap = self.collector.collect()
        self.assertIsNone(snap["localCallers"])
        self.assertIsNone(snap["models"][0]["metadata"]["callers"])

    def test_share_link_is_aged_by_presence_and_normalized(self):
        # Codex round 2, findings 12 and 13.
        self.configure_mac(hostname="louis-mac")
        share = Path(self.tmp.name)
        data = self.collector.collect()
        probes.write_json_atomic(probes.peer_dir(share) / "louis-mac.json",
                                 {**probes.peer_presence(data, "louis-mac"), "platform": "macos"}, probes.PEER_MAX_BYTES)
        snap = self.collector.collect()
        mac = snap["macPeer"]
        self.assertEqual(mac["state"], "linked")
        self.assertIsNotNone(mac["ageSeconds"])
        self.assertLessEqual(mac["ageSeconds"], 30)
        self.assertTrue(all(set(m) == {"id", "state"} and m["state"] == "loaded" for m in mac["models"]))

    def write_mac_report(self, kind, observed, model):
        import datetime as dt
        share = Path(self.tmp.name)
        fields = {"schemaVersion": 1,
                  "models": [{"id": model, "host": "mac", "loadedState": "loaded"}]}
        if kind == "agiw-peers":
            target = probes.peer_dir(share) / "mac.json"
            fields.update(kind="agiw-peer", host="mac", platform="macos", observedUnix=observed)
        else:
            target = probes.peer_dir(share).parent / "mac-status.json"
            fields.update(host="mac", observedAt=dt.datetime.fromtimestamp(observed, dt.timezone.utc).isoformat())
        probes.write_json_atomic(target, fields, probes.PEER_MAX_BYTES, stop_at=share)

    def configure_mac(self, **fields):
        target = Path(self.tmp.name) / "llm-lab" / "mac-reach" / "hosts.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"mac": fields}), encoding="utf-8")

    def write_mac_presence(self, host, observed, models, **fields):
        share = Path(self.tmp.name)
        target = probes.peer_dir(share) / f"{host}.json"
        probes.write_json_atomic(target, {"schemaVersion": 1, "kind": "agiw-peer", "host": host,
            "platform": "macos", "observedUnix": observed, "models": models, **fields},
            probes.PEER_MAX_BYTES, stop_at=share)

    def clear_mac_reports(self):
        share = Path(self.tmp.name)
        for target in probes.peer_dir(share).glob("*.json"):
            target.unlink()
        (probes.peer_dir(share).parent / "mac-status.json").unlink(missing_ok=True)

    def set_mac_result(self, observed, **fields):
        result = {"state": "unreachable", "observedAt": observed, "name": "ConfiguredMac",
                  "models": [], "loadedCount": None, "detail": "Test LAN refusal", "reason": "refused",
                  "address": None, "latencyMs": None, **fields}
        self.collector.mac_peer = probes.MacPeer(Path(self.tmp.name), probe=lambda _hosts: result)
        self.collector.mac_peer.poll_once()

    def assert_local_readings_preserved(self, snapshot):
        self.assertEqual(snapshot["host"], "windows")
        self.assertEqual({m["id"]: m["state"] for m in snapshot["models"]},
                         {"openai/gpt-oss-20b": "idle", "qwen3.8-27b": "idle"})
        self.assertEqual((snapshot["memory"]["level"], snapshot["share"]["state"]), ("ok", "ready"))
        sources = {s["id"]: s["state"] for s in snapshot["sources"]}
        self.assertEqual((sources["llama-slots"], sources["lane-inventory"]), ("live", "live"))
        for lane in snapshot["windowsWorker"]["lanes"].values():
            self.assertEqual((lane["up"], lane["slotsBusy"], lane["slotsTotal"]), (True, 0, 2))

    def test_share_links_only_the_current_configured_mac(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now  # Set before any local lane poller observes its first answer.
        models = [{"id": "reported-model", "loadedState": "loaded"}]
        cases = [("matching", {"hostname": "target.local"}, "target", "target", True),
                 ("different-reporter", {"hostname": "target.local"}, "other-mac", "target", False),
                 ("missing-config", None, "old-mac", "old-mac", False),
                 ("unnamed-config", {}, "old-mac", "old-mac", False),
                 ("config-changed", {"hostname": "target.local"}, "target", "old-mac", True),
                 ("old-name-cannot-link", {"hostname": "target.local"}, "old-mac", "old-mac", False)]
        for label, config, reporter, old_name, linked in cases:
            with self.subTest(case=label):
                self.clear_mac_reports()
                if config is None:
                    (Path(self.tmp.name) / "llm-lab" / "mac-reach" / "hosts.json").unlink(missing_ok=True)
                else:
                    self.configure_mac(**config)
                self.set_mac_result(now - 46, name=old_name)
                self.write_mac_presence(reporter, now, models, modelsTotal=1)
                snapshot = self.collector.collect()
                self.assert_local_readings_preserved(snapshot)
                mac = snapshot["macPeer"]
                self.assertEqual(mac["state"], "linked" if linked else "unknown")
                if linked:
                    self.assertEqual(mac["name"].split(".")[0].casefold(), "target")
                    self.assertEqual(mac["models"], [{"id": "reported-model", "state": "loaded"}])
                    self.assertFalse(mac["verified"])
                else:
                    self.assertEqual(mac["models"], [])
                    self.assertNotIn("linkSource", mac)
                self.assertEqual([p["host"] for p in snapshot["peers"]], [reporter],
                                 "unattributable reports remain visible as their own AGIW peers")

    def test_configured_mac_matching_uses_case_domain_and_mdns_fallback(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now
        models = [{"id": "reported-model", "loadedState": "loaded"}]
        cases = [({"hostname": "TaRgEt.Example", "mdns": "other.local"}, "TARGET.local", True),
                 ({"hostname": "TaRgEt.Example", "mdns": "other.local"}, "other", False),
                 ({"mdns": "FaLlBaCk.local"}, "FALLBACK.example", True),
                 ({"hostname": None, "mdns": "FaLlBaCk.local"}, "fallback", True)]
        for config, reporter, linked in cases:
            with self.subTest(config=config, reporter=reporter):
                self.clear_mac_reports()
                self.configure_mac(**config)
                self.set_mac_result(now, name=None)
                self.write_mac_presence(reporter, now, models, modelsTotal=1)
                snapshot = self.collector.collect()
                self.assert_local_readings_preserved(snapshot)
                mac = snapshot["macPeer"]
                self.assertEqual(mac["state"], "linked" if linked else "unreachable")
                if linked:
                    self.assertEqual(mac["loadedCount"], 1)
                    self.assertIsInstance(mac["name"], str)
                    self.assertEqual(mac["name"].split(".")[0].casefold(), reporter.split(".")[0].casefold())

    def test_linked_share_loaded_count_needs_complete_known_residency(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now
        self.configure_mac(hostname="mac")
        known = [{"id": "loaded-model", "loadedState": "loaded"},
                 {"id": "inactive-model", "loadedState": "inactive"}]
        cases = [("complete", known, {"modelsTotal": 2}, 1),
                 ("missing-total", known, {}, None),
                 ("longer-inventory", known, {"modelsTotal": 3}, None),
                 ("short-total", known, {"modelsTotal": 1}, None),
                 ("boolean-total", known, {"modelsTotal": True}, None),
                 ("float-total", known, {"modelsTotal": 2.0}, None),
                 ("unknown-residency", [known[0], {"id": "unknown-model", "loadedState": "unknown"}],
                  {"modelsTotal": 2}, None),
                 ("rejected-row", [known[0], {"id": "", "loadedState": "loaded"}], {"modelsTotal": 2}, None),
                 ("duplicate-row", [known[0], known[0]], {"modelsTotal": 2}, None),
                 ("capped-inventory", [{"id": f"model-{i}", "loadedState": "loaded"} for i in range(33)],
                  {"modelsTotal": 33}, None),
                 ("empty-complete", [], {"modelsTotal": 0}, None)]
        for label, models, fields, expected_count in cases:
            with self.subTest(case=label):
                self.clear_mac_reports()
                self.write_mac_presence("mac", now, models, **fields)
                snapshot = self.collector.collect()
                self.assert_local_readings_preserved(snapshot)
                mac = snapshot["macPeer"]
                self.assertEqual(mac["state"], "linked")
                self.assertEqual(mac["loadedCount"], expected_count)
                self.assertFalse(mac["verified"])

    def test_linked_share_retains_only_a_current_lan_failure_on_its_own_clock(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now
        self.configure_mac(hostname="mac")
        self.write_mac_presence("mac", now - 2.4, [{"id": "reported-model", "loadedState": "loaded"}], modelsTotal=1)
        for age, current in ((0.0, True), (45.0, True), (45.04, False), (-5.0, True), (-5.04, False)):
            with self.subTest(lan_age=age):
                self.set_mac_result(now - age, name="mac")
                _, expected_source = self.collector.mac_peer.read(now)
                snapshot = self.collector.collect()
                self.assert_local_readings_preserved(snapshot)
                mac = snapshot["macPeer"]
                self.assertEqual(mac["state"], "linked")
                self.assertEqual((mac["ageSeconds"], mac["linkAgeSeconds"]), (2.4, 2.4))
                self.assertEqual(mac.get("lanFailure"), "refused" if current else None)
                self.assertEqual(mac.get("lanFailureAgeSeconds"), max(0.0, age) if current else None)
                self.assertFalse(mac["verified"])
                self.assertIsNone(mac["address"])
                self.assertIsNone(mac["latencyMs"])
                source = next(s for s in snapshot["sources"] if s["id"] == "mac-peer")
                self.assertEqual(source, expected_source, "the independent LAN source must keep its own result")
                self.assertEqual(probes.pc_status(snapshot)["limitations"],
                                 ["mac LM Studio LAN check failed (refused); cause unknown"] if current else
                                 ["mac LM Studio LAN reachability unknown"])

    def test_missing_stale_or_invalid_share_keeps_the_direct_mac_result(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now
        self.configure_mac(hostname="mac")
        self.set_mac_result(now, name="mac")
        for kind in ("missing", "stale", "invalid"):
            with self.subTest(kind=kind):
                self.clear_mac_reports()
                if kind != "missing":
                    self.write_mac_presence("mac", now - 30.04 if kind == "stale" else 10 ** 1000,
                                            [{"id": "reported-model", "loadedState": "loaded"}], modelsTotal=1)
                snapshot = self.collector.collect()
                self.assert_local_readings_preserved(snapshot)
                mac = snapshot["macPeer"]
                self.assertEqual((mac["state"], mac["name"], mac["reason"], mac["observedAt"]),
                                 ("unreachable", "mac", "refused", now))
                self.assertEqual(mac["models"], [])
                self.assertIsNone(mac["loadedCount"])
                self.assertNotIn("linkSource", mac)

    def test_a_current_direct_mac_answer_takes_priority_over_matching_share(self):
        now = 2_000_000_000.0
        self.collector.clock = lambda: now
        self.configure_mac(hostname="mac")
        self.set_mac_result(now, state="reachable", name="mac", reason=None,
                            models=[{"id": "direct-model", "state": "loaded"}], loadedCount=1,
                            address="192.168.77.2", latencyMs=3, detail="Test direct model listing")
        self.write_mac_presence("mac", now, [{"id": "share-model", "loadedState": "loaded"}], modelsTotal=1)
        snapshot = self.collector.collect()
        self.assert_local_readings_preserved(snapshot)
        mac = snapshot["macPeer"]
        self.assertEqual((mac["state"], mac["loadedCount"], mac["models"]),
                         ("reachable", 1, [{"id": "direct-model", "state": "loaded"}]))
        self.assertEqual((mac["address"], mac["latencyMs"]), ("192.168.77.2", 3))
        self.assertNotIn("linkSource", mac)

    def test_expired_share_reports_do_not_promote_the_mac_to_linked(self):
        observed = 2_000_000_000.0
        self.configure_mac(hostname="mac")
        self.collector.mac_peer = probes.MacPeer(Path(self.tmp.name), probe=lambda _hosts: {
            "state": "unreachable", "observedAt": observed, "models": [], "loadedCount": None,
            "detail": "Test LAN refusal", "reason": "refused"})
        self.collector.mac_peer.poll_once()
        for kind in ("agiw-peers", "mac-status.json"):
            for target in (probes.peer_dir(Path(self.tmp.name)) / "mac.json",
                           probes.peer_dir(Path(self.tmp.name)).parent / "mac-status.json"):
                target.unlink(missing_ok=True)
            self.write_mac_report(kind, observed, "reported-model")
            for age in (30.0, 30.04):
                with self.subTest(kind=kind, age=age):
                    self.collector.clock = lambda: observed + age
                    snap = self.collector.collect()
                    self.assertEqual(snap["peers"][0]["fresh"], age <= 30)
                    source = next(s for s in snap["sources"] if s["id"] == "agiw-peers")
                    self.assertEqual(source["state"], "live" if age <= 30 else "unavailable")
                    mac = snap["macPeer"]
                    if age <= 30:
                        self.assertEqual((mac["state"], mac["linkSource"]), ("linked", kind))
                        self.assertEqual(mac["models"], [{"id": "reported-model", "state": "loaded"}])
                        self.assertFalse(mac["verified"])
                    else:
                        self.assertEqual((mac["state"], mac["reason"], mac["models"]), ("unreachable", "refused", []))
                        self.assertNotIn("linkSource", mac)
                        self.assertEqual(snap["peers"][0]["models"][0]["id"], "reported-model")

    def test_share_link_uses_fresh_legacy_after_presence_expires(self):
        now = 2_000_000_000.0
        self.configure_mac(hostname="mac")
        self.collector.clock = lambda: now
        for presence_age, legacy_age, expected in ((30.04, 10, "mac-status.json"),
                                                  (10, 30.04, "agiw-peers")):
            with self.subTest(presence_age=presence_age, legacy_age=legacy_age):
                self.write_mac_report("agiw-peers", now - presence_age, "presence-model")
                self.write_mac_report("mac-status.json", now - legacy_age, "legacy-model")
                snap = self.collector.collect()
                mac = snap["macPeer"]
                self.assertEqual((mac["state"], mac["linkSource"], mac["linkAgeSeconds"]), ("linked", expected, 10))
                model = "legacy-model" if expected == "mac-status.json" else "presence-model"
                self.assertEqual(mac["models"], [{"id": model, "state": "loaded"}])
                self.assertFalse(mac["verified"])

    def test_invalid_peer_clock_keeps_peers_and_local_readings(self):
        now = 2_000_000_000.0
        self.configure_mac(hostname="mac")
        self.collector.clock = lambda: now
        share = Path(self.tmp.name)
        for kind in ("agiw-peers", "mac-status.json"):
            for target in (probes.peer_dir(share) / "mac.json",
                           probes.peer_dir(share).parent / "mac-status.json"):
                target.unlink(missing_ok=True)
            self.write_mac_report(kind, now - 4, "reported-model")
            for sign in (1, -1):
                with self.subTest(kind=kind, sign=sign):
                    probes.write_json_atomic(probes.peer_dir(share) / "bad-peer.json", {
                        "schemaVersion": 1, "kind": "agiw-peer", "host": "bad-peer", "platform": "macos",
                        "observedUnix": sign * 10**1000, "models": []}, probes.PEER_MAX_BYTES, stop_at=share)
                    snap = self.collector.collect()
                    self.assertEqual(snap["host"], "windows")
                    self.assertEqual({m["id"]: m["state"] for m in snap["models"]},
                                     {"openai/gpt-oss-20b": "idle", "qwen3.8-27b": "idle"})
                    self.assertEqual((snap["memory"]["level"], snap["share"]["state"]), ("ok", "ready"))
                    sources = {s["id"]: s["state"] for s in snap["sources"]}
                    self.assertEqual((sources["llama-slots"], sources["lane-inventory"]), ("live", "live"))
                    self.assertEqual(sources["agiw-peers"], "live")
                    self.assertEqual([(p["host"], p["fresh"]) for p in snap["peers"]], [("mac", True)])
                    mac = snap["macPeer"]
                    self.assertEqual((mac["state"], mac["linkSource"]), ("linked", kind))
                    self.assertEqual(mac["models"], [{"id": "reported-model", "state": "loaded"}])
                    self.assertFalse(mac["verified"])

    def test_observer_log_escapes_and_rate_limits(self):
        import io
        from agiw_win.server import RateLimitedLog
        sink = io.StringIO()
        log = RateLimitedLog(sink)
        for _ in range(5):
            log.write("2026-10-10T10:00:00 sample failed: boom\n")
        log.write("2026-10-10T10:00:01 forged\r2026-01-01T00:00:00 fake line\x1b[31m\n")
        lines = sink.getvalue().splitlines()
        self.assertEqual(sum("boom" in l for l in lines), 1)
        self.assertFalse(any("\x1b" in l for l in lines))
        self.assertEqual(sum("forged" in l for l in lines), 1)
        self.assertFalse(any(l.startswith("2026-01-01T00:00:00") for l in lines))

    def test_observer_log_rotates_during_long_run(self):
        from agiw_win.server import RateLimitedLog
        path = Path(self.tmp.name) / "observer.log"
        log = RateLimitedLog(open(path, "a", encoding="utf-8", buffering=1), path, max_bytes=100)
        try:
            log.write("2026-10-10T10:00:00 first distinct error with enough detail\n")
            log.write("2026-10-10T10:00:01 second distinct error with enough detail\n")
            log.flush()
            self.assertIn("first distinct error", path.with_suffix(".log.1").read_text())
            self.assertIn("second distinct error", path.read_text())
            self.assertLessEqual(path.stat().st_size, 100)
        finally:
            log.stream.close()

    def test_observer_log_keeps_writing_when_rotation_is_blocked(self):
        from unittest import mock
        from agiw_win.server import RateLimitedLog
        path = Path(self.tmp.name) / "observer.log"
        log = RateLimitedLog(open(path, "a", encoding="utf-8", buffering=1), path, max_bytes=100)
        try:
            with mock.patch("agiw_win.server.os.replace", side_effect=PermissionError(13, "locked")):
                for i in range(8):
                    log.write(f"2026-10-10T10:00:0{i} distinct error number {i} with some detail\n")
            log.flush()
            text = path.read_text()
            self.assertIn("distinct error number 0", text)
            self.assertIn("distinct error number 2", text)  # past the cap, not silenced
            self.assertLessEqual(path.stat().st_size, 200)  # but still bounded at twice the cap
        finally:
            log.stream.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing semantics")
    def test_persistent_publish_failure_keeps_the_old_file_and_keeps_sampling(self):
        share = Path(self.tmp.name)
        target = share / "llm-lab" / "status" / probes.STATUS_FILE_NAME
        probes.write_json_atomic(target, {"old": True}, stop_at=share)
        old = target.read_bytes()
        stop = threading.Event()
        worker = threading.Thread(target=sample, args=(self.store, self.collector, stop, 0.05),
                                  kwargs={"publish_to": target, "publish_every": 0.0}, daemon=True)
        holder = open(target, "rb")  # a reader that never shares delete: every replace is refused
        try:
            worker.start()
            deadline, first, failed = time.monotonic() + 15, None, None
            while time.monotonic() < deadline and failed is None:
                data, sequence = self.store.wait_for_change(None, timeout=1.0)
                first = first or sequence
                source = next((s for s in (data or {}).get("sources", []) if s["id"] == "share-publish"), None)
                if source and source["state"] == "unavailable" and "failed" in source["detail"]:
                    failed = (sequence, source)
            self.assertIsNotNone(failed, "the publish failure never reached the dashboard")
            sequence, source = failed
            self.assertGreater(sequence, first)  # sampling kept advancing while publishing failed
            self.assertIn("PermissionError", source["detail"])
            self.assertNotIn(str(share), source["detail"])
        finally:
            stop.set()
            worker.join(10)
            holder.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(target.read_bytes(), old)
        self.assertEqual([p.name for p in target.parent.iterdir()], [target.name])  # no temp file left behind

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing semantics")
    def test_a_blocked_status_file_does_not_stop_presence(self):
        share = Path(self.tmp.name)
        target = share / "llm-lab" / "status" / probes.STATUS_FILE_NAME
        presence = probes.peer_dir(share) / "thispc.json"
        probes.write_json_atomic(target, {"old": True}, stop_at=share)
        stop = threading.Event()
        worker = threading.Thread(target=sample, args=(self.store, self.collector, stop, 0.05),
                                  kwargs={"publish_to": target, "presence_to": presence, "publish_every": 0.0},
                                  daemon=True)
        holder = open(target, "rb")  # never shares delete: the status replace is refused
        try:
            worker.start()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and not presence.exists():
                time.sleep(0.1)
        finally:
            stop.set()
            worker.join(10)
            holder.close()
        self.assertTrue(presence.exists(), "presence was not published while the status file was blocked")
        self.assertEqual(json.loads(target.read_text()), {"old": True})

    def test_publish_source_reports_pending_success_and_failure(self):
        from agiw_win.server import publish_error_name, publish_source
        pending = publish_source({"ok": None, "error": None}, 5.0)
        self.assertEqual((pending["state"], pending["outcome"]), ("unavailable", "pending"))
        live = publish_source({"ok": 100.0, "error": None}, 103.0)
        self.assertEqual((live["state"], live["ageSeconds"], live["outcome"]), ("live", 3.0, "ok"))
        failed = publish_source({"ok": 100.0, "error": publish_error_name(PermissionError(13, "Access is denied", "C:/x"))},
                                130.0)
        self.assertEqual((failed["state"], failed["ageSeconds"], failed["outcome"]), ("unavailable", None, "failed"))
        self.assertIn("last success 30 s ago", failed["detail"])
        self.assertNotIn("C:/x", failed["detail"])
        self.assertIn("no successful publish", publish_source({"ok": None, "error": "OSError"}, 1.0)["detail"])
        # Success names only the files this observer is configured to write.
        done = {"ok": 100.0, "error": None}
        self.assertTrue(publish_source(done, 101.0, ("status",))["detail"].startswith("Status published"))
        self.assertTrue(publish_source(done, 101.0, ("presence",))["detail"].startswith("Presence published"))
        self.assertTrue(publish_source(done, 101.0, ("status", "presence"))["detail"].startswith("Status and presence published"))

    def test_post_is_read_only(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        origin = f"http://127.0.0.1:{self.port}"
        conn.request("POST", "/api/models/control", body=b"{}", headers={"Host": f"127.0.0.1:{self.port}",
                                                                          "Origin": origin, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 501)


class PostBodyRequests(unittest.TestCase):
    """Rejected POSTs still return readable responses for segmented request bodies."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = MonitorServer(("127.0.0.1", 0), SnapshotStore(), Path(self.tmp.name))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.tmp.cleanup()
        self.assertFalse(self.thread.is_alive())

    def headers(self, content_lengths=("2",), origin=None):
        host = f"127.0.0.1:{self.port}"
        origin = f"http://{host}" if origin is None else origin
        return (
            "POST /api/models/control HTTP/1.1\r\n"
            "Accept-Encoding: identity\r\n"
            + "".join(f"Content-Length: {length}\r\n" for length in content_lengths)
            + f"Host: {host}\r\nOrigin: {origin}\r\n"
            + "Content-Type: application/json\r\n\r\n"
        ).encode("ascii")

    def assert_response(self, content_lengths=("2",), body=b"{}", origin=None, status=501):
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(self.headers(content_lengths, origin))
            if body:
                time.sleep(0.005)
                sock.sendall(body)
            with http.client.HTTPResponse(sock, method="POST") as response:
                response.begin()
                payload = response.read()
                self.assertEqual(response.status, status)
                self.assertEqual(response.getheader("Content-Type"), "application/json")
                self.assertEqual(response.getheader("Content-Length"), str(len(payload)))
                self.assertEqual(json.loads(payload), {
                    "status": "error",
                    "message": ("The Windows edition is read-only in this version." if status == 501 else
                                "Exact same-origin Origin and Host are required.")})

    def test_delayed_body_returns_readable_501(self):
        self.assert_response()

    def test_delayed_bad_origin_body_returns_readable_403(self):
        self.assert_response(origin="http://foreign.invalid", status=403)

    def test_zero_and_maximum_body_lengths_return_readable_501(self):
        for length in (0, 65536):
            with self.subTest(length=length):
                self.assert_response(content_lengths=(str(length),), body=b"x" * length)

    def test_invalid_body_lengths_reply_without_waiting_for_body(self):
        cases = [("missing", ()), ("negative", ("-1",)), ("nondecimal", ("bad",)),
                 ("fraction", ("2.0",)), ("oversized", ("65537",)), ("duplicate", ("2", "3")),
                 ("excessive-decimal", ("9" * 5000,))]
        for label, lengths in cases:
            with self.subTest(case=label):
                started = time.monotonic()
                self.assert_response(content_lengths=lengths, body=b"")
                self.assertLess(time.monotonic() - started, 1)

    def test_stalled_valid_body_is_closed_by_existing_deadline(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=4.5) as sock:
            started = time.monotonic()
            sock.sendall(self.headers())
            try:
                first = sock.recv(1)
            except ConnectionResetError:
                first = b""
            elapsed = time.monotonic() - started
            self.assertEqual(first, b"")
            self.assertGreater(elapsed, 2)
            self.assertLess(elapsed, 4.5)


class UnsupportedBodyRequests(unittest.TestCase):
    """The three refused write verbs keep complete stdlib replies for split sends."""

    setUp = PostBodyRequests.setUp
    tearDown = PostBodyRequests.tearDown
    headers = PostBodyRequests.headers

    def request(self, verb, content_lengths=("2",), body=b"{}", mode="delayed", path="/api/models/control"):
        host = f"127.0.0.1:{self.port}"
        headers = (
            f"{verb} {path} HTTP/1.1\r\nAccept-Encoding: identity\r\n"
            + "".join(f"Content-Length: {length}\r\n" for length in content_lengths)
            + f"Host: {host}\r\nOrigin: http://{host}\r\nContent-Type: application/json\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            if mode == "combined":
                sock.sendall(headers + body)
            else:
                sock.sendall(headers)
                if body:
                    time.sleep(0.005)
                    sock.sendall(body)
            with http.client.HTTPResponse(sock, method=verb) as response:
                response.begin()
                return response.status, response.reason, dict(response.getheaders()), response.read()

    def assert_html_refusal(self, result, verb):
        status, reason, headers, payload = result
        message = f"Unsupported method ({verb!r})"
        self.assertEqual((status, reason), (501, message))
        self.assertEqual(headers["Content-Type"], "text/html;charset=utf-8")
        self.assertEqual(headers["Content-Length"], str(len(payload)))
        self.assertEqual(headers["Connection"], "close")
        self.assertIn(message, payload.decode("utf-8"))
        self.assertTrue(payload.startswith(b"<!DOCTYPE HTML>\n"))
        self.assertTrue(payload.endswith(b"</html>\n"))

    def test_delayed_bodies_return_full_501_html(self):
        for verb in ("PUT", "DELETE", "PATCH"):
            with self.subTest(verb=verb):
                baseline = self.request(verb, content_lengths=("0",), body=b"")
                self.assert_html_refusal(baseline, verb)
                delayed = self.request(verb)
                self.assert_html_refusal(delayed, verb)
                self.assertEqual(delayed[3], baseline[3])

    def test_combined_and_empty_bodies_keep_stdlib_refusal(self):
        for verb in ("PUT", "DELETE", "PATCH"):
            with self.subTest(verb=verb):
                empty = self.request(verb, content_lengths=("0",), body=b"")
                combined = self.request(verb, mode="combined")
                self.assert_html_refusal(empty, verb)
                self.assert_html_refusal(combined, verb)
                self.assertEqual(combined[3], empty[3])

    def test_maximum_bodies_return_full_501_html(self):
        for verb in ("PUT", "DELETE", "PATCH"):
            with self.subTest(verb=verb):
                self.assert_html_refusal(self.request(verb, content_lengths=("65536",), body=b"x" * 65536), verb)

    def test_invalid_lengths_reply_without_waiting_for_body(self):
        cases = [(), ("-1",), ("bad",), ("2.0",), ("65537",), ("2", "3"), ("9" * 5000,)]
        for verb in ("PUT", "DELETE", "PATCH"):
            for index, lengths in enumerate(cases):
                with self.subTest(verb=verb, case=index):
                    started = time.monotonic()
                    self.assert_html_refusal(self.request(verb, content_lengths=lengths, body=b""), verb)
                    self.assertLess(time.monotonic() - started, 1)

    def test_stalled_body_keeps_existing_deadline(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=4.5) as sock:
            started = time.monotonic()
            sock.sendall(self.headers().replace(b"POST ", b"PUT ", 1))
            try:
                first = sock.recv(1)
            except ConnectionResetError:
                first = b""
            elapsed = time.monotonic() - started
            self.assertEqual(first, b"")
            self.assertGreater(elapsed, 2)
            self.assertLess(elapsed, 4.5)

    def test_get_and_post_compatibility(self):
        status, _, headers, payload = self.request("GET", content_lengths=("0",), body=b"", path="/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(set(json.loads(payload)), {"service", "version", "pid"})
        PostBodyRequests.assert_response(self)

    def test_other_unsupported_methods_do_not_wait_for_body(self):
        for verb in ("HEAD", "OPTIONS", "UNKNOWN"):
            with self.subTest(verb=verb):
                result = self.request(verb, body=b"")
                if verb == "HEAD":
                    self.assertEqual((result[0], result[1], result[3]), (501, "Unsupported method ('HEAD')", b""))
                    self.assertGreater(int(result[2]["Content-Length"]), 0)
                else:
                    self.assert_html_refusal(result, verb)

    def test_parser_error_delegates_without_body_read(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=1) as sock:
            sock.sendall(b"PUT /api/models/control HTTP/2.0\r\n")
            with http.client.HTTPResponse(sock, method="PUT") as response:
                response.begin()
                payload = response.read()
                self.assertEqual(response.status, 505)
                self.assertEqual(response.getheader("Content-Length"), str(len(payload)))
                self.assertIn(b"Invalid HTTP version (2.0)", payload)


class ObserverRecovery(unittest.TestCase):
    """The real observer process, run from a copy so its source can change: the tray relies on these exits."""

    def setUp(self):
        import shutil
        import socket
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.install = root / "install"
        windows = Path(__file__).resolve().parents[1]
        shutil.copytree(windows / "agiw_win", self.install / "agiw_win", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(windows / "agiw_observer.py", self.install / "agiw_observer.py")
        (self.install / "web").mkdir()
        (self.install / "web" / "index.html").write_text("<!doctype html>")
        (root / "config.json").write_text(json.dumps({"publishStatus": False, "peerLink": False}))
        (root / "share").mkdir()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.root = root

    def tearDown(self):
        for process in getattr(self, "processes", []):
            if process.poll() is None:
                process.kill()
                process.wait(5)
            if process.stdout:
                process.stdout.close()
        self.tmp.cleanup()

    def start(self, parent_pid: int):
        import subprocess
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", str(self.install / "agiw_observer.py"), "--port", str(self.port),
             "--parent-pid", str(parent_pid), "--no-lan", "--config", str(self.root / "config.json"),
             "--share-root", str(self.root / "share"), "--log-file", str(self.root / "observer.log")],
            stdout=subprocess.PIPE, text=True)
        self.processes = [*getattr(self, "processes", []), process]
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready["port"], self.port)
        return process

    def test_source_change_exits_for_reload(self):
        process = self.start(os.getpid())
        time.sleep(2.5)  # past the first fingerprint
        source = self.install / "agiw_win" / "probes.py"
        source.write_text(source.read_text() + "\n# changed\n")
        self.assertEqual(process.wait(15), 3)  # exit code 3 tells the tray this is an update
        self.assertIn("source changed; exiting for reload", (self.root / "observer.log").read_text())

    def test_parent_exit_stops_the_observer(self):
        import subprocess
        parent = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"])
        self.processes = [parent]
        process = self.start(parent.pid)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/api/health", headers={"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        parent.kill()
        parent.wait(5)
        self.assertEqual(process.wait(15), 0)  # an orphaned observer must not keep the port


if __name__ == "__main__":
    unittest.main()
