"""Native Windows enrollment boundary tests using synthetic files inside this checkout only."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock
import uuid

from agiw_win import winsys
from tests.install_helpers import remove_tree


@unittest.skipUnless(os.name == 'nt', 'native Windows handles and DACLs required')
class NativeEnrollment(unittest.TestCase):
    def setUp(self):
        cwd = Path.cwd().resolve()
        profile = Path.home().resolve()
        if cwd != profile and profile not in cwd.parents:
            self.skipTest('checkout is outside the current user profile')
        self.root = cwd / ('.agiw-native-enrollment-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(remove_tree, self.root)
        self.folder = self.root / 'private'
        self.folder.mkdir()
        self.path = self.folder / 'lane-processes.json'
        self.payload = {'schema': 1, 'lanes': {'fast': {'pid': 4242, 'created': 123456789,
                                                     'image': r'C:\fixture\llama-server.exe'}}}
        self.path.write_text(json.dumps(self.payload), encoding='utf-8')
        self.powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        # Restrict only these newly created fixture objects. Nothing is inherited from live enrollment.
        for path in (self.root, self.folder, self.path):
            quoted = str(path).replace("'", "''")
            self.ps(f"""
$acl = Get-Acl -LiteralPath '{quoted}'
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {{ [void]$acl.RemoveAccessRuleSpecific($rule) }}
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl.SetOwner($me)
foreach ($sid in @($me, (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'),
                        (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544'))) {{
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
    $acl.AddAccessRule($rule)
}}
Set-Acl -LiteralPath '{quoted}' -AclObject $acl
""")

    def ps(self, body):
        result = subprocess.run([str(self.powershell), '-NoProfile', '-NonInteractive', '-Command',
                                 "$ErrorActionPreference='Stop'; " + body], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_private_real_file_is_accepted_without_changing_owner_or_dacl(self):
        import peer_link
        before = peer_link._win_owner_and_allow_sids(str(self.path))
        self.assertEqual(json.loads(winsys._read_local_enrollment(self.path)), self.payload)
        self.assertEqual(winsys.local_lane_identities(self.path), self.payload['lanes'])
        self.assertEqual(peer_link._win_owner_and_allow_sids(str(self.path)), before)
        self.assertEqual(before[0], peer_link._win_current_sid())
        self.assertEqual(set(before[1]), {before[0], 'S-1-5-18', 'S-1-5-32-544'})

    def test_retained_parent_prevents_directory_swap_until_read_finishes(self):
        original = winsys._open_enrollment_relative
        moved = self.root / 'moved'
        attempted = []
        def attempt_swap(parent, name):
            with self.assertRaises(OSError):
                self.folder.rename(moved)
            attempted.append(True)
            return original(parent, name)
        with mock.patch.object(winsys, '_open_enrollment_relative', side_effect=attempt_swap):
            self.assertEqual(json.loads(winsys._read_local_enrollment(self.path)), self.payload)
        self.assertEqual(attempted, [True])
        self.folder.rename(moved)
        self.assertTrue((moved / self.path.name).is_file())

    def test_parent_writer_is_refused_and_permissions_are_not_repaired(self):
        import peer_link
        # Change only the DACL on our owned fixture. Set-Acl can also write the
        # SACL/owner sections and demand SeSecurityPrivilege on an ordinary user.
        changed = subprocess.run(['icacls.exe', str(self.folder), '/grant', '*S-1-1-0:(M)'],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        before = peer_link._win_owner_and_allow_sids(str(self.folder))
        self.assertIn('S-1-1-0', before[1], 'fixture must actually expose the parent to another writer')
        with self.assertRaises(PermissionError):
            winsys._read_local_enrollment(self.path)
        self.assertEqual(winsys.local_lane_identities(self.path), {})
        self.assertEqual(peer_link._win_owner_and_allow_sids(str(self.folder)), before)
