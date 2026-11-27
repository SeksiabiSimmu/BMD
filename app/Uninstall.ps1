# Brave Music Downloader 2.5.0 uninstaller.
#
# Removes the program's own files: the app folder, its private tools, the temporary cache, the
# native-host registration (only when it still points at this app) and the Start menu entries.
# KEEPS your music, your settings and the download history; nothing outside the program's own
# folder is touched, except when -RemoveHistory is given.
#
#   (no option)        removes the program and shows what it does
#   -Silent            no question and no pause; the exit code is the result ("Programs and Features")
#   -DryRun            show what would be removed; change nothing
#   -RemoveHistory     also delete the download history database (you are asked to type DELETE
#                      HISTORY; with -Silent the switch itself is the confirmation)
#   -Diagnose          write a diagnosis report instead of removing anything
#   -AppRoot <folder>  the app folder to remove (default: the folder this file is in)
#
# The work is done by installer\installer_core.py, the same engine setup uses, so an uninstall is
# as careful as an install: it removes what it recorded as its own and leaves everything else.
#
# Exit codes are those of the setup program: 0 removed, 1 failed, 2 cancelled, 6 setup is already
# running, 9 nothing is installed to remove.
#
# Dot-sourcing this file defines the functions without running anything (tests/ps/ does that).
param(
    [switch]$Silent,
    [switch]$DryRun,
    [switch]$RemoveHistory,
    [switch]$Diagnose,
    [string]$AppRoot = ''
)

$script:ConfirmPhrase = 'DELETE HISTORY'
$script:ExitCodes = @{ 'ok' = 0; 'failed' = 1; 'cancelled' = 2; 'damaged' = 3; 'download' = 4; 'downgrade' = 5
                       'busy' = 6; 'brave-missing' = 7; 'unsupported' = 8; 'not-installed' = 9 }
$script:ExitText = @{
    0 = 'Brave Music Downloader was removed.'
    1 = 'The uninstall did not complete. What could not be removed is still there; run it again after restarting the computer.'
    2 = 'Cancelled. Nothing was changed.'
    6 = 'Setup or another uninstall is already running. Wait for it to finish, then try again.'
    9 = 'Brave Music Downloader is not installed, so there is nothing to remove.'
}
$script:EngineFiles = @(@('installer', 'installer_core.py'), @('installer', 'payload_container.py'), @('helper', 'bmdfs.py'))

function Join-Parts([string]$Base, [string[]]$Parts) {
    $path = $Base
    foreach ($part in $Parts) { $path = [System.IO.Path]::Combine($path, $part) }
    return $path
}

function Get-UninstallExitCode($Result) {
    if ($null -eq $Result) { return 1 }
    if ($Result.ok) { return 0 }
    $category = [string]$Result.category
    if ($category -and $script:ExitCodes.ContainsKey($category)) { return [int]$script:ExitCodes[$category] }
    return 1
}

function Get-UninstallExitText([int]$Code) {
    if ($script:ExitText.ContainsKey($Code)) { return [string]$script:ExitText[$Code] }
    return [string]$script:ExitText[1]
}

function Read-UninstallJson([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Quote([string]$Text) {
    if ($Text -match '"') { throw 'A path contains a quote and cannot be passed on.' }
    return '"' + $Text + '"'
}

function ConvertTo-EngineCommandLine([string[]]$Arguments) {
    return ((@($Arguments | ForEach-Object { Quote ([string]$_) })) -join ' ')
}

function Test-UninstallPrerequisites([string]$App) {
    $missing = @()
    foreach ($relative in (@(,@('python', 'python.exe')) + $script:EngineFiles)) {
        if (-not (Test-Path -LiteralPath (Join-Parts $App $relative) -PathType Leaf)) { $missing += ($relative -join '\') }
    }
    return $missing
}

# The engine runs from a private copy in %TEMP%: the app's own Python and files are deleted while
# it runs, and a running python.exe cannot be deleted on Windows.
function New-WorkCopy([string]$App, [string]$Work) {
    New-Item -ItemType Directory -Path (Join-Path $Work 'installer'), (Join-Path $Work 'helper') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $App 'python') -Destination (Join-Path $Work 'python') -Recurse
    foreach ($relative in $script:EngineFiles) {
        Copy-Item -LiteralPath (Join-Parts $App $relative) -Destination (Join-Parts $Work $relative)
    }
}

function Get-UninstallArguments([string]$Root, [string]$Work, [bool]$History, [bool]$Preview, [bool]$Report) {
    $result = Join-Path $Work 'result.json'
    if ($Report) {
        $text = Join-Path $Root 'diagnosis.txt'
        return @('diagnose', '--root', $Root, '--json-report', (Join-Path $Root 'diagnosis.json'),
                 '--log-file', $text, '--result-file', $result)
    }
    $arguments = @('uninstall', '--root', $Root, '--log-file', (Join-Path $Work 'engine.log'), '--result-file', $result)
    if ($Preview) { $arguments += '--dry-run' }
    if ($History) { $arguments += @('--remove-history', '--confirm', $script:ConfirmPhrase) }
    return $arguments
}

# A console program started without CREATE_NO_WINDOW gets a console window of its own when the
# starter has none (silent uninstall from "Programs and Features"). With the flag it writes into
# the starter's console when there is one, and nowhere when there is not.
function Start-EngineProcess([string]$Python, [string]$Core, [string[]]$Arguments, [string]$Folder) {
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $info.Arguments = ConvertTo-EngineCommandLine (@($Core) + $Arguments)
    $info.WorkingDirectory = $Folder
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    return $process
}

# The helper (pythonw.exe under the app folder) holds its own files open. The browser restarts it
# whenever it is missing, so stopping it here is harmless.
function Stop-AppProcesses([string]$App, $Processes = @(Get-Process -ErrorAction SilentlyContinue)) {
    $prefix = $App.TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    foreach ($process in @($Processes)) {
        try {
            if ($process.Path -and $process.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                $process.Kill()
            }
        } catch { }
    }
}

# Only what is certainly this program's: its own script (still open while it runs) and the folders
# that have become empty. Nothing is removed recursively here; the engine has done that, file by
# file, for everything it recorded as its own.
function Remove-AppLeftovers([string]$App, [string]$Root) {
    $removed = @()
    try {
        $script = Join-Path $App 'Uninstall.ps1'
        if (Test-Path -LiteralPath $script -PathType Leaf) { Remove-Item -LiteralPath $script -Force -ErrorAction Stop }
    } catch { }
    foreach ($folder in @($App, $Root)) {
        if ($folder -and (Test-Path -LiteralPath $folder -PathType Container)) {
            try { [System.IO.Directory]::Delete($folder, $false); $removed += $folder } catch { }
        }
    }
    return $removed
}

function Invoke-UninstallMain {
    $ErrorActionPreference = 'Stop'
    $app = $AppRoot
    if (-not $app) { $app = $PSScriptRoot }
    # A relative -AppRoot counts from where the user stands. Windows PowerShell 5.1 keeps the
    # .NET current directory apart from PowerShell's, so GetFullPath alone would look elsewhere.
    if (-not [System.IO.Path]::IsPathRooted($app)) { $app = Join-Path (Get-Location).ProviderPath $app }
    $app = [System.IO.Path]::GetFullPath($app)
    $root = Split-Path -Parent $app
    $work = Join-Path $env:TEMP ('bmd-uninstall-' + [guid]::NewGuid().ToString('N'))
    $log = Join-Path $root 'Uninstall.log'
    $code = 1
    try {
        if ($env:TEMP) { Set-Location -LiteralPath $env:TEMP }    # a folder we are standing in cannot be deleted
        $missing = Test-UninstallPrerequisites $app
        if ($missing.Count) {
            $text = 'This app folder is incomplete, so the uninstaller cannot run safely (' + ($missing -join ', ') + ').'
            if (-not $Silent) {
                Write-Host $text -ForegroundColor Red
                Write-Host 'Run the Brave Music Downloader setup again (it repairs the installation), then uninstall.'
            }
            exit 1
        }
        if ($RemoveHistory -and -not $Silent -and -not $DryRun) {
            $answer = Read-Host ('This also deletes your download history. Your music is NOT deleted. Type ' + $script:ConfirmPhrase + ' to continue')
            if ($answer -cne $script:ConfirmPhrase) {
                Write-Host 'Cancelled. Nothing was changed.'
                exit 2
            }
        }

        New-WorkCopy -App $app -Work $work
        if (-not $Diagnose) { Stop-AppProcesses -App $app }
        $arguments = Get-UninstallArguments -Root $root -Work $work -History ([bool]$RemoveHistory) `
                                             -Preview ([bool]$DryRun) -Report ([bool]$Diagnose)
        $process = Start-EngineProcess -Python (Join-Parts $work @('python', 'python.exe')) `
                                       -Core (Join-Parts $work @('installer', 'installer_core.py')) `
                                       -Arguments $arguments -Folder $work
        $process.WaitForExit()
        $engineExit = $process.ExitCode
        $process.Close()
        $result = Read-UninstallJson (Join-Path $work 'result.json')
        $code = Get-UninstallExitCode $result
        if ($null -eq $result -and $engineExit -ne 0) {
            $code = 1
            if (-not $Silent) { Write-Host ("The uninstall engine ended unexpectedly (exit code $engineExit).") -ForegroundColor Red }
        }

        if ($Diagnose) {
            $report = Join-Path $root 'diagnosis.txt'
            if (-not $Silent) {
                Write-Host ''
                Write-Host "The diagnosis report was written to $report"
                if ($code -ne 0) { Write-Host 'Some checks failed; the FAIL lines in the report say what.' -ForegroundColor Yellow }
                try { Start-Process -FilePath 'notepad.exe' -ArgumentList (Quote $report) } catch { }
            }
        } elseif ($DryRun) {
            if (-not $Silent) { Write-Host "`nNothing was changed (dry run). To remove the program, run this again without -DryRun." }
        } elseif ($code -eq 0) {
            Remove-AppLeftovers -App $app -Root $root | Out-Null
            if (-not $Silent) {
                Write-Host ''
                Write-Host (Get-UninstallExitText 0) -ForegroundColor Green
                if ($RemoveHistory) { Write-Host 'The download history was removed, as requested.' }
                foreach ($line in @($result.result.kept)) { Write-Host ('Kept: ' + $line) }
                Write-Host 'Now remove the extension in Brave as well: open brave://extensions and click Remove.'
            }
        } else {
            # Keep the engine's log: the work folder is deleted below.
            try {
                $engineLog = Join-Path $work 'engine.log'
                if (Test-Path -LiteralPath $engineLog -PathType Leaf) {
                    New-Item -ItemType Directory -Force -Path $root | Out-Null
                    Copy-Item -LiteralPath $engineLog -Destination $log -Force
                }
            } catch { }
            if (-not $Silent) {
                Write-Host ''
                Write-Host (Get-UninstallExitText $code) -ForegroundColor Red
                if ($result -and $result.message) { Write-Host ('Details: ' + $result.message) }
                if (Test-Path -LiteralPath $log) { Write-Host "Log: $log" }
            }
        }
    } catch {
        $code = 1
        if (-not $Silent) { Write-Host ('The uninstall did not complete: ' + $_.Exception.Message) -ForegroundColor Red }
    } finally {
        if (Test-Path -LiteralPath $work) { try { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction Stop } catch { } }
    }
    if (-not $Silent -and -not ($Diagnose -and $code -eq 0)) {   # the report is open in Notepad: nothing to wait for
        [void](Read-Host 'Press Enter to exit')
    }
    exit $code
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-UninstallMain }
