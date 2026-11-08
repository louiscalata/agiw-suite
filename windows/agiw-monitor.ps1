<#
  AGIW Inference Monitor | Windows tray shell.

  Supervises the loopback observer (agiw_observer.py), shows lane status in the
  notification area, and opens the dashboard. Runs under Windows PowerShell 5.1
  or PowerShell 7 with -STA. Nothing here loads, unloads or restarts a model.
#>
param(
    [ValidateRange(0, 65535)][int]$Port = 8767,
    [switch]$OpenDashboard
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$Root = $PSScriptRoot
$StateDir = Join-Path $Root 'state'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$LogPath = Join-Path $StateDir 'monitor.log'
$ObserverLog = Join-Path $StateDir 'observer.log'
$StatePath = Join-Path $StateDir 'observer.json'
$StartupLink = Join-Path ([Environment]::GetFolderPath('Startup')) 'AGIW Inference Monitor.lnk'
$Launcher = Join-Path $Root 'AGIW Monitor.vbs'

function Write-Log([string]$Message) {
    try {
        if ((Test-Path $LogPath) -and (Get-Item $LogPath).Length -gt 1MB) { Move-Item $LogPath "$LogPath.1" -Force }
        # One line per entry: control characters (CR/LF included) are escaped so a message cannot forge lines.
        $clean = ($Message -replace '[\x00-\x1f\x7f]', ' ')
        if ($clean.Length -gt 500) { $clean = $clean.Substring(0, 500) }
        Add-Content -Path $LogPath -Value ("{0} {1}" -f (Get-Date).ToString('o'), $clean) -Encoding UTF8
    } catch { }
}

# ---------------------------------------------------------------- single instance
$createdNew = $false
$Mutex = New-Object System.Threading.Mutex($true, 'Local\AGIW-Inference-Monitor-Windows', [ref]$createdNew)
if (-not $createdNew) {
    # Already running: open its dashboard. The URL is rebuilt from a validated port, never taken from the file.
    try {
        $saved = Get-Content -Raw $StatePath | ConvertFrom-Json
        $savedPort = [int]$saved.port
        if ($saved.service -eq 'agiw-win-observer' -and $savedPort -ge 1024 -and $savedPort -le 65535) {
            Start-Process ("http://127.0.0.1:{0}/monitor" -f $savedPort)
        }
    } catch { }
    exit 0
}

# ---------------------------------------------------------------- python
function Find-Python {
    $candidates = @()
    if ($env:AGIW_PYTHON) { $candidates += $env:AGIW_PYTHON }
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    foreach ($name in 'python.exe', 'py.exe') {
        $found = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found -and $found.Source -notlike '*WindowsApps*') { $candidates += $found.Source }
    }
    foreach ($candidate in $candidates) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        # Only an interpreter that reports Python 3.9+ is used.
        try {
            $version = & $candidate -I -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($version -and [version]$version -ge [version]'3.9') { return $candidate }
        } catch { }
    }
    return $null
}
$Python = Find-Python

# ---------------------------------------------------------------- observer supervision
$script:Observer = $null
$script:ObserverPort = $null
$script:Backoff = 2
$script:NextStart = [DateTime]::MinValue
$script:LastError = $null

function Start-Observer {
    if (-not $Python) {
        $script:LastError = 'Python 3.9+ not found (set AGIW_PYTHON)'
        Write-Log $script:LastError
        $script:NextStart = (Get-Date).AddSeconds($script:Backoff)
        $script:Backoff = [Math]::Min(300, $script:Backoff * 2)
        return
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $observerScript = Join-Path $Root 'agiw_observer.py'
    # -I: isolated mode ignores PYTHON* variables and the user site directory; the observer adds its own folder.
    $info.Arguments = ('-I -B "{0}" --port {1} --parent-pid {2} --state-file "{3}" --log-file "{4}"' -f $observerScript, $Port, $PID, $StatePath, $ObserverLog)
    $info.WorkingDirectory = $Root
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $process = $null
    try {
        $process = [System.Diagnostics.Process]::Start($info)
        $read = $process.StandardOutput.ReadLineAsync()
        if (-not $read.Wait(20000) -or -not $read.Result) { throw 'observer did not report its port within 20 s' }
        $hello = $read.Result | ConvertFrom-Json
        if ($hello.error -eq 'port-unavailable') { throw "port $($hello.port) is in use by another program or observer" }
        if ($hello.service -ne 'agiw-win-observer' -or -not $hello.port) { throw "unexpected handshake: $($read.Result)" }
        $script:Observer = $process
        $script:ObserverPort = [int]$hello.port
        $script:Backoff = 2
        $script:LastError = $null
        Write-Log "observer pid $($process.Id) on port $($script:ObserverPort)"
    } catch {
        $script:LastError = "Observer failed to start: $($_.Exception.Message)"
        Write-Log $script:LastError
        if ($process -and -not $process.HasExited) { try { $process.Kill() } catch { } }
        $script:Observer = $null
        $script:NextStart = (Get-Date).AddSeconds($script:Backoff)
        $script:Backoff = [Math]::Min(30, $script:Backoff * 2)
    }
}

function Stop-Observer {
    if ($script:Observer -and -not $script:Observer.HasExited) {
        try { $script:Observer.Kill(); [void]$script:Observer.WaitForExit(3000) } catch { }
    }
    $script:Observer = $null
    $script:ObserverPort = $null
}

function Get-DashboardUrl { if ($script:ObserverPort) { "http://127.0.0.1:$($script:ObserverPort)/" } else { $null } }

function Open-Dashboard {
    $url = Get-DashboardUrl
    if (-not $url) { [System.Windows.Forms.MessageBox]::Show('The observer is not running yet.', 'AGIW Monitor') | Out-Null; return }
    $url = $url + 'monitor'
    $edge = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    # An Edge app window feels like the Mac edition's dashboard window; any browser works.
    if ($edge) { Start-Process -FilePath $edge -ArgumentList "--app=$url" } else { Start-Process $url }
}

# ---------------------------------------------------------------- icons
# One black-and-white AGIW mark per state. The state is a shape (plain, inverted, framed, dashed),
# never a colour; the tray text says it in words.
# BEGIN generated tray icons (brand/build-icons.py; do not edit by hand)
$TrayIconData = @{
    ok = 'AAABAAIAEBAAAAEAIABoBAAAJgAAACAgAAABACAAqBAAAI4EAAAoAAAAEAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/x4eHv++vr7/AgIC/wAAAP8/Pz//oKCg/wAAAP9ra2v/zs7O/8XFxf9MTEz/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/1NTU/4CAgP9mZmb/urq6/3Z2dv9OTk7/09PT/yUlJf8wMDD/5ubm/xgYGP8AAAD/AAAA/wAAAP8AAAD/AAAA/3t7e//Hx8f/bGxs//v7+/8dHR3/m5ub/2lpaf8AAAD/paWl/+fn5/9ERET/AAAA/wAAAP8AAAD/AAAA/wAAAP8jIyP/4ODg/0hISP+/v7//AAAA/5ubm/9vb2//AAAA/yEhIf8iIiL/CgoK/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/8jIyP+5ubn/Y2Nj/wAAAP9NTU3/19fX/ygoKP9CQkL/6+vr/xAQEP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP9gYGD/ysrK/xAQEP8AAAD/AAAA/2lpaf/Ozs7/urq6/z4+Pv8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAIAQAAAAAAAAAAAAAAAAAAAAAAAAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/0dHR/+ZmZn/bW1t/wAAAP8AAAD/AAAA/wAAAP8AAAD/RERE/5mZmf9ycnL/AAAA/wAAAP8AAAD/LS0t/4iIiP+tra3/pKSk/3Fxcf8UFBT/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/Ly8v///////x8fH/CAgI/wAAAP8AAAD/AAAA/wAAAP+4uLj//////3V1df8AAAD/AAAA/4GBgf/+/v7//////////////////////+jo6P82Njb/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/1dXV//////9OTk7/AAAA/wAAAP8AAAD/FRUV//v7+//9/f3/HBwc/wAAAP9eXl7///////X19f9tbW3/IyMj/ywsLP+Tk5P//////+Xl5f8JCQn/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP98fHz//////+bm5v/MzMz/zMzM/8zMzP/a2tr//////76+vv8AAAD/AQEB/9zc3P//////V1dX/wAAAP8AAAD/AAAA/wAAAP+ysrL//////1hYWP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/yQkJP///////////9HR0f/MzMz/zMzM//n5+f//////Y2Nj/wAAAP8pKSn//////+Pj4/8BAQH/AAAA/wAAAP9RUVH/VVVV/6Ghof//////goKC/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/8rKyv//////S0tL/wAAAP8YGBj//Pz8//f39/8QEBD/AAAA/0ZGRv//////vb29/wAAAP8AAAD/AAAA//Ly8v////////////////+Ojo7/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/cXFx//////+dnZ3/AAAA/2pqav//////rKys/wAAAP8AAAD/RkZG//////+4uLj/AAAA/wAAAP8AAAD/QUFB/0RERP9ERET/RERE/yYmJv8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8aGhr//f39/+bm5v8BAQH/t7e3//////9RUVH/AAAA/wAAAP8pKSn//////+Hh4f8BAQH/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP+9vb3//////zc3N//39/f/7u7u/wcHB/8AAAD/AAAA/wAAAP/a2tr//////09PT/8AAAD/AAAA/wAAAP8CAgL/zc3N//////9SUlL/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/2VlZf//////uLi4//////+ampr/AAAA/wAAAP8AAAD/AAAA/1paWv//////8vLy/2RkZP8aGhr/Li4u/6enp///////3t7e/wgICP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/EhIS//n5+f///////////z4+Pv8AAAD/AAAA/wAAAP8AAAD/AAAA/319ff/+/v7//////////////////////+Li4v8sLCz/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/dXV1/5mZmf+RkZH/AgIC/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/y0tLf+Kior/r6+v/6SkpP9qamr/DAwM/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    live = 'AAABAAIAEBAAAAEAIABoBAAAJgAAACAgAAABACAAqBAAAI4EAAAoAAAAEAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////////////////////////////////////////////////////////////////////wAAAP8AAAD///////////////////////////////////////////////////////////////////////////8AAAD/AAAA////////////////////////////////////////////////////////////////////////////AAAA/wAAAP///////////////////////////////////////////////////////////////////////////wAAAP8AAAD//////+Hh4f9BQUH//f39///////AwMD/X19f//////+UlJT/MTEx/zo6Ov+zs7P///////////8AAAD/AAAA////////////Kysr/39/f/+ZmZn/RUVF/4mJif+xsbH/LCws/9ra2v/Pz8//GRkZ/+fn5///////AAAA/wAAAP///////////4SEhP84ODj/k5OT/wQEBP/i4uL/ZGRk/5aWlv//////Wlpa/xgYGP+7u7v//////wAAAP8AAAD////////////c3Nz/Hx8f/7e3t/9AQED//////2RkZP+QkJD//////97e3v/d3d3/9fX1//////8AAAD/AAAA/////////////////zc3N/9GRkb/nJyc//////+ysrL/KCgo/9fX1/+9vb3/FBQU/+/v7///////AAAA/wAAAP////////////////+fn5//NTU1/+/v7////////////5aWlv8xMTH/RUVF/8HBwf///////////wAAAP8AAAD///////////////////////////////////////////////////////////////////////////8AAAD/AAAA////////////////////////////////////////////////////////////////////////////AAAA/wAAAP///////////////////////////////////////////////////////////////////////////wAAAP8AAAD///////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAIAQAAAAAAAAAAAAAAAAAAAAAAAAAAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA/////////////////7i4uP9mZmb/kpKS////////////////////////////u7u7/2ZmZv+NjY3/////////////////0tLS/3d3d/9SUlL/W1tb/46Ojv/r6+v///////////////////////////8AAAD/AAAA/wAAAP8AAAD/////////////////0NDQ/wAAAP8ODg7/9/f3//////////////////////9HR0f/AAAA/4qKiv///////////35+fv8BAQH/AAAA/wAAAP8AAAD/AAAA/xcXF//Jycn//////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////Kioq/wAAAP+xsbH/////////////////6urq/wQEBP8CAgL/4+Pj//////+hoaH/AAAA/woKCv+SkpL/3Nzc/9PT0/9sbGz/AAAA/xoaGv/29vb/////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////+Dg4P/AAAA/xkZGf8zMzP/MzMz/zMzM/8lJSX/AAAA/0FBQf///////v7+/yMjI/8AAAD/qKio//////////////////////9NTU3/AAAA/6enp/////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////9vb2/8AAAD/AAAA/y4uLv8zMzP/MzMz/wYGBv8AAAD/nJyc///////W1tb/AAAA/xwcHP/+/v7///////////+urq7/qqqq/15eXv8AAAD/fX19/////////////////wAAAP8AAAD/AAAA/wAAAP///////////////////////////zU1Nf8AAAD/tLS0///////n5+f/AwMD/wgICP/v7+///////7m5uf8AAAD/QkJC/////////////////w0NDf8AAAD/AAAA/wAAAP9xcXH/////////////////AAAA/wAAAP8AAAD/AAAA////////////////////////////jo6O/wAAAP9iYmL//////5WVlf8AAAD/U1NT////////////ubm5/wAAAP9HR0f/////////////////vr6+/7u7u/+7u7v/u7u7/9nZ2f////////////////8AAAD/AAAA/wAAAP8AAAD////////////////////////////l5eX/AgIC/xkZGf/+/v7/SEhI/wAAAP+urq7////////////W1tb/AAAA/x4eHv/+/v7//////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP////////////////////////////////9CQkL/AAAA/8jIyP8ICAj/ERER//j4+P////////////////8lJSX/AAAA/7CwsP/////////////////9/f3/MjIy/wAAAP+tra3/////////////////AAAA/wAAAP8AAAD/AAAA/////////////////////////////////5qamv8AAAD/R0dH/wAAAP9lZWX//////////////////////6Wlpf8AAAD/DQ0N/5ubm//l5eX/0dHR/1hYWP8AAAD/ISEh//f39/////////////////8AAAD/AAAA/wAAAP8AAAD/////////////////////////////////7e3t/wYGBv8AAAD/AAAA/8HBwf///////////////////////////4KCgv8BAQH/AAAA/wAAAP8AAAD/AAAA/x0dHf/T09P//////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////ioqK/2ZmZv9ubm7//f39/////////////////////////////////9LS0v91dXX/UFBQ/1tbW/+VlZX/8/Pz////////////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    warn = 'AAABAAIAEBAAAAEAIABoBAAAJgAAACAgAAABACAAqBAAAI4EAAAoAAAAEAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAA//////////////////////////////////////////////////////////////////////////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/x4eHv++vr7/AgIC/wAAAP8/Pz//oKCg/wAAAP9ra2v/zs7O/8XFxf9MTEz/AAAA/wAAAP///////////wAAAP8AAAD/1NTU/4CAgP9mZmb/urq6/3Z2dv9OTk7/09PT/yUlJf8wMDD/5ubm/xgYGP8AAAD///////////8AAAD/AAAA/3t7e//Hx8f/bGxs//v7+/8dHR3/m5ub/2lpaf8AAAD/paWl/+fn5/9ERET/AAAA////////////AAAA/wAAAP8jIyP/4ODg/0hISP+/v7//AAAA/5ubm/9vb2//AAAA/yEhIf8iIiL/CgoK/wAAAP///////////wAAAP8AAAD/AAAA/8jIyP+5ubn/Y2Nj/wAAAP9NTU3/19fX/ygoKP9CQkL/6+vr/xAQEP8AAAD///////////8AAAD/AAAA/wAAAP9gYGD/ysrK/xAQEP8AAAD/AAAA/2lpaf/Ozs7/urq6/z4+Pv8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////////////////////////////////////////////////////////////////////////////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAIAQAAAAAAAAAAAAAAAAAAAAAAAA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/0dHR/+ZmZn/bW1t/wAAAP8AAAD/AAAA/wAAAP8AAAD/RERE/5mZmf9ycnL/AAAA/wAAAP8AAAD/LS0t/4iIiP+tra3/pKSk/3Fxcf8UFBT/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/Ly8v///////x8fH/CAgI/wAAAP8AAAD/AAAA/wAAAP+4uLj//////3V1df8AAAD/AAAA/4GBgf/+/v7//////////////////////+jo6P82Njb/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/1dXV//////9OTk7/AAAA/wAAAP8AAAD/FRUV//v7+//9/f3/HBwc/wAAAP9eXl7///////X19f9tbW3/IyMj/ywsLP+Tk5P//////+Xl5f8JCQn/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP98fHz//////+bm5v/MzMz/zMzM/8zMzP/a2tr//////76+vv8AAAD/AQEB/9zc3P//////V1dX/wAAAP8AAAD/AAAA/wAAAP+ysrL//////1hYWP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/yQkJP///////////9HR0f/MzMz/zMzM//n5+f//////Y2Nj/wAAAP8pKSn//////+Pj4/8BAQH/AAAA/wAAAP9RUVH/VVVV/6Ghof//////goKC/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/8rKyv//////S0tL/wAAAP8YGBj//Pz8//f39/8QEBD/AAAA/0ZGRv//////vb29/wAAAP8AAAD/AAAA//Ly8v////////////////+Ojo7/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/cXFx//////+dnZ3/AAAA/2pqav//////rKys/wAAAP8AAAD/RkZG//////+4uLj/AAAA/wAAAP8AAAD/QUFB/0RERP9ERET/RERE/yYmJv8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8aGhr//f39/+bm5v8BAQH/t7e3//////9RUVH/AAAA/wAAAP8pKSn//////+Hh4f8BAQH/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP+9vb3//////zc3N//39/f/7u7u/wcHB/8AAAD/AAAA/wAAAP/a2tr//////09PT/8AAAD/AAAA/wAAAP8CAgL/zc3N//////9SUlL/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/2VlZf//////uLi4//////+ampr/AAAA/wAAAP8AAAD/AAAA/1paWv//////8vLy/2RkZP8aGhr/Li4u/6enp///////3t7e/wgICP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/EhIS//n5+f///////////z4+Pv8AAAD/AAAA/wAAAP8AAAD/AAAA/319ff/+/v7//////////////////////+Li4v8sLCz/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/dXV1/5mZmf+RkZH/AgIC/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/y0tLf+Kior/r6+v/6SkpP9qamr/DAwM/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    muted = 'AAABAAIAEBAAAAEAIABoBAAAJgAAACAgAAABACAAqBAAAI4EAAAoAAAAEAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAA/////4CAgP8AAAD/gICA//////+AgID/AAAA/4CAgP//////gICA/wAAAP+AgID//////4CAgP8AAAD/wMDA/4CAgP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP+AgID/gICA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/4CAgP+AgID/AAAA/x4eHv++vr7/AgIC/wAAAP8/Pz//oKCg/wAAAP9ra2v/zs7O/8XFxf9MTEz/AAAA/wAAAP//////AAAA/wAAAP8AAAD/1NTU/4CAgP9mZmb/urq6/3Z2dv9OTk7/09PT/yUlJf8wMDD/5ubm/xgYGP8AAAD/gICA/4CAgP8AAAD/AAAA/3t7e//Hx8f/bGxs//v7+/8dHR3/m5ub/2lpaf8AAAD/paWl/+fn5/9ERET/AAAA/wAAAP//////AAAA/wAAAP8jIyP/4ODg/0hISP+/v7//AAAA/5ubm/9vb2//AAAA/yEhIf8iIiL/CgoK/wAAAP+AgID/gICA/wAAAP8AAAD/AAAA/8jIyP+5ubn/Y2Nj/wAAAP9NTU3/19fX/ygoKP9CQkL/6+vr/xAQEP8AAAD//////wAAAP8AAAD/AAAA/wAAAP9gYGD/ysrK/xAQEP8AAAD/AAAA/2lpaf/Ozs7/urq6/z4+Pv8AAAD/AAAA/4CAgP+AgID/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/gICA/4CAgP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP+AgID/wMDA//////+AgID/AAAA/4CAgP//////gICA/wAAAP+AgID//////4CAgP8AAAD/gICA//////+AgID/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAoAAAAIAAAAEAAAAABACAAAAAAAIAQAAAAAAAAAAAAAAAAAAAAAAAA////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/0dHR/+ZmZn/bW1t/wAAAP8AAAD/AAAA/wAAAP8AAAD/RERE/5mZmf9ycnL/AAAA/wAAAP8AAAD/LS0t/4iIiP+tra3/pKSk/3Fxcf8UFBT/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/Ly8v///////x8fH/CAgI/wAAAP8AAAD/AAAA/wAAAP+4uLj//////3V1df8AAAD/AAAA/4GBgf/+/v7//////////////////////+jo6P82Njb/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/1dXV//////9OTk7/AAAA/wAAAP8AAAD/FRUV//v7+//9/f3/HBwc/wAAAP9eXl7///////X19f9tbW3/IyMj/ywsLP+Tk5P//////+Xl5f8JCQn/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP98fHz//////+bm5v/MzMz/zMzM/8zMzP/a2tr//////76+vv8AAAD/AQEB/9zc3P//////V1dX/wAAAP8AAAD/AAAA/wAAAP+ysrL//////1hYWP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/yQkJP///////////9HR0f/MzMz/zMzM//n5+f//////Y2Nj/wAAAP8pKSn//////+Pj4/8BAQH/AAAA/wAAAP9RUVH/VVVV/6Ghof//////goKC/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/8rKyv//////S0tL/wAAAP8YGBj//Pz8//f39/8QEBD/AAAA/0ZGRv//////vb29/wAAAP8AAAD/AAAA//Ly8v////////////////+Ojo7/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/cXFx//////+dnZ3/AAAA/2pqav//////rKys/wAAAP8AAAD/RkZG//////+4uLj/AAAA/wAAAP8AAAD/QUFB/0RERP9ERET/RERE/yYmJv8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8aGhr//f39/+bm5v8BAQH/t7e3//////9RUVH/AAAA/wAAAP8pKSn//////+Hh4f8BAQH/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP+9vb3//////zc3N//39/f/7u7u/wcHB/8AAAD/AAAA/wAAAP/a2tr//////09PT/8AAAD/AAAA/wAAAP8CAgL/zc3N//////9SUlL/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/2VlZf//////uLi4//////+ampr/AAAA/wAAAP8AAAD/AAAA/1paWv//////8vLy/2RkZP8aGhr/Li4u/6enp///////3t7e/wgICP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/EhIS//n5+f///////////z4+Pv8AAAD/AAAA/wAAAP8AAAD/AAAA/319ff/+/v7//////////////////////+Li4v8sLCz/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/dXV1/5mZmf+RkZH/AgIC/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/y0tLf+Kior/r6+v/6SkpP9qamr/DAwM/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA////////////AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD///////////8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP///////////wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP///////////////////////////wAAAP8AAAD/AAAA/wAAAP//////////////////////AAAA/wAAAP8AAAD/AAAA//////////////////////8AAAD/AAAA/wAAAP8AAAD//////////////////////wAAAP8AAAD/AAAA/wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
}
# END generated tray icons
function New-TrayIcon([string]$Data) {
    $stream = New-Object System.IO.MemoryStream (, [Convert]::FromBase64String($Data))
    New-Object System.Drawing.Icon $stream, 16, 16
}
$Icons = @{
    live    = New-TrayIcon $TrayIconData.live    # a lane is evaluating or generating
    ok      = New-TrayIcon $TrayIconData.ok      # lanes up and idle
    warn    = New-TrayIcon $TrayIconData.warn    # a lane down, identity mismatch, or memory tight
    muted   = New-TrayIcon $TrayIconData.muted   # observer starting or feed stale
}

# ---------------------------------------------------------------- status
function Test-FiniteNumber($Value) {
    $numeric = $Value -is [byte] -or $Value -is [sbyte] -or $Value -is [int16] -or $Value -is [uint16] -or
        $Value -is [int32] -or $Value -is [uint32] -or $Value -is [int64] -or $Value -is [uint64] -or
        $Value -is [single] -or $Value -is [double] -or $Value -is [decimal]
    return $numeric -and -not [double]::IsNaN([double]$Value) -and -not [double]::IsInfinity([double]$Value)
}

function Get-StatusSummary {
    $url = Get-DashboardUrl
    if (-not $url) { return @{ tone = 'muted'; text = ($script:LastError, 'Starting observer...' | Where-Object { $_ } | Select-Object -First 1) } }
    try {
        $snap = Invoke-RestMethod -Uri "$($url)api/snapshot" -TimeoutSec 1 -UseBasicParsing
        $nowUnix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
        return Get-SnapshotSummary $snap $nowUnix
    } catch {
        return @{ tone = 'muted'; text = 'Waiting for the first sample' }
    }
}

function Get-SnapshotSummary($Snapshot, [double]$NowUnix) {
    $snap = $Snapshot
    if (($snap -isnot [PSCustomObject] -and $snap -isnot [Collections.IDictionary]) -or
        -not (Test-FiniteNumber $snap.schemaVersion) -or $snap.schemaVersion -ne 1 -or
        $snap.host -isnot [string] -or $snap.host -cne 'windows') {
        return @{ tone = 'muted'; text = 'Snapshot unknown - see Open log folder' }
    }
    # A sampler that stopped publishing leaves its last snapshot behind; never show that as live.
    if (-not (Test-FiniteNumber $NowUnix) -or -not (Test-FiniteNumber $snap.sampledAt) -or
        $snap.sampledAt -lt 0 -or $snap.sampledAt -gt $NowUnix -or ($NowUnix - [double]$snap.sampledAt) -gt 5) {
        return @{ tone = 'muted'; text = 'Feed stale - see Open log folder' }
    }
    $lanes = @()
    $tooltipLanes = @()
    $idle = 0
    $warn = $false
    $busy = $false
    $unknown = $false
    $elapsed = $NowUnix - [double]$snap.sampledAt
    $laneStates = $null
    if ($snap.windowsWorker -is [PSCustomObject] -or $snap.windowsWorker -is [Collections.IDictionary]) {
        if ($snap.windowsWorker.lanes -is [PSCustomObject] -or $snap.windowsWorker.lanes -is [Collections.IDictionary]) {
            $laneStates = $snap.windowsWorker.lanes
        }
    }
    foreach ($id in 'fast', 'deep') {
        $lane = $laneStates.$id
        if ($lane -isnot [PSCustomObject] -and $lane -isnot [Collections.IDictionary]) { $unknown = $true; $lanes += "$id ?"; $tooltipLanes += "$id ?"; continue }
        # A lane answer counts as current only while its own age plus the snapshot's age is within 3 s.
        $status = if ($lane.status -is [string]) { $lane.status } else { 'unknown' }
        if (-not (Test-FiniteNumber $lane.ageSeconds) -or $lane.ageSeconds -lt 0) { $status = 'unknown' }
        elseif (([double]$lane.ageSeconds + $elapsed) -gt 3) { $status = 'stale' }
        if ($status -cin 'idle', 'busy') {
            $slotsKnown = (Test-FiniteNumber $lane.slotsBusy) -and (Test-FiniteNumber $lane.slotsTotal) -and
                $lane.slotsBusy -ge 0 -and $lane.slotsTotal -gt 0 -and $lane.slotsTotal -le 32 -and
                $lane.slotsBusy -le $lane.slotsTotal -and
                [Math]::Floor([double]$lane.slotsBusy) -eq $lane.slotsBusy -and
                [Math]::Floor([double]$lane.slotsTotal) -eq $lane.slotsTotal
            if (-not $slotsKnown -or ($status -eq 'idle' -and $lane.slotsBusy -ne 0) -or
                ($status -eq 'busy' -and $lane.slotsBusy -le 0)) { $status = 'unknown' }
        }
        switch -CaseSensitive ($status) {
            'busy'              { $cpu = if ($id -eq 'deep' -and (Test-FiniteNumber $lane.cpuPercent) -and $lane.cpuPercent -ge 0 -and $lane.cpuPercent -le 100) { " cpu $([int]$lane.cpuPercent)%" } else { '' }; $lanes += "$id $([int]$lane.slotsBusy)/$([int]$lane.slotsTotal)$cpu"; $busy = $true }
            'idle'              { $lanes += "$id idle"; $idle++ }
            'identity_mismatch' { $lanes += "$id WRONG MODEL"; $warn = $true }
            'loading'           { $lanes += "$id loading"; $warn = $true }
            'slots_unknown'     { $lanes += "$id up ?"; $unknown = $true }
            'stale'             { $lanes += "$id stale"; $unknown = $true }
            'unreachable'       { $lanes += "$id no answer"; $warn = $true }
            default             { $lanes += "$id ?"; $unknown = $true }
        }
        # Keep the lane state in the tooltip; the full menu also carries deep CPU.
        $tooltipLanes += if ($status -ceq 'busy') { "$id $([int]$lane.slotsBusy)/$([int]$lane.slotsTotal)" } else { $lanes[-1] }
    }
    # Green only for fresh, confirmed idle on both lanes; activity that cannot be confirmed is grey.
    $tone = if ($busy) { 'live' } elseif ($warn) { 'warn' } elseif ($unknown -or $idle -lt 2) { 'muted' } else { 'ok' }
    $memoryState = if ($snap.memory -is [PSCustomObject] -or $snap.memory -is [Collections.IDictionary]) { $snap.memory } else { $null }
    $memoryKnown = $memoryState.level -is [string] -and $memoryState.level -cin 'ok', 'tight', 'critical' -and
        (Test-FiniteNumber $memoryState.availablePercent) -and $memoryState.availablePercent -ge 0 -and $memoryState.availablePercent -le 100
    $memory = if ($memoryKnown) { "RAM $([int]$memoryState.availablePercent)% free" } else { $null }
    if ($memoryKnown -and $memoryState.level -cin 'tight', 'critical' -and $tone -ne 'live') { $tone = 'warn' }
    # Every adapter with a live load reading (NVIDIA from nvidia-smi, the AMD card from Windows counters).
    $loads = @()
    if ($snap.pcGpus -is [array]) {
        foreach ($adapter in $snap.pcGpus) {
            if (($adapter -is [PSCustomObject] -or $adapter -is [Collections.IDictionary]) -and
                (Test-FiniteNumber $adapter.utilizationPercent) -and $adapter.utilizationPercent -ge 0 -and $adapter.utilizationPercent -le 100) {
                $loads += [int]$adapter.utilizationPercent
            }
        }
    }
    $gpuText = if ($loads.Count) { "GPU " + ($loads -join '/') + '%' } else { $null }
    $macPeer = if ($snap.macPeer -is [PSCustomObject] -or $snap.macPeer -is [Collections.IDictionary]) { $snap.macPeer } else { $null }
    $macLimit = if ($macPeer.state -ceq 'linked') { 30 } else { 45 }
    $macFresh = (Test-FiniteNumber $macPeer.ageSeconds) -and $macPeer.ageSeconds -ge 0 -and
        ([double]$macPeer.ageSeconds + $elapsed) -le $macLimit
    $mac = if (-not $macFresh -or $macPeer.state -isnot [string]) { $null } else {
        switch -CaseSensitive ($macPeer.state) {
            'reachable' { 'Mac answers' } 'linked' { 'Mac reported' } 'unreachable' { 'Mac no answer' } default { $null }
        }
    }
    $publishRows = @()
    if ($snap.sources -is [array]) {
        foreach ($source in $snap.sources) {
            if (($source -is [PSCustomObject] -or $source -is [Collections.IDictionary]) -and
                $source.id -is [string] -and $source.id -ceq 'share-publish') { $publishRows += $source }
        }
    }
    # A single explicit outcome proves failure; duplicate or malformed reports stay unknown.
    $publishFailed = $publishRows.Count -eq 1 -and $publishRows[0].outcome -is [string] -and $publishRows[0].outcome -ceq 'failed'
    $publishText = if ($publishFailed) { 'Share publish failed' } else { $null }
    $publishTooltip = if ($publishFailed) { 'Share failed' } else { $null }
    if ($publishFailed -and $tone -ne 'live') { $tone = 'warn' }
    @{ tone = $tone; text = (@($lanes -join ' | ') + @($gpuText, $memory, $mac, $publishText) | Where-Object { $_ }) -join ' | ';
        tooltipParts = @($tooltipLanes) + @(@($mac, $publishTooltip, $memory, $gpuText) | Where-Object { $_ }); snapshot = $snap }
}

function Format-TrayText([string]$Message, [string[]]$Parts = @()) {
    if ($Parts.Count) {
        $text = 'AGIW'
        foreach ($part in $Parts) {
            $candidate = "$text | $part"
            # Keep complete lane and Mac states; lower-priority readings stay in the menu.
            if ($candidate.Length -gt 63) { break }
            $text = $candidate
        }
        return $text
    }
    $text = "AGIW | $Message"
    # NotifyIcon.Text is capped at 63 characters on .NET Framework.
    return $(if ($text.Length -gt 63) { $text.Substring(0, 60) + '...' } else { $text })
}

# ---------------------------------------------------------------- tray UI
$Tray = New-Object System.Windows.Forms.NotifyIcon
$Tray.Icon = $Icons.muted
$Tray.Text = 'AGIW Monitor | starting'
$Tray.Visible = $true

$Menu = New-Object System.Windows.Forms.ContextMenuStrip
$openItem = $Menu.Items.Add('Open dashboard')
$openItem.Font = New-Object System.Drawing.Font($openItem.Font, [System.Drawing.FontStyle]::Bold)
$copyItem = $Menu.Items.Add('Copy dashboard URL')
[void]$Menu.Items.Add('-')
$statusItem = $Menu.Items.Add('Starting...')
$statusItem.Enabled = $false
[void]$Menu.Items.Add('-')
$restartItem = $Menu.Items.Add('Restart observer')
$startupItem = New-Object System.Windows.Forms.ToolStripMenuItem 'Start at sign-in'
$startupItem.CheckOnClick = $false
$startupItem.Checked = Test-Path -LiteralPath $StartupLink
[void]$Menu.Items.Add($startupItem)
$logsItem = $Menu.Items.Add('Open log folder')
[void]$Menu.Items.Add('-')
$quitItem = $Menu.Items.Add('Quit')
$Tray.ContextMenuStrip = $Menu

$openItem.add_Click({ try { Open-Dashboard } catch { Write-Log "open failed: $($_.Exception.Message)" } })
$Tray.add_DoubleClick({ try { Open-Dashboard } catch { Write-Log "open failed: $($_.Exception.Message)" } })
$copyItem.add_Click({ try { $url = Get-DashboardUrl; if ($url) { [System.Windows.Forms.Clipboard]::SetText($url + 'monitor') } } catch { Write-Log "copy failed: $($_.Exception.Message)" } })
$restartItem.add_Click({ try { Write-Log 'restart requested'; Stop-Observer; Start-Observer } catch { Write-Log "restart failed: $($_.Exception.Message)" } })
$logsItem.add_Click({ try { Start-Process explorer.exe $StateDir } catch { } })
function Test-OwnShortcut {
    # Only a shortcut that starts this install's launcher is ours to replace or remove.
    if (-not (Test-Path -LiteralPath $StartupLink)) { return $false }
    try {
        $existing = (New-Object -ComObject WScript.Shell).CreateShortcut($StartupLink)
        # Literal, case-insensitive comparison: wildcard matching would misread [ ] in an install path.
        $wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
        return ($existing.TargetPath -ieq $wscript) -and
            $existing.Arguments.ToLowerInvariant().Contains(('"' + $Launcher + '"').ToLowerInvariant())
    } catch { return $false }
}
$startupItem.Checked = Test-OwnShortcut
$startupItem.add_Click({
    try {
        if (Test-Path -LiteralPath $StartupLink) {
            if (-not (Test-OwnShortcut)) { Write-Log 'startup shortcut belongs to another install; left unchanged'; return }
            Remove-Item -LiteralPath $StartupLink -Force
        } else {
            $shell = New-Object -ComObject WScript.Shell
            $link = $shell.CreateShortcut($StartupLink)
            $link.TargetPath = Join-Path $env:SystemRoot 'System32\wscript.exe'
            $link.Arguments = '"{0}" /quiet' -f $Launcher
            $link.WorkingDirectory = $Root
            $link.IconLocation = '{0},0' -f (Join-Path $Root 'brand\windows\agiw.ico')
            $link.Description = 'AGIW Inference Monitor (Windows)'
            $link.Save()
        }
    } catch { Write-Log "startup toggle failed: $($_.Exception.Message)" }
    $startupItem.Checked = Test-OwnShortcut
})
$quitItem.add_Click({
    Write-Log 'quit'
    $Timer.Stop()
    Stop-Observer
    $Tray.Visible = $false
    $Tray.Dispose()
    [System.Windows.Forms.Application]::Exit()
})

$Timer = New-Object System.Windows.Forms.Timer
$Timer.Interval = 2000
$Timer.add_Tick({
    try {
        if ($script:Observer -and $script:Observer.HasExited) {
            $code = $script:Observer.ExitCode
            $script:Observer = $null
            $script:ObserverPort = $null
            if ($code -eq 3) {
                # The observer's source changed (an update was installed): start the new code right away.
                Write-Log 'observer updated; starting new code'
                $script:NextStart = [DateTime]::MinValue
                $script:Backoff = 2
            } else {
                Write-Log "observer exited with code $code; restarting in $($script:Backoff) s"
                $script:NextStart = (Get-Date).AddSeconds($script:Backoff)
                $script:Backoff = [Math]::Min(30, $script:Backoff * 2)
            }
        }
        if (-not $script:Observer -and (Get-Date) -ge $script:NextStart) { Start-Observer }
        $summary = Get-StatusSummary
        $Tray.Icon = $Icons[$summary.tone]
        $Tray.Text = Format-TrayText $summary.text $summary.tooltipParts
        $statusItem.Text = $summary.text
    } catch { Write-Log "tick failed: $($_.Exception.Message)" }
})

Write-Log "tray starting (PowerShell $($PSVersionTable.PSVersion); python $Python)"
Start-Observer
$Timer.Start()
if ($OpenDashboard) { Open-Dashboard }
try {
    [System.Windows.Forms.Application]::Run()
} finally {
    Stop-Observer
    $Tray.Dispose()
    $Mutex.ReleaseMutex()
    $Mutex.Dispose()
}
