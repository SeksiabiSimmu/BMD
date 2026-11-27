# Brave Music Downloader 2.5.0 - setup script.
#
# The setup program (BraveMusicDownloader-Setup.exe) unpacks this file, the application archive and,
# in the offline build, a Python archive into a private temporary folder and starts Windows
# PowerShell on it. This script:
#   1. provides a working Python: the bundled archive, the installed copy, or a verified download;
#   2. starts installer_core.py, the engine that does the transactional install, the uninstall and
#      the diagnosis (everything risky lives there, where it can be tested on any machine);
#   3. reports progress (a window, text, or nothing), writes the log and returns one exit code.
# It runs as the current Windows user and never asks for administrator rights. It does not touch
# browser policies, the system PATH or security settings.
#
# Modes (-Mode):    Gui     a window (the default)
#                  Console text progress in this console
#                  Silent  no output at all; the exit code is the result
# Actions (-Action): Install (the default), Uninstall, Repair, Diagnose.
#
# Exit codes (the setup program's table):
#   0 done  1 failed  2 cancelled  3 installer damaged  4 a download could not be verified
#   5 a newer version is installed  6 setup is already running  7 Brave is not installed
#   8 this Windows is not supported  9 nothing is installed to remove
#  10 unknown option  11 not a Brave Music Downloader installer  12 Windows PowerShell is missing
# (10, 11 and 12 are decided by the setup program before this script starts.)
#
# Dot-sourcing this file defines the functions without running anything, which is how the tests in
# tests/ps/ call them.
param(
    [string]$PayloadZip = (Join-Path $PSScriptRoot 'Application.zip'),
    [string]$Installer = '',
    [string]$PythonZip = '',
    [ValidateSet('Gui', 'Console', 'Silent')][string]$Mode = 'Gui',
    [ValidateSet('Install', 'Uninstall', 'Repair', 'Diagnose')][string]$Action = 'Install',
    [string]$Log = '',
    [switch]$Bundle,
    [switch]$AllowDowngrade,
    [switch]$RequireBrave,
    [switch]$NoShortcuts,
    [switch]$RemoveHistory,
    [switch]$AllowUnverified
)

$script:Version = '2.5.0'
$script:ExtensionId = 'lliokngbogdlnnjffdfgdjabpmjpobab'
$script:StartMenuFolder = 'Brave Music Downloader'
$script:LogFile = ''          # set by Invoke-Main; the engine gets a copy through --log-file
$script:Echo = $false         # Console mode: log lines are also printed

# The engine reports a category; it decides the exit code.
$script:ExitCodes = @{ 'ok' = 0; 'failed' = 1; 'cancelled' = 2; 'damaged' = 3; 'download' = 4; 'downgrade' = 5
                       'busy' = 6; 'brave-missing' = 7; 'unsupported' = 8; 'not-installed' = 9 }
$script:ExitText = @{
    0 = 'Done.'
    1 = 'Setup did not complete. Your previous installation was left or restored as it was.'
    2 = 'Setup was cancelled. Nothing was changed.'
    3 = 'The installer file is damaged or incomplete. Download it again and compare its SHA-256 with the published one.'
    4 = 'A download could not be completed or verified. Check your internet connection and security software, then run setup again.'
    5 = 'A newer version is already installed. Nothing was changed. Run setup with /ALLOWDOWNGRADE to install this version on purpose.'
    6 = 'Another setup is already running. Wait for it to finish, or restart the computer, then run setup again.'
    7 = 'Brave is not installed. Install Brave first (https://brave.com/download), then run setup again.'
    8 = 'This version of Windows is not supported. Brave Music Downloader needs 64-bit Windows 10 or Windows 11.'
    9 = 'Brave Music Downloader is not installed, so there is nothing to remove.'
    10 = 'Unknown or malformed option. Run setup with /? for the list of options.'
    11 = 'This file is not a Brave Music Downloader installer.'
    12 = 'Windows PowerShell could not be started. Setup needs the PowerShell that ships with Windows.'
}

# ---- small helpers ---------------------------------------------------------------------

# An exception that carries the exit category of the setup program.
function New-SetupError([string]$Category, [string]$Message) {
    $problem = New-Object System.InvalidOperationException $Message
    $problem.Data['Category'] = $Category
    return $problem
}

function Get-ErrorCategory($Exception) {
    for ($item = $Exception; $null -ne $item; $item = $item.InnerException) {
        if ($item.Data -and $item.Data['Category']) { return [string]$item.Data['Category'] }
    }
    return 'failed'
}

function Get-ExitCode($Result) {
    if ($null -eq $Result) { return 1 }                # no result file: the engine did not get that far
    if ($Result.ok) { return 0 }
    $category = [string]$Result.category
    if ($category -and $script:ExitCodes.ContainsKey($category)) { return [int]$script:ExitCodes[$category] }
    return 1
}

function Get-ExitText([int]$Code) {
    if ($script:ExitText.ContainsKey($Code)) { return [string]$script:ExitText[$Code] }
    return [string]$script:ExitText[1]
}

function Write-Log {
    param([string]$Message)
    if ($script:LogFile) {
        $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
        try { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 -ErrorAction Stop } catch { }
    }
    if ($script:Echo) { Write-Host $Message }
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

# folder\part\part, with the separator of this system (tests/ps runs the logic elsewhere too)
function Join-Parts([string]$Base, [string[]]$Parts) {
    $path = $Base
    foreach ($part in $Parts) { $path = [System.IO.Path]::Combine($path, $part) }
    return $path
}

function Test-Sha256([string]$Path, [string]$Expected) {
    if (-not $Expected) { return $false }
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ieq $Expected)
}

function Quote([string]$Text) {
    if ($Text -match '"') { throw (New-SetupError 'failed' 'A path contains a quote and cannot be passed on.') }
    return '"' + $Text + '"'
}

# The command line of a child process: every argument in double quotes. Paths cannot contain
# quotes, so there is nothing to escape.
function ConvertTo-ArgumentLine([string[]]$Arguments) {
    return ((@($Arguments | ForEach-Object { Quote ([string]$_) })) -join ' ')
}

function Get-LockComponent($Lock, [string]$Name) {
    if ($null -eq $Lock) { throw (New-SetupError 'damaged' 'tools.lock.json is missing or unreadable.') }
    foreach ($component in @($Lock.components)) {
        if ($component.name -eq $Name) { return $component }
    }
    throw (New-SetupError 'damaged' "tools.lock.json has no entry for $Name.")
}

function Format-Size([object]$Bytes) {
    if ($null -eq $Bytes) { return '' }
    return ('{0:N1} MB' -f ([double]$Bytes / 1MB))
}

function Get-DownloadName([string]$Url) {
    if ($Url -like 'bundle:*') { return $Url.Substring(7) }
    $name = ($Url -split '[?#]')[0]
    return ($name.TrimEnd('/') -split '/')[-1]
}

function Format-Download($Event) {
    $name = Get-DownloadName ([string]$Event.url)
    $received = Format-Size $Event.received
    if ($Event.announced -and [double]$Event.announced -gt 0) {
        $percent = [Math]::Min(100, ([double]$Event.received / [double]$Event.announced) * 100)
        return ('Downloading {0}  {1} of {2} ({3:P0})' -f $name, $received, (Format-Size $Event.announced), ($percent / 100))
    }
    return ('Downloading {0}  {1}' -f $name, $received)
}

function New-ProgressEvent([string]$Event, [hashtable]$Fields) {
    $data = @{ event = $Event }
    foreach ($key in $Fields.Keys) { $data[$key] = $Fields[$key] }
    return (New-Object psobject -Property $data)
}

# ---- what the setup program left in its work folder --------------------------------------

# The script itself: 64-bit Windows 10 or 11. The values are parameters so that they can be tested.
function Test-SupportedWindows {
    param([string]$OsName = $env:OS, [version]$Version = [Environment]::OSVersion.Version,
          [bool]$Is64Bit = [Environment]::Is64BitOperatingSystem)
    if ($OsName -ne 'Windows_NT') { return $true }      # not Windows: nothing to check here
    return ($Is64Bit -and $Version.Major -ge 10)
}

function Get-BraveCandidates {
    $candidates = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Parts $base @('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe')) }
    }
    return $candidates
}

# Brave's own entry in the registry (best effort; not every Brave install writes it).
function Get-RegisteredBrave {
    foreach ($hive in 'HKLM:', 'HKCU:') {
        $key = $hive + '\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\brave.exe'
        try {
            $value = (Get-ItemProperty -LiteralPath $key -ErrorAction Stop).'(default)'
            if ($value -and (Test-Path -LiteralPath $value -PathType Leaf)) { return [string]$value }
        } catch { }
    }
    return $null
}

function Find-Brave([string[]]$Candidates = @(Get-BraveCandidates)) {
    foreach ($path in $Candidates) {
        if ($path -and (Test-Path -LiteralPath $path -PathType Leaf)) { return [string]$path }
    }
    return (Get-RegisteredBrave)
}

# Work folders of earlier runs: %TEMP%\BMD-Setup-<process id>-<tick count> belongs to the setup
# program with that process id. When that process no longer exists the folder is garbage. The
# other names come from older versions and have no owner, so they are removed only after a day.
function Remove-StaleWorkFolders {
    param([string]$Temp = $env:TEMP, [string]$Keep = '', [datetime]$Now = (Get-Date))
    $removed = @()
    if (-not $Temp -or -not (Test-Path -LiteralPath $Temp -PathType Container)) { return $removed }
    $keepPath = ''
    if ($Keep) {
        $keepPath = [System.IO.Path]::GetFullPath($Keep).TrimEnd([System.IO.Path]::DirectorySeparatorChar,
                                                                 [System.IO.Path]::AltDirectorySeparatorChar)
    }
    $folders = @(Get-ChildItem -LiteralPath $Temp -Directory -ErrorAction SilentlyContinue | Where-Object {
        $_.Name -match '^BMD-Setup-\d+-\d+$' -or $_.Name -like 'BMD-Install-*' -or $_.Name -like 'bmd-uninstall-*' })
    foreach ($folder in $folders) {
        if ($keepPath -and ($folder.FullName -ieq $keepPath)) { continue }
        if ($folder.Name -match '^BMD-Setup-(\d+)-\d+$') {
            # An id that cannot be asked about is not proof that its owner is gone: the folder stays.
            $owner = [long]0
            if (-not [long]::TryParse($Matches[1], [ref]$owner) -or $owner -gt [int]::MaxValue) { continue }
            if (Get-Process -Id ([int]$owner) -ErrorAction SilentlyContinue) { continue }   # that setup is still running
        } elseif (($Now - $folder.LastWriteTime).TotalHours -lt 24) {
            continue
        }
        try {
            Remove-Item -LiteralPath $folder.FullName -Recurse -Force -ErrorAction Stop
            $removed += $folder.Name
        } catch { }
    }
    return $removed
}

# ---- Python -----------------------------------------------------------------------------

# Download `Url` to `Destination` and check its SHA-256. The file is only moved into place after
# the hash matched, so nothing unverified is ever used. Progress goes through `Report` as the same
# events the engine emits; `Pump` is called while waiting (a window stays responsive that way).
function Get-VerifiedDownload {
    param([string]$Url, [string]$Destination, [string]$Sha256, [scriptblock]$Report, [scriptblock]$Pump,
          [int]$Attempts = 3)
    $parent = Split-Path -Parent $Destination
    if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
    if ((Test-Path -LiteralPath $Destination -PathType Leaf) -and (Test-Sha256 $Destination $Sha256)) { return }
    $partial = $Destination + '.partial'
    $failure = 'no attempt was made'
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $response = $null
        $source = $null
        $target = $null
        try {
            Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
            Write-Log "Downloading $Url"
            & $Report (New-ProgressEvent 'component' @{ name = 'download'; status = "Downloading $(Get-DownloadName $Url)..." })
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $request = [System.Net.HttpWebRequest]::Create($Url)
            $request.UserAgent = "BraveMusicDownloader-Installer/$script:Version"
            $request.Timeout = 60000
            $request.ReadWriteTimeout = 60000
            $response = $request.GetResponse()
            $total = $response.ContentLength
            $source = $response.GetResponseStream()
            $target = [System.IO.File]::Create($partial)
            $buffer = New-Object byte[] 262144
            $received = [long]0
            $lastReport = [Diagnostics.Stopwatch]::StartNew()
            while (($read = $source.Read($buffer, 0, $buffer.Length)) -gt 0) {
                $target.Write($buffer, 0, $read)
                $received += $read
                if ($received -gt 200MB) { throw (New-SetupError 'download' 'The download is larger than expected; it was discarded.') }
                if ($lastReport.ElapsedMilliseconds -ge 200) {
                    & $Report (New-ProgressEvent 'download' @{ url = $Url; received = $received; resume_from = 0
                                                              announced = $(if ($total -gt 0) { $total } else { $null }) })
                    & $Pump
                    $lastReport.Restart()
                }
            }
            $target.Flush(); $target.Close(); $target = $null
            $source.Close(); $source = $null
            $response.Close(); $response = $null
            if ($Sha256 -and -not (Test-Sha256 $partial $Sha256)) {
                Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
                throw (New-SetupError 'download' "Checksum mismatch for $(Get-DownloadName $Url): the file is not the one this setup expects. It was discarded.")
            }
            Move-Item -LiteralPath $partial -Destination $Destination -Force
            & $Report (New-ProgressEvent 'downloaded' @{ url = $Url; bytes = $received })
            return
        } catch {
            $failure = $_.Exception.Message
            Write-Log "  Download attempt $attempt failed: $failure"
            if ((Get-ErrorCategory $_.Exception) -eq 'download') { break }     # a wrong file is not retried
            if ($attempt -lt $Attempts) { Start-Sleep -Seconds (2 * $attempt) }
        } finally {
            if ($target) { $target.Close() }
            if ($source) { $source.Close() }
            if ($response) { $response.Close() }
        }
    }
    throw (New-SetupError 'download' "Download failed ($failure). Check your internet connection and security software, then run setup again.")
}

# Without a pinned hash, Python is trusted only when Windows says python.exe carries the
# publisher's code signature. `SignatureChecker` is a function of the executable returning
# @{ Status; Subject } (Get-AuthenticodeSignature on Windows, a stand-in in the tests).
function Get-PythonVerification {
    param([string]$PythonExe, $Entry, [bool]$AllowUnverified, [scriptblock]$SignatureChecker)
    $signature = & $SignatureChecker $PythonExe
    $signer = [string]$Entry.authenticode
    if ($signature.Status -eq 'Valid' -and $signer -and ([string]$signature.Subject -like ('*' + $signer + '*'))) {
        Write-Log "python.exe has a valid $signer signature."
        return 'authenticode'
    }
    if ($AllowUnverified) {
        Write-Log "WARNING: Python could not be verified (signature status: $($signature.Status)); continuing because -AllowUnverified was given."
        return 'unverified'
    }
    throw (New-SetupError 'download' "Python could not be verified: python.exe is not signed by '$signer' (signature status: $($signature.Status)). Refusing to continue.")
}

# A working Python for the engine, in `<work folder>\python`.
#   Install and Repair need the archive as well (the engine installs it): the bundled one, else a
#   verified download. Uninstall and Diagnose only need an interpreter: the installed copy (no
#   download), else the bundled one, else a verified download.
# Returns @{ Exe; Zip; Source; Verified; Entry }.
# .NET's own unzip instead of Expand-Archive: it needs no module (Microsoft.PowerShell.Archive
# refused to load on a PowerShell without culture data), and the same calls exist in 5.1 and 7.
# Existing files are overwritten, as Expand-Archive -Force does.
function Expand-Zip([string]$Zip, [string]$Target) {
    if (-not ('System.IO.Compression.ZipFile' -as [type])) { Add-Type -AssemblyName System.IO.Compression.FileSystem }
    New-Item -ItemType Directory -Force -Path $Target | Out-Null
    $archive = [System.IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -match '(^|/)\.\.(/|$)') { throw 'The archive holds a path that leaves its folder.' }
            $path = Join-Path $Target ($entry.FullName.Replace('/', [System.IO.Path]::DirectorySeparatorChar))
            if ($entry.FullName.EndsWith('/')) { New-Item -ItemType Directory -Force -Path $path | Out-Null; continue }
            $parent = Split-Path -Parent $path
            if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $path, $true)
        }
    } finally { $archive.Dispose() }
}

function Initialize-Python {
    param([hashtable]$Setup, [string]$PythonZip, [bool]$AllowUnverified, [scriptblock]$Report, [scriptblock]$Pump,
          [scriptblock]$SignatureChecker = {
            param([string]$Path)
            $signature = Get-AuthenticodeSignature -LiteralPath $Path
            return @{ Status = [string]$signature.Status
                      Subject = $(if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }) }
          })
    $entry = Get-LockComponent (Read-JsonFile (Join-Path $Setup.EngineRoot 'tools.lock.json')) 'python'
    $target = Join-Path $Setup.Work 'python'
    $installed = Join-Parts $Setup.App @('python', 'python.exe')
    $needsArchive = $Setup.Action -eq 'Install' -or $Setup.Action -eq 'Repair'
    $source = ''
    $zip = ''
    $verified = 'sha256'
    $pinned = [string]$entry.sha256

    if (-not $needsArchive -and (Test-Path -LiteralPath $installed -PathType Leaf)) {
        Copy-Item -LiteralPath (Join-Path $Setup.App 'python') -Destination $target -Recurse
        $source = 'installed'
        Write-Log 'Using the installed Python.'
    } else {
        if ($PythonZip) {
            # The setup program checked this archive while unpacking it; with a pinned lock it must
            # also be the version this release was built from.
            if ($pinned -and -not (Test-Sha256 $PythonZip $pinned)) {
                throw (New-SetupError 'damaged' 'The bundled Python is not the version pinned for this release.')
            }
            $zip = $PythonZip
            $source = 'bundled'
            Write-Log 'Using the Python bundled in this installer.'
        } else {
            $zip = Join-Path $Setup.Work 'python-download.zip'
            Get-VerifiedDownload -Url ([string]$entry.url) -Destination $zip -Sha256 $pinned -Report $Report -Pump $Pump
            $source = 'download'
        }
        Expand-Zip -Zip $zip -Target $target
    }
    $exe = Join-Path $target 'python.exe'
    if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
        throw (New-SetupError 'failed' 'The Python archive could not be unpacked (python.exe is missing).')
    }
    if ($source -ne 'installed' -and -not $pinned) {
        $verified = Get-PythonVerification -PythonExe $exe -Entry $entry -AllowUnverified $AllowUnverified -SignatureChecker $SignatureChecker
    }
    return @{ Exe = $exe; Zip = $zip; Source = $source; Verified = $verified; Entry = $entry }
}

# ---- the engine -------------------------------------------------------------------------

function Start-Engine {
    param([string]$Python, [string[]]$Arguments, [string]$Folder)
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = $Python
    $info.Arguments = ConvertTo-ArgumentLine $Arguments
    $info.WorkingDirectory = $Folder
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true              # no console window, whatever mode setup runs in
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
    } catch {
        throw (New-SetupError 'failed' "The setup engine could not be started ($($_.Exception.Message)).")
    }
    return $process
}

# Where a file was read up to. The engine appends JSON lines to the progress file; only complete
# lines are handed out.
function New-LineCursor([string]$Path) {
    return New-Object psobject -Property @{ Path = $Path; Offset = [long]0 }
}

function Read-NewLines($Cursor) {
    $lines = @()
    if (-not (Test-Path -LiteralPath $Cursor.Path -PathType Leaf)) { return $lines }
    $stream = $null
    try {
        $stream = New-Object System.IO.FileStream($Cursor.Path, ([System.IO.FileMode]::Open),
                                                  ([System.IO.FileAccess]::Read), ([System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete))
        if ($stream.Length -lt $Cursor.Offset) { $Cursor.Offset = 0 }        # the file was replaced
        [void]$stream.Seek($Cursor.Offset, [System.IO.SeekOrigin]::Begin)
        $buffer = New-Object byte[] ([int]($stream.Length - $Cursor.Offset))
        $count = 0
        while ($count -lt $buffer.Length) {
            $read = $stream.Read($buffer, $count, $buffer.Length - $count)
            if ($read -le 0) { break }
            $count += $read
        }
        $last = -1
        for ($i = $count - 1; $i -ge 0; $i--) { if ($buffer[$i] -eq 10) { $last = $i; break } }
        if ($last -lt 0) { return $lines }
        $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $last + 1)
        $Cursor.Offset += $last + 1
        $lines = @($text -split "`n" | ForEach-Object { $_.TrimEnd("`r") } | Where-Object { $_ -ne '' })
    } catch {
        return $lines                          # being written right now: the next poll reads the rest
    } finally {
        if ($stream) { $stream.Close() }
    }
    return $lines
}

function Read-ProgressEvents($Cursor) {
    $events = @()
    foreach ($line in (Read-NewLines $Cursor)) {
        try { $events += ($line | ConvertFrom-Json) } catch { }
    }
    return $events
}

# Run one engine command and wait for it. Returns @{ Code; Result; Exit; Message }: `Code` is the
# exit code of the setup program as the result file says, `Result` the parsed result file.
function Invoke-EngineRun {
    param([hashtable]$Setup, [string[]]$Arguments, [scriptblock]$Report, [scriptblock]$Pump)
    $progress = Join-Path $Setup.Work 'progress.jsonl'
    $resultFile = Join-Path $Setup.Work 'result.json'
    $cancel = Join-Path $Setup.Work 'cancel'
    foreach ($stale in @($progress, $resultFile, $cancel)) {
        Remove-Item -LiteralPath $stale -Force -ErrorAction SilentlyContinue
    }
    $full = @($Arguments) + @('--progress-file', $progress, '--cancel-file', $cancel, '--result-file', $resultFile)
    if ($script:LogFile) { $full += @('--log-file', $script:LogFile) }
    Write-Log ('Running: ' + ((@($Arguments | ForEach-Object { [string]$_ })) -join ' '))
    $process = Start-Engine -Python $Setup.Python.Exe -Arguments $full -Folder $Setup.Work
    $cursor = New-LineCursor $progress
    $showing = $true
    while (-not $process.HasExited) {
        if ($showing) {
            # A failing display must not leave the engine running on its own: it is only switched off.
            try { foreach ($event in (Read-ProgressEvents $cursor)) { & $Report $event } }
            catch { $showing = $false; Write-Log "The progress display stopped: $($_.Exception.Message)" }
        }
        if ($script:CancelRequested -and -not (Test-Path -LiteralPath $cancel)) {
            New-Item -ItemType File -Path $cancel -Force | Out-Null     # the engine stops at its next safe point
        }
        try { & $Pump } catch { Start-Sleep -Milliseconds 250 }
    }
    $process.WaitForExit()
    # What was written since the last poll, or everything if the engine ended before the first one.
    # The display may fail here as anywhere else: the result is the engine's either way.
    if ($showing) {
        try { foreach ($event in (Read-ProgressEvents $cursor)) { & $Report $event } }
        catch { Write-Log "The progress display stopped: $($_.Exception.Message)" }
    }
    $exitCode = $process.ExitCode
    $process.Close()
    $result = Read-JsonFile $resultFile
    if ($null -eq $result) {
        # The engine always writes the file, even when it fails; without it nothing is known about
        # the state of the installation, so this counts as a failed setup.
        Write-Log "The engine ended with exit code $exitCode without writing a result file."
        return @{ Code = 1; Result = $null; Exit = $exitCode; Message = "The setup engine ended unexpectedly (exit code $exitCode)." }
    }
    Write-Log ("Result: ok={0} category={1}" -f $result.ok, $result.category)
    if ($result.message) { Write-Log ([string]$result.message) }
    return @{ Code = (Get-ExitCode $result); Result = $result; Exit = $exitCode; Message = [string]$result.message }
}

# ---- what is shown at the end -----------------------------------------------------------

function Get-ExtensionSteps([string]$ExtensionFolder, [bool]$OnClipboard) {
    $lines = @('Finish in Brave:', '  1. Open brave://extensions.', '  2. Turn on Developer mode (upper right).')
    if ($OnClipboard) { $lines += '  3. Click Load unpacked and select this folder (its path is on your clipboard):' }
    else { $lines += '  3. Click Load unpacked and select this folder:' }
    $lines += @("     $ExtensionFolder", '     If an older version is already loaded, click its reload arrow instead.',
                '  4. Open the extension, choose Full dashboard > Diagnostics, and press Run checks.',
                "  Extension ID: $script:ExtensionId")
    return $lines
}

function Copy-ExtensionFolder([string]$ExtensionFolder) {
    try { Set-Clipboard -Value $ExtensionFolder -ErrorAction Stop; return $true } catch { return $false }
}

# Start menu entries. They run the uninstaller that is installed with the program, so they keep
# working when the installer file is gone.
function Get-ShortcutDefinitions {
    param([string]$Root, [string]$PowerShell,
          [string]$Explorer = $(if ($env:WINDIR) { Join-Path $env:WINDIR 'explorer.exe' } else { '' }), [string]$Icon = '')
    $app = Join-Parts $Root @('app')
    $uninstall = Join-Parts $app @('Uninstall.ps1')
    $prefix = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File ' + (Quote $uninstall)
    $definitions = @(
        @{ Name = 'Extension folder'; Target = $Explorer; Arguments = (Quote (Join-Parts $app @('extension')))
           Description = 'The folder to load in Brave (brave://extensions, Load unpacked)' },
        @{ Name = 'Diagnose Brave Music Downloader'; Target = $PowerShell; Arguments = "$prefix -Diagnose"
           Description = 'Check the installation and write a report' },
        @{ Name = 'Uninstall Brave Music Downloader'; Target = $PowerShell; Arguments = $prefix
           Description = 'Remove the program; music, settings and history are kept' }
    )
    if ($Icon) { foreach ($definition in $definitions) { $definition.Icon = $Icon } }
    return $definitions
}

function New-Shortcuts([string]$Folder, $Definitions) {
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($definition in $Definitions) {
            $link = $shell.CreateShortcut((Join-Path $Folder ($definition.Name + '.lnk')))
            $link.TargetPath = [string]$definition.Target
            $link.Arguments = [string]$definition.Arguments
            $link.Description = [string]$definition.Description
            if ($definition.Icon) { $link.IconLocation = [string]$definition.Icon + ',0' }
            $link.Save()
        }
    } finally {
        [void][System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

function Get-StartMenuFolder {
    if (-not $env:APPDATA) { return '' }
    return (Join-Parts $env:APPDATA @('Microsoft', 'Windows', 'Start Menu', 'Programs', $script:StartMenuFolder))
}

# ---- the work itself: the same in every mode ---------------------------------------------

# `Report` receives the progress events, `Pump` is called while waiting. Returns the exit code and
# leaves the text for the end in $Setup.Outcome (@{ Kind; Code; Lines }).
function Invoke-SetupWork {
    param([hashtable]$Setup, [scriptblock]$Report, [scriptblock]$Pump)
    try {
        & $Report (New-ProgressEvent 'phase' @{ phase = 'preparing'; label = 'Preparing setup' })
        Write-Log ("Brave: " + $(if ($Setup.Browser) { $Setup.Browser } else { 'not found in its usual folders' }))

        # Before anything is downloaded: an uninstall with nothing to remove needs no Python at all.
        if ($Setup.Action -eq 'Uninstall') {
            $record = Join-Path $Setup.Root 'install-manifest.json'
            if (-not (Test-Path -LiteralPath $record) -and -not (Test-Path -LiteralPath $Setup.App)) {
                Write-Log 'Nothing is installed; nothing to remove.'
                $Setup.Outcome = @{ Kind = 'Nothing'; Code = 9; Lines = @((Get-ExitText 9)) }
                return 9
            }
        }

        $Setup.Python = Initialize-Python -Setup $Setup -PythonZip $Setup.PythonZip -AllowUnverified $Setup.AllowUnverified `
                                           -Report $Report -Pump $Pump

        $common = @($Setup.Core)
        $run = $null
        if ($Setup.Action -eq 'Install' -or $Setup.Action -eq 'Repair') {
            $engine = @('install', '--root', $Setup.Root, '--payload', $Setup.PayloadZip, '--extension-id', $script:ExtensionId,
                        '--smoke', '--build-launcher', '--check-host')
            if ($Setup.Python.Zip) { $engine += @('--python-zip', $Setup.Python.Zip, '--python-verified', $Setup.Python.Verified) }
            if ($Setup.Bundle) { $engine += @('--bundle', $Setup.Installer) }
            if ($Setup.AllowUnverified) { $engine += '--allow-unverified' }
            if ($Setup.AllowDowngrade) { $engine += '--allow-downgrade' }
            $run = Invoke-EngineRun -Setup $Setup -Arguments ($common + $engine) -Report $Report -Pump $Pump
            $Setup.Outcome = Get-InstallOutcome -Setup $Setup -Run $run
        } elseif ($Setup.Action -eq 'Uninstall') {
            $engine = @('uninstall', '--root', $Setup.Root)
            if ($Setup.RemoveHistory) { $engine += @('--remove-history', '--confirm', 'DELETE HISTORY') }
            $run = Invoke-EngineRun -Setup $Setup -Arguments ($common + $engine) -Report $Report -Pump $Pump
            $Setup.Outcome = Get-UninstallOutcome -Setup $Setup -Run $run
        } else {
            # Not $report: that is the $Report parameter (names ignore case), which the engine run needs.
            $reportPath = Join-Path $Setup.Root 'diagnosis.txt'
            New-Item -ItemType Directory -Force -Path $Setup.Root | Out-Null
            $browserText = 'not found'
            if ($Setup.Browser) { $browserText = [string]$Setup.Browser }
            $header = @("Brave Music Downloader $script:Version - diagnosis report, $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))",
                        "Setup mode: $($Setup.Mode); Brave: $browserText", '')
            # Without a byte-order mark and with line feeds, as the engine writes: Windows PowerShell 5.1
            # would put a mark in front of Set-Content -Encoding UTF8, and the engine could not read
            # browser.json then. UTF8Encoding($false) writes the same bytes in both PowerShell versions.
            $utf8 = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($reportPath, (($header -join "`n") + "`n"), $utf8)
            $facts = Join-Path $Setup.Work 'browser.json'
            $browser = @{ found = [bool]$Setup.Browser; path = $Setup.Browser; mode = $Setup.Mode }
            [System.IO.File]::WriteAllText($facts, ($browser | ConvertTo-Json -Compress), $utf8)
            $engine = @('diagnose', '--root', $Setup.Root, '--extra-json', $facts, '--json-report',
                        (Join-Path $Setup.Root 'diagnosis.json'), '--log-file', $reportPath)
            $run = Invoke-EngineRun -Setup $Setup -Arguments ($common + $engine) -Report $Report -Pump $Pump
            $Setup.Outcome = @{ Kind = 'Diagnosed'; Code = $run.Code; Lines = @(
                'The diagnosis report was written to', "  $reportPath", 'Send this file with a support request if you need help.') }
            if ($run.Code -ne 0) { $Setup.Outcome.Lines += 'Some checks failed; the FAIL lines in the report say what.' }
        }
        return $Setup.Outcome.Code
    } catch {
        $code = Get-ExitCode @{ ok = $false; category = (Get-ErrorCategory $_.Exception) }
        $message = $_.Exception.Message
        Write-Log "Setup did not complete (exit code $code): $message"
        $Setup.Outcome = @{ Kind = 'Failed'; Code = $code; Lines = @(Get-ExitText $code) }
        if ($message -and $message -ne (Get-ExitText $code)) { $Setup.Outcome.Lines += "Details: $message" }
        if ($script:LogFile) { $Setup.Outcome.Lines += "Log: $script:LogFile" }
        return $code
    }
}

function Get-InstallOutcome([hashtable]$Setup, [hashtable]$Run) {
    if ($Run.Code -ne 0) {
        $lines = @(Get-ExitText $Run.Code)
        if ($Run.Message) { $lines += "Details: $($Run.Message)" }
        if ($script:LogFile) { $lines += "Log: $script:LogFile" }
        return @{ Kind = 'Failed'; Code = $Run.Code; Lines = $lines }
    }
    # The Start menu entries are a convenience: when they cannot be created the installation stands.
    if (-not $Setup.NoShortcuts) {
        $folder = Get-StartMenuFolder
        if ($folder) {
            try {
                $icon = Join-Parts $Setup.App @('bridge', 'setup.ico')
                if (-not (Test-Path -LiteralPath $icon)) { $icon = '' }
                $powerShell = [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
                New-Shortcuts -Folder $folder -Definitions (Get-ShortcutDefinitions -Root $Setup.Root -PowerShell $powerShell -Icon $icon)
                Write-Log "Start menu shortcuts created in $folder"
            } catch {
                Write-Log "Start menu shortcuts could not be created: $($_.Exception.Message)"
            }
        }
    }
    $lines = @()
    $result = $Run.Result.result
    if ($result.fresh_install) { $lines += "Brave Music Downloader $script:Version is installed." }
    elseif ($result.previous_version) { $lines += "Updated $($result.previous_version) -> $script:Version." }
    else { $lines += "Brave Music Downloader $script:Version is installed again; settings and history were kept." }
    if ($result.reused) { $lines += 'Kept unchanged components: ' + (@($result.reused) -join ', ') + '.' }
    if ($result.repaired) { $lines += 'Repaired components: ' + (@($result.repaired) -join ', ') + '.' }
    $Setup.ExtensionFolder = Join-Path $Setup.App 'extension'
    $onClipboard = $Setup.Mode -ne 'Silent' -and (Copy-ExtensionFolder $Setup.ExtensionFolder)
    $lines += ''
    $lines += (Get-ExtensionSteps -ExtensionFolder $Setup.ExtensionFolder -OnClipboard:$onClipboard)
    return @{ Kind = 'Installed'; Code = 0; Lines = $lines }
}

function Get-UninstallOutcome([hashtable]$Setup, [hashtable]$Run) {
    if ($Run.Code -ne 0) {
        $lines = @(Get-ExitText $Run.Code)
        if ($Run.Message) { $lines += "Details: $($Run.Message)" }
        if ($script:LogFile) { $lines += "Log: $script:LogFile" }
        return @{ Kind = 'Failed'; Code = $Run.Code; Lines = $lines }
    }
    $lines = @('Brave Music Downloader was removed.')
    if ($Setup.RemoveHistory) { $lines += 'The download history was removed, as requested.' }
    else { $lines += 'Your music, settings and download history were kept.' }
    $lines += 'Remove the extension in Brave as well: open brave://extensions and click Remove.'
    return @{ Kind = 'Removed'; Code = 0; Lines = $lines }
}

# ---- console output ---------------------------------------------------------------------

function Write-ProgressEvent($Event) {
    switch ([string]$Event.event) {
        'phase' { Write-Host ("`n== {0} ==" -f $Event.label) -ForegroundColor Cyan }
        'component' { Write-Progress -Activity 'Brave Music Downloader Setup' -Status ([string]$Event.status) -PercentComplete -1 }
        'download' {
            if ($Event.announced -and [double]$Event.announced -gt 0) {
                $percent = [Math]::Min(100, ([double]$Event.received / [double]$Event.announced) * 100)
                Write-Progress -Activity 'Brave Music Downloader Setup' -Status (Format-Download $Event) -PercentComplete $percent
            } else {
                Write-Progress -Activity 'Brave Music Downloader Setup' -Status (Format-Download $Event) -PercentComplete -1
            }
        }
        'downloaded' {
            Write-Progress -Activity 'Brave Music Downloader Setup' -Completed
            Write-Host ('Downloaded {0} ({1})' -f (Get-DownloadName ([string]$Event.url)), (Format-Size $Event.bytes))
        }
    }
}

function Invoke-Console {
    param([hashtable]$Setup)
    $report = { param($event) Write-ProgressEvent $event }
    $code = Invoke-SetupWork -Setup $Setup -Report $report -Pump { Start-Sleep -Milliseconds 200 }
    Write-Progress -Activity 'Brave Music Downloader Setup' -Completed
    Write-Host ''
    foreach ($line in $Setup.Outcome.Lines) { Write-Host $line }
    if ($code -eq 0) { Write-Host "Setup finished (exit code 0)." -ForegroundColor Green }
    elseif ($code -eq 9) { Write-Host "Nothing to remove (exit code 9)." -ForegroundColor Yellow }
    else { Write-Host "Setup did not complete (exit code $code)." -ForegroundColor Red }
    if ($Setup.Outcome.Kind -eq 'Installed' -and $Setup.Browser) {
        try { Start-Process -FilePath $Setup.Browser -ArgumentList 'brave://extensions' } catch { }
    }
    return $code
}

function Invoke-Silent {
    param([hashtable]$Setup)
    return (Invoke-SetupWork -Setup $Setup -Report { param($event) } -Pump { Start-Sleep -Milliseconds 250 })
}

# ---- the window -------------------------------------------------------------------------

$script:WindowXaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Title="Brave Music Downloader Setup" Width="600" Height="360"
        WindowStartupLocation="CenterScreen" ResizeMode="CanMinimize"
        SnapsToDevicePixels="True" UseLayoutRounding="True">
  <Window.Resources>
    <Style TargetType="TextBlock">
      <Setter Property="FontFamily" Value="Segoe UI" />
      <Setter Property="Foreground" Value="#FF202124" />
    </Style>
    <Style x:Key="Muted" TargetType="TextBlock" BasedOn="{StaticResource {x:Type TextBlock}}">
      <Setter Property="Foreground" Value="#FF5F6368" />
    </Style>
    <Style TargetType="Button">
      <Setter Property="MinWidth" Value="110" />
      <Setter Property="Height" Value="30" />
      <Setter Property="Margin" Value="8,0,0,0" />
      <Setter Property="FontSize" Value="13" />
    </Style>
  </Window.Resources>
  <Grid Margin="26,22,26,20">
    <Grid.RowDefinitions>
      <RowDefinition Height="Auto" />
      <RowDefinition Height="Auto" />
      <RowDefinition Height="*" />
      <RowDefinition Height="Auto" />
      <RowDefinition Height="Auto" />
    </Grid.RowDefinitions>
    <TextBlock x:Name="Heading" Grid.Row="0" FontSize="21" FontWeight="SemiBold" Text="Brave Music Downloader" />
    <TextBlock x:Name="Subheading" Grid.Row="1" Style="{StaticResource Muted}" Margin="0,2,0,14" Text="Setup" />
    <StackPanel x:Name="ProgressPanel" Grid.Row="2">
      <TextBlock x:Name="Status" TextWrapping="Wrap" FontSize="15" Text="Preparing setup..." />
      <TextBlock x:Name="Detail" TextWrapping="Wrap" Style="{StaticResource Muted}" FontSize="12" Margin="0,6,0,0" Text="" />
    </StackPanel>
    <TextBox x:Name="Details" Grid.Row="2" Visibility="Collapsed" IsReadOnly="True" TextWrapping="Wrap"
             BorderThickness="0" FontFamily="Segoe UI" FontSize="13" VerticalScrollBarVisibility="Auto"
             Foreground="#FF202124" Background="Transparent" />
    <ProgressBar x:Name="Bar" Grid.Row="3" Height="20" Margin="0,14,0,0" Minimum="0" Maximum="100" IsIndeterminate="True" />
    <StackPanel Grid.Row="4" Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,16,0,0">
      <Button x:Name="Secondary" Content="Copy folder" Visibility="Collapsed" />
      <Button x:Name="Primary" Content="Cancel" IsCancel="False" />
    </StackPanel>
  </Grid>
</Window>
'@

function Show-ErrorMessage([string]$Title, [string]$Message) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        [void][System.Windows.Forms.MessageBox]::Show($Message, $Title, 'OK', 'Error')
    } catch { }
}

# What the window shows for a progress event. `Controls` holds the controls an event touches.
function Show-ProgressEvent($Event, $Controls) {
    switch ([string]$Event.event) {
        'phase' {
            $Controls.Status.Text = [string]$Event.label
            $Controls.Bar.IsIndeterminate = $true
            $Controls.Detail.Text = ''
            if ($Event.critical) { $script:Critical = $true; $Controls.Primary.IsEnabled = $false }
        }
        'component' { $Controls.Detail.Text = [string]$Event.status }
        'download' {
            $Controls.Detail.Text = Format-Download $Event
            if ($Event.announced -and [double]$Event.announced -gt 0) {
                $Controls.Bar.IsIndeterminate = $false
                $Controls.Bar.Value = [Math]::Min(100, ([double]$Event.received / [double]$Event.announced) * 100)
            } else { $Controls.Bar.IsIndeterminate = $true }
        }
        'downloaded' { $Controls.Bar.Value = 100; $Controls.Detail.Text = 'Downloaded ' + (Format-Size $Event.bytes) }
    }
}

# The user tries to close the window. While the installation is being swapped in that is refused
# (the button is disabled for the same phase); at any other time it is a cancel.
function Set-WindowCloseRequest($EventArgs) {
    if ($script:Critical -and -not $script:Finished) { $EventArgs.Cancel = $true }
    else { $script:CancelRequested = $true }
}

# The one button: Cancel while setup runs; Close, opening Brave first, when it is done.
function Invoke-PrimaryClick($Window, $Controls) {
    if ($script:Finished) {
        if ($script:BrowserToOpen) { try { Start-Process -FilePath $script:BrowserToOpen -ArgumentList 'brave://extensions' } catch { } }
        $Window.Close()
    } else {
        $script:CancelRequested = $true
        $Controls.Primary.IsEnabled = $false
        $Controls.Primary.Content = 'Cancelling...'
    }
}

function Copy-FolderToClipboard([string]$Folder, $Button) {
    try { Set-Clipboard -Value $Folder -ErrorAction Stop; $Button.Content = 'Copied' } catch { }
}

# After the work: the progress gives way to the result, and the button becomes Close.
function Set-ResultControls($Setup, $Controls) {
    $Controls.ProgressPanel.Visibility = 'Collapsed'
    $Controls.Details.Text = (@($Setup.Outcome.Lines) -join "`r`n")
    $Controls.Details.Visibility = 'Visible'
    $Controls.Bar.Visibility = 'Collapsed'
    $Controls.Primary.IsEnabled = $true
    $Controls.Primary.Content = 'Close'
    $Controls.Primary.IsDefault = $true
    if ($Setup.Outcome.Kind -eq 'Installed') {
        $script:FolderToCopy = $Setup.ExtensionFolder
        $Controls.Secondary.Content = 'Copy folder'
        $Controls.Secondary.Visibility = 'Visible'
        if ($Setup.Browser) {
            $script:BrowserToOpen = $Setup.Browser
            $Controls.Primary.Content = 'Open Brave and close'
        }
    }
}

# The window is driven by one loop: each round processes the window's messages (so it repaints and
# reacts to clicks) and then does a bit of waiting for the engine. It cannot run anywhere but on
# Windows; WINDOWS_QA.md lists what to look at. What the handlers decide is in the functions above.
function Invoke-Gui {
    param([hashtable]$Setup)
    $script:Critical = $false          # the installation is being swapped in: no cancel, no close
    $script:Finished = $false
    $script:WindowClosed = $false
    $script:CancelRequested = $false
    $script:BrowserToOpen = ''
    $script:FolderToCopy = ''
    try {
        Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
        [xml]$xaml = $script:WindowXaml
        $window = [System.Windows.Markup.XamlReader]::Load((New-Object System.Xml.XmlNodeReader $xaml))
    } catch {
        Write-Log "The setup window could not be created: $($_.Exception.Message)"
        Show-ErrorMessage 'Brave Music Downloader Setup' ("The setup window could not be shown ($($_.Exception.Message)).`n`nRun setup again with /CONSOLE, or install silently with /S.")
        return 1
    }
    $controls = @{}
    foreach ($name in 'Subheading', 'ProgressPanel', 'Status', 'Detail', 'Details', 'Bar', 'Secondary', 'Primary') {
        $controls[$name] = $window.FindName($name)
    }
    $controls.Subheading.Text = "Setup $script:Version"
    if ($Setup.Installer -and (Test-Path -LiteralPath $Setup.Installer -PathType Leaf)) {
        try {
            Add-Type -AssemblyName System.Drawing
            $stream = New-Object System.IO.MemoryStream
            ([System.Drawing.Icon]::ExtractAssociatedIcon($Setup.Installer)).Save($stream)
            $stream.Position = 0
            $window.Icon = [System.Windows.Media.Imaging.BitmapFrame]::Create($stream)
            $stream.Close()
        } catch { }                   # the icon is decoration
    }

    # An event handler's arguments are in $args ($_ is not set there). The handlers only hand over
    # to the functions above, which need no window and are tested in tests/ps.
    $window.Add_Closing({ Set-WindowCloseRequest $args[1] })
    $window.Add_Closed({ $script:WindowClosed = $true })
    $controls.Primary.Add_Click({ Invoke-PrimaryClick -Window $window -Controls $controls })
    $controls.Secondary.Add_Click({ Copy-FolderToClipboard -Folder $script:FolderToCopy -Button $controls.Secondary })

    $pump = {
        $window.Dispatcher.Invoke([Action] { }, [System.Windows.Threading.DispatcherPriority]::ApplicationIdle)
        Start-Sleep -Milliseconds 20
    }
    $report = { param($event) Show-ProgressEvent -Event $event -Controls $controls }

    $window.Show()
    & $pump
    $code = Invoke-SetupWork -Setup $Setup -Report $report -Pump $pump
    $script:Finished = $true
    Set-ResultControls -Setup $Setup -Controls $controls
    while (-not $script:WindowClosed) { & $pump }
    return $code
}

# ---- entry point ------------------------------------------------------------------------

function New-SetupContext([string]$Root, [string]$Work, [string]$Mode, [string]$Action) {
    return @{
        Root = $Root; App = (Join-Path $Root 'app'); Work = $Work; EngineRoot = (Join-Path $Work 'engine')
        Core = (Join-Parts $Work @('engine', 'installer', 'installer_core.py'))
        Mode = $Mode; Action = $Action; Browser = $null; Python = $null; Outcome = $null
        PayloadZip = $PayloadZip; Installer = $Installer; PythonZip = $PythonZip
        Bundle = [bool]$Bundle; AllowUnverified = [bool]$AllowUnverified; AllowDowngrade = [bool]$AllowDowngrade
        NoShortcuts = [bool]$NoShortcuts; RemoveHistory = [bool]$RemoveHistory
    }
}

function Invoke-Main {
    $ErrorActionPreference = 'Stop'
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = Join-Path $env:USERPROFILE 'AppData\Local' }
    if (-not $local) { exit 1 }                      # no user profile: nothing can be installed
    $root = Join-Path $local 'BraveMusicDownloader'
    $work = $PSScriptRoot
    $script:LogFile = $Log
    if (-not $script:LogFile) { $script:LogFile = Join-Path $root 'Install.log' }
    try { New-Item -ItemType Directory -Force -Path (Split-Path -Parent $script:LogFile) | Out-Null }
    catch { $script:LogFile = Join-Path $env:TEMP 'BraveMusicDownloader-Setup.log' }
    $script:Echo = ($Mode -eq 'Console')

    $code = 1
    try {
        Write-Log "Brave Music Downloader v$script:Version - setup ($Mode mode, $Action)"
        foreach ($stale in @(Remove-StaleWorkFolders -Keep $work)) { Write-Log "Removed the leftover work folder $stale" }
        if (-not (Test-SupportedWindows)) { throw (New-SetupError 'unsupported' (Get-ExitText 8)) }
        if (-not (Test-Path -LiteralPath $PayloadZip -PathType Leaf)) {
            throw (New-SetupError 'damaged' 'The application archive is missing.')
        }
        $setup = New-SetupContext -Root $root -Work $work -Mode $Mode -Action $Action
        Expand-Zip -Zip $PayloadZip -Target $setup.EngineRoot
        # The engine needs these three to start; everything else in the archive it reads itself.
        foreach ($required in @(@('installer', 'installer_core.py'), @('installer', 'payload_container.py'), @('tools.lock.json'))) {
            if (-not (Test-Path -LiteralPath (Join-Parts $setup.EngineRoot $required) -PathType Leaf)) {
                throw (New-SetupError 'damaged' "The application archive is incomplete ($($required -join '\') is missing).")
            }
        }
        $setup.Browser = Find-Brave
        if ($RequireBrave -and -not $setup.Browser) { throw (New-SetupError 'brave-missing' (Get-ExitText 7)) }
        if ($Mode -eq 'Gui') { $code = Invoke-Gui -Setup $setup }
        elseif ($Mode -eq 'Console') { $code = Invoke-Console -Setup $setup }
        else { $code = Invoke-Silent -Setup $setup }
    } catch {
        $code = Get-ExitCode @{ ok = $false; category = (Get-ErrorCategory $_.Exception) }
        $message = $_.Exception.Message
        Write-Log "Setup did not complete (exit code $code): $message"
        if ($Mode -eq 'Console') {
            Write-Host ''
            Write-Host (Get-ExitText $code) -ForegroundColor Red
            Write-Host "Details: $message"
            Write-Host "Log: $script:LogFile"
        } elseif ($Mode -eq 'Gui' -and -not $script:WindowClosed) {
            Show-ErrorMessage 'Brave Music Downloader Setup' ((Get-ExitText $code) + "`n`n" + $message + "`n`nLog: " + $script:LogFile)
        }
    }
    Write-Log "Exit code $code"
    exit $code
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Main }
