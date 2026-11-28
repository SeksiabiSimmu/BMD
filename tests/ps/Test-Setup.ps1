# Tests for the logic of Setup.ps1 and app\Uninstall.ps1, on any operating system.
#
# Both scripts are dot-sourced, which defines their functions and runs nothing (each starts the
# work only when it is run, not when it is dot-sourced). The functions are then called with files in
# a temporary folder, a stand-in engine and a local HTTP server; the entry points run in child
# PowerShell processes.
#
# What this cannot show: the window itself (WPF needs a Windows desktop; what its handlers decide
# is tested here), the real registry, code signatures, a real Brave, Start menu shortcuts and the
# Windows builds of the tools. WINDOWS_QA.md lists those steps.
#
# Run it through tests/test_setup_ps.py, which starts the HTTP server and passes what the cases
# need in the environment:
#   BMD_TEST_PYTHON   a Python interpreter
#   BMD_TEST_URL      a file served over HTTP, BMD_TEST_SHA256 its hash (computed by someone else)
#   BMD_TEST_PAYLOAD  an Application.zip for the entry-point cases
# or directly:  pwsh -NoProfile -File tests/ps/Test-Setup.ps1 [-Repo <folder>]
# Every case prints one line: `pass|<name>`, `fail|<name>|<what went wrong>` or `skip|<name>|<why>`.
# The exit code is the number of failed cases.
param([string]$Repo = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))

$ErrorActionPreference = 'Stop'
$Repo = [System.IO.Path]::GetFullPath($Repo)
$script:cases = New-Object System.Collections.ArrayList
$python = $env:BMD_TEST_PYTHON
$payload = $env:BMD_TEST_PAYLOAD
$url = $env:BMD_TEST_URL
$urlHash = $env:BMD_TEST_SHA256
$onWindows = $env:OS -eq 'Windows_NT'
$scratch = if ($env:BMD_TEST_TMP) { $env:BMD_TEST_TMP } else {
    Join-Path ([System.IO.Path]::GetTempPath()) ('bmd-ps-tests-' + [guid]::NewGuid().ToString('N')) }
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
Write-Output ("host|" + $PSVersionTable.PSVersion + "|" + [System.Environment]::OSVersion + "|" + $Repo)

. (Join-Path $Repo 'Setup.ps1')

# ---- the harness ------------------------------------------------------------------------------

function ConvertTo-Plain($Value) {
    if ($null -eq $Value) { return '<empty>' }
    if ($Value -is [string]) { return $Value }
    if ($Value -is [System.Collections.IDictionary]) {
        $items = @($Value.Keys | Sort-Object | ForEach-Object { "$_=" + (ConvertTo-Plain $Value[$_]) })
        if ($items.Count -eq 0) { return '{}' }
        return '{' + ($items -join '; ') + '}'
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @($Value | ForEach-Object { ConvertTo-Plain $_ })
        if ($items.Count -eq 0) { return '<empty>' }
        return ($items -join ', ')
    }
    return [string]$Value
}

function Assert($Condition, [string]$Message) { if (-not $Condition) { throw "assertion failed: $Message" } }

function Assert-Same($Expected, $Actual, [string]$What) {
    $left = ConvertTo-Plain $Expected
    $right = ConvertTo-Plain $Actual
    if ($left -cne $right) { throw "$What - expected [$left], got [$right]" }
}

function Assert-Throws([scriptblock]$Body, [string]$Category, [string]$Text, [string]$What) {
    $problem = $null
    try { & $Body | Out-Null } catch { $problem = $_.Exception }
    if (-not $problem) { throw "$What - no error was raised" }
    Assert-Same $Category (Get-ErrorCategory $problem) "$What (error category)"
    if ($Text) { Assert ($problem.Message -like $Text) "$What (the message was: $($problem.Message))" }
}

# A case that cannot run here (something is missing) ends with Skip instead of a pass.
function Skip([string]$Why) { throw "SKIP: $Why" }

function Check([string]$Name, [scriptblock]$Body) {
    try { & $Body | Out-Null; [void]$script:cases.Add("pass|$Name") }
    catch {
        $text = ($_.Exception.Message -replace '[\r\n]+', ' ')
        $line = $_.InvocationInfo.ScriptLineNumber
        if ($text.StartsWith('SKIP:')) { [void]$script:cases.Add("skip|$Name|" + $text.Substring(5).Trim()) }
        else { [void]$script:cases.Add("fail|$Name|line ${line}: $text") }
    }
}

# The comma matters: an empty list given back through the pipeline would come out as nothing.
function New-List { return ,(New-Object System.Collections.ArrayList) }

# A zip archive from a folder (Compress-Archive needs the en-us culture, which this pwsh lacks).
function New-ZipFromFolder([string]$Source, [string]$Zip) {
    if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
    [System.IO.Compression.ZipFile]::CreateFromDirectory($Source, $Zip)
    return $Zip
}

function New-Folder([string]$Name) {
    $folder = Join-Path $scratch $Name
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
    return $folder
}

# Run a PowerShell script in a child process, in the environment of a machine without Brave.
function Invoke-Script([string]$Script, [string[]]$Arguments, [hashtable]$Environment) {
    $quoted = (@(@('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Script) + $Arguments) |
               ForEach-Object { '"' + [string]$_ + '"' }) -join ' '
    $info = New-Object System.Diagnostics.ProcessStartInfo
    $info.FileName = (Get-Process -Id $PID).Path
    $info.Arguments = $quoted
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = $true
    foreach ($name in 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramW6432') { $info.EnvironmentVariables.Remove($name) }
    foreach ($name in $Environment.Keys) { $info.EnvironmentVariables[$name] = [string]$Environment[$name] }
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $info
    [void]$process.Start()
    $process.StandardInput.Close()
    $errors = $process.StandardError.ReadToEndAsync()
    $output = $process.StandardOutput.ReadToEnd()
    $process.WaitForExit()
    $code = $process.ExitCode
    $process.Close()
    return @{ Code = $code; Out = $output; Error = $errors.Result }
}

function New-ChildEnvironment([string]$Name) {
    $values = @{ LOCALAPPDATA = (New-Folder "$Name-local"); TEMP = (New-Folder "$Name-temp")
                 APPDATA = (New-Folder "$Name-roaming") }
    $values['TMP'] = $values['TEMP']
    return $values
}

# A copy of Setup.ps1 in its own folder, as the setup program leaves it, plus an Application.zip.
function New-WorkFolder([string]$Name, [string]$Archive = $payload) {
    $work = New-Folder $Name
    Copy-Item -LiteralPath (Join-Path $Repo 'Setup.ps1') -Destination $work
    if ($Archive) { Copy-Item -LiteralPath $Archive -Destination (Join-Path $work 'Application.zip') }
    return $work
}

# tools.lock.json with only a python entry, as Setup.ps1 reads it.
function Write-Lock([string]$EngineRoot, [string]$Sha256) {
    New-Item -ItemType Directory -Force -Path $EngineRoot | Out-Null
    $lock = @{ components = @(@{ name = 'python'; url = 'https://example.invalid/python.zip'; sha256 = $Sha256
                                  authenticode = 'Python Software Foundation' }, @{ name = 'yt-dlp'; url = 'x' }) }
    ($lock | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath (Join-Path $EngineRoot 'tools.lock.json') -Encoding UTF8
}

# A zip archive holding `python.exe`, as the bundled Python does.
function New-PythonArchive([string]$Zip, [bool]$WithExe = $true) {
    $source = New-Folder ('archive-' + [guid]::NewGuid().ToString('N'))    # never the folder the zip goes into
    if ($WithExe) { [System.IO.File]::WriteAllText((Join-Path $source 'python.exe'), 'interpreter') }
    else { [System.IO.File]::WriteAllText((Join-Path $source 'readme.txt'), 'nothing useful') }
    return (New-ZipFromFolder $source $Zip)
}

function Get-FileSha256([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower() }

# A checker for Get-PythonVerification that answers as Windows would for one signature.
function New-SignatureChecker([string]$Status, [string]$Subject) {
    return [scriptblock]::Create('param($Path) return @{ Status = "' + $Status + '"; Subject = "' + $Subject + '" }')
}

function New-SetupContextIn([string]$Folder, [string]$Action) {
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    return (New-SetupContext -Root (Join-Path $Folder 'BraveMusicDownloader') -Work $Folder -Mode 'Silent' -Action $Action)
}

# A stand-in for Invoke-EngineRun: it records what it was asked and answers as told.
function New-RecordingEngine($Calls, [string]$Outcome, [int]$Code = 0) {
    $shapes = @{
        installed = @{ fresh_install = $true; previous_version = $null; reused = @('python', 'deno'); repaired = @() }
        updated   = @{ fresh_install = $false; previous_version = '2.4.0'; reused = @(); repaired = @('ffmpeg') }
        removed   = @{ removed = @('tool yt-dlp.exe'); kept = @('your music (never touched): C:\Users\me\Music')
                       skipped = @(); failed = @(); dry_run = $false }
    }
    $result = New-Object psobject -Property @{ result = (New-Object psobject -Property $shapes[$Outcome]) }
    return {
        param($Setup, $Arguments, $Report, $Pump)
        [void]$Calls.Add(@($Arguments))
        if ($Code -eq 0) { return @{ Code = 0; Result = $result; Exit = 0; Message = '' } }
        return @{ Code = $Code; Result = $null; Exit = 2; Message = 'the engine said why' }
    }.GetNewClosure()
}

# Controls of the window, as plain objects: the handlers only set properties.
function New-FakeControls {
    return @{
        Subheading     = (New-Object psobject -Property @{ Text = '' })
        ProgressPanel  = (New-Object psobject -Property @{ Visibility = 'Visible' })
        Status         = (New-Object psobject -Property @{ Text = '' })
        Detail         = (New-Object psobject -Property @{ Text = '' })
        Details        = (New-Object psobject -Property @{ Text = ''; Visibility = 'Collapsed' })
        Bar            = (New-Object psobject -Property @{ IsIndeterminate = $true; Value = [double]0; Visibility = 'Visible' })
        Secondary      = (New-Object psobject -Property @{ Content = 'Copy folder'; Visibility = 'Collapsed' })
        Primary        = (New-Object psobject -Property @{ Content = 'Cancel'; IsEnabled = $true; IsDefault = $false })
    }
}

# A fake window: it counts the Close() calls.
function New-FakeWindow {
    $window = New-Object psobject -Property @{ CloseCount = 0 }
    $window | Add-Member -MemberType ScriptMethod -Name Close -Value { $this.CloseCount = $this.CloseCount + 1 }
    return $window
}

# A stand-in for the engine of Setup.ps1: it reports what it was asked and behaves as told
# (FAKE_ENGINE = ok | wait-for-cancel | wait-for-release | no-result).
$script:FakeEngine = @'
import json, os, sys, time
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
progress, cancel, result, log = (value('--progress-file'), value('--cancel-file'), value('--result-file'), value('--log-file'))
mode = os.environ.get('FAKE_ENGINE', 'ok')
with open(os.environ['FAKE_ARGS'], 'w') as handle:
    handle.write(json.dumps(args))
if log:
    with open(log, 'a') as handle:
        handle.write('engine log line\n')
def emit(event):
    with open(progress, 'a') as handle:
        handle.write(json.dumps(event) + '\n')
if mode == 'wait-for-cancel':
    emit({'event': 'phase', 'phase': 'installing', 'label': 'Installing', 'critical': False})
    deadline = time.time() + 30
    while not os.path.exists(cancel) and time.time() < deadline:
        time.sleep(0.05)
    outcome = {'ok': False, 'code': 2, 'category': 'cancelled', 'message': None, 'result': None}
elif mode == 'wait-for-release':
    emit({'event': 'phase', 'phase': 'installing', 'label': 'Installing', 'critical': False})
    deadline = time.time() + 30
    while not os.path.exists(os.environ['FAKE_RELEASE']) and time.time() < deadline:
        time.sleep(0.05)
    outcome = json.loads(os.environ['FAKE_RESULT'])
elif mode == 'no-result':
    emit({'event': 'download', 'url': 'https://example.invalid/y.zip', 'received': 10, 'announced': 100})
    sys.exit(3)
else:
    for event in ({'event': 'phase', 'phase': 'preparing', 'label': 'Preparing', 'critical': False},
                  {'event': 'component', 'name': 'python', 'status': 'Installing python'},
                  {'event': 'downloaded', 'url': 'https://example.invalid/y.zip', 'bytes': 12345}):
        emit(event)
    outcome = json.loads(os.environ['FAKE_RESULT'])
with open(result, 'w') as handle:
    handle.write(json.dumps(outcome))
sys.exit(0 if outcome.get('ok') else 2)
'@

# A stand-in for the engine in app\Uninstall.ps1 tests: records what it sees, writes the result.
$script:UninstallStandIn = @'
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1] if flag in args else None
with open(os.environ['BMD_UNINSTALL_RECORD'], 'w') as handle:
    handle.write(json.dumps(sorted(os.listdir(here))) + '\n')
    handle.write(json.dumps(args) + '\n')
    handle.write(json.dumps(sorted(os.listdir(os.path.join(here, '..', 'helper')))) + '\n')
if value('--log-file'):
    with open(value('--log-file'), 'a') as handle:
        handle.write('uninstall engine log line\n')
with open(value('--result-file'), 'w') as handle:
    handle.write(os.environ['BMD_UNINSTALL_RESULT'])
sys.exit(0 if json.loads(os.environ['BMD_UNINSTALL_RESULT']).get('ok') else 2)
'@

# ==== Setup.ps1: the scripts ==================================================================

Check 'scripts-parse-ascii' {
    # Windows PowerShell 5.1 runs the setup; what only PowerShell 7 knows would stop it at once.
    $newOperators = @('QuestionQuestion', 'QuestionQuestionEquals', 'AmpersandAmpersand', 'PipePipe')
    $newCmdlets = @('Test-Json', 'Get-Error', 'Invoke-Parallel', 'Join-String')
    $newParameters = @{ 'ConvertFrom-Json' = @('AsHashtable', 'Depth'); 'ConvertTo-Json' = @('AsArray')
                        'Join-Path' = @('AdditionalChildPath'); 'Split-Path' = @('LeafBase')
                        'Test-Path' = @('NewerThan', 'OlderThan'); 'ForEach-Object' = @('Parallel') }
    foreach ($relative in 'Setup.ps1', 'app\Uninstall.ps1') {
        $path = Join-Path $Repo $relative
        $tokens = $null
        $problems = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$problems)
        Assert-Same @() @($problems | ForEach-Object { $_.Message }) "$relative parses"
        Assert-Same @() @($tokens | Where-Object { $newOperators -contains $_.Kind.ToString() } | ForEach-Object { $_.Text }) `
                    "${relative}: no operator that only PowerShell 7 knows"
        $nodes = @($ast.FindAll({ param($node)
            $node -is [System.Management.Automation.Language.TernaryExpressionAst] -or
            $node -is [System.Management.Automation.Language.PipelineChainAst] -or
            ($node -is [System.Management.Automation.Language.ForEachStatementAst] -and $node.Parallel) }, $true))
        Assert-Same @() @($nodes | ForEach-Object { $_.GetType().Name }) "${relative}: no syntax that only PowerShell 7 knows"
        foreach ($command in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            $first = $command.CommandElements[0]
            $name = if ($first -is [System.Management.Automation.Language.StringConstantExpressionAst]) { [string]$first.Value } else { '' }
            $name = ($name -split '[\\/.]')[-1]
            Assert (-not ($newCmdlets -contains $name)) "$relative uses the cmdlet $name, which Windows PowerShell 5.1 does not have"
            foreach ($element in $command.CommandElements) {
                if ($element -is [System.Management.Automation.Language.CommandParameterAst] -and $newParameters.ContainsKey($name)) {
                    Assert (-not ($newParameters[$name] -contains $element.ParameterName)) `
                            "$relative uses $name -$($element.ParameterName), which Windows PowerShell 5.1 does not have"
                }
            }
        }
        # 5.1 reads a file without byte-order mark as ANSI: one character outside ASCII would turn
        # into another one; with a mark in front the first line may not be read as a comment.
        $bytes = [System.IO.File]::ReadAllBytes($path)
        $outside = @()
        foreach ($byte in $bytes) { if ($byte -gt 127) { $outside += $byte } }
        Assert-Same @() $outside "${relative}: no character outside ASCII"
        Assert (-not ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) `
               "${relative}: no byte-order mark"
    }
}

Check 'exit-code-table' {
    $expected = @{ 'ok' = 0; 'failed' = 1; 'cancelled' = 2; 'damaged' = 3; 'download' = 4; 'downgrade' = 5
                   'busy' = 6; 'brave-missing' = 7; 'unsupported' = 8; 'not-installed' = 9 }
    foreach ($category in $expected.Keys) {
        Assert-Same $expected[$category] (Get-ExitCode @{ ok = ($category -eq 'ok'); category = $category }) "category $category"
    }
    Assert-Same 0 (Get-ExitCode @{ ok = $true; category = $null }) 'a success has no category'
    Assert-Same 1 (Get-ExitCode $null) 'no result file'
    Assert-Same 1 (Get-ExitCode @{ ok = $false; category = 'something-new' }) 'a category that is not in the table'
    Assert-Same 1 (Get-ExitCode @{ ok = $false; category = '' }) 'a failure without a category'
    Assert-Same 13 $script:ExitText.Count 'a text for every code the setup program can end with'
    Assert-Same 0 (@($script:ExitText.Keys | Where-Object { [int]$_ -lt 0 -or [int]$_ -gt 12 })).Count 'no code outside the table'
    foreach ($code in 0..12) { Assert (-not [string]::IsNullOrWhiteSpace((Get-ExitText $code))) "text for code $code" }
    Assert-Same (Get-ExitText 1) (Get-ExitText 99) 'an unknown code gets the failure text'
}

Check 'error-categories' {
    $problem = New-SetupError 'download' 'no connection'
    Assert-Same 'download' (Get-ErrorCategory $problem) 'a setup error'
    Assert-Same 'no connection' $problem.Message 'keeps its message'
    Assert-Same 'failed' (Get-ErrorCategory (New-Object System.ArgumentException 'x')) 'any other error'
    $wrapped = New-Object System.InvalidOperationException 'outer', $problem
    Assert-Same 'download' (Get-ErrorCategory $wrapped) 'through inner exceptions'
}

Check 'argument-line' {
    Assert-Same '"a b" "c"' (ConvertTo-ArgumentLine @('a b', 'c')) 'every argument in double quotes'
    Assert-Same '""' (ConvertTo-ArgumentLine @('')) 'an empty argument stays an argument'
    Assert-Same '"C:\x\y" "--flag=a b"' (ConvertTo-ArgumentLine @('C:\x\y', '--flag=a b')) 'spaces inside a value'
    Assert-Throws { ConvertTo-ArgumentLine @('say "hi"') } 'failed' '*quote*' 'a quote in an argument is refused, not escaped'
}

Check 'log-file' {
    $file = Join-Path (New-Folder 'log') 'Install.log'
    $savedLog = $script:LogFile
    $savedEcho = $script:Echo
    try {
        $script:LogFile = $file
        $script:Echo = $false
        $shown = (Write-Log 'first line' 6>&1 | Out-String)
        Assert-Same '' $shown.Trim() 'nothing is shown when the mode has no console'
        $script:Echo = $true
        $shown = (Write-Log 'second line' 6>&1 | Out-String)
        Assert-Same 'second line' $shown.Trim() 'Console mode echoes the line'
        $text = Get-Content -LiteralPath $file -Raw
        Assert ($text -match '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  first line') 'timestamp and text'
        Assert ($text -match '\d{2}:\d{2}:\d{2}  second line\s*$') 'appended, not replaced'
    } finally { $script:LogFile = $savedLog; $script:Echo = $savedEcho }
    $script:LogFile = Join-Path $scratch 'no-such-folder\Install.log'      # cannot be written: never throws
    Write-Log 'nothing happens'
    $script:LogFile = $savedLog
}

Check 'progress-cursor' {
    $file = Join-Path (New-Folder 'cursor') 'progress.jsonl'
    $cursor = New-LineCursor $file
    Assert-Same @() (Read-ProgressEvents $cursor) 'no file yet'
    [System.IO.File]::WriteAllText($file, '{"event":"one"}')
    Assert-Same @() (Read-ProgressEvents $cursor) 'a line without its newline is not ready'
    [System.IO.File]::AppendAllText($file, "`nnot json`n" + '{"event":"two"}' + "`r`n")
    $events = @(Read-ProgressEvents $cursor)
    Assert-Same 'one,two' (@($events | ForEach-Object { $_.event }) -join ',') 'two events; the line that is not JSON is skipped'
    Assert-Same 'two' $events[1].event 'parsed as objects'
    [System.IO.File]::AppendAllText($file, '{"event":"three"}' + "`n")
    Assert-Same 'three' ((Read-ProgressEvents $cursor).event) 'the next line, written later'
    [System.IO.File]::WriteAllText($file, '{"event":"new"}' + "`n")        # replaced by a shorter file
    Assert-Same 'new' ((Read-ProgressEvents $cursor).event) 'a replaced file is read from its start'
}

Check 'lock-file' {
    $lock = Read-JsonFile (Join-Path $Repo 'tools.lock.json')
    $entry = Get-LockComponent $lock 'python'
    Assert-Same 'python' $entry.name 'the python entry'
    Assert ([string]$entry.url -like 'https://www.python.org/ftp/python/*embed-amd64.zip') 'where Python comes from'
    # Since 2.5.0 the tree's lock is pinned: setup checks the exact file, whoever signed the download.
    foreach ($component in $lock.components) {
        Assert ([string]$component.sha256 -match '^[0-9a-f]{64}$') ('the lock pins ' + $component.name + ' to one SHA-256')
    }
    Assert-Throws { Get-LockComponent $lock 'no-such-tool' } 'damaged' '*no entry*' 'a missing entry'
    Assert-Throws { Get-LockComponent $null 'python' } 'damaged' '*missing or unreadable*' 'no lock file'
}

Check 'stale-work-folders' {
    $temp = New-Folder 'temp'
    foreach ($name in "BMD-Setup-$PID-1", 'BMD-Setup-99999999-1', 'BMD-Setup-99999998-7', 'BMD-Setup-99999999999999999999-1',
                     'BMD-Setup-2147483650-1', 'BMD-Install-old', 'BMD-Install-new', 'bmd-uninstall-abc-old',
                     'bmd-uninstall-abc-new', 'other') {
        $folder = Join-Path $temp $name
        New-Item -ItemType Directory -Force -Path $folder | Out-Null
        [System.IO.File]::WriteAllText((Join-Path $folder 'leftover.bin'), 'x')
        if ($name -like '*-old') { (Get-Item -LiteralPath $folder).LastWriteTime = (Get-Date).AddHours(-30) }
    }
    $keep = Join-Path $temp 'BMD-Setup-99999998-7'
    $removed = @(Remove-StaleWorkFolders -Temp $temp -Keep $keep | Sort-Object)
    Assert-Same 'BMD-Install-old, BMD-Setup-99999999-1, bmd-uninstall-abc-old' ($removed -join ', ') 'what was removed'
    foreach ($name in "BMD-Setup-$PID-1", 'BMD-Setup-99999998-7', 'BMD-Setup-99999999999999999999-1', 'BMD-Setup-2147483650-1',
                     'BMD-Install-new', 'bmd-uninstall-abc-new', 'other') {
        Assert (Test-Path -LiteralPath (Join-Path $temp $name)) "$name stays"
    }
    Assert-Same @() (Remove-StaleWorkFolders -Temp (Join-Path $temp 'not-there')) 'a missing temp folder is not an error'
}

Check 'brave-detection' {
    # The registry is looked at only when the folders have nothing: it differs per machine.
    Set-Item function:Get-RegisteredBrave { return $null }
    $home2 = New-Folder 'brave'
    $brave = Join-Parts $home2 @('BraveSoftware', 'Brave-Browser', 'Application', 'brave.exe')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $brave) | Out-Null
    [System.IO.File]::WriteAllText($brave, '')
    $nowhere = Join-Path $home2 'nowhere\brave.exe'
    Assert-Same $brave (Find-Brave -Candidates @($nowhere, $brave)) 'the second candidate exists'
    Assert ($null -eq (Find-Brave -Candidates @($nowhere))) 'no candidate exists, nothing in the registry'
    Assert ($null -eq (Find-Brave -Candidates @())) 'no candidates at all'
    $savedLocal = $env:LOCALAPPDATA
    $savedFiles = $env:ProgramFiles
    $savedX86 = ${env:ProgramFiles(x86)}
    try {
        $env:LOCALAPPDATA = $home2
        Remove-Item Env:\ProgramFiles, 'Env:\ProgramFiles(x86)' -ErrorAction SilentlyContinue
        Assert-Same $brave (Get-BraveCandidates | Select-Object -Last 1) 'the candidates are built from the environment'
        Assert-Same $brave (Find-Brave) 'and found without an argument'
    } finally { $env:LOCALAPPDATA = $savedLocal; $env:ProgramFiles = $savedFiles; ${env:ProgramFiles(x86)} = $savedX86 }
}

Check 'supported-windows' {
    Assert (Test-SupportedWindows -OsName 'Windows_NT' -Version ([version]'10.0.19045') -Is64Bit $true) 'Windows 10 x64'
    Assert (Test-SupportedWindows -OsName 'Windows_NT' -Version ([version]'10.0.22631') -Is64Bit $true) 'Windows 11 x64'
    Assert (-not (Test-SupportedWindows -OsName 'Windows_NT' -Version ([version]'6.3.9600') -Is64Bit $true)) 'Windows 8.1'
    Assert (-not (Test-SupportedWindows -OsName 'Windows_NT' -Version ([version]'6.1.7601') -Is64Bit $true)) 'Windows 7'
    Assert (-not (Test-SupportedWindows -OsName 'Windows_NT' -Version ([version]'10.0.19045') -Is64Bit $false)) '32-bit Windows 10'
    Assert (Test-SupportedWindows -OsName 'Linux' -Version ([version]'0.0') -Is64Bit $false) 'not Windows: nothing to check'
}

Check 'python-archive' {
    $folder = New-Folder 'python'
    $zip = New-PythonArchive (Join-Path $folder 'python.zip')
    $zipHash = Get-FileSha256 $zip
    $wrong = if ($zipHash -eq ('a' * 64)) { 'b' * 64 } else { 'a' * 64 }
    $engineRoot = Join-Path $folder 'engine'
    $signature = 'C=US, O=Python Software Foundation'
    $none = { param($event) }

    # pinned: the archive must be the one this release was built from
    Write-Lock $engineRoot $zipHash
    $context = New-SetupContextIn (Join-Path $folder 'w1') 'Install'
    $context.EngineRoot = $engineRoot
    $result = Initialize-Python -Setup $context -PythonZip $zip -AllowUnverified $false -Report $none -Pump $none `
                                -SignatureChecker (New-SignatureChecker 'NotSigned' '')
    Assert-Same 'sha256, bundled' ($result.Verified + ', ' + $result.Source) 'a pinned archive is verified by its hash, whatever the signature says'
    Assert (Test-Path -LiteralPath $result.Exe -PathType Leaf) 'python.exe is unpacked into the work folder'
    Assert-Same 'interpreter' (Get-Content -LiteralPath $result.Exe -Raw) 'from the archive'
    Assert-Same $zip $result.Zip 'the archive is handed on to the engine'

    Write-Lock $engineRoot $wrong
    $context = New-SetupContextIn (Join-Path $folder 'w2') 'Install'
    $context.EngineRoot = $engineRoot
    Assert-Throws { Initialize-Python -Setup $context -PythonZip $zip -AllowUnverified $false -Report $none -Pump $none } `
                   'damaged' '*not the version pinned*' 'another archive than the pinned one'

    # not pinned: Windows has to vouch for the signature of python.exe
    $cases = @(@('Valid', $signature, $false, 'authenticode'), @('NotSigned', '', $false, 'download'),
               @('Valid', 'C=US, O=Someone Else', $false, 'download'), @('NotSigned', '', $true, 'unverified'))
    $number = 0
    foreach ($case in $cases) {
        $number++
        Write-Lock $engineRoot ''
        $context = New-SetupContextIn (Join-Path $folder "w-s$number") 'Install'
        $context.EngineRoot = $engineRoot
        $outcome = ''
        $problem = $null
        try {
            $outcome = (Initialize-Python -Setup $context -PythonZip $zip -AllowUnverified $case[2] -Report $none -Pump $none `
                                           -SignatureChecker (New-SignatureChecker $case[0] $case[1])).Verified
        } catch { $problem = $_.Exception }
        if ($case[3] -eq 'download') {
            if (-not $problem) { throw "signature $($case[0])/$($case[1]) was accepted" }
            Assert-Same 'download' (Get-ErrorCategory $problem) "signature $($case[0])/$($case[1])"
        } else {
            if ($problem) { throw "signature $($case[0])/$($case[1]) was refused: $($problem.Message)" }
            Assert-Same $case[3] $outcome "signature $($case[0])/$($case[1])"
        }
    }

    # an archive without python.exe is not a Python
    $empty = New-PythonArchive (Join-Path $folder 'empty.zip') $false
    Write-Lock $engineRoot ''
    $context = New-SetupContextIn (Join-Path $folder 'w-empty') 'Install'
    $context.EngineRoot = $engineRoot
    Assert-Throws { Initialize-Python -Setup $context -PythonZip $empty -AllowUnverified $true -Report $none -Pump $none } `
                   'failed' '*python.exe is missing*' 'an archive without the interpreter'
}

Check 'python-installed-copy' {
    # Uninstall and Diagnose only need an interpreter: the installed one is copied, nothing is downloaded.
    $context = New-SetupContextIn (New-Folder 'python-installed') 'Uninstall'
    Write-Lock $context.EngineRoot ''
    $installed = Join-Parts $context.App @('python', 'python.exe')
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $installed) | Out-Null
    [System.IO.File]::WriteAllText($installed, 'installed')
    $result = Initialize-Python -Setup $context -PythonZip '' -AllowUnverified $false -Report { param($event) } -Pump { }
    Assert-Same 'installed' $result.Source 'the installed Python'
    Assert-Same '' $result.Zip 'no archive is needed'
    Assert-Same 'installed' (Get-Content -LiteralPath $result.Exe -Raw) 'copied into the work folder, not used in place'
    Assert-Same 'installed' (Get-Content -LiteralPath $installed -Raw) 'the installed file itself is untouched'
}

Check 'download-verified' {
    if (-not $url -or -not $urlHash) { Skip 'needs BMD_TEST_URL and BMD_TEST_SHA256' }
    $folder = New-Folder 'download'
    $destination = Join-Path $folder 'python.zip'
    $events = New-List
    $report = { param($event) [void]$events.Add($event) }
    Get-VerifiedDownload -Url $url -Destination $destination -Sha256 $urlHash -Report $report -Pump { }
    Assert (Test-Path -LiteralPath $destination -PathType Leaf) 'the file is there'
    Assert-Same $urlHash (Get-FileSha256 $destination) 'and it is the file that was expected'
    Assert-Same $urlHash (Get-FileSha256 $env:BMD_TEST_FILE) '...which is what the server has'
    Assert-Same 'component, downloaded' (@($events | ForEach-Object { $_.event }) -join ', ') 'the events a display gets'
    Assert-Same (Get-Item -LiteralPath $destination).Length $events[1].bytes 'the size was reported'
    # A file that is already there and correct is not downloaded again.
    $events.Clear()
    Get-VerifiedDownload -Url $url -Destination $destination -Sha256 $urlHash -Report $report -Pump { }
    Assert-Same 0 $events.Count 'no second download'
}

Check 'download-refused' {
    if (-not $url -or -not $urlHash) { Skip 'needs BMD_TEST_URL and BMD_TEST_SHA256' }
    $folder = New-Folder 'download-refused'
    $wrong = if ($urlHash -eq ('a' * 64)) { 'b' * 64 } else { 'a' * 64 }
    $destination = Join-Path $folder 'python.zip'
    $events = New-List
    $report = { param($event) [void]$events.Add($event) }
    Assert-Throws { Get-VerifiedDownload -Url $url -Destination $destination -Sha256 $wrong -Report $report -Pump { } } `
                   'download' '*Checksum mismatch*' 'a file with another hash'
    Assert (-not (Test-Path -LiteralPath $destination)) 'nothing is left in place'
    Assert (-not (Test-Path -LiteralPath ($destination + '.partial'))) 'and nothing half-downloaded'
    Assert-Same 'component' (@($events | ForEach-Object { $_.event }) -join ', ') 'a wrong file is not announced as downloaded'
    $started = Get-Date
    Assert-Throws { Get-VerifiedDownload -Url 'http://127.0.0.1:9/nothing.zip' -Destination (Join-Path $folder 'dead.zip') `
                                         -Sha256 '' -Report $report -Pump { } -Attempts 2 } `
                   'download' 'Download failed*' 'a server that does not answer'
    Assert (((Get-Date) - $started).TotalSeconds -lt 90) 'gives up in time'
}

Check 'shortcut-definitions' {
    $root = 'C:\Users\me\AppData\Local\BraveMusicDownloader'
    $powerShell = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
    $app = Join-Parts $root @('app')
    $definitions = @(Get-ShortcutDefinitions -Root $root -PowerShell $powerShell -Explorer 'C:\Windows\explorer.exe' -Icon 'C:\x\setup.ico')
    Assert-Same 'Diagnose Brave Music Downloader, Extension folder, Uninstall Brave Music Downloader' `
                (@($definitions | ForEach-Object { $_.Name } | Sort-Object) -join ', ') 'three entries'
    $diagnose = @($definitions | Where-Object { $_.Name -like 'Diagnose*' })[0]
    $uninstall = @($definitions | Where-Object { $_.Name -like 'Uninstall*' })[0]
    $folder = @($definitions | Where-Object { $_.Name -eq 'Extension folder' })[0]
    Assert-Same $powerShell $diagnose.Target 'the diagnosis entry runs Windows PowerShell'
    Assert-Same ('-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Parts $app @('Uninstall.ps1')) + '" -Diagnose') $diagnose.Arguments 'with the installed uninstaller'
    Assert-Same ('-NoLogo -NoProfile -ExecutionPolicy Bypass -File "' + (Join-Parts $app @('Uninstall.ps1')) + '"') $uninstall.Arguments 'the uninstall entry'
    Assert-Same 'C:\Windows\explorer.exe' $folder.Target 'the extension entry opens the folder'
    Assert-Same ('"' + (Join-Parts $app @('extension')) + '"') $folder.Arguments 'the installed extension folder'
    Assert-Same 'C:\x\setup.ico' $folder.Icon 'the icon, when there is one'
    $plain = @(Get-ShortcutDefinitions -Root $root -PowerShell $powerShell -Explorer 'C:\Windows\explorer.exe')
    Assert-Same @() @($plain | Where-Object { $_.Icon }) 'no icon key without an icon'
    $saved = $env:APPDATA
    try {
        Remove-Item Env:\APPDATA -ErrorAction SilentlyContinue
        Assert-Same '' (Get-StartMenuFolder) 'no Start menu folder without APPDATA'
        $env:APPDATA = 'C:\Users\me\AppData\Roaming'
        Assert-Same (Join-Parts 'C:\Users\me\AppData\Roaming' @('Microsoft', 'Windows', 'Start Menu', 'Programs', 'Brave Music Downloader')) `
                    (Get-StartMenuFolder) 'the folder the engine removes on uninstall'
    } finally { $env:APPDATA = $saved }
}

Check 'extension-steps' {
    $withClipboard = @(Get-ExtensionSteps -ExtensionFolder 'C:\Apps\extension' -OnClipboard $true)
    $without = @(Get-ExtensionSteps -ExtensionFolder 'C:\Apps\extension' -OnClipboard $false)
    Assert ($withClipboard -join "`n" -match 'brave://extensions') 'where to go'
    Assert ($withClipboard -join "`n" -match 'Developer mode') 'what to turn on'
    Assert ($withClipboard -join "`n" -match 'Load unpacked') 'what to click'
    Assert ($withClipboard -join "`n" -match [regex]::Escape('C:\Apps\extension')) 'the folder to select'
    Assert ($withClipboard -join "`n" -match 'lliokngbogdlnnjffdfgdjabpmjpobab') 'the extension ID'
    Assert ($withClipboard -join "`n" -match 'Run checks') 'the health check'
    Assert (-not (($without -join "`n") -match 'clipboard')) 'no clipboard promise when there is none'
    Assert (($withClipboard -join "`n") -match 'clipboard') 'a clipboard hint when the path was copied'
}

Check 'window-xaml' {
    [xml]$xaml = $script:WindowXaml
    $namespace = 'http://schemas.microsoft.com/winfx/2006/xaml'
    $manager = New-Object System.Xml.XmlNamespaceManager($xaml.NameTable)
    $manager.AddNamespace('x', $namespace)
    $named = @($xaml.SelectNodes("//*[@x:Name]", $manager) | ForEach-Object { $_.GetAttribute('Name', $namespace) } | Sort-Object -Unique)
    $source = Get-Content -LiteralPath (Join-Path $Repo 'Setup.ps1') -Raw
    $lookedUp = @([regex]::Matches(($source -split "`n" | Where-Object { $_ -match 'foreach \(\$name in ' }), "'([^']+)'") |
                  ForEach-Object { $_.Groups[1].Value })
    Assert-Same 8 $lookedUp.Count 'the code looks up eight controls'
    foreach ($name in $lookedUp) { Assert ($named -contains $name) "the window has a control named $name" }
    Assert-Same 'Heading' (@($named | Where-Object { $lookedUp -notcontains $_ }) -join ', ') 'every other named control is static text'
    Assert-Same 'Brave Music Downloader Setup' $xaml.Window.Title 'the title bar'
}

Check 'window-progress' {
    $controls = New-FakeControls
    $script:Critical = $false
    Show-ProgressEvent (New-ProgressEvent 'phase' @{ phase = 'installing'; label = 'Installing components' }) $controls
    Assert-Same 'Installing components' $controls.Status.Text 'the status line'
    Assert (-not $script:Critical) 'a normal phase does not block closing'
    Assert ($controls.Primary.IsEnabled) 'the cancel button stays available'
    Show-ProgressEvent (New-ProgressEvent 'download' @{ url = 'bundle:yt-dlp.zip'; received = 10MB; announced = 40MB }) $controls
    Assert-Same 25 $controls.Bar.Value 'the bar follows the download'
    Assert ($controls.Detail.Text -match 'Downloading yt-dlp\.zip  10\.0 MB of 40\.0 MB \(25 ?%\)') ('the detail line; was: ' + $controls.Detail.Text)
    Show-ProgressEvent (New-ProgressEvent 'download' @{ url = 'https://example.invalid/ffmpeg.zip'; received = 10MB }) $controls
    Assert ($controls.Bar.IsIndeterminate) 'without a size the bar keeps moving'
    Show-ProgressEvent (New-ProgressEvent 'component' @{ name = 'deno'; status = 'Installing deno' }) $controls
    Assert-Same 'Installing deno' $controls.Detail.Text 'a component'
    Show-ProgressEvent (New-ProgressEvent 'phase' @{ phase = 'swapping'; label = 'Swapping the installation'; critical = $true }) $controls
    Assert $script:Critical 'the swap phase is critical'
    Assert (-not $controls.Primary.IsEnabled) 'and cannot be cancelled'
    Show-ProgressEvent (New-ProgressEvent 'downloaded' @{ url = 'bundle:yt-dlp.zip'; bytes = 40MB }) $controls
    Assert-Same 100 $controls.Bar.Value 'a finished download fills the bar'
    Assert-Same 'Downloaded 40.0 MB' $controls.Detail.Text ''
    # An event as it comes back from the engine's JSON line behaves the same.
    $fromJson = '{"event":"download","url":"https://example.invalid/y.zip","received":52428800,"resume_from":0,"announced":104857600}' | ConvertFrom-Json
    Show-ProgressEvent $fromJson $controls
    Assert-Same 50 $controls.Bar.Value 'parsed events too'
    Assert-Same 'y.zip' (Get-DownloadName 'https://example.invalid/tools/y.zip?download=1') 'the name of a download'
    Assert-Same 'y.zip' (Get-DownloadName 'bundle:y.zip') 'also of one inside the installer'
}

Check 'window-close-and-button' {
    $controls = New-FakeControls
    $window = New-FakeWindow
    $script:Critical = $false; $script:Finished = $false; $script:CancelRequested = $false; $script:BrowserToOpen = ''
    $request = New-Object psobject -Property @{ Cancel = $false }
    Set-WindowCloseRequest $request
    Assert ($script:CancelRequested) 'closing while setup runs is a cancel'
    Assert (-not $request.Cancel) 'and the window may close'
    $script:CancelRequested = $false
    $script:Critical = $true
    $request = New-Object psobject -Property @{ Cancel = $false }
    Set-WindowCloseRequest $request
    Assert ($request.Cancel) 'closing during the swap is refused'
    Assert (-not $script:CancelRequested) 'and is not taken for a cancel'
    $script:Finished = $true
    $request = New-Object psobject -Property @{ Cancel = $false }
    Set-WindowCloseRequest $request
    Assert (-not $request.Cancel) 'when the work is done the window closes, whatever phase it ended in'
    $script:Critical = $false; $script:Finished = $false
    $controls.Primary.IsEnabled = $true
    Invoke-PrimaryClick -Window $window -Controls $controls
    Assert ($script:CancelRequested) 'the button cancels while setup runs'
    Assert (-not $controls.Primary.IsEnabled) 'and is disabled'
    Assert-Same 'Cancelling...' $controls.Primary.Content ''
    Assert-Same 0 $window.CloseCount 'the window stays until the engine has stopped'
    $script:Finished = $true
    Invoke-PrimaryClick -Window $window -Controls $controls
    Assert-Same 1 $window.CloseCount 'after the work the button closes the window'
    $script:BrowserToOpen = Join-Path $scratch 'no-brave-here\brave.exe'      # missing: starting it fails, the window still closes
    Invoke-PrimaryClick -Window $window -Controls $controls
    Assert-Same 2 $window.CloseCount ''
    $script:BrowserToOpen = ''
}

Check 'window-result' {
    $controls = New-FakeControls
    $script:FolderToCopy = ''; $script:BrowserToOpen = ''
    $setup = @{ Outcome = @{ Kind = 'Installed'; Lines = @('Brave Music Downloader 2.5.0 is installed.', 'Finish in Brave:') }
                ExtensionFolder = 'C:\Apps\extension'; Browser = 'C:\Brave\brave.exe' }
    Set-ResultControls -Setup $setup -Controls $controls
    Assert-Same 'Collapsed' $controls.ProgressPanel.Visibility ''
    Assert-Same 'Visible' $controls.Details.Visibility ''
    Assert-Same "Brave Music Downloader 2.5.0 is installed.`r`nFinish in Brave:" $controls.Details.Text ''
    Assert-Same 'Visible' $controls.Secondary.Visibility 'the copy button appears after an install'
    Assert-Same 'C:\Apps\extension' $script:FolderToCopy ''
    Assert-Same 'Open Brave and close' $controls.Primary.Content ''
    Assert ($controls.Primary.IsDefault) 'the button answers to Enter'
    Assert ($controls.Primary.IsEnabled) 'even if a phase had disabled it'
    $setup.Outcome = @{ Kind = 'Failed'; Lines = @('Setup did not complete.', 'Details: no connection') }
    $setup.Browser = $null
    $controls.Secondary.Visibility = 'Collapsed'
    Set-ResultControls -Setup $setup -Controls $controls
    Assert-Same 'Collapsed' $controls.Secondary.Visibility 'no copy button after a failure'
    Assert-Same 'Close' $controls.Primary.Content ''
    Assert-Same "Setup did not complete.`r`nDetails: no connection" $controls.Details.Text 'the reason is in the window'
    $script:FolderToCopy = ''; $script:BrowserToOpen = ''
}

Check 'console-progress' {
    $shown = (Write-ProgressEvent (New-ProgressEvent 'phase' @{ label = 'Installing components' }) 6>&1 | Out-String)
    Assert-Same '== Installing components ==' $shown.Trim() ''
    # Write-Progress with a parameter that does not exist would throw here and switch the display off.
    $shown = (Write-ProgressEvent (New-ProgressEvent 'download' @{ url = 'bundle:yt-dlp.zip'; received = 10MB; announced = 40MB }) 6>&1 | Out-String)
    Assert-Same '' $shown.Trim() 'a download writes no line, it moves the bar'
    $shown = (Write-ProgressEvent (New-ProgressEvent 'downloaded' @{ url = 'bundle:yt-dlp.zip'; bytes = 13107200 }) 6>&1 | Out-String)
    Assert-Same 'Downloaded yt-dlp.zip (12.5 MB)' $shown.Trim() 'the line after a download'
}

# ==== Setup.ps1: running the engine ==========================================================

Check 'engine-run-ok' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $folder = New-Folder 'engine-ok'
    $fake = Join-Path $folder 'fake_engine.py'
    [System.IO.File]::WriteAllText($fake, $script:FakeEngine)
    $context = New-SetupContextIn $folder 'Install'
    $context.Python = @{ Exe = $python }
    $env:FAKE_ENGINE = 'ok'
    $env:FAKE_ARGS = Join-Path $folder 'args.json'
    $env:FAKE_RESULT = '{"ok": true, "code": 0, "category": null, "message": null, "result": {"fresh_install": true}}'
    try {
        $events = New-List
        $report = { param($event) [void]$events.Add($event) }
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report $report -Pump { }
        Assert-Same 0 $run.Code 'exit code'
        Assert ($run.Result.ok) 'the result file was read'
        Assert-Same 'phase, component, downloaded' (@($events | ForEach-Object { $_.event }) -join ', ') 'every event was reported'
        Assert-Same 'Installing python' $events[1].status 'with its fields'
        $arguments = Read-JsonFile $env:FAKE_ARGS
        Assert-Same 'install, --root, C:\Apps' (@($arguments[0..2]) -join ', ') 'the command'
        Assert-Same $folder (Split-Path -Parent ($arguments[$arguments.IndexOf('--result-file') + 1])) 'the result file is in the work folder'
        Assert ($arguments -contains '--cancel-file') 'and the cancel file too'
        Assert (-not ($arguments -contains '--log-file')) 'no log file unless there is a log'
        $script:LogFile = Join-Path $folder 'Setup.log'
        Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report $report -Pump { } | Out-Null
        $arguments = Read-JsonFile $env:FAKE_ARGS
        Assert-Same $script:LogFile ($arguments[$arguments.IndexOf('--log-file') + 1]) 'the engine writes into the setup log itself'
        Assert ((Get-Content -LiteralPath $script:LogFile) -contains 'engine log line') 'which it did'
        Assert (@(Get-Content -LiteralPath $script:LogFile | Where-Object { $_ -match 'Result: ok=True' }).Count -eq 1) 'and setup logged the result'
    } finally { $script:LogFile = ''; Remove-Item Env:\FAKE_ENGINE, Env:\FAKE_ARGS, Env:\FAKE_RESULT -ErrorAction SilentlyContinue }
}

Check 'engine-run-categories' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $folder = New-Folder 'engine-categories'
    $fake = Join-Path $folder 'fake_engine.py'
    [System.IO.File]::WriteAllText($fake, $script:FakeEngine)
    $context = New-SetupContextIn $folder 'Install'
    $context.Python = @{ Exe = $python }
    $env:FAKE_ARGS = Join-Path $folder 'args.json'
    $none = { param($event) }
    try {
        foreach ($case in @(@('{"ok": false, "code": 2, "category": "download", "message": "Checksum mismatch for ffmpeg", "result": null}', 4, 2, 'Checksum mismatch for ffmpeg'),
                            @('{"ok": false, "code": 2, "category": "downgrade", "message": "9.9.9 is already installed", "result": null}', 5, 2, '9.9.9 is already installed'),
                            @('{"ok": false, "code": 2, "category": "busy", "message": "already running", "result": null}', 6, 2, 'already running'))) {
            $env:FAKE_ENGINE = 'ok'
            $env:FAKE_RESULT = $case[0]
            $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report $none -Pump { }
            Assert-Same $case[1] $run.Code ('the setup exit code for ' + $case[0])
            Assert-Same $case[2] $run.Exit 'the engine exit code'
            Assert-Same $case[3] $run.Message 'the message for the user'
        }
        # A crashing engine (no result file) is a failed setup, whatever its exit code says.
        $env:FAKE_ENGINE = 'no-result'
        $events = New-List
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') `
                                -Report { param($event) [void]$events.Add($event) } -Pump { }
        Assert-Same 1 $run.Code 'a crash is a failed setup'
        Assert-Same 3 $run.Exit 'the exit code is kept for the log'
        Assert ($run.Message -like '*exit code 3*') 'and named in the message'
        Assert-Same 'download' (@($events | ForEach-Object { $_.event }) -join ', ') 'what was reported before the crash was still reported'
    } finally { Remove-Item Env:\FAKE_ENGINE, Env:\FAKE_ARGS, Env:\FAKE_RESULT -ErrorAction SilentlyContinue }
}

Check 'engine-run-cancel' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $folder = New-Folder 'engine-cancel'
    $fake = Join-Path $folder 'fake_engine.py'
    [System.IO.File]::WriteAllText($fake, $script:FakeEngine)
    $context = New-SetupContextIn $folder 'Install'
    $context.Python = @{ Exe = $python }
    $env:FAKE_ENGINE = 'wait-for-cancel'
    $env:FAKE_ARGS = Join-Path $folder 'args.json'
    $events = New-List
    $pumped = New-List
    $script:CancelRequested = $false
    # The window asks for a cancel while the engine is running: the pump stands in for the user.
    $pump = { [void]$pumped.Add(1); if ($pumped.Count -eq 3) { $script:CancelRequested = $true } }
    try {
        $started = Get-Date
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') `
                                -Report { param($event) [void]$events.Add($event) } -Pump $pump
        Assert-Same 2 $run.Code 'a cancel ends with exit code 2'
        Assert-Same 'cancelled' $run.Result.category 'as the engine reports it'
        Assert-Same 'phase' (@($events | ForEach-Object { $_.event }) -join ', ') 'the events before the cancel were shown'
        Assert (((Get-Date) - $started).TotalSeconds -lt 20) 'the engine is not left waiting'
        # Without a request no cancel file appears.
        $script:CancelRequested = $false
        $env:FAKE_ENGINE = 'ok'
        $env:FAKE_RESULT = '{"ok": true, "code": 0, "category": null, "message": null, "result": {}}'
        $context.Work = New-Folder 'engine-no-cancel'
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report { param($event) } -Pump { }
        Assert-Same 0 $run.Code 'no cancel requested'
        Assert (-not (Test-Path -LiteralPath (Join-Path $context.Work 'cancel'))) 'no cancel file is created'
    } finally { $script:CancelRequested = $false; Remove-Item Env:FAKE_ENGINE, Env:FAKE_ARGS, Env:FAKE_RESULT -ErrorAction SilentlyContinue }
}

Check 'engine-run-display' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $folder = New-Folder 'engine-display'
    $fake = Join-Path $folder 'fake_engine.py'
    [System.IO.File]::WriteAllText($fake, $script:FakeEngine)
    $context = New-SetupContextIn $folder 'Install'
    $context.Python = @{ Exe = $python }
    $env:FAKE_ARGS = Join-Path $folder 'args.json'
    $env:FAKE_RESULT = '{"ok": true, "code": 0, "category": null, "message": null, "result": {}}'
    $env:FAKE_RELEASE = Join-Path $folder 'release'
    $gone = { param($event) throw 'the display is gone' }
    $start = ${function:Start-Engine}
    try {
        # The display fails while the engine is running (it waits for the file the pump creates):
        # the display is switched off, the engine still runs to its end and its result stands.
        $env:FAKE_ENGINE = 'wait-for-release'
        $pumped = New-List
        $pump = { [void]$pumped.Add(1); if ($pumped.Count -eq 3) { New-Item -ItemType File -Path $env:FAKE_RELEASE -Force | Out-Null } }
        $started = Get-Date
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report $gone -Pump $pump
        Assert-Same 0 $run.Code 'a failing display does not change the result'
        Assert (((Get-Date) - $started).TotalSeconds -lt 20) 'the engine was released, not left to time out'
        # An engine that ended before the first poll leaves everything for the read after the wait.
        $env:FAKE_ENGINE = 'ok'
        Set-Item function:Start-Engine {
            param($Python, $Arguments, $Folder)
            $process = & $start -Python $Python -Arguments $Arguments -Folder $Folder
            [void]$process.WaitForExit()
            return $process
        }
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') -Report $gone -Pump { }
        Assert-Same 0 $run.Code 'the display may fail there too: the result is still the engine''s'
        $shown = New-List
        $run = Invoke-EngineRun -Setup $context -Arguments @($fake, 'install', '--root', 'C:\Apps') `
                                -Report { param($event) [void]$shown.Add($event.event) } -Pump { }
        Assert-Same 0 $run.Code 'the run is a success'
        Assert-Same 'phase, component, downloaded' (@($shown) -join ', ') 'a working display is given everything after the wait'
    } finally {
        Set-Item function:Start-Engine $start
        Remove-Item Env:FAKE_ENGINE, Env:FAKE_ARGS, Env:FAKE_RESULT, Env:FAKE_RELEASE -ErrorAction SilentlyContinue
    }
}

Check 'engine-run-real' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $folder = New-Folder 'engine-real'
    $context = New-SetupContextIn $folder 'Install'
    $context.Python = @{ Exe = $python }
    $core = Join-Parts $Repo @('app', 'installer', 'installer_core.py')
    $root = Join-Path $folder 'BraveMusicDownloader'
    $log = Join-Path $folder 'Setup.log'
    $script:LogFile = $log
    try {
        $run = Invoke-EngineRun -Setup $context -Arguments @($core, 'diagnose', '--root', $root, '--data-dir', (Join-Path $folder 'data'),
                                                                '--registry', 'fake') -Report { param($event) } -Pump { }
        Assert-Same 1 $run.Code 'nothing is installed: the diagnosis ends with exit code 1'
        Assert ($run.Result.result.checks.Count -gt 5) 'the report lists the checks'
        Assert-Same ('Diagnosing Brave Music Downloader 2.5.0 (' + $root + ')') `
                    (@(Get-Content -LiteralPath $log | Where-Object { $_ -like 'Diagnosing*' })[0]) 'the engine wrote into the setup log'
        Assert (@(Get-Content -LiteralPath $log | Where-Object { $_ -like 'FAIL installation record*' }).Count -eq 1) 'with its findings'
        $run = Invoke-EngineRun -Setup $context -Arguments @($core, 'uninstall', '--root', $root, '--data-dir', (Join-Path $folder 'data'),
                                                                '--registry', 'fake') -Report { param($event) } -Pump { }
        Assert-Same 0 $run.Code 'the uninstall of nothing succeeds'
        Assert-Same 0 $run.Exit ''
    } finally { $script:LogFile = '' }
}

# ==== Setup.ps1: the worker ===================================================================

Check 'work-install' {
    $folder = New-Folder 'work-install'
    $calls = New-List
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'installed')
    Set-Item function:Initialize-Python { return @{ Exe = 'python.exe'; Zip = 'C:\Temp\BMD-Setup-1-2\python'; Source = 'bundled'; Verified = 'sha256' } }
    $context = New-SetupContextIn $folder 'Install'
    $events = New-List
    $code = Invoke-SetupWork -Setup $context -Report { param($event) [void]$events.Add($event) } -Pump { }
    Assert-Same 0 $code 'exit code'
    Assert-Same 'Installed' $context.Outcome.Kind 'outcome'
    Assert-Same 1 $calls.Count 'one engine run'
    $expected = @($context.Core, 'install', '--root', $context.Root, '--payload', $context.PayloadZip, '--extension-id',
                  'lliokngbogdlnnjffdfgdjabpmjpobab', '--smoke', '--build-launcher', '--check-host',
                  '--python-zip', 'C:\Temp\BMD-Setup-1-2\python', '--python-verified', 'sha256')
    Assert-Same ($expected -join ' ') ($calls[0] -join ' ') 'the engine is told everything it needs, and nothing more'
    Assert-Same 'phase' (@($events | ForEach-Object { $_.event }) -join ', ') 'the first event is the preparing phase'
    Assert (($context.Outcome.Lines -join "`n") -match 'Brave Music Downloader 2\.5\.0 is installed') 'the result line'
    Assert (($context.Outcome.Lines -join "`n") -match 'Kept unchanged components: python, deno') 'what was reused'
    Assert (($context.Outcome.Lines -join "`n") -match 'brave://extensions') 'how to finish in Brave'

    # the switches, and the offline installer
    $context = New-SetupContextIn (New-Folder 'work-install-2') 'Install'
    $context.Bundle = $true; $context.Installer = 'C:\Downloads\Brave Music Downloader Setup.exe'
    $context.AllowDowngrade = $true; $context.AllowUnverified = $true
    Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { } | Out-Null
    $line = $calls[1] -join ' '
    Assert ($line -like ('*--bundle C:\Downloads\Brave Music Downloader Setup.exe*')) 'the bundle switch names the installer file'
    Assert ($line -like '*--allow-unverified --allow-downgrade*') 'the two permissions'
    Assert (-not ($line -match 'Silent|Console|Gui')) 'the mode is not the engine''s business'
}

Check 'work-install-update-and-failure' {
    $calls = New-List
    Set-Item function:Initialize-Python { return @{ Exe = 'python.exe'; Zip = 'z'; Source = 'download'; Verified = 'authenticode' } }
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'updated')
    $context = New-SetupContextIn (New-Folder 'work-update') 'Repair'
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 0 $code 'a repair succeeds'
    Assert-Same 'install' $calls[0][1] 'a repair is an install over the existing files'
    Assert (($context.Outcome.Lines -join "`n") -match 'Updated 2\.4\.0 -> 2\.5\.0') 'the update line'
    Assert (($context.Outcome.Lines -join "`n") -match 'Repaired components: ffmpeg') 'what was repaired'
    Assert (($calls[0] -join ' ') -like '*--python-verified authenticode*') 'how Python was verified'

    foreach ($case in @(@(4, 'A download could not be completed or verified'), (6, 'Another setup is already running'), (5, 'A newer version is already installed'))) {
        $calls.Clear()
        Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'installed' $case[0])
        $context = New-SetupContextIn (New-Folder "work-fail-$($case[0])") 'Install'
        $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
        Assert-Same $case[0] $code 'the engine''s category is the exit code'
        Assert-Same 'Failed' $context.Outcome.Kind 'outcome'
        Assert (($context.Outcome.Lines[0] -like ($case[1] + '*'))) "the text for exit code $($case[0]) was: $($context.Outcome.Lines[0])"
        Assert (($context.Outcome.Lines -join "`n") -match 'Details: the engine said why') 'the engine''s message is shown too'
    }
    # A failure before the engine (here: Python cannot be verified) has the same shape.
    $calls.Clear()
    Set-Item function:Initialize-Python { throw (New-SetupError 'download' 'Download failed (no connection).') }
    $context = New-SetupContextIn (New-Folder 'work-fail-python') 'Install'
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 4 $code 'no Python, no install'
    Assert-Same 0 $calls.Count 'the engine was never started'
    Assert (($context.Outcome.Lines -join "`n") -match 'Details: Download failed') 'the reason is shown'
}

Check 'work-install-shortcuts' {
    $calls = New-List
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'installed')
    Set-Item function:Initialize-Python { return @{ Exe = 'python.exe'; Zip = 'z'; Source = 'bundled'; Verified = 'sha256' } }
    $created = New-List
    Set-Item function:New-Shortcuts { param($Folder, $Definitions) [void]$created.Add(@($Folder, @($Definitions | ForEach-Object { $_.Name }))) }
    $saved = $env:APPDATA
    try {
        $env:APPDATA = Join-Path $scratch 'work-install-roaming'
        $folder = New-Folder 'work-shortcuts'
        $context = New-SetupContextIn $folder 'Install'
        Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { } | Out-Null
        Assert-Same 1 $created.Count 'the Start menu entries are created after a successful install'
        Assert-Same (Get-StartMenuFolder) $created[0][0] 'in the folder the engine knows'
        Assert-Same 'Extension folder, Diagnose Brave Music Downloader, Uninstall Brave Music Downloader' ($created[0][1] -join ', ') 'all three'
        $context = New-SetupContextIn (New-Folder 'work-no-shortcuts') 'Install'
        $context.NoShortcuts = $true
        Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { } | Out-Null
        Assert-Same 1 $created.Count 'not with -NoShortcuts'
        # When the folder cannot be written the installation still counts as done.
        Set-Item function:New-Shortcuts { param($Folder, $Definitions) throw (New-Object System.UnauthorizedAccessException 'denied') }
        $context = New-SetupContextIn (New-Folder 'work-shortcuts-denied') 'Install'
        $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
        Assert-Same 0 $code 'a denied Start menu folder does not fail the install'
        Assert-Same 'Installed' $context.Outcome.Kind ''
    } finally { $env:APPDATA = $saved }
}

Check 'work-uninstall' {
    $calls = New-List
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'removed')
    $pythonCalls = New-List
    Set-Item function:Initialize-Python { param($Setup) [void]$pythonCalls.Add(1); return @{ Exe = 'python.exe'; Zip = ''; Source = 'installed'; Verified = 'sha256' } }
    $context = New-SetupContextIn (New-Folder 'work-uninstall-nothing') 'Uninstall'
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 9 $code 'nothing is installed: exit code 9'
    Assert-Same 0 $calls.Count 'and the engine is not started'
    Assert-Same 0 $pythonCalls.Count 'Python is not even looked for'
    Assert-Same 'Brave Music Downloader is not installed, so there is nothing to remove.' ($context.Outcome.Lines -join ' ') 'the text'
    New-Item -ItemType Directory -Force -Path $context.App | Out-Null                  # a leftover folder is "installed"
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 0 $code 'the engine decides over a leftover folder'
    Assert-Same 1 $calls.Count ''
    Assert-Same ($context.Core + ' uninstall --root ' + $context.Root) ($calls[0] -join ' ') 'the engine is told to uninstall, nothing else'
    Assert (($context.Outcome.Lines -join "`n") -match 'music, settings and download history were kept') 'what is kept'
    $context = New-SetupContextIn (New-Folder 'work-uninstall-history') 'Uninstall'
    New-Item -ItemType Directory -Force -Path $context.App | Out-Null                  # a leftover folder is "installed"
    $context.RemoveHistory = $true
    Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { } | Out-Null
    $history = @($calls[1])
    Assert-Same '--remove-history --confirm DELETE HISTORY' (($history[($history.Count - 3)..($history.Count - 1)]) -join ' ') 'the typed phrase is passed on'
    Assert (($context.Outcome.Lines -join "`n") -match 'history was removed') 'the text'
    $calls.Clear()
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'removed' 6)
    $context = New-SetupContextIn (New-Folder 'work-uninstall-busy') 'Uninstall'
    New-Item -ItemType Directory -Force -Path $context.App | Out-Null
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 6 $code 'another setup is running'
}

Check 'work-diagnose' {
    $calls = New-List
    Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'installed')
    Set-Item function:Initialize-Python { return @{ Exe = 'python.exe'; Zip = ''; Source = 'installed'; Verified = 'sha256' } }
    $folder = New-Folder 'work-diagnose'
    $context = New-SetupContextIn $folder 'Diagnose'
    $context.Mode = 'Gui'
    $context.Browser = 'C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe'
    $script:LogFile = Join-Path $folder 'Setup.log'
    $code = Invoke-SetupWork -Setup $context -Report { param($event) } -Pump { }
    Assert-Same 0 $code ('exit code; outcome lines: ' + (@($context.Outcome.Lines) -join ' | ') + '; log: ' +
                         (@(Get-Content -LiteralPath $script:LogFile -ErrorAction SilentlyContinue) -join ' | '))
    Assert-Same 'Diagnosed' $context.Outcome.Kind 'outcome'
    Assert-Same 1 $calls.Count 'one engine run'
    $line = $calls[0] -join ' '
    Assert ($line -like ('*' + $context.Core + ' diagnose --root ' + $context.Root + ' --extra-json *')) 'the command'
    $report = Join-Path $context.Root 'diagnosis.txt'
    Assert ($line -like ('*--json-report *diagnosis.json --log-file ' + $report)) 'the engine writes the text report itself'
    $header = @(Get-Content -LiteralPath $report)
    Assert ($header[0] -like 'Brave Music Downloader 2.5.0 - diagnosis report, ????-??-?? ??:??:??') ('what setup knows goes in front; was: ' + $header[0])
    Assert-Same 'Setup mode: Gui; Brave: C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe' $header[1] ''
    $factsFile = $calls[0][$calls[0].IndexOf('--extra-json') + 1]
    $facts = Read-JsonFile $factsFile
    Assert-Same 'Gui, C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe' ($facts.mode + ', ' + $facts.path) 'and into the JSON report'
    $bytes = [System.IO.File]::ReadAllBytes($factsFile)
    Assert (-not ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) 'without a byte-order mark, which the engine''s reader would not forgive'
    Assert (($context.Outcome.Lines -join "`n") -match [regex]::Escape($report)) 'the user is told where the report is'
}

Check 'work-logs-outcome' {
    $log = Join-Path (New-Folder 'work-log') 'Install.log'
    $script:LogFile = $log
    try {
        $calls = New-List
        Set-Item function:Invoke-EngineRun (New-RecordingEngine $calls 'removed')
        Invoke-SetupWork -Setup (New-SetupContextIn (New-Folder 'work-log') 'Uninstall') -Report { param($event) } -Pump { } | Out-Null
        $text = Get-Content -LiteralPath $log -Raw
        Assert ($text -match 'Nothing is installed; nothing to remove') 'the reason for exit code 9 is in the log'
        Assert ($text -match 'Brave: not found') 'and what was found out about the browser'
    } finally { $script:LogFile = '' }
}

Check 'work-cleanup' {
    # What the script filled during the run is emptied at the end, so the setup program's
    # RemoveDirectory succeeds (found on a real Windows PC: the work folder survived).
    $folder = New-Folder 'work-cleanup'
    foreach ($sub in 'engine\installer', 'python') {
        New-Item -ItemType Directory -Force -Path (Join-Path $folder $sub) | Out-Null
    }
    foreach ($file in 'engine\installer\installer_core.py', 'python\python.exe', 'python-download.zip',
                     'progress.jsonl', 'result.json', 'cancel') {
        [System.IO.File]::WriteAllText((Join-Path $folder $file), 'x')
    }
    [System.IO.File]::WriteAllText((Join-Path $folder 'Setup.ps1'), 'kept')
    [System.IO.File]::WriteAllText((Join-Path $folder 'Application.zip'), 'kept')
    Remove-SetupWork -Work $folder
    $left = @(Get-ChildItem -LiteralPath $folder -Force | ForEach-Object { $_.Name } | Sort-Object)
    Assert-Same 'Application.zip, Setup.ps1' ($left -join ', ') 'only the two bootstrap files are left for the setup program'
    Assert-Same 'kept' (Get-Content -LiteralPath (Join-Path $folder 'Setup.ps1') -Raw) 'the running script itself is untouched'
    Remove-SetupWork -Work ''                                             # no folder: no error
    Remove-SetupWork -Work (Join-Path $folder 'gone')                     # a missing folder: no error
}

Check 'install-opens-help-windows' {
    # A finished install opens the extension folder (no folder navigation for Load unpacked)
    # and, when Brave was found, its extension page. Found while walking the new-user flow.
    $calls = New-List
    $original = ${function:Start-Process}
    try {
        Set-Item function:Start-Process { param($FilePath, $ArgumentList) [void]$calls.Add(($FilePath + ' ' + $ArgumentList)) }
        Open-InstallHelp -Browser 'C:\Brave\brave.exe' -Folder 'C:\app\extension'
        Assert-Same 'explorer.exe "C:\app\extension"' $calls[0] 'the extension folder opens for Load unpacked'
        Assert-Same 'C:\Brave\brave.exe brave://extensions' $calls[1] 'Brave opens at its extension page'
        $calls.Clear()
        Open-InstallHelp -Browser '' -Folder 'C:\app\extension'
        Assert-Same 1 $calls.Count 'without Brave only the folder opens'
        $calls.Clear()
        Open-InstallHelp -Browser '' -Folder ''
        Assert-Same 0 $calls.Count 'nothing to open: nothing is started'
        Set-Item function:Start-Process { param($FilePath, $ArgumentList) throw 'explorer is busy' }
        Open-InstallHelp -Browser 'C:\Brave\brave.exe' -Folder 'C:\app\extension'
        Assert $true 'a window that cannot open is swallowed, not fatal'
    } finally {
        Set-Item function:Start-Process $original
    }
}

# ==== Setup.ps1: the entry point in a child process ==========================================

Check 'entry-uninstall-nothing' {
    if (-not $payload) { Skip 'needs BMD_TEST_PAYLOAD' }
    $work = New-WorkFolder 'entry-uninstall'
    $environment = New-ChildEnvironment 'entry-uninstall'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Console', '-Action', 'Uninstall') -Environment $environment
    Assert-Same 9 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert ($run.Out -like '*nothing to remove*') 'the console says so'
    $text = Get-Content -LiteralPath (Join-Parts $environment['LOCALAPPDATA'] @('BraveMusicDownloader', 'Install.log')) -Raw
    Assert ($text -match 'Brave Music Downloader v2\.5\.0 - setup \(Console mode, Uninstall\)') 'the log names version, mode and action'
    Assert ($text -match 'Nothing is installed') 'and the reason'
    Assert ($text -match 'Exit code 9') 'and the exit code'
    # Silent: the same decision, without a word.
    $work = New-WorkFolder 'entry-uninstall-silent'
    $environment = New-ChildEnvironment 'entry-uninstall-silent'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Silent', '-Action', 'Uninstall') -Environment $environment
    Assert-Same 9 $run.Code "silent exit code; error: $($run.Error)"
    Assert-Same '' $run.Out.Trim() 'silent says nothing'
}

Check 'entry-require-brave' {
    if (-not $payload) { Skip 'needs BMD_TEST_PAYLOAD' }
    if ($onWindows) { Skip 'a Brave on this machine may be found through the registry' }
    $work = New-WorkFolder 'entry-require-brave'
    $environment = New-ChildEnvironment 'entry-require-brave'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Console', '-Action', 'Install', '-RequireBrave') -Environment $environment
    Assert-Same 7 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert ($run.Out -like '*Brave is not installed*') 'the reason'
    $root = Join-Path $environment['LOCALAPPDATA'] 'BraveMusicDownloader'
    Assert-Same 'Install.log' (@(Get-ChildItem -LiteralPath $root | ForEach-Object { $_.Name }) -join ', ') 'nothing was installed'
}

Check 'entry-damaged-archive' {
    if (-not $payload) { Skip 'needs BMD_TEST_PAYLOAD' }
    $staging = New-Folder 'damaged-payload'
    New-Item -ItemType Directory -Force -Path (Join-Path $staging 'installer') | Out-Null
    Copy-Item -LiteralPath (Join-Path $Repo 'app\installer\installer_core.py') -Destination (Join-Path $staging 'installer')
    Copy-Item -LiteralPath (Join-Path $Repo 'tools.lock.json') -Destination $staging
    $zip = Join-Path (New-Folder 'damaged-payload-zip') 'Application.zip'
    [void](New-ZipFromFolder $staging $zip)
    $work = New-WorkFolder 'entry-damaged' $zip
    $environment = New-ChildEnvironment 'entry-damaged'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Console', '-Action', 'Install') -Environment $environment
    Assert-Same 3 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert ($run.Out -like '*incomplete*payload_container.py is missing*') 'what is missing'
    $work = New-WorkFolder 'entry-no-archive' ''
    $environment = New-ChildEnvironment 'entry-no-archive'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Console', '-Action', 'Install') -Environment $environment
    Assert-Same 3 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert ($run.Out -like '*application archive is missing*') 'the reason'
}

Check 'entry-unknown-mode' {
    if (-not $payload) { Skip 'needs BMD_TEST_PAYLOAD' }
    # The setup program only passes Gui, Console or Silent; a caller who does must not get a window.
    $work = New-WorkFolder 'entry-unknown'
    $environment = New-ChildEnvironment 'entry-unknown'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Quiet', '-Action', 'Uninstall') -Environment $environment
    Assert-Same 1 $run.Code 'a value outside the set is refused by PowerShell itself'
    Assert ($run.Error -like '*"Quiet"*') 'with a message that names the value'
    Assert ($run.Error -like '*ValidateSet*') 'and the rule'
}

Check 'entry-no-window' {
    if ($onWindows) { Skip 'on Windows the window opens and waits for the user: WINDOWS_QA.md' }
    if (-not $payload) { Skip 'needs BMD_TEST_PAYLOAD' }
    # Where the window cannot be shown setup must say so and fail, not hang or install unseen.
    $work = New-WorkFolder 'entry-gui'
    $environment = New-ChildEnvironment 'entry-gui'
    $run = Invoke-Script -Script (Join-Path $work 'Setup.ps1') -Arguments @('-Mode', 'Gui', '-Action', 'Uninstall') -Environment $environment
    Assert-Same 1 $run.Code "exit code; error: $($run.Error)"
    $root = Join-Parts $environment['LOCALAPPDATA'] @('BraveMusicDownloader')
    $text = Get-Content -LiteralPath (Join-Path $root 'Install.log') -Raw
    Assert ($text -match 'The setup window could not be created') 'the log says why'
    Assert-Same 'Install.log' (@(Get-ChildItem -LiteralPath $root | ForEach-Object { $_.Name }) -join ', ') 'nothing was installed'
}

# ==== app\Uninstall.ps1 =======================================================================
# (This script has tables and a Quote of its own with the same names: everything of Setup.ps1
# is tested above; from here on its Get-ErrorCategory is the only function still used.)

. (Join-Path $Repo 'app\Uninstall.ps1')

Check 'uninstall-exit-codes' {
    Assert-Same 0 (Get-UninstallExitCode @{ ok = $true; category = $null }) 'removed'
    foreach ($pair in @(@('failed', 1), @('cancelled', 2), @('damaged', 3), @('download', 4), @('downgrade', 5), @('busy', 6), @('not-installed', 9))) {
        Assert-Same $pair[1] (Get-UninstallExitCode @{ ok = $false; category = $pair[0] }) $pair[0]
    }
    Assert-Same 1 (Get-UninstallExitCode $null) 'no result file'
    Assert-Same 1 (Get-UninstallExitCode @{ ok = $false; category = 'new-one' }) 'an unknown category'
    foreach ($code in 0, 1, 2, 6, 9) { Assert (-not [string]::IsNullOrWhiteSpace((Get-UninstallExitText $code))) "text for $code" }
    Assert-Same 'DELETE HISTORY' $script:ConfirmPhrase 'the phrase the engine insists on'
}

Check 'uninstall-arguments' {
    $work = Join-Path $scratch 'u-args'
    $root = Join-Path $scratch 'u-root'
    $plain = @(Get-UninstallArguments -Root $root -Work $work -History $false -Preview $false -Report $false)
    Assert-Same 'uninstall, --root' ($plain[0..1] -join ', ') 'the command'
    Assert-Same $root $plain[2] 'the installation folder'
    Assert ($plain -join ' ' -match '--result-file') 'the result file'
    Assert ($plain -join ' ' -match '--log-file') 'the log file'
    Assert (-not ($plain -join ' ' -match '--dry-run|--remove-history|--confirm')) 'no option that was not asked for'
    $history = @(Get-UninstallArguments -Root $root -Work $work -History $true -Preview $false -Report $false)
    Assert (($history -join ' ') -match '--remove-history --confirm DELETE HISTORY$') 'history removal carries the confirmation phrase'
    $preview = @(Get-UninstallArguments -Root $root -Work $work -History $true -Preview $true -Report $false)
    Assert (($preview -join ' ') -match '--dry-run --remove-history --confirm DELETE HISTORY$') 'a dry run may show everything'
    $report = @(Get-UninstallArguments -Root $root -Work $work -History $true -Preview $true -Report $true)
    Assert-Same 'diagnose' $report[0] 'the report is a diagnosis'
    Assert (($report -join ' ') -match '--json-report') 'with the JSON report'
    Assert (-not ($report -join ' ' -match '--remove-history|--dry-run')) 'and changes nothing'
}

Check 'uninstall-prerequisites' {
    $app = New-Folder 'u-app-missing'
    $missing = @(Test-UninstallPrerequisites $app)
    Assert-Same 'python\python.exe, installer\installer_core.py, installer\payload_container.py, helper\bmdfs.py' ($missing -join ', ') 'everything is missing'
    New-Item -ItemType Directory -Force -Path (Join-Parts $app @('python')), (Join-Parts $app @('installer')), (Join-Parts $app @('helper')) | Out-Null
    foreach ($relative in @(@('python', 'python.exe'), @('installer', 'installer_core.py'), @('installer', 'payload_container.py'), @('helper', 'bmdfs.py'))) {
        [System.IO.File]::WriteAllText((Join-Parts $app $relative), 'x')
    }
    Assert-Same @() (Test-UninstallPrerequisites $app) 'a complete folder'
    Remove-Item -LiteralPath (Join-Parts $app @('installer', 'payload_container.py'))      # the 2.4.0 uninstaller forgot this one
    Assert-Same 'installer\payload_container.py' ((Test-UninstallPrerequisites $app) -join ', ') 'without the file the engine imports'
}

# A fake app folder for the uninstaller: a stand-in engine and a program called python.exe.
function New-FakeApp([string]$Name) {
    $home2 = New-Folder $Name
    $app = Join-Parts $home2 @('BraveMusicDownloader', 'app')
    $pythonFolder = Join-Parts $app @('python')
    New-Item -ItemType Directory -Force -Path $pythonFolder, (Join-Parts $app @('installer')), (Join-Parts $app @('helper')) | Out-Null
    if ($onWindows) {
        # a real interpreter needs its folder: a junction makes it appear as app\python
        Remove-Item -LiteralPath $pythonFolder
        New-Item -ItemType Junction -Path $pythonFolder -Target (Split-Path -Parent $python) | Out-Null
    } else {
        $launcher = '#!/bin/sh' + "`n" + 'exec "' + $python + '" "$@"' + "`n"
        [System.IO.File]::WriteAllText((Join-Path $pythonFolder 'python.exe'), $launcher)
        & chmod 755 (Join-Path $pythonFolder 'python.exe')
    }
    [System.IO.File]::WriteAllText((Join-Path $app @('installer', 'installer_core.py')), $script:UninstallStandIn)
    [System.IO.File]::WriteAllText((Join-Path $app @('installer', 'payload_container.py')), "# stand-in`n")
    [System.IO.File]::WriteAllText((Join-Path $app @('helper', 'bmdfs.py')), "# stand-in`n")
    Copy-Item -LiteralPath (Join-Path $Repo 'app\Uninstall.ps1') -Destination $app
    return $app
}

# -InputObject (not the pipeline) keeps a list of one element a list, in both PowerShells.
function Read-JsonList([string]$Text) { return (ConvertFrom-Json -InputObject $Text) }

function Get-UninstallRecord([string]$Path) {
    $lines = @(Get-Content -LiteralPath $Path)
    return @{ Files = (Read-JsonList $lines[0]); Arguments = (Read-JsonList $lines[1]); Helper = (Read-JsonList $lines[2]) }
}

$script:Removed = '{"ok": true, "code": 0, "category": null, "message": null, "result": {"removed": ["tool yt-dlp.exe"], "kept": ["your music (never touched): C:\\Users\\me\\Music\\Brave Music Downloader"], "skipped": [], "failed": [], "legacy_install": false, "dry_run": false}}'

Check 'uninstall-silent' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $app = New-FakeApp 'u-silent-app'
    $environment = New-ChildEnvironment 'u-silent'
    $recordPath = Join-Path $scratch 'u-record.txt'
    $environment['BMD_UNINSTALL_RECORD'] = $recordPath
    $environment['BMD_UNINSTALL_RESULT'] = $script:Removed
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent') -Environment $environment
    Assert-Same 0 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert-Same '' $run.Out.Trim() 'silent: no output'
    Assert-Same '' $run.Error.Trim() 'and no error text'
    $record = Get-UninstallRecord $recordPath
    Assert-Same 'installer_core.py, payload_container.py' (@($record.Files) -join ', ') 'the engine runs from a private copy that has the second engine file too'
    Assert-Same 'bmdfs.py' (@($record.Helper) -join ', ') 'and the helper module'
    Assert-Same 'uninstall' $record.Arguments[0] 'the command'
    Assert-Same (Split-Path -Parent $app) $record.Arguments[$record.Arguments.IndexOf('--root') + 1] 'the installation folder'
    Assert (-not (@($record.Arguments) -contains '--confirm')) 'nothing about history unless asked'
    Assert-Same @() @(Get-ChildItem -LiteralPath $environment['TEMP'] -Directory -Filter 'bmd-uninstall-*') 'the private copy is removed afterwards'
    Assert (-not (Test-Path -LiteralPath (Join-Path $app 'Uninstall.ps1'))) 'the uninstaller removed its own copy'
    Assert (Test-Path -LiteralPath (Join-Path $app 'python')) 'and nothing else: the engine has decided what goes'
}

Check 'uninstall-silent-history-and-outcomes' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $app = New-FakeApp 'u-history-app'
    $root = Split-Path -Parent $app
    $environment = New-ChildEnvironment 'u-silent-history'
    $recordPath = Join-Path $scratch 'u-record-history.txt'
    $environment['BMD_UNINSTALL_RECORD'] = $recordPath
    $environment['BMD_UNINSTALL_RESULT'] = $script:Removed
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent', '-RemoveHistory') -Environment $environment
    Assert-Same 0 $run.Code "exit code; error: $($run.Error)"
    $arguments = (Get-UninstallRecord $recordPath).Arguments
    Assert-Same 'DELETE HISTORY' $arguments[$arguments.IndexOf('--confirm') + 1] 'with -Silent the switch is the confirmation'
    # A busy engine: the category becomes the exit code and the engine's log is kept.
    # (The uninstaller removed its own copy after the success above, as it does for real.)
    Copy-Item -LiteralPath (Join-Path $Repo 'app\Uninstall.ps1') -Destination $app
    $environment['BMD_UNINSTALL_RESULT'] = '{"ok": false, "code": 2, "category": "busy", "message": "Setup is already running.", "result": null}'
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent') -Environment $environment
    Assert-Same 6 $run.Code 'another setup is running: exit code 6'
    Assert (Test-Path -LiteralPath (Join-Path $root 'Uninstall.log')) 'the engine''s log was kept next to the app'
    $environment['BMD_UNINSTALL_RESULT'] = '{"ok": false, "code": 2, "category": "failed", "message": "Files are in use.", "result": {"removed": [], "kept": [], "skipped": [], "failed": ["folder tools"], "dry_run": false}}'
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent') -Environment $environment
    Assert-Same 1 $run.Code 'a failed uninstall: exit code 1'
    Assert-Same @() @(Get-ChildItem -LiteralPath $environment['TEMP'] -Directory -Filter 'bmd-uninstall-*') 'the private copy is removed after a failure too'
    # -Diagnose writes a report and removes nothing.
    $environment['BMD_UNINSTALL_RECORD'] = Join-Path $scratch 'u-record-diagnose.txt'
    $environment['BMD_UNINSTALL_RESULT'] = '{"ok": true, "code": 0, "category": null, "message": null, "result": {"checks": []}}'
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent', '-Diagnose') -Environment $environment
    Assert-Same 0 $run.Code "diagnosis: exit code 0; error: $($run.Error)"
    $arguments = (Get-UninstallRecord $environment['BMD_UNINSTALL_RECORD']).Arguments
    Assert-Same 'diagnose' $arguments[0] 'the command'
    Assert (Test-Path -LiteralPath (Join-Path $app 'python')) 'the app is still there'
    # An incomplete folder is refused before anything runs.
    Remove-Item -LiteralPath (Join-Path $app 'helper\bmdfs.py')
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-Silent') -Environment $environment
    Assert-Same 1 $run.Code 'an incomplete folder'
}

Check 'uninstall-asks-before-removing-history' {
    if (-not $python) { Skip 'needs BMD_TEST_PYTHON' }
    $app = New-FakeApp 'u-ask-app'
    $environment = New-ChildEnvironment 'u-ask'
    $recordPath = Join-Path $scratch 'u-record-ask.txt'
    $environment['BMD_UNINSTALL_RECORD'] = $recordPath
    $environment['BMD_UNINSTALL_RESULT'] = $script:Removed
    # Nobody typed the phrase (the input is closed): cancelled, and the engine was never started.
    $run = Invoke-Script -Script (Join-Path $app 'Uninstall.ps1') -Arguments @('-RemoveHistory') -Environment $environment
    Assert-Same 2 $run.Code "exit code; output: $($run.Out) error: $($run.Error)"
    Assert ($run.Out -like '*Cancelled. Nothing was changed.*') 'the answer'
    Assert (-not (Test-Path -LiteralPath $recordPath)) 'the engine was not started'
    Assert (Test-Path -LiteralPath (Join-Path $app 'python')) 'nothing was removed'
}

Check 'uninstall-stop-processes-and-leftovers' {
    $app = New-Folder 'u-leftovers'
    $under = New-Object psobject -Property @{ Path = (Join-Path $app 'python\pythonw.exe'); Killed = $false }
    $beside = New-Object psobject -Property @{ Path = ($app + '-old\python\pythonw.exe'); Killed = $false }
    $elsewhere = New-Object psobject -Property @{ Path = '/usr/bin/python3'; Killed = $false }
    $noPath = New-Object psobject -Property @{ Path = $null; Killed = $false }
    foreach ($process in $under, $beside, $elsewhere, $noPath) {
        $process | Add-Member -MemberType ScriptMethod -Name Kill -Value { $this.Killed = $true }
    }
    Stop-AppProcesses -App $app -Processes @($under, $beside, $elsewhere, $noPath)
    Assert ($under.Killed) 'the helper under the app folder is stopped'
    foreach ($process in $beside, $elsewhere, $noPath) { Assert (-not $process.Killed) "another program ($($process.Path)) is left alone" }
    # Leftovers: only the script itself and folders that have become empty; never anything recursive.
    $home2 = New-Folder 'u-leftover-app'
    $real = Join-Path $home2 @('BraveMusicDownloader', 'app')
    New-Item -ItemType Directory -Force -Path (Join-Path $real 'python') | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $real 'python\python.exe'), 'x')
    Copy-Item -LiteralPath (Join-Path $Repo 'app\Uninstall.ps1') -Destination $real
    $removed = @(Remove-AppLeftovers -App $real -Root (Split-Path -Parent $real))
    Assert-Same @() $removed 'a folder with files in it stays'
    Assert (-not (Test-Path -LiteralPath (Join-Path $real 'Uninstall.ps1'))) 'the uninstaller''s own copy is gone'
    Assert (Test-Path -LiteralPath (Join-Path $real 'python')) 'everything else is still there'
    $emptyHome = New-Folder 'u-leftover-empty'
    $empty = Join-Parts $emptyHome @('BraveMusicDownloader', 'app')
    New-Item -ItemType Directory -Force -Path $empty | Out-Null
    $root = Split-Path -Parent $empty
    $removed = @(Remove-AppLeftovers -App $empty -Root $root | Sort-Object)
    $expected = @($root, $empty) | Sort-Object
    Assert-Same ($expected -join ', ') ($removed -join ', ') 'empty folders go'
    Assert-Same @() @(Remove-AppLeftovers -App (Join-Path $scratch 'not-there') -Root (Join-Path $scratch 'not-there')) 'missing folders are not an error'
}

# ---- the end ---------------------------------------------------------------------------------

$script:cases | ForEach-Object { Write-Output $_ }
$failed = @($script:cases | Where-Object { $_ -like 'fail|*' }).Count
Write-Output ("cases|" + $script:cases.Count + '|' + $failed)
if (-not $env:BMD_TEST_TMP) { Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue }
exit $failed
