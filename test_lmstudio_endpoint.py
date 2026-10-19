"""A4 endpoint provenance and bounded loopback inventory regressions."""
import contextlib
import http.server
import io
import os
import threading
import time
import unittest
from unittest import mock

import telemetry
from server import SnapshotStore


_UNCHANGED = object()
_LM_LISTENER = (41, "lmstudio", "/Applications/LM Studio.app/Contents/MacOS/LM Studio")
_PAIR_LISTENER = (52, "pair", "/Applications/NVIDIA PAIR.app/Contents/MacOS/nvpair-proxy")


class EndpointEvidenceTests(unittest.TestCase):
    def collect(self, status, owner, api, port=None, after=_UNCHANGED, cli=_UNCHANGED):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(os.environ))
            os.environ.pop(telemetry._API_PORT_ENV, None)
            if port is not None:
                os.environ[telemetry._API_PORT_ENV] = str(port)
            stack.enter_context(mock.patch.object(telemetry, "_lms_server_status", return_value=status))
            stack.enter_context(mock.patch.object(telemetry, "_pair_snapshot", return_value=None))
            owners = stack.enter_context(mock.patch.object(telemetry, "_inventory_listener", create=True,
                                                          side_effect=[owner, owner if after is _UNCHANGED else after]))
            http = stack.enter_context(mock.patch.object(telemetry, "_bounded_http", return_value=api))
            stack.enter_context(mock.patch.object(telemetry, "_bounded_lms",
                **({"side_effect": OSError()} if cli is _UNCHANGED else {"return_value": cli})))
            stack.enter_context(mock.patch.object(telemetry, "_windows_worker", return_value=(
                {"state": "unavailable"}, {"id": "windows-worker", "state": "unavailable"})))
            stack.enter_context(mock.patch.object(telemetry.peer_feed, "windows_feed", return_value=(
                None, {"id": "windows-telemetry", "state": "unavailable"})))
            for name, value in (
                    ("observe_router", {}), ("_pipeline", ({}, {"id": "router", "state": "unavailable"})),
                    ("_online_code_mode", {}), ("_mark_live_route", None),
                    ("_afm_status", {"state": "missing"}), ("_windows_headless", {}),
                    ("_windows_jobs", ({}, {"id": "windows-jobs", "state": "unavailable"})),
                    ("_canary", ({}, {"id": "canary", "state": "unavailable"})),
                    ("collect_nisi_v02", ({"runtimeIntegrity": "VERIFIED", "hostBinding": "VERIFIED"},
                                          {"id": "nisi-v02-runtime", "state": "live"})),
                    ("_last_jev_judgment", (None, None))):
                stack.enter_context(mock.patch.object(telemetry, name, return_value=value))
            snapshot = telemetry.collect_snapshot()
        source = next(row for row in snapshot["sources"] if row["id"] == "lmstudio-api")
        return snapshot, source, http, owners

    def test_unknown_owner_never_supplies_inventory_or_a_known_zero(self):
        for status, port in ((None, None), ({"port": 1235, "running": True}, 1236)):
            for models in ([], [{"key": "false/mac-model", "loaded_instances": [{"id": "x"}]}]):
                with self.subTest(status=status, models=models):
                    snapshot, source, http, _ = self.collect(status, None, {"models": models}, port)
                    self.assertNotEqual(source["state"], "live")
                    self.assertEqual(source["route"], "unknown")
                    self.assertEqual(snapshot["models"], [])
                    http.assert_not_called()

    def test_stopped_or_unknown_running_flag_never_proves_direct(self):
        for running in (False, None):
            with self.subTest(running=running):
                snapshot, source, http, _ = self.collect({"port": 1235, "running": running},
                    _LM_LISTENER, {"models": [{"key": "false/model", "loaded_instances": []}]})
                self.assertEqual(source["route"], "unknown")
                self.assertNotEqual(source["state"], "live")
                self.assertEqual(snapshot["models"], [])
                http.assert_not_called()

    def test_running_status_is_refreshed_even_inside_the_old_cache_window(self):
        saved = dict(telemetry._LMS_STATUS_CACHE)
        self.addCleanup(telemetry._LMS_STATUS_CACHE.update, saved)
        telemetry._LMS_STATUS_CACHE.update(nextCheck=float("-inf"), value=None)
        with mock.patch.object(telemetry, "_bounded_lms_json", side_effect=[
                {"running": True, "port": 1235}, {"running": False, "port": 1235}]) as read:
            self.assertTrue(telemetry._lms_server_status()["running"])
            self.assertFalse(telemetry._lms_server_status()["running"])
            self.assertEqual(read.call_count, 2)

    def test_port_handoff_during_inventory_read_discards_the_response(self):
        for after in (None, (42, "lmstudio", _LM_LISTENER[2]), (41, "pair", _PAIR_LISTENER[2])):
            with self.subTest(after=after):
                snapshot, source, _, owners = self.collect({"port": 1235, "running": True},
                    _LM_LISTENER, {"models": [{"key": "old/model", "loaded_instances": []}]}, after=after)
                self.assertEqual(source["route"], "unknown")
                self.assertNotEqual(source["state"], "live")
                self.assertEqual(snapshot["models"], [])
                self.assertEqual(owners.call_count, 2)

    def test_local_pair_listener_is_attributed_without_claiming_mac_residency(self):
        snapshot, source, http, _ = self.collect({"port": 1235, "running": True},
            _PAIR_LISTENER, {"models": [{"key": "remote/model", "loaded_instances": [{"id": "x"}]}]}, 1234)
        self.assertEqual((source["route"], source["via"]), ("via-pair", "NVIDIA PAIR"))
        self.assertIn("via NVIDIA PAIR", source["label"])
        self.assertEqual(source["state"], "recorded")
        self.assertEqual(snapshot["models"], [])
        http.assert_called_once()

    def test_api_inventory_over_limit_is_rejected_instead_of_truncated(self):
        models = [{"key": f"local/model-{i}", "loaded_instances": []} for i in range(256)]
        models.append({"key": "local/loaded-model", "loaded_instances": [{"id": "loaded"}]})
        with self.assertRaisesRegex(ValueError, "inventory incomplete"):
            telemetry._parse_api({"models": models}, time.time())

    def test_oversize_direct_inventory_cannot_establish_a_known_zero(self):
        models = [{"key": f"local/model-{i}", "loaded_instances": []} for i in range(256)]
        models.append({"key": "local/loaded-model", "loaded_instances": [{"id": "loaded"}]})
        snapshot, source, http, _ = self.collect({"port": 1235, "running": True},
            _LM_LISTENER, {"models": models})
        self.assertEqual(source["state"], "error")
        self.assertIn("inventory incomplete", source["detail"])
        self.assertIn("this source cannot establish the loaded count", source["detail"])
        self.assertEqual(snapshot["models"], [])
        self.assertFalse(any(row["state"] == "live" for row in snapshot["sources"]
                             if row["id"] in ("lmstudio-api", "lms-ps")))
        http.assert_called_once()

    def test_complete_empty_and_limit_inventories_remain_live(self):
        for count in (0, 256):
            with self.subTest(count=count):
                models = [{"key": f"local/model-{i}", "loaded_instances": []} for i in range(count)]
                if models:
                    models[-1]["loaded_instances"] = [{"id": "loaded"}]
                snapshot, source, _, _ = self.collect({"port": 1235, "running": True},
                    _LM_LISTENER, {"models": models})
                self.assertEqual(source["state"], "live")
                self.assertEqual(len(snapshot["models"]), count)
                self.assertEqual(sum(row["loaded"] for row in snapshot["models"]), int(count > 0))

    def assert_unknown_counts(self, snapshot):
        store = SnapshotStore()
        store.publish(snapshot)
        published = store.read()
        self.assertFalse(published["activityKnown"])
        self.assertIsNone(published["history"][-1]["loaded"])
        self.assertIsNone(published["history"][-1]["active"])

    def test_api_invalid_rows_cannot_establish_a_complete_inventory(self):
        good = {"key": "local/model", "loaded_instances": []}
        for bad in (None, {}, {"key": "bad\nmodel", "loaded_instances": [{"id": "loaded"}]},
                    {"key": "local/model", "loaded_instances": [{"id": "duplicate"}]}):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "inventory incomplete"):
                    telemetry._parse_api({"models": [good, bad]}, time.time())

    def test_cli_invalid_and_oversize_lists_cannot_establish_complete_activity(self):
        good = {"identifier": "local/model", "status": "idle"}
        lists = [[good, bad] for bad in (None, {}, {"identifier": "bad\nmodel"}, good)]
        lists.append([{"identifier": f"local/model-{i}"} for i in range(257)])
        for rows in lists:
            with self.subTest(rows=len(rows), bad=rows[-1]):
                with self.assertRaisesRegex(ValueError, "inventory incomplete"):
                    telemetry._parse_lms({"models": rows}, time.time())

    def test_invalid_api_inventory_keeps_loaded_and_activity_counts_unknown(self):
        snapshot, source, _, _ = self.collect({"port": 1235, "running": True},
            _LM_LISTENER, {"models": [{"key": "bad\nmodel", "loaded_instances": [{"id": "loaded"}]}]})
        self.assertEqual(source["state"], "error")
        self.assertIn("inventory incomplete", source["detail"])
        self.assertEqual(snapshot["models"], [])
        self.assert_unknown_counts(snapshot)

    def test_invalid_cli_inventory_cannot_publish_quiet_or_zero_loaded(self):
        for rows in ([{"identifier": "bad\nmodel", "status": "idle"}],
                     [{"identifier": f"local/model-{i}", "status": "idle"} for i in range(257)]):
            with self.subTest(count=len(rows)):
                snapshot, _, _, _ = self.collect(None, None, {"models": []}, cli={"models": rows})
                source = next(row for row in snapshot["sources"] if row["id"] == "lms-ps")
                self.assertEqual(source["state"], "error")
                self.assertIn("inventory incomplete", source["detail"])
                self.assertEqual(snapshot["models"], [])
                self.assert_unknown_counts(snapshot)

    def test_combined_inventory_over_limit_cannot_hide_a_loaded_cli_model(self):
        api = {"models": [{"key": f"local/model-{i}", "loaded_instances": []} for i in range(256)]}
        cli = {"models": [{"identifier": "local/new-model", "status": "idle"}]}
        snapshot, _, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER, api, cli=cli)
        for source in snapshot["sources"]:
            if source["id"] in ("lmstudio-api", "lms-ps"):
                self.assertEqual(source["state"], "error")
                self.assertIn("combined inventory incomplete", source["detail"])
                self.assertIn("this source cannot establish the loaded count", source["detail"])
        self.assertEqual(len(snapshot["models"]), 256)
        self.assertFalse(snapshot["machines"][0]["modelsComplete"])
        self.assertTrue(all(row["loaded"] is False for row in snapshot["models"]))
        self.assert_unknown_counts(snapshot)

    def test_complete_merged_empty_and_limit_inventories_keep_exact_counts(self):
        for api_count, cli_id in ((0, None), (255, "local/new-model"), (256, "local/model-255")):
            with self.subTest(api_count=api_count, cli_id=cli_id):
                api = {"models": [{"key": f"local/model-{i}", "loaded_instances": []}
                                  for i in range(api_count)]}
                if cli_id and any(row["key"] == cli_id for row in api["models"]):
                    next(row for row in api["models"] if row["key"] == cli_id)["loaded_instances"] = [{"id":cli_id}]
                cli = {"models": [{"identifier": cli_id, "status": "idle"}] if cli_id else []}
                snapshot, _, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER, api, cli=cli)
                self.assertEqual(len(snapshot["models"]), 256 if api_count else 0)
                store = SnapshotStore()
                store.publish(snapshot)
                published = store.read()
                self.assertTrue(published["activityKnown"])
                self.assertEqual(published["history"][-1]["loaded"], int(cli_id is not None))
                self.assertEqual(published["history"][-1]["active"], 0)

    def test_complete_cli_inventory_survives_rejected_api_inventory(self):
        snapshot, source, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER,
            {"models": [{"key": "bad\nmodel"}]},
            cli={"models": [{"identifier": "local/model", "status": "generating"}]})
        self.assertEqual(source["state"], "error")
        store = SnapshotStore()
        store.publish(snapshot)
        published = store.read()
        self.assertFalse(published["activityKnown"])
        self.assertIsNone(published["history"][-1]["loaded"])
        self.assertIsNone(published["history"][-1]["active"])
        self.assertTrue(any(row["state"]=="generating" for row in published["models"]))

    def test_transient_status_errors_retry_but_missing_cli_backs_off(self):
        for error in (TimeoutError(), ValueError(), OSError(), FileNotFoundError()):
            with self.subTest(error=type(error).__name__), \
                    mock.patch.dict(telemetry._LMS_STATUS_CACHE, nextCheck=float("-inf"), value=None), \
                    mock.patch.object(telemetry.time, "monotonic", return_value=1000.0), \
                    mock.patch.object(telemetry, "_bounded_lms_json", side_effect=[
                        error, {"running": True, "port": 1235}]) as read:
                self.assertIsNone(telemetry._lms_server_status())
                next_status = telemetry._lms_server_status()
                if isinstance(error, FileNotFoundError):
                    self.assertIsNone(next_status)
                    self.assertEqual(read.call_count, 1)
                else:
                    self.assertEqual(next_status, {"running": True, "port": 1235})
                    self.assertEqual(read.call_count, 2)

    def test_unknown_status_does_not_probe_default_or_explicit_proxy(self):
        for port in (None, 1234, 1235):
            with self.subTest(port=port):
                snapshot, source, http, owners = self.collect(None, _PAIR_LISTENER, {"models": []}, port)
                self.assertEqual(source["route"], "unknown")
                http.assert_not_called()
                owners.assert_not_called()
                self.assert_unknown_counts(snapshot)

    def test_only_verified_live_direct_inventory_exports_listener_identity(self):
        listener = (41, "lmstudio", "/Applications/LM Studio.app/Contents/MacOS/LM Studio")
        _, source, _, _ = self.collect({"port": 1235, "running": True}, listener, {"models": []})
        self.assertEqual(source["verifiedListener"],
                         {"pid": listener[0], "kind": listener[1], "executable": listener[2]})
        for api, after, port in (({"models": []}, None, None),
                                  ({"models": [{}]}, listener, None),
                                  ({"models": []}, (52, "pair", "/fixture/pair"), 1234)):
            owner = (52, "pair", "/fixture/pair") if port else listener
            _, rejected, _, _ = self.collect({"port": 1235, "running": True}, owner, api,
                                              port=port, after=after)
            self.assertNotIn("verifiedListener", rejected)

    def test_nisi_cannot_infer_a_shortfall_from_incomplete_inventory(self):
        cases = [
            (None, None, {"models": []}, _UNCHANGED),
            ({"port": 1235, "running": True}, _PAIR_LISTENER, {"models": []}, _UNCHANGED),
            ({"port": 1235, "running": True}, _LM_LISTENER,
             {"models": [{"key": "one", "loaded_instances": [{"id": "one"}]}, {"key": "two"}]}, _UNCHANGED),
            ({"port": 1235, "running": True}, _LM_LISTENER,
             {"models": [{"key": "unknown"}]}, {"models": []}),
            ({"port": 1235, "running": True}, _LM_LISTENER,
             {"models": [{"key": f"model-{i}", "loaded_instances": []} for i in range(256)]},
             {"models": [{"identifier": "extra", "status": "idle"}]})]
        for status, owner, api, cli in cases:
            with self.subTest(api_count=len(api["models"]), status=status, owner=owner):
                snapshot, _, _, _ = self.collect(status, owner, api, cli=cli)
                nisi = next(row for row in snapshot["components"] if row["id"] == "nisi")
                self.assertEqual(nisi["state"], "unknown")
                self.assertEqual(nisi["detail"], "v0.2 pins verified; loaded models unknown")
                self.assert_unknown_counts(snapshot)

    def test_alias_counts_one_instance_and_retains_model_control_confirmation(self):
        from model_control import _exact_loaded_instance
        api = {"models": [{"key": "provider/model", "loaded_instances": [{"id": "alias-1"}]}]}
        cli = {"models": [{"identifier": "alias-1", "modelKey": "provider/model", "status": "idle"}]}
        snapshot, _, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER, api, cli=cli)
        rows = {row["id"]: row for row in snapshot["models"]}
        self.assertTrue(rows["provider/model"]["representedByInstances"])
        self.assertEqual(rows["provider/model"]["loadedInstanceIds"], ["alias-1"])
        self.assertTrue(_exact_loaded_instance(snapshot["models"], "provider/model", "provider/model"))
        nisi = next(row for row in snapshot["components"] if row["id"] == "nisi")
        self.assertEqual(nisi["state"], "partial")
        store = SnapshotStore()
        store.publish(snapshot)
        published = store.read()
        self.assertTrue(published["activityKnown"])
        self.assertEqual(published["history"][-1]["loaded"], 1)

    def test_conflicting_alias_preserves_catalog_and_isolates_cli_observation(self):
        api = {"models": [{"key": "a/foo", "display_name": "Foo", "size_bytes": 111, "loaded_instances": []},
                           {"key": "b/bar", "display_name": "Bar", "size_bytes": 999,
                            "loaded_instances": [{"id": "a/foo"}]}]}
        cli = {"models": [{"identifier": "a/foo", "modelKey": "b/bar", "status": "idle"}]}
        snapshot, _, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER, api, cli=cli)
        rows = {row["id"]: row for row in snapshot["models"]}
        self.assertEqual((rows["a/foo"]["name"], rows["a/foo"]["sizeBytes"], rows["a/foo"]["loaded"]),
                         ("Foo", 111, False))
        self.assertEqual((rows["b/bar"]["name"], rows["b/bar"]["sizeBytes"], rows["b/bar"]["loaded"]),
                         ("Bar", 999, True))
        cli_sources = [row for row in snapshot["sources"] if row["id"] == "lms-ps"]
        self.assertEqual(len(cli_sources), 1)
        self.assertEqual(cli_sources[0]["state"], "live")
        self.assertEqual([(r["id"],r["modelKey"]) for r in snapshot["localInstanceObservations"]], [("a/foo","b/bar")])

    def test_incomplete_source_preserves_rows_but_withholds_aggregate_counts(self):
        for api, cli, failed_source in (({"models": [{}]},
                {"models": [{"identifier": "local/model", "status": "idle"}]}, "lmstudio-api"),
                ({"models": [{"key": "local/model", "loaded_instances": [{"id": "local/model"}]}]},
                 {"models": [{}]}, "lms-ps")):
            with self.subTest(failed_source=failed_source):
                snapshot, _, _, _ = self.collect({"port": 1235, "running": True}, _LM_LISTENER, api, cli=cli)
                source = next(row for row in snapshot["sources"] if row["id"] == failed_source)
                self.assertIn("this source cannot establish the loaded count", source["detail"])
                self.assertNotIn("loaded count unknown", source["detail"])
                store = SnapshotStore()
                store.publish(snapshot)
                self.assertIsNone(store.read()["history"][-1]["loaded"])
                self.assertTrue(any(row["loaded"] is True for row in snapshot["models"]))


class ListenerOwnershipTests(unittest.TestCase):
    def test_exact_local_owner_with_no_process_arguments(self):
        for address in ("127.0.0.1", "*"):
            with self.subTest(address=address), \
                    mock.patch.object(telemetry, "_bounded_command", return_value=f"p41\nf9\nn{address}:1235\n") as read, \
                    mock.patch.object(telemetry, "_listener_executable", return_value="/Applications/LM Studio.app/Contents/MacOS/LM Studio"):
                self.assertEqual(telemetry._inventory_listener(1235)[:2], (41, "lmstudio"))
                self.assertEqual(read.call_args.args[0], ["/usr/sbin/lsof", "-nP", "-b", "-w", "-a", "-i4TCP:1235",
                                                       "-sTCP:LISTEN", "-F", "pfn"])
                self.assertEqual(read.call_args.kwargs["limit"], 8192)

    def test_remote_ambiguous_malformed_or_inaccessible_owners_are_unknown(self):
        for listing in ("", "p41\nf9\nn192.0.2.1:1235\n", "p41\nf9\nn127.0.0.1:1236\n",
                        "p41\nn*:1235\np42\nn*:1235\n", "pbad\nn*:1235\n"):
            with self.subTest(listing=listing), mock.patch.object(telemetry, "_bounded_command", return_value=listing), \
                    mock.patch.object(telemetry, "_listener_executable") as executable:
                self.assertIsNone(telemetry._inventory_listener(1235))
                executable.assert_not_called()
        with mock.patch.object(telemetry, "_bounded_command", side_effect=PermissionError()):
            self.assertIsNone(telemetry._inventory_listener(1235))

    def test_owner_path_is_required_and_pair_basename_alone_is_insufficient(self):
        for path, expected in ((None, None), ("/tmp/nvpair-proxy", None),
                ("/tmp/LM Studio.app/Contents/MacOS/LM Studio", None),
                ("/Applications/NVIDIA PAIR.app/Contents/MacOS/nvpair-proxy", "pair"),
                ("/Applications/NVIDIA PAIR.app/Contents/../nvpair-proxy", None)):
            with self.subTest(path=path), mock.patch.object(telemetry, "_bounded_command", return_value="p41\nn*:1234\n"), \
                    mock.patch.object(telemetry, "_listener_executable", return_value=path):
                result = telemetry._inventory_listener(1234)
                self.assertEqual(result[1] if result else None, expected)


class InMemoryDeadlineTests(unittest.TestCase):
    def test_fast_valid_inventory_and_response_caps(self):
        for response, expected in (
                (b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n\r\n{\"models\":[]}", {"models": []}),
                (b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n", OSError),
                (b"HTTP/1.1 200 OK\r\nContent-Length: 65\r\n\r\n" + b" " * 65, ValueError)):
            with self.subTest(expected=expected):
                sock = mock.Mock()
                sock.makefile.return_value = io.BytesIO(response)
                with mock.patch.object(telemetry.socket, "create_connection", return_value=sock), \
                        mock.patch.object(telemetry, "_MAX_HTTP", 64):
                    if isinstance(expected, dict):
                        self.assertEqual(telemetry._bounded_http({"host": "127.0.0.1", "port": 19001}), expected)
                    else:
                        with self.assertRaises(expected):
                            telemetry._bounded_http({"host": "127.0.0.1", "port": 19001})
                sock.shutdown.assert_not_called()
                sock.close.assert_called()

    def test_real_http_parser_interrupts_drip_headers_and_body_without_a_socket_bind(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                closed = threading.Event()
                prefix = b"HTTP/1.1 200 OK\r\nContent-Length: 13\r\n"
                body = b'{"models":[]}'
                raw = prefix + (b"X-Slow: " + b"x" * 30 + b"\r\n" if phase == "headers" else b"") + b"\r\n" + body
                slow_at = len(prefix) if phase == "headers" else len(raw) - len(body)

                class Drip(io.RawIOBase):
                    index = 0

                    def readable(self):
                        return True

                    def readinto(self, buffer):
                        if self.index == len(raw) or closed.is_set():
                            return 0
                        if self.index >= slow_at and closed.wait(0.12):
                            return 0
                        buffer[0] = raw[self.index]
                        self.index += 1
                        return 1

                sock = mock.Mock()
                sock.makefile.side_effect = lambda *args: io.BufferedReader(Drip())
                sock.shutdown.side_effect = lambda *args: closed.set()
                started = time.monotonic()
                with mock.patch.object(telemetry.socket, "create_connection", return_value=sock):
                    with self.assertRaises(TimeoutError):
                        telemetry._bounded_http({"host": "127.0.0.1", "port": 19001})
                self.assertLess(time.monotonic() - started, 2.0)
                self.assertTrue(closed.is_set())
                sock.close.assert_called()


class SlowInventoryTests(unittest.TestCase):
    def test_slow_headers_and_body_have_one_wall_clock_deadline(self):
        for phase in ("headers", "body"):
            with self.subTest(phase=phase):
                stopped = threading.Event()

                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        if phase == "headers":
                            chunks = b"HTTP/1.1 200 OK\r\nX-Slow: " + b"x" * 40
                        else:
                            self.send_response(200)
                            self.send_header("Content-Length", "40")
                            self.end_headers()
                            chunks = b" " * 40
                        for byte in chunks:
                            try:
                                self.wfile.write(bytes([byte]))
                                self.wfile.flush()
                            except OSError:
                                return
                            if stopped.wait(0.08):
                                return

                    def log_message(self, *args):
                        pass

                server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
                worker = threading.Thread(target=server.handle_request, daemon=True)
                worker.start()
                try:
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        telemetry._bounded_http({"host": "127.0.0.1", "port": server.server_port})
                    self.assertLess(time.monotonic() - started, 2.0)
                finally:
                    stopped.set()
                    worker.join(2)
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
