"""Exercise the real embedded installer against fixtures; never start a service."""
import contextlib
import time
import io
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


LINUX = Path(__file__).resolve().parents[1]
SCRIPT = LINUX / "install.sh"
FIRST = "a" * 40
SECOND = "b" * 40
BODY = SCRIPT.read_text().split("<<'PY_INSTALLER'\n", 1)[1].rsplit("\nPY_INSTALLER", 1)[0]
INSTALLER = {"__name__": "fixture_installer"}
exec(compile(BODY, str(SCRIPT), "exec"), INSTALLER)
UNVERIFIED_IDLE_ALLOWANCE = INSTALLER["verify_idle_observer_allowance"]
PREPARE_IDLE_ROLLBACK = INSTALLER.get("prepare_idle_rollback")


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.source = self.root / "source"
        self.home.mkdir(mode=0o700)
        self.source.mkdir()
        (self.source / "agiw_linux").mkdir()
        (self.source / "agiw-linux-status").write_text("raise AssertionError('must never execute observer')\n")
        (self.source / "agiw-linux-once").write_text("#!/bin/sh\nexit 99\n")
        for name in INSTALLER["MODULES"]:
            (self.source / "agiw_linux" / name).write_text("raise AssertionError('must never import module')\n")
        (self.source / "agiw_linux/idle_allowance.py").write_bytes((LINUX / "agiw_linux/idle_allowance.py").read_bytes())
        (self.source / INSTALLER["UNIT"]).write_text((LINUX / INSTALLER["UNIT"]).read_text())
        self.base = self.home / ".local/share/agiw-linux-observer"
        self.unit = self.home / ".config/systemd/user" / INSTALLER["UNIT"]
        self.calls = []
        self.enabled = False
        self.active = False
        self.fail_action = None
        self.git_head = None
        self.git_dirty = ""
        self.blobs = {}
        self.signed = True
        self.publish = True
        self.heartbeat_commit = None
        self.idle_properties = None
        self.idle_pairings = {}
        self.patches = [
            # Transaction fixtures model the future verified prerequisite; the
            # production guard is exercised separately and never accepts a flag.
            mock.patch.dict(INSTALLER, {"verify_idle_observer_allowance": mock.Mock()}),
            mock.patch.dict(INSTALLER, {"prepare_idle_rollback": mock.Mock(return_value=None)}),
            mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True),
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(subprocess, "run", side_effect=self.command),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "/usr/bin/git":
            if "rev-parse" in argv:
                value = "linux/" if "--show-prefix" in argv else self.git_head or ""
                return subprocess.CompletedProcess(argv, 0 if self.git_head else 128, value + "\n", "")
            if "verify-commit" in argv:
                return subprocess.CompletedProcess(argv, 0 if self.signed else 1, b"", b"")
            if "show" in argv:
                content = self.blobs.get(argv[-1])
                return subprocess.CompletedProcess(argv, 0 if content is not None else 128, content or b"", b"")
            return subprocess.CompletedProcess(argv, 0, self.git_dirty, "")
        self.assertEqual(argv[:2], ["/usr/bin/systemctl", "--user"])
        action = argv[2]
        if self.fail_action == action:
            self.fail_action = None
            # An activation failure can occur after partial state changes.
            if action == "enable":
                self.enabled = self.active = True
            return subprocess.CompletedProcess(argv, 1, "", "injected failure")
        if action == "show" and "agiw-idle-analysis.service" in argv:
            if self.idle_properties is None:
                return subprocess.CompletedProcess(argv, 1, "", "not loaded")
            return subprocess.CompletedProcess(argv, 0, "\n".join(
                key + "=" + value for key, value in self.idle_properties.items()) + "\n", "")
        if action == "show":
            if any(arg.startswith('--property=LoadState,ActiveState,SubState,MainPID') for arg in argv):
                value = {'LoadState': 'loaded' if self.unit.exists() else 'not-found',
                         'ActiveState': 'active' if self.active else 'inactive',
                         'SubState': 'running' if self.active else 'dead',
                         'MainPID': '123' if self.active else '0'}
                return subprocess.CompletedProcess(argv, 0, ''.join(k + '=' + v + '\n' for k, v in value.items()), '')
            if any(arg.startswith('--property=MainPID,ActiveState,SubState') for arg in argv):
                return subprocess.CompletedProcess(argv, 0, ''.join(k + '=' + v + '\n' for k, v in self.observer_properties.items()), '')
            value = str(self.unit) if "--property=FragmentPath" in argv else ""
            return subprocess.CompletedProcess(argv, 0, value + "\n", "")
        if action == "is-enabled":
            return subprocess.CompletedProcess(argv, 0 if self.enabled else 1,
                                               "enabled\n" if self.enabled else "disabled\n", "")
        if action == "is-active":
            return subprocess.CompletedProcess(argv, 0 if self.active else 3,
                                               "active\n" if self.active else "inactive\n", "")
        if action == "enable":
            self.enabled = self.active = True
        if action == "disable":
            self.enabled = False
        if action == "stop":
            self.active = False
        if action in {"start", "restart"}:
            self.active = True
        if action in {"enable", "start", "restart"} and self.publish:
            state = Path(os.environ.get("XDG_STATE_HOME", str(self.home / ".local/state"))) / "agiw/linux-status.json"
            state.write_text(json.dumps({"schemaVersion": 1, "platform": "linux", "observedUnix": time.time(),
                "releaseCommit": self.heartbeat_commit or os.readlink(self.base / "current"),
                "components": [{"id": "heartbeatUnit", "state": "up"}]}))
            state.chmod(0o600)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def invoke(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            INSTALLER["install"](["-", str(self.source / "install.sh"), *args])
        return output.getvalue()

    def track(self, commit):
        self.git_head = commit
        for path in self.source.rglob("*"):
            if path.is_file():
                self.blobs[f"{commit}:linux/{path.relative_to(self.source)}"] = path.read_bytes()

    def installed(self, commit):
        self.track(commit)
        output = self.invoke("--commit", commit, "--enable")
        self.enabled = self.active = False
        self.calls.clear()
        return output

    def leftovers(self):
        return [p.name for p in self.base.iterdir() if p.name.startswith((".agiw-", ".current-", ".install-"))]

    def test_outer_deadline_wrapper_is_retained_hashed_and_executable(self):
        self.track(FIRST)
        self.invoke("--commit", FIRST)
        wrapper = self.base / FIRST / "agiw-linux-once"
        self.assertTrue(wrapper.is_file())
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o700)
        manifest = json.loads((self.base / FIRST / "install-manifest.json").read_text())
        self.assertIn("agiw-linux-once", manifest["files"])
        wrapper.write_text("#!/bin/sh\nexit 98\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.invoke("--rollback", FIRST)

    def paired_idle_fixture(self, commit):
        # Synthetic historical bytes, with pins in the signed fixture candidate.
        # Only OS/git/systemd boundaries are mocked; the production guard runs.
        namespace = {"__name__": "fixture_allowance"}
        source = (LINUX / "agiw_linux/idle_allowance.py").read_text()
        exec(compile(source, "allowance-fixture", "exec"), namespace)
        baseline = {name: ("fixture " + name).encode() for name in namespace["PINS"]}
        for name, pin in namespace["PINS"].items():
            source = source.replace(pin, namespace["digest"](baseline[name]))
        (self.source / "agiw_linux/idle_allowance.py").write_text(source)
        exec(compile(source, "allowance-fixture", "exec"), namespace)
        files = {name: (self.source / name).read_bytes() for name in
                 ("agiw-linux-status", "agiw-linux-once", INSTALLER["UNIT"],
                  *("agiw_linux/" + name for name in INSTALLER["MODULES"]))}
        config_home = Path(os.environ.get('XDG_CONFIG_HOME', self.home / '.config'))
        state_home = Path(os.environ.get('XDG_STATE_HOME', self.home / '.local/state'))
        runtime, config = namespace["layout"](self.home, self.base, commit, files,
                                             config_home=config_home, state_home=state_home)
        self.idle_pairings[runtime] = config
        runtime.mkdir(parents=True, exist_ok=True)
        fragment = config_home / "systemd/user/agiw-idle-analysis.service"
        fragment.parent.mkdir(parents=True, exist_ok=True)
        for name, raw in namespace["bundle"](self.home, self.base, commit, files, baseline,
                                            config_home=config_home, state_home=state_home).items():
            target = fragment if name == "agiw-idle-analysis.service" else runtime / name
            target.write_bytes(raw)
        self.idle_properties = {"LoadState": "loaded", "ActiveState": "inactive", "FragmentPath": str(fragment),
            "DropInPaths": "", "NeedDaemonReload": "no", "ExecStart":
            "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I -B " + str(runtime / "service-launcher.py")
            + " ; ignore_errors=no ; pid=0 ; status=0/0 }"}
        self.track(commit)
        return runtime

    def run_idle_gate(self):
        """Run the installed pairing's actual gate against synthetic kernel state."""
        fragment = Path(self.idle_properties['FragmentPath'])
        command = next(line.split('=', 1)[1] for line in fragment.read_text().splitlines() if line.startswith('ExecStart='))
        runtime = Path(shlex.split(command)[-1]).parent
        namespace = {'__name__': 'fixture_active_allowance'}
        exec(compile((runtime / 'observer_allowance.py').read_bytes(), 'active-allowance', 'exec'), namespace)
        proc = self.root / 'proc'
        proc.mkdir()
        if self.active:
            root = proc / '123'
            root.mkdir()
            group = f'/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{INSTALLER["UNIT"]}'
            self.observer_properties = {'MainPID': '123', 'ActiveState': 'active', 'SubState': 'running',
                'FragmentPath': str(self.unit), 'DropInPaths': '', 'ControlGroup': group,
                'InvocationID': 'a' * 32, 'NeedDaemonReload': 'no'}
            (root / 'stat').write_bytes(b'123 (python3) S ' + b'0 ' * 18 + b'100\n')
            (root / 'status').write_text('Uid:\t' + '\t'.join([str(os.getuid())] * 4)
                + '\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\n')
            (root / 'cmdline').write_bytes(b'\0'.join((b'/usr/bin/python3', b'-I', b'-B',
                str(self.base / 'current/agiw-linux-status').encode(), b'--serve')) + b'\0')
            (root / 'cgroup').write_text('0::' + group + '\n')
            (root / 'exe').symlink_to(Path('/usr/bin/python3').resolve())
        seen = []
        def historical(filtered, uid, own_pid, *, idle_comms):
            seen.extend(path.name for path in filtered.iterdir())
            if seen:
                raise ValueError('historical gate refuses unapproved Python')
        namespace['process_gate'](self.idle_pairings[runtime], historical, proc, idle_comms=set())
        self.assertEqual(seen, [])
        return self.idle_pairings[runtime]['commit']

    def failed_activation_keeps_analysis(self, upgrade, heartbeat):
        with mock.patch.dict(INSTALLER, {'verify_idle_observer_allowance': UNVERIFIED_IDLE_ALLOWANCE,
                                        'prepare_idle_rollback': PREPARE_IDLE_ROLLBACK,
                                        'ACTIVATION_SECONDS': .01}):
            if upgrade:
                self.paired_idle_fixture(FIRST)
                self.invoke('--commit', FIRST, '--enable')
            self.paired_idle_fixture(SECOND)
            if heartbeat:
                # Only the candidate heartbeat is withheld; restored FIRST must publish.
                self.heartbeat_commit = FIRST
            else:
                self.fail_action = 'enable'
            original = self.command
            def manager(argv, **kwargs):
                result = original(argv, **kwargs)
                if argv[:3] == ['/usr/bin/systemctl', '--user', 'daemon-reload']:
                    fragment = Path(self.idle_properties['FragmentPath'])
                    command = next(line.split('=', 1)[1] for line in fragment.read_text().splitlines() if line.startswith('ExecStart='))
                    self.idle_properties['ExecStart'] = '{ path=/usr/bin/python3 ; argv[]=' + ' '.join(shlex.split(command)) + ' ; ignore_errors=no ; pid=0 ; status=0/0 }'
                return result
            with mock.patch.object(subprocess, 'run', side_effect=manager), self.assertRaisesRegex(RuntimeError, 'install failed and was rolled back'):
                self.invoke('--commit', SECOND, '--enable')
        self.assertEqual(self.active, upgrade)
        self.assertEqual(self.enabled, upgrade)
        if upgrade:
            self.assertEqual(os.readlink(self.base / 'current'), FIRST)
            status = json.loads((self.home / '.local/state/agiw/linux-status.json').read_text())
            self.assertEqual(status['releaseCommit'], FIRST)
        else:
            self.assertFalse(os.path.lexists(self.base / 'current'))
            self.assertFalse(self.unit.exists())
        self.assertEqual(self.run_idle_gate(), FIRST if upgrade else SECOND)

    def test_first_install_enable_failure_preserves_analyzer_gate(self):
        self.failed_activation_keeps_analysis(upgrade=False, heartbeat=False)

    def test_first_install_heartbeat_failure_preserves_analyzer_gate(self):
        self.failed_activation_keeps_analysis(upgrade=False, heartbeat=True)

    def test_upgrade_enable_failure_restores_analyzer_gate_and_heartbeat(self):
        self.failed_activation_keeps_analysis(upgrade=True, heartbeat=False)

    def test_upgrade_heartbeat_failure_restores_analyzer_gate_and_heartbeat(self):
        self.failed_activation_keeps_analysis(upgrade=True, heartbeat=True)

    def test_unavailable_or_tampered_rollback_analyzer_blocks_promotion(self):
        with mock.patch.dict(INSTALLER, {'verify_idle_observer_allowance': UNVERIFIED_IDLE_ALLOWANCE,
                                        'prepare_idle_rollback': PREPARE_IDLE_ROLLBACK}):
            runtime = self.paired_idle_fixture(FIRST)
            self.invoke('--commit', FIRST, '--enable')
            self.paired_idle_fixture(SECOND)
            target = runtime / 'observer_allowance.py'
            for bad in (b'tampered', None):
                with self.subTest(bad=bad):
                    if bad is None:
                        target.unlink()
                    else:
                        target.write_bytes(bad)
                    self.calls.clear()
                    with self.assertRaises((OSError, ValueError)):
                        self.invoke('--commit', SECOND, '--enable')
                    self.assertEqual(os.readlink(self.base / 'current'), FIRST)
                    self.assertTrue(self.active)
                    self.assertFalse(any(call[0] == '/usr/bin/systemctl' and call[2] not in
                        {'show', 'is-active', 'is-enabled'} for call in self.calls))

    def test_failed_analyzer_pairing_restoration_is_reported_incomplete(self):
        with mock.patch.dict(INSTALLER, {'verify_idle_observer_allowance': UNVERIFIED_IDLE_ALLOWANCE,
                                        'prepare_idle_rollback': PREPARE_IDLE_ROLLBACK}):
            self.paired_idle_fixture(FIRST)
            self.invoke('--commit', FIRST, '--enable')
            self.paired_idle_fixture(SECOND)
            self.fail_action = 'enable'
            # Simulate a manager that fails to reload the restored analyzer unit.
            with self.assertRaisesRegex(RuntimeError, 'rollback incomplete.*effective idle analyzer pairing'):
                self.invoke('--commit', SECOND, '--enable')
            self.assertTrue(self.active)
            self.assertEqual(os.readlink(self.base / 'current'), FIRST)
            status = json.loads((self.home / '.local/state/agiw/linux-status.json').read_text())
            self.assertEqual(status['releaseCommit'], FIRST)

    def test_real_guard_activates_space_home_and_xdg_paths(self):
        self.home = self.root / 'space name'
        self.home.mkdir(mode=0o700)
        self.base = self.home / 'data space/agiw-linux-observer'
        self.unit = self.home / 'config space/systemd/user' / INSTALLER['UNIT']
        with mock.patch.dict(os.environ, {'HOME': str(self.home), 'XDG_DATA_HOME': str(self.home / 'data space'),
                'XDG_CONFIG_HOME': str(self.home / 'config space'), 'XDG_STATE_HOME': str(self.home / 'state space')}), \
                mock.patch.dict(INSTALLER, {'verify_idle_observer_allowance': UNVERIFIED_IDLE_ALLOWANCE}):
            self.paired_idle_fixture(FIRST)
            self.invoke('--commit', FIRST, '--enable')
            self.assertEqual(self.run_idle_gate(), FIRST)

    def test_real_guard_allows_paired_install_and_retained_rollback(self):
        self.paired_idle_fixture(FIRST)
        with mock.patch.dict(INSTALLER, {"verify_idle_observer_allowance": UNVERIFIED_IDLE_ALLOWANCE}):
            self.invoke("--commit", FIRST, "--enable")
            self.paired_idle_fixture(SECOND)
            self.invoke("--commit", SECOND, "--enable")
            self.paired_idle_fixture(FIRST)
            self.invoke("--rollback", FIRST, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertTrue(self.active)

    def test_real_guard_refuses_wrong_effective_pair_before_service_mutation(self):
        self.paired_idle_fixture(FIRST)
        self.idle_properties["DropInPaths"] = "/run/override.conf"
        with mock.patch.dict(INSTALLER, {"verify_idle_observer_allowance": UNVERIFIED_IDLE_ALLOWANCE}), \
                self.assertRaisesRegex(ValueError, "effective idle analyzer pairing"):
            self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(self.base.exists())
        self.assertFalse(any(call[0] == "/usr/bin/systemctl" and call[2] != "show" for call in self.calls))

    def test_all_external_commands_use_trusted_absolute_paths(self):
        self.track(FIRST)
        with mock.patch.dict(os.environ, {"PATH": str(self.root / "untrusted-bin")}):
            self.invoke("--commit", FIRST, "--enable")
        self.assertTrue(self.calls)
        self.assertTrue(all(call[0] in {"/usr/bin/git", "/usr/bin/systemctl"} for call in self.calls),
                        self.calls)

    def test_activation_without_verified_idle_allowance_is_refused(self):
        self.track(FIRST)
        with mock.patch.dict(INSTALLER, {"verify_idle_observer_allowance": UNVERIFIED_IDLE_ALLOWANCE}), \
                self.assertRaisesRegex(ValueError, "idle.*allowance.*not verified"):
            self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(self.base.exists())
        self.assertFalse(self.unit.exists())
        self.assertFalse(any(Path(call[0]).name == "systemctl" for call in self.calls))

    def test_staging_needs_no_idle_allowance_but_retained_activation_does(self):
        self.installed(FIRST)
        old_unit = self.unit.read_bytes()
        self.track(SECOND)
        with mock.patch.dict(INSTALLER, {"verify_idle_observer_allowance": UNVERIFIED_IDLE_ALLOWANCE}):
            self.invoke("--commit", SECOND)
            self.calls.clear()
            with self.assertRaisesRegex(ValueError, "idle.*allowance.*not verified"):
                self.invoke("--rollback", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertFalse(any(Path(call[0]).name == "systemctl" for call in self.calls))

    def test_restore_start_success_without_active_service_is_incomplete(self):
        self.track(FIRST)
        self.invoke("--commit", FIRST, "--enable")
        self.track(SECOND)
        self.fail_action = "restart"
        command = self.command

        def exits_after_start(argv, **kwargs):
            result = command(argv, **kwargs)
            if Path(argv[0]).name == "systemctl" and argv[2] == "start":
                self.active = False
            return result

        with mock.patch.object(subprocess, "run", side_effect=exits_after_start), \
                mock.patch.dict(INSTALLER, {"ACTIVATION_SECONDS": .01}):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete.*fresh heartbeat"):
                self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)

    def test_restore_active_without_fresh_prior_release_heartbeat_is_incomplete(self):
        for kind in ("stale", "wrong-release"):
            with self.subTest(kind=kind):
                self.track(FIRST)
                self.publish = True
                self.heartbeat_commit = None
                self.invoke("--commit", FIRST, "--enable")
                self.track(SECOND)
                self.fail_action = "restart"
                self.publish = kind != "stale"
                self.heartbeat_commit = SECOND
                with mock.patch.dict(INSTALLER, {"ACTIVATION_SECONDS": .01}):
                    with self.assertRaisesRegex(RuntimeError, "rollback incomplete.*fresh heartbeat"):
                        self.invoke("--commit", SECOND, "--enable")
                self.assertEqual(os.readlink(self.base / "current"), FIRST)

    def test_stage_copies_only_runtime_without_execution_and_no_service_calls(self):
        output = self.invoke("--commit", FIRST)
        self.assertIn("unverified-export", output)
        self.assertIn("not activated", output)
        self.assertFalse(os.path.lexists(self.base / "current"))
        self.assertFalse(self.unit.exists())
        self.assertEqual((self.base / FIRST / "agiw-linux-status").stat().st_mode & 0o777, 0o700)
        manifest = json.loads((self.base / FIRST / "install-manifest.json").read_text())
        self.assertEqual(manifest["commit"], FIRST)
        self.assertEqual(len(manifest["files"]), 10)
        self.assertFalse(any(c[0] == "/usr/bin/systemctl" for c in self.calls))
        self.assertEqual(self.leftovers(), [])

    def test_root_and_effective_root_refused_before_mutation(self):
        for name in ("getuid", "geteuid"):
            with self.subTest(name=name), mock.patch.object(os, name, return_value=0):
                with self.assertRaisesRegex(ValueError, "refusing root"):
                    self.installed(FIRST)
        self.assertEqual(list(self.home.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_non_linux_refused(self):
        with mock.patch.object(sys, "platform", "darwin"):
            with self.assertRaisesRegex(ValueError, "Linux only"):
                self.installed(FIRST)

    def test_commit_validation_and_clean_git_provenance(self):
        for commit in ("../outside", "abc", "x" * 40):
            with self.subTest(commit=commit), self.assertRaisesRegex(ValueError, "full lowercase"):
                self.invoke("--commit", commit)
        self.track(FIRST)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.invoke("--commit", SECOND)
        self.git_dirty = "?? agiw_linux/collect.py\n"
        with self.assertRaisesRegex(ValueError, "dirty"):
            self.invoke()
        self.git_dirty = ""
        self.assertIn("git-blobs-verified; signature-not-verified", self.invoke())

    def test_idempotent_install_keeps_state_and_release(self):
        self.installed(FIRST)
        state = self.home / ".local/state/agiw/linux-status.json"
        state.write_text("kept")
        self.invoke("--commit", FIRST)
        self.assertEqual(state.read_text(), "kept")
        (self.source / "agiw_linux/collect.py").write_text("CHANGED = True\n")
        self.git_head = None
        with self.assertRaisesRegex(RuntimeError, "different content"):
            self.invoke("--commit", FIRST)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.leftovers(), [])

    def test_upgrade_and_explicit_rollback_retain_both_releases(self):
        self.installed(FIRST)
        old_unit = self.unit.read_bytes()
        (self.source / "agiw_linux/collect.py").write_text("CHANGED = True\n")
        self.track(SECOND)
        self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), SECOND)
        self.assertIn("heartbeat verified", self.invoke("--rollback", FIRST, "--enable"))
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertTrue((self.base / SECOND).is_dir())

    def test_xdg_paths_and_space_in_home_render_consistent_unit(self):
        spaced = self.root / "home with spaces"
        self.home.rename(spaced)
        self.track(FIRST)
        with mock.patch.dict(INSTALLER, {"wait_for_heartbeat": mock.Mock()}), mock.patch.dict(os.environ, {
            "HOME": str(spaced), "XDG_DATA_HOME": str(spaced / "data"),
            "XDG_CONFIG_HOME": str(spaced / "config"), "XDG_STATE_HOME": str(spaced / "state"),
        }):
            self.publish = False
            with mock.patch.dict(INSTALLER, {"check_unit_overrides": mock.Mock()}):
                self.invoke("--commit", FIRST, "--enable")
        text = (spaced / "config/systemd/user" / INSTALLER["UNIT"]).read_text()
        self.assertIn(f'"{spaced}/data/agiw-linux-observer/current/agiw-linux-status" --serve', text)
        self.assertIn(f'Environment="XDG_STATE_HOME={spaced}/state"', text)
        self.assertIn(f'ReadWritePaths="{spaced}/state/agiw"', text)
        self.assertNotIn("%h", text)

    def test_unsafe_xdg_paths_refused(self):
        for value in ("relative", str(self.root / "outside"), str(self.home / "../outside"),
                      str(self.home / "bad%specifier"), str(self.home / "bad\ninjection")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"XDG_DATA_HOME": value}):
                with self.assertRaises(ValueError):
                    self.installed(FIRST)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_symlink_ancestors_and_dangling_links_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        for target in (outside, self.root / "absent"):
            with self.subTest(target=target):
                (self.home / ".local").symlink_to(target)
                with self.assertRaisesRegex(ValueError, "symlink"):
                    self.installed(FIRST)
                (self.home / ".local").unlink()
        self.assertEqual(list(outside.iterdir()), [])

    def test_writable_or_foreign_owned_destination_refused(self):
        self.home.chmod(0o777)
        with self.assertRaisesRegex(ValueError, "user-owned"):
            self.installed(FIRST)
        self.home.chmod(0o700)
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaisesRegex(ValueError, "user-owned"):
                self.installed(FIRST)

    def test_nonprivate_existing_state_dir_refused_without_chmod(self):
        target = self.home / ".local/state/agiw"
        target.mkdir(parents=True, mode=0o755)
        target.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "private.*0700"):
            self.installed(FIRST)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)
        self.assertFalse(self.base.exists())

    def test_writable_nonsticky_ancestor_of_home_refused(self):
        self.root.chmod(0o777)
        try:
            with self.assertRaisesRegex(ValueError, "unsafe writable"):
                self.installed(FIRST)
            self.assertEqual(list(self.home.iterdir()), [])
        finally:
            self.root.chmod(0o700)

    def test_source_symlink_missing_file_and_syntax_error_refused_before_switch(self):
        self.installed(FIRST)
        source = self.source / "agiw_linux/collect.py"
        saved = source.read_bytes()
        self.git_head = None
        source.unlink()
        source.symlink_to(self.source / "agiw_linux/probes.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.invoke("--commit", SECOND)
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.invoke("--commit", SECOND)
        source.write_text("if invalid syntax\n")
        with self.assertRaises(SyntaxError):
            self.invoke("--commit", SECOND)
        source.write_bytes(saved)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)

    def test_current_link_cannot_escape_or_be_a_real_file(self):
        self.base.mkdir(parents=True)
        current = self.base / "current"
        for target in ("../../outside", str(self.root), "c" * 40):
            with self.subTest(target=target):
                current.symlink_to(target)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.installed(FIRST)
                current.unlink()
        current.write_text("owned by something else")
        with self.assertRaisesRegex(ValueError, "managed symlink"):
            self.installed(FIRST)

    def test_unit_symlink_or_unmanaged_unit_refused(self):
        self.unit.parent.mkdir(parents=True)
        target = self.root / "unit"
        target.write_text("untouched")
        self.unit.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.installed(FIRST)
        self.unit.unlink()
        self.unit.write_text("another application\n")
        with self.assertRaisesRegex(ValueError, "unmanaged"):
            self.installed(FIRST)
        self.assertEqual(target.read_text(), "untouched")

    def test_tampered_rollback_release_refused(self):
        self.installed(FIRST)
        self.track(SECOND)
        self.invoke("--commit", SECOND, "--enable")
        (self.base / FIRST / "agiw_linux/collect.py").write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.invoke("--rollback", FIRST)
        self.assertEqual(os.readlink(self.base / "current"), SECOND)

    def test_existing_lock_fails_closed(self):
        self.installed(FIRST)
        (self.base / ".install-lock").mkdir()
        self.track(SECOND)
        with self.assertRaises(FileExistsError):
            self.invoke("--commit", SECOND)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)

    def test_export_install_does_not_require_git(self):
        command = self.command

        def missing_git(argv, **kwargs):
            if argv[0] == "/usr/bin/git":
                raise FileNotFoundError("git")
            return command(argv, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=missing_git):
            self.assertIn("unverified-export", self.invoke("--commit", FIRST))

    def test_retained_release_extra_files_or_symlink_directory_refused(self):
        self.installed(FIRST)
        self.track(SECOND)
        self.invoke("--commit", SECOND, "--enable")
        target = self.base / FIRST / "agiw_linux/__pycache__"
        target.symlink_to(self.source / "agiw_linux", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.invoke("--rollback", FIRST)
        target.unlink()
        target.mkdir()
        with self.assertRaisesRegex(ValueError, "unexpected release directory"):
            self.invoke("--rollback", FIRST)
        target.rmdir()
        (self.base / FIRST / "extra.py").write_text("pass\n")
        with self.assertRaisesRegex(ValueError, "unexpected release files"):
            self.invoke("--rollback", FIRST)

    def test_manifest_path_traversal_and_group_writable_manifest_refused(self):
        self.installed(FIRST)
        self.track(SECOND)
        self.invoke("--commit", SECOND, "--enable")
        target = self.base / FIRST / "install-manifest.json"
        original = target.read_bytes()
        document = json.loads(original)
        document["files"]["../../escaped.py"] = "c" * 64
        target.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "file list mismatch"):
            self.invoke("--rollback", FIRST)
        target.write_bytes(original)
        target.chmod(0o666)
        with self.assertRaisesRegex(ValueError, "user-owned"):
            self.invoke("--rollback", FIRST)

    def test_nonregular_source_is_refused_without_blocking(self):
        target = self.source / "agiw_linux/collect.py"
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, "bounded regular file"):
            self.installed(FIRST)

    def test_masked_unit_or_unavailable_user_manager_refused(self):
        self.track(FIRST)
        for action, output in (("is-enabled", "masked"), ("is-active", "")):
            with self.subTest(action=action):
                command = self.command

                def unavailable(argv, **kwargs):
                    if argv[0] == "/usr/bin/systemctl" and argv[2] == action:
                        return subprocess.CompletedProcess(argv, 1, output, "")
                    return command(argv, **kwargs)

                with mock.patch.object(subprocess, "run", side_effect=unavailable):
                    with self.assertRaisesRegex(RuntimeError, "cannot establish"):
                        self.invoke("--commit", FIRST, "--enable")
                self.assertFalse(os.path.lexists(self.base / "current"))
                self.assertFalse(self.unit.exists())
                self.assertEqual(self.leftovers(), [])

    def test_link_snapshot_change_before_lock_refused_without_clobber(self):
        self.installed(FIRST)
        self.track(SECOND)
        original = Path.mkdir

        def acquire(path, *args, **kwargs):
            if path.name == ".install-lock":
                (self.base / "current").unlink()
                (self.base / "current").symlink_to(SECOND)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", acquire):
            with self.assertRaisesRegex(RuntimeError, "current changed during preparation"):
                self.invoke("--commit", SECOND)
        self.assertEqual(os.readlink(self.base / "current"), SECOND)
        self.assertEqual(self.leftovers(), [])

    def test_daemon_reload_failure_restores_previous_files(self):
        self.installed(FIRST)
        self.track(SECOND)
        old_unit = self.unit.read_bytes()
        self.fail_action = "daemon-reload"
        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertFalse(self.enabled)
        self.assertFalse(self.active)

    def test_failed_stage_copy_cleans_temp_and_keeps_previous_release(self):
        self.installed(FIRST)
        self.track(SECOND)
        original = INSTALLER["atomic_write"]

        def fail(path, *args):
            if path.name == "collect.py":
                raise OSError("injected copy failure")
            return original(path, *args)

        with mock.patch.dict(INSTALLER, {"atomic_write": fail}):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                self.invoke("--commit", SECOND)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertFalse((self.base / SECOND).exists())
        self.assertEqual(self.leftovers(), [])

    def test_failed_link_switch_restores_unit(self):
        self.installed(FIRST)
        self.track(SECOND)
        old_unit = self.unit.read_bytes()
        with mock.patch.dict(INSTALLER, {"switch_link": mock.Mock(side_effect=OSError("link failure"))}):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)

    def test_first_install_activation_failure_restores_no_unit_or_link(self):
        self.track(FIRST)
        self.fail_action = "enable"
        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(os.path.lexists(self.base / "current"))
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.enabled)
        self.assertFalse(self.active)
        self.assertEqual(self.leftovers(), [])

    def test_upgrade_restart_failure_restores_prior_active_service(self):
        self.track(FIRST)
        self.invoke("--commit", FIRST, "--enable")
        old_unit = self.unit.read_bytes()
        self.track(SECOND)
        self.fail_action = "restart"
        with self.assertRaisesRegex(RuntimeError, "rolled back"):
            self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)
        self.assertTrue(self.enabled)
        self.assertTrue(self.active)
        status = json.loads((self.home / ".local/state/agiw/linux-status.json").read_text())
        self.assertEqual(status["releaseCommit"], FIRST)

    def test_rollback_failure_is_reported_instead_of_claiming_success(self):
        self.installed(FIRST)
        self.track(SECOND)
        original = INSTALLER["systemctl"]

        def fail(*args, **kwargs):
            if args[0] in {"enable", "stop"}:
                raise RuntimeError("injected service failure")
            return original(*args, **kwargs)

        with mock.patch.dict(INSTALLER, {"systemctl": fail}):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
                self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)

    def test_unit_hardening_and_no_listener_default(self):
        text = (LINUX / INSTALLER["UNIT"]).read_text()
        for setting in ("ProtectSystem=strict", "ProtectHome=read-only", "MemoryMax=128M", "CPUQuota=10%",
                        'ReadWritePaths="%h/.local/state/agiw"', "NoNewPrivileges=yes", "UMask=0077"):
            self.assertIn(setting, text)
        command = next(line for line in text.splitlines() if line.startswith("ExecStart="))
        self.assertTrue(command.endswith(" --serve"))
        self.assertNotIn("--port", command)
        self.assertIn(" -I -B ", command)

    def test_staging_preserves_running_release_unit_and_rollback_target(self):
        self.track(FIRST)
        self.invoke("--commit", FIRST, "--enable")
        unit = self.unit.read_bytes()
        (self.source / INSTALLER["UNIT"]).write_bytes(unit + b"# next release\n")
        self.track(SECOND)
        self.calls.clear()
        self.invoke("--commit", SECOND)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), unit)
        self.assertTrue(self.active)
        self.assertTrue(self.enabled)
        self.assertFalse(any(call[0] == "/usr/bin/systemctl" for call in self.calls))
        self.invoke("--rollback", SECOND)
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), unit)

    def test_ignored_or_changed_source_cannot_borrow_commit_label(self):
        self.track(FIRST)
        target = self.source / "agiw_linux/collect.py"
        for kind in ("ignored", "changed"):
            with self.subTest(kind=kind):
                self.track(FIRST)
                if kind == "ignored":
                    del self.blobs[f"{FIRST}:linux/agiw_linux/collect.py"]
                else:
                    target.write_text("CHANGED_AFTER_STATUS = True\n")
                with self.assertRaisesRegex(ValueError, "commit blob"):
                    self.invoke("--commit", FIRST)
                self.assertFalse(self.base.exists())

    def test_export_and_unsigned_commit_cannot_activate(self):
        with self.assertRaisesRegex(ValueError, "exports are stage-only"):
            self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(self.base.exists())
        self.track(FIRST)
        self.signed = False
        with self.assertRaisesRegex(ValueError, "signature verification failed"):
            self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(self.base.exists())
        self.assertFalse(any(call[0] == "/usr/bin/systemctl" for call in self.calls))

    def test_rollback_rechecks_signature_and_retained_bytes_against_git(self):
        self.installed(FIRST)
        self.track(SECOND)
        self.invoke("--commit", SECOND, "--enable")
        self.signed = False
        with self.assertRaisesRegex(ValueError, "signature verification failed"):
            self.invoke("--rollback", FIRST, "--enable")
        self.signed = True
        # A self-consistent local manifest is not evidence of the signed commit.
        self.blobs[f"{FIRST}:linux/agiw_linux/collect.py"] = b"different signed bytes\n"
        with self.assertRaisesRegex(ValueError, "commit blob"):
            self.invoke("--rollback", FIRST, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), SECOND)

    def test_user_dropin_refused_before_promotion(self):
        self.installed(FIRST)
        old_unit = self.unit.read_bytes()
        dropins = self.unit.parent / (INSTALLER["UNIT"] + ".d")
        dropins.mkdir()
        (dropins / "override.conf").write_text("[Service]\nProtectSystem=off\n")
        self.track(SECOND)
        with self.assertRaisesRegex(RuntimeError, "unexpected systemd drop-ins"):
            self.invoke("--commit", SECOND, "--enable")
        self.assertEqual(os.readlink(self.base / "current"), FIRST)
        self.assertEqual(self.unit.read_bytes(), old_unit)

    def test_effective_dropins_or_wrong_fragment_after_reload_roll_back(self):
        self.installed(FIRST)
        self.track(SECOND)
        old_unit = self.unit.read_bytes()
        for prop in ("DropInPaths", "FragmentPath"):
            loaded = False
            command = self.command

            def override(argv, **kwargs):
                nonlocal loaded
                if argv[:3] == ["/usr/bin/systemctl", "--user", "daemon-reload"]:
                    loaded = True
                if loaded and argv[:3] == ["/usr/bin/systemctl", "--user", "show"] and f"--property={prop}" in argv:
                    return subprocess.CompletedProcess(argv, 0, "/run/fixture/override.conf\n", "")
                return command(argv, **kwargs)

            with self.subTest(prop=prop), mock.patch.object(subprocess, "run", side_effect=override):
                with self.assertRaisesRegex(RuntimeError, "rolled back"):
                    self.invoke("--commit", SECOND, "--enable")
            self.assertEqual(os.readlink(self.base / "current"), FIRST)
            self.assertEqual(self.unit.read_bytes(), old_unit)
            self.assertFalse(self.active)

    def test_successful_start_without_heartbeat_rolls_back(self):
        self.track(FIRST)
        self.publish = False
        with mock.patch.dict(INSTALLER, {"ACTIVATION_SECONDS": .01}):
            with self.assertRaisesRegex(RuntimeError, "activation deadline"):
                self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(os.path.lexists(self.base / "current"))
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)

    def test_wrong_release_or_stale_heartbeat_cannot_accept_upgrade(self):
        self.track(FIRST)
        self.invoke("--commit", FIRST, "--enable")
        self.track(SECOND)
        for kind in ("wrong-release", "stale"):
            self.publish = kind != "stale"
            self.heartbeat_commit = FIRST
            with self.subTest(kind=kind), mock.patch.dict(INSTALLER, {"ACTIVATION_SECONDS": .01}):
                with self.assertRaisesRegex(RuntimeError, "activation deadline"):
                    self.invoke("--commit", SECOND, "--enable")
            self.assertEqual(os.readlink(self.base / "current"), FIRST)
            self.assertTrue(self.active)
            self.assertTrue(self.enabled)

    def test_fresh_heartbeat_with_inactive_unit_is_not_success(self):
        self.track(FIRST)
        command = self.command

        def inactive(argv, **kwargs):
            result = command(argv, **kwargs)
            if argv[:3] == ["/usr/bin/systemctl", "--user", "is-active"]:
                return subprocess.CompletedProcess(argv, 3, "inactive\n", "")
            return result

        with mock.patch.object(subprocess, "run", side_effect=inactive), \
                mock.patch.dict(INSTALLER, {"ACTIVATION_SECONDS": .01}):
            with self.assertRaisesRegex(RuntimeError, "activation deadline"):
                self.invoke("--commit", FIRST, "--enable")
        self.assertFalse(self.unit.exists())


class EntryPointTest(unittest.TestCase):
    def test_cold_retained_entry_ignores_path_pythonpath_and_user_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            release = root / FIRST
            release.mkdir()
            shutil.copytree(LINUX / "agiw_linux", release / "agiw_linux",
                            ignore=shutil.ignore_patterns("__pycache__"))
            entry = release / "agiw-linux-status"
            shutil.copyfile(LINUX / "agiw-linux-status", entry)
            entry.chmod(0o700)
            untrusted = root / "untrusted"
            untrusted.mkdir()
            sentinel = root / "injected"
            # If isolation regresses, either PATH or Python startup executes this
            # harmless fixture payload, never an actual external command.
            (untrusted / "python3").write_text("#!/bin/sh\nexit 98\n")
            (untrusted / "python3").chmod(0o700)
            injection = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
            (untrusted / "sitecustomize.py").write_text(injection)
            user_site = root / "userbase" / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
            user_site.mkdir(parents=True)
            (user_site / "usercustomize.py").write_text(injection)
            with mock.patch.object(sys, "path", [str(LINUX), *sys.path]):
                from agiw_linux.collect import project_status, unknown_snapshot
                from agiw_linux.server import write_status
            env = dict(os.environ, PATH=str(untrusted), PYTHONPATH=str(untrusted),
                       PYTHONUSERBASE=str(root / "userbase"), HOME=str(root), XDG_STATE_HOME=str(root / "state"))
            for command in ([str(entry), "--once"],
                            [sys.executable, "-I", "-B", str(entry), "--once"]):
                with self.subTest(command=command[0]):
                    value = project_status(unknown_snapshot(now=time.time(), host="fixture"))
                    write_status(root / "state/agiw", value)
                    started = time.monotonic()
                    result = subprocess.run(command, env=env, cwd=untrusted, capture_output=True, timeout=4)
                    elapsed = time.monotonic() - started
                    self.assertEqual(result.returncode, 0, result.stderr.decode())
                    self.assertEqual(json.loads(result.stdout)["observedUnix"], value["observedUnix"])
                    self.assertLessEqual(len(result.stdout), 65536)
                    self.assertLess(elapsed, 4)
                    self.assertFalse(sentinel.exists())
                    self.assertFalse(list(release.rglob("__pycache__")))


if __name__ == "__main__":
    unittest.main()
