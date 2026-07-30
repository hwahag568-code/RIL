[CmdletBinding()]
param(
    [string]$TargetDir = "C:\Program Files (x86)\LIS_Interface\AU_3",
    [string]$BackupRoot = "C:\LIS_Interface_Backup",
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$exitCode = 0
$backupDir = $null
$replacementStarted = $false

$expected = @{
    CurrentLis = "C466E40210559B7A6CA0CD0FC11EE68CC81F7E5E97A13B8F060FF159CFFF70A2"
    CurrentEqp = "27F032657E1637A6585130B5B991040249BDBAF114B11D87B8F73740AD4EC4E7"
    RestoreLis = "43EE3D4184B6F84640B4D8D1724C4CFC2199E10FC9FF975BB5E3AD803E3E1C3A"
    RestoreEqp = "B1ED74D2175DB2688AB2B64584DD880E8290D0E6A8782FA49BF086FFCD63ABDA"
    Util       = "15045F967F44F3ADE3A5DBD2F9F6389BEE9B1409CA0104F0A5656BC1ECE93A51"
    SQLite     = "3F5704C66BD6B9947E3B34959A514FDA769915FE680D162589023AE6D8CA2CFC"
    X86Interop = "4A07AC9D0767500DE1474399EF397D47D7BDC06293A97461CE9AAD7CDB7A4DA1"
    X64Interop = "92F0AA4AB393885EAA04C94B57DEC61B5CF79221340367FB768463D7BE7AEBDF"
}

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

function Assert-FileHash {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$ExpectedHash,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not found: $Path"
    }

    $actualHash = Get-FileHashValue -Path $Path
    if ($actualHash -ne $ExpectedHash) {
        throw "$Label hash is unexpected.`nExpected: $ExpectedHash`nActual:   $actualHash`nFile: $Path"
    }
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
                $info.OriginalLisSha256 -ne $expected.CurrentLis -or
                $info.OriginalEqpSha256 -ne $expected.CurrentEqp
            ) {
                continue
            }
            if (
                (Get-FileHashValue -Path $backupLis) -ne $expected.CurrentLis -or
                (Get-FileHashValue -Path $backupEqp) -ne $expected.CurrentEqp
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

    $temporary = "$Destination.codex_restore_tmp"
    $replaceBackup = "$Destination.codex_replace_backup"
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Force
    }
    if (Test-Path -LiteralPath $replaceBackup) {
        Remove-Item -LiteralPath $replaceBackup -Force
    }

    Copy-Item -LiteralPath $Source -Destination $temporary -Force
    Assert-FileHash -Path $temporary -ExpectedHash $ExpectedHash -Label "Staged replacement"
    [System.IO.File]::Replace($temporary, $Destination, $replaceBackup)
    Assert-FileHash -Path $Destination -ExpectedHash $ExpectedHash -Label "Installed replacement"
    Remove-Item -LiteralPath $replaceBackup -Force
}

try {
    $target = Get-NormalizedPath -Path $TargetDir
    $backupBase = Get-NormalizedPath -Path $BackupRoot

    if (-not (Test-Path -LiteralPath $target -PathType Container)) {
        throw "AU 3 Order folder was not found: $target"
    }

    $currentLis = Join-Path $target "Lis.Interface.dll"
    $currentEqp = Join-Path $target "Eqp.Interface.dll"
    $restoreLis = Join-Path $target "Lis.Interface_1.dll"
    $restoreEqp = Join-Path $target "Eqp.Interface_260725.dll"
    $utilDll = Join-Path $target "Util.Library.dll"
    $sqliteDll = Join-Path $target "System.Data.SQLite.dll"
    $x86Interop = Join-Path $target "x86\SQLite.Interop.dll"
    $x64Interop = Join-Path $target "x64\SQLite.Interop.dll"

    Write-Host ""
    Write-Host "AU 3 Order coherent-set restore"
    Write-Host "Target: $target"
    Write-Host ""

    Assert-FileHash -Path $restoreLis -ExpectedHash $expected.RestoreLis -Label "Restore Lis.Interface_1.dll"
    Assert-FileHash -Path $restoreEqp -ExpectedHash $expected.RestoreEqp -Label "Restore Eqp.Interface_260725.dll"
    Assert-FileHash -Path $utilDll -ExpectedHash $expected.Util -Label "Existing Util.Library.dll"
    Assert-FileHash -Path $sqliteDll -ExpectedHash $expected.SQLite -Label "Existing System.Data.SQLite.dll"

    Assert-FileHash -Path $x86Interop -ExpectedHash $expected.X86Interop -Label "Existing x86 SQLite.Interop.dll"
    Assert-FileHash -Path $x64Interop -ExpectedHash $expected.X64Interop -Label "Existing x64 SQLite.Interop.dll"

    $sqliteIdentity = [Reflection.AssemblyName]::GetAssemblyName($sqliteDll)
    if ($sqliteIdentity.Version.ToString() -ne "1.0.111.0") {
        throw "Existing SQLite identity is not 1.0.111.0: $($sqliteIdentity.FullName)"
    }

    $activeLisHash = Get-FileHashValue -Path $currentLis
    $activeEqpHash = Get-FileHashValue -Path $currentEqp

    if (
        $activeLisHash -eq $expected.RestoreLis -and
        $activeEqpHash -eq $expected.RestoreEqp
    ) {
        Write-Host "The coherent Order set is already installed."
        Write-Host "No files were changed."
        exit 0
    }

    $validLisHashes = @($expected.CurrentLis, $expected.RestoreLis)
    $validEqpHashes = @($expected.CurrentEqp, $expected.RestoreEqp)
    if ($activeLisHash -notin $validLisHashes) {
        throw "Active Lis.Interface.dll is not a diagnosed file. No change was made.`nActual: $activeLisHash"
    }
    if ($activeEqpHash -notin $validEqpHashes) {
        throw "Active Eqp.Interface.dll is not a diagnosed file. No change was made.`nActual: $activeEqpHash"
    }

    Assert-RelatedProcessesStopped
    Assert-FileUnlocked -Path $currentLis
    Assert-FileUnlocked -Path $currentEqp

    New-Item -ItemType Directory -Path $backupBase -Force | Out-Null
    $isFreshRestore = (
        $activeLisHash -eq $expected.CurrentLis -and
        $activeEqpHash -eq $expected.CurrentEqp
    )
    if ($isFreshRestore) {
        $backupDir = Join-Path $backupBase (
            "AU3_Order_before_coherent_restore_" +
            (Get-Date -Format "yyyyMMdd_HHmmss_fff")
        )
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

        Copy-Item -LiteralPath $currentLis -Destination (Join-Path $backupDir "Lis.Interface.dll")
        Copy-Item -LiteralPath $currentEqp -Destination (Join-Path $backupDir "Eqp.Interface.dll")

        @(
            "Created=$(Get-Date -Format o)"
            "Target=$target"
            "OriginalLisSha256=$activeLisHash"
            "OriginalEqpSha256=$activeEqpHash"
            "RestoreLisSha256=$($expected.RestoreLis)"
            "RestoreEqpSha256=$($expected.RestoreEqp)"
            "DatabaseChanged=False"
            "SQLiteChanged=False"
            "UtilLibraryChanged=False"
        ) | Set-Content -LiteralPath (Join-Path $backupDir "restore-info.txt") -Encoding UTF8

        Assert-FileHash `
            -Path (Join-Path $backupDir "Lis.Interface.dll") `
            -ExpectedHash $expected.CurrentLis `
            -Label "Backed-up Lis.Interface.dll"
        Assert-FileHash `
            -Path (Join-Path $backupDir "Eqp.Interface.dll") `
            -ExpectedHash $expected.CurrentEqp `
            -Label "Backed-up Eqp.Interface.dll"
    }
    else {
        $validatedBackup = Get-ValidatedOriginalBackup -BackupBase $backupBase -Target $target
        if ($null -eq $validatedBackup) {
            throw "A partial prior restore was detected, but its validated original backup was not found. No change was made."
        }
        $backupDir = $validatedBackup.FullName
        Write-Host "Resuming a partial prior restore."
        Write-Host "Validated backup: $backupDir"
    }

    $replacementStarted = $true
    if ($activeLisHash -ne $expected.RestoreLis) {
        Replace-FileFromSource `
            -Source $restoreLis `
            -Destination $currentLis `
            -ExpectedHash $expected.RestoreLis
    }
    if ($activeEqpHash -ne $expected.RestoreEqp) {
        Replace-FileFromSource `
            -Source $restoreEqp `
            -Destination $currentEqp `
            -ExpectedHash $expected.RestoreEqp
    }

    Write-Host ""
    Write-Host "Restore completed successfully."
    Write-Host "Backup: $backupDir"
    Write-Host "Changed: Lis.Interface.dll, Eqp.Interface.dll"
    Write-Host "Unchanged: Interface.db, SQLite, Interop, Util.Library.dll"
    Write-Host ""
    Write-Host "Start AU 3 Order manually and confirm login before using RIL remote login."
}
catch {
    $exitCode = 1
    Write-Host ""
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red

    if ($replacementStarted -and $null -ne $backupDir) {
        Write-Host "Attempting automatic rollback..."
        try {
            $backupLis = Join-Path $backupDir "Lis.Interface.dll"
            $backupEqp = Join-Path $backupDir "Eqp.Interface.dll"
            Assert-FileHash -Path $backupLis -ExpectedHash $expected.CurrentLis -Label "Rollback Lis.Interface.dll"
            Assert-FileHash -Path $backupEqp -ExpectedHash $expected.CurrentEqp -Label "Rollback Eqp.Interface.dll"
            Replace-FileFromSource `
                -Source $backupLis `
                -Destination $currentLis `
                -ExpectedHash $expected.CurrentLis
            Replace-FileFromSource `
                -Source $backupEqp `
                -Destination $currentEqp `
                -ExpectedHash $expected.CurrentEqp
            Write-Host "Automatic rollback completed."
        }
        catch {
            Write-Host "Automatic rollback also failed: $($_.Exception.Message)" -ForegroundColor Red
            Write-Host "Use the backup folder shown below for manual recovery:"
            Write-Host $backupDir
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
