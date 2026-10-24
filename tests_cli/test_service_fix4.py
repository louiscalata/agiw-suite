"""Fourth-round service regressions: all manager commands use injected runners."""
import io
import subprocess
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from support import FakeRunner, forbid_real_commands, temp_dir
from agiw import cli, service
from agiw.service import ServiceError, ServiceManager


SID = "S-1-5-21-111-222-333-1001"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
REAL_RUNNER = service.real_runner


class ServiceFix4(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-service-fix4-")

    def manager(self, kind, runner=None):
        return ServiceManager(kind, home=self.home, python="python", runner=runner or FakeRunner(),
                              env={}, uid=501, system_dir="C:\\Windows\\System32")

    def windows_runner(self, states):
        calls = []
        remaining = iter(states)
        last = [None]

        def runner(argv):
            calls.append(list(argv))
            if "/user" in argv:
                return subprocess.CompletedProcess(argv, 0, f'"tester","{SID}"\n', "")
            if "-Command" in argv:
                last[0] = next(remaining, last[0])
                if isinstance(last[0], Exception):
                    raise last[0]
                code, stdout, stderr = last[0]
                return subprocess.CompletedProcess(argv, code, stdout, stderr)
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.calls = calls
        return runner

    def test_windows_task_restarts_crashes_and_marks_service_lock_refusal_clean(self):
        root = ET.fromstring(self.manager("windows").render().decode("utf-16"))
        restart = root.find("t:Settings/t:RestartOnFailure", NS)
        self.assertIsNotNone(restart)
        self.assertEqual(restart.find("t:Interval", NS).text, "PT1M")
        self.assertEqual(restart.find("t:Count", NS).text, "3")
        arguments = root.find("t:Actions/t:Exec/t:Arguments", NS).text
        self.assertEqual(arguments, "-m agiw serve --service")

    def test_windows_install_waits_for_locale_independent_running_state(self):
        runner = self.windows_runner([(0, "Ready\n", ""), (0, "Queued\n", ""), (0, "Running\n", "")])
        with mock.patch("agiw.service.time.sleep") as sleep:
            steps = self.manager("windows", runner).install()
        queries = [argv for argv in runner.calls if "-Command" in argv]
        self.assertEqual(len(queries), 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1.0), mock.call(1.0)])
        self.assertTrue(all("Get-ScheduledTask" in argv[-1] and ".State" in argv[-1] for argv in queries))
        self.assertFalse(any("/Query" in argv for argv in runner.calls))
        self.assertIn("running", steps[-1])

    def test_windows_install_unconfirmed_states_exit_one_and_keep_definition(self):
        for state in ("Ready", "Queued", "Unknown"):
            with self.subTest(state=state):
                runner = self.windows_runner([(0, state + "\n", "")])
                output, error = [], io.StringIO()
                with mock.patch("agiw.service.time.sleep") as sleep, mock.patch("sys.stderr", error):
                    code = cli.main(["service", "install", "--status-file", str(self.home / "status.json")],
                                    kind="windows", home=self.home, env={}, runner=runner, out=output.append)
                self.assertEqual(code, 1, output)
                self.assertIn("installed but not confirmed running", error.getvalue())
                self.assertIn("State is " + state, error.getvalue())
                self.assertNotIn("and started it", "\n".join(output))
                self.assertTrue(self.manager("windows").definition_path().is_file())
                self.assertEqual(len([argv for argv in runner.calls if "-Command" in argv]), 6)
                self.assertEqual(sleep.call_args_list, [mock.call(1.0)] * 5)

    def test_windows_install_query_failure_explains_reason_and_exits_one(self):
        for response, reason in (((1, "", "access denied"), "access denied"),
                                 (OSError("query unavailable"), "query unavailable")):
            with self.subTest(reason=reason):
                runner = self.windows_runner([response])
                error = io.StringIO()
                with mock.patch("agiw.service.time.sleep"), mock.patch("sys.stderr", error):
                    code = cli.main(["service", "install", "--status-file", str(self.home / "status.json")],
                                    kind="windows", home=self.home, env={}, runner=runner, out=lambda line: None)
                self.assertEqual(code, 1)
                self.assertIn("installed but not confirmed running", error.getvalue())
                self.assertIn("state query failed", error.getvalue())
                self.assertIn(reason, error.getvalue())
                self.assertNotIn("Traceback", error.getvalue())

    def test_windows_install_retries_a_query_failure_before_running(self):
        runner = self.windows_runner([(1, "", "temporary query failure"), (0, "Running\n", "")])
        with mock.patch("agiw.service.time.sleep") as sleep:
            steps = self.manager("windows", runner).install()
        self.assertEqual(len([argv for argv in runner.calls if "-Command" in argv]), 2)
        sleep.assert_called_once_with(1.0)
        self.assertIn("running", steps[-1])

    def test_windows_install_does_not_retry_after_query_consumes_wait_budget(self):
        clock = [0.0]
        state_runner = self.windows_runner([(0, "Ready\n", "")])

        def slow_runner(argv):
            result = state_runner(argv)
            if "-Command" in argv:
                clock[0] += 6.0
            return result

        error = io.StringIO()
        with mock.patch("agiw.service.time.monotonic", side_effect=lambda: clock[0]), \
                mock.patch("agiw.service.time.sleep") as sleep, mock.patch("sys.stderr", error):
            code = cli.main(["service", "install", "--status-file", str(self.home / "status.json")],
                            kind="windows", home=self.home, env={}, runner=slow_runner, out=lambda line: None)
        self.assertEqual(code, 1)
        self.assertIn("installed but not confirmed running", error.getvalue())
        self.assertIn("State is Ready", error.getvalue())
        self.assertEqual(len([argv for argv in state_runner.calls if "-Command" in argv]), 1)
        sleep.assert_not_called()

    def test_mac_rollback_bootstrap_waits_and_retries_like_forward_bootstrap(self):
        manager = self.manager("mac")
        path = manager.definition_path()
        path.parent.mkdir(parents=True)
        previous = b"previous LaunchAgent definition\n"
        path.write_bytes(previous)
        rollback_calls = []
        forward_calls = []

        def runner(argv):
            restored = path.read_bytes() == previous
            calls = rollback_calls if restored else forward_calls
            calls.append(argv[1])
            code = 0
            if argv[1] == "bootstrap":
                code = 5 if not restored or calls.count("bootstrap") == 1 else 0
            return subprocess.CompletedProcess(argv, code, "", "bootstrap still busy" if code else "")

        manager.runner = runner
        with mock.patch("agiw.service.time.sleep") as sleep:
            with self.assertRaisesRegex(ServiceError, "rolled back to the previous loaded definition"):
                manager.install()
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(forward_calls, ["bootout", "bootstrap", "bootstrap"])
        self.assertEqual(rollback_calls, ["bootout", "bootstrap", "bootstrap", "print"])
        self.assertEqual(sleep.call_args_list, [mock.call(2.0), mock.call(2.0)])

    def test_real_runner_replaces_undecodable_subprocess_bytes(self):
        raw = subprocess.CompletedProcess(["fake-tool"], 1, b"output \xff", b"localized \xfe")
        with mock.patch("agiw.service.subprocess.run", return_value=raw) as run, \
                mock.patch("locale.getpreferredencoding", return_value="ascii"):
            result = REAL_RUNNER(["fake-tool"])
        self.assertEqual(result.stdout, "output \ufffd")
        self.assertEqual(result.stderr, "localized \ufffd")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(run.call_args.kwargs.get("text", False))


if __name__ == "__main__":
    unittest.main()
