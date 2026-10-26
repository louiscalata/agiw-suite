"""Installer review regressions; source contracts complement the native Windows tests."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'install.ps1'
REPO = SCRIPT.parent.parent


class InstallerRegressionContracts(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding='utf-8')

    def test_directory_handles_request_data_access_for_delete_exclusion(self):
        self.assertRegex(self.source, r'CreateFileW\(path, directory \? 0x20081u : 0x20080u')

    def test_acl_reflection_fixture_unwraps_descriptor(self):
        source = SCRIPT.with_name('tests').joinpath('test_install.py').read_text()
        self.assertIn('[Security.AccessControl.RawSecurityDescriptor]$security.psobject.BaseObject', source)
        self.assertIn("[string](Join-Path $HOME 'agiw-acl-fixture')", source)

    def test_crlf_payload_hash_matches_git_clean_blob(self):
        command = re.search(r'\$GitPath -C \$Repo hash-object (.+?) 2>\$null', self.source).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / 'launcher.ps1'
            staged.write_bytes(b'Write-Host "fixture"\r\n')
            args = command.replace('"--path=$relative"', '--path=windows/agiw-monitor.ps1').replace('$staged', str(staged)).split()
            actual = subprocess.run(['git', '-C', str(REPO), 'hash-object', *args], capture_output=True, text=True, check=True).stdout.strip()
            expected = subprocess.run(['git', 'hash-object', '--stdin'], input=b'Write-Host "fixture"\n', capture_output=True, check=True).stdout.decode().strip()
            self.assertEqual(actual, expected)
        self.assertIn('check-attr filter', self.source)

    def test_shortcut_conflict_is_checked_before_swap_and_save_is_staged(self):
        self.assertLess(self.source.index("A different 'AGIW Inference Monitor'"), self.source.index('foreach ($relative in $installed)'))
        self.assertIn('$existing.TargetPath -ine $wscript', self.source)
        self.assertIn('$guard.Target($link)', self.source)
        self.assertIn('$shortcut = $shell.CreateShortcut($stagedLink)', self.source)
        self.assertIn('$replaced.Add(@($savedLink, $link))', self.source)

    def test_failed_install_removes_created_empty_directories(self):
        self.assertIn('RemoveEmptyCreatedDirectories()', self.source)

    def test_rollback_does_not_silence_failed_added_file_removal(self):
        self.assertIn('Remove-Item -LiteralPath $target -Force -ErrorAction Stop', self.source)
        self.assertIn('Rollback left file:', self.source)
        self.assertIn('Install cleanup left:', self.source)

    def test_unexpected_code_and_state_acl_are_checked_before_staging(self):
        preflight = self.source[:self.source.index('$guard.Directory($stage, $true)')]
        self.assertIn('Unexpected install entries', preflight)
        self.assertIn('$guard.Directory($state, $false)', preflight)

    def test_junction_cases_need_no_symlink_privilege(self):
        source = SCRIPT.with_name('tests').joinpath('test_install.py').read_text()
        for name in ('test_dangling_link_ancestor_is_rejected', 'test_missing_destination_beneath_junction_is_rejected'):
            body = source.split('    def ' + name, 1)[1].split('    def ', 1)[0]
            self.assertNotIn('os.symlink(', body)
            self.assertIn('self.junction(', body)
