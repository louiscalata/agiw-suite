"""Portable fixture checks; native PowerShell assertions remain in their original tests."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from tests import test_build_and_feed as build_tests
from tests.install_helpers import temporary_root


class SignedFixtureTrust(unittest.TestCase):
    def test_fixture_signs_and_verifies_without_host_git_configuration(self):
        with temporary_root(self) as tmp:
            root = Path(tmp)
            hostile = root / 'host.gitconfig'
            hostile.write_text('[gpg]\n\tformat = invalid-host-format\n')
            with mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(hostile),
                                             'GIT_CONFIG_COUNT': '1',
                                             'GIT_CONFIG_KEY_0': 'gpg.format',
                                             'GIT_CONFIG_VALUE_0': 'invalid-injected-format'}):
                repo, env, commit = build_tests.signed_fixture_repo(root)
                verified = subprocess.run(['git', '-C', str(repo), 'verify-commit', commit],
                                          env=env, capture_output=True, text=True)
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertEqual(env['GIT_CONFIG_NOSYSTEM'], '1')
            self.assertNotEqual(env['GIT_CONFIG_GLOBAL'], str(hostile))
            self.assertNotIn('GIT_CONFIG_COUNT', env)

    def test_fixture_unsigned_head_is_rejected_by_real_git(self):
        with temporary_root(self) as tmp:
            repo, env, commit = build_tests.signed_fixture_repo(Path(tmp), signature=False)
            result = subprocess.run(['git', '-C', str(repo), 'verify-commit', commit],
                                    env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)

    def test_fixture_normalizes_crlf_but_detects_equal_size_tampering(self):
        with temporary_root(self) as tmp:
            root = Path(tmp)
            repo, env, commit = build_tests.signed_fixture_repo(root)
            def git(*args):
                return subprocess.run(['git', '-C', str(repo), *args], env=env,
                                      capture_output=True, text=True, check=True).stdout.strip()
            expected = git('rev-parse', f'{commit}:windows/agiw_win/source.py')
            stage = root / 'staged.py'
            stage.write_bytes(b'VALUE = 1\r\n')
            self.assertEqual(git('hash-object', '--path=windows/agiw_win/source.py', '--', str(stage)), expected)
            stage.write_bytes(b'VALUE = 2\n')
            self.assertNotEqual(git('hash-object', '--path=windows/agiw_win/source.py', '--', str(stage)), expected)
