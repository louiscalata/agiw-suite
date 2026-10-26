"""Unit-state evidence stays distinct from process presence and service health."""
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agiw_linux import collect, probes


V8_RUNTIME = "linux-scheduler-authority-v8-jev114-qwen35-boot-v1"
V7_RUNTIME = "linux-scheduler-authority-v7"
V8_SHA = "85adaf746dd8775029d9505eadca04eb0282d9af56d056f2fce9004a58c43e9d"
V7_SHA = "dd29f5df43c51460a23a7d8a5765d3048aa2aff237daa85e9e2db5310a932cdf"


def unit(load="loaded", enabled="disabled", active="inactive", sub="dead", *,
         runtime=V8_RUNTIME, release=V8_SHA, main_pid=None):
    script = "coordinator_v8.py" if runtime == V8_RUNTIME else "coordinator.py"
    upgrade = " --upgrade-directory=/home/fixture/.local/state/upgrade --upgrade-binding-sha256=" + "a" * 64 \
        if runtime == V8_RUNTIME else ""
    command = ("{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -B "
               f"/home/fixture/.local/share/agiw-runtimes/{runtime}/{script} "
               f"serve --release-sha256={release}{upgrade} ; ignore_errors=no ; pid=0 ; status=0/0 }}")
    return {"Id": "agiw-coordinator.service", "LoadState": load,
            "UnitFileState": enabled, "ActiveState": active, "SubState": sub,
            "MainPID": str(main_pid if main_pid is not None else 101 if active == "active" else 0),
            "ExecStart": "" if load == "not-found" else command}


def encoded(value):
    return "".join(f"{key}={item}\n" for key, item in value.items()).encode()


ABSENT = unit("not-found", "", "inactive", "dead")


class CoordinatorUnitTests(unittest.TestCase):
    def test_disabled_user_unit_is_preserved_alongside_missing_system_unit(self):
        result = probes.coordinator_status([], units={"user": unit(runtime=V7_RUNTIME, release=V7_SHA), "system": ABSENT})
        self.assertEqual(result["state"], "absent")
        self.assertEqual(result["health"], "down")
        self.assertEqual(result["units"]["user"]["state"], "disabled")
        self.assertEqual(result["units"]["system"]["state"], "absent")
        self.assertEqual(result["intendedUnit"]["scope"], "system")
        self.assertEqual(result["intendedUnit"]["runtime"], V8_RUNTIME)
        self.assertEqual(result["intendedUnit"]["releaseSha256"], V8_SHA)
        self.assertEqual(result["units"]["user"]["runtime"], V7_RUNTIME)
        self.assertEqual(result["units"]["user"]["releaseSha256"], V7_SHA)

    def test_absent_requires_complete_negative_unit_evidence(self):
        result = probes.coordinator_status([], units={"user": ABSENT, "system": ABSENT})
        self.assertEqual((result["state"], result["health"]), ("absent", "down"))
        for partial in (None, {}, {"user": ABSENT}, {"user": ABSENT, "system": None},
                        {"user": unit(), "system": None}):
            with self.subTest(partial=partial):
                result = probes.coordinator_status([], units=partial)
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_active_requires_complete_valid_unit_evidence(self):
        active = unit("loaded", "disabled", "active", "running")
        process = {"pid": 101, "coordinator": True, "coordinatorRuntime": V8_RUNTIME,
                   "coordinatorReleaseSha256": V8_SHA}
        result = probes.coordinator_status([process], units={"user": ABSENT, "system": active})
        self.assertEqual((result["state"], result["health"]), ("active", "up"))
        for other in (None, {"LoadState": "not-found"}):
            result = probes.coordinator_status([process], units={"user": ABSENT, "system": other})
            self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_active_unit_requires_observed_main_pid_runtime_and_release(self):
        active = unit(active="active", sub="running")
        for runtime, release in ((V7_RUNTIME, V7_SHA), (V7_RUNTIME, V8_SHA),
                                 (V8_RUNTIME, V7_SHA), (V8_RUNTIME, None), (None, V8_SHA)):
            with self.subTest(runtime=runtime, release=release):
                process = {"pid": 101, "coordinator": True, "coordinatorRuntime": runtime,
                           "coordinatorReleaseSha256": release}
                result = probes.coordinator_status([process], units={"user": ABSENT, "system": active})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["reason"], "process-unit-state-conflict")
                self.assertEqual(result["units"]["system"]["state"], "active")

    def test_active_unit_requires_matching_integer_main_pid(self):
        active = unit(active="active", sub="running")
        for pid in (None, 0, 102, "101", True):
            with self.subTest(pid=pid):
                process = {"pid": pid, "coordinator": True, "coordinatorRuntime": V8_RUNTIME,
                           "coordinatorReleaseSha256": V8_SHA}
                result = probes.coordinator_status([process], units={"user": ABSENT, "system": active})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["reason"], "process-unit-state-conflict")

    def test_active_unit_without_observed_coordinator_stays_unknown(self):
        active = unit(active="active", sub="running")
        for processes in ([], [{"pid": 101, "name": "unrelated"}], None):
            with self.subTest(processes=processes):
                result = probes.coordinator_status(processes, units={"user": ABSENT, "system": active})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_active_intended_and_retired_processes_are_both_accounted_for(self):
        intended = {"pid": 101, "coordinator": True, "coordinatorRuntime": V8_RUNTIME,
                    "coordinatorReleaseSha256": V8_SHA}
        retired = {"pid": 102, "coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                   "coordinatorReleaseSha256": V7_SHA}
        units = {"user": unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA, main_pid=102),
                 "system": unit(active="active", sub="running")}
        result = probes.coordinator_status([intended, retired], units=units)
        self.assertEqual((result["state"], result["health"]), ("active", "up"))
        # A matching retired unit cannot explain away the wrong process at v8's MainPID.
        units["user"] = dict(units["user"], MainPID="101")
        result = probes.coordinator_status([dict(retired, pid=101)], units=units)
        self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_active_unit_does_not_hide_extra_or_conflicting_coordinator_process(self):
        intended = {"pid": 101, "coordinator": True, "coordinatorRuntime": V8_RUNTIME,
                    "coordinatorReleaseSha256": V8_SHA}
        units = {"user": ABSENT, "system": unit(active="active", sub="running")}
        for extra in ({"pid": 102, "coordinator": True}, dict(intended, pid=102),
                      dict(intended, coordinatorRuntime=V7_RUNTIME, coordinatorReleaseSha256=V7_SHA)):
            with self.subTest(extra=extra):
                result = probes.coordinator_status([intended, extra], units=units)
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["reason"], "process-unit-state-conflict")

    def test_intended_system_unit_can_be_disabled(self):
        result = probes.coordinator_status([], units={"user": ABSENT, "system": unit()})
        self.assertEqual((result["state"], result["health"]), ("disabled", "down"))

    def test_wrong_runtime_or_release_cannot_report_intended_unit_active(self):
        for runtime, release in ((V7_RUNTIME, V7_SHA), (V8_RUNTIME, V7_SHA),
                                 (V7_RUNTIME, V8_SHA), (V8_RUNTIME, "invalid"), (V8_RUNTIME, "85adaf74" + "0" * 56)):
            with self.subTest(runtime=runtime, release=release):
                active = unit(active="active", sub="running", runtime=runtime, release=release)
                result = probes.coordinator_status([], units={"user": ABSENT, "system": active})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["units"]["system"]["state"], "active")

    def test_unit_command_is_not_exported_and_ambiguous_identity_is_unknown(self):
        for suffix in (" extra-secret", " --release-sha256=" + V7_SHA):
            value = unit(active="active", sub="running")
            value["ExecStart"] = value["ExecStart"].replace(" ; ignore_errors", suffix + " ; ignore_errors")
            result = probes.coordinator_status([], units={"user": ABSENT, "system": value})
            self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
            self.assertNotIn("extra-secret", str(result))
            self.assertNotIn("ExecStart", str(result))
            self.assertNotIn("/home/fixture", str(result))

    def test_unit_from_wrong_runtime_root_or_entrypoint_is_unknown(self):
        for old, new in (("/home/fixture/.local/share/agiw-runtimes/", "/opt/rogue/agiw-runtimes/"),
                         ("/home/fixture/.local/share/agiw-runtimes/", "/etc/agiw-runtimes/"),
                         ("/home/fixture/.local/share/agiw-runtimes/", "/home/../.local/share/agiw-runtimes/"),
                         ("/coordinator_v8.py ", "/coordinator.py "),
                         ("/usr/bin/python3 -B ", "/usr/bin/python3 ")):
            with self.subTest(replacement=new):
                value = unit(active="active", sub="running")
                value["ExecStart"] = value["ExecStart"].replace(old, new)
                result = probes.coordinator_status([], units={"user": ABSENT, "system": value})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertIsNone(result["units"]["system"]["runtime"])

    def test_missing_unit_with_a_configured_command_is_conflicting_evidence(self):
        value = dict(ABSENT, ExecStart=unit()["ExecStart"])
        result = probes.coordinator_status([], units={"user": ABSENT, "system": value})
        self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_retired_user_unit_never_supplies_intended_active_state(self):
        active = unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA)
        result = probes.coordinator_status([], units={"user": active, "system": ABSENT})
        self.assertEqual((result["state"], result["health"]), ("absent", "down"))
        self.assertEqual(result["units"]["user"]["state"], "active")

    def test_live_retired_process_is_separate_from_intended_system_unit(self):
        active = unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA)
        process = {"pid": 101, "coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                   "coordinatorReleaseSha256": V7_SHA}
        for system, state in ((ABSENT, "absent"), (unit(), "disabled")):
            with self.subTest(system=state):
                result = probes.coordinator_status([process], units={"user": active, "system": system})
                self.assertEqual((result["state"], result["health"]), (state, "down"))
                self.assertTrue(result["processPresent"])
                self.assertEqual(result["units"]["user"]["state"], "active")
                self.assertEqual(result["units"]["user"]["runtime"], V7_RUNTIME)
                self.assertEqual(result["units"]["system"]["state"], state)

    def test_retired_unit_does_not_account_for_unidentified_or_different_process(self):
        active = unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA)
        retired = {"pid": 101, "coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                   "coordinatorReleaseSha256": V7_SHA}
        for extra in ({"coordinator": True}, {"name": "agiw-coordinator"},
                      dict(retired, coordinatorReleaseSha256=V8_SHA),
                      dict(retired, coordinatorRuntime=V8_RUNTIME),
                      dict(retired, coordinatorReleaseSha256=None)):
            with self.subTest(extra=extra):
                result = probes.coordinator_status([retired, extra], units={"user": active, "system": ABSENT})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["reason"], "process-unit-state-conflict")

    def test_inactive_retired_unit_does_not_account_for_live_retired_process(self):
        process = {"coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                   "coordinatorReleaseSha256": V7_SHA}
        for user in (ABSENT, unit(runtime=V7_RUNTIME, release=V7_SHA)):
            with self.subTest(user=user):
                result = probes.coordinator_status([process], units={"user": user, "system": ABSENT})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
                self.assertEqual(result["reason"], "process-unit-state-conflict")

    def test_second_process_with_same_retired_runtime_is_unaccounted(self):
        active = unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA)
        retired = {"pid": 101, "coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                   "coordinatorReleaseSha256": V7_SHA}
        result = probes.coordinator_status([retired, dict(retired, pid=102)],
                                           units={"user": active, "system": ABSENT})
        self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))
        self.assertEqual(result["reason"], "process-unit-state-conflict")

    def test_retired_process_requires_matching_nonzero_main_pid(self):
        active = unit(active="active", sub="running", runtime=V7_RUNTIME, release=V7_SHA)
        for pid in (None, 0, 102, "101", True):
            with self.subTest(pid=pid):
                process = {"pid": pid, "coordinator": True, "coordinatorRuntime": V7_RUNTIME,
                           "coordinatorReleaseSha256": V7_SHA}
                result = probes.coordinator_status([process], units={"user": active, "system": ABSENT})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_unit_main_pid_requires_bounded_decimal_and_consistent_state(self):
        for pid in ("", "-1", "01", "1.0", "2147483648", "9" * 4000, "private", 101, None):
            with self.subTest(pid=pid):
                value = dict(unit(), MainPID=pid)
                self.assertFalse(probes._valid_unit(value))
                result = probes.coordinator_status([], units={"user": ABSENT, "system": value})
                self.assertEqual(result["state"], "unknown")
        for value in (dict(ABSENT, MainPID="101"), unit(main_pid=101),
                      unit(active="active", sub="running", main_pid=0)):
            with self.subTest(value=value):
                self.assertEqual(probes._unit_state(value), "unknown")
        self.assertTrue(probes._valid_unit(unit(main_pid=2147483647)))
        missing = unit()
        del missing["MainPID"]
        self.assertFalse(probes._valid_unit(missing))

    def test_process_presence_is_not_complete_unit_evidence(self):
        result = probes.coordinator_status([{"coordinator": True}])
        self.assertTrue(result["processPresent"])
        self.assertEqual(result["health"], "unknown")
        self.assertEqual(result["state"], "unknown")

    def test_unavailable_process_inventory_keeps_unit_observation_unknown(self):
        result = probes.coordinator_status(None, units={"user": ABSENT, "system": ABSENT})
        self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_conflicting_process_and_absent_units_is_unknown(self):
        result = probes.coordinator_status([{"coordinator": True}], units={"user": ABSENT, "system": ABSENT})
        self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_disabled_enabled_inactive_and_transitions_are_not_active(self):
        for value in (unit("loaded", "enabled"), unit("loaded", "disabled", "activating", "start"),
                      unit("loaded", "disabled", "failed", "failed"), unit("masked", "masked")):
            with self.subTest(value=value):
                result = probes.coordinator_status([], units={"user": ABSENT, "system": value})
                self.assertEqual((result["state"], result["health"]), ("unknown", "unknown"))

    def test_projection_retains_coordinator_state_and_three_state_health(self):
        for state, health in (("active", "up"), ("disabled", "down"), ("absent", "down"), ("unknown", "unknown")):
            snapshot = collect.unknown_snapshot(now=100, host="fixture")
            snapshot["components"] = [{"id": "coordinator", "state": state, "health": health}]
            component = collect.project_status(snapshot)["components"][0]
            self.assertEqual(component["state"], state)
            self.assertEqual(component["health"], health)

    def test_unit_reader_rejects_partial_duplicate_oversized_and_wrong_identity(self):
        valid = encoded(unit())
        invalid = (None, b"", b"LoadState=not-found\n", valid + b"ActiveState=active\n",
                   valid.replace(b"agiw-coordinator", b"foreign"), valid + b"Extra=secret\n",
                   b"x" * 4097, valid.replace(b"disabled", b"invented"), valid + b"\xff")
        for value in invalid:
            with self.subTest(value=value), patch.object(probes, "_unit_query", return_value=value):
                self.assertEqual(probes.coordinator_units(), {"user": None, "system": None})
        with patch.object(probes, "_unit_query", side_effect=[valid, encoded(ABSENT)]):
            self.assertEqual(probes.coordinator_units(), {"user": unit(), "system": ABSENT})

    def command_mocks(self, output, status=0):
        process = MagicMock()
        process.stdout.fileno.return_value = 123
        process.wait.return_value = status
        process.poll.return_value = status
        selector = MagicMock()
        selector.select.return_value = [(object(), 1)]
        return process, selector

    def test_unit_query_uses_fixed_read_only_absolute_command_and_byte_cap(self):
        for scope in ("user", "system"):
            payload = encoded(unit())
            process, selector = self.command_mocks(payload)
            with patch.object(probes.sys, "platform", "linux"), \
                    patch.object(probes.subprocess, "Popen", return_value=process) as start, \
                    patch.object(probes.selectors, "DefaultSelector", return_value=selector), \
                    patch.object(probes.os, "read", side_effect=[payload, b""]) as read:
                self.assertEqual(probes._unit_query(scope), payload)
            command = start.call_args.args[0]
            self.assertEqual(command[:5], ["/usr/bin/systemctl", f"--{scope}", "--no-pager", "show", "agiw-coordinator.service"])
            self.assertEqual(command[5:], ["--all", "--property=Id,LoadState,UnitFileState,ActiveState,SubState,ExecStart,MainPID"])
            self.assertNotIn("shell", start.call_args.kwargs)
            self.assertEqual(start.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertTrue(all(call.args[1] <= 4097 for call in read.call_args_list))
            self.assertLessEqual(process.wait.call_args.kwargs["timeout"], probes.UNIT_TIMEOUT)

    def test_failed_timed_out_and_oversized_commands_are_unknown(self):
        for failure in ("failed", "timeout", "oversized"):
            process, selector = self.command_mocks(b"", status=1 if failure == "failed" else 0)
            if failure == "timeout":
                selector.select.return_value = []
                process.poll.return_value = None
            data = b"x" * 4097 if failure == "oversized" else encoded(unit())
            with self.subTest(failure=failure), patch.object(probes.sys, "platform", "linux"), \
                    patch.object(probes.subprocess, "Popen", return_value=process), \
                    patch.object(probes.selectors, "DefaultSelector", return_value=selector), \
                    patch.object(probes.os, "read", side_effect=[data, b""]):
                self.assertIsNone(probes._unit_query("user"))
            if failure == "timeout":
                process.kill.assert_called_once()

    def test_unit_query_never_executes_on_another_platform_or_scope(self):
        for platform, scope in (("darwin", "user"), ("win32", "system"), ("linux", "other")):
            with patch.object(probes.sys, "platform", platform), patch.object(probes.subprocess, "Popen") as start:
                self.assertIsNone(probes._unit_query(scope))
                start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
