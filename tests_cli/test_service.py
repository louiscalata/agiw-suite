"""Service definitions are rendered into a temporary home and every command goes to a fake runner; any real
process start (launchctl, systemctl, schtasks) fails these tests."""
import plistlib
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from support import FakeRunner, forbid_real_commands, temp_dir

from agiw import cli
from agiw.service import LAUNCHD_LABEL, SYSTEMD_UNIT, WINDOWS_TASK, ServiceManager, _systemd_quote

TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
ELEVATION_WORDS = ("sudo", "runas", "HighestAvailable", "/RL", "HIGHEST", "LaunchDaemons", "--system", "SYSTEM")
TEST_SID = "S-1-5-21-1000"
TEST_TASK = WINDOWS_TASK + "-" + TEST_SID
WHOAMI = (0, f'"WORKGROUP\\tester","{TEST_SID}"\n')


class Base(unittest.TestCase):
    def setUp(self):
        self.real_runner = forbid_real_commands(self)
        self.home = temp_dir(self, "agiw-home-")

    def tearDown(self):
        self.assertFalse(self.real_runner.called, "the real command runner was used")

    def manager(self, kind, runner, *args, **kw):
        env = kw.pop("env", {"USERNAME": "tester", "USERDOMAIN": "WORKGROUP"})
        return ServiceManager(kind, *args, home=self.home, python="/opt/py/bin/python3", runner=runner, env=env,
                              uid=501, system_dir="C:\\Windows\\System32", **kw)

    def assert_user_level(self, runner):
        for argv in runner.calls:
            for word in ELEVATION_WORDS:
                self.assertNotIn(word, " ".join(argv), argv)

    def files_under_home(self):
        return sorted(p.relative_to(self.home).as_posix() for p in self.home.rglob("*") if p.is_file())


class Launchd(Base):
    def test_install_renders_a_user_launch_agent_and_bootstraps_it(self):
        runner = FakeRunner()
        steps = self.manager("mac", runner).install()
        path = self.home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/opt/py/bin/python3", "-m", "agiw", "serve", "--service"])
        self.assertEqual((plist["Label"], plist["RunAtLoad"], plist["KeepAlive"]),
                         (LAUNCHD_LABEL, True, {"SuccessfulExit": False}))
        self.assertTrue(plist["StandardErrorPath"].startswith(str(self.home)))
        self.assertEqual(runner.calls, [["/bin/launchctl", "bootout", f"gui/501/{LAUNCHD_LABEL}"],
                                        ["/bin/launchctl", "bootstrap", "gui/501", str(path)]])
        self.assertEqual(self.files_under_home(), [f"Library/LaunchAgents/{LAUNCHD_LABEL}.plist"])
        self.assert_user_level(runner)
        self.assertEqual(len(steps), 2)

    def test_bootstrap_is_retried_once_after_replacing_a_running_copy(self):
        attempts = []

        def runner(argv):
            if "bootstrap" in argv:
                attempts.append(argv)
                return FakeRunner(default=(5 if len(attempts) == 1 else 0, ""))(argv)
            return FakeRunner()(argv)

        with mock.patch("agiw.service.time.sleep") as sleep:
            self.manager("mac", runner).install()
        self.assertEqual(len(attempts), 2)
        sleep.assert_called_once()

    def test_status_and_uninstall(self):
        runner = FakeRunner({"print": (0, "gui/501/x = {\n\tstate = running\n}\n")})
        manager = self.manager("mac", runner)
        manager.install()
        self.assertEqual(manager.status(), {"kind": "mac", "definition": str(manager.definition_path()),
                                            "installed": True, "loaded": True, "running": True})
        runner.results["print"] = (113, "")  # bootout has removed the loaded job
        manager.uninstall()
        self.assertFalse(manager.definition_path().exists())
        self.assertEqual(runner.calls[-1], ["/bin/launchctl", "print", f"gui/501/{LAUNCHD_LABEL}"])
        stopped = self.manager("mac", FakeRunner({"print": (113, "")}))
        self.assertEqual((stopped.status()["installed"], stopped.status()["loaded"]), (False, False))
        self.assert_user_level(runner)


class Systemd(Base):
    def test_install_renders_a_user_unit_and_enables_it(self):
        runner = FakeRunner({"is-active": (0, "active\n")})
        manager = self.manager("linux", runner, ["--state-dir", "/srv/agiw state"], ["--name", "lab 100%"])
        manager.install()
        path = self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
        text = path.read_text()
        self.assertIn('ExecStart="/opt/py/bin/python3" "-m" "agiw" "--state-dir" "/srv/agiw state" "serve" '
                      '"--name" "lab 100%%"\n', text)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("Restart=on-failure", text)
        systemctl = runner.calls[0][0]
        self.assertTrue(systemctl.endswith("systemctl"))
        self.assertEqual(runner.calls, [[systemctl, "--user", "daemon-reload"],
                                        [systemctl, "--user", "enable", SYSTEMD_UNIT],
                                        [systemctl, "--user", "restart", SYSTEMD_UNIT],
                                        [systemctl, "--user", "is-active", SYSTEMD_UNIT]])
        for argv in runner.calls:
            self.assertEqual(argv[1], "--user")
        self.assert_user_level(runner)

    def test_xdg_config_home_is_respected_when_absolute(self):
        config = temp_dir(self, "agiw-config-")
        manager = self.manager("linux", FakeRunner(), env={"XDG_CONFIG_HOME": str(config)})
        self.assertEqual(manager.definition_path(), config / "systemd" / "user" / SYSTEMD_UNIT)
        relative = self.manager("linux", FakeRunner(), env={"XDG_CONFIG_HOME": "rel"})
        self.assertEqual(relative.definition_path(), self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT)

    def test_status_and_uninstall(self):
        runner = FakeRunner({"is-enabled": (0, "enabled\n"), "is-active": (0, "active\n")})
        manager = self.manager("linux", runner)
        manager.install()
        state = manager.status()
        self.assertEqual((state["installed"], state["loaded"], state["running"]), (True, True, True))
        runner.results["is-active"] = (3, "inactive\n")  # disable --now stopped it
        manager.uninstall()
        self.assertFalse(manager.definition_path().exists())
        self.assertIn([runner.calls[0][0], "--user", "disable", "--now", SYSTEMD_UNIT], runner.calls)
        self.assert_user_level(runner)


class TaskScheduler(Base):
    def test_install_registers_a_least_privilege_logon_task_for_this_user(self):
        runner = FakeRunner({"/user": WHOAMI})
        manager = self.manager("windows", runner, ["--state-dir", "C:\\Users\\tester\\My State"],
                               ["--status-file", "C:\\AGIW\\state\\peer-link-status.json"])
        manager.install()
        path = manager.definition_path()
        self.assertEqual(path, self.home / "AppData" / "Local" / "AGIW" / "service" / "agiw-peer-link.xml")
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"), "schtasks needs UTF-16")
        root = ET.fromstring(raw.decode("utf-16").split("?>", 1)[1])
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", TASK_NS).text, "WORKGROUP\\tester")
        self.assertEqual(root.find("t:Principals/t:Principal/t:RunLevel", TASK_NS).text, "LeastPrivilege")
        self.assertEqual(root.find("t:Principals/t:Principal/t:LogonType", TASK_NS).text, "InteractiveToken")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Command", TASK_NS).text, "/opt/py/bin/python3")
        self.assertEqual(root.find("t:Actions/t:Exec/t:Arguments", TASK_NS).text,
                         '-m agiw --state-dir "C:\\Users\\tester\\My State" serve '
                         '--status-file C:\\AGIW\\state\\peer-link-status.json')
        schtasks = "C:\\Windows\\System32\\schtasks.exe"
        self.assertEqual(runner.calls, [["C:\\Windows\\System32\\whoami.exe", "/user", "/fo", "csv", "/nh"],
                                        [schtasks, "/Create", "/TN", TEST_TASK, "/XML", str(path), "/F"],
                                        [schtasks, "/Run", "/TN", TEST_TASK]])
        self.assert_user_level(runner)

    def test_status_and_uninstall(self):
        runner = FakeRunner({"/user": WHOAMI, "-Command": (0, "Running\n")})
        manager = self.manager("windows", runner)
        manager.install()
        state = manager.status()
        self.assertEqual((state["installed"], state["loaded"], state["running"]), (True, True, True))
        runner.results["-Command"] = (0, "Ready\n")  # /End has stopped the task
        manager.uninstall()
        self.assertFalse(manager.definition_path().exists())
        self.assertIn(["C:\\Windows\\System32\\schtasks.exe", "/Delete", "/TN", TEST_TASK, "/F"], runner.calls)
        self.assert_user_level(runner)


class ThroughTheCli(Base):
    def run_cli(self, kind, *argv, runner=None):
        lines = []
        code = cli.main(list(argv), out=lines.append, runner=runner or FakeRunner(), kind=kind, home=self.home,
                        env={"USERNAME": "tester"})
        return code, lines

    def test_service_install_status_uninstall_on_each_kind(self):
        for kind in ("mac", "linux", "windows"):
            runner = FakeRunner({"/user": WHOAMI, "is-active": (0, "active\n"), "print": (113, ""),
                                 "-Command": (0, "Ready\n")})
            extra = ["--status-file", str(self.home / "status.json")] if kind == "windows" else []
            code, lines = self.run_cli(kind, "service", "install", *extra, runner=runner)
            self.assertEqual(code, 0, (kind, lines))
            self.assertTrue(runner.calls, kind)
            self.assertTrue(any(line.startswith("wrote ~") for line in lines), lines)
            self.assertFalse(any(str(self.home) in line for line in lines), lines)
            code, lines = self.run_cli(kind, "service", "status", runner=runner)
            self.assertEqual(code, 0)
            runner.results["is-active"] = (3, "inactive\n")
            code, lines = self.run_cli(kind, "uninstall", runner=runner)
            self.assertEqual(code, 0)
            self.assertEqual(self.files_under_home(), [])
            self.assert_user_level(runner)

    def test_windows_install_without_a_status_source_writes_nothing(self):
        runner = FakeRunner()
        with mock.patch("sys.stderr"):
            code, _ = self.run_cli("windows", "service", "install", runner=runner)
        self.assertEqual(code, 2)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.files_under_home(), [])

    def test_custom_state_dir_and_options_reach_the_service_command(self):
        runner = FakeRunner({"is-active": (0, "active\n")})
        state = self.home / "custom state"
        code, _ = self.run_cli("linux", "--state-dir", str(state), "service", "install", "--name", "lab",
                               runner=runner)
        self.assertEqual(code, 0)
        text = (self.home / ".config" / "systemd" / "user" / SYSTEMD_UNIT).read_text()
        self.assertIn(f'"--state-dir" {_systemd_quote(str(state.resolve()))} "serve" "--name" "lab"', text)


if __name__ == "__main__":
    unittest.main()
