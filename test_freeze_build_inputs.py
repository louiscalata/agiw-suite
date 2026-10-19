"""No-follow packaging checks with fixtures; never compile, sign or install."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import bundle_nisi
import freeze_build_inputs as freeze


@unittest.skipUnless(os.name == 'posix', 'descriptor-relative POSIX source reads')
class FrozenInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='agiw-freeze-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'frozen'
        self.root_fd = os.open(self.source, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.root_fd)

    def test_regular_inputs_are_frozen_independently(self):
        source = self.source / 'pair_status.py'
        source.write_bytes(b'checked source')
        freeze.freeze_inputs(self.root_fd, self.destination, ['pair_status.py'])
        source.write_bytes(b'changed source')
        self.assertEqual((self.destination / source.name).read_bytes(), b'checked source')

    def test_file_and_parent_symlinks_are_refused_before_copying(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'status.py').write_bytes(b'outside fixture')
        for name in ('file', 'parent'):
            with self.subTest(name=name):
                link = self.source / name
                link.symlink_to(outside / 'status.py' if name == 'file' else outside)
                relative = name if name == 'file' else name + '/status.py'
                with self.assertRaises(OSError):
                    freeze.freeze_inputs(self.root_fd, self.destination, [relative])
                self.assertFalse(self.destination.exists())

    def test_replaced_file_is_read_from_the_retained_descriptor(self):
        source = self.source / 'pair_status.py'
        source.write_bytes(b'checked source')
        outside = self.root / 'outside.py'
        outside.write_bytes(b'outside fixture')
        original_open = os.open
        swapped = []

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == 'pair_status.py' and not swapped:
                source.unlink()
                source.symlink_to(outside)
                swapped.append(True)
            return descriptor

        with patch.object(freeze.os, 'open', side_effect=replace_after_open):
            freeze.freeze_inputs(self.root_fd, self.destination, ['pair_status.py'])
        self.assertEqual(swapped, [True])
        self.assertEqual((self.destination / source.name).read_bytes(), b'checked source')

    def test_replaced_directory_keeps_the_checked_children(self):
        web = self.source / 'web'
        web.mkdir()
        (web / 'app.js').write_bytes(b'checked source')
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'app.js').write_bytes(b'outside fixture')
        original_open = os.open
        swapped = []

        def replace_after_open(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            if path == 'web' and not swapped:
                web.rename(self.source / 'retained-web')
                web.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
            return descriptor

        with patch.object(freeze.os, 'open', side_effect=replace_after_open):
            freeze.freeze_inputs(self.root_fd, self.destination, ['web/app.js'])
        self.assertEqual(swapped, [True])
        self.assertEqual((self.destination / 'web/app.js').read_bytes(), b'checked source')

    def test_nonregular_source_is_refused_without_blocking(self):
        os.mkfifo(self.source / 'pair_status.py')
        with self.assertRaisesRegex(ValueError, 'Nonregular build input'):
            freeze.freeze_inputs(self.root_fd, self.destination, ['pair_status.py'])
        self.assertFalse(self.destination.exists())

    def test_in_place_change_during_copy_aborts(self):
        source = self.source / 'pair_status.py'
        source.write_bytes(b'checked source')
        original_read = os.read
        changed = []

        def mutate_before_read(descriptor, length):
            if not changed:
                source.write_bytes(b'changed fixture with different size')
                changed.append(True)
            return original_read(descriptor, length)

        with patch.object(freeze.os, 'read', side_effect=mutate_before_read):
            with self.assertRaisesRegex(ValueError, 'changed while freezing'):
                freeze.freeze_inputs(self.root_fd, self.destination, ['pair_status.py'])

    def test_unlisted_nisi_payload_is_refused_before_copying(self):
        vendor = self.source / 'vendor/nisi'
        (vendor / 'package').mkdir(parents=True)
        (vendor / 'manifest.json').write_text(json.dumps({'files': {'index.mjs': 'fixture'}}))
        (vendor / 'package/index.mjs').write_bytes(b'listed source')
        extra = vendor / 'extra'
        for kind in ('file', 'empty directory'):
            with self.subTest(kind=kind):
                if kind == 'file':
                    extra.write_bytes(b'unlisted fixture')
                else:
                    extra.mkdir()
                with self.assertRaisesRegex(ValueError, 'Nisi payload file set mismatch'):
                    freeze.freeze_inputs(self.root_fd, self.destination, ['vendor/nisi'])
                self.assertFalse(self.destination.exists())
                if kind == 'file':
                    extra.unlink()


@unittest.skipUnless(sys.platform == 'darwin', 'build entry points require macOS')
class BuildInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parent
        cls.resources = shlex.split(re.search(
            r'resources=\((.*?)\)', (cls.repo / 'package-release.sh').read_text(),
            re.DOTALL).group(1))
        cls.nisi_inputs = [str(path.relative_to(cls.repo)) for path in bundle_nisi.resource_inputs()]
        cls.names = cls.resources + cls.nisi_inputs + [
            'Info.plist', 'Monitor.swift', 'JevKeychain.swift', 'LICENSE', 'NOTICE',
            'freeze_build_inputs.py', 'build.sh', 'package-release.sh']
        cls.inputs = {name: (cls.repo / name).read_bytes() for name in cls.names}

    def run_script(self, script, linked_input=None, replacement=False):
        with tempfile.TemporaryDirectory(prefix='agiw-input-test-') as temporary:
            root = Path(temporary)
            source = root / 'source'
            for name, content in self.inputs.items():
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            outside = root / 'outside-fixture'
            outside.write_bytes(b'outside fixture must never be bundled')
            if linked_input:
                target = source / linked_input
                target.unlink()
                target.symlink_to(outside)
            tools = root / 'tools'
            tools.mkdir()
            calls = root / 'calls.jsonl'
            shim = '''#!/bin/bash
name="${0##*/}"
printf '%s\\n' "$name $*" >> "$INPUT_TEST_CALLS"
case "$name" in
    uname) if [[ "$1" == -s ]]; then echo Darwin; else echo arm64; fi ;;
    git) if [[ "$*" == *rev-parse* ]]; then echo 0000000000000000000000000000000000000000; fi ;;
    mktemp)
        directory="$(/usr/bin/mktemp "$@")" || exit $?
        target="$INPUT_TEST_SOURCE/pair_status.py"
        if [[ "$INPUT_TEST_REPLACE" == 1 && ! -L "$target" ]]; then
            /bin/rm "$target"
            /bin/ln -s "$INPUT_TEST_OUTSIDE" "$target"
        fi
        echo "$directory" ;;
    xcode-select) echo /fake/Developer ;;
    xcodebuild) echo 'Xcode test' ;;
    xcrun)
        case "$*" in
            '--sdk macosx --show-sdk-version') echo 13.0 ;;
            '--find swiftc') echo /fake/swiftc ;;
            'swiftc --version') echo 'Apple Swift test' ;;
            *) exit 97 ;;
        esac ;;
    *) exit 97 ;;
esac
'''
            for name in ('uname', 'git', 'mktemp', 'xcrun', 'xcodebuild',
                         'xcode-select', 'codesign', 'hdiutil', 'lipo', 'plutil'):
                tool = tools / name
                tool.write_text(shim)
                tool.chmod(0o700)
            environment = dict(os.environ)
            environment.update({
                'PATH': str(tools) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                'HOME': str(root / 'home'), 'PYTHONDONTWRITEBYTECODE': '1',
                'MONITOR_RELEASE_SIGN_IDENTITY': '0' * 40,
                'MONITOR_RELEASE_ALLOW_DIRTY': '1',
                'MONITOR_RELEASE_OUTPUT_DIR': str(root / 'output'),
                'INPUT_TEST_CALLS': str(calls), 'INPUT_TEST_SOURCE': str(source),
                'INPUT_TEST_OUTSIDE': str(outside),
                'INPUT_TEST_REPLACE': '1' if replacement else '0',
            })
            result = subprocess.run(['/bin/bash', str(source / script)], cwd=source,
                                    env=environment, capture_output=True, text=True,
                                    timeout=20)
            observed = [line.split() for line in calls.read_text().splitlines()]
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(any(call[0] in ('codesign', 'hdiutil') or
                                 call[:3] == ['xcrun', 'swiftc', '-O']
                                 for call in observed), (result.stderr, observed))
            self.assertFalse((root / 'home/Applications/Inference Monitor.app').exists())
            self.assertFalse((root / 'home/.local').exists())
            if replacement:
                self.assertTrue((source / 'pair_status.py').is_symlink())
                self.assertIn('Too many levels of symbolic links', result.stderr)

    def test_every_build_input_rejects_a_static_symlink(self):
        for script in ('build.sh', 'package-release.sh'):
            names = self.resources + self.nisi_inputs + [
                'Info.plist', 'Monitor.swift', 'JevKeychain.swift', 'freeze_build_inputs.py']
            if script == 'package-release.sh':
                names += ['LICENSE', 'NOTICE']
            for name in names:
                with self.subTest(script=script, name=name):
                    self.run_script(script, linked_input=name)

    def test_source_replacement_after_preflight_cannot_redirect_copy(self):
        for script in ('build.sh', 'package-release.sh'):
            with self.subTest(script=script):
                self.run_script(script, replacement=True)


if __name__ == '__main__':
    unittest.main()
