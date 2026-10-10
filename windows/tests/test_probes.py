"""Probe behaviour with fakes; runs on any OS."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_win import probes  # noqa: E402

FAST = {"id": "fast", "port": 1235, "alias": "openai/gpt-oss-20b", "device": "CUDA0", "parallel": 2, "context": 32768}


def fake_fetch(routes):
    def fetch(url, _timeout, _limit):
        for suffix, value in routes.items():
            if url.endswith(suffix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ConnectionRefusedError(url)
    return fetch


def slots(*states):
    return [{"id": i, "is_processing": busy, "n_ctx": 16384, "next_token": [{"n_decoded": decoded}]}
            for i, (busy, decoded) in enumerate(states)]


class LaneProbe(unittest.TestCase):
    def test_idle_lane(self):
        lane = probes.probe_lane(FAST, fake_fetch({"/health": {"status": "ok"},
                                                   "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                   "/slots": slots((False, 0), (False, 0))}))
        self.assertEqual((lane["status"], lane["phase"], lane["slotsBusy"], lane["slotsTotal"]), ("idle", "idle", 0, 2))

    def test_generating_vs_prompt_phase(self):
        gen = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                  "/slots": slots((True, 12), (False, 0))}))
        self.assertEqual((gen["status"], gen["phase"], gen["slotsBusy"]), ("busy", "generating", 1))
        prompt = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                     "/slots": slots((True, 0), (False, 0))}))
        self.assertEqual(prompt["phase"], "busy")

    def test_identity_mismatch_never_claims_activity(self):
        lane = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "qwen3.8-27b"}]},
                                                   "/slots": slots((True, 5))}))
        self.assertEqual(lane["status"], "identity_mismatch")
        self.assertIsNone(lane["slotsBusy"])
        row = probes.lane_model_row(lane, FAST, time.time())
        self.assertIsNone(row["loaded"])
        self.assertNotIn(row["state"], ("busy", "generating", "idle"))

    def test_unreachable_and_loading(self):
        self.assertEqual(probes.probe_lane(FAST, fake_fetch({}))["status"], "unreachable")
        loading = probes.probe_lane(FAST, fake_fetch({"/health": HTTPError("u", 503, "Loading", {}, None)}))
        self.assertEqual(loading["status"], "loading")

    def test_malformed_slots_are_unknown_not_idle(self):
        for payload in ([], [{"id": 0}], "text", [{"is_processing": "yes"}]):
            lane = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                       "/slots": payload}))
            self.assertEqual(lane["status"], "slots_unknown", payload)
            row = probes.lane_model_row(lane, FAST, 0)
            self.assertEqual((row["state"], row["source"], row["loaded"]), ("loaded", "llama-models", True))

    def test_row_shape(self):
        lane = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                   "/slots": slots((True, 3), (True, 0))}))
        row = probes.lane_model_row(lane, FAST, 0)
        self.assertEqual(row["host"], "windows")
        self.assertEqual(row["source"], "llama-slots")
        self.assertEqual(row["state"], "generating")
        self.assertEqual(row["parallel"], 2)
        self.assertEqual(row["context"], 32768)


class SlowSlots(unittest.TestCase):
    def test_slots_get_a_longer_timeout_than_health(self):
        seen = {}
        def fetch(url, timeout, _limit):
            seen[url.rsplit("/", 1)[-1]] = timeout
            return {"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]}}.get(
                url[url.index("/", 8):], slots((True, 2)))
        self.assertEqual(probes.probe_lane(FAST, fetch)["status"], "busy")
        self.assertGreaterEqual(seen["slots"], 3.0)
        self.assertLess(seen["health"], 1.0)


class LiveRate(unittest.TestCase):
    def lane(self, *slots):
        return {"slots": [{"id": i, "isProcessing": busy, "decodedTokens": dec} for i, (busy, dec) in enumerate(slots)]}

    def test_rate_from_two_polls_of_one_request(self):
        rate, prev = probes.live_decode_rate({}, self.lane((True, 10), (False, None)), 100.0)
        self.assertIsNone(rate)
        rate, prev = probes.live_decode_rate(prev, self.lane((True, 110), (False, None)), 101.0)
        self.assertEqual(rate, 100.0)

    def test_new_request_or_long_gap_is_not_a_rate(self):
        _, prev = probes.live_decode_rate({}, self.lane((True, 500)), 100.0)
        self.assertIsNone(probes.live_decode_rate(prev, self.lane((True, 5)), 101.0)[0])
        _, prev = probes.live_decode_rate({}, self.lane((True, 5)), 100.0)
        self.assertIsNone(probes.live_decode_rate(prev, self.lane((True, 900)), 130.0)[0])

    def test_two_busy_slots_sum(self):
        _, prev = probes.live_decode_rate({}, self.lane((True, 0), (True, 0)), 10.0)
        self.assertEqual(probes.live_decode_rate(prev, self.lane((True, 20), (True, 30)), 11.0)[0], 50.0)


class Codemode(unittest.TestCase):
    def test_runs_become_lane_jobs_with_whole_request_rates(self):
        specs = [{"id": "fast", "port": 1235, "alias": "g"}, {"id": "deep", "port": 1234, "alias": "q"}]
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "20260924-053702-fd15a3"
            run.mkdir()
            (run / "manifest.json").write_text(json.dumps({"run_id": run.name, "verification": "CERTIFIED", "metrics": [
                {"stage": "draft", "kind": "local", "served": "q", "endpoint": "http://127.0.0.1:1234", "tok": 334, "sec": 32.2, "finish": "stop"},
                {"stage": "verify", "kind": "local", "served": "g", "endpoint": "http://127.0.0.1:1235", "tok": 499, "sec": 6.4, "finish": "length"},
                {"stage": "verify", "kind": "cloud", "served": "x", "endpoint": "https://e", "tok": 1, "sec": 1}]}))
            jobs, source = probes.codemode_jobs(Path(tmp), specs, time.time())
        rows = {r["stage"]: r for r in jobs["recent"]}
        self.assertEqual(set(rows), {"draft", "verify"})
        self.assertEqual((rows["draft"]["lane"], rows["draft"]["state"]), ("deep", "success"))
        self.assertEqual(rows["verify"]["flags"], ["hit-token-limit"])
        self.assertIsNone(rows["draft"]["predictedPerSecond"])
        self.assertEqual(rows["draft"]["approxPerSecond"], 10.4)
        self.assertEqual(jobs["lastSuccess"]["model"], "q")
        self.assertIn("not decode", source["detail"])

    def test_missing_folder(self):
        jobs, source = probes.codemode_jobs(Path("/nonexistent"), [], 0)
        self.assertEqual((jobs["recent"], source["state"]), ([], "unavailable"))


class LaneConfig(unittest.TestCase):
    def test_reads_declared_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows-llm-pipeline"
            path.mkdir()
            (path / "config.json").write_text(json.dumps({"runtime": {"lane_layout": {"name": "L"}, "lanes": {
                "fast": {"port": 1235, "alias": "a", "device": "CUDA0"},
                "deep": {"port": 1234, "alias": "b", "device": "none"}}}}), encoding="utf-8-sig")
            specs, source = probes.lane_specs(Path(tmp))
        self.assertEqual([(s["id"], s["port"], s["alias"]) for s in specs], [("fast", 1235, "a"), ("deep", 1234, "b")])
        self.assertEqual(source["state"], "live")
        self.assertIn("layout L", source["detail"])

    def test_falls_back(self):
        specs, source = probes.lane_specs(Path("/nonexistent"))
        self.assertEqual(len(specs), 2)
        self.assertEqual(source["state"], "unavailable")


class Memory(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(probes.memory_level(50, 40), "ok")
        self.assertEqual(probes.memory_level(15, 40), "watch")
        self.assertEqual(probes.memory_level(8, 40), "tight")
        self.assertEqual(probes.memory_level(3, 40), "critical")
        self.assertEqual(probes.memory_level(50, 97), "tight")

    def test_block(self):
        gib = 2 ** 30
        block, source = probes.memory_block(lambda: {"totalPhys": 64 * gib, "availPhys": 4 * gib,
                                                     "commitLimit": 80 * gib, "commitAvail": 20 * gib},
                                            lambda: [{"label": "llama-server.exe", "residentBytes": 30 * gib,
                                                      "processCount": 2, "gpuAllocBytes": None}])
        self.assertEqual(block["level"], "tight")
        self.assertEqual(block["availablePercent"], 6.2)
        self.assertEqual(block["consumers"][0]["label"], "llama-server.exe")
        self.assertEqual(source["state"], "live")

    def test_unreadable(self):
        def broken():
            raise OSError("no")
        block, source = probes.memory_block(broken)
        self.assertEqual(block["level"], "unknown")
        self.assertEqual(source["state"], "unavailable")


class Gpu(unittest.TestCase):
    def test_parse(self):
        rows = probes.parse_nvidia_csv("0, NVIDIA GeForce RTX 3080 Ti, 37, 11000, 12288, 61, 220.5\n")
        self.assertEqual(rows[0]["name"], "NVIDIA GeForce RTX 3080 Ti")
        self.assertEqual(rows[0]["utilizationPercent"], 37.0)

    def test_unreported_fields_are_none_not_zero(self):
        row = probes.parse_nvidia_csv("0, X, [N/A], 1, 2, 30, [N/A]")[0]
        self.assertIsNone(row["powerW"])
        self.assertIsNone(row["utilizationPercent"])

    def test_bad_reading_voids(self):
        for text in ("0, X, 140, 1, 2, 30, 1", "0, X, 1, 5, 2, 30, 1", "0, X, 1"):
            with self.assertRaises(ValueError):
                probes.parse_nvidia_csv(text)

    def test_runner_failure_is_a_source_not_a_crash(self):
        def runner(*_):
            raise RuntimeError("exit 9")
        gpus, source = probes.nvidia_gpus(runner)
        self.assertEqual((gpus, source["state"]), ([], "error"))


class Queue(unittest.TestCase):
    def test_counts_and_stale_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for state, names in (("pending", ["a", "b"]), ("claimed", ["c", "d"]), ("completed", ["e"])):
                (root / state).mkdir()
                for name in names:
                    (root / state / f"{name}.json").write_text("{}")
            old = time.time() - 1800
            os.utime(root / "claimed" / "c.json", (old, old))
            pipeline, source = probes.route_queue(root)
        self.assertEqual(pipeline["queue"]["staleClaimed"], 1)
        self.assertEqual(pipeline["status"], "running")
        self.assertIn("older than 15 min", source["detail"])

    def test_day_old_pending_is_stale_not_queued(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending").mkdir()
            (root / "pending" / "p.json").write_text("{}")
            old = time.time() - 16 * 86400
            os.utime(root / "pending" / "p.json", (old, old))
            pipeline, source = probes.route_queue(root)
        self.assertEqual(pipeline["status"], "idle")
        self.assertIn("stale, not queued", source["detail"])

    def test_missing_queue_is_unavailable_not_idle(self):
        pipeline, source = probes.route_queue(Path("/nonexistent/queue"))
        self.assertIsNone(pipeline["status"])
        self.assertEqual(source["state"], "unavailable")

    def test_only_stale_claims_reads_queued_or_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "claimed").mkdir()
            (root / "claimed" / "x.json").write_text("{}")
            old = time.time() - 9000
            os.utime(root / "claimed" / "x.json", (old, old))
            pipeline, _ = probes.route_queue(root)
        self.assertEqual(pipeline["status"], "idle")


class MacPeer(unittest.TestCase):
    HOSTS = {"mdns": "mac.local", "hostname": "mac", "ips": ["10.0.0.176", "10.0.0.194"], "port": 1234,
             "expectedVerify": "openai/gpt-oss-20b", "ownIp": "10.0.0.71"}

    def test_v0_listing_reports_loaded(self):
        fetch = fake_fetch({"10.0.0.176:1234/api/v0/models": {"data": [
            {"id": "openai/gpt-oss-20b", "type": "llm", "state": "loaded"},
            {"id": "qwen/qwen3.8-27b", "type": "llm", "state": "not-loaded"},
            {"id": "text-embedding-nomic", "type": "embeddings", "state": "loaded"}]}})
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: ["10.0.0.176"], own=set())
        self.assertEqual(result["state"], "reachable")
        self.assertEqual(result["via"], "mdns")
        self.assertEqual(result["loadedCount"], 1)
        self.assertTrue(result["expectedVerifyLoaded"])

    def test_falls_back_to_recorded_ip_and_v1(self):
        fetch = fake_fetch({"10.0.0.194:1234/v1/models": {"data": [{"id": "m"}]}})
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: (_ for _ in ()).throw(OSError("no mdns")), own=set())
        self.assertEqual((result["state"], result["address"], result["via"]), ("reachable", "10.0.0.194", "recorded-ip"))
        self.assertIsNone(result["loadedCount"])

    def test_never_probes_itself_or_loopback(self):
        seen = []
        def fetch(url, *_):
            seen.append(url)
            raise ConnectionRefusedError()
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: ["10.0.0.71", "127.0.0.1", "10.0.0.50"],
                                  own={"10.0.0.50"})
        self.assertEqual(result["state"], "unreachable")
        self.assertFalse(any("10.0.0.71" in u or "127.0.0.1" in u or "10.0.0.50" in u for u in seen))

    def test_results_do_not_depend_on_the_test_machine(self):
        # On the Mac, 10.0.0.176/.194 are its own addresses; an explicit own set keeps the test hermetic.
        fetch = fake_fetch({"10.0.0.176:1234/v1/models": {"data": [{"id": "m"}]}})
        original = probes._own_addresses
        probes._own_addresses = lambda: {"10.0.0.176", "10.0.0.194"}
        try:
            self.assertEqual(probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: [], own=set())["state"], "reachable")
            self.assertEqual(probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: [])["state"], "unreachable")
        finally:
            probes._own_addresses = original

    def test_peer_ages_result(self):
        peer = probes.MacPeer(Path("/x"), probe=lambda _h: {"state": "reachable", "observedAt": 100.0, "detail": "ok", "models": []})
        peer.poll_once()
        data, source = peer.read(now=112.0)
        self.assertEqual(data["ageSeconds"], 12.0)
        self.assertEqual(source["ageSeconds"], 12.0)

    def test_hosts_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm-lab" / "mac-reach"
            path.mkdir(parents=True)
            (path / "hosts.json").write_text(json.dumps({"mac": {"mdns": "m.local", "ips": ["1.2.3.4"], "lm_studio_port": 1234},
                                                          "windows": {"ip": "10.0.0.71"}}))
            hosts = probes.mac_hosts(Path(tmp))
        self.assertEqual((hosts["mdns"], hosts["ips"], hosts["ownIp"]), ("m.local", ["1.2.3.4"], "10.0.0.71"))


class Nisi(unittest.TestCase):
    def lanes(self, fast, deep):
        return [{"id": "fast", "status": fast, "expectedModel": "gpt"}, {"id": "deep", "status": deep, "expectedModel": "qwen"}]

    def test_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(probes.nisi_component(self.lanes("idle", "idle"), root)["state"], "unknown")
            (root / "nisi-gate-v0.2").mkdir()
            (root / "nisi-gate-v0.2" / "nisi_gate.mjs").write_text("")
            ready = probes.nisi_component(self.lanes("idle", "busy"), root)
            self.assertEqual((ready["state"], ready["authorModel"], ready["reviewerModel"]), ("ready", "qwen", "gpt"))
            self.assertEqual(probes.nisi_component(self.lanes("idle", "unreachable"), root)["state"], "partial")
            self.assertEqual(probes.nisi_component(self.lanes("identity_mismatch", "idle"), root)["state"], "needs-action")


class Share(unittest.TestCase):
    def test_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(probes.share_health(root)[0]["state"], "missing")
            (root / "llm-lab").mkdir()
            self.assertEqual(probes.share_health(root)[0]["state"], "degraded")
            (root / "llm-lab" / "config.json").write_text("{}")
            self.assertEqual(probes.share_health(root)[0]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
