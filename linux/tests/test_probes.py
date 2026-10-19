import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import linsys, probes


def listener(process, port):
    return {"state": "up", "listeners": [{"local": "127.0.0.1", "localPort": port,
            "pid": process["pid"], "startTimeTicks": process["startTimeTicks"], "ownershipVerified": True}]}


class PairTests(unittest.TestCase):
    def probe(self, fetch):
        process = {"pid": 321, "kind": "pair-router", "startTimeTicks": 100}
        return probes.probe_pair(fetch, processes=[process], network=listener(process, 8081))

    def test_exact_allowlist(self):
        calls = []
        def fetch(url, timeout, cap):
            calls.append((url, timeout, cap))
            return {}
        for path in ("/health", "/models"):
            probes.pair_get(probes.PAIR_BASE + path, fetch=fetch)
        self.assertEqual([call[0] for call in calls], sorted(probes.PAIR_URLS))
        self.assertTrue(all(call[1] <= .35 and call[2] <= 65536 for call in calls))

    def test_forbidden_urls_raise_before_transport(self):
        values = [probes.PAIR_BASE + suffix for suffix in ("/slots", "/v1/models", "/props", "/completion", "/health/",
                  "/health?model=x", "/models#fragment", "/models?", "/models#", "/%68ealth", "/../health", "//health")]
        values += ["http://localhost:8081/health", "http://127.0.0.1:8080/health", "https://127.0.0.1:8081/health",
                   "http://127.0.0.1:08081/health", "http://user@127.0.0.1:8081/health", "http://192.0.2.95:8081/health",
                   "http://[::1]:8081/health", "HTTP://127.0.0.1:8081/health", "http://127.0.0.1:8081/health\n"]
        for url in values:
            with self.subTest(url=url), patch.object(probes, "_http_get") as transport:
                with self.assertRaises(ValueError):
                    probes.pair_get(url)
                transport.assert_not_called()

    def test_only_get_method(self):
        for method in ("POST", "HEAD", "PUT", "DELETE", "get", None):
            with self.subTest(method=method), patch.object(probes, "_http_get") as transport:
                with self.assertRaises(ValueError):
                    probes.pair_get(probes.PAIR_BASE + "/health", method=method)
                transport.assert_not_called()

    def test_invalid_probe_bounds_raise(self):
        for timeout, cap in ((0, 1), (-1, 1), (float("nan"), 1), (float("inf"), 1), (.1, 0), (.1, True)):
            with self.subTest(timeout=timeout, cap=cap), patch.object(probes, "_http_get") as transport:
                with self.assertRaises(ValueError):
                    probes.pair_get(probes.PAIR_BASE + "/health", timeout, cap)
                transport.assert_not_called()

    def test_raw_transport_also_refuses_pair_slots(self):
        with patch.object(probes.http.client, "HTTPConnection") as connection:
            with self.assertRaises(ValueError):
                probes._http_get(probes.PAIR_BASE + "/slots", .1, 1024)
            connection.assert_not_called()

    def test_models_are_local_inventory_with_unknown_activity_and_rates(self):
        calls = []
        def fetch(url, *_):
            calls.append(url)
            return {"status": "ok"} if url.endswith("/health") else {"data": [
                {"id": "loaded-model", "status": {"value": "loaded"}},
                {"id": "unloaded-model", "status": {"value": "unloaded"}},
                {"id": "loading-model", "status": {"value": "loading"}}]}
        result = self.probe(fetch)
        self.assertEqual(result["health"], "up")
        self.assertEqual([r["loadedState"] for r in result["models"]], ["loaded", "inactive", "unknown"])
        self.assertTrue(result["modelsComplete"])
        self.assertIsNone(result["liveTokensPerSecond"])
        self.assertTrue(all(r["activity"] == "unknown" and r["liveTokensPerSecond"] is None for r in result["models"]))
        self.assertEqual(calls, [probes.PAIR_BASE + "/health", probes.PAIR_BASE + "/models"])

    def test_failures_never_mean_down_or_idle(self):
        for error, reason in ((ConnectionRefusedError(), "refused"), (TimeoutError(), "timeout"),
                              (PermissionError(), "permission-denied"), (ValueError(), "invalid-response")):
            def fetch(*_):
                raise error
            result = self.probe(fetch)
            self.assertEqual((result["health"], result["status"], result["reason"]), ("unknown", "unknown", reason))
            self.assertFalse(result["modelsComplete"])
            self.assertIsNone(result["liveTokensPerSecond"])

    def test_bad_health_is_unknown(self):
        for value in (None, "ok", {}, {"status": "error"}):
            result = self.probe(lambda *_: value)
            self.assertEqual(result["reason"], "invalid-health")
            self.assertEqual(result["health"], "unknown")

    def test_bad_model_inventory_is_unknown(self):
        row = {"id": "m", "status": {"value": "loaded"}}
        for value in ({}, {"data": "wrong"}, {"data": [row, row]}, {"data": [row] * 33},
                      {"data": [{"id": "m"}]}, {"data": [None]}):
            result = self.probe(lambda url, *_: {"status": "ok"} if url.endswith("/health") else value)
            self.assertFalse(result["modelsComplete"])
            self.assertEqual(result["health"], "unknown")
            self.assertEqual(result["models"], [])

    def test_http_refuses_redirects_and_ignores_proxy_environment(self):
        with patch.object(probes.http.client, "HTTPConnection") as connection:
            instance = connection.return_value
            instance.getresponse.return_value.status = 302
            with patch.dict(os.environ, {"http_proxy": "http://invalid.example:99"}):
                with self.assertRaises(OSError):
                    probes.pair_get(probes.PAIR_BASE + "/health")
            connection.assert_called_once_with("127.0.0.1", 8081, timeout=.35)
            instance.request.assert_called_once_with("GET", "/health", headers={"Accept": "application/json"})

    def test_http_caps_response_and_rejects_nonfinite_json(self):
        for payload in (b"x" * 65537, b'{"x":NaN}', b'{"x":Infinity}', b"not json"):
            with patch.object(probes.http.client, "HTTPConnection") as connection:
                response = connection.return_value.getresponse.return_value
                response.status = 200
                response.read.return_value = payload
                with self.assertRaises(ValueError):
                    probes.pair_get(probes.PAIR_BASE + "/health")
                response.read.assert_called_once_with(65537)


class WrapperTests(unittest.TestCase):
    def process(self, **change):
        return {"pid": 123, "name": "llama-server", "exe": probes.WRAPPER_EXE,
                "startTimeTicks": 1234, "flags": {"--port": "49152", "--host": "127.0.0.1", "--alias": "fixture-model"}, **change}

    def test_unverified_executable_does_not_probe(self):
        for exe in (None, "llama-server", probes.WRAPPER_EXE + "-other", "/opt/nvpair/llama-server"):
            with patch.object(probes, "_http_get") as fetch:
                self.assertEqual(probes.probe_wrapper([self.process(exe=exe)]), [])
                fetch.assert_not_called()

    def test_unverified_endpoint_does_not_probe(self):
        for flags in ({"--port": "8081", "--host": "127.0.0.1", "--alias": "m"},
                      {"--port": "49152", "--host": "0.0.0.0", "--alias": "m"},
                      {"--port": "49152", "--alias": "m"}, {"--port": "n/a", "--host": "127.0.0.1"},
                      {"--port": "49152", "--host": "127.0.0.1"}):
            with patch.object(probes, "_http_get") as fetch:
                row = probes.probe_wrapper([self.process(flags=flags)])[0]
                self.assertEqual(row["health"], "unknown")
                fetch.assert_not_called()

    def test_verified_wrapper_only_reads_health_models_and_slots(self):
        calls = []
        def fetch(url, *_):
            calls.append(url)
            if url.endswith("/health"):
                return {"status": "ok"}
            if url.endswith("/v1/models"):
                return {"data": [{"id": "fixture-model"}]}
            return [{"id": 0, "id_task": 1, "is_processing": True, "n_decoded": 10}]
        row = probes.probe_wrapper([self.process()], fetch, network=listener(self.process(), 49152))[0]
        self.assertEqual(row["health"], "up")
        self.assertEqual(row["status"], "busy")
        self.assertIsNone(row["liveTokensPerSecond"])
        self.assertEqual(calls, ["http://127.0.0.1:49152" + p for p in ("/health", "/v1/models", "/slots")])

    def test_malformed_wrapper_health_stays_unknown(self):
        row = probes.probe_wrapper([self.process()], lambda *_: "ok", network=listener(self.process(), 49152))[0]
        self.assertEqual(row["health"], "unknown")
        self.assertNotEqual(row["reason"], "unverified-listener-owner")

    def test_normalized_process_flags_are_supported(self):
        process = self.process(flags={"port": "49152", "host": "127.0.0.1", "model": "fixture-model"})
        calls = []
        def fetch(url, *_):
            calls.append(url)
            return {"status": "ok"} if url.endswith("/health") else {"data": [{"id": "fixture-model"}]} if url.endswith("/v1/models") else [{"id": 0, "is_processing": False}]
        self.assertEqual(probes.probe_wrapper([process], fetch, network=listener(process, 49152))[0]["health"], "up")
        self.assertEqual(len(calls), 3)

    def test_process_fixture_to_wrapper_preserves_alias_identity(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as temp:
            root = Path(temp).resolve()
            directory = root / "proc/123"
            directory.mkdir(parents=True)
            (directory / "stat").write_text("123 (llama-server) S " + "0 " * 18 + "1234 0 0\n")
            (directory / "status").write_text(f"Name:\tllama-server\nUid:\t{os.getuid()} 0 0 0\n")
            (directory / "cmdline").write_text("\0".join((probes.WRAPPER_EXE, "--host", "127.0.0.1", "--port", "49152",
                                                        "--alias", "org/fixture-model")) + "\0")
            (directory / "exe").symlink_to(probes.WRAPPER_EXE)
            processes = linsys.read_processes(root)
            self.assertEqual(processes[0]["kind"], "gpu-wrapper")
            def fetch(url, *_):
                return {"status": "ok"} if url.endswith("/health") else {"data": [{"id": "org/fixture-model"}]} if url.endswith("/v1/models") else [{"id": 0, "is_processing": False}]
            result = probes.probe_wrapper(processes, fetch, network=listener(processes[0], 49152))[0]
            self.assertEqual(result["health"], "up")
            self.assertEqual(result["servedModel"], "org/fixture-model")

    def test_process_model_filename_matches_only_same_public_basename(self):
        process = self.process(flags={"port": "49152", "host": "127.0.0.1", "model": "fixture.gguf"})
        for model, health in (("/srv/storage/models/fixture.gguf", "up"), ("other.gguf", "unknown")):
            def fetch(url, *_):
                return {"status": "ok"} if url.endswith("/health") else {"data": [{"id": model}]} if url.endswith("/v1/models") else [{"id": 0, "is_processing": False}]
            result = probes.probe_wrapper([process], fetch, network=listener(process, 49152))[0]
            self.assertEqual(result["health"], health)
            self.assertNotIn("/srv/storage", result["servedModel"])


class FileProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = 1800000000

    def write(self, name, text, age=30):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (self.now - age, self.now - age))
        return path

    def test_historical_rates_split_prompt_generation_and_age(self):
        path = self.write("last-code.log", "llama_perf_context_print: prompt eval time = 1 ms / 1 tokens (1 ms per token, 80.83 tokens per second)\n"
                          "llama_perf_context_print: eval time = 2 ms / 1 runs (2 ms per token, 31.06 tokens per second)\n")
        result = probes.last_rates(path, self.now)
        self.assertEqual(result["kind"], "last-measured")
        self.assertEqual(result["generationTokensPerSecond"], 31.06)
        self.assertEqual(result["promptTokensPerSecond"], 80.83)
        self.assertEqual(result["ageSeconds"], 30)
        self.assertIsNone(result["model"])
        self.assertEqual(result["expectedModel"], "Qwen3-8B-Q4_K_M")
        self.assertEqual(result["modelLabelSource"], "role-map-unverified")

    def test_last_match_wins_from_bounded_tail(self):
        path = self.write("last-extract.log", "x" * (probes.LOG_MAX_BYTES + 100) + "\neval time = x (1 ms per token, 9.5 tokens per second)\n")
        result = probes.last_rates(path, self.now)
        self.assertEqual(result["generationTokensPerSecond"], 9.5)
        self.assertIsNone(result["promptTokensPerSecond"])

    def test_missing_or_malformed_log_is_unknown(self):
        for path in (self.root / "last-code.log", self.write("last-extract.log", "no timing")):
            result = probes.last_rates(path, self.now)
            self.assertEqual(result["kind"], "unknown")
            self.assertIsNone(result["generationTokensPerSecond"])

    def test_future_log_is_unknown(self):
        path = self.write("last-code.log", "eval time = x (1 ms per token, 9.5 tokens per second)", age=-10)
        self.assertEqual(probes.last_rates(path, self.now)["reason"], "clock-invalid")

    def test_symlink_file_and_parent_are_refused(self):
        actual = self.write("actual/last-code.log", "eval time = x (1 ms per token, 9.5 tokens per second)")
        (self.root / "last-code.log").symlink_to(actual)
        (self.root / "linked").symlink_to(actual.parent, target_is_directory=True)
        for path in (self.root / "last-code.log", self.root / "linked/last-code.log"):
            self.assertIsNone(probes._record(path, 65536))
            self.assertEqual(probes.last_rates(path, self.now)["kind"], "unknown")

    def test_nofollow_on_every_open_and_readonly(self):
        path = self.write("last-code.log", "record")
        real_open = os.open
        calls = []
        def opened(path, flags, *args, **kwargs):
            calls.append(flags)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(os, "open", side_effect=opened):
            self.assertEqual(probes._record(path, 100)[0], b"record")
        self.assertTrue(calls)
        self.assertTrue(all(flags & os.O_NOFOLLOW for flags in calls))
        self.assertTrue(all(not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) for flags in calls))

    def test_non_regular_and_oversized_journal_refused(self):
        self.assertIsNone(probes._record(self.root, 10))
        path = self.write("large.json", "x" * 11)
        self.assertIsNone(probes._record(path, 10))
        self.assertIsNone(probes._record(Path("relative.json"), 10))

    def test_codex_journals_project_only_safe_keys_with_age(self):
        self.write("codemode-router/scheduler-authority/epochs/00000007.json", json.dumps({"epoch": 7, "prompt": "do not publish", "apiKey": "fixture"}))
        self.write("codemode-router/install-fence.json", json.dumps({"generation": "85adaf74", "state": "installed"}))
        result = probes.read_codex_state(self.root, self.now)
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(result["authority"]["epochs"], {"epoch": 7, "observedUnix": self.now - 30, "ageSeconds": 30})
        self.assertEqual(result["qwen35"]["state"], "staged")
        self.assertIsNone(result["qwen35"]["serving"])
        self.assertNotIn("do not publish", json.dumps(result))
        self.assertEqual(probes.read_codex_state(self.root / "codemode-router", self.now), result)

    def test_codex_missing_and_linked_journals_are_unknown(self):
        self.assertEqual(probes.read_codex_state(self.root, self.now)["state"], "unknown")

    def test_codex_scans_directory_descriptors_and_refuses_symlinks(self):
        directory = self.root / "epochs"
        directory.mkdir()
        self.write("epochs/00000007.json", '{"epoch":7}')
        real_scan = os.scandir
        with patch.object(os, "scandir", wraps=real_scan) as scan:
            self.assertEqual(probes._journal_names(directory), ["00000007.json"])
            self.assertIs(type(scan.call_args.args[0]), int)
        (self.root / "linked-epochs").symlink_to(directory, target_is_directory=True)
        with patch.object(os, "scandir") as scan:
            self.assertEqual(probes._journal_names(self.root / "linked-epochs"), [])
            scan.assert_not_called()
        actual = self.write("actual/install-fence.json", '{"state":"installed"}')
        (self.root / "codemode-router").symlink_to(actual.parent, target_is_directory=True)
        self.assertEqual(probes.read_codex_state(self.root, self.now)["state"], "unknown")

    def test_codex_scan_is_bounded(self):
        for index in range(probes.STATE_SCAN_LIMIT + 1):
            self.write(f"codemode-router/scheduler-authority/epochs/{index:08}.json", '{"epoch":7}')
        self.assertIsNone(probes.read_codex_state(self.root, self.now)["authority"]["epochs"])

    def test_coordinator_unobserved_never_idle_or_absent(self):
        self.assertEqual(probes.coordinator_status([])["state"], "unknown")
        self.assertEqual(probes.coordinator_status(None)["state"], "unknown")
        self.assertEqual(probes.coordinator_status([{"name": "python3", "coordinator": True}])["state"], "unknown")

    def test_pair_members_are_attributed_vendor_records_with_age(self):
        self.write(".config/Nvidia Corporation/Personal AI Router/cluster/members.json", json.dumps({"members": [
            {"id": "node-fixture", "name": "fixture-linux", "state": "member", "lastSeen": "2026-10-17T00:00:00Z",
             "prompt": "do not publish", "engineHealthy": True, "token": "fixture"}]}))
        result = probes.read_pair_files(self.root, self.now)
        self.assertEqual(result["attribution"], "PAIR says")
        self.assertEqual(result["state"], "recorded")
        self.assertEqual(result["ageSeconds"], 30)
        self.assertFalse(result["pathConfirmed"])
        self.assertEqual(result["members"], [{"id": "node-fixture", "name": "fixture-linux", "state": "member",
                                               "lastSeen": "2026-10-17T00:00:00Z"}])
        self.assertNotIn("engineHealthy", json.dumps(result))

    def test_pair_member_missing_invalid_and_oversized_are_unknown(self):
        self.assertEqual(probes.read_pair_files(self.root, self.now)["state"], "unknown")
        for payload in ('{"members":{}}', '{"members":[null]}', '{"members":[{"id":"a"},{"id":"a"}]}',
                        '{"members":[{"id":"a/b"}]}', 'x' * (probes.HTTP_MAX_BYTES + 1), '{"members":NaN}'):
            self.write(".config/Nvidia Corporation/Personal AI Router/cluster/members.json", payload)
            result = probes.read_pair_files(self.root, self.now)
            self.assertEqual(result["state"], "unknown")
            self.assertFalse(result["membersComplete"])

    def test_pair_member_symlink_refused(self):
        actual = self.write("actual.json", '{"members":[]}')
        link = self.root / ".config/Nvidia Corporation/Personal AI Router/cluster/members.json"
        link.parent.mkdir(parents=True)
        link.symlink_to(actual)
        self.assertEqual(probes.read_pair_files(self.root, self.now)["state"], "unknown")


if __name__ == "__main__":
    unittest.main()
