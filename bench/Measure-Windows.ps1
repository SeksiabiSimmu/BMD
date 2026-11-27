# Brave Music Downloader - Windows baseline measurements (UNTESTED: written without a Windows machine).
# Read-only except for two small temp files. Run in Windows PowerShell 5.1 AFTER a successful setup:
#   powershell -ExecutionPolicy Bypass -File Measure-Windows.ps1
# Output: a table on screen and bmd-windows-baseline.json next to this script. Ctrl+C if it ever hangs.
param([string]$Root = (Join-Path $env:LOCALAPPDATA 'BraveMusicDownloader'), [int]$Rounds = 200)
$ErrorActionPreference = 'Stop'
$result = [ordered]@{}

function Get-Pct($values, [double]$q) {
    $s = @($values | Sort-Object)
    if ($s.Count -eq 0) { return $null }
    return [math]::Round($s[[math]::Min($s.Count - 1, [int][math]::Round($q * ($s.Count - 1)))], 3)
}
function Get-Summary($values) {
    [ordered]@{ n = @($values).Count; p50 = (Get-Pct $values 0.5); p95 = (Get-Pct $values 0.95); max = (Get-Pct $values 1.0) }
}
function Get-FolderMB($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $null }
    $sum = (Get-ChildItem -LiteralPath $path -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    return [math]::Round($sum / 1MB, 1)
}
function Test-Fsync($folder) {
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    $file = Join-Path $folder ('bmd-fsync-' + [guid]::NewGuid().ToString('N') + '.tmp')
    $samples = @()
    $stream = New-Object System.IO.FileStream($file, 'Create', 'Write', 'None', 4096)
    try {
        $block = New-Object byte[] 4096
        for ($i = 0; $i -lt 60; $i++) {
            $stream.Write($block, 0, 4096)
            $sw = [Diagnostics.Stopwatch]::StartNew()
            $stream.Flush($true)
            $samples += $sw.Elapsed.TotalMilliseconds
        }
    } finally { $stream.Dispose(); Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
    return (Get-Summary $samples)
}
function Send-Frame($stream, $object) {
    $json = [System.Text.Encoding]::UTF8.GetBytes(($object | ConvertTo-Json -Compress))
    $length = [BitConverter]::GetBytes([int]$json.Length)
    $stream.Write($length, 0, 4); $stream.Write($json, 0, $json.Length); $stream.Flush()
}
function Read-Exact($stream, [int]$count) {
    $buffer = New-Object byte[] $count
    $offset = 0
    while ($offset -lt $count) {
        $task = $stream.ReadAsync($buffer, $offset, $count - $offset)
        if (-not $task.Wait(20000)) { throw 'The helper did not answer within 20 seconds.' }
        if ($task.Result -le 0) { throw 'The helper closed its output.' }
        $offset += $task.Result
    }
    return , $buffer
}
function Read-Frame($stream) {
    $size = [BitConverter]::ToInt32((Read-Exact $stream 4), 0)
    [void](Read-Exact $stream $size)
    return $size
}
function Start-Host($exe) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo $exe
    $psi.UseShellExecute = $false; $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true; $psi.CreateNoWindow = $true
    return [System.Diagnostics.Process]::Start($psi)
}
function Invoke-Request($p, $method, $params) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Send-Frame $p.StandardInput.BaseStream @{ id = 1; method = $method; params = $params }
    $bytes = Read-Frame $p.StandardOutput.BaseStream
    return @{ ms = $sw.Elapsed.TotalMilliseconds; bytes = $bytes }
}

$launcher = Join-Path $Root 'app\bridge\BraveMusicNativeHost.exe'
if (-not (Test-Path -LiteralPath $launcher)) { throw "Not installed: $launcher" }

$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$os = Get-CimInstance Win32_OperatingSystem
$result.environment = [ordered]@{ os = $os.Caption + ' ' + $os.BuildNumber; cpu = $cpu.Name; cores = $cpu.NumberOfLogicalProcessors;
    ram_mb = [math]::Round($os.TotalVisibleMemorySize / 1024) }

Write-Host 'fsync latency (4 KB write + flush to disk)...'
$result.fsync_ms = [ordered]@{ app_drive = (Test-Fsync $Root); appdata_drive = (Test-Fsync (Join-Path $env:APPDATA 'BraveMusicDownloader'));
    music_drive = (Test-Fsync (Join-Path $env:USERPROFILE 'Music')) }

Write-Host 'Install footprint...'
$result.footprint_mb = [ordered]@{ app = (Get-FolderMB (Join-Path $Root 'app')); python = (Get-FolderMB (Join-Path $Root 'app\python'));
    tools = (Get-FolderMB (Join-Path $Root 'app\tools')); cache = (Get-FolderMB (Join-Path $Root 'cache')); logs = (Get-FolderMB (Join-Path $Root 'logs')) }
$result.tool_files_mb = @{}
Get-ChildItem -LiteralPath (Join-Path $Root 'app\tools') -File -ErrorAction SilentlyContinue | ForEach-Object { $result.tool_files_mb[$_.Name] = [math]::Round($_.Length / 1MB, 1) }

Write-Host 'Download sizes from the lock file (HEAD requests)...'
$lock = Get-Content -LiteralPath (Join-Path $Root 'app\tools.lock.json') -Raw | ConvertFrom-Json
$sizes = [ordered]@{}
foreach ($c in $lock.components) {
    try {
        $head = Invoke-WebRequest -Uri $c.url -Method Head -UseBasicParsing -MaximumRedirection 10 -TimeoutSec 60
        $sizes[$c.name] = [math]::Round([double]$head.Headers['Content-Length'] / 1MB, 1)
    } catch { $sizes[$c.name] = 'unknown: ' + $_.Exception.Message }
}
$result.download_mb = $sizes

Write-Host 'Native host through the real launcher: first-run, warm, round trips...'
$starts = @()
for ($i = 0; $i -lt 8; $i++) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $p = Start-Host $launcher
    [void](Invoke-Request $p 'health' @{})
    $starts += $sw.Elapsed.TotalMilliseconds
    $p.StandardInput.Close(); [void]$p.WaitForExit(10000)
}
$result.launcher_spawn_to_first_reply_ms = [ordered]@{ first = [math]::Round($starts[0], 1); rest = (Get-Summary $starts[1..7]) }
$p = Start-Host $launcher
[void](Invoke-Request $p 'health' @{})
$health = 1..$Rounds | ForEach-Object { (Invoke-Request $p 'health' @{}).ms }
$jobs = 1..$Rounds | ForEach-Object { (Invoke-Request $p 'jobs' @{ limit = 50 }).ms }
$result.round_trip_ms = [ordered]@{ health = (Get-Summary $health); jobs50 = (Get-Summary $jobs) }
$children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.Id)"
$result.idle_helper_working_set_mb = @($children | ForEach-Object { [math]::Round((Get-Process -Id $_.ProcessId).WorkingSet64 / 1MB, 1) })
$result.idle_launcher_working_set_mb = [math]::Round((Get-Process -Id $p.Id).WorkingSet64 / 1MB, 1)

Write-Host 'Reconnect: new helper waiting while the old one is killed...'
$reconnect = @()
for ($i = 0; $i -lt 6; $i++) {
    $b = Start-Host $launcher
    Start-Sleep -Milliseconds 800
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & taskkill.exe /PID $p.Id /T /F | Out-Null
    [void](Invoke-Request $b 'health' @{})
    $reconnect += $sw.Elapsed.TotalMilliseconds
    $p = $b
}
$result.reconnect_until_new_helper_answers_ms = (Get-Summary $reconnect)
$p.StandardInput.Close(); [void]$p.WaitForExit(10000)

Write-Host 'taskkill process-tree latency (what a Cancel costs on Windows)...'
$kill = @()
for ($i = 0; $i -lt 8; $i++) {
    $c = Start-Process -FilePath cmd.exe -ArgumentList '/c ping -n 60 127.0.0.1 >nul' -PassThru -WindowStyle Hidden
    Start-Sleep -Milliseconds 300
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & taskkill.exe /PID $c.Id /T /F | Out-Null
    [void]$c.WaitForExit(5000)
    $kill += $sw.Elapsed.TotalMilliseconds
}
$result.taskkill_tree_kill_ms = (Get-Summary $kill)

$path = Join-Path $PSScriptRoot 'bmd-windows-baseline.json'
$result | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding UTF8
$result | ConvertTo-Json -Depth 6
Write-Host "`nSaved $path - please send it back."
