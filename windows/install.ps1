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
$sourceWindows = $PSScriptRoot
$marker = Join-Path $Destination '.agiw-win-install'
# Only overwrite a folder this installer created (it leaves a marker), or an empty or new one.
if ((Test-Path -LiteralPath $Destination) -and -not (Test-Path -LiteralPath $marker) -and
        @(Get-ChildItem -LiteralPath $Destination -Force -ErrorAction SilentlyContinue).Count -gt 0) {
    throw "$Destination already has files that this installer did not create; choose an empty folder."
}

New-Item -ItemType Directory -Force -Path $Destination, (Join-Path $Destination 'agiw_win'), (Join-Path $Destination 'web') | Out-Null
Copy-Item -Path (Join-Path $sourceWindows 'agiw_win\*.py') -Destination (Join-Path $Destination 'agiw_win') -Force
foreach ($file in 'agiw_observer.py', 'agiw-monitor.ps1', 'AGIW Monitor.vbs', 'AGIW Monitor.cmd', 'README.md') {
    Copy-Item -LiteralPath (Join-Path $sourceWindows $file) -Destination $Destination -Force
}
foreach ($file in 'index.html', 'app.js', 'style.css', 'map-layout.mjs', 'model-control-view.mjs', 'online-code-mode.mjs') {
    Copy-Item -LiteralPath (Join-Path $repo "web\$file") -Destination (Join-Path $Destination 'web') -Force
}
Copy-Item -LiteralPath (Join-Path $repo 'usage-format.mjs') -Destination $Destination -Force
Copy-Item -LiteralPath (Join-Path $repo 'client_models.py') -Destination $Destination -Force
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination $Destination -Force
Copy-Item -LiteralPath (Join-Path $repo 'NOTICE') -Destination $Destination -Force
Set-Content -LiteralPath $marker -Value 'AGIW Inference Monitor (Windows) install folder' -Encoding ASCII

if ($StartAtSignIn) {
    $link = Join-Path ([Environment]::GetFolderPath('Startup')) 'AGIW Inference Monitor.lnk'
    $shell = New-Object -ComObject WScript.Shell
    $launcher = Join-Path $Destination 'AGIW Monitor.vbs'
    if ((Test-Path -LiteralPath $link) -and ($shell.CreateShortcut($link).Arguments -notlike ('*' + $launcher + '*'))) {
        throw "A different 'AGIW Inference Monitor' sign-in shortcut exists; remove it first."
    }
    $shortcut = $shell.CreateShortcut($link)
    $shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $shortcut.Arguments = '"{0}" /quiet' -f (Join-Path $Destination 'AGIW Monitor.vbs')
    $shortcut.WorkingDirectory = $Destination
    $shortcut.Save()
}
Write-Host "Installed to $Destination"
if ($Launch) { Start-Process wscript.exe -ArgumentList ('"{0}"' -f (Join-Path $Destination 'AGIW Monitor.vbs')) }
