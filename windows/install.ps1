<#
  Copy the Windows edition from a repository checkout into an install folder.
  Usage (from the repository root):  powershell -NoProfile -File windows\install.ps1 [-Destination <folder>] [-StartAtSignIn]
  It copies files and writes build.json (the source commit); it does not start anything unless -Launch is given.
  The installed observer requires Python 3.9+; the tray enforces that floor. This script installs no interpreter.
#>
param(
    [string]$Destination = (Join-Path $HOME 'bin\agiw-win'),
    [switch]$StartAtSignIn,
    [switch]$Launch
)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
# The install runs code at sign-in, so it must live in a private folder: never on a network path or the
# guest-writable SharedChami share, and never under a junction or symlink.
# A relative destination means relative to the PowerShell location, not the process directory.
$full = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination))
if ($full.StartsWith('\\')) {
    throw "$Destination is shared, remote or a link; install into a private folder such as $HOME\bin\agiw-win."
}
$shared = [IO.Path]::GetFullPath('C:\SharedChami').TrimEnd('\')
if ($full.Equals($shared, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($shared + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "$Destination is shared, remote or a link; install into a private folder such as $HOME\bin\agiw-win."
}
$drive = New-Object IO.DriveInfo ([IO.Path]::GetPathRoot($full))
if ($drive.DriveType -eq [IO.DriveType]::Network) {
    throw "$Destination is shared, remote or a link; install into a private folder such as $HOME\bin\agiw-win."
}
# Retained no-follow handles keep every destination parent in place until the swap, rollback and
# optional shortcut update finish. ACLs exclude other users even for an empty, local destination.
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
public sealed class AgiwInstallGuard : IDisposable {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int kind, out AttributeTag info, uint size);
    [DllImport("advapi32.dll", SetLastError=true)]
    static extern uint GetSecurityInfo(SafeFileHandle handle, int kind, uint requested, out IntPtr owner,
        out IntPtr group, out IntPtr dacl, out IntPtr sacl, out IntPtr descriptor);
    [DllImport("advapi32.dll")] static extern uint GetSecurityDescriptorLength(IntPtr descriptor);
    [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr memory);
    [StructLayout(LayoutKind.Sequential)] struct AttributeTag { public uint Attributes; public uint Tag; }
    readonly Dictionary<string, SafeFileHandle> held = new Dictionary<string, SafeFileHandle>(StringComparer.OrdinalIgnoreCase);
    readonly string profile = Path.GetFullPath(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)).TrimEnd('\\');
    readonly List<string> createdDirectories = new List<string>();
    readonly string user = WindowsIdentity.GetCurrent().User.Value;
    bool Trusted(SecurityIdentifier sid, SecurityIdentifier owner = null) {
        return sid != null && (sid.Value == user || sid.Value == "S-1-5-18" || sid.Value == "S-1-5-32-544" ||
            sid.Value == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464" || // TrustedInstaller
            (sid.Value == "S-1-3-4" && Trusted(owner))); // OWNER RIGHTS applies only to this descriptor's trusted owner
    }
    bool InsideProfile(string path) {
        return path.Equals(profile, StringComparison.OrdinalIgnoreCase) || path.StartsWith(profile + "\\", StringComparison.OrdinalIgnoreCase);
    }
    public void CheckDestination(string path) {
        if (!path.StartsWith(profile + "\\", StringComparison.OrdinalIgnoreCase))
            throw new IOException("Install into a private folder beneath the current user's profile.");
    }
    void CheckAcl(SafeFileHandle handle, string path) {
        IntPtr owner, group, dacl, sacl, descriptor;
        uint error = GetSecurityInfo(handle, 1, 5, out owner, out group, out dacl, out sacl, out descriptor);
        if (error != 0) throw new Win32Exception((int)error, "Cannot verify private install permissions.");
        try {
            byte[] bytes = new byte[GetSecurityDescriptorLength(descriptor)];
            Marshal.Copy(descriptor, bytes, 0, bytes.Length);
            RawSecurityDescriptor security = new RawSecurityDescriptor(bytes, 0);
            CheckPermissions(security, path);
        } finally { LocalFree(descriptor); }
    }
    void CheckPermissions(RawSecurityDescriptor security, string path) {
        if (!Trusted(security.Owner) || security.DiscretionaryAcl == null)
            throw new IOException("Install path ownership is not private to this user or system administrators.");
        // Ancestors outside the profile may allow creating siblings, but never replacing this path.
        int writes = InsideProfile(path) ? 0x500d0156 : 0x500d0040;
        foreach (GenericAce entry in security.DiscretionaryAcl) {
            QualifiedAce ace = entry as QualifiedAce;
            if (ace == null) throw new IOException("Cannot verify an install path access rule.");
            bool inheritOnly = (ace.AceFlags & AceFlags.InheritOnly) != 0;
            // New files inherit permissions too; CREATOR OWNER resolves to this trusted creating user.
            bool creatorOwner = inheritOnly && ace.SecurityIdentifier.Value == "S-1-3-0";
            if (ace.AceQualifier == AceQualifier.AccessAllowed && (InsideProfile(path) || !inheritOnly) &&
                (ace.AccessMask & writes) != 0 && !Trusted(ace.SecurityIdentifier, security.Owner) && !creatorOwner)
                throw new IOException("Install path is writable by another user; choose a private folder.");
        }
    }
    SafeFileHandle Open(string path, bool directory) {
        // Directories never share DELETE; targets share it only so File.Replace can move the held old file.
        SafeFileHandle handle = CreateFileW(path, directory ? 0x20081u : 0x20080u, directory ? 3u : 5u, IntPtr.Zero, 3,
            0x02200000, IntPtr.Zero); // Directories also request FILE_LIST_DIRECTORY to enforce share-delete exclusion; BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        if (handle.IsInvalid) {
            int error = Marshal.GetLastWin32Error();
            handle.Dispose();
            if (error == 2 || error == 3) return null;
            throw new Win32Exception(error, "Cannot retain install path safely.");
        }
        try {
            AttributeTag info;
            if (!GetFileInformationByHandleEx(handle, 9, out info, 8)) throw new Win32Exception(Marshal.GetLastWin32Error());
            if ((info.Attributes & 0x400) != 0) throw new IOException("Install path is shared, remote or a link.");
            if (((info.Attributes & 0x10) != 0) != directory) throw new IOException("Unexpected install path type.");
            CheckAcl(handle, path);
            return handle;
        } catch { handle.Dispose(); throw; }
    }
    public void Directory(string path, bool create) {
        path = Path.GetFullPath(path).TrimEnd('\\');
        if (path.EndsWith(":")) path += "\\";
        if (held.ContainsKey(path)) return;
        string parent = Path.GetDirectoryName(path);
        if (!String.IsNullOrEmpty(parent) && !parent.Equals(path, StringComparison.OrdinalIgnoreCase)) Directory(parent, false);
        SafeFileHandle handle = Open(path, true);
        if (handle == null && create) {
            System.IO.Directory.CreateDirectory(path);
            createdDirectories.Add(path);
            handle = Open(path, true);
        }
        if (handle == null) throw new IOException("An install parent directory is missing.");
        held.Add(path, handle);
    }
    public void Target(string path) {
        Directory(Path.GetDirectoryName(path), true);
        if (held.ContainsKey(path)) return;
        SafeFileHandle handle = Open(path, false);
        if (handle != null) held.Add(path, handle);
    }
    public void ReleaseTree(string path) {
        foreach (string item in new List<string>(held.Keys)) {
            if (item.Equals(path, StringComparison.OrdinalIgnoreCase) || item.StartsWith(path + "\\", StringComparison.OrdinalIgnoreCase)) {
                held[item].Dispose(); held.Remove(item);
            }
        }
    }
    public void RemoveEmptyCreatedDirectories() {
        for (int i = createdDirectories.Count - 1; i >= 0; i--) {
            string path = createdDirectories[i];
            if (System.IO.Directory.Exists(path) && System.IO.Directory.GetFileSystemEntries(path).Length == 0) {
                ReleaseTree(path);
                System.IO.Directory.Delete(path);
            }
        }
    }
    public void Dispose() { foreach (SafeFileHandle handle in held.Values) handle.Dispose(); held.Clear(); }
}
'@
$Destination = $full.TrimEnd('\')
$sourceWindows = $PSScriptRoot
$marker = Join-Path $Destination '.agiw-win-install'
$guard = New-Object AgiwInstallGuard
$installSucceeded = $false
try {
    $guard.CheckDestination($Destination)
    $created = -not (Test-Path -LiteralPath $Destination)
    # Create any missing path one component at a time, retaining and validating its parent first.
    $missing = New-Object System.Collections.Generic.List[string]
    $part = $Destination
    while (-not (Test-Path -LiteralPath $part)) { $missing.Add($part); $part = Split-Path -Parent $part }
    $guard.Directory($part, $false)
    for ($i = $missing.Count - 1; $i -ge 0; $i--) { $guard.Directory($missing[$i], $true) }
    $guard.Target($marker)
    if (-not (Test-Path -LiteralPath $marker) -and
            @(Get-ChildItem -LiteralPath $Destination -Force -ErrorAction Stop).Count -gt 0) {
        throw "$Destination already has files that this installer did not create; choose an empty folder."
    }

# Every installed file as (source, path inside the install folder).
$files = New-Object System.Collections.Generic.List[object]
foreach ($item in Get-ChildItem -LiteralPath (Join-Path $sourceWindows 'agiw_win') -Filter '*.py' -File) {
    $files.Add(@($item.FullName, "agiw_win\$($item.Name)"))
}
foreach ($file in 'agiw_observer.py', 'agiw-monitor.ps1', 'AGIW Monitor.vbs', 'AGIW Monitor.cmd', 'README.md') {
    $files.Add(@((Join-Path $sourceWindows $file), $file))
}
foreach ($file in 'index.html', 'app.js', 'style.css', 'map-layout.mjs', 'model-control-view.mjs', 'online-code-mode.mjs') {
    $files.Add(@((Join-Path $repo "web\$file"), "web\$file"))
}
# peer_link.py is the LAN pairing service (agiw-peer/2), shipped by Louis's decision of 2026-10-17. Installing it
# starts nothing: it runs only when started (python peer_link.py run --status-file state\peer-link-status.json), after
# PIN pairing and the 8768/8769 firewall rules.
foreach ($file in 'usage-format.mjs', 'client_models.py', 'peer_link.py', 'LICENSE', 'NOTICE') {
    $files.Add(@((Join-Path $repo $file), $file))
}

function Test-InstalledInventory($Files, $Destination, $Stage, $Guard, $RejectUnexpected = $false) {
    # Retained no-follow directory handles also protect the inventory walk. Extra code is never deleted.
    $expected = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($pair in $Files) { [void]$expected.Add($pair[1]) }
    foreach ($name in '.agiw-win-install', 'build.json', 'config.json', 'lane-processes.json') { [void]$expected.Add($name) }
    $pending = New-Object System.Collections.Generic.Queue[string]
    $pending.Enqueue($Destination)
    while ($pending.Count) {
        $directory = $pending.Dequeue()
        $Guard.Directory($directory, $false)
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            if ($item.FullName.Equals($Stage, [StringComparison]::OrdinalIgnoreCase)) { continue }
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                if ($RejectUnexpected) { throw "Unexpected install entries (shared, remote or a link): $($item.FullName)" }
                return $false
            }
            $relative = $item.FullName.Substring($Destination.Length + 1)
            if ($item.PSIsContainer) {
                # Observer state is mutable and not on the package import path.
                if ($relative -eq 'state') { continue }
                if ($relative -notin @('agiw_win', 'web')) {
                    if ($RejectUnexpected) { throw "Unexpected install entries: $($item.FullName); inspect before retrying." }
                    return $false
                }
                $pending.Enqueue($item.FullName)
            } elseif (-not $expected.Contains($relative)) {
                if ($RejectUnexpected) { throw "Unexpected install entries: $($item.FullName); inspect before retrying." }
                return $false
            }
        }
    }
    return $true
}

function Test-StagedProvenance($Files, $Stage, $Repo, $Commit, $Dirty, $GitPath, $Destination, $Guard) {
    $result = @{ signatureVerified = $false; filesMatchHead = $null; provenance = 'unverified' }
    if (-not $GitPath -or -not $Commit) { return $result }
    try {
        & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo verify-commit $Commit 2>$null } | Out-Null
        $result.signatureVerified = $LASTEXITCODE -eq 0
        # A deleted tracked module is absent from the source glob and its scoped status command.
        $tree = & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo ls-tree -r --name-only $Commit -- windows/agiw_win 2>$null }
        if ($LASTEXITCODE -ne 0) { return $result }
        $expectedModules = @($tree | Where-Object { $_ -match '^windows/agiw_win/.+\.py$' })
        $actualModules = @($Files | ForEach-Object {
            $_[0].Substring($Repo.TrimEnd('\').Length + 1).Replace('\', '/')
        } | Where-Object { $_ -match '^windows/agiw_win/.+\.py$' })
        $result.filesMatchHead = $false
        if (-not $expectedModules.Count -or -not $actualModules.Count -or
                @(Compare-Object $expectedModules $actualModules).Count -ne 0) { return $result }
        if (-not $Destination -or -not $Guard -or -not (Test-InstalledInventory $Files $Destination $Stage $Guard)) { return $result }
        $result.filesMatchHead = $true
        foreach ($pair in $Files) {
            $relative = $pair[0].Substring($Repo.TrimEnd('\').Length + 1).Replace('\', '/')
            $expected = & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo rev-parse --verify "${Commit}:$relative" 2>$null }
            if ($LASTEXITCODE -ne 0 -or "$expected".Trim() -notmatch '^[0-9a-f]{40}$') {
                $result.filesMatchHead = $false; break
            }
            # Permit Git's declared text/EOL normalization, but never execute a custom clean filter.
            $filter = & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo check-attr filter -- $relative 2>$null }
            if ($LASTEXITCODE -ne 0 -or "$filter".Trim() -notmatch ': filter: (unspecified|unset)$') {
                $result.filesMatchHead = $false; break
            }
            $staged = Join-Path $Stage $pair[1]
            $actual = & { $ErrorActionPreference = 'Continue'; & $GitPath -C $Repo hash-object "--path=$relative" -- $staged 2>$null }
            if ($LASTEXITCODE -ne 0 -or "$actual".Trim() -ne "$expected".Trim()) {
                $result.filesMatchHead = $false; break
            }
        }
        if ($Dirty -is [bool] -and -not $Dirty -and $result.signatureVerified -and $result.filesMatchHead) {
            $result.provenance = 'signed-head'
        }
    } catch { $result.provenance = 'unverified'; $result.filesMatchHead = $null }
    return $result
}

# A commit identifies the checkout; only successful status, signature verification and staged-blob
# comparison justify a clean signed-head record. Any unavailable check remains explicitly unverified.
$initText = [IO.File]::ReadAllText((Join-Path $sourceWindows 'agiw_win\__init__.py'))
$versionMatch = [regex]::Match($initText, '(?m)^VERSION = "([0-9A-Za-z.+-]{1,32})"\s*$')
if (-not $versionMatch.Success) { throw "Cannot read VERSION from agiw_win\__init__.py." }
$commit = $null
$dirty = $null
$git = Get-Command git.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if ($git) {
    # git's stderr (warnings) must not abort the install under 'Stop', so each call runs with a local 'Continue'.
    try {
        $head = & { $ErrorActionPreference = 'Continue'; & $git.Source -C $repo rev-parse --verify HEAD 2>$null }
        if ($LASTEXITCODE -eq 0 -and "$head".Trim() -match '^[0-9a-f]{40}$') { $commit = "$head".Trim() }
    } catch { $commit = $null }
    if ($commit) {
        try {
            # The glob above also ships untracked or ignored modules; include every copied source in provenance.
            $sourcePaths = @($files | ForEach-Object { $_[0] })
            $changes = & { $ErrorActionPreference = 'Continue'; & $git.Source -C $repo status --porcelain --untracked-files=all --ignored -- $sourcePaths 2>$null }
            if ($LASTEXITCODE -eq 0) { $dirty = [bool]("$changes".Trim()) }
        } catch { $dirty = $null }
    }
}
$build = [ordered]@{
    schemaVersion = 1
    service = 'agiw-win-observer'
    version = $versionMatch.Groups[1].Value
    commit = $commit
    dirty = $dirty
    installedAt = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
    source = 'install.ps1'
}

# Reject stale executable code and recovery directories before changing any package files.
if (-not (Test-InstalledInventory $files $Destination '' $guard $true)) {
    throw "Unexpected install entries in $Destination; inspect extra code or .agiw-stage-*/.agiw-backup-* recovery folders before retrying."
}
$state = Join-Path $Destination 'state'
if (Test-Path -LiteralPath $state) { $guard.Directory($state, $false) }
$link = $null
if ($StartAtSignIn) {
    $startup = [Environment]::GetFolderPath('Startup')
    $guard.CheckDestination($startup)
    $guard.Directory($startup, $false)
    $link = Join-Path $startup 'AGIW Inference Monitor.lnk'
    $guard.Target($link)
    $shell = New-Object -ComObject WScript.Shell
    $wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $launcher = Join-Path $Destination 'AGIW Monitor.vbs'
    $expectedArguments = '"{0}" /quiet' -f $launcher
    if (Test-Path -LiteralPath $link) {
        $existing = $shell.CreateShortcut($link)
        if ($existing.TargetPath -ine $wscript -or
                $existing.Arguments -cne $expectedArguments -or
                $existing.WorkingDirectory -ine $Destination) {
            throw "A different 'AGIW Inference Monitor' sign-in shortcut exists; remove it first."
        }
    }
}

# The running tray and observer keep the folder and state\ open, so the folder itself is never swapped.
# Every file is staged on the same volume first, then renamed into place one by one with the old copy
# kept; any failure puts every old file back, so an update is never left half-applied.
$token = [Guid]::NewGuid().ToString('N').Substring(0, 12)
$stage = Join-Path $Destination ".agiw-stage-$token"
$backup = Join-Path $Destination ".agiw-backup-$token"
$added = New-Object System.Collections.Generic.List[string]
$keepBackup = $false
$replaced = New-Object System.Collections.Generic.List[object]
try {
    foreach ($relative in @($files | ForEach-Object { $_[1] }) + @('build.json')) {
        $guard.Target((Join-Path $Destination $relative))
    }
    $guard.Directory($stage, $true)
    foreach ($pair in $files) {
        $staged = Join-Path $stage $pair[1]
        $guard.Directory((Split-Path -Parent $staged), $true)
        Copy-Item -LiteralPath $pair[0] -Destination $staged -Force
        if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $pair[0] -Algorithm SHA256).Hash) {
            throw "Staging $($pair[1]) was incomplete."
        }
    }
    if ($StartAtSignIn) {
        $stagedLink = Join-Path $stage 'startup.lnk'
        $shortcut = $shell.CreateShortcut($stagedLink)
        $shortcut.TargetPath = $wscript
        $shortcut.Arguments = $expectedArguments
        $shortcut.WorkingDirectory = $Destination
        $shortcut.Save()
        if (-not (Test-Path -LiteralPath $stagedLink)) { throw 'Sign-in shortcut staging failed.' }
    }
    # The generated build record is staged and swapped like every copied file, so a rollback restores it too.
    $provenance = Test-StagedProvenance $files $stage $repo $commit $dirty $(if ($git) { $git.Source }) $Destination $guard
    $build.signatureVerified = $provenance.signatureVerified
    $build.filesMatchHead = $provenance.filesMatchHead
    $build.provenance = $provenance.provenance
    $buildJson = $build | ConvertTo-Json -Compress
    [IO.File]::WriteAllText((Join-Path $stage 'build.json'), $buildJson, (New-Object Text.UTF8Encoding $false))
    $installed = @($files | ForEach-Object { $_[1] }) + @('build.json')
    foreach ($relative in $installed) {
        $target = Join-Path $Destination $relative
        $staged = Join-Path $stage $relative
        $guard.Directory((Split-Path -Parent $target), $false)
        if (Test-Path -LiteralPath $target) {
            $saved = Join-Path $backup $relative
            $guard.Directory($backup, $true)
            $guard.Directory((Split-Path -Parent $saved), $true)
            $replaced.Add(@($saved, $target))
            [IO.File]::Replace($staged, $target, $saved)
        } else {
            [IO.File]::Move($staged, $target)
            $added.Add($target)
        }
    }
    if ($StartAtSignIn) {
        if (Test-Path -LiteralPath $link) {
            $guard.Directory($backup, $true)
            $savedLink = Join-Path $backup 'startup.lnk'
            $replaced.Add(@($savedLink, $link))
            [IO.File]::Replace($stagedLink, $link, $savedLink)
        } else {
            [IO.File]::Move($stagedLink, $link)
            $added.Add($link)
        }
    }
    if (-not (Test-Path -LiteralPath $marker)) {
        $added.Add($marker)
        Set-Content -LiteralPath $marker -Value 'AGIW Inference Monitor (Windows) install folder' -Encoding ASCII
    }
} catch {
    $failure = $_
    $rollbackErrors = New-Object System.Collections.Generic.List[string]
    # Try every restoration even when one file is locked; preserve backups for any incomplete rollback.
    foreach ($pair in $replaced) {
        try {
            if (Test-Path -LiteralPath $pair[0]) { [IO.File]::Copy($pair[0], $pair[1], $true) }
        } catch { $rollbackErrors.Add("Restore failed: $($pair[1]): $($_.Exception.Message)") }
    }
    foreach ($target in $added) {
        try {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force -ErrorAction Stop }
            if (Test-Path -LiteralPath $target) { throw "Rollback left file: $target" }
        } catch { $rollbackErrors.Add("Rollback left file: $target ($($_.Exception.Message))") }
    }
    if ($rollbackErrors.Count) {
        $keepBackup = $true
        throw "Install failed ($($failure.Exception.Message)) and rollback was incomplete; previous files are in $backup. $($rollbackErrors -join '; ')"
    }
    throw "Install failed and was rolled back; the previous install is unchanged. $($failure.Exception.Message)"
} finally {
    $guard.ReleaseTree($stage)
    $guard.ReleaseTree($backup)
    foreach ($cleanup in @($stage) + $(if (-not $keepBackup) { @($backup) } else { @() })) {
        try {
            if (Test-Path -LiteralPath $cleanup) { Remove-Item -LiteralPath $cleanup -Recurse -Force -ErrorAction Stop }
            if (Test-Path -LiteralPath $cleanup) { throw 'Path remains.' }
        } catch { Write-Warning "Install cleanup left: $cleanup ($($_.Exception.Message))" }
    }
}

$installSucceeded = $true
Write-Host "Installed to $Destination"
if ($Launch) { Start-Process (Join-Path $env:SystemRoot 'System32\wscript.exe') -ArgumentList ('"{0}"' -f (Join-Path $Destination 'AGIW Monitor.vbs')) }

} finally {
    try {
        if (-not $installSucceeded) { $guard.RemoveEmptyCreatedDirectories() }
    } catch { Write-Warning "Install cleanup left empty directories: $($_.Exception.Message)" }
    finally { $guard.Dispose() }
}
