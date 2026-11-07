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
    ok = 'AAABAAIAEBAAAAEAIAAhAQAAJgAAACAgAAABACAAjQIAAEcBAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIAgAAAJCRaDYAAAAGYktHRAD/AP8A/6C9p5MAAADWSURBVCiR5ZCtioZAGIUPRpM3oOAfeA0iCiaDdqcJJjV4EzYvweI9mMbgpAExTBKsNoN3MMUNH3zLLps27j7twPscDi/w3ymKYts2TdNesaoqIcQ8z2EY/iys60opLcsSQJZlx3F4nhfH8X3f75ZPbNs+zzNJEsYYgGEY2rYFYJqmZVmqqr7OlLeQ5/l1XY7jBEGg6zoAKSWArus4577vfxcIIZRSKSVjjBAyTVNd167r9n3/PM++71/2GIaxLIuiKACiKBrHEUDTNEIIznmapr944R/iAwqrTrYT+VuDAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAABmJLR0QA/wD/AP+gvaeTAAACQklEQVRIie2TvUuyURjGbx9fUDcVhBCd1CHwa8pNcJEgUBTSSHTRoKXBwbGh/gAXN6dqSQhycNBFcFIQExy0MBRU/EAh6CG/ML3e4eF9CHvJGgN/0zn3dXNdnPucQ7Rly5bfj+ALLRKJaLXa2WwWDodXq9WaajAY/H6/Tqcbj8f5fP7m5ubt7e0HyXK5fD6fAwBgtVrX1PPz89VqhQ+02229Xv+DgJOTEwCZTAZAPB7/KB0fHwNgWTYUCimVSqPReHt7C6DZbIrF4u8GZLNZABaLZTqdvry8iEQiXqrX6wCcTidfYRjm4eEBgNvt/pa7UqlcLpe9Xk8gEKTTaQAul4uTdnd3AbRaLYHgq/vj+fPfqtfrZRiGs06lUvv7+z6fL5lMEpFarSaix8dHAFyzzWYTCoXcut1u1+v1zbHFYhHAwcEBEe3s7Ly/v0+nU6lUSkR2ux1ANpvlm1mW5a86Go1udtdqtVx3pVIplUqlUol7TsFgkP6NqNPp8CNyuVyHh4fX19cALi8v19yYzwFHR0dE1O/3F4sFV2m1WkTk8/mI6OnpqdFoqFQqj8fDqclk8u7uTiaTEVGtVtt8gmq1CsDhcPAVhUKxWCyWy6VKpSKiQCAAYDwen52daTQavV4fi8UAdLtdiUSywd1kMgF4fX1de9Hch4hEItz24uJi7aMNBoO9vb3PhsK1vdlsnkwmiUQin89/rA8Gg9lsNhqNCoUCEeVyufv7e5Zlh8NhuVy+uro6PT19fn7ePJ8tW7b8Rv4CiQtOYQCCBE0AAAAASUVORK5CYII='
    live = 'AAABAAIAEBAAAAEAIAA8AQAAJgAAACAgAAABACAAmAIAAGIBAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIAgAAAJCRaDYAAAAGYktHRAD/AP8A/6C9p5MAAADxSURBVCiR3ZE/rkVgEMXHK6xAo9IqNBQkNL7mU1mDxhZ0FqEQGoWOmkIlERFbUPiTiAXQKGVuIS8391VP84p3uknOL3PODMBDMQCAiL91M8zX0w1/DKRpqqrqcRz3GMexoiiU0rZtPyD8lqZplNIkSRCxKApRFIdhqOua47h939+3ud3TNAmCUFWVaZqI6LpuEASIuCzLPM/ned7AO1Ke5zzPT9PUdd22bQDAsiwA+L5vGEbf9z87ZFlmWRbLsoSQLMts246iaBxHz/MYhpEk6aPDuq6EkOu6ELFpGsdxEDEMQ1mWdV0vy/KODf/j04/1AlRDhebds7bbAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAABmJLR0QA/wD/AP+gvaeTAAACTUlEQVRIie1VPWjqYBQ9xh/SqgVBEEsbEVHaIg4NLo4u1qGDYKFLs7WTi+BqoR11KHS0LiKBKigKHeokbu1WCbjEzQalULKpi/k6fBB86nsWXpf38CzJd8/lni/n3kuAfx0G+iCE/HxpgwEA8+N1F7AR2Aj8PUx/4PL5fL/fZ1n27u6OYRavIklSuVyWZdlqtUYiEUEQbDbbb2uRJXx+flosFsp2Op0F9vb2lu6Rjv39fUmS5nPWCBQKBQCxWAzA5eXlPCWKIgC73f7w8KAoSrfbPT8/B+D1eieTyXcFotEogJeXF5ZlHQ7HdDrVKb/fD6DRaOiR2Wx2fHwMoFarfUtAURSGYdxut6ZpJycnAOr1OqV6vR4AjuM0TVu+1rJFq5tcqVQ0TYvH4waD4fT09Pn5WRTFRCIBYDAYADg8PNR70G63Z7MZfec4LhAIrG9yOBwG8PT0RAgZDodGo5FlWVVVCSGtVgtANBrVk+12u14nnU6vt0iWZRoMhUI8z/M8T8epWCzqFu3t7ekW1ev1arUqCAKAbDa7ILBi0R4fHwG43W6z2UwjHo8HAB2eg4MDn8/3/v5erVYpm0gkzs7OVFUFcHR0tNLzX76AJjWbTT3y8fFhMpkYhhkMBoSQUqkEYHt7+/7+vt/vS5KUSqUA7O7ujsfjNRa9vb0B2NnZmZ9oQghdiFwuR4/X19cLi+ZyuV5fX9dP0Wg0urq6CgaDLMvOxzOZjMfjMRqN9Hhzc5NMJkVRlGV5a2uL5/mLiwun07nszeafvBHYCPwP+ALuKAEV0ScUDAAAAABJRU5ErkJggg=='
    warn = 'AAABAAIAEBAAAAEAIAA1AQAAJgAAACAgAAABACAArgIAAFsBAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIAgAAAJCRaDYAAAAGYktHRAD/AP8A/6C9p5MAAADqSURBVCiR3ZItqoVQFIXXNZqcgIJ/4BhEFEwG7doEkxqchM0hWJyD6Rg0CWI4SbDaDM7glHPDDb53m+WFt9uGtRZ8e+0X5xyP5pGBcy48iwf+2JCm6bqukiR91jzPKaXDMLiue4t+Qi/LQgjJsgxAFEX7vluW5fv+dV2fFM75bdB1/TiOIAimaQLQtm1VVQBUVdU0TRRFfF0pjuPzPA3DcBxHlmUAjDEAdV3P82zb9jdDkiSEEMbYNE1JkvR9XxSFaZpN03DOt237xaAoyjiOgiAA8Dyv6zoAZVlSSud5DsPwFv+Dpl9P3/sNc2Jyon+QZKsAAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIAgAAAPwY7aMAAAAGYktHRAD/AP8A/6C9p5MAAAJjSURBVEiJ7VYxSDJhGH7vDMqtgkBEJ3UQrLbcgpYIBEVBCw9bytHBwTFCZ2lpc7OGhgQHB11cHUQHQSsEhVOshgg6Ok6x82n44JDT/y/4/4af33e5+97neJ73e77v/b7jANBPBv+j7ES0xB4cx/11aubNj89gIbAQ+PNY+g2WSCTsdvtwOIzH45PJRIdubm5GIhGHwyHLcqVSubq6en9/n8MCYO5psb6+PhqNGLq7u6tDz87OJpMJpqLX67lcrjnMvxKIRqMASqUSgEwmMw2Fw2EAkiSdnp6azeatra2bmxsA3W53ZWXluwLlchmA2+1WFOX19XV5eVmD2u02AJ/Pp2V4nq/X6wACgcC3BMxms6qqj4+PHMcVi0UAfr+fQU6nE4Aoil8eX4x5/iIfHh7yPM+oC4XCwcGBIAj5fJ6IrFYrEd3f32tl7e3tGQwG9t7r9drt9hwdnUC1WgXg8XiIyGQyfXx8KIqyurpKRPv7+wDK5bL2sSRJ2lJfXFx8bZHdbmfJRqNRq9VqtRrbTicnJ5pF/X5fs8jv9weDwWw2CyCVSukE5jTa0dERET09PY3HY5YRRZGIBEEgooeHh06nY7FYQqEQQ/P5/O3t7draGhHd3d3p6WZn0Gq1AHi9Xi2zsbExHo9VVbVYLER0fHwMQJblWCxms9lcLtfl5SWAwWBgNBq/sGh7exvA29vb9I4mItYQiUSCDZPJpK7Rnp+fd3Z2ZkvX7yKTyZTJZJrN5nA4nM6n02lRFFVVZcPz8/NcLicIgsPhUBSlXq9fX1+/vLzMGs6x8hd38kLgvxbg/vm/608NP7ZyQKin2AAAAABJRU5ErkJggg=='
    muted = 'AAABAAIAEBAAAAEAIABcAQAAJgAAACAgAAABACAA1wIAAIIBAACJUE5HDQoaCgAAAA1JSERSAAAAEAAAABAIAgAAAJCRaDYAAAAGYktHRAD/AP8A/6C9p5MAAAERSURBVCiRvZKxaoNgFIVPHePiCySgZnB2DKLg5KCLOGQMOIUMeYVfiE+RxQdwFgyi/yKIg5Ogo5MR8gZZ/g6WlpRCG2h7pnuHc797uBeUUsYYIQQAIeTb+jkRQvCUjzEGxtgfEj5rt9vVdS0Iwtzu9/umaS6Xi67rXxOqqkrT1Pd9AI7jdF2nKIppmrfbbZ7ykEGW5WEYLMsqigLA+Xw+Ho8ARFGUJGmxWMwELgiC2bDdbq/X63q91jRtuVwCuN/vAE6nU1mWm81mNry879O2bRzH4zh6npdlWd/3YRi6rsvzfJIkqqpO0/SRYbVa5XnOcRwAwzCiKAJwOByapinL0rbteej/3+H3CW8Zfv7blNJXmc6sPyBBGPcAAAAASUVORK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIAgAAAPwY7aMAAAAGYktHRAD/AP8A/6C9p5MAAAKMSURBVEiJ7VYxSGpRGP7vLbC2DAQRndJB0Np0E1xECBIDLbrkYo0NDY4ROYuLm5s2OBQ4NOji2hA6BFohFFwlbQghUa5h1+8NJy6X+3z53sPL40HfdM7//+f/zv+f79x7OACkAsdxyljt+ms7r474L6Hj9lnHeP0IGHgAmnOeM4F+qRkW9UvN9Kl7BbrjK5kmEgm73T4ajY6PjyeTicbrdrv39/cdDsdwOLy+vs7n84PB4A+YV1dX39/fmcZ8Pp/Ge3JyMplMoEKr1XK5XOqYGfo8PDwEUC6XAWSzWbVrb28PQL/fPzg4sFgs6+vrhUIBwNPT09LSkpbgVzyVSgWA1+uVJKnX6xkMBsXVbDYBhEIhxcLzfK1WA7C9vf1bBBaLRZblTqfDcVypVAIQDoeZy+l0AhBFceZXkmWefg92dnZ4nmepr66ugsGgIAjFYpGIbDYbEd3f3yvb8vv9CwsLbNxqtZrNJht/7mBqBTc3NwA2NzeJyGw2f3x8SJK0srJCRIFAAEClUlGC+/2+ctTpdPrrsoiI7HY7i769va1Wq9VqlckpHo8rLWq320qLwuFwJBLJ5XIAksmkJtuUm7y7u0tE3W53PB4ziyiKRCQIAhE9PDw8Pj5ardZoNMq8xWLx4uLCaDQS0d3d3ewKGo0GgK2tLcViMpnG47Esy1arlYhisRiA4XB4dHS0trbmcrkymQyA5+fn5eVlZdV0fW5sbAB4e3tTK5qI2IVIJBJsenZ2prloLy8vHo9HveRTRYxEaajZbM5ms/V6fTQaqaNTqZQoirIss+np6enl5aUgCA6HQ5KkWq12fn7++vr6cz84DcEcwTJ//w/+Pb7fRTPxKVMAc39XM+jYIsb0A0tYsJfp9CFuAAAAAElFTkSuQmCC'
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
