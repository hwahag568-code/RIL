[CmdletBinding()]
param(
    [string]$TargetDir = "C:\Program Files (x86)\LIS_Interface\AU_3",
    [string]$BackupRoot = "C:\LIS_Interface_Backup",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$exitCode = 0
$safetyBackupDir = $null
$replacementStarted = $false
$safetyLisHash = $null
$safetyEqpHash = $null

$originalLisHash = "C466E40210559B7A6CA0CD0FC11EE68CC81F7E5E97A13B8F060FF159CFFF70A2"
$originalEqpHash = "27F032657E1637A6585130B5B991040249BDBAF114B11D87B8F73740AD4EC4E7"
$restoredLisHash = "43EE3D4184B6F84640B4D8D1724C4CFC2199E10FC9FF975BB5E3AD803E3E1C3A"
$restoredEqpHash = "B1ED74D2175DB2688AB2B64584DD880E8290D0E6A8782FA49BF086FFCD63ABDA"

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
}

function Get-FileHashValue {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function Assert-FileUnlocked {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    try {
        $stream = [System.IO.File]::Open(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    }
    catch {
        throw "File is still in use: $Path`nClose AU Order, AU Result, and the RIL server, then retry."
    }
    finally {
        if ($null -ne $stream) {
            $stream.Dispose()
        }
    }
}

function Assert-RelatedProcessesStopped {
    $blocking = @()

    try {
        $processes = Get-CimInstance Win32_Process -ErrorAction Stop
        foreach ($process in $processes) {
            $name = [string]$process.Name
            $isRilPython = (
                $name -in @("python.exe", "pythonw.exe") -and
                [string]$process.CommandLine -match "(?i)RIL_server.*\.py"
            )
            if (
                $name -ieq "Ui.Kumc.GR.Interface.exe" -or
                $name -ieq "AU_RSLT.exe" -or
                $name -like "RIL_server*.exe" -or
                $isRilPython
            ) {
                $blocking += "$name (PID $($process.ProcessId))"
            }
        }
    }
    catch {
        $processes = Get-Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ProcessName -ieq "Ui.Kumc.GR.Interface" -or
                $_.ProcessName -ieq "AU_RSLT" -or
                $_.ProcessName -like "RIL_server*"
            }
        foreach ($process in $processes) {
            $blocking += "$($process.ProcessName).exe (PID $($process.Id))"
        }
    }

    if ($blocking.Count -gt 0) {
        throw "Related programs are still running:`n$($blocking -join "`n")`nClose all AU programs and the RIL server, then retry."
    }
}

function Read-RestoreInfo {
    param([Parameter(Mandatory = $true)][string]$Path)

    $info = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^([^=]+)=(.*)$") {
            $info[$matches[1]] = $matches[2]
        }
    }
    return $info
}

function Get-ValidatedOriginalBackup {
    param(
        [Parameter(Mandatory = $true)][string]$BackupBase,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $candidates = Get-ChildItem -LiteralPath $BackupBase `
        -Directory `
        -Filter "AU3_Order_before_coherent_restore_*" `
        -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending

    foreach ($candidate in $candidates) {
        try {
            $infoPath = Join-Path $candidate.FullName "restore-info.txt"
            $backupLis = Join-Path $candidate.FullName "Lis.Interface.dll"
            $backupEqp = Join-Path $candidate.FullName "Eqp.Interface.dll"
            if (
                -not (Test-Path -LiteralPath $infoPath -PathType Leaf) -or
                -not (Test-Path -LiteralPath $backupLis -PathType Leaf) -or
                -not (Test-Path -LiteralPath $backupEqp -PathType Leaf)
            ) {
                continue
            }

            $info = Read-RestoreInfo -Path $infoPath
            $infoTarget = Get-NormalizedPath -Path $info.Target
            if (-not $infoTarget.Equals($Target, [System.StringComparison]::OrdinalIgnoreCase)) {
                continue
            }
            if (
                $info.OriginalLisSha256 -ne $originalLisHash -or
                $info.OriginalEqpSha256 -ne $originalEqpHash
            ) {
                continue
            }
            if (
                (Get-FileHashValue -Path $backupLis) -ne $originalLisHash -or
                (Get-FileHashValue -Path $backupEqp) -ne $originalEqpHash
            ) {
                continue
            }
            return $candidate
        }
        catch {
            continue
        }
    }

    return $null
}

function Replace-FileFromSource {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedHash
    )

    $temporary = "$Destination.codex_rollback_tmp"
    $replaceBackup = "$Destination.codex_rollback_replace_backup"
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
    if (Test-Path -LiteralPath $replaceBackup) {
        Remove-Item -LiteralPath $replaceBackup -Force
    }

    Copy-Item -LiteralPath $Source -Destination $temporary -Force
    if ((Get-FileHashValue -Path $temporary) -ne $ExpectedHash) {
        throw "Staged rollback file failed hash verification: $temporary"
    }
    [System.IO.File]::Replace($temporary, $Destination, $replaceBackup)
    if ((Get-FileHashValue -Path $Destination) -ne $ExpectedHash) {
        throw "Installed rollback file failed hash verification: $Destination"
    }
    Remove-Item -LiteralPath $replaceBackup -Force
}

try {
    $target = Get-NormalizedPath -Path $TargetDir
    $backupBase = Get-NormalizedPath -Path $BackupRoot

    if (-not (Test-Path -LiteralPath $target -PathType Container)) {
        throw "AU 3 Order folder was not found: $target"
    }
    if (-not (Test-Path -LiteralPath $backupBase -PathType Container)) {
        throw "Backup root was not found: $backupBase"
    }

    $currentLis = Join-Path $target "Lis.Interface.dll"
    $currentEqp = Join-Path $target "Eqp.Interface.dll"

    $restoreBackup = Get-ValidatedOriginalBackup -BackupBase $backupBase -Target $target

    if ($null -eq $restoreBackup) {
        throw "No validated original backup created by script 03 was found."
    }

    $backupLis = Join-Path $restoreBackup.FullName "Lis.Interface.dll"
    $backupEqp = Join-Path $restoreBackup.FullName "Eqp.Interface.dll"
    $backupLisHash = Get-FileHashValue -Path $backupLis
    $backupEqpHash = Get-FileHashValue -Path $backupEqp

    $activeLisHash = Get-FileHashValue -Path $currentLis
    $activeEqpHash = Get-FileHashValue -Path $currentEqp
    if (
        $activeLisHash -notin @($originalLisHash, $restoredLisHash) -or
        $activeEqpHash -notin @($originalEqpHash, $restoredEqpHash)
    ) {
        throw "The active files are not a diagnosed original/restored state. No change was made."
    }

    if (
        $activeLisHash -eq $originalLisHash -and
        $activeEqpHash -eq $originalEqpHash
    ) {
        Write-Host "The original pre-restore files are already installed."
        Write-Host "No files were changed."
        exit 0
    }

    Assert-RelatedProcessesStopped
    Assert-FileUnlocked -Path $currentLis
    Assert-FileUnlocked -Path $currentEqp

    $safetyBackupDir = Join-Path $backupBase (
        "AU3_Order_before_restore_rollback_" +
        (Get-Date -Format "yyyyMMdd_HHmmss_fff")
    )
    New-Item -ItemType Directory -Path $safetyBackupDir -Force | Out-Null
    Copy-Item -LiteralPath $currentLis -Destination (Join-Path $safetyBackupDir "Lis.Interface.dll")
    Copy-Item -LiteralPath $currentEqp -Destination (Join-Path $safetyBackupDir "Eqp.Interface.dll")
    $safetyLisHash = $activeLisHash
    $safetyEqpHash = $activeEqpHash

    $replacementStarted = $true
    if ($activeLisHash -ne $originalLisHash) {
        Replace-FileFromSource `
            -Source $backupLis `
            -Destination $currentLis `
            -ExpectedHash $backupLisHash
    }
    if ($activeEqpHash -ne $originalEqpHash) {
        Replace-FileFromSource `
            -Source $backupEqp `
            -Destination $currentEqp `
            -ExpectedHash $backupEqpHash
    }

    Write-Host ""
    Write-Host "Rollback completed successfully."
    Write-Host "Restored from: $($restoreBackup.FullName)"
    Write-Host "Safety backup: $safetyBackupDir"
    Write-Host "Unchanged: Interface.db, SQLite, Interop, Util.Library.dll"
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red

    if ($replacementStarted -and $null -ne $safetyBackupDir) {
        Write-Host "Attempting automatic recovery of the coherent set..."
        try {
            $safetyLis = Join-Path $safetyBackupDir "Lis.Interface.dll"
            $safetyEqp = Join-Path $safetyBackupDir "Eqp.Interface.dll"
            if ((Get-FileHashValue -Path $safetyLis) -ne $safetyLisHash) {
                throw "Safety Lis.Interface.dll failed hash verification."
            }
            if ((Get-FileHashValue -Path $safetyEqp) -ne $safetyEqpHash) {
                throw "Safety Eqp.Interface.dll failed hash verification."
            }
            Replace-FileFromSource `
                -Source $safetyLis `
                -Destination $currentLis `
                -ExpectedHash $safetyLisHash
            Replace-FileFromSource `
                -Source $safetyEqp `
                -Destination $currentEqp `
                -ExpectedHash $safetyEqpHash
            Write-Host "Automatic recovery completed."
        }
        catch {
            Write-Host "Automatic recovery also failed: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "Use this safety backup for manual recovery:"
            Write-Host $safetyBackupDir
        }
    }
}
finally {
    if (-not $NoPause) {
        Write-Host ""
        [void](Read-Host "Press Enter to close")
    }
}

exit $exitCode
