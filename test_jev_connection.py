"""Mock-only tests for the native Jev helper boundary and manual check action."""
import json
import io
from email.message import Message
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest import mock

import jev_connection


class JevHTTPContractTests(unittest.TestCase):
    """Exercise the real handlers without binding a socket or starting an observer."""

    def test_public_routes_payloads_and_codes(self):
        from server import Handler
        payload = {'configured': True, 'state': 'idle', 'message': 'Connection check has not been run.'}
        handler = Handler.__new__(Handler)
        handler.server = mock.Mock(server_address=('127.0.0.1', 8765), control_capability='fixture-only')
        handler.server.jev_connection.status.return_value = payload
        handler.server.jev_connection.request_check.return_value = payload
        handler.reply = mock.Mock()
        for method, path, origin, capability, body, code, expected in (
            ('GET', '/api/components/jev', 'http://127.0.0.1:8765', 'fixture-only', b'', 200, payload),
            ('POST', '/api/components/jev/check', 'http://127.0.0.1:8765', 'fixture-only',
             b'{"action":"connection-check"}', 202, payload),
            ('POST', '/api/components/jev/check', 'https://example.invalid', 'fixture-only',
             b'{"action":"connection-check"}', 403,
             {'status': 'error', 'message': 'Exact same-origin Origin and Host are required.'}),
            ('POST', '/api/components/jev/check', 'http://127.0.0.1:8765', 'wrong',
             b'{"action":"connection-check"}', 403,
             {'status': 'error', 'message': 'Native control capability required.'}),
            ('POST', '/api/components/jev/check', 'http://127.0.0.1:8765', 'fixture-only',
             b'{"action":"connection-check","key":"forbidden"}', 400,
             {'status': 'error', 'message': 'Body must contain only action: connection-check.'}),
        ):
            with self.subTest(method=method, code=code, body=body):
                handler.path = path
                handler.headers = Message()
                for key, value in {'Host': '127.0.0.1:8765', 'Origin': origin,
                                   'X-AGIW-Control': capability, 'Content-Type': 'application/json',
                                   'Content-Length': str(len(body))}.items():
                    handler.headers[key] = value
                handler.rfile = io.BytesIO(body)
                handler.reply.reset_mock()
                getattr(handler, 'do_' + method)()
                handler.reply.assert_called_once_with(code, json.dumps(expected).encode(), 'application/json')
        handler.server.jev_connection.status.assert_called_once_with()
        handler.server.jev_connection.request_check.assert_called_once_with()


class JevHTTPTests(unittest.TestCase):
    def test_jev_check_requires_exact_origin_and_closed_body(self):
        from server import MonitorServer, SnapshotStore
        with mock.patch('server.mac_online_code_controls_enabled', return_value=False):
            server = MonitorServer(('127.0.0.1', 0), SnapshotStore(), control_capability='fixture-only')
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
                                   headers={'Content-Type': 'application/json', 'Origin': origin, 'X-AGIW-Control': 'fixture-only'})
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
            if command == "status-revision":
                return self._result({"configured": configured, "available": available,
                                     "revision": "credential-a" if configured else None})
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
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["status-revision"])
        self.assertNotIn("secret", repr(status))

    def test_unconfigured_never_runs_explicit_check(self):
        connection = jev_connection.JevConnection(self.helper)
        with mock.patch.object(jev_connection.subprocess, "run", side_effect=self._run(configured=False)) as run:
            status = connection.request_check()
        self.assertEqual(status["state"], "not-configured")
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["status-revision"])

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
        self.assertGreaterEqual(commands.count("status-revision"), 2)
        self.assertLessEqual(set(commands), {"status-revision", "check"})
        check_call = next(call for call in run.call_args_list if call.args[0][-1] == "check")
        self.assertEqual(check_call.args[0], [str(self.helper), "check"])
        self.assertEqual(check_call.kwargs["timeout"], jev_connection._CHECK_TIMEOUT)
        self.assertNotIn("private diagnostic", repr(final))

    def test_helper_result_enum_is_strict_and_diagnostics_are_sanitized(self):
        for receipt in ("private text", {"state": "unexpected", "secret": "key"}, {}, {"state": None}):
            connection = jev_connection.JevConnection(self.helper)

            def run(args, **kwargs):
                if args[-1] == "status-revision":
                    return self._result({"configured": True, "available": True, "revision": "credential-a"})
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
            if args[-1] == "status-revision":
                return self._result({"configured": True, "available": True, "revision": "credential-a"})
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

    def test_public_configuration_payloads(self):
        for configured, available, state, message in (
            (True, True, 'idle', 'Connection check has not been run.'),
            (False, True, 'not-configured', 'Add a TypeSafe API key to enable the optional check.'),
            (False, False, 'unavailable', 'Secure Jev credential storage is unavailable.'),
            (True, False, 'unavailable', 'Secure Jev credential storage is unavailable.'),
        ):
            with self.subTest(configured=configured, available=available):
                connection = jev_connection.JevConnection(self.helper)
                with mock.patch.object(jev_connection.subprocess, 'run',
                                       side_effect=self._run(configured, available)):
                    self.assertEqual(connection.status(), {
                        'configured': configured and available, 'state': state, 'message': message})

    def test_all_public_probe_results_and_messages(self):
        messages = {
            'connected': 'The hosted Jev connection check succeeded.',
            'not-configured': 'Add a TypeSafe API key to enable the optional check.',
            'auth-failed': 'The TypeSafe API key was rejected.',
            'rate-limited': 'The hosted Jev service is rate limiting requests. Try again later.',
            'network-error': 'The hosted Jev connection could not be reached.',
            'invalid-response': 'The hosted Jev response did not match the expected format.',
            'unavailable': 'Secure Jev credential storage is unavailable.',
            'error': 'The hosted Jev connection could not be checked.',
        }
        for state, message in messages.items():
            with self.subTest(state=state):
                connection = jev_connection.JevConnection(self.helper)
                with mock.patch.object(jev_connection.subprocess, 'run', side_effect=self._run(check_state=state)):
                    connection.request_check()
                    connection._worker.join(2)
                    self.assertFalse(connection._worker.is_alive())
                    self.assertEqual(connection.status(), {'configured': True, 'state': state, 'message': message})

    def test_checking_and_start_failure_payloads(self):
        for error, state, message in (
            (None, 'checking', 'Checking the hosted Jev connection.'),
            (RuntimeError('private'), 'error', 'The connection check could not be started.'),
        ):
            connection = jev_connection.JevConnection(self.helper)
            with mock.patch.object(jev_connection.subprocess, 'run', side_effect=self._run()), \
                    mock.patch.object(threading.Thread, 'start', side_effect=error):
                self.assertEqual(connection.request_check(), {'configured': True, 'state': state, 'message': message})

    def _association_fixture(self, *, blocked=False):
        credential = {'revision': 'credential-a', 'configured': True, 'available': True}
        entered, release = threading.Event(), threading.Event()
        calls = []
        if not blocked:
            release.set()

        def run(command, **kwargs):
            calls.append(command)
            if command in ('status', 'status-revision'):
                payload = {key: credential[key] for key in ('configured', 'available')}
                if command == 'status-revision':
                    payload['revision'] = credential['revision'] if credential['configured'] else None
                return self._result(payload)
            self.assertEqual(command, 'check')
            entered.set()
            self.assertTrue(release.wait(2), 'fixture probe was not released')
            return self._result({'state': 'connected'})

        return credential, entered, release, calls, run

    def test_association_replacement_invalidates_finished_probe(self):
        connection = jev_connection.JevConnection(self.helper)
        credential, _, _, calls, run = self._association_fixture()
        with mock.patch.object(connection, '_helper_command', side_effect=run):
            connection.request_check()
            connection._worker.join(2)
            self.assertEqual(connection.status()['state'], 'connected')
            credential['revision'] = 'credential-b'
            self.assertEqual(connection.status(), {'configured': True, 'state': 'idle',
                                                  'message': 'Connection check has not been run.'})
            self.assertEqual(calls.count('check'), 1)
            connection.request_check()
            connection._worker.join(2)
            self.assertEqual(connection.status()['state'], 'connected')
            self.assertEqual(calls.count('check'), 2)

    def test_association_removal_does_not_resurrect_success(self):
        connection = jev_connection.JevConnection(self.helper)
        credential, _, _, _, run = self._association_fixture()
        with mock.patch.object(connection, '_helper_command', side_effect=run):
            connection.request_check()
            connection._worker.join(2)
            self.assertEqual(connection.status()['state'], 'connected')
            credential['configured'] = False
            self.assertEqual(connection.status()['state'], 'not-configured')
            credential.update(configured=True, revision='credential-b')
            self.assertEqual(connection.status()['state'], 'idle')

    def test_association_old_completion_after_replacement_is_discarded(self):
        for poll_during_check in (False, True):
            with self.subTest(poll_during_check=poll_during_check):
                connection = jev_connection.JevConnection(self.helper)
                credential, entered, release, calls, run = self._association_fixture(blocked=True)
                with mock.patch.object(connection, '_helper_command', side_effect=run):
                    connection.request_check()
                    worker = connection._worker
                    try:
                        self.assertTrue(entered.wait(1))
                        credential['revision'] = 'credential-b'
                        if poll_during_check:
                            self.assertEqual(connection.status()['state'], 'idle')
                            self.assertEqual(connection.request_check()['state'], 'idle')
                            self.assertIs(connection._worker, worker)
                    finally:
                        release.set()
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(connection.status()['state'], 'idle')
                    self.assertEqual(calls.count('check'), 1)

    def test_association_unavailable_observation_invalidates_inflight_probe(self):
        connection = jev_connection.JevConnection(self.helper)
        credential, entered, release, _, run = self._association_fixture(blocked=True)
        with mock.patch.object(connection, '_helper_command', side_effect=run):
            connection.request_check()
            worker = connection._worker
            try:
                self.assertTrue(entered.wait(1))
                credential.update(configured=False, available=False)
                self.assertEqual(connection.status()['state'], 'unavailable')
                credential.update(configured=True, available=True)
            finally:
                release.set()
                worker.join(2)
            self.assertEqual(connection.status()['state'], 'idle')

    def test_association_replacement_before_worker_starts_never_checks_new_key(self):
        connection = jev_connection.JevConnection(self.helper)
        credential, _, _, calls, run = self._association_fixture()
        with mock.patch.object(connection, '_helper_command', side_effect=run), \
                mock.patch.object(threading.Thread, 'start'):
            self.assertEqual(connection.request_check()['state'], 'checking')
            credential['revision'] = 'credential-b'
            connection._run_probe('credential-a')
            self.assertEqual(connection.status()['state'], 'idle')
            self.assertNotIn('check', calls)

    def test_association_missing_or_invalid_revision_fails_closed(self):
        for revision in (None, '', False, [], {}, 'a' * 129, 'private\ntext'):
            with self.subTest(revision=revision):
                connection = jev_connection.JevConnection(self.helper)
                receipt = {'configured': True, 'available': True, 'revision': revision}
                with mock.patch.object(connection, '_helper_command', return_value=self._result(receipt)) as helper:
                    self.assertEqual(connection.request_check(), {
                        'configured': False, 'state': 'unavailable',
                        'message': 'Secure Jev credential storage is unavailable.'})
                    helper.assert_called_once_with('status-revision')


if __name__ == "__main__":
    unittest.main()
