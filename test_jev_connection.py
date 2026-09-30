"""Mock-only tests for the native Jev helper boundary and manual check action."""
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest import mock

import jev_connection


class JevHTTPTests(unittest.TestCase):
    def test_jev_check_requires_exact_origin_and_closed_body(self):
        from server import MonitorServer, SnapshotStore
        with mock.patch('server.mac_online_code_controls_enabled', return_value=False):
            server = MonitorServer(('127.0.0.1', 0), SnapshotStore())
        server.jev_connection = mock.Mock()
        server.jev_connection.request_check.return_value = {'configured': False, 'state': 'not-configured'}
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            port = server.server_address[1]
            for origin, body, expected in (
                ('https://example.invalid', '{"action":"connection-check"}', 403),
                (f'http://127.0.0.1:{port}', '{"action":"connection-check","key":"must-never-be-accepted"}', 400),
                (f'http://127.0.0.1:{port}', '{"action":"connection-check"}', 202),
            ):
                connection = HTTPConnection('127.0.0.1', port, timeout=3)
                connection.request('POST', '/api/components/jev/check', body=body,
                                   headers={'Content-Type': 'application/json', 'Origin': origin})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                connection.close()
            server.jev_connection.request_check.assert_called_once_with()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(3)


class JevConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.helper = Path(self.temp.name) / "JevKeychain"
        self.helper.touch()

    @staticmethod
    def _result(payload, *, code=0):
        return subprocess.CompletedProcess([], code, json.dumps(payload).encode(), b"private diagnostic")

    def _run(self, configured=True, available=True, check_state="connected"):
        def run(args, **kwargs):
            command = args[-1]
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertTrue(kwargs["capture_output"])
            if command == "status":
                return self._result({"configured": configured, "available": available})
            if command == "check":
                return self._result({"state": check_state})
            self.fail("Python must never ask the helper to export a key")
        return run

    def _wait(self, connection):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if connection.status()["state"] != "checking":
                return connection.status()
            time.sleep(.01)
        self.fail("helper check did not finish")

    def test_status_only_invokes_status_and_never_reads_key_or_runs_check(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run", side_effect=self._run()) as run:
            status = connection.status()
        self.assertEqual(status, {"configured": True, "state": "idle",
                                  "message": "Connection check has not been run."})
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["status"])
        self.assertNotIn("secret", repr(status))

    def test_unconfigured_never_runs_explicit_check(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run", side_effect=self._run(configured=False)) as run:
            status = connection.request_check()
        self.assertEqual(status["state"], "not-configured")
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["status"])

    def test_explicit_check_calls_only_native_check_and_maps_fixed_result(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run", side_effect=self._run()) as run:
            initial = connection.request_check()
            self.assertEqual(initial["state"], "checking")
            final = self._wait(connection)
        self.assertEqual(final["state"], "connected")
        commands = [call.args[0][-1] for call in run.call_args_list]
        self.assertEqual(commands.count("check"), 1)
        # Polling may observe the worker before or after it finishes. Its exact
        # status-call count is scheduling-dependent; only one probe may run.
        self.assertGreaterEqual(commands.count("status"), 2)
        self.assertLessEqual(set(commands), {"status", "check"})
        check_call = next(call for call in run.call_args_list if call.args[0][-1] == "check")
        self.assertEqual(check_call.args[0], [str(self.helper), "check"])
        self.assertEqual(check_call.kwargs["timeout"], jev_connection._CHECK_TIMEOUT)
        self.assertNotIn("private diagnostic", repr(final))

    def test_helper_result_enum_is_strict_and_diagnostics_are_sanitized(self):
        for receipt in ("private text", {"state": "unexpected", "secret": "key"}, {}, {"state": None}):
            connection = jev_connection.JevConnection(self.helper)

            def run(args, **kwargs):
                if args[-1] == "status":
                    return self._result({"configured": True, "available": True})
                result = (subprocess.CompletedProcess([], 0, receipt.encode(), b"provider secret")
                          if isinstance(receipt, str) else self._result(receipt))
                return result

            with mock.patch.object(jev_connection.subprocess, "run", side_effect=run):
                connection.request_check()
                final = self._wait(connection)
            self.assertEqual(final["state"], "error")
            self.assertNotIn("provider", repr(final))
            self.assertNotIn("secret", repr(final))

    def test_known_helper_errors_map_to_fixed_messages(self):
        cases = {
            "auth-failed": "auth-failed",
            "rate-limited": "rate-limited",
            "network-error": "network-error",
            "invalid-response": "invalid-response",
            "unavailable": "unavailable",
        }
        for raw, expected in cases.items():
            connection = jev_connection.JevConnection(self.helper)
            with mock.patch.object(jev_connection.subprocess, "run",
                                   side_effect=self._run(check_state=raw)):
                connection.request_check()
                final = self._wait(connection)
            self.assertEqual(final["state"], expected)

    def test_concurrent_requests_share_single_native_check(self):
        connection = jev_connection.JevConnection(self.helper)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def run(args, **kwargs):
            if args[-1] == "status":
                return self._result({"configured": True, "available": True})
            calls.append(args[-1])
            entered.set()
            release.wait(1)
            return self._result({"state": "connected"})

        with mock.patch.object(jev_connection.subprocess, "run", side_effect=run):
            self.assertEqual(connection.request_check()["state"], "checking")
            self.assertTrue(entered.wait(1))
            self.assertEqual(connection.request_check()["state"], "checking")
            release.set()
            final = self._wait(connection)
        self.assertEqual(final["state"], "connected")
        self.assertEqual(calls, ["check"])

    def test_start_failure_does_not_leave_check_stuck(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run", side_effect=self._run()), \
                mock.patch.object(jev_connection.threading.Thread, "start", side_effect=RuntimeError("private")):
            status = connection.request_check()
        self.assertEqual(status["state"], "error")
        self.assertNotIn("private", repr(status))

    def test_invalid_status_receipt_fails_closed(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run",
                               return_value=self._result({"configured": True, "available": True,
                                                          "extra": "secret"})):
            status = connection.status()
        self.assertEqual(status["state"], "unavailable")
        self.assertFalse(status["configured"])


if __name__ == "__main__":
    unittest.main()
