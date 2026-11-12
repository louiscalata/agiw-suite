"""Registry regressions; network cases use ephemeral loopback HTTP servers only."""
import contextlib
import copy
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from inference_collect import ProbeError, collect_map
from inference_stats import MAP_SCHEMA, expire_map, public_map
import remote_machines as remote

ROOT = Path(__file__).resolve().parent


def config(port=21001, **lane):
    return {"schemaVersion": 1, "machines": [{"id": "pc", "label": "Worker placeholder", "lanes": [
        {"id": "deep", "label": "Deep", "localPort": port, **lane}]}]}


def fetch(url):
    return 200, '{"data":[{"id":"server/current-model"}]}', 12.5


@contextlib.contextmanager
def endpoint(delay=0, body=b'{"data":[{"id":"observed/model"}]}', status=200, tracker=None):
    state = {"delay": delay, "body": body, "status": status, "calls": [], "active": 0, "peak": 0}
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            with lock:
                state["calls"].append(self.path)
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            if tracker is not None:
                with tracker["lock"]:
                    tracker["active"] += 1
                    tracker["peak"] = max(tracker["peak"], tracker["active"])
            try:
                time.sleep(state["delay"])
                raw = state["body"]
                self.send_response(state["status"])
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    state["active"] -= 1
                if tracker is not None:
                    with tracker["lock"]:
                        tracker["active"] -= 1
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError("probe result was not published before deadline")


def registry_lane(document):
    return next(node for node in document["nodes"] if node["id"] == "registry:pc:deep")


class RegistryTests(unittest.TestCase):
    def test_protected_ports_rejected_by_name_before_any_probe(self):
        for port in (8081, 1234, 1235, 8080, 11434, 18082, 8768, 8769):
            with self.subTest(port=port), self.assertRaisesRegex(ValueError, str(port)):
                remote.validate_config(config(port))
            probe = mock.Mock(side_effect=AssertionError("protected service contacted"))
            with self.subTest(port=port), self.assertRaises(ValueError):
                remote.collect(config(port), fetch=probe, now=1000)
            probe.assert_not_called()

    def test_only_bounded_numeric_ports_and_no_connection_fields(self):
        for port in (0, 1, 1023, 65536, True, "21001", None):
            with self.subTest(port=port), self.assertRaises(ValueError):
                remote.validate_config(config(port))
        for port in (1024, 21001, 65535):
            remote.validate_config(config(port))
        for key, value in (("host", "127.0.0.2"), ("base", "http://remote.invalid"),
                           ("expectedModel", "alias")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                remote.validate_config(config(**{key: value}))

    def test_timeout_and_capability_fields_validated_and_passed_untouched(self):
        for timeout in (0, -1, 6.01, True, "6", float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                remote.validate_config(config(timeoutSeconds=timeout))
        for key, values in (("contextTokens", (-1, True, 1.5, "4096", None)),
                            ("thinking", (True, None, "configurable", "ON"))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    remote.validate_config(config(**{key: value}))
        for thinking in ("on", "off", "unknown"):
            value = config(timeoutSeconds=6, contextTokens=4096, thinking=thinking)
            original = copy.deepcopy(value)
            target = remote.lane_targets(value)[0]
            lane = remote.collect(value, fetch=fetch, now=1000)[1]
            for row in (target, lane):
                self.assertEqual((row["contextTokens"], row["thinking"]), (4096, thinking))
            self.assertEqual(value, original)
        self.assertEqual(remote.lane_targets(config())[0]["timeoutSeconds"], 1.5)

    def test_duplicate_keys_and_nonprintable_model_identity_rejected(self):
        bodies = ['{"data":[{"id":"first","id":"second"}]}',
                  '{"data":[],"data":[{"id":"second"}]}',
                  json.dumps({"data": [{"id": "a\u200bb"}]}),
                  json.dumps({"data": [{"id": "a\ud800b"}]}),
                  json.dumps({"data": [{"id": "x" * 1025}]}),
                  'null', '{"data":[]}', '{"data":[{},{}]}']
        for body in bodies:
            with self.subTest(body=body):
                lane = remote.collect(config(), fetch=lambda url: (200, body, 1), now=1000)[1]
                self.assertEqual(lane["health"], "not_measured")
                self.assertIsNone(lane["modelId"])
                self.assertEqual(lane["reason"], "identity_unknown")

    def test_failure_reason_clears_previous_identity(self):
        self.assertEqual(remote.collect(config(), fetch=fetch, now=1000)[1]["health"], "ok")
        for failure in (ProbeError("timeout"), ProbeError("incomplete_body")):
            def failed(url):
                raise failure
            lane = remote.collect(config(), fetch=failed, now=1001)[1]
            self.assertIsNone(lane["modelId"])
            self.assertEqual(lane["reason"], failure.reason)

    def test_namespace_keeps_legacy_rows_unless_explicit_replacement(self):
        from inference_collect import closed_targets
        local = {"id": "mac:lmstudio", "machine": "mac", "role": "lmstudio", "label": "Local",
                 "health": "not_measured", "reason": "not_observed"}
        with endpoint() as (port, _), remote.BackgroundProbes() as probes:
            for replacement in (False, True):
                value = config(port)
                if replacement:
                    value["machines"][0]["replaces"] = "legacy-pc"
                document = collect_map("mac", remote_config=value, remote_probes=probes,
                                       fetch=fetch, now=time.time(), targets=closed_targets(),
                                       lmstudio=local, veritas_url="")
                ids = [node["id"] for node in document["nodes"]]
                self.assertIn("registry:pc", ids)
                self.assertIn("registry:pc:deep", ids)
                self.assertEqual("pc:router" in ids, not replacement)
                self.assertEqual("pc" in ids, not replacement)
                self.assertEqual(len(ids), len(set(ids)))
                self.assertIn({"from": "registry:pc", "to": "registry:pc:deep", "kind": "hosts"}, document["edges"])
        for marker in (True, "pc", "legacy-mac"):
            value = config()
            value["machines"][0]["replaces"] = marker
            with self.assertRaises(ValueError):
                remote.validate_config(value)

    def test_invalid_config_is_off_and_preserves_legacy_rows(self):
        from inference_collect import closed_targets
        doc = collect_map("mac", remote_config={"error": "config_invalid"}, fetch=fetch,
                          targets=closed_targets(), veritas_url="", now=1000)
        self.assertIn("pc:router", [node["id"] for node in doc["nodes"]])
        self.assertEqual(doc["remoteMachines"]["state"], "invalid")

    def test_maximum_registry_export_and_invalid_bounds(self):
        value = {"schemaVersion": 1, "machines": [
            {"id": f"worker-{i}", "label": f"Worker {i}", "lanes": [
                {"id": f"lane-{j}", "label": f"Lane {j}", "localPort": 22000 + i * 2 + j}
                for j in range(2)]} for i in range(32)]}
        nodes = remote.collect(value, fetch=fetch, now=1000)
        self.assertEqual(len(nodes), 96)
        self.assertIsNotNone(public_map({"schema": MAP_SCHEMA, "nodes": nodes, "edges": []}))
        self.assertIsNone(public_map({"schema": MAP_SCHEMA, "nodes": nodes * 2, "edges": []}))
        value["machines"].append(value["machines"][0])
        with self.assertRaises(ValueError):
            remote.validate_config(value)


class ConfigSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "registry.json"
        self.path.write_text(json.dumps(config()))
        self.path.chmod(0o600)

    def test_regular_owned_private_file_and_optional_missing_default(self):
        self.assertEqual(remote.load_config(self.path, environ={}), config())
        with mock.patch.object(Path, "home", return_value=Path(self.temp.name)):
            self.assertIsNone(remote.load_config(environ={}))

    def test_symlink_and_fifo_fail_closed_without_stall(self):
        link = self.path.with_name("link.json")
        link.symlink_to(self.path)
        self.assertEqual(remote.load_config(link), {"error": "config_invalid"})
        fifo = self.path.with_name("pipe.json")
        os.mkfifo(fifo, 0o600)
        # A daemon bounds even the defective implementation's FIFO open.
        result = []
        thread = threading.Thread(target=lambda: result.append(remote.load_config(fifo)), daemon=True)
        thread.start()
        thread.join(.3)
        if thread.is_alive():
            writer = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            os.close(writer)
            self.fail("FIFO open stalled instead of disabling registry")
        self.assertEqual(result, [{"error": "config_invalid"}])

    def test_open_flags_descriptor_checks_and_size_before_read(self):
        real_open = os.open
        with mock.patch.object(remote.os, "open", wraps=real_open) as opened:
            remote.load_config(self.path)
        flags = opened.call_args.args[1]
        self.assertTrue(flags & os.O_NOFOLLOW)
        self.assertTrue(flags & os.O_NONBLOCK)
        for mode in (0o620, 0o602, 0o666):
            self.path.chmod(mode)
            with self.subTest(mode=mode):
                self.assertEqual(remote.load_config(self.path), {"error": "config_invalid"})
        self.path.chmod(0o600)
        actual = os.stat(self.path)
        wrong_owner = list(actual)
        wrong_owner[stat.ST_UID] = os.getuid() + 1
        with mock.patch.object(remote.os, "fstat", return_value=os.stat_result(wrong_owner)):
            self.assertEqual(remote.load_config(self.path), {"error": "config_invalid"})
        self.path.write_bytes(b" " * 65537)
        with mock.patch.object(remote.os, "read", side_effect=AssertionError("oversize read")):
            self.assertEqual(remote.load_config(self.path), {"error": "config_invalid"})

    def test_huge_numeric_timeout_disables_registry_without_raising(self):
        self.path.write_text(json.dumps(config(timeoutSeconds=10 ** 1000)))
        with self.assertLogs(remote.__name__, level=logging.WARNING) as logs:
            self.assertEqual(remote.load_config(self.path), {"error": "config_invalid"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("timeoutSeconds", logs.output[0])

    def test_invalid_config_logs_one_reason_even_when_resampled(self):
        self.path.write_text("invalid")
        with self.assertLogs(remote.__name__, level=logging.WARNING) as logs:
            for _ in range(3):
                self.assertEqual(remote.load_config(self.path), {"error": "config_invalid"})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("json_invalid", logs.output[0])


class BackgroundTests(unittest.TestCase):
    def test_snapshot_is_nonblocking_and_slow_router_can_complete(self):
        with endpoint(delay=4.2) as (port, state), remote.BackgroundProbes() as probes:
            started = time.monotonic()
            doc = collect_map("mac", remote_config=config(port, timeoutSeconds=6), remote_probes=probes,
                              targets=[], lmstudio={"id": "mac:lmstudio", "machine": "mac", "role": "lmstudio",
                                  "label": "Local", "health": "not_measured"}, veritas_url="", now=time.time())
            self.assertLess(time.monotonic() - started, .25)
            self.assertIsNone(registry_lane(doc)["modelId"])
            value = config(port, timeoutSeconds=6)
            lane = wait_for(lambda: next((n for n in probes.snapshot(value) if n["role"] == "llama"
                                         and n["health"] == "ok"), None), timeout=5.5)
            self.assertEqual(lane["modelId"], "observed/model")
            self.assertLess(lane["ageSeconds"], .2)
            self.assertTrue(state["calls"])
            self.assertEqual(set(state["calls"]), {"/v1/models"})

    def test_failure_and_expiry_clear_identity_at_snapshot_boundary(self):
        with endpoint() as (port, state), remote.BackgroundProbes(interval=.1) as probes:
            value = config(port)
            probes.snapshot(value)
            wait_for(lambda: any(n["health"] == "ok" and n["role"] == "llama" for n in probes.snapshot(value)))
            state["status"] = 503
            failed = wait_for(lambda: next((n for n in probes.snapshot(value) if n.get("reason") == "upstream_error"), None))
            self.assertIsNone(failed["modelId"])
            state["status"] = 200
            good = wait_for(lambda: next((n for n in probes.snapshot(value) if n["role"] == "llama"
                                         and n["health"] == "ok"), None))
            probes.close()
            doc = {"schema": MAP_SCHEMA, "observedAt": good["observedAt"], "nodes": [good], "edges": []}
            expire_map(doc, time.time() + .3)
            self.assertEqual(good["reason"], "stale")
            self.assertIsNone(good["modelId"])

    def test_bounded_pool_does_not_starve_queued_fast_lane_or_snapshots(self):
        with contextlib.ExitStack() as stack:
            tracker = {"lock": threading.Lock(), "active": 0, "peak": 0}
            servers = [stack.enter_context(endpoint(delay=.2 if i < 8 else 0, tracker=tracker)) for i in range(9)]
            value = {"schemaVersion": 1, "machines": [
                {"id": f"w{i}", "label": "Worker", "lanes": [{"id": "lane", "label": "Lane", "localPort": port}]}
                for i, (port, _) in enumerate(servers)]}
            probes = stack.enter_context(remote.BackgroundProbes(interval=1))
            started = time.monotonic()
            for _ in range(10):
                nodes = probes.snapshot(value)
            self.assertLess(time.monotonic() - started, .2)
            wait_for(lambda: any(n["id"] == "registry:w8:lane" and n["health"] == "ok"
                                 for n in probes.snapshot(value)))
            self.assertGreaterEqual(tracker["peak"], 2)
            self.assertLessEqual(tracker["peak"], 8)
            self.assertTrue(all(set(state["calls"]) == {"/v1/models"} for _, state in servers))

    def test_removed_registry_stops_probes_and_readmission_starts_unknown(self):
        with endpoint() as (port, state), remote.BackgroundProbes(interval=.1) as probes:
            value = config(port)
            def snapshot(selected):
                return collect_map("mac", remote_config=selected, remote_probes=probes,
                    targets=[], lmstudio={"id": "mac:lmstudio", "machine": "mac", "role": "lmstudio",
                    "label": "Local", "health": "not_measured"}, veritas_url="")
            snapshot(value)
            wait_for(lambda: registry_lane(snapshot(value))["health"] == "ok")
            snapshot(None)
            time.sleep(.05)  # let any request already in flight finish
            count = len(state["calls"])
            time.sleep(.25)
            self.assertEqual(len(state["calls"]), count)
            lane = registry_lane(snapshot(value))
            self.assertEqual(lane["reason"], "pending")
            self.assertIsNone(lane["modelId"])

    def test_config_change_does_not_reuse_identity_or_late_result(self):
        with endpoint(delay=.15) as (port, _), remote.BackgroundProbes(interval=.1) as probes:
            value = config(port)
            probes.snapshot(value)
            time.sleep(.03)
            changed = config(port, thinking="unknown")
            self.assertTrue(all(n["modelId"] is None for n in probes.snapshot(changed)))
            self.assertEqual(probes.snapshot({"error": "config_invalid"}), [])
            time.sleep(.2)
            self.assertTrue(all(n["modelId"] is None for n in probes.snapshot(value)))


class SamplerTests(unittest.TestCase):
    def test_registry_does_not_bypass_legacy_tunnel_unless_replacement(self):
        import server
        from test_server import MemoryGuardSampleTest
        helper = MemoryGuardSampleTest()
        for replacement in (False, True):
            value = config()
            if replacement:
                value["machines"][0]["replaces"] = "legacy-pc"
            with mock.patch.object(server.remote_machines, "load_config", return_value=value), \
                 mock.patch.object(server, "tunnel_targets", return_value=({"state": "unknown"}, ["legacy"])) as tunnel, \
                 mock.patch.object(server, "model_control_capability", return_value={}), \
                 mock.patch.object(server, "attach") as attach:
                self.assertIsInstance(helper.run_sampler(helper.FakeGuard()), dict)
            self.assertEqual(tunnel.call_count, 0 if replacement else 1)
            self.assertEqual(attach.call_args.kwargs["targets"], [] if replacement else ["legacy"])


from test_registry_transport import RegistryTransportTests
from test_feature_documentation import FeatureDocumentationTests


if __name__ == "__main__":
    unittest.main()
