"""Service observation contracts; temporary files and injected commands only."""
import hashlib
import io
import json
import subprocess
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from support import forbid_real_commands, temp_dir
from agiw import cli
from agiw.service import LAUNCHD_LABEL, ServiceManager


SID = "S-1-5-21-111-222-333-1001"


class ObservationBase(unittest.TestCase):
    def setUp(self):
        forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-service-observation-")

    def manager(self, kind, code=0, stdout="", stderr=""):
        def runner(argv):
            if "/user" in argv:
                return subprocess.CompletedProcess(argv, 0, f'"tester","{SID}"\n', "")
            return subprocess.CompletedProcess(argv, code, stdout, stderr)

        return ServiceManager(kind, home=self.home, python="/opt/python3", runner=mock.Mock(side_effect=runner),
                              env={"USERNAME": "tester", "USERDOMAIN": "WORKGROUP"}, uid=501,
                              system_dir="C:\\Windows\\System32")

    def assert_payload(self, manager, installed, loaded, running):
        self.assertEqual(manager.status(), {
            "kind": manager.kind, "definition": str(manager.definition_path()),
            "installed": installed, "loaded": loaded, "running": running,
        })

    def assert_cli_status(self, manager, installed, loaded, running):
        definition = str(Path("~") / manager.definition_path().relative_to(self.home))
        words = {True: "yes", False: "no", None: "unknown"}
        expected_text = [
            f"service ({manager.kind}): {'installed' if installed else 'not installed'}, definition {definition}",
            f"loaded: {words[loaded]}, running: {words[running]}",
        ]
        literals = {True: "true", False: "false", None: "null"}
        expected_json = ('{\n "definition": %s,\n "installed": %s,\n "kind": "%s",\n'
                         ' "loaded": %s,\n "running": %s\n}') % (
                             json.dumps(definition), literals[installed], manager.kind,
                             literals[loaded], literals[running])
        for options, expected in (([], expected_text), (["--json"], [expected_json])):
            with self.subTest(options=options), mock.patch.object(cli, "make_manager", return_value=manager):
                lines, error = [], io.StringIO()
                with mock.patch("sys.stderr", error):
                    code = cli.main(["service", "status", *options], out=lines.append,
                                    kind=manager.kind, home=self.home, env={})
                self.assertEqual((code, lines, error.getvalue()), (0, expected, ""))


class ServiceObservationContracts(ObservationBase):
    def test_mac_payload_and_query_identity_with_and_without_definition(self):
        cases = ((0, "\tstate = running\n", "", True, True),
                 (0, "state = waiting\n", "", True, False),
                 (0, "state = not running\n", "", True, False),
                 (0, "", "", True, False),
                 (0, "Could not find service\n", "", True, False),
                 (113, "", "", False, False),
                 (5, "Could not find service", "", False, False),
                 (5, "", "COULD NOT FIND SERVICE", False, False))
        for code, stdout, stderr, loaded, running in cases:
            for installed in (False, True):
                with self.subTest(code=code, stdout=stdout, stderr=stderr, installed=installed):
                    manager = self.manager("mac", code, stdout, stderr)
                    path = manager.definition_path()
                    if installed:
                        manager._write_definition()
                    self.assert_payload(manager, installed, loaded, running)
                    manager.runner.assert_called_once_with(
                        ["/bin/launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"])
                    if installed:
                        path.unlink()

    def test_windows_payload_for_every_scheduler_state_and_query_failure(self):
        cases = ((0, "Running\n", True, True), (0, "Ready\n", True, False),
                 (0, "Queued\n", True, False), (0, "Disabled\n", True, False),
                 (0, "Unknown\n", True, None), (3, "", False, False),
                 (1, "Ready\n", None, None), (0, "invalid\n", None, None))
        for code, stdout, loaded, running in cases:
            for installed in (False, True):
                with self.subTest(code=code, stdout=stdout, installed=installed):
                    manager = self.manager("windows", code, stdout)
                    if installed:
                        manager._write_definition()
                    self.assert_payload(manager, installed, loaded, running)
                    self.assertEqual(manager.windows_task(), "\\AGIW\\PeerLink-" + SID)
                    self.assertEqual(manager.runner.call_args_list[-1].args[0], manager._windows_state_command())
                    if installed:
                        manager.definition_path().unlink()

    def test_cli_status_bytes_and_exit_codes(self):
        cases = (("mac", 0, "state = running\n", True, True),
                 ("mac", 0, "state = exited\n", True, False),
                 ("mac", 113, "", False, False),
                 ("windows", 0, "Running\n", True, True),
                 ("windows", 0, "Ready\n", True, False),
                 ("windows", 0, "Unknown\n", True, None),
                 ("windows", 1, "", None, None))
        for kind, code, stdout, loaded, running in cases:
            for installed in (False, True):
                with self.subTest(kind=kind, stdout=stdout, installed=installed):
                    manager = self.manager(kind, code, stdout)
                    if installed:
                        manager._write_definition()
                    self.assert_cli_status(manager, installed, loaded, running)
                    if installed:
                        manager.definition_path().unlink()

    def test_definition_bytes_and_status_are_read_only(self):
        hashes = {"mac": "ab062fcda311d1534a736dd8b28b5fed3ca17e0115922e06c52eaf0b5f42274d",
                  "windows": "0d26dc97e30c40c3b5de00dd8ebc0b433b352e624ba8a0e253a268e1e20de059"}
        for kind, digest in hashes.items():
            with self.subTest(kind=kind):
                manager = self.manager(kind, 0, "state = running\n" if kind == "mac" else "Running\n")
                manager.home = PurePosixPath("/agiw-service-fixture")  # render-only fixture, also on Windows
                manager.serve_args = ["--state-dir", "/agiw-state"]
                manager.serve_options = ["--name", "fixture"]
                self.assertEqual(hashlib.sha256(manager.render()).hexdigest(), digest)
                manager.home = self.home
                self.assert_payload(manager, False, True, True)
                path = manager._write_definition()
                saved = path.read_bytes()
                self.assertEqual(saved, manager.render())
                before = sorted(p.relative_to(self.home) for p in self.home.rglob("*"))
                self.assert_payload(manager, True, True, True)
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual(sorted(p.relative_to(self.home) for p in self.home.rglob("*")), before)

    def test_mac_query_exceptions_keep_existing_error_contract(self):
        manager = self.manager("mac")
        manager.runner.side_effect = PermissionError("query denied")
        with self.assertRaisesRegex(PermissionError, "query denied"):
            manager.status()
        lines, error = [], io.StringIO()
        with mock.patch.object(cli, "make_manager", return_value=manager), mock.patch("sys.stderr", error):
            code = cli.main(["service", "status"], out=lines.append, kind="mac", home=self.home, env={})
        self.assertEqual((code, lines, error.getvalue()), (1, [], "agiw: PermissionError: query denied\n"))
        manager.runner.side_effect = subprocess.TimeoutExpired("launchctl", 60)
        with self.assertRaises(subprocess.TimeoutExpired):
            manager.status()


class ServiceObservationRegressions(ObservationBase):
    def test_mac_failed_query_is_unknown_with_or_without_definition(self):
        for code, stdout, stderr in ((5, "", "Operation not permitted"),
                                     (1, "state = running\n", "query failed"),
                                     (3, "", ""), (-9, "", "")):
            for installed in (False, True):
                with self.subTest(code=code, installed=installed):
                    manager = self.manager("mac", code, stdout, stderr)
                    if installed:
                        manager._write_definition()
                    try:
                        self.assert_payload(manager, installed, None, None)
                        if installed:
                            self.assertEqual(manager.definition_path().read_bytes(), manager.render())
                    finally:
                        if installed:
                            manager.definition_path().unlink()

    def test_mac_failed_query_cli_reports_unknown_and_json_null(self):
        manager = self.manager("mac", 5, stderr="Operation not permitted")
        manager._write_definition()
        self.assert_cli_status(manager, True, None, None)

    def test_new_query_replaces_previous_observation_without_changing_definition(self):
        manager = self.manager("mac")
        path = manager._write_definition()
        saved = path.read_bytes()
        manager.runner.side_effect = [
            subprocess.CompletedProcess([], 0, "state = running\n", ""),
            subprocess.CompletedProcess([], 5, "", "Operation not permitted"),
            subprocess.CompletedProcess([], 113, "", ""),
            subprocess.CompletedProcess([], 0, "state = waiting\n", ""),
            subprocess.CompletedProcess([], 0, "state = running\n", ""),
        ]
        for loaded, running in ((True, True), (None, None), (False, False), (True, False)):
            self.assert_payload(manager, True, loaded, running)
            self.assertEqual(path.read_bytes(), saved)
        path.unlink()  # a loaded job can outlive its definition
        self.assert_payload(manager, False, True, True)


if __name__ == "__main__":
    unittest.main()
