"""Installer destination checks, exercised without changing an installed copy."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.install_helpers import install_root


SCRIPT = Path(__file__).resolve().parents[1] / "install.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"


class InstallRootTest(unittest.TestCase):
    def test_root_uses_default_mkdir_mode_under_profile_and_cleans_up(self):
        owner = unittest.TestCase()
        with tempfile.TemporaryDirectory() as profile:
            with mock.patch("tests.install_helpers.Path.home", return_value=Path(profile)), \
                    mock.patch("tests.install_helpers.os.mkdir", wraps=os.mkdir) as mkdir:
                with install_root(owner) as root:
                    self.assertEqual(root.parent, Path(profile))
                    mkdir.assert_called_once_with(root)
                    (root / "sentinel").write_text("fixture")
                self.assertTrue(root.exists(), "cleanup must wait until the test body has finished")
                self.assertTrue(owner.doCleanups())
                self.assertFalse(root.exists())

    def test_root_cleans_up_after_fixture_failure(self):
        owner = unittest.TestCase()
        with tempfile.TemporaryDirectory() as profile:
            with mock.patch("tests.install_helpers.Path.home", return_value=Path(profile)):
                with self.assertRaisesRegex(RuntimeError, "fixture failed"):
                    with install_root(owner) as root:
                        (root / "sentinel").write_text("fixture")
                        raise RuntimeError("fixture failed")
                self.assertTrue(owner.doCleanups())
                self.assertFalse(root.exists())

    def test_preflight_fixture_does_not_create_missing_destination_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / 'new-parent' / 'install'
            startup = root / 'startup'
            startup.mkdir()
            case = InstallerDestinationTest()
            with mock.patch.object(subprocess, 'run') as run:
                case.invoke_fixture(destination, startup=startup)
            fixture = Path(run.call_args.args[0][6])
            self.assertTrue(fixture.is_file())
            self.assertEqual(fixture.parent, root)
            self.assertFalse(destination.parent.exists())


@unittest.skipUnless(os.name == "nt", "Windows installer only")
class InstallerDestinationTest(unittest.TestCase):
    def invoke(self, destination: str):
        return subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-File", str(SCRIPT), "-Destination", destination],
                              capture_output=True, text=True, timeout=60)

    def junction(self, link, target):
        made = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.addCleanup(lambda: os.rmdir(link) if os.path.lexists(link) else None)

    def leftovers(self, destination: Path):
        return [p.name for p in destination.iterdir() if p.name.startswith((".agiw-stage-", ".agiw-backup-"))]

    def owner_rights_acl(self, owner, extra_ace=""):
        # Exercise the production descriptor check without needing privileges to change a real folder's owner.
        script = SCRIPT.read_text(encoding="utf-8")
        helper = script[script.index("Add-Type -TypeDefinition @'"):script.index("$Destination = $full")]
        command = helper + f"""
$ErrorActionPreference = 'Stop'
$owner = {owner}
$security = New-Object Security.AccessControl.RawSecurityDescriptor ("O:" + $owner + "G:SYD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;S-1-3-4){extra_ace}")
$guard = New-Object AgiwInstallGuard
try {{
    $check = [AgiwInstallGuard].GetMethod('CheckPermissions', [Reflection.BindingFlags]'Instance,NonPublic')
    $check.Invoke($guard, [object[]]@([Security.AccessControl.RawSecurityDescriptor]$security.psobject.BaseObject, [string](Join-Path $HOME 'agiw-acl-fixture'))) | Out-Null
}} finally {{ $guard.Dispose() }}
"""
        return subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, text=True, timeout=60)

    def invoke_fixture(self, destination, change=lambda source: source, startup=None):
        # Redirect only fixture paths and fault injection; never touch the user's Startup folder.
        quote = lambda value: str(value).replace("'", "''")
        source = SCRIPT.read_text(encoding="utf-8")
        source = source.replace('$repo = Split-Path -Parent $PSScriptRoot',
                                "$repo = '" + quote(SCRIPT.parent.parent) + "'")
        source = source.replace('$sourceWindows = $PSScriptRoot',
                                "$sourceWindows = '" + quote(SCRIPT.parent) + "'")
        if startup is not None:
            source = source.replace("[Environment]::GetFolderPath('Startup')", "'" + quote(startup) + "'")
        fixture_root = next(parent for parent in destination.parents if parent.is_dir())
        fixture = fixture_root / 'fixture-install.ps1'
        fixture.write_bytes(change(source).replace("\n", "\r\n").encode())
        args = [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(fixture), "-Destination", str(destination)]
        if startup is not None:
            args.append('-StartAtSignIn')
        return subprocess.run(args, capture_output=True, text=True, timeout=90)

    def make_shortcut(self, path, target, arguments, working_directory=""):
        quote = lambda value: str(value).replace("'", "''")
        command = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + quote(path) + "'); "
                   "$s.TargetPath='" + quote(target) + "'; $s.Arguments='" + quote(arguments) + "'; $s.WorkingDirectory='" + quote(working_directory) + "'; $s.Save()")
        result = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-Command', command],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflicting_shortcut_does_not_change_installed_package(self):
        for wrong_target in (False, True):
            with self.subTest(wrong_target=wrong_target), install_root(self) as root:
                dest, startup = root / 'install', root / 'startup'
                startup.mkdir()
                self.assertEqual(self.invoke(str(dest)).returncode, 0)
                old = dest / 'README.md'
                old.write_text('previous package')
                link = startup / 'AGIW Inference Monitor.lnk'
                target = POWERSHELL if wrong_target else POWERSHELL.parents[2] / 'wscript.exe'
                args = '"' + str(dest / 'AGIW Monitor.vbs') + '"' if wrong_target else '"other.vbs"'
                self.make_shortcut(link, target, args)
                before = link.read_bytes()
                result = self.invoke_fixture(dest, startup=startup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('different', result.stderr)
                self.assertEqual(old.read_text(), 'previous package')
                self.assertEqual(link.read_bytes(), before)

    def test_shortcut_with_other_first_script_or_working_directory_is_not_owned(self):
        for other_script in (True, False):
            with self.subTest(other_script=other_script), install_root(self) as root:
                dest, startup = root / 'install', root / 'startup'
                startup.mkdir()
                self.assertEqual(self.invoke(str(dest)).returncode, 0)
                old = dest / 'README.md'
                old.write_text('previous package')
                args = '"' + str(dest / 'AGIW Monitor.vbs') + '" /quiet'
                if other_script:
                    args = '"C:\\other\\evil.vbs" ' + args
                link = startup / 'AGIW Inference Monitor.lnk'
                self.make_shortcut(link, POWERSHELL.parents[2] / 'wscript.exe', args,
                                   dest if other_script else root)
                before = link.read_bytes()
                result = self.invoke_fixture(dest, startup=startup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('different', result.stderr)
                self.assertEqual(old.read_text(), 'previous package')
                self.assertEqual(link.read_bytes(), before)

    def test_shortcut_preflight_failure_removes_new_empty_destination_and_parents(self):
        with install_root(self) as root:
            dest, startup = root / 'new-parent/install', root / 'startup'
            startup.mkdir()
            link = startup / 'AGIW Inference Monitor.lnk'
            self.make_shortcut(link, POWERSHELL, 'other.ps1')
            before = link.read_bytes()
            result = self.invoke_fixture(dest, startup=startup)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('different', result.stderr)
            self.assertFalse(dest.parent.exists())
            self.assertEqual(link.read_bytes(), before)

    def test_shortcut_save_failure_and_post_swap_failure_roll_back(self):
        for after_swap in (False, True):
            with self.subTest(after_swap=after_swap), install_root(self) as root:
                dest, startup = root / 'install', root / 'startup'
                startup.mkdir()
                self.assertEqual(self.invoke(str(dest)).returncode, 0)
                old = dest / 'README.md'
                old.write_text('previous package')
                def fail(source):
                    if after_swap:
                        return source.replace("    if (-not (Test-Path -LiteralPath $marker)) {\n        $added.Add($marker)",
                                              "    throw 'fixture after swaps'\n    if (-not (Test-Path -LiteralPath $marker)) {\n        $added.Add($marker)")
                    return source.replace('$shortcut.Save()', "throw 'fixture Save failure'")
                result = self.invoke_fixture(dest, fail, startup)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('rolled back', result.stderr)
                self.assertEqual(old.read_text(), 'previous package')
                self.assertFalse((startup / 'AGIW Inference Monitor.lnk').exists())
                self.assertEqual(self.leftovers(dest), [])

    def test_existing_empty_destination_is_retryable_after_staging_failure(self):
        with install_root(self) as root:
            dest = root / 'install'
            dest.mkdir()
            result = self.invoke_fixture(dest, lambda source: source.replace(
                'Copy-Item -LiteralPath $pair[0] -Destination $staged -Force', "throw 'fixture copy failure'"))
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(dest.iterdir()), [])
            retry = self.invoke(str(dest))
            self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_failed_added_file_removal_reports_incomplete_rollback(self):
        with install_root(self) as root:
            dest = root / 'install'
            self.assertEqual(self.invoke(str(dest)).returncode, 0)
            (dest / 'README.md').unlink()
            def fail(source):
                source = source.replace("    if (-not (Test-Path -LiteralPath $marker)) {\n        $added.Add($marker)",
                                        "    throw 'fixture after swaps'\n    if (-not (Test-Path -LiteralPath $marker)) {\n        $added.Add($marker)")
                return source.replace('Remove-Item -LiteralPath $target -Force -ErrorAction Stop',
                                      "throw 'fixture locked added file'")
            result = self.invoke_fixture(dest, fail)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('rollback was incomplete', result.stderr)
            self.assertIn('README.md', result.stderr)
            self.assertTrue(any(name.startswith('.agiw-backup-') for name in self.leftovers(dest)))

    def test_unexpected_executable_inventory_is_refused_before_update(self):
        for extra in ('agiw_win/stale.py', 'agiw_win/probes.cp313-win_amd64.pyd',
                      'agiw_win/__pycache__/probes.pyc', '.agiw-backup-old/saved.py'):
            with self.subTest(extra=extra), install_root(self) as root:
                dest = root / 'install'
                self.assertEqual(self.invoke(str(dest)).returncode, 0)
                old = dest / 'README.md'
                old.write_text('previous package')
                path = dest / extra
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('unexpected code')
                result = self.invoke(str(dest))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unexpected install entries', result.stderr)
                self.assertEqual(old.read_text(), 'previous package')
                self.assertEqual(path.read_text(), 'unexpected code')

    def test_startup_junction_is_refused_before_package_swap(self):
        with install_root(self) as root:
            dest, target, startup = root / 'install', root / 'outside', root / 'startup'
            target.mkdir()
            self.junction(startup, target)
            self.assertEqual(self.invoke(str(dest)).returncode, 0)
            old = dest / 'README.md'
            old.write_text('previous package')
            result = self.invoke_fixture(dest, startup=startup)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('a link', result.stderr)
            self.assertEqual(old.read_text(), 'previous package')
            self.assertEqual(list(target.iterdir()), [])

    def test_existing_state_with_other_writer_is_refused(self):
        with install_root(self) as root:
            dest = root / 'install'
            self.assertEqual(self.invoke(str(dest)).returncode, 0)
            state = dest / 'state'
            state.mkdir()
            changed = subprocess.run(['icacls.exe', str(state), '/grant', '*S-1-1-0:(OI)(CI)(M)'],
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            result = self.invoke(str(dest))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('writable by another user', result.stderr)

    def test_owner_rights_ace_with_trusted_owner_passes(self):
        for owner in ("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value", "'S-1-5-18'",
                      "'S-1-5-32-544'", "'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'"):
            with self.subTest(owner=owner):
                result = self.owner_rights_acl(owner)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_owner_rights_ace_with_untrusted_owner_is_rejected(self):
        for owner in ("'S-1-5-21-100-200-300-1001'", "'S-1-3-4'"):
            with self.subTest(owner=owner):
                result = self.owner_rights_acl(owner)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ownership is not private", " ".join(result.stderr.split()))

    def test_owner_rights_ace_does_not_hide_another_writer(self):
        result = self.owner_rights_acl("[Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
                                       "(A;OICI;FA;;;WD)")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writable by another user", " ".join(result.stderr.split()))

    def test_owner_rights_folder_with_trusted_owner_allows_install(self):
        with install_root(self) as root:
            destination = root / "install"
            destination.mkdir()
            quoted = str(destination).replace("'", "''")
            command = f"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath '{quoted}'
$owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
$trusted = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18', 'S-1-5-32-544',
             'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
if ($owner -notin $trusted) {{ throw 'Fixture owner is not trusted.' }}
$sid = New-Object Security.Principal.SecurityIdentifier 'S-1-3-4'
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath '{quoted}' -AclObject $acl
"""
            changed = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
                                     capture_output=True, text=True, timeout=60)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            result = self.invoke(str(destination))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((destination / ".agiw-win-install").is_file())

    def test_update_is_staged_keeps_state_and_rolls_back_on_failure(self):
        with install_root(self) as tmp:
            dest = Path(tmp) / "agiw-win"
            first = self.invoke(str(dest))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue((dest / ".agiw-win-install").is_file())
            self.assertTrue((dest / "agiw_win" / "probes.py").is_file() and (dest / "web" / "app.js").is_file())
            self.assertEqual(self.leftovers(dest), [])
            (dest / "state").mkdir()
            (dest / "state" / "observer.log").write_text("kept")
            (dest / "agiw_win" / "probes.py").write_text("OLD probes")
            (dest / "web" / "app.js").write_text("OLD app")
            # NOTICE is swapped last; a reader that never shares delete makes its replace fail mid-update.
            with open(dest / "NOTICE", "rb"):
                failed = self.invoke(str(dest))
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("rolled back", failed.stderr)
            self.assertEqual((dest / "agiw_win" / "probes.py").read_text(), "OLD probes")
            self.assertEqual((dest / "web" / "app.js").read_text(), "OLD app")
            self.assertEqual(self.leftovers(dest), [])
            again = self.invoke(str(dest))
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotEqual((dest / "agiw_win" / "probes.py").read_text(), "OLD probes")
            self.assertEqual((dest / "state" / "observer.log").read_text(), "kept")
            self.assertEqual(self.leftovers(dest), [])

    def test_dangling_link_ancestor_is_rejected(self):
        with install_root(self) as tmp:
            root = Path(tmp)
            target = root / "gone"
            target.mkdir()
            link = root / "link"
            self.junction(link, target)
            target.rmdir()
            result = self.invoke(str(link / "new-install"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shared, remote or a link", result.stderr)

    def test_relative_destination_follows_the_powershell_location(self):
        # After Set-Location the process directory still points elsewhere; the install must follow PowerShell.
        with install_root(self) as tmp, install_root(self) as elsewhere:
            command = f"Set-Location -LiteralPath '{tmp}'; & '{SCRIPT}' -Destination rel-install"
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-Command", command], capture_output=True, text=True, timeout=60, cwd=elsewhere)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((Path(tmp) / "rel-install" / ".agiw-win-install").is_file())
            self.assertFalse((Path(elsewhere) / "rel-install").exists())

    def test_missing_destination_beneath_junction_is_rejected(self):
        with install_root(self) as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            self.junction(link, target)
            result = self.invoke(str(link / "new-install"))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shared, remote or a link", result.stderr)
            self.assertEqual(list(target.iterdir()), [])

    def test_unc_destination_is_rejected(self):
        result = self.invoke(r"\\localhost\C$\agiw-test-never-create")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("shared, remote or a link", result.stderr)

    def test_marked_install_nested_junctions_are_rejected_before_any_swap(self):
        for name in ("agiw_win", "web"):
            with self.subTest(name=name), install_root(self) as tmp:
                root = Path(tmp)
                destination, outside = root / "install", root / "outside"
                destination.mkdir()
                outside.mkdir()
                (destination / ".agiw-win-install").write_text("existing install")
                (destination / "README.md").write_text("previous build")
                (outside / "sentinel").write_text("untouched")
                link = destination / name
                made = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                      capture_output=True, text=True, timeout=20)
                self.assertEqual(made.returncode, 0, made.stderr)
                try:
                    result = self.invoke(str(destination))
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("a link", result.stderr)
                    self.assertEqual((destination / "README.md").read_text(), "previous build")
                    self.assertEqual(list(p.name for p in outside.iterdir()), ["sentinel"])
                    self.assertEqual(self.leftovers(destination), [])
                finally:
                    os.rmdir(link)

    def test_other_user_write_access_on_destination_or_parent_is_rejected(self):
        for writable_parent in (False, True):
            with self.subTest(parent=writable_parent), install_root(self) as tmp:
                root = Path(tmp)
                destination = root / "install"
                destination.mkdir()
                writable = root if writable_parent else destination
                changed = subprocess.run(["icacls.exe", str(writable), "/grant", "*S-1-1-0:(OI)(CI)(M)"],
                                         capture_output=True, text=True, timeout=20)
                self.assertEqual(changed.returncode, 0, changed.stderr)
                result = self.invoke(str(destination))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("writable by another user", result.stderr)
                self.assertFalse((destination / ".agiw-win-install").exists())
                self.assertEqual(list(destination.iterdir()), [])

    def test_inherit_only_guest_write_acl_cannot_make_staged_files_guest_writable(self):
        with install_root(self) as tmp:
            destination = Path(tmp) / "install"
            destination.mkdir()
            quoted = str(destination).replace("'", "''")
            command = f"""
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath '{quoted}'
$sid = New-Object Security.Principal.SecurityIdentifier 'S-1-1-0'
$rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'WriteData', 'ObjectInherit', 'InheritOnly', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath '{quoted}' -AclObject $acl
"""
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            refused = self.invoke(str(destination))
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("writable by another user", refused.stderr)
            self.assertEqual(list(destination.iterdir()), [])

    def test_destination_outside_current_user_profile_is_rejected(self):
        destination = Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "agiw-never-install-outside-profile"
        result = self.invoke(str(destination))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("beneath the current user's profile", " ".join(result.stderr.split()))

    def test_guard_retains_nested_parent_handles_until_released(self):
        script = SCRIPT.read_text(encoding="utf-8")
        helper = script[script.index("Add-Type -TypeDefinition @'"):script.index("$Destination = $full")]
        with install_root(self) as tmp:
            parent = Path(tmp) / "install"
            nested = parent / "agiw_win"
            nested.mkdir(parents=True)
            moved = parent / "renamed"
            quote = lambda path: str(path).replace("'", "''")
            command = helper + f"""
$ErrorActionPreference = 'Stop'
$guard = New-Object AgiwInstallGuard
try {{
    $guard.CheckDestination('{quote(parent)}')
    $guard.Directory('{quote(nested)}', $false)
    $blocked = $false
    try {{ [IO.Directory]::Move('{quote(nested)}', '{quote(moved)}') }} catch {{ $blocked = $true }}
    if (-not $blocked) {{ throw 'Retained parent handle permitted a directory swap.' }}
}} finally {{ $guard.Dispose() }}
[IO.Directory]::Move('{quote(nested)}', '{quote(moved)}')
"""
            result = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(moved.is_dir())


if __name__ == "__main__":
    unittest.main()
