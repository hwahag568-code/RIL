[CmdletBinding()]
param(
    [string]$OutputRoot = "",
    [string]$ExpectedVersion = "",
    [string]$ExpectedServerSha256 = "",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$exitCode = 0
$workDir = $null
$zipPath = $null

function Write-Section {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Title,
        [Parameter(Mandatory = $true)][AllowNull()]$Value
    )

    Add-Content -LiteralPath $Path -Value ""
    Add-Content -LiteralPath $Path -Value "===== $Title ====="
    $Value | Out-String -Width 4096 | Add-Content -LiteralPath $Path
}

function Copy-RedactedLogTail {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [int]$Tail = 500
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        return
    }

    Get-Content -LiteralPath $Source -Tail $Tail -ErrorAction SilentlyContinue |
        ForEach-Object {
            $_ `
                -replace "(?i)(ID:\s*)[^),\s]+", '$1[REDACTED]' `
                -replace "(?i)(PW:\s*)[^),\s]+", '$1[REDACTED]'
        } |
        Set-Content -LiteralPath $Destination -Encoding UTF8
}

try {
    if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
        $desktop = [Environment]::GetFolderPath("Desktop")
        $OutputRoot = Join-Path $desktop "RIL_AU_Alignment_Diagnostic"
    }

    $stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    New-Item -ItemType Directory -Path $OutputRoot -Force | Out-Null
    $workDir = Join-Path $OutputRoot "RIL_AU_Alignment_$stamp"
    New-Item -ItemType Directory -Path $workDir -Force | Out-Null

    $summaryPath = Join-Path $workDir "summary.txt"
    $serverExe = "C:\Program Files\RIL\RIL_server.exe"

    @(
        "Collected=$(Get-Date -Format o)"
        "Computer=$env:COMPUTERNAME"
        "User=$env:USERNAME"
        "ExpectedVersion=$ExpectedVersion"
        "ExpectedServerSha256=$ExpectedServerSha256"
        "ReadOnlyDiagnostic=True"
    ) | Set-Content -LiteralPath $summaryPath -Encoding UTF8

    if (Test-Path -LiteralPath $serverExe -PathType Leaf) {
        $serverItem = Get-Item -LiteralPath $serverExe
        $actualServerHash = (Get-FileHash -LiteralPath $serverExe -Algorithm SHA256).Hash
        $matchesExpectedBuild = $null
        if (-not [string]::IsNullOrWhiteSpace($ExpectedServerSha256)) {
            $matchesExpectedBuild = (
                $actualServerHash -eq $ExpectedServerSha256
            )
        }
        Write-Section -Path $summaryPath -Title "Installed server file" -Value (
            [PSCustomObject]@{
                Path = $serverItem.FullName
                Length = $serverItem.Length
                LastWriteTime = $serverItem.LastWriteTime
                SHA256 = $actualServerHash
                MatchesExpectedBuild = $matchesExpectedBuild
            }
        )
    }
    else {
        Write-Section -Path $summaryPath -Title "Installed server file" -Value "NOT FOUND: $serverExe"
    }

    try {
        $registry = Get-ItemProperty -LiteralPath "HKLM:\Software\RIL" -ErrorAction Stop
        Write-Section -Path $summaryPath -Title "RIL registry" -Value (
            $registry | Select-Object Install_Dir, ServerVersion
        )
    }
    catch {
        Write-Section -Path $summaryPath -Title "RIL registry" -Value "Registry read failed: $($_.Exception.Message)"
    }

    $allProcesses = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
    $relevantProcesses = $allProcesses |
        Where-Object {
            $_.Name -ieq "RIL_server.exe" -or
            $_.Name -ieq "Ui.Kumc.GR.Interface.exe" -or
            $_.Name -ieq "AU_RSLT.exe"
        } |
        Select-Object Name, ProcessId, ParentProcessId, SessionId,
            CreationDate, ExecutablePath, CommandLine
    $relevantProcesses |
        Export-Csv -LiteralPath (Join-Path $workDir "processes.csv") `
            -NoTypeInformation -Encoding UTF8
    Write-Section -Path $summaryPath -Title "Relevant processes" -Value $relevantProcesses

    Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public sealed class RilWindowRecord
{
    public long Hwnd { get; set; }
    public uint ProcessId { get; set; }
    public string Title { get; set; }
    public bool IsVisible { get; set; }
    public bool IsMaximized { get; set; }
    public bool IsMinimized { get; set; }
    public int Left { get; set; }
    public int Top { get; set; }
    public int Right { get; set; }
    public int Bottom { get; set; }
    public int WorkLeft { get; set; }
    public int WorkTop { get; set; }
    public int WorkRight { get; set; }
    public int WorkBottom { get; set; }
}

public static class RilWindowInspector
{
    private delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO
    {
        public int cbSize;
        public RECT rcMonitor;
        public RECT rcWork;
        public uint dwFlags;
    }

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr lParam);

    [DllImport("user32.dll")]
    private static extern int GetWindowTextLength(IntPtr hWnd);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr hWnd, StringBuilder text, int count);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsZoomed(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);

    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hWnd, uint flags);

    [DllImport("user32.dll")]
    private static extern bool GetMonitorInfo(IntPtr monitor, ref MONITORINFO info);

    public static List<RilWindowRecord> GetVisibleAuWindows()
    {
        List<RilWindowRecord> result = new List<RilWindowRecord>();
        EnumWindows(delegate(IntPtr hWnd, IntPtr lParam)
        {
            int length = GetWindowTextLength(hWnd);
            if (length <= 0 || !IsWindowVisible(hWnd))
            {
                return true;
            }

            StringBuilder title = new StringBuilder(length + 1);
            GetWindowText(hWnd, title, title.Capacity);
            string value = title.ToString();
            if (value.IndexOf("AU_", StringComparison.OrdinalIgnoreCase) < 0)
            {
                return true;
            }

            uint processId;
            GetWindowThreadProcessId(hWnd, out processId);

            RECT rect;
            GetWindowRect(hWnd, out rect);

            MONITORINFO monitorInfo = new MONITORINFO();
            monitorInfo.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
            IntPtr monitor = MonitorFromWindow(hWnd, 2);
            GetMonitorInfo(monitor, ref monitorInfo);

            result.Add(new RilWindowRecord
            {
                Hwnd = hWnd.ToInt64(),
                ProcessId = processId,
                Title = value,
                IsVisible = true,
                IsMaximized = IsZoomed(hWnd),
                IsMinimized = IsIconic(hWnd),
                Left = rect.Left,
                Top = rect.Top,
                Right = rect.Right,
                Bottom = rect.Bottom,
                WorkLeft = monitorInfo.rcWork.Left,
                WorkTop = monitorInfo.rcWork.Top,
                WorkRight = monitorInfo.rcWork.Right,
                WorkBottom = monitorInfo.rcWork.Bottom
            });
            return true;
        }, IntPtr.Zero);
        return result;
    }
}
"@

    $windows = [RilWindowInspector]::GetVisibleAuWindows() |
        Where-Object { $_.Title -match "AU_[123] (Result )?INTERFACE" } |
        Sort-Object Title, ProcessId
    $windows |
        Export-Csv -LiteralPath (Join-Path $workDir "au_windows.csv") `
            -NoTypeInformation -Encoding UTF8
    Write-Section -Path $summaryPath -Title "Visible AU windows" -Value $windows

    try {
        $tasks = Get-ScheduledTask -TaskName "RIL_server", "RIL_server_restarter" `
            -ErrorAction SilentlyContinue |
            ForEach-Object {
                [PSCustomObject]@{
                    TaskName = $_.TaskName
                    State = $_.State
                    Actions = ($_.Actions | ForEach-Object {
                        "$($_.Execute) $($_.Arguments) [WorkingDirectory=$($_.WorkingDirectory)]"
                    }) -join " | "
                }
            }
        Write-Section -Path $summaryPath -Title "Scheduled tasks" -Value $tasks
    }
    catch {
        Write-Section -Path $summaryPath -Title "Scheduled tasks" -Value "Task read failed: $($_.Exception.Message)"
    }

    $logSources = @(
        @{ Path = "C:\Program Files\RIL\server_output_log.txt"; Name = "server_output_log.txt" },
        @{ Path = "C:\Program Files\RIL\server_error_log.txt"; Name = "server_error_log.txt" },
        @{ Path = "C:\Windows\System32\server_output_log.txt"; Name = "system32_server_output_log.txt" },
        @{ Path = "C:\Windows\System32\server_error_log.txt"; Name = "system32_server_error_log.txt" }
    )

    foreach ($source in $logSources) {
        Copy-RedactedLogTail `
            -Source $source.Path `
            -Destination (Join-Path $workDir $source.Name)
    }

    $auFolders = @(
        "C:\Program Files\LIS_Interface\AU_5822",
        "C:\Program Files\LIS_Interface\AU_5822_RSLT",
        "C:\Program Files\LIS_Interface\AU_5832",
        "C:\Program Files\LIS_Interface\AU_5832_RSLT",
        "C:\Program Files (x86)\LIS_Interface\AU_3",
        "C:\Program Files (x86)\LIS_Interface\AU_3_RSLT"
    )

    $logIndex = 0
    $logManifest = @()
    foreach ($folder in $auFolders) {
        if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
            continue
        }
        $logs = Get-ChildItem -LiteralPath $folder `
            -File -Filter "RIL_server_Log_*.txt" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 2
        foreach ($log in $logs) {
            $logIndex++
            $outputName = "ril_log_{0:D2}.txt" -f $logIndex
            Copy-RedactedLogTail `
                -Source $log.FullName `
                -Destination (Join-Path $workDir $outputName)
            $logManifest += [PSCustomObject]@{
                Output = $outputName
                Source = $log.FullName
                LastWriteTime = $log.LastWriteTime
            }
        }
    }
    $logManifest |
        Export-Csv -LiteralPath (Join-Path $workDir "ril_log_manifest.csv") `
            -NoTypeInformation -Encoding UTF8

    $zipPath = Join-Path $OutputRoot "RIL_AU_Alignment_$stamp.zip"
    Compress-Archive -LiteralPath $workDir -DestinationPath $zipPath -Force

    Write-Host ""
    Write-Host "Diagnostic completed."
    Write-Host "ZIP: $zipPath"
    Write-Host "No application, registry, task, or program file was changed."
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    if ($null -ne $workDir) {
        Write-Host "Partial output: $workDir"
    }
}
finally {
    if (-not $NoPause) {
        Write-Host ""
        [void](Read-Host "Press Enter to close")
    }
}

exit $exitCode
