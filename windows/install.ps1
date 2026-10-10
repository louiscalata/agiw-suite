<#
  Copy the Windows edition from a repository checkout into an install folder.
  Usage (from the repository root):  powershell -NoProfile -File windows\install.ps1 [-Destination <folder>] [-StartAtSignIn]
  It copies files only; it does not start anything unless -Launch is given.
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
$part = $full
while ($part) {
    # GetAttributes reads the link itself, so a dangling junction is refused too.
    $attributes = $null
    try { $attributes = [IO.File]::GetAttributes($part) } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { }
    if ($null -ne $attributes -and ($attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "$Destination is shared, remote or a link; install into a private folder such as $HOME\bin\agiw-win."
    }
    $parent = Split-Path -Parent $part
    if (-not $parent -or $parent -eq $part) { break }
    $part = $parent
}
$Destination = $full
$sourceWindows = $PSScriptRoot
$marker = Join-Path $Destination '.agiw-win-install'
# Only overwrite a folder this installer created (it leaves a marker), or an empty or new one.
if ((Test-Path -LiteralPath $Destination) -and -not (Test-Path -LiteralPath $marker) -and
        @(Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue).Count -gt 0) {
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
foreach ($file in 'usage-format.mjs', 'client_models.py', 'LICENSE', 'NOTICE') {
    $files.Add(@((Join-Path $repo $file), $file))
}

# The running tray and observer keep the folder and state\ open, so the folder itself is never swapped.
# Every file is staged on the same volume first, then renamed into place one by one with the old copy
# kept; any failure puts every old file back, so an update is never left half-applied.
$created = -not (Test-Path -LiteralPath $Destination)
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$token = [Guid]::NewGuid().ToString('N').Substring(0, 12)
$stage = Join-Path $Destination ".agiw-stage-$token"
$backup = Join-Path $Destination ".agiw-backup-$token"
$added = New-Object System.Collections.Generic.List[string]
$keepBackup = $false
try {
    foreach ($pair in $files) {
        $staged = Join-Path $stage $pair[1]
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $staged) | Out-Null
        Copy-Item -LiteralPath $pair[0] -Destination $staged -Force
        if ((Get-Item -LiteralPath $staged -Force).Length -ne (Get-Item -LiteralPath $pair[0] -Force).Length) {
            throw "Staging $($pair[1]) was incomplete."
        }
    }
    foreach ($pair in $files) {
        $target = Join-Path $Destination $pair[1]
        $staged = Join-Path $stage $pair[1]
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        if (Test-Path -LiteralPath $target) {
            $saved = Join-Path $backup $pair[1]
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $saved) | Out-Null
            [IO.File]::Replace($staged, $target, $saved)
        } else {
            [IO.File]::Move($staged, $target)
            $added.Add($target)
        }
    }
    Set-Content -LiteralPath $marker -Value 'AGIW Inference Monitor (Windows) install folder' -Encoding ASCII
} catch {
    $failure = $_
    try {
        # Every file with a saved copy gets it back, including one a failed Replace left missing.
        if (Test-Path -LiteralPath $backup) {
            foreach ($saved in Get-ChildItem -LiteralPath $backup -Recurse -File -Force) {
                $relative = $saved.FullName.Substring($backup.Length + 1)
                [IO.File]::Copy($saved.FullName, (Join-Path $Destination $relative), $true)
            }
        }
        foreach ($target in $added) { Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue }
    } catch {
        $keepBackup = $true
        throw "Install failed ($($failure.Exception.Message)) and rollback was incomplete; the previous files are in $backup."
    }
    throw "Install failed and was rolled back; the previous install is unchanged. $($failure.Exception.Message)"
} finally {
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $keepBackup) { Remove-Item -LiteralPath $backup -Recurse -Force -ErrorAction SilentlyContinue }
    if ($created -and -not (Test-Path -LiteralPath $marker)) {
        Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($StartAtSignIn) {
    $link = Join-Path ([Environment]::GetFolderPath('Startup')) 'AGIW Inference Monitor.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $launcher = Join-Path $Destination 'AGIW Monitor.vbs'
    if ((Test-Path -LiteralPath $link) -and -not $shell.CreateShortcut($link).Arguments.ToLowerInvariant().Contains(('"' + $launcher + '"').ToLowerInvariant())) {
        throw "A different 'AGIW Inference Monitor' sign-in shortcut exists; remove it first."
    }
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $shortcut.Arguments = '"{0}" /quiet' -f (Join-Path $Destination 'AGIW Monitor.vbs')
    $shortcut.WorkingDirectory = $Destination
    $shortcut.Save()
}
Write-Host "Installed to $Destination"
if ($Launch) { Start-Process (Join-Path $env:SystemRoot 'System32\wscript.exe') -ArgumentList ('"{0}"' -f (Join-Path $Destination 'AGIW Monitor.vbs')) }
