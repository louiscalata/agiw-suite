"""Third-round service regressions: fake runners and temporary homes only."""
import plistlib
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from support import FakeRunner, forbid_real_commands, temp_dir
from agiw.service import LAUNCHD_LABEL, ServiceError, ServiceManager


SID = "S-1-5-21-111-222-333-1001"
TASK = "\\AGIW\\PeerLink-" + SID
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class ServiceFix3(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-service-fix3-")

    def manager(self, kind, runner=None, **kwargs):
        return ServiceManager(kind, home=self.home, runner=runner or FakeRunner(), env={}, uid=501,
                              system_dir="C:\\Windows\\System32", **kwargs)

    def existing(self, manager):
        path = manager.definition_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"previous definition\n")
        return path

    def windows_runner(self, state="Ready", code=0, stderr=""):
        calls = []

        def runner(argv):
            calls.append(list(argv))
            if "/user" in argv:
                return subprocess.CompletedProcess(argv, 0, f'"tester","{SID}"\n', "")
            if "-Command" in argv:
                return subprocess.CompletedProcess(argv, code, state, stderr)
            if "/Query" in argv:
                # Deliberately localized and inconsistent: this must never be parsed.
                return subprocess.CompletedProcess(argv, 0, "Aufgabenname: PeerLink\nStatus: Wird ausgeführt\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.calls = calls
        return runner

    def test_mac_rollback_boots_out_loaded_new_job_before_bootstrap(self):
        manager = self.manager("mac", python="python")
        path = self.existing(manager)
        calls = []
        new_loaded = [False]

        def runner(argv):
            calls.append((argv[1], path.read_bytes()))
            code = 0
            if argv[1] == "bootout":
                new_loaded[0] = False
            elif argv[1] == "bootstrap":
                if path.read_bytes() != b"previous definition\n":
                    new_loaded[0] = True
                    code = 5
                elif new_loaded[0]:
                    code = 5
            return subprocess.CompletedProcess(argv, code, "", "loaded job prevents bootstrap" if code else "")

        manager.runner = runner
        with mock.patch("agiw.service.time.sleep"), self.assertRaisesRegex(ServiceError, "rolled back"):
            manager.install()
        self.assertEqual(path.read_bytes(), b"previous definition\n")
        self.assertEqual([verb for verb, data in calls if data == b"previous definition\n"],
                         ["bootout", "bootstrap", "print"])

    def test_mac_rollback_not_loaded_bootout_still_requires_successful_print(self):
        for print_code in (0, 113):
            with self.subTest(print_code=print_code):
                manager = self.manager("mac", python="python")
                path = self.existing(manager)
                calls = []

                def runner(argv):
                    old = path.read_bytes() == b"previous definition\n"
                    calls.append((argv[1], old))
                    code = 113 if argv[1] == "bootout" else 0
                    if argv[1] == "bootstrap" and not old:
                        code = 5
                    if argv[1] == "print":
                        code = print_code
                    return subprocess.CompletedProcess(argv, code, "", "")

                manager.runner = runner
                expected = "rolled back" if print_code == 0 else "rollback failed.*loading is unconfirmed"
                with self.assertRaisesRegex(ServiceError, expected):
                    manager.install()
                self.assertEqual([verb for verb, old in calls if old], ["bootout", "bootstrap", "print"])

    def test_windows_status_uses_locale_independent_state_for_each_enum(self):
        for enum, running in (("Unknown", None), ("Disabled", False), ("Queued", False),
                              ("Ready", False), ("Running", True)):
            with self.subTest(enum=enum):
                runner = self.windows_runner(enum + "\n")
                result = self.manager("windows", runner, python="python").status()
                self.assertEqual((result["loaded"], result["running"]), (True, running))
                self.assertFalse(any("/Query" in argv for argv in runner.calls))
                command = next(argv for argv in runner.calls if "-Command" in argv)
                self.assertEqual(command[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
                self.assertTrue(command[0].endswith("powershell.exe"))
                self.assertIn("Get-ScheduledTask -TaskPath '\\AGIW\\' -TaskName 'PeerLink-" + SID + "'", command[4])
                self.assertIn(".State", command[4])

    def test_windows_missing_state_is_missing_and_stale_definition_is_removed(self):
        runner = self.windows_runner("", code=3)
        manager = self.manager("windows", runner, python="python")
        path = self.existing(manager)
        state = manager.status()
        self.assertEqual((state["loaded"], state["running"]), (False, False))
        manager.uninstall()
        self.assertFalse(path.exists())
        self.assertFalse(any("/Delete" in argv for argv in runner.calls))

    def test_windows_missing_detection_requires_scheduled_task_query_error_identity(self):
        # CommandNotFoundException also has category ObjectNotFound. The fixed
        # script must require the ScheduledTasks query error ID before exit 3;
        # an unavailable cmdlet/module must keep the ordinary failure exit.
        runner = self.windows_runner()
        script = self.manager("windows", runner, python="python")._windows_state_command()[-1]
        self.assertIn("$_.CategoryInfo.Category -eq [System.Management.Automation.ErrorCategory]::ObjectNotFound", script)
        self.assertIn("-and $_.FullyQualifiedErrorId -like 'CmdletizationQuery_NotFound*,Get-ScheduledTask'", script)
        self.assertIn("{ exit 3 }; [Console]::Error.WriteLine($_.Exception.Message); exit 1", script)

    def test_windows_uninstall_accepts_ready_and_disabled_without_localized_query(self):
        for enum in ("Ready", "Disabled"):
            with self.subTest(enum=enum):
                runner = self.windows_runner(enum + "\n")
                manager = self.manager("windows", runner, python="python")
                path = self.existing(manager)
                manager.uninstall()
                self.assertFalse(path.exists())
                self.assertFalse(any("/Query" in argv for argv in runner.calls))
                self.assertTrue(any("/End" in argv for argv in runner.calls))
                self.assertTrue(any("/Delete" in argv for argv in runner.calls))

    def test_windows_uninstall_keeps_unknown_queued_and_running_states(self):
        for enum in ("Unknown", "Queued", "Running"):
            with self.subTest(enum=enum):
                runner = self.windows_runner(enum + "\n")
                manager = self.manager("windows", runner, python="python")
                path = self.existing(manager)
                with self.assertRaisesRegex(ServiceError, "definition was kept.*" + enum):
                    manager.uninstall()
                self.assertEqual(path.read_bytes(), b"previous definition\n")
                self.assertFalse(any("/Delete" in argv for argv in runner.calls))

    def test_windows_powershell_failure_is_unknown_and_uninstall_explains_why(self):
        for output, code in (("", 1), ("Ready\n", 1), ("not an enum\n", 0)):
            with self.subTest(output=output, code=code):
                runner = self.windows_runner(output, code, "Zugriff verweigert")
                manager = self.manager("windows", runner, python="python")
                path = self.existing(manager)
                state = manager.status()
                self.assertIsNone(state["loaded"])
                self.assertIsNone(state["running"])
                with self.assertRaisesRegex(ServiceError, "definition was kept.*state query failed.*Zugriff verweigert"):
                    manager.uninstall()
                self.assertEqual(path.read_bytes(), b"previous definition\n")
                self.assertFalse(any("/Delete" in argv for argv in runner.calls))

    def test_windows_task_name_rejects_powershell_metacharacters_before_runner(self):
        for suffix in ("bad'name", "bad_name", "bad name", "bad;name", "bad\\name"):
            with self.subTest(suffix=suffix):
                runner = self.windows_runner()
                manager = self.manager("windows", runner, python="python")
                manager._windows_task = "\\AGIW\\" + suffix
                with self.assertRaisesRegex(ServiceError, "task name"):
                    manager.uninstall()
                self.assertEqual(runner.calls, [])

    def test_launch_agent_marks_service_lock_refusal_as_clean_and_throttles(self):
        plist = plistlib.loads(self.manager("mac", python="python").render())
        self.assertEqual(plist["ProgramArguments"], ["python", "-m", "agiw", "serve", "--service"])
        self.assertEqual(plist["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(plist["ThrottleInterval"], 60)

    def test_systemd_prevents_lock_restart_and_has_effective_unit_start_limit(self):
        text = self.manager("linux", python="python").render().decode()
        unit, service = text.split("[Service]", 1)
        self.assertIn("StartLimitIntervalSec=300\n", unit)
        self.assertIn("StartLimitBurst=5\n", unit)
        self.assertIn("RestartPreventExitStatus=75\n", service)
        self.assertIn("RestartSec=30\n", service)
        self.assertIn("Restart=on-failure\n", service)
        self.assertNotIn("--service", text)

    def test_windows_task_does_not_restart_on_failure(self):
        root = ET.fromstring(self.manager("windows", python="python").render().decode("utf-16"))
        self.assertIsNone(root.find("t:Settings/t:RestartOnFailure", NS))

    def test_windows_pythonw_beside_executable_wins_over_base_prefix(self):
        venv = self.home / "venv" / "Scripts"
        base = self.home / "base"
        venv.mkdir(parents=True)
        base.mkdir()
        exe = venv / "python.exe"
        quiet = venv / "pythonw.exe"
        exe.touch()
        quiet.touch()
        (base / "pythonw.exe").touch()
        with mock.patch.object(sys, "executable", str(exe)), mock.patch.object(sys, "base_exec_prefix", str(base)):
            self.assertEqual(self.manager("windows").python, str(quiet))

    def test_windows_pythonw_in_base_exec_prefix_is_used_for_venv(self):
        venv = self.home / "venv" / "Scripts"
        base = self.home / "base"
        venv.mkdir(parents=True)
        base.mkdir()
        exe = venv / "python.exe"
        quiet = base / "pythonw.exe"
        exe.touch()
        quiet.touch()
        with mock.patch.object(sys, "executable", str(exe)), mock.patch.object(sys, "base_exec_prefix", str(base)):
            self.assertEqual(self.manager("windows").python, str(quiet))

    def test_windows_python_falls_back_when_neither_pythonw_exists(self):
        exe = self.home / "venv" / "Scripts" / "python.exe"
        with mock.patch.object(sys, "executable", str(exe)), mock.patch.object(sys, "base_exec_prefix", str(self.home)):
            self.assertEqual(self.manager("windows").python, str(exe))


if __name__ == "__main__":
    unittest.main()
