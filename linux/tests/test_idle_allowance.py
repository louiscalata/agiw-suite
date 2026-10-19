"""Synthetic filesystem/kernel fixtures for the exact observer allowance."""
import hashlib
import os
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import shlex
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('idle_allowance', Path(__file__).parents[1] / 'agiw_linux/idle_allowance.py')
idle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(idle)


class AllowanceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.base = self.home / '.local/share/agiw-linux-observer'
        self.commit = 'a' * 40
        self.files = {idle.MODULE: Path(idle.__file__).read_bytes(), 'agiw-linux-status': b'fixture',
                      idle.OBSERVER_UNIT: b'[Service]\nExecStart=fixture\n'}
        self.baseline = {name: ('fixture ' + name).encode() for name in idle.PINS}
        pins = {name: hashlib.sha256(raw).hexdigest() for name, raw in self.baseline.items()}
        patch = mock.patch.dict(idle.PINS, pins, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.runtime, self.config = idle.layout(self.home, self.base, self.commit, self.files)
        self.runtime.mkdir(parents=True)
        self.fragment = self.home / '.config/systemd/user' / idle.IDLE_UNIT
        self.fragment.parent.mkdir(parents=True)
        for name, raw in idle.bundle(self.home, self.base, self.commit, self.files, self.baseline).items():
            path = self.fragment if name == idle.IDLE_UNIT else self.runtime / name
            path.write_bytes(raw)
        self.props = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'FragmentPath': str(self.fragment),
                      'DropInPaths': '', 'NeedDaemonReload': 'no',
                      'ExecStart': '{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I -B '
                      + str(self.runtime / 'service-launcher.py') + ' ; ignore_errors=no ; pid=0 ; status=0/0 }'}

    def verify(self):
        with mock.patch.object(idle, 'properties', return_value=self.props):
            idle.verify_pairing(self.home, self.base, self.commit, self.files)

    def test_properties_uses_absolute_read_only_command_and_preserves_value_equals(self):
        result = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nExecStart={ argv[]=value=with=equals }\n', '')
        with mock.patch.object(idle.subprocess, 'run', return_value=result) as command:
            parsed = idle.properties(idle.IDLE_UNIT, ('LoadState', 'ExecStart'))
        self.assertEqual(parsed, {'LoadState': 'loaded', 'ExecStart': '{ argv[]=value=with=equals }'})
        command.assert_called_once_with(
            ['/usr/bin/systemctl', '--user', 'show', idle.IDLE_UNIT, '--property=LoadState,ExecStart'],
            capture_output=True, text=True, timeout=2, check=False)

    def test_properties_refuses_failed_oversized_and_ambiguous_manager_output(self):
        samples = [('failure', 1, 'LoadState=loaded\n', 'visibility'),
                   ('oversized', 0, 'x' * 16385, 'visibility'),
                   ('duplicate', 0, 'LoadState=loaded\nLoadState=loaded\nExecStart=x\n', 'schema'),
                   ('unknown', 0, 'LoadState=loaded\nUnexpected=x\n', 'schema'),
                   ('malformed', 0, 'LoadState\nExecStart=x\n', 'schema'),
                   ('missing', 0, 'LoadState=loaded\n', 'missing')]
        for label, code, output, reason in samples:
            with self.subTest(label=label), mock.patch.object(idle.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], code, output, '')), self.assertRaisesRegex(ValueError, reason):
                idle.properties(idle.IDLE_UNIT, ('LoadState', 'ExecStart'))

    def test_nondefault_xdg_pairing_and_observer_unit_are_bound_to_exact_paths(self):
        config_home, state_home = self.home / 'custom-config', self.home / 'custom-state'
        self.files[idle.OBSERVER_UNIT] = (b'[Service]\nEnvironment="XDG_STATE_HOME=%h/.local/state"\n'
            b'ExecStart=%h/.local/share/agiw-linux-observer/current/agiw-linux-status --serve\n')
        self.runtime, self.config = idle.layout(self.home, self.base, self.commit, self.files,
                                               config_home=config_home, state_home=state_home)
        custom_fragment = config_home / 'systemd/user' / idle.IDLE_UNIT
        custom_fragment.parent.mkdir(parents=True)
        for name, raw in idle.bundle(self.home, self.base, self.commit, self.files, self.baseline,
                                     config_home=config_home, state_home=state_home).items():
            (custom_fragment if name == idle.IDLE_UNIT else self.runtime / name).write_bytes(raw)
        props = dict(self.props, FragmentPath=str(custom_fragment))
        output = ''.join(key + '=' + value + '\n' for key, value in props.items())
        with mock.patch.object(idle.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
            idle.verify_pairing(self.home, self.base, self.commit, self.files,
                                config_home=config_home, state_home=state_home)
            for kwargs in ({'config_home': self.home / '.config', 'state_home': state_home},
                           {'config_home': config_home, 'state_home': self.home / '.local/state'}):
                with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'pairing mismatch'):
                    idle.verify_pairing(self.home, self.base, self.commit, self.files, **kwargs)
        proc, root, properties = self.observer_fixture()
        custom_observer = custom_fragment.parent / idle.OBSERVER_UNIT
        expected_unit = self.files[idle.OBSERVER_UNIT].decode().replace(
            '%h/.local/state', str(state_home)).replace('%h/.local/share/agiw-linux-observer', str(self.base))
        custom_observer.write_text(expected_unit)
        properties['FragmentPath'] = str(custom_observer)
        output = ''.join(key + '=' + value + '\n' for key, value in properties.items())
        with mock.patch.object(idle.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')):
            self.assertEqual(idle.observer_identity(self.config, proc)[:3], (123, b'100', 'a' * 32))
            custom_observer.write_text(expected_unit.replace(str(state_home), str(self.home / '.local/state')))
            with self.assertRaisesRegex(ValueError, 'service bytes mismatch'):
                idle.observer_identity(self.config, proc)

    def test_exact_derived_successor_passes(self):
        self.verify()
        compile((self.runtime / 'service-launcher.py').read_bytes(), 'launcher', 'exec')

    def test_space_paths_share_installer_policy_and_unit_arguments(self):
        home = self.home / 'space name'
        runtime, config = idle.layout(home, home / 'data space/observer', self.commit, self.files,
            config_home=home / 'config space', state_home=home / 'state space')
        unit = dict(line.split('=', 1) for line in idle.unit_bytes(runtime).decode().splitlines() if '=' in line)
        self.assertEqual(shlex.split(unit['ExecStart']),
                         ['/usr/bin/python3', '-I', '-B', str(runtime / 'service-launcher.py')])
        self.assertEqual(shlex.split(unit['WorkingDirectory']), [str(runtime)])
        for bad in ('relative', str(home / '../escape'), str(home / 'bad%specifier'),
                    str(home / 'bad$variable'), str(home / 'bad"quote'), str(home / 'bad\nline')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                idle.layout(bad, self.base, self.commit, self.files)

    def test_absent_observer_runs_unfiltered_historical_gate(self):
        proc = self.home / 'proc'
        proc.mkdir()
        (proc / '10').mkdir()
        absent = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0'}
        seen = []
        def historical(root, uid, own_pid, *, idle_comms):
            self.assertIs(root, proc)
            seen.extend(p.name for p in root.iterdir())
        with mock.patch.object(idle, 'properties', return_value=absent):
            idle.process_gate(self.config, historical, proc, idle_comms=set())
        self.assertEqual(seen, ['10'])

    def test_absent_fallback_keeps_historical_refusals_and_rechecks_absence(self):
        proc = self.home / 'proc'
        proc.mkdir()
        absent = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0'}
        with mock.patch.object(idle, 'properties', return_value=absent), self.assertRaisesRegex(ValueError, 'historical refusal'):
            idle.process_gate(self.config, mock.Mock(side_effect=ValueError('historical refusal')), proc, idle_comms=set())
        active = dict(absent, ActiveState='active', SubState='running', MainPID='123')
        with mock.patch.object(idle, 'properties', side_effect=[absent, active]), self.assertRaises(ValueError):
            idle.process_gate(self.config, lambda *a, **k: None, proc, idle_comms=set())

    def test_unknown_or_unverified_resident_never_gets_fallback(self):
        proc = self.home / 'proc'
        proc.mkdir()
        absent = {'LoadState': 'not-found', 'ActiveState': 'inactive', 'SubState': 'dead', 'MainPID': '0'}
        for change in ({'LoadState': 'error'}, {'ActiveState': 'unknown'}, {'MainPID': '123'},
                       {'ActiveState': 'active', 'SubState': 'running', 'MainPID': '123'}):
            historical = mock.Mock()
            with self.subTest(change=change), mock.patch.object(idle, 'properties', return_value=dict(absent, **change)), self.assertRaises(ValueError):
                idle.process_gate(self.config, historical, proc, idle_comms=set())
            historical.assert_not_called()

    def test_baseline_pin_or_unsigned_declaration_never_authorizes(self):
        with self.assertRaisesRegex(ValueError, 'baseline pin'):
            idle.bundle(self.home, self.base, self.commit, self.files, {**self.baseline, 'analyzer.py': b'bad'})
        (self.runtime / 'allowance.json').write_text('{"compatible":true}')
        (self.runtime / 'analyzer.py').write_bytes(b'bad')
        with self.assertRaisesRegex(ValueError, 'baseline pin'):
            self.verify()

    def test_tampered_attestor_launcher_and_unit_are_rejected(self):
        for path in (self.runtime / 'observer_allowance.py', self.runtime / 'service-launcher.py', self.fragment):
            before = path.read_bytes()
            path.write_bytes(before + b'\n# changed\n')
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                self.verify()
            path.write_bytes(before)

    def test_wrong_release_is_not_accepted(self):
        with mock.patch.object(idle, 'properties', return_value=self.props), self.assertRaises(OSError):
            idle.verify_pairing(self.home, self.base, 'b' * 40, self.files)

    def test_stale_loaded_unit_dropin_active_old_analyzer_and_exec_override_are_rejected(self):
        for key, value in [('NeedDaemonReload', 'yes'), ('DropInPaths', '/run/user/override.conf'),
                           ('ActiveState', 'active'), ('FragmentPath', '/tmp/other.service'),
                           ('ExecStart', self.props['ExecStart'].replace('python3 -I', 'python3 -O'))]:
            with self.subTest(key=key), mock.patch.dict(self.props, {key: value}), self.assertRaises(ValueError):
                self.verify()

    def test_symlinked_runtime_code_is_rejected(self):
        source = self.runtime / 'observer_allowance.py'
        saved = self.runtime / 'saved.py'
        source.rename(saved)
        source.symlink_to(saved)
        with self.assertRaises(OSError):
            self.verify()

    def test_preserves_original_gate_and_does_not_exempt_other_python(self):
        proc = self.home / 'proc'
        proc.mkdir()
        for pid in ('10', '20', '30'):
            (proc / pid).mkdir()
        identity = (20, b'100', 'a' * 32, 1, 2)
        seen = []
        def original(filtered, uid, own_pid, *, idle_comms):
            seen.extend(sorted(p.name for p in filtered.iterdir()))
            self.assertEqual((uid, own_pid, idle_comms), (1000, 10, {'systemd'}))
            if '30' in seen:
                raise ValueError('unapproved extra process')
        with mock.patch.object(idle, 'observer_identity', return_value=identity), self.assertRaisesRegex(ValueError, 'extra process'):
            idle.process_gate(self.config, original, proc, 1000, 10, idle_comms={'systemd'})
        self.assertEqual(seen, ['10', '30'])

    def test_pid_reuse_or_restart_during_gate_refuses(self):
        proc = self.home / 'proc'
        proc.mkdir()
        identity = (20, b'100', 'a' * 32, 1, 2)
        with mock.patch.object(idle, 'observer_identity', side_effect=[identity, (20, b'101', 'b' * 32, 1, 2)]), self.assertRaisesRegex(ValueError, 'changed'):
            idle.process_gate(self.config, lambda *a, **k: None, proc, idle_comms=set())

    def observer_fixture(self):
        release = self.base / self.commit
        for name, raw in self.files.items():
            path = release / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        (release / 'install-manifest.json').write_text('{}')
        (self.base / 'current').symlink_to(self.commit)
        (self.fragment.parent / idle.OBSERVER_UNIT).write_bytes(self.files[idle.OBSERVER_UNIT])
        proc = self.home / 'proc'
        root = proc / '123'
        root.mkdir(parents=True)
        group = f'/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{idle.OBSERVER_UNIT}'
        properties = {'MainPID': '123', 'ActiveState': 'active', 'SubState': 'running',
            'FragmentPath': str(self.fragment.parent / idle.OBSERVER_UNIT), 'DropInPaths': '',
            'ControlGroup': group, 'InvocationID': 'a' * 32, 'NeedDaemonReload': 'no'}
        (root / 'stat').write_bytes(b'123 (python3) S ' + b'0 ' * 18 + b'100\n')
        (root / 'status').write_text('Uid:\t' + '\t'.join([str(os.getuid())] * 4)
                                    + '\nNoNewPrivs:\t1\nCapEff:\t0000000000000000\n')
        (root / 'cmdline').write_bytes(b'\0'.join((b'/usr/bin/python3', b'-I', b'-B',
            str(self.base / 'current/agiw-linux-status').encode(), b'--serve')) + b'\0')
        (root / 'cgroup').write_text('0::' + group + '\n')
        (root / 'exe').symlink_to(Path('/usr/bin/python3').resolve())
        return proc, root, properties

    def test_real_process_metadata_accepts_exact_resident_service(self):
        proc, root, properties = self.observer_fixture()
        with mock.patch.object(idle, 'properties', return_value=properties):
            identity = idle.observer_identity(self.config, proc)
        self.assertEqual(identity[:3], (123, b'100', 'a' * 32))

    def test_real_process_metadata_refuses_spoofed_argv_cgroup_or_privileges(self):
        proc, root, properties = self.observer_fixture()
        for name, raw in [('cmdline', b'python3\0-c\0pass\0'), ('cgroup', b'0::/other\n'),
                          ('status', b'Uid: 0 0 0 0\n')]:
            original = (root / name).read_bytes()
            (root / name).write_bytes(raw)
            with self.subTest(name=name), mock.patch.object(idle, 'properties', return_value=properties), self.assertRaises(ValueError):
                idle.observer_identity(self.config, proc)
            (root / name).write_bytes(original)

    def test_unexpected_import_payload_and_tampered_observer_are_rejected(self):
        proc, root, properties = self.observer_fixture()
        extra = self.base / self.commit / 'agiw_linux/__pycache__'
        extra.mkdir()
        with mock.patch.object(idle, 'properties', return_value=properties), self.assertRaisesRegex(ValueError, 'import directory'):
            idle.observer_identity(self.config, proc)
        extra.rmdir()
        (self.base / self.commit / 'agiw-linux-status').write_bytes(b'changed')
        with mock.patch.object(idle, 'properties', return_value=properties), self.assertRaisesRegex(ValueError, 'bytes mismatch'):
            idle.observer_identity(self.config, proc)


if __name__ == '__main__':
    unittest.main()
