[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Supervise,
    [ValidateRange(0, 300)]
    [int]$NapCatWaitSeconds = 75,
    [ValidateRange(0, 120)]
    [int]$BotWaitSeconds = 30,
    [ValidateRange(1, 300)]
    [int]$SuperviseIntervalSeconds = 10,
    [ValidateRange(0, 3600)]
    [int]$RestartBackoffSeconds = 30
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# StrictMode treats an unset variable reference as fatal; the supervision
# branch below reads these even when the launch paths were skipped.
$botProcess = $null
$napCatProcess = $null

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$NapCatDirectory = Join-Path $ProjectRoot "napcat\NapCat.Shell"
$NapCatExecutable = Join-Path $NapCatDirectory "node.exe"
$NapCatEntryPoint = Join-Path $NapCatDirectory "index.js"
$NapCatConfigDirectory = Join-Path $NapCatDirectory "napcat\config"
$BotEntryPoint = Join-Path $ProjectRoot "bot.py"
$EnvironmentFile = Join-Path $ProjectRoot ".env"
$NapCatPort = 3001
$BotPort = 8080

function Write-Status {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,
        [ConsoleColor]$Color = [ConsoleColor]::Gray
    )

    Write-Host $Message -ForegroundColor $Color
}

function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)]
        [string]$HostName,
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = New-Object System.Net.Sockets.TcpClient
    $waitHandle = $null
    try {
        $asyncResult = $client.BeginConnect($HostName, $Port, $null, $null)
        $waitHandle = $asyncResult.AsyncWaitHandle
        if (-not $waitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }
        $client.EndConnect($asyncResult)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $waitHandle) {
            $waitHandle.Close()
        }
        $client.Close()
    }
}

function Wait-TcpPort {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds
    )

    if (Test-TcpPort -HostName "127.0.0.1" -Port $Port) {
        return $true
    }
    if ($TimeoutSeconds -le 0) {
        return $false
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-TcpPort -HostName "127.0.0.1" -Port $Port) {
            return $true
        }
    }
    return $false
}

function Get-NapCatProcess {
    $expectedPath = [IO.Path]::GetFullPath($NapCatExecutable)
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" |
                Where-Object {
                    $_.ExecutablePath -and
                    [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $expectedPath
                }
        ) | Select-Object -First 1
    }
    catch {
        return $null
    }
}

function Get-NapCatAccount {
    if (-not (Test-Path -LiteralPath $NapCatConfigDirectory -PathType Container)) {
        return $null
    }

    $accountIds = @(
        Get-ChildItem -LiteralPath $NapCatConfigDirectory -Filter "onebot11_*.json" -File |
            ForEach-Object {
                if ($_.BaseName -match '^onebot11_(\d+)$') {
                    $Matches[1]
                }
            } |
            Select-Object -Unique
    )
    if ($accountIds.Count -eq 1) {
        return $accountIds[0]
    }
    return $null
}

function Assert-LauncherPrerequisites {
    foreach ($requiredFile in @($NapCatExecutable, $NapCatEntryPoint, $BotEntryPoint, $EnvironmentFile)) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Required file is missing: $requiredFile"
        }
    }

    $uvCommand = Get-Command "uv.exe" -ErrorAction SilentlyContinue
    if ($null -eq $uvCommand) {
        $uvCommand = Get-Command "uv" -ErrorAction SilentlyContinue
    }
    if ($null -eq $uvCommand) {
        throw "uv was not found. Install uv or add it to PATH, then try again."
    }
    return $uvCommand.Source
}

function Get-BotProcess {
    $expectedPath = [IO.Path]::GetFullPath((Join-Path $ProjectRoot ".venv\Scripts\python.exe"))
    try {
        return @(
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
                Where-Object {
                    $_.ExecutablePath -and
                    [IO.Path]::GetFullPath($_.ExecutablePath) -ieq $expectedPath -and
                    $_.CommandLine -like '*bot.py*'
                }
        ) | Select-Object -First 1
    }
    catch {
        return $null
    }
}

function Start-NapCatProcess {
    $ffmpegExecutable = Join-Path (Split-Path $ProjectRoot -Parent) ".local\bin\ffmpeg.exe"
    if (Test-Path -LiteralPath $ffmpegExecutable -PathType Leaf) {
        $env:FFMPEG_PATH = $ffmpegExecutable
    }

    $napCatArguments = @("./index.js")
    $account = Get-NapCatAccount
    if ($null -ne $account) {
        $napCatArguments += @("-q", $account)
    }

    $process = Start-Process `
        -FilePath $NapCatExecutable `
        -ArgumentList $napCatArguments `
        -WorkingDirectory $NapCatDirectory `
        -WindowStyle Normal `
        -PassThru
    Write-Status "[STARTED] NapCat process PID $($process.Id)." Cyan
    return $process
}

function Start-BotProcess {
    $process = Start-Process `
        -FilePath $uvExecutable `
        -ArgumentList @("run", "--frozen", "python", "bot.py") `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Normal `
        -PassThru
    Write-Status "[STARTED] Bot process PID $($process.Id)." Cyan
    return $process
}

function Stop-ProcessGracefully {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId,
        [int]$WaitSeconds = 20,
        [string]$Label = "process"
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.HasExited) {
        return
    }
    Write-Status "[STOP] Closing $Label window (graceful path)..." Yellow
    try {
        $null = $process.CloseMainWindow()
    }
    catch {
        # No main window to close; fall through to forced stop.
    }
    if (-not $process.WaitForExit($WaitSeconds * 1000)) {
        Write-Status "[STOP] $Label did not exit gracefully; forcing stop." Red
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
    else {
        Write-Status "[STOP] $Label exited cleanly." Green
    }
}

function Invoke-Supervisor {
    param(
        [bool]$NapCatStarted,
        [bool]$BotStarted
    )

    $now = [DateTime]::UtcNow
    $napCatLastStarted = if ($NapCatStarted) { $now } else { $null }
    $botLastStarted = if ($BotStarted) { $now } else { $null }
    $backoff = [TimeSpan]::FromSeconds($RestartBackoffSeconds)
    $ctrlHandlerRegistered = $false

    try {
        Add-Type -ErrorAction Stop @"
using System;
using System.Runtime.InteropServices;
public static class TaffySupervisorStop {
    public delegate bool HandlerRoutine(uint dwCtrlType);
    [DllImport("kernel32.dll")] public static extern bool SetConsoleCtrlHandler(HandlerRoutine handler, bool add);
    public static volatile bool StopRequested = false;
    public static bool OnStop(uint dwCtrlType) { StopRequested = true; return true; }
}
"@
        [TaffySupervisorStop]::SetConsoleCtrlHandler(
            [TaffySupervisorStop+HandlerRoutine][TaffySupervisorStop]::OnStop,
            $true
        ) | Out-Null
        $ctrlHandlerRegistered = $true
    }
    catch {
        Write-Status "[SUPERVISE] Ctrl+C handler unavailable; stop by closing this window or killing the watchdog." Yellow
    }

    Write-Status "[SUPERVISE] Monitoring every $SuperviseIntervalSeconds s (backoff $RestartBackoffSeconds s). Ctrl+C to stop." Cyan
    while ($true) {
        Start-Sleep -Seconds $SuperviseIntervalSeconds
        if ($ctrlHandlerRegistered -and [TaffySupervisorStop]::StopRequested) {
            break
        }

        if (-not (Test-TcpPort -HostName "127.0.0.1" -Port $BotPort)) {
            if ($null -ne $botLastStarted -and
                ([DateTime]::UtcNow - $botLastStarted) -lt $backoff) {
                Write-Status "[SUPERVISE] Bot port $BotPort down; restart backoff in effect, skipping." DarkGray
            }
            else {
                Write-Status "[SUPERVISE] Bot is down, restarting..." Red
                try {
                    $null = Start-BotProcess
                }
                catch {
                    Write-Status "[SUPERVISE] Bot restart failed: $($_.Exception.Message)" Red
                }
                $botLastStarted = [DateTime]::UtcNow
            }
        }

        if (-not (Test-TcpPort -HostName "127.0.0.1" -Port $NapCatPort)) {
            if ($null -ne $napCatLastStarted -and
                ([DateTime]::UtcNow - $napCatLastStarted) -lt $backoff) {
                Write-Status "[SUPERVISE] NapCat port $NapCatPort down; restart backoff in effect, skipping." DarkGray
            }
            else {
                Write-Status "[SUPERVISE] NapCat is down, restarting..." Red
                try {
                    $null = Start-NapCatProcess
                }
                catch {
                    Write-Status "[SUPERVISE] NapCat restart failed: $($_.Exception.Message)" Red
                }
                $napCatLastStarted = [DateTime]::UtcNow
            }
        }
    }

    Write-Status "[SUPERVISE] Stop requested; shutting down children..." Yellow
    $botProcess = Get-BotProcess
    if ($null -ne $botProcess) {
        $botProcessId = $botProcess.ProcessId
        if ($null -eq $botProcessId) { $botProcessId = $botProcess.Id }
        Stop-ProcessGracefully -ProcessId $botProcessId -Label "bot"
    }
    $napCatProcess = Get-NapCatProcess
    if ($null -ne $napCatProcess) {
        $napCatProcessId = $napCatProcess.ProcessId
        if ($null -eq $napCatProcessId) { $napCatProcessId = $napCatProcess.Id }
        Stop-ProcessGracefully -ProcessId $napCatProcessId -Label "NapCat"
    }
    Write-Status "[SUPERVISE] Supervisor exited." Green
}

try {
    $uvExecutable = Assert-LauncherPrerequisites
}
catch {
    Write-Status "[ERROR] $($_.Exception.Message)" Red
    exit 1
}

$napCatReady = Test-TcpPort -HostName "127.0.0.1" -Port $NapCatPort
$botReady = Test-TcpPort -HostName "127.0.0.1" -Port $BotPort
$napCatProcess = Get-NapCatProcess
$napCatAccount = Get-NapCatAccount

Write-Status "Taffy QQ Bot launcher" Cyan
Write-Status "Project: $ProjectRoot" DarkGray

if ($DryRun) {
    Write-Status "[DRY RUN] No processes will be started." Yellow
    Write-Status "NapCat port $NapCatPort ready: $napCatReady"
    Write-Status "NapCat process found: $($null -ne $napCatProcess)"
    Write-Status "Fast-login account found: $($null -ne $napCatAccount)"
    Write-Status "Bot port $BotPort ready: $botReady"
    exit 0
}

if ($napCatReady) {
    Write-Status "[OK] NapCat is already running on port $NapCatPort." Green
}
else {
    if ($null -ne $napCatProcess) {
        Write-Status "[WAIT] NapCat is already starting." Yellow
    }
    else {
        try {
            $napCatProcess = Start-NapCatProcess
            Write-Status "[WAIT] Waiting up to $NapCatWaitSeconds seconds for QQ login and port $NapCatPort..." Yellow
        }
        catch {
            Write-Status "[ERROR] NapCat could not start: $($_.Exception.Message)" Red
            exit 1
        }
    }
    $napCatReady = Wait-TcpPort -Port $NapCatPort -TimeoutSeconds $NapCatWaitSeconds
    if ($napCatReady) {
        Write-Status "[OK] NapCat is connected." Green
    }
    else {
        Write-Status "[NOTICE] NapCat is not connected yet. Complete QQ login or QR scan in its window." Yellow
        Write-Status "         The bot will still start and reconnect automatically." Yellow
    }
}

if ($botReady) {
    Write-Status "[OK] The bot is already running on port $BotPort." Green
}
else {
    try {
        $botProcess = Start-BotProcess
        Write-Status "[WAIT] Waiting up to $BotWaitSeconds seconds for port $BotPort..." Yellow
    }
    catch {
        Write-Status "[ERROR] The bot could not start: $($_.Exception.Message)" Red
        exit 1
    }

    $botReady = Wait-TcpPort -Port $BotPort -TimeoutSeconds $BotWaitSeconds
    if ($botReady) {
        Write-Status "[OK] The bot is listening on port $BotPort." Green
    }
    elseif ($botProcess.HasExited) {
        Write-Status "[ERROR] The bot exited early with code $($botProcess.ExitCode). Check its window for details." Red
        exit 1
    }
    else {
        Write-Status "[NOTICE] The bot is still starting. Check its window for details." Yellow
    }
}

if ($napCatReady -and $botReady) {
    Write-Status "[READY] NapCat and the bot are both running." Green
}
else {
    Write-Status "[PARTIAL] Both processes were launched, but QQ/NapCat may still need login." Yellow
}

if ($Supervise) {
    if ($null -eq $botProcess) {
        $botProcess = Get-BotProcess
    }
    Invoke-Supervisor `
        -NapCatStarted ($napCatReady -or $null -ne $napCatProcess) `
        -BotStarted ($botReady -or $null -ne $botProcess)
}

exit 0
