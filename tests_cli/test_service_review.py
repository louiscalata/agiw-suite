"""Regression tests for service lifecycle review findings; no native service commands run."""
import csv
import io
import subprocess
import unittest
from unittest import mock

from support import FakeRunner, forbid_real_commands, temp_dir
from agiw.service import LAUNCHD_LABEL, ServiceError, ServiceManager

SID = "S-1-5-21-111-222-333-1001"
TASK = "\\AGIW\\PeerLink-" + SID


class ServiceReview(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-service-review-")

    def manager(self, kind, runner):
        return ServiceManager(kind, home=self.home, python="python", runner=runner, env={}, uid=501,
                              system_dir="C:\\Windows\\System32")

    def existing(self, manager):
        path = manager.definition_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"previous definition\n")
        return path

    def windows_runner(self, results=None):
        output = io.StringIO()
        csv.writer(output).writerow(['Doma,in\\Élise "Test"', SID])
        return FakeRunner({"/user": (0, output.getvalue()), **(results or {})})

    def test_windows_sid_with_csv_quoted_unicode_name_scopes_every_operation(self):
        runner = self.windows_runner({"-Command": (0, "Running\n")})
        manager = self.manager("windows", runner)
        manager.install()
        manager.status()
        runner.results["-Command"] = (0, "Ready\n")
        manager.uninstall()
        self.assertIn(["C:\\Windows\\System32\\whoami.exe", "/user", "/fo", "csv", "/nh"], runner.calls)
        tasks = [argv[argv.index("/TN") + 1] for argv in runner.calls if "/TN" in argv]
        self.assertTrue(tasks)
        self.assertEqual(set(tasks), {TASK})
        scripts = [argv[-1] for argv in runner.calls if "-Command" in argv]
        self.assertTrue(scripts)
        for script in scripts:
            self.assertIn("-TaskPath '\\AGIW\\' -TaskName 'PeerLink-" + SID + "'", script)

    def test_windows_missing_sid_refuses_before_creating_anything(self):
        runner = FakeRunner({"/user": (0, '"Domaine\\Élise","not a SID"\n')})
        with self.assertRaisesRegex(ServiceError, "SID"):
            self.manager("windows", runner).install()
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertFalse(any("/Create" in argv for argv in runner.calls))

    def test_uninstall_keeps_definition_when_stop_fails_and_service_is_running(self):
        cases = (("mac", {"bootout": (5, ""), "print": (0, "state = running\n")}),
                 ("windows", {"/End": (1, ""), "-Command": (0, "Running\n")}))
        for kind, results in cases:
            with self.subTest(kind=kind):
                runner = self.windows_runner(results) if kind == "windows" else FakeRunner(results)
                manager = self.manager(kind, runner)
                path = self.existing(manager)
                with self.assertRaisesRegex(ServiceError, "definition.*kept.*stop"):
                    manager.uninstall()
                self.assertEqual(path.read_bytes(), b"previous definition\n")
                self.assertFalse(any("/Delete" in argv for argv in runner.calls))

    def test_uninstall_keeps_definition_when_post_stop_state_is_unknown(self):
        cases = (("mac", {"print": (5, "")}),
                 ("windows", {"-Command": (0, "Unknown\n")}))
        for kind, results in cases:
            with self.subTest(kind=kind):
                runner = self.windows_runner(results) if kind == "windows" else FakeRunner(results)
                manager = self.manager(kind, runner)
                path = self.existing(manager)
                with self.assertRaisesRegex(ServiceError, "definition.*kept.*stop"):
                    manager.uninstall()
                self.assertTrue(path.exists())

    def test_mac_uninstall_keeps_definition_for_every_successful_print(self):
        for output in ("state = waiting\n", "state = exited\n", "state = not running\n",
                       "Could not find service\n"):
            with self.subTest(output=output):
                runner = FakeRunner({"print": (0, output)})
                manager = self.manager("mac", runner)
                path = self.existing(manager)
                with self.assertRaisesRegex(ServiceError, "definition.*kept.*launchctl bootout"):
                    manager.uninstall()
                self.assertEqual(path.read_bytes(), b"previous definition\n")

    def test_mac_uninstall_accepts_nonzero_missing_service_message(self):
        runner = FakeRunner({"bootout": (5, ""), "print": (5, "Could not find service")})
        manager = self.manager("mac", runner)
        path = self.existing(manager)
        manager.uninstall()
        self.assertFalse(path.exists())

    def test_failed_stop_can_remove_definition_after_confirmed_stopped_query(self):
        cases = (("mac", {"bootout": (5, ""), "print": (113, "")}),
                 ("windows", {"/End": (1, ""), "-Command": (0, "Ready\n")}))
        for kind, results in cases:
            with self.subTest(kind=kind):
                runner = self.windows_runner(results) if kind == "windows" else FakeRunner(results)
                manager = self.manager(kind, runner)
                path = self.existing(manager)
                manager.uninstall()
                self.assertFalse(path.exists())
                verb = {"mac": "print", "windows": "-Command"}[kind]
                self.assertTrue(any(verb in argv for argv in runner.calls))

    def test_never_installed_uninstall_succeeds_on_each_platform(self):
        cases = (("mac", {"bootout": (113, ""), "print": (113, "")}),
                 ("windows", {"-Command": (3, "")}))
        for kind, results in cases:
            with self.subTest(kind=kind):
                runner = self.windows_runner(results) if kind == "windows" else FakeRunner(results)
                self.assertEqual(self.manager(kind, runner).uninstall(), ["no agiw service was installed"])

    def test_windows_absent_task_removes_stale_xml_without_deleting_a_task(self):
        runner = self.windows_runner({"-Command": (3, "")})
        manager = self.manager("windows", runner)
        path = self.existing(manager)
        manager.uninstall()
        self.assertFalse(path.exists())
        self.assertFalse(any("/Delete" in argv for argv in runner.calls))

    def test_stop_timeout_is_followed_by_query_and_unknown_query_keeps_definition(self):
        for kind, stop, query, inactive in (("mac", "bootout", "print", (113, "")),
                                           ("windows", "/End", "-Command", (0, "Ready\n"))):
            for query_fails in (False, True):
                with self.subTest(kind=kind, query_fails=query_fails):
                    fake = self.windows_runner() if kind == "windows" else FakeRunner()
                    stopped = []

                    def runner(argv):
                        if stop in argv:
                            fake.calls.append(list(argv))
                            stopped.append(True)
                            raise subprocess.TimeoutExpired(argv, 1)
                        if query in argv:
                            fake.calls.append(list(argv))
                            if stopped and query_fails:
                                raise subprocess.TimeoutExpired(argv, 1)
                            code, output = inactive
                            return subprocess.CompletedProcess(argv, code, output, "")
                        return fake(argv)

                    manager = self.manager(kind, runner)
                    path = self.existing(manager)
                    if query_fails:
                        with self.assertRaisesRegex(ServiceError, "definition.*kept.*stop"):
                            manager.uninstall()
                        self.assertTrue(path.exists())
                    else:
                        manager.uninstall()
                        self.assertFalse(path.exists())
                    self.assertTrue(stopped)
                    self.assertTrue(any(query in argv for argv in fake.calls))

    def rollback_runner(self, rollback_code=0, print_code=0):
        calls = []
        bootstraps = []

        def runner(argv):
            calls.append(list(argv))
            code, output = 0, ""
            if "bootstrap" in argv:
                bootstraps.append(self.manager("mac", runner).definition_path().read_bytes())
                code = rollback_code if bootstraps[-1] == b"previous definition\n" else 5
            if "print" in argv:
                code, output = print_code, "state = running\n" if print_code == 0 else ""
            return subprocess.CompletedProcess(argv, code, output, "bootstrap rejected" if code else "")

        return runner, calls, bootstraps

    def test_mac_failed_replacement_restores_and_verifies_old_definition(self):
        runner, calls, bootstraps = self.rollback_runner()
        manager = self.manager("mac", runner)
        path = self.existing(manager)
        with mock.patch("agiw.service.time.sleep"), self.assertRaisesRegex(ServiceError, "rolled back"):
            manager.install()
        self.assertEqual(path.read_bytes(), b"previous definition\n")
        self.assertEqual(bootstraps[-1], b"previous definition\n")
        self.assertIn(["/bin/launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"], calls)

    def test_mac_bootout_exceptions_restore_and_verify_previous_definition(self):
        for error in (subprocess.TimeoutExpired("launchctl", 60), OSError("bootout unavailable")):
            with self.subTest(error=error):
                rollback_runner, calls, bootstraps = self.rollback_runner()

                def runner(argv):
                    if "bootout" in argv and not any("bootout" in call for call in calls):
                        calls.append(list(argv))
                        raise error
                    return rollback_runner(argv)

                manager = self.manager("mac", runner)
                path = self.existing(manager)
                with self.assertRaisesRegex(ServiceError, "rolled back to the previous loaded definition"):
                    manager.install()
                self.assertEqual(path.read_bytes(), b"previous definition\n")
                self.assertEqual(bootstraps, [b"previous definition\n"])
                self.assertIn(["/bin/launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"], calls)

    def test_mac_bootout_exception_does_not_claim_rollback_without_loaded_query(self):
        rollback_runner, calls, _ = self.rollback_runner(print_code=113)

        def runner(argv):
            if "bootout" in argv and not any("bootout" in call for call in calls):
                calls.append(list(argv))
                raise OSError("bootout unavailable")
            return rollback_runner(argv)

        manager = self.manager("mac", runner)
        path = self.existing(manager)
        with self.assertRaisesRegex(ServiceError, "rollback failed.*loading is unconfirmed") as raised:
            manager.install()
        self.assertNotIn("rolled back", str(raised.exception))
        self.assertEqual(path.read_bytes(), b"previous definition\n")
        self.assertIn(["/bin/launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"], calls)

    def test_mac_rollback_requires_bootstrap_and_print_success(self):
        for bootstrap_code, print_code in ((5, 0), (0, 113)):
            with self.subTest(bootstrap_code=bootstrap_code, print_code=print_code):
                runner, _, _ = self.rollback_runner(bootstrap_code, print_code)
                manager = self.manager("mac", runner)
                path = self.existing(manager)
                with mock.patch("agiw.service.time.sleep"), self.assertRaisesRegex(ServiceError, "rollback failed"):
                    manager.install()
                self.assertEqual(path.read_bytes(), b"previous definition\n")

    def test_mac_rollback_reports_failed_definition_restoration(self):
        runner, _, bootstraps = self.rollback_runner()
        manager = self.manager("mac", runner)
        path = self.existing(manager)
        original_write = type(path).write_bytes

        def write_bytes(target, data):
            if target == path and data == b"previous definition\n":
                raise PermissionError("definition is no longer writable")
            return original_write(target, data)

        with mock.patch("agiw.service.time.sleep"), mock.patch.object(type(path), "write_bytes", write_bytes):
            with self.assertRaisesRegex(ServiceError, "rollback failed.*definition restoration incomplete"):
                manager.install()
        self.assertNotIn(b"previous definition\n", bootstraps)

    def test_windows_run_failure_reports_installed_task_and_reason(self):
        runner = self.windows_runner({"/Run": (5, "Access denied by scheduler")})
        manager = self.manager("windows", runner)
        with self.assertRaisesRegex(ServiceError, "installed but did not start.*Access denied"):
            manager.install()
        self.assertTrue(manager.definition_path().exists())
        self.assertTrue(any("/Create" in argv for argv in runner.calls))


if __name__ == "__main__":
    unittest.main()
