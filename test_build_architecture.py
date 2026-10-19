"""Black-box architecture gates with fake tools; never compile, sign or install."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == 'darwin', 'build entry points require macOS')
class ArchitectureTests(unittest.TestCase):
    def run_script(self, script, architecture='arm64', system='Darwin',
                   compiled_architecture=None, inspect_resources=False):
        repo = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='agiw-architecture-test-') as temporary:
            root = Path(temporary)
            tool_dir = root / 'tools'
            tool_dir.mkdir()
            calls = root / 'calls.jsonl'
            # Every compiler/signing/image operation is intercepted. A compiler
            # placeholder lets the installer's executable gate be exercised.
            shim = '''#!{python}
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['ARCH_TEST_CALLS'], 'a') as stream:
    stream.write(json.dumps([name, *args]) + '\\n')
if name == 'uname':
    print(os.environ['ARCH_TEST_SYSTEM'] if args == ['-s'] else os.environ['ARCH_TEST_HOST'])
elif name == 'xcode-select':
    print('/fake/Developer')
elif name == 'xcodebuild':
    print('Xcode test\\nBuild version test')
elif name == 'xcrun' and args == ['--sdk', 'macosx', '--show-sdk-version']:
    print('13.0')
elif name == 'xcrun' and args == ['--find', 'swiftc']:
    print('/fake/swiftc')
elif name == 'xcrun' and args == ['swiftc', '--version']:
    print('Apple Swift test')
elif name == 'xcrun' and args[:2] == ['swiftc', '-O']:
    if not os.environ.get('ARCH_TEST_BINARY'):
        sys.exit(97)
    pathlib.Path(args[args.index('-o') + 1]).write_bytes(b'fixture only')
elif name == 'lipo':
    print(os.environ['ARCH_TEST_BINARY'])
elif name == 'codesign' and os.environ.get('ARCH_TEST_RESOURCES'):
    app = next(path for path in pathlib.Path(args[-1]).parents if path.name.endswith('.app'))
    resources = app / 'Contents' / 'Resources'
    source = pathlib.Path(os.environ['ARCH_TEST_RESOURCES'])
    staged = []
    for filename in ('machine_state.py', 'agiw_status.py', 'agiw-status'):
        path = resources / filename
        staged.append([filename, path.is_file() and path.read_bytes() == (source / filename).read_bytes()])
    with open(os.environ['ARCH_TEST_CALLS'], 'a') as stream:
        stream.write(json.dumps(['staged-resources', staged]) + '\\n')
    sys.exit(98)
else:
    sys.exit(98)
'''.format(python=sys.executable)
            for name in ('uname', 'xcrun', 'xcodebuild', 'xcode-select',
                         'codesign', 'hdiutil', 'lipo'):
                tool = tool_dir / name
                tool.write_text(shim)
                tool.chmod(0o700)
            environment = dict(os.environ)
            environment.update({
                'PATH': str(tool_dir) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                'HOME': str(root / 'home'),
                'PYTHONDONTWRITEBYTECODE': '1',
                'ARCH_TEST_CALLS': str(calls),
                'ARCH_TEST_SYSTEM': system,
                'ARCH_TEST_HOST': architecture,
                'ARCH_TEST_BINARY': compiled_architecture or '',
                'ARCH_TEST_RESOURCES': str(repo) if inspect_resources else '',
                'MONITOR_RELEASE_SIGN_IDENTITY': '0' * 40,
                'MONITOR_RELEASE_ALLOW_DIRTY': '1',
                'MONITOR_RELEASE_OUTPUT_DIR': str(root / 'output'),
            })
            result = subprocess.run(['/bin/bash', str(repo / script)], cwd=repo,
                                    env=environment, capture_output=True, text=True,
                                    timeout=20)
            observed = [json.loads(line) for line in calls.read_text().splitlines()]
            self.assertFalse((root / 'home/Applications/Inference Monitor.app').exists())
            self.assertFalse((root / 'home/.local').exists())
            self.assertFalse(any(call[0] == 'hdiutil' for call in observed), observed)
            sign_calls = [call for call in observed if call[0] == 'codesign']
            self.assertEqual(len(sign_calls), int(inspect_resources), observed)
            # Refused installers must not even create the Applications directory.
            if result.returncode == 2 and script == 'build.sh':
                self.assertFalse((root / 'home/Applications').exists())
            return result, observed

    def test_installer_refuses_non_native_hosts_before_side_effects(self):
        for system, architecture in (('Darwin', 'x86_64'), ('Linux', 'arm64')):
            with self.subTest(system=system, architecture=architecture):
                result, calls = self.run_script('build.sh', architecture, system)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('native arm64 macOS terminal', result.stderr)
                self.assertTrue(all(call[0] == 'uname' for call in calls), calls)

    def test_native_installer_compiles_only_arm64(self):
        result, calls = self.run_script('build.sh')
        self.assertEqual(result.returncode, 97, result.stderr)
        compile_call = next(call for call in calls if call[:3] == ['xcrun', 'swiftc', '-O'])
        self.assertEqual(compile_call[compile_call.index('-target') + 1], 'arm64-apple-macosx13.0')

    def test_installer_rejects_wrong_or_universal_executable_before_signing(self):
        for architecture in ('x86_64', 'arm64 x86_64'):
            with self.subTest(architecture=architecture):
                result, _ = self.run_script('build.sh', compiled_architecture=architecture)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('must contain only arm64', result.stderr)

    def test_packager_selects_arm64_on_either_mac_host(self):
        for architecture in ('arm64', 'x86_64'):
            with self.subTest(architecture=architecture):
                result, calls = self.run_script('package-release.sh', architecture)
                self.assertEqual(result.returncode, 97, result.stderr)
                compile_call = next(call for call in calls if call[:3] == ['xcrun', 'swiftc', '-O'])
                self.assertEqual(compile_call[compile_call.index('-target') + 1], 'arm64-apple-macosx13.0')
                self.assertFalse(any(call[:2] == ['uname', '-m'] for call in calls), calls)

    def test_installer_and_packager_stage_three_machine_core_and_status_cli(self):
        for script in ('build.sh', 'package-release.sh'):
            with self.subTest(script=script):
                result, calls = self.run_script(script, compiled_architecture='arm64', inspect_resources=True)
                self.assertEqual(result.returncode, 98, result.stderr)
                resources = next(call[1] for call in calls if call[0] == 'staged-resources')
                self.assertEqual(resources, [['machine_state.py', True], ['agiw_status.py', True], ['agiw-status', True]])


if __name__ == '__main__':
    unittest.main()
