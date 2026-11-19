"""Re-running `agiw service install` on Windows restarts a task that is already running.

The logon task uses MultipleInstancesPolicy IgnoreNew, so `schtasks /Run` on a running task starts nothing and
the old peer-link process keeps serving the code it loaded. Every command goes to a scripted runner; any real
process start fails these tests."""

import io
import subprocess
import unittest
from unittest import mock

from support import forbid_real_commands, temp_dir

from agiw import cli
from agiw.service import ServiceError, ServiceManager

SID = "S-1-5-21-111-222-333-1001"
SCHTASKS = "C:\\Windows\\System32\\schtasks.exe"
TASK = "\\AGIW\\PeerLink-" + SID


class WindowsReinstall(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-service-reinstall-")

    def manager(self, runner):
        return ServiceManager(
            "windows",
            home=self.home,
            python="python",
            runner=runner,
            env={},
            uid=501,
            system_dir="C:\\Windows\\System32",
        )

    def scripted(self, states, end=(0, "")):
        """Each State query takes the next (code, stdout) from states; the last one repeats."""
        calls = []
        remaining = iter(states)
        last = [None]

        def runner(argv):
            argv = list(argv)
            calls.append(argv)
            if "/user" in argv:
                return subprocess.CompletedProcess(argv, 0, f'"tester","{SID}"\n', "")
            if "-Command" in argv:
                last[0] = next(remaining, last[0])
                if isinstance(last[0], Exception):
                    raise last[0]
                code, stdout = last[0]
                return subprocess.CompletedProcess(argv, code, stdout, "")
            if "/End" in argv:
                return subprocess.CompletedProcess(argv, end[0], end[1], "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.calls = calls
        return runner

    def verbs(self, runner):
        out = []
        for argv in runner.calls:
            if argv[0] == SCHTASKS:
                out.append(argv[1])
            elif "-Command" in argv:
                out.append("state")
        return out

    def test_running_task_is_stopped_and_started_again(self):
        runner = self.scripted([(0, "Running\n"), (0, "Ready\n"), (0, "Running\n")])
        with mock.patch("agiw.service.time.sleep"):
            steps = self.manager(runner).install()
        self.assertEqual(self.verbs(runner), ["/Create", "state", "/End", "state", "/Run", "state"])
        self.assertIn([SCHTASKS, "/End", "/TN", TASK], runner.calls)
        self.assertTrue(any("restarted" in step for step in steps), steps)
        self.assertIn("confirmed running", steps[-1])

    def test_queued_instance_is_also_ended_before_the_new_run(self):
        runner = self.scripted([(0, "Queued\n"), (0, "Ready\n"), (0, "Running\n")])
        with mock.patch("agiw.service.time.sleep"):
            self.manager(runner).install()
        self.assertEqual(self.verbs(runner), ["/Create", "state", "/End", "state", "/Run", "state"])

    def test_waits_for_the_old_instance_to_stop(self):
        runner = self.scripted([(0, "Running\n"), (0, "Running\n"), (0, "Ready\n"), (0, "Running\n")])
        with mock.patch("agiw.service.time.sleep") as sleep:
            self.manager(runner).install()
        self.assertEqual(self.verbs(runner), ["/Create", "state", "/End", "state", "state", "/Run", "state"])
        sleep.assert_called_once_with(1.0)

    def test_fresh_or_stopped_task_is_not_ended(self):
        for before in ((3, ""), (0, "Ready\n"), (0, "Disabled\n")):
            with self.subTest(before=before):
                runner = self.scripted([before, (0, "Running\n")])
                with mock.patch("agiw.service.time.sleep"):
                    steps = self.manager(runner).install()
                self.assertEqual(self.verbs(runner), ["/Create", "state", "/Run", "state"])
                self.assertFalse(any("restarted" in step for step in steps), steps)

    def test_old_instance_that_never_stops_fails_without_a_false_running_claim(self):
        runner = self.scripted([(0, "Running\n")])
        error = io.StringIO()
        with mock.patch("agiw.service.time.sleep"), mock.patch("sys.stderr", error):
            code = cli.main(
                ["service", "install", "--status-file", str(self.home / "status.json")],
                kind="windows",
                home=self.home,
                env={},
                runner=runner,
                out=lambda line: None,
            )
        self.assertEqual(code, 1)
        self.assertIn("previous instance", error.getvalue())
        self.assertIn("schtasks /End /TN " + TASK, error.getvalue())
        self.assertNotIn("/Run", self.verbs(runner))
        self.assertTrue(self.manager(runner).definition_path().is_file())

    def test_unknown_state_before_run_refuses_instead_of_guessing(self):
        for before in ((1, ""), (0, "Unknown\n"), OSError("query unavailable")):
            with self.subTest(before=before):
                runner = self.scripted([before])
                with (
                    mock.patch("agiw.service.time.sleep"),
                    self.assertRaisesRegex(ServiceError, "cannot tell whether .* is running"),
                ):
                    self.manager(runner).install()
                self.assertNotIn("/Run", self.verbs(runner))
                self.assertNotIn("/End", self.verbs(runner))


if __name__ == "__main__":
    unittest.main()
