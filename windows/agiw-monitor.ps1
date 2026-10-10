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
            Start-Process ("http://127.0.0.1:{0}/" -f $savedPort)
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
}

function Get-DashboardUrl { if ($script:ObserverPort) { "http://127.0.0.1:$($script:ObserverPort)/" } else { $null } }

function Open-Dashboard {
    $url = Get-DashboardUrl
    if (-not $url) { [System.Windows.Forms.MessageBox]::Show('The observer is not running yet.', 'AGIW Monitor') | Out-Null; return }
    $edge = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
        (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    # An Edge app window feels like the Mac edition's dashboard window; any browser works.
    if ($edge) { Start-Process -FilePath $edge -ArgumentList "--app=$url" } else { Start-Process $url }
}

# ---------------------------------------------------------------- icons
function New-DotIcon([string]$Hex) {
    $bitmap = New-Object System.Drawing.Bitmap 16, 16
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = 'AntiAlias'
    $graphics.Clear([System.Drawing.Color]::Transparent)
    $ring = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#d7d7d7')), 1.5
    $fill = New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml($Hex))
    $graphics.FillEllipse($fill, 2, 2, 12, 12)
    $graphics.DrawEllipse($ring, 2, 2, 12, 12)
    $graphics.Dispose()
    [System.Drawing.Icon]::FromHandle($bitmap.GetHicon())
}
$Icons = @{
    live    = New-DotIcon '#4c9cff'   # a lane is evaluating or generating
    ok      = New-DotIcon '#36c48a'   # lanes up and idle
    warn    = New-DotIcon '#e9b85c'   # a lane down, identity mismatch, or memory tight
    muted   = New-DotIcon '#7a7a86'   # observer starting or feed stale
}

# ---------------------------------------------------------------- status
function Get-StatusSummary {
    $url = Get-DashboardUrl
    if (-not $url) { return @{ tone = 'muted'; text = ($script:LastError, 'Starting observer...' | Where-Object { $_ } | Select-Object -First 1) } }
    try {
        $snap = Invoke-RestMethod -Uri "$($url)api/snapshot" -TimeoutSec 1 -UseBasicParsing
    } catch {
        return @{ tone = 'muted'; text = 'Waiting for the first sample' }
    }
    # A sampler that stopped publishing leaves its last snapshot behind; never show that as live.
    $nowUnix = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0
    if (-not $snap.sampledAt -or ($nowUnix - [double]$snap.sampledAt) -gt 5) {
        return @{ tone = 'muted'; text = 'Feed stale - see Open log folder' }
    }
    $lanes = @()
    $idle = 0
    $warn = $false
    $busy = $false
    $unknown = $false
    $elapsed = [Math]::Max(0, $nowUnix - [double]$snap.sampledAt)
    foreach ($id in 'fast', 'deep') {
        $lane = $snap.windowsWorker.lanes.$id
        if (-not $lane) { $unknown = $true; $lanes += "$id ?"; continue }
        # A lane answer counts as current only while its own age plus the snapshot's age is within 3 s.
        $status = $lane.status
        if ($lane.ageSeconds -eq $null -or ([double]$lane.ageSeconds + $elapsed) -gt 3) { if ($status -in 'idle', 'busy', 'slots_unknown') { $status = 'stale' } }
        switch ($status) {
            'busy'              { $cpu = if ($id -eq 'deep' -and $lane.cpuPercent -ne $null) { " cpu $([int]$lane.cpuPercent)%" } else { '' }; $lanes += "$id $($lane.slotsBusy)/$($lane.slotsTotal)$cpu"; $busy = $true }
            'idle'              { $lanes += "$id idle"; $idle++ }
            'identity_mismatch' { $lanes += "$id WRONG MODEL"; $warn = $true }
            'loading'           { $lanes += "$id loading"; $warn = $true }
            'slots_unknown'     { $lanes += "$id up ?"; $unknown = $true }
            'stale'             { $lanes += "$id stale"; $unknown = $true }
            'unreachable'       { $lanes += "$id no answer"; $warn = $true }
            default             { $lanes += "$id ?"; $unknown = $true }
        }
    }
    # Green only for fresh, confirmed idle on both lanes; activity that cannot be confirmed is grey.
    $tone = if ($busy) { 'live' } elseif ($warn) { 'warn' } elseif ($unknown -or $idle -lt 2) { 'muted' } else { 'ok' }
    $memory = if ($snap.memory.level -and $snap.memory.level -ne 'unknown') { "RAM $([int]$snap.memory.availablePercent)% free" } else { $null }
    if ($snap.memory.level -in 'tight', 'critical' -and $tone -ne 'live') { $tone = 'warn' }
    # Every adapter with a live load reading (NVIDIA from nvidia-smi, the AMD card from Windows counters).
    $loads = @($snap.pcGpus | Where-Object { $_.utilizationPercent -ne $null } | ForEach-Object { [int]$_.utilizationPercent })
    $gpuText = if ($loads.Count) { "GPU " + ($loads -join '/') + '%' } else { $null }
    $macFresh = $snap.macPeer.ageSeconds -ne $null -and ([double]$snap.macPeer.ageSeconds + $elapsed) -le 45
    $mac = if (-not $macFresh) { $null } else { switch ($snap.macPeer.state) { 'reachable' { 'Mac ok' } 'linked' { 'Mac linked' } 'unreachable' { 'Mac offline' } default { $null } } }
    @{ tone = $tone; text = (@($lanes -join ' | ') + @($gpuText, $memory, $mac) | Where-Object { $_ }) -join ' | '; snapshot = $snap }
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
$copyItem.add_Click({ try { $url = Get-DashboardUrl; if ($url) { [System.Windows.Forms.Clipboard]::SetText($url) } } catch { Write-Log "copy failed: $($_.Exception.Message)" } })
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
        $text = "AGIW | $($summary.text)"
        # NotifyIcon.Text is capped at 63 characters on .NET Framework.
        $Tray.Text = if ($text.Length -gt 63) { $text.Substring(0, 60) + '...' } else { $text }
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
