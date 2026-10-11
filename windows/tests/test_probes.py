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
        down = probes.probe_lane(FAST, fake_fetch({}))
        self.assertEqual(down["status"], "unreachable")
        # Codex finding 7: no answer is unknown residency, never "unloaded".
        row = probes.lane_model_row(down, FAST, 0)
        self.assertEqual((row["state"], row["loaded"]), ("unknown", None))
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


class Round2(unittest.TestCase):
    """Codex review round 2 (2026-10-10, PC-run): one test per fixed finding."""

    def test_http_query_and_validated_host_header(self):
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        request = []
        def answer():
            conn, _ = server.accept()
            try:
                request.append(conn.recv(4096).decode("ascii"))
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
            finally:
                conn.close()
        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        try:
            self.assertEqual(probes.http_get_json(f"http://127.0.0.1:{port}/status?key=value"), {})
            with self.assertRaises(ValueError):
                probes.http_get_json(f"http://user@127.0.0.1:{port}/status")
        finally:
            server.close()
            thread.join(2)
        self.assertIn("GET /status?key=value HTTP/1.1", request[0])
        self.assertIn(f"Host: 127.0.0.1:{port}", request[0])

    def test_deadline_cuts_a_trickling_server(self):
        import socket as s_
        import threading as t_
        server = s_.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        stop = t_.Event()
        def trickle():
            conn, _ = server.accept()
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
            while not stop.is_set():
                try:
                    conn.sendall(b"x")
                except OSError:
                    break
                time.sleep(0.15)
            conn.close()
        t_.Thread(target=trickle, daemon=True).start()
        started = time.monotonic()
        try:
            with self.assertRaises(TimeoutError):
                probes.http_get_json(f"http://127.0.0.1:{port}/slots", timeout=0.4)
        finally:
            stop.set()
            server.close()
        self.assertLess(time.monotonic() - started, 2.0)

    def test_nan_and_deep_nesting_are_malformed(self):
        with self.assertRaises(ValueError):
            probes.loads_strict('{"observedUnix": NaN}')
        with self.assertRaises(ValueError):
            probes.loads_strict("[" * 100000 + "]" * 100000)
        self.assertIsNone(probes._validate_peer({"schemaVersion": 1, "kind": "agiw-peer", "host": "m",
                                                 "observedUnix": float("nan")}, 1000.0))

    def test_duplicate_or_missing_slot_ids_are_not_evidence(self):
        for payload in ([{"id": 0, "is_processing": False}, {"id": 0, "is_processing": False}],
                        [{"is_processing": False}, {"id": 1, "is_processing": False}],
                        [{"id": 0.5, "is_processing": False}]):
            lane = probes.probe_lane(FAST, fake_fetch({"/health": {}, "/v1/models": {"data": [{"id": "openai/gpt-oss-20b"}]},
                                                       "/slots": payload}))
            self.assertEqual(lane["status"], "slots_unknown", payload)


class Hardening(unittest.TestCase):
    """Codex security review (2026-10-10, PC-run): share inputs are untrusted."""

    def peer(self, host, observed=995.0):
        return {"schemaVersion": 1, "kind": "agiw-peer", "host": host, "platform": "macos", "observedUnix": observed,
                "models": [{"id": "m", "loadedState": "loaded"}]}

    def test_file_name_must_match_host_and_hosts_count_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = probes.peer_dir(Path(tmp))
            directory.mkdir(parents=True)
            (directory / "evil.json").write_text(json.dumps(self.peer("louis-mac")))       # name != host
            (directory / "louis-mac.json").write_text(json.dumps(self.peer("louis-mac")))
            (directory / "not a host!.json").write_text(json.dumps(self.peer("x")))
            peers, source = probes.read_peers(Path(tmp), "pc", 1000.0)
        self.assertEqual([p["host"] for p in peers], ["louis-mac"])
        self.assertFalse(peers[0]["verified"])
        self.assertIn("not verified", source["detail"])

    def test_flooded_peer_folder_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = probes.peer_dir(Path(tmp))
            directory.mkdir(parents=True)
            for i in range(probes.PEER_SCAN_LIMIT + 50):
                (directory / f"junk{i}.txt").write_text("x")
            _, source = probes.read_peers(Path(tmp), "pc", 1000.0)
        self.assertIn("scan stopped", source["detail"])

    def test_publishing_refuses_a_linked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "elsewhere"
            real.mkdir()
            (root / "llm-lab").mkdir()
            os.symlink(real, root / "llm-lab" / "status", target_is_directory=True)
            with self.assertRaises(ValueError):
                probes.write_json_atomic(root / "llm-lab" / "status" / "x.json", {"a": 1}, stop_at=root)
            self.assertEqual(list(real.iterdir()), [])
            probes.write_json_atomic(root / "llm-lab" / "ok" / "x.json", {"a": 1}, stop_at=root)
            self.assertEqual(json.loads((root / "llm-lab" / "ok" / "x.json").read_text()), {"a": 1})
            self.assertEqual([p.name for p in (root / "llm-lab" / "ok").iterdir()], ["x.json"])  # no temp left

    def test_replace_retries_a_transiently_locked_target(self):
        if os.name == "nt":
            import ctypes
            import threading
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                target = root / "x.json"
                target.write_text("old")
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create = kernel32.CreateFileW
                create.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                   ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
                create.restype = ctypes.c_void_p
                held = create(str(target), 0x80, 0x1, None, 3, 0, None)
                self.assertNotEqual(held, ctypes.c_void_p(-1).value)
                def release():
                    time.sleep(0.15)
                    kernel32.CloseHandle(ctypes.c_void_p(held))
                thread = threading.Thread(target=release)
                thread.start()
                try:
                    probes.write_json_atomic(target, {"ok": 1}, stop_at=root)
                finally:
                    thread.join(2)
                self.assertEqual(json.loads(target.read_text()), {"ok": 1})
            return
        import unittest.mock as m
        real = os.replace
        calls = []
        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError(13, "Access is denied")
            return real(src, dst)
        with tempfile.TemporaryDirectory() as tmp, m.patch.object(probes.os, "replace", flaky):
            probes.write_json_atomic(Path(tmp) / "a" / "x.json", {"ok": 1}, stop_at=Path(tmp))
            self.assertEqual(json.loads((Path(tmp) / "a" / "x.json").read_text()), {"ok": 1})
        self.assertEqual(len(calls), 3)

    @unittest.skipUnless(sys.platform == "win32", "Windows file sharing semantics")
    def test_publish_replaces_a_file_a_share_reader_holds_open(self):
        # Live 2026-10-10: the SMB server held windows-status.json open for the Mac with delete sharing,
        # and classic rename failed with access denied on every publish.
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                         ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "llm-lab" / "status" / "x.json"
            probes.write_json_atomic(target, {"n": 0}, stop_at=root)
            reader = kernel32.CreateFileW(str(target), 0x80000000, 0x7, None, 3, 0x80, None)  # read, share all
            self.assertNotEqual(reader, ctypes.c_void_p(-1).value)
            try:
                probes.write_json_atomic(target, {"n": 1}, stop_at=root)
            finally:
                kernel32.CloseHandle(reader)
            self.assertEqual(json.loads(target.read_text()), {"n": 1})
            self.assertEqual([p.name for p in target.parent.iterdir()], ["x.json"])

    def test_publisher_holds_parent_against_directory_swap(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "publish"
            directory.mkdir()
            outside = root / "outside"
            outside.mkdir()
            original_check = probes.is_link_or_junction
            attempted = []
            def swap_during_check(path):
                result = original_check(path)
                if path == directory and not attempted:
                    try:
                        directory.rename(root / "old-publish")
                        os.symlink(outside, directory, target_is_directory=True)
                        attempted.append(True)
                    except OSError:
                        attempted.append(False)  # the open directory handle blocks replacement
                return result
            with mock.patch.object(probes, "is_link_or_junction", side_effect=swap_during_check):
                try:
                    probes.write_json_atomic(directory / "x.json", {"ok": 1}, stop_at=root)
                except (OSError, ValueError):
                    pass
            self.assertEqual(attempted, [True])
            self.assertEqual(list(outside.iterdir()), [])

    def test_path_shaped_model_ids_are_not_published(self):
        self.assertEqual(probes.public_id(r"%USERPROFILE%\.lmstudio\models\Qwen.gguf"), "Qwen.gguf")
        self.assertEqual(probes.public_id("/home/x/m.gguf"), "m.gguf")
        self.assertEqual(probes.public_id("models/foo.gguf"), "foo.gguf")
        self.assertEqual(probes.public_id("openai/gpt-oss-20b"), "openai/gpt-oss-20b")

    def test_peer_removed_during_stat_does_not_hide_other_peers(self):
        import contextlib
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = probes.peer_dir(root)
            directory.mkdir(parents=True)
            (directory / "mac.json").write_text(json.dumps(self.peer("mac")))
            with os.scandir(directory) as scan:
                live = next(iter(scan))
            class Vanished:
                name = "gone.json"
                path = str(directory / name)
                def is_file(self, follow_symlinks=False):
                    return True
                def stat(self, follow_symlinks=False):
                    raise FileNotFoundError()
            with mock.patch.object(probes.os, "scandir", return_value=contextlib.nullcontext(iter([Vanished(), live]))):
                peers, _ = probes.read_peers(root, "pc", 1000.0)
        self.assertEqual([peer["host"] for peer in peers], ["mac"])

    def test_legacy_mac_replaces_stale_duplicate(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = probes.peer_dir(root)
            directory.mkdir(parents=True)
            (directory / "mac.json").write_text(json.dumps(self.peer("mac", now - 3600)))
            status = directory.parent / "mac-status.json"
            observed = dt.datetime.fromtimestamp(now - 5, dt.timezone.utc).isoformat().replace("+00:00", "Z")
            status.write_text(json.dumps({"schemaVersion": 1, "host": "mac", "observedAt": observed, "models": []}))
            peers, _ = probes.read_peers(root, "pc", now)
        self.assertEqual([(peer["host"], peer["source"]) for peer in peers], [("mac", "mac-status.json")])

    def test_legacy_mac_keeps_a_newer_stale_peer_and_survives_bad_models(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = probes.peer_dir(root)
            directory.mkdir(parents=True)
            (directory / "mac.json").write_text(json.dumps(self.peer("mac", now - 60)))
            status = directory.parent / "mac-status.json"
            observed = dt.datetime.fromtimestamp(now - 3600, dt.timezone.utc).isoformat().replace("+00:00", "Z")
            status.write_text(json.dumps({"schemaVersion": 1, "host": "mac", "observedAt": observed, "models": []}))
            peers, _ = probes.read_peers(root, "pc", now)
            self.assertEqual([(peer["host"], peer["source"]) for peer in peers], [("mac", "agiw-peers")])
            for models in (5, {"id": "x"}):
                status.write_text(json.dumps({"schemaVersion": 1, "host": "mac", "observedAt": observed, "models": models}))
                peers, _ = probes.read_peers(root, "pc", now)  # malformed legacy file is ignored, not fatal
                self.assertEqual([peer["source"] for peer in peers], ["agiw-peers"])

    def test_nvidia_path_does_not_search_user_path(self):
        from unittest import mock
        with mock.patch.object(Path, "is_file", return_value=False), mock.patch.dict(os.environ, {"PATH": "C:\\untrusted"}):
            self.assertIsNone(probes.nvidia_smi_path())

    def test_link_local_mac_targets_are_refused(self):
        seen = []
        def fetch(url, *_):
            seen.append(url)
            raise ConnectionRefusedError()
        hosts = {"mdns": None, "ips": ["169.254.1.5", "<lan-ip-9>"], "port": 1234, "ownIp": None}
        probes.probe_mac(hosts, fetch, resolve=lambda _n: [], own=set())
        self.assertTrue(seen and all("<lan-ip-9>" in u for u in seen))


class LiveRate(unittest.TestCase):
    def lane(self, *slots, task=7):
        return {"slots": [{"id": i, "isProcessing": busy, "decodedTokens": dec, "taskId": task + i}
                          for i, (busy, dec) in enumerate(slots)]}

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

    def test_a_new_request_in_the_same_slot_is_not_a_rate(self):
        # Codex finding 8: 5 -> 120 decoded across two requests must not read as 115 tok/s.
        _, prev = probes.live_decode_rate({}, self.lane((True, 5), task=7), 100.0)
        self.assertIsNone(probes.live_decode_rate(prev, self.lane((True, 120), task=8), 101.0)[0])

    def test_no_task_id_means_no_rate(self):
        lane = {"slots": [{"id": 0, "isProcessing": True, "decodedTokens": 5, "taskId": None}]}
        _, prev = probes.live_decode_rate({}, lane, 100.0)
        lane["slots"][0]["decodedTokens"] = 50
        self.assertIsNone(probes.live_decode_rate(prev, lane, 101.0)[0])

    def test_two_busy_slots_sum(self):
        _, prev = probes.live_decode_rate({}, self.lane((True, 0), (True, 0)), 10.0)
        self.assertEqual(probes.live_decode_rate(prev, self.lane((True, 20), (True, 30)), 11.0)[0], 50.0)


class Codemode(unittest.TestCase):
    def test_malformed_manifests_are_skipped_not_fatal(self):
        # Codex finding 1: [] or {"metrics": {}} must not stop sampling.
        with tempfile.TemporaryDirectory() as tmp:
            for i, value in enumerate(([], {"metrics": {}}, {"metrics": ["x", 3]})):
                run = Path(tmp) / f"2026092{i}-000000-aaaaaa"
                run.mkdir()
                (run / "manifest.json").write_text(json.dumps(value))
            jobs, source = probes.codemode_jobs(Path(tmp), [], time.time())
        self.assertEqual(jobs["recent"], [])
        self.assertIn("2 malformed manifests skipped", source["detail"])

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


class PeerLink(unittest.TestCase):
    def write(self, root, name, value, age=0.0):
        directory = probes.peer_dir(root)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(json.dumps(value))

    def peer(self, host, observed, **extra):
        return {"schemaVersion": 1, "kind": "agiw-peer", "host": host, "platform": "macos", "edition": "mac",
                "version": "1.0.0", "observedUnix": observed, "health": "ok",
                "models": [{"id": "qwen/qwen3.8-27b", "loadedState": "loaded", "activity": "idle"}], **extra}

    def test_reads_fresh_and_stale_peers_and_skips_itself(self):
        now = 1000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "louis-m5-pro.json", self.peer("louis-m5-pro", now - 4))
            self.write(root, "macpro51.json", self.peer("macpro51", now - 7200, platform="linux"))
            self.write(root, "louisaurorar12.json", self.peer("louisaurorar12", now - 1, platform="windows"))
            self.write(root, "junk.json", {"kind": "other"})
            peers, source = probes.read_peers(root, "louisaurorar12", now)
        self.assertEqual([p["host"] for p in peers], ["louis-m5-pro", "macpro51"])
        self.assertEqual([p["fresh"] for p in peers], [True, False])
        self.assertEqual(source["state"], "live")
        self.assertIn("1 reported (louis-m5-pro;", source["detail"])

    def test_legacy_mac_status_counts_when_no_fresh_mac_peer(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "llm-lab" / "status"
            status.mkdir(parents=True)
            observed = dt.datetime.fromtimestamp(now - 10, dt.timezone.utc).isoformat().replace("+00:00", "Z")
            (status / "mac-status.json").write_text(json.dumps({"schemaVersion": 1, "host": "mac", "observedAt": observed,
                "health": "partial", "models": [{"id": "qwen/qwen3.8-27b", "host": "mac", "loadedState": "loaded"},
                                                {"id": "x", "host": "windows", "loadedState": "loaded"}]}))
            peers, source = probes.read_peers(root, "pc", now)
        self.assertEqual((peers[0]["host"], peers[0]["source"], peers[0]["fresh"]), ("mac", "mac-status.json", True))
        self.assertEqual([m["id"] for m in peers[0]["models"]], ["qwen/qwen3.8-27b"])

    def test_presence_freshness_uses_unrounded_age(self):
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for age in (29.999, 30.0, 30.04, 30.05):
                with self.subTest(age=age):
                    self.write(root, "mac.json", self.peer("mac", now - age))
                    peers, source = probes.read_peers(root, "pc", now)
                    self.assertEqual(len(peers), 1)
                    self.assertEqual(peers[0]["fresh"], age <= 30)
                    self.assertEqual(peers[0]["ageSeconds"], 30.0)
                    self.assertEqual(source["state"], "live" if age <= 30 else "unavailable")
                    self.assertFalse(peers[0]["verified"])
                    self.assertEqual(peers[0]["models"][0]["loadedState"], "loaded")

    def test_legacy_freshness_uses_unrounded_age(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = root / "llm-lab" / "status"
            status.mkdir(parents=True)
            for age in (29.999, 30.0, 30.04, 30.05):
                with self.subTest(age=age):
                    observed = dt.datetime.fromtimestamp(now - age, dt.timezone.utc).isoformat()
                    (status / "mac-status.json").write_text(json.dumps({
                        "schemaVersion": 1, "host": "mac", "observedAt": observed,
                        "models": [{"id": "reported-model", "host": "mac", "loadedState": "loaded"}]}))
                    peers, source = probes.read_peers(root, "pc", now)
                    self.assertEqual(len(peers), 1)
                    self.assertEqual(peers[0]["fresh"], age <= 30)
                    self.assertEqual(peers[0]["ageSeconds"], 30.0)
                    self.assertEqual(source["state"], "live" if age <= 30 else "unavailable")
                    self.assertFalse(peers[0]["verified"])
                    self.assertEqual(peers[0]["models"][0]["loadedState"], "loaded")

    def test_expired_presence_allows_a_fresh_legacy_fallback(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for presence_age, legacy_age, expected in ((30.04, 10, "mac-status.json"),
                                                       (10, 30.04, "agiw-peers")):
                with self.subTest(presence_age=presence_age, legacy_age=legacy_age):
                    self.write(root, "mac.json", self.peer("mac", now - presence_age))
                    observed = dt.datetime.fromtimestamp(now - legacy_age, dt.timezone.utc).isoformat()
                    (probes.peer_dir(root).parent / "mac-status.json").write_text(json.dumps({
                        "schemaVersion": 1, "host": "mac", "observedAt": observed,
                        "models": [{"id": "legacy-model", "host": "mac", "loadedState": "inactive"}]}))
                    peers, source = probes.read_peers(root, "pc", now)
                    self.assertEqual([(p["source"], p["fresh"]) for p in peers], [(expected, True)])
                    self.assertEqual(peers[0]["ageSeconds"], 10)
                    self.assertEqual(source["state"], "live")
                    self.assertFalse(peers[0]["verified"])

    def test_future_or_malformed_presence_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "f.json", self.peer("future", 5000.0))
            self.write(root, "h.json", self.peer("bad host!", 990.0))
            peers, source = probes.read_peers(root, "pc", 1000.0)
        self.assertEqual(peers, [])
        self.assertEqual(source["state"], "unavailable")

    def test_invalid_clock_keeps_a_valid_peer(self):
        now = 2_000_000_000.0
        invalid = (10**1000, -10**1000, 10**12 + 1, -10**12 - 1,
                   1e12 + 1, -1e12 - 1, True, None, "2000000000", float("nan"), float("inf"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, observed in enumerate(invalid):
                with self.subTest(clock=index):
                    bad = self.peer("bad-peer", observed)
                    # Both validation and file isolation must handle invalid clocks.
                    self.assertIsNone(probes._validate_peer(bad, now))
                    for bad_newest in (False, True):
                        with self.subTest(bad_newest=bad_newest):
                            self.write(root, "bad-peer.json", bad)
                            self.write(root, "good-peer.json", self.peer("good-peer", now - 4))
                            for name, mtime in (("bad-peer.json", now if bad_newest else now - 1),
                                                ("good-peer.json", now - 1 if bad_newest else now)):
                                os.utime(probes.peer_dir(root) / name, (mtime, mtime))
                            peers, source = probes.read_peers(root, "pc", now)
                            self.assertEqual([(p["host"], p["fresh"]) for p in peers], [("good-peer", True)])
                            self.assertEqual(source["state"], "live")

    def test_oversized_clock_keeps_fresh_legacy_fallback(self):
        import datetime as dt
        now = 2_000_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sign in (1, -1):
                with self.subTest(sign=sign):
                    self.write(root, "bad-peer.json", self.peer("bad-peer", sign * 10**1000))
                    observed = dt.datetime.fromtimestamp(now - 4, dt.timezone.utc).isoformat()
                    (probes.peer_dir(root).parent / "mac-status.json").write_text(json.dumps({
                        "schemaVersion": 1, "host": "mac", "observedAt": observed,
                        "models": [{"id": "legacy-model", "host": "mac", "loadedState": "loaded"}]}))
                    peers, source = probes.read_peers(root, "pc", now)
                    self.assertEqual([(p["host"], p["source"], p["fresh"]) for p in peers],
                                     [("mac", "mac-status.json", True)])
                    self.assertEqual(peers[0]["models"][0]["id"], "legacy-model")
                    self.assertEqual(source["state"], "live")

    def test_clock_future_and_freshness_boundaries(self):
        now = 2_000_000_000.0
        for offset, accepted, fresh in ((4, True, True), (5, True, True), (6, False, None),
                                         (-30, True, True), (-30.04, True, False)):
            with self.subTest(offset=offset):
                peer = probes._validate_peer(self.peer("mac", now + offset), now)
                if accepted:
                    self.assertEqual(peer["fresh"], fresh)
                    self.assertEqual(peer["ageSeconds"], round(max(0.0, -offset), 1))
                else:
                    self.assertIsNone(peer)
        self.assertFalse(probes._validate_peer(self.peer("mac", -1), now)["fresh"])

    def test_overflow_in_one_candidate_keeps_other_peer_reports(self):
        from unittest import mock
        now = 2_000_000_000.0
        validate = probes._validate_peer

        def validate_with_overflow(value, at):
            if value["host"] == "bad-peer":
                raise OverflowError("Test numeric overflow")
            return validate(value, at)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write(root, "good-peer.json", self.peer("good-peer", now - 4))
            self.write(root, "bad-peer.json", self.peer("bad-peer", now - 4))
            with mock.patch.object(probes, "_validate_peer", side_effect=validate_with_overflow):
                peers, source = probes.read_peers(root, "pc", now)
        self.assertEqual([p["host"] for p in peers], ["good-peer"])
        self.assertEqual(source["state"], "live")


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

    def test_missing_lane_stays_as_explicit_unknown(self):
        # Codex finding 6: a config with only the fast lane must not drop the deep lane silently.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "windows-llm-pipeline"
            path.mkdir()
            (path / "config.json").write_text(json.dumps({"runtime": {"lanes": {"fast": {"port": 1235, "alias": "a"}}}}))
            specs, source = probes.lane_specs(Path(tmp))
        self.assertEqual([s["id"] for s in specs], ["fast", "deep"])
        self.assertTrue(specs[1]["undeclared"])
        self.assertEqual(source["state"], "error")
        lane = probes.probe_lane(specs[1], fake_fetch({"/health": {}}))
        self.assertEqual(lane["status"], "undeclared")

    def test_malformed_shapes_fall_back(self):
        # Codex finding 1: valid JSON with the wrong shape must not raise.
        for config in ([], {"runtime": []}, {"runtime": {"lanes": []}},
                       {"runtime": {"lane_layout": "x", "lanes": {"fast": {"port": 1, "alias": "a"}, "deep": {"port": 2, "alias": "b"}}}}):
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "windows-llm-pipeline"
                path.mkdir()
                (path / "config.json").write_text(json.dumps(config))
                specs, _ = probes.lane_specs(Path(tmp))
            self.assertEqual(len(specs), 2, config)

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
        # Codex round 2, finding 6: a recent claim is "claimed" (execution unknown), never "running".
        self.assertEqual(pipeline["status"], "claimed")
        self.assertIn("execution unknown", source["detail"])
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

    def test_a_stale_pending_job_does_not_hide_a_fresh_one(self):
        # Codex finding 12.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending").mkdir()
            for name in ("old", "new"):
                (root / "pending" / f"{name}.json").write_text("{}")
            old = time.time() - 3 * 86400
            os.utime(root / "pending" / "old.json", (old, old))
            pipeline, source = probes.route_queue(root)
        self.assertEqual(pipeline["status"], "queued")
        self.assertEqual(pipeline["queue"]["stalePending"], 1)

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
    HOSTS = {"mdns": "mac.local", "hostname": "mac", "ips": ["<mac-lan-ip>", "<lan-ip-194>"], "port": 1234,
             "expectedVerify": "openai/gpt-oss-20b", "ownIp": "<pc-lan-ip>"}

    def test_v0_listing_reports_loaded(self):
        fetch = fake_fetch({"<mac-lan-ip>:1234/api/v0/models": {"data": [
            {"id": "openai/gpt-oss-20b", "type": "llm", "state": "loaded"},
            {"id": "qwen/qwen3.8-27b", "type": "llm", "state": "not-loaded"},
            {"id": "text-embedding-nomic", "type": "embeddings", "state": "loaded"}]}})
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: ["<mac-lan-ip>"], own=set())
        self.assertEqual(result["state"], "reachable")
        self.assertEqual(result["via"], "mdns")
        self.assertEqual(result["loadedCount"], 1)
        self.assertTrue(result["expectedVerifyLoaded"])

    def test_falls_back_to_recorded_ip_and_v1(self):
        fetch = fake_fetch({"<lan-ip-194>:1234/v1/models": {"data": [{"id": "m"}]}})
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: (_ for _ in ()).throw(OSError("no mdns")), own=set())
        self.assertEqual((result["state"], result["address"], result["via"]), ("reachable", "<lan-ip-194>", "recorded-ip"))
        self.assertIsNone(result["loadedCount"])

    def test_never_probes_itself_or_loopback(self):
        seen = []
        def fetch(url, *_):
            seen.append(url)
            raise ConnectionRefusedError()
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: ["<pc-lan-ip>", "127.0.0.1", "<lan-ip>"],
                                  own={"<lan-ip>"})
        self.assertEqual(result["state"], "unreachable")
        self.assertFalse(any("<pc-lan-ip>" in u or "127.0.0.1" in u or "<lan-ip>" in u for u in seen))

    def test_results_do_not_depend_on_the_test_machine(self):
        # On the Mac, <mac-lan-ip>/.194 are its own addresses; an explicit own set keeps the test hermetic.
        fetch = fake_fetch({"<mac-lan-ip>:1234/v1/models": {"data": [{"id": "m"}]}})
        original = probes._own_addresses
        probes._own_addresses = lambda: {"<mac-lan-ip>", "<lan-ip-194>"}
        try:
            self.assertEqual(probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: [], own=set())["state"], "reachable")
            self.assertEqual(probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: [])["state"], "unreachable")
        finally:
            probes._own_addresses = original

    def test_names_and_loopback_in_recorded_ips_are_never_probed(self):
        # Codex finding 9: "localhost" would reach this PC's own lane on :1234.
        seen = []
        def fetch(url, *_):
            seen.append(url)
            raise ConnectionRefusedError()
        hosts = {**self.HOSTS, "ips": ["localhost", "127.0.0.2", "8.8.8.8", "::1", "<lan-ip-9>"]}
        probes.probe_mac(hosts, fetch, resolve=lambda _n: [], own=set())
        self.assertTrue(seen and all("<lan-ip-9>" in u for u in seen), seen)

    def test_expected_model_needs_the_full_id(self):
        # Codex finding 10: other/gpt-oss-20b is not openai/gpt-oss-20b.
        fetch = fake_fetch({"<mac-lan-ip>:1234/api/v0/models": {"data": [{"id": "other/gpt-oss-20b", "type": "llm", "state": "loaded"}]}})
        result = probes.probe_mac(self.HOSTS, fetch, resolve=lambda _n: ["<mac-lan-ip>"], own=set())
        self.assertFalse(result["expectedVerifyLoaded"])

    def test_unreachable_reports_why(self):
        def failing(error):
            def fetch(url, *_):
                raise error
            return fetch
        cases = [(ConnectionRefusedError(), "refused", "Connection refused on"), (TimeoutError(), "timeout", "No answer"),
                 (OSError("HTTP Error 404"), "invalid", "not with an LM Studio model list"),
                 (ValueError("model list missing"), "invalid", "not with an LM Studio model list"),
                 (OSError("unreachable network"), "error", "No answer")]
        for error, reason, wording in cases:
            result = probes.probe_mac(self.HOSTS, failing(error), resolve=lambda _n: [], own=set())
            self.assertEqual((result["state"], result["reason"]), ("unreachable", reason), error)
            self.assertIn(wording, result["detail"])
            self.assertIn("<mac-lan-ip>", result["detail"])
            self.assertNotIn("LM Studio is not", result["detail"])  # a TCP error never names its cause
        # One address refusing beats another timing out: a host is there.
        def mixed(url, *_):
            raise ConnectionRefusedError() if "<lan-ip-194>" in url else TimeoutError()
        self.assertEqual(probes.probe_mac(self.HOSTS, mixed, resolve=lambda _n: [], own=set())["reason"], "refused")
        nowhere = {**self.HOSTS, "ips": []}
        result = probes.probe_mac(nowhere, failing(TimeoutError()), resolve=lambda _n: [], own=set())
        self.assertEqual(result["reason"], "unresolved")

    def test_unknown_residency_does_not_prove_a_total(self):
        partial = fake_fetch({"<mac-lan-ip>:1234/api/v0/models": {"data": [
            {"id": "qwen/qwen3.8-27b", "type": "llm", "state": "loaded"},
            {"id": "openai/gpt-oss-20b", "type": "llm", "state": "loading"}]}})
        result = probes.probe_mac(self.HOSTS, partial, resolve=lambda _n: ["<mac-lan-ip>"], own=set())
        self.assertIsNone(result["loadedCount"])
        self.assertIsNone(result["expectedVerifyLoaded"])  # its own row is unknown, not proven unloaded
        self.assertIn("at least 1 loaded", result["detail"])
        empty = fake_fetch({"<mac-lan-ip>:1234/api/v0/models": {"data": []}})
        result = probes.probe_mac(self.HOSTS, empty, resolve=lambda _n: ["<mac-lan-ip>"], own=set())
        self.assertEqual((result["loadedCount"], result["expectedVerifyLoaded"]), (0, False))
        self.assertIn("; 0 loaded", result["detail"])

    def test_old_probe_result_expires(self):
        # Codex finding 11.
        peer = probes.MacPeer(Path("/x"), probe=lambda _h: {"state": "reachable", "observedAt": 100.0, "detail": "ok",
                                                            "models": [{"id": "m", "state": "loaded"}], "loadedCount": 1,
                                                            "reason": "refused"})
        peer.poll_once()
        data, source = peer.read(now=200.0)
        self.assertEqual((data["state"], data["models"], source["state"]), ("unknown", [], "unavailable"))
        self.assertIsNone(data.get("reason"), "an expired probe must not retain an old LAN diagnosis")

    def test_peer_ages_result(self):
        peer = probes.MacPeer(Path("/x"), probe=lambda _h: {"state": "reachable", "observedAt": 100.0, "detail": "ok", "models": []})
        peer.poll_once()
        data, source = peer.read(now=112.0)
        self.assertEqual(data["ageSeconds"], 12.0)
        self.assertEqual(source["ageSeconds"], 12.0)

    def test_invalid_probe_times_cannot_prove_lan_state(self):
        from copy import deepcopy

        times = [{}, *({"observedAt": value} for value in
                      (None, "100", [], {}, True, False, float("nan"), float("inf"),
                       -float("inf"), -1, 10 ** 1000, 106.0, 3700.0))]
        for fields in times:
            with self.subTest(fields=fields):
                result = {"state": "reachable", "models": [{"id": "m", "state": "loaded"}],
                          "loadedCount": 1, "expectedVerifyLoaded": True, "reason": "refused", **fields}
                peer = probes.MacPeer(Path("/x"), probe=lambda _h: result)
                peer.poll_once()
                cached = deepcopy(result)
                data, source = peer.read(now=100.0)
                self.assertEqual((data["state"], data["models"], source["state"]),
                                 ("unknown", [], "unavailable"))
                self.assertIsNone(data["loadedCount"])
                self.assertIsNone(data.get("expectedVerifyLoaded"))
                self.assertIsNone(data.get("reason"))
                self.assertIsNone(data["ageSeconds"])
                self.assertIn("Mac state unknown", data["detail"])
                self.assertIsNone(source["ageSeconds"])
                json.dumps(data, allow_nan=False)
                self.assertEqual(peer._latest, cached, "reading must not alter the cached probe")

    def test_small_future_skew_is_accepted_up_to_five_seconds(self):
        for skew, state in ((4.0, "reachable"), (5.0, "reachable"), (5.01, "unknown")):
            with self.subTest(skew=skew):
                peer = probes.MacPeer(Path("/x"), probe=lambda _h: {
                    "state": "reachable", "observedAt": 100.0 + skew, "detail": "ok",
                    "models": [{"id": "m", "state": "loaded"}], "loadedCount": 1,
                })
                peer.poll_once()
                data, source = peer.read(now=100.0)
                self.assertEqual(data["state"], state)
                if state == "reachable":
                    self.assertEqual(data["ageSeconds"], 0.0)
                    self.assertEqual((data["loadedCount"], source["state"]), (1, "live"))
                else:
                    self.assertIsNone(data["ageSeconds"])
                    self.assertIn("ahead of this PC's clock", data["detail"])

    def test_probe_expiry_uses_unrounded_age(self):
        peer = probes.MacPeer(Path("/x"), probe=lambda _h: {
            "state": "reachable", "observedAt": 100.0, "detail": "ok",
            "models": [{"id": "m", "state": "loaded"}], "loadedCount": 1,
            "expectedVerifyLoaded": False,
        })
        peer.poll_once()
        self.assertEqual(peer.read(now=145.0)[0]["state"], "reachable")
        data, source = peer.read(now=145.01)
        self.assertEqual((data["state"], data["models"], source["state"]),
                         ("unknown", [], "unavailable"))
        self.assertIsNone(data.get("expectedVerifyLoaded"))

    def test_hosts_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "llm-lab" / "mac-reach"
            path.mkdir(parents=True)
            (path / "hosts.json").write_text(json.dumps({"mac": {"mdns": "m.local", "ips": ["1.2.3.4"], "lm_studio_port": 1234},
                                                          "windows": {"ip": "<pc-lan-ip>"}}))
            hosts = probes.mac_hosts(Path(tmp))
        self.assertEqual((hosts["mdns"], hosts["ips"], hosts["ownIp"]), ("m.local", ["1.2.3.4"], "<pc-lan-ip>"))


class PCPresenceModels(unittest.TestCase):
    NOW = 2_000_000_000.0

    def snapshot(self):
        import datetime as dt

        return {"sampledAt": self.NOW,
                "observedAt": dt.datetime.fromtimestamp(self.NOW, dt.timezone.utc).isoformat(),
                "observerVersion": "test", "dashboardPort": 8767,
                "lanes": [{"id": "fast", "port": 1235, "expectedModel": "openai/gpt-oss-20b",
                           "status": "busy", "phase": "decoding", "slotsBusy": 1, "slotsTotal": 2,
                           "servedModel": r"C:\private\models\gpt-oss-20b.gguf", "liveTokensPerSecond": 12.5},
                          {"id": "deep", "port": 1234, "expectedModel": "qwen3.8-27b",
                           "status": "idle", "phase": "idle", "slotsBusy": 0, "slotsTotal": 2,
                           "servedModel": "/private/models/qwen.gguf", "liveTokensPerSecond": 0}]}

    def test_presence_keeps_status_model_details_from_the_same_sample(self):
        from copy import deepcopy

        snapshot = self.snapshot()
        original = deepcopy(snapshot)
        status = probes.pc_status(snapshot)
        presence = probes.peer_presence(snapshot, "test-pc")
        self.assertEqual(presence["observedAt"], status["observedAt"])
        self.assertEqual(presence["observedUnix"], snapshot["sampledAt"])
        self.assertEqual([row["id"] for row in presence["models"]],
                         ["gpt-oss-20b", "Qwen3.8-27B Q4_K_M"])
        for row, status_row in zip(presence["models"], status["models"]):
            for key in ("id", "aliases", "loadedState", "activity", "slotsBusy", "slotsTotal",
                        "lane", "servedModel", "liveTokensPerSecond"):
                with self.subTest(model=row["id"], key=key):
                    self.assertIn(key, row)
                    self.assertEqual(row[key], status_row[key])
        payload = json.dumps(presence, allow_nan=False).encode("utf-8")
        self.assertNotIn(b"private", payload)
        self.assertLessEqual(len(payload), probes.PEER_MAX_BYTES)
        self.assertEqual(snapshot, original, "publishing must not alter the sampled evidence")

    def test_presence_omits_invalid_optional_model_details(self):
        bad_text = (None, True, 4, [], {}, "", "fast\n", "fast\x00")
        bad_rates = (None, True, False, "12.5", [], {}, -0.01, float("nan"),
                     float("inf"), -float("inf"), 10 ** 1000)
        for key, values in (("id", bad_text), ("servedModel", bad_text),
                            ("liveTokensPerSecond", bad_rates)):
            for value in values:
                with self.subTest(key=key, value_type=type(value).__name__, value=repr(value)[:80]):
                    snapshot = self.snapshot()
                    snapshot["lanes"][0][key] = value
                    presence = probes.peer_presence(snapshot, "test-pc")
                    self.assertNotIn("lane" if key == "id" else key, presence["models"][0])
                    self.assertEqual(len(presence["models"]), 2, "invalid optional fields must keep the sample")
                    self.assertEqual(presence["models"][1]["id"], "Qwen3.8-27B Q4_K_M")
                    json.dumps(presence, allow_nan=False)

    def test_extra_presence_details_do_not_change_the_pc_peer_reader(self):
        from copy import deepcopy

        presence = probes.peer_presence(self.snapshot(), "other-pc")
        # Exercise the reader independently of the publisher, including fields
        # that it deliberately does not project into an unsigned peer report.
        presence["models"][0].update(lane="fast", servedModel="gpt-oss-20b.gguf", liveTokensPerSecond=12.5)
        legacy = deepcopy(presence)
        for row in legacy["models"]:
            for key in ("lane", "servedModel", "liveTokensPerSecond"):
                row.pop(key, None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = probes.peer_dir(root) / "other-pc.json"
            probes.write_json_atomic(target, legacy, probes.PEER_MAX_BYTES)
            before = probes.read_peers(root, "test-pc", self.NOW)
            probes.write_json_atomic(target, presence, probes.PEER_MAX_BYTES)
            after = probes.read_peers(root, "test-pc", self.NOW)
        self.assertEqual(after, before)
        self.assertTrue(after[0][0]["fresh"])
        self.assertFalse(after[0][0]["verified"])


class PCStatusComponents(unittest.TestCase):
    def publish(self, *components):
        snapshot = {"observedAt": "2023-11-14T22:13:20Z", "observerVersion": "test",
                    "lanes": [{"id": "fast", "expectedModel": "openai/gpt-oss-20b", "status": "idle"},
                              {"id": "deep", "expectedModel": "qwen3.8-27b", "status": "busy"}]}
        baseline = probes.pc_status(snapshot)
        out = probes.pc_status({**snapshot, "components": list(components)})
        probes.validate_status(out)
        self.assertEqual({k: v for k, v in out.items() if k != "components"},
                         {k: v for k, v in baseline.items() if k != "components"})
        return out

    def test_known_components_keep_peer_fields_and_legacy_reader_fields(self):
        nisi = {"id": "nisi", "label": "Nisi gate", "state": "ready", "detail": "pins verified",
                "authorModel": "private-extra", "secretPath": "do not publish"}
        jev = {"id": "jev", "label": "Jev", "state": "unknown", "detail": "no passive record",
               "lastJudgedAgeSeconds": 12.5, "prompt": "do not publish"}
        out = self.publish(nisi, jev)
        self.assertEqual(out["components"], [
            {"id": "nisi", "label": "Nisi gate", "state": "ready", "detail": "pins verified",
             "name": "Nisi gate", "status": "ready"},
            {"id": "jev", "label": "Jev", "state": "unknown", "detail": "no passive record",
             "lastJudgedAgeSeconds": 12.5, "name": "Jev", "status": "unknown"}])
        self.assertLessEqual(len(json.dumps(out, allow_nan=False).encode("utf-8")), probes.STATUS_MAX_BYTES)
        self.assertEqual(nisi["secretPath"], "do not publish")  # publishing does not mutate its snapshot

    def test_canonical_text_removes_controls_then_caps_without_changing_legacy_fields(self):
        text = "\x00\n" + "a\t" * 130 + "\x7f\x85"
        row = self.publish({"id": "nisi", "label": text, "state": text, "detail": text})["components"][0]
        self.assertEqual({k: row[k] for k in ("label", "state", "detail")},
                         {k: "a" * 128 for k in ("label", "state", "detail")})
        self.assertEqual((row["name"], row["status"]), (text, text))
        row = self.publish({"id": "jev", "label": "Jev", "state": "unknown", "detail": "caf\u00e9"})["components"][0]
        self.assertEqual(row["detail"], "caf\u00e9")

    def test_missing_or_invalid_canonical_text_is_omitted_without_guesses(self):
        for value in (None, True, 123, [], {}, "", "\x00\n\t", "   "):
            with self.subTest(value=value):
                row = self.publish({"id": "jev", "label": value, "state": value, "detail": value})["components"][0]
                self.assertEqual(row, {"id": "jev", "name": value, "status": value})
        self.assertEqual(self.publish({"id": "nisi"})["components"],
                         [{"id": "nisi", "name": None, "status": None}])

    def test_judgment_age_requires_a_finite_nonnegative_number_and_preserves_zero(self):
        for age in (0, -0.0, 12.5, 10**13, 1e300, sys.float_info.max):
            with self.subTest(age=age):
                row = self.publish({"id": "jev", "lastJudgedAgeSeconds": age})["components"][0]
                self.assertEqual(row["lastJudgedAgeSeconds"], age)
        for age in (None, True, False, -0.001, float("nan"), float("inf"), -float("inf"),
                    "12", [], {}, 10**400):
            with self.subTest(age_type=type(age).__name__):
                row = self.publish({"id": "jev", "lastJudgedAgeSeconds": age})["components"][0]
                self.assertNotIn("lastJudgedAgeSeconds", row)

    def test_other_or_missing_ids_keep_exactly_the_existing_reader_shape(self):
        for component_id in ("other", "NISI", "", None, True, [], {}):
            with self.subTest(component_id=component_id):
                component = {"id": component_id, "label": "original\nlabel", "state": ["unknown"],
                             "detail": "not exported", "lastJudgedAgeSeconds": 4}
                self.assertEqual(self.publish(component)["components"],
                                 [{"name": "original\nlabel", "status": ["unknown"]}])
        self.assertEqual(self.publish({"label": "Jev", "state": "unknown"})["components"],
                         [{"name": "Jev", "status": "unknown"}])

    def test_malformed_component_rows_cannot_drop_the_status_sample(self):
        out = self.publish(None, True, [], "nisi", {"id": "nisi", "label": "Nisi gate", "state": "ready"})
        self.assertEqual(out["components"],
                         [{"id": "nisi", "label": "Nisi gate", "state": "ready", "name": "Nisi gate", "status": "ready"}])
        for malformed in (True, 123, "nisi", {"id": "nisi"}):
            with self.subTest(container=malformed):
                self.assertEqual(probes.pc_status({"components": malformed})["components"], [])


class PCStatusLimitations(unittest.TestCase):
    NOW = 1700000000.0

    def status(self, peer=Ellipsis, mismatch=False, sampled_at=1700000000.0):
        data = {"observedAt": "2023-11-14T22:13:20Z", "sampledAt": sampled_at, "observerVersion": "test",
                "lanes": [{"id": "fast", "expectedModel": "openai/gpt-oss-20b", "port": 1235,
                           "status": "idle", "phase": "idle", "slotsBusy": 0, "slotsTotal": 2},
                          {"id": "deep", "expectedModel": "qwen3.8-27b", "port": 1234,
                           "status": "identity_mismatch" if mismatch else "busy", "phase": "generating",
                           "slotsBusy": 1, "slotsTotal": 2}]}
        if peer is not Ellipsis:
            data["macPeer"] = peer
        status = probes.pc_status(data)
        probes.validate_status(status)
        self.assertEqual((status["schemaVersion"], status["host"]), (1, "windows"))
        self.assertEqual([m["id"] for m in status["models"]], ["gpt-oss-20b", "Qwen3.8-27B Q4_K_M"])
        self.assertEqual([m["aliases"] for m in status["models"]], [["openai/gpt-oss-20b"], ["qwen3.8-27b"]])
        self.assertEqual([m["loadedState"] for m in status["models"]],
                         ["loaded", "unknown" if mismatch else "loaded"])
        self.assertEqual(status["observedAt"], data["observedAt"])
        self.assertEqual(set(status), {"schemaVersion", "observedAt", "host", "producer", "health", "tasks",
                                      "models", "gpus", "adapters", "memory", "routeQueue", "components", "limitations"})
        self.assertLessEqual(len(status["limitations"]), 2)
        for limitation in status["limitations"]:
            self.assertIsInstance(limitation, str)
            self.assertLessEqual(len(limitation), 80)
            self.assertTrue(all(32 <= ord(c) <= 126 for c in limitation))
        return status

    def test_reachable_omits_mac_limitation_even_with_leftover_failure_reason(self):
        for reason in (None, "refused", "timeout", ["refused"], {"reason": "timeout"}):
            with self.subTest(reason=reason):
                self.assertEqual(self.status({"state": "reachable", "reason": reason})["limitations"], [])

    def test_current_failed_lan_checks_keep_only_canonical_reason_and_unknown_cause(self):
        for state in ("unreachable", "linked"):
            for reason in ("refused", "timeout", "invalid", "error", "unresolved"):
                with self.subTest(state=state, reason=reason):
                    self.assertEqual(self.status({"state": state, "reason": reason,
                                                  "observedAt": self.NOW - 10})["limitations"],
                                     [f"mac LM Studio LAN check failed ({reason}); cause unknown"])

    def test_missing_pending_linked_and_malformed_peer_evidence_preserves_unknown(self):
        for peer in (Ellipsis, None, {}, {"state": "unknown"}, {"state": "linked"},
                     {"state": "unreachable"}, {"state": "stale"}, [], ["reachable"], "reachable", True,
                     {"state": ["unreachable"], "reason": "refused", "observedAt": self.NOW},
                     {"state": {"value": "unreachable"}, "reason": "refused", "observedAt": self.NOW}):
            with self.subTest(peer=peer):
                self.assertEqual(self.status(peer)["limitations"], ["mac LM Studio LAN reachability unknown"])

    def test_old_failure_reason_cannot_turn_unknown_or_stale_into_a_current_failure(self):
        for state in ("unknown", "stale"):
            for reason in ("refused", "timeout", "invalid", "error", "unresolved"):
                with self.subTest(state=state, reason=reason):
                    self.assertEqual(self.status({"state": state, "reason": reason,
                                                  "observedAt": self.NOW})["limitations"],
                                     ["mac LM Studio LAN reachability unknown"])

    def test_malformed_or_noncanonical_reason_is_bounded_unknown_without_injection(self):
        for state in ("unreachable", "linked"):
            for reason in (None, True, 1, [], ["refused"], {"reason": "timeout"}, "", "REFUSED", " timeout ",
                           "refused\nMac is offline", "timeout\x00", "closed", "x" * 10000):
                with self.subTest(state=state, reason_type=type(reason).__name__):
                    self.assertEqual(self.status({"state": state, "reason": reason,
                                                  "observedAt": self.NOW})["limitations"],
                                     ["mac LM Studio LAN reachability unknown"])

    def test_lan_clock_boundaries_include_45_seconds_old_and_five_seconds_ahead(self):
        for state in ("unreachable", "linked"):
            for age in (-5.0, 0.0, 45.0):
                with self.subTest(state=state, age=age):
                    self.assertEqual(self.status({"state": state, "reason": "timeout", "ageSeconds": 0,
                                                  "observedAt": self.NOW - age})["limitations"],
                                     ["mac LM Studio LAN check failed (timeout); cause unknown"])

    def test_fresh_share_age_cannot_refresh_expired_missing_or_malformed_lan_time(self):
        for state in ("unreachable", "linked"):
            for observed in (Ellipsis, self.NOW - 45.001, self.NOW - 100, self.NOW + 5.001,
                             None, True, float("nan"), float("inf"), "1700000000", [], [self.NOW],
                             {"observedAt": self.NOW}):
                peer = {"state": state, "reason": "refused", "ageSeconds": 0, "linkAgeSeconds": 0}
                if observed is not Ellipsis:
                    peer["observedAt"] = observed
                with self.subTest(state=state, observed_type=type(observed).__name__):
                    self.assertEqual(self.status(peer)["limitations"], ["mac LM Studio LAN reachability unknown"])

    def test_invalid_snapshot_clock_cannot_make_a_lan_reason_current(self):
        for sampled in (None, True, float("nan"), float("inf"), "1700000000", [], [self.NOW],
                        {"sampledAt": self.NOW}):
            with self.subTest(sampled_type=type(sampled).__name__):
                self.assertEqual(self.status({"state": "linked", "reason": "timeout", "ageSeconds": 0,
                                              "observedAt": self.NOW}, sampled_at=sampled)["limitations"],
                                 ["mac LM Studio LAN reachability unknown"])

    def test_mac_wording_keeps_the_existing_identity_mismatch_limitation(self):
        status = self.status({"state": "linked", "reason": "timeout", "observedAt": self.NOW}, mismatch=True)
        self.assertEqual(status["limitations"], ["lane identity mismatch: deep",
                                                "mac LM Studio LAN check failed (timeout); cause unknown"])


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
