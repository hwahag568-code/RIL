#Requires -Version 5.1

[CmdletBinding()]
param(
    [string]$OutputRoot = "C:\AU3_Diagnostic_Output",
    [switch]$NoPause
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function New-ReportDirectory {
    param(
        [string]$PreferredRoot,
        [string]$ReportName
    )

    $candidateRoots = @($PreferredRoot)
    $desktop = [Environment]::GetFolderPath(
        [Environment+SpecialFolder]::DesktopDirectory
    )

    if ($desktop) {
        $fallbackRoot = Join-Path $desktop "AU3_Diagnostic_Output"
        if ($fallbackRoot -ne $PreferredRoot) {
            $candidateRoots += $fallbackRoot
        }
    }

    $lastError = $null

    foreach ($candidateRoot in $candidateRoots) {
        try {
            New-Item -ItemType Directory -Path $candidateRoot -Force |
                Out-Null

            $reportDirectory = Join-Path $candidateRoot $ReportName
            New-Item -ItemType Directory -Path $reportDirectory |
                Out-Null

            return $reportDirectory
        }
        catch {
            $lastError = $_
            Write-Warning (
                "Cannot write to '{0}'. Trying the next location." -f
                $candidateRoot
            )
        }
    }

    throw (
        "Cannot create the diagnostic output directory. Last error: {0}" -f
        $lastError.Exception.Message
    )
}

function Wait-BeforeExit {
    if (-not $NoPause) {
        [void](Read-Host "Press Enter to close this window")
    }
}

function Get-RelativePath {
    param(
        [string]$Root,
        [string]$FullName
    )

    return $FullName.Substring(
        $Root.TrimEnd([char]92).Length
    ).TrimStart([char]92)
}

$scriptExitCode = 0

try {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
    $reportName = "AU_Binary_Comparison_{0}" -f $timestamp
    $reportDirectory = New-ReportDirectory `
        -PreferredRoot $OutputRoot `
        -ReportName $reportName

    $copiedFilesRoot = Join-Path $reportDirectory "files"
    New-Item -ItemType Directory -Path $copiedFilesRoot |
        Out-Null

    $virtualStoreBase = Join-Path $env:LOCALAPPDATA "VirtualStore"
    $sourceGroups = @(
        [PSCustomObject]@{
            Name = "AU1_Order"
            Kind = "Installation"
            Path = "C:\Program Files\LIS_Interface\AU_5822"
        },
        [PSCustomObject]@{
            Name = "AU1_Result"
            Kind = "Installation"
            Path = "C:\Program Files\LIS_Interface\AU_5822_RSLT"
        },
        [PSCustomObject]@{
            Name = "AU2_Order"
            Kind = "Installation"
            Path = "C:\Program Files\LIS_Interface\AU_5832"
        },
        [PSCustomObject]@{
            Name = "AU2_Result"
            Kind = "Installation"
            Path = "C:\Program Files\LIS_Interface\AU_5832_RSLT"
        },
        [PSCustomObject]@{
            Name = "AU3_Order"
            Kind = "Installation"
            Path = "C:\Program Files (x86)\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Name = "AU3_Result"
            Kind = "Installation"
            Path = "C:\Program Files (x86)\LIS_Interface\AU_3_RSLT"
        },
        [PSCustomObject]@{
            Name = "AU3_Order_VirtualStore_x86"
            Kind = "VirtualStore"
            Path = Join-Path $virtualStoreBase `
                "Program Files (x86)\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Name = "AU3_Result_VirtualStore_x86"
            Kind = "VirtualStore"
            Path = Join-Path $virtualStoreBase `
                "Program Files (x86)\LIS_Interface\AU_3_RSLT"
        },
        [PSCustomObject]@{
            Name = "AU3_Order_VirtualStore_x64"
            Kind = "VirtualStore"
            Path = Join-Path $virtualStoreBase `
                "Program Files\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Name = "AU3_Result_VirtualStore_x64"
            Kind = "VirtualStore"
            Path = Join-Path $virtualStoreBase `
                "Program Files\LIS_Interface\AU_3_RSLT"
        }
    )

    $coreFileNames = @(
        "Ui.Kumc.GR.Interface.exe",
        "AU_RSLT.exe",
        "Lis.Interface.dll",
        "Util.Library.dll",
        "System.Data.SQLite.dll",
        "System.Data.SQLite.dll.config",
        "SQLite.Interop.dll",
        "Newtonsoft.Json.dll"
    )

    $diagnosticFileNames = @(
        "limas.ini",
        "Server.xml",
        "AppVer.xml",
        "RIL_server_Log_26.07.28.txt",
        "20260728 11_TimeStamp.log",
        "20260728 12_TimeStamp.log",
        "20260728 13_TimeStamp.log"
    )

    $manifestRows = @()
    $warnings = @()
    $missingFolders = @()
    $copiedCount = 0

    foreach ($group in $sourceGroups) {
        if (-not (Test-Path -LiteralPath $group.Path -PathType Container)) {
            $missingFolders += "$($group.Name): $($group.Path)"
            continue
        }

        try {
            $allFiles = Get-ChildItem -LiteralPath $group.Path `
                -Recurse -Force -File -ErrorAction Stop
        }
        catch {
            $warnings += (
                "Cannot enumerate '{0}': {1}" -f
                $group.Path, $_.Exception.Message
            )
            continue
        }

        foreach ($file in $allFiles) {
            $shouldCopy = (
                ($coreFileNames -contains $file.Name) -or
                ($diagnosticFileNames -contains $file.Name) -or
                ($file.Extension -ieq ".config") -or
                ($file.Name -like "Lis.Interface*.dll") -or
                ($file.Name -like "Eqp.Interface*.dll") -or
                ($file.Name -like "Util.Library*.dll")
            )

            $shouldInventory = (
                $shouldCopy -or
                ($group.Kind -eq "VirtualStore")
            )

            if (-not $shouldInventory) {
                continue
            }

            $relativePath = Get-RelativePath `
                -Root $group.Path `
                -FullName $file.FullName

            $hash = ""
            $copiedHash = ""
            $assemblyIdentity = ""
            $copyStatus = "Not selected"
            $rowError = ""
            $fileLength = ""
            $lastWriteTime = ""
            $fileVersion = ""
            $productVersion = ""

            try {
                $fileLength = $file.Length
                $lastWriteTime = $file.LastWriteTime
                $fileVersion = $file.VersionInfo.FileVersion
                $productVersion = $file.VersionInfo.ProductVersion
            }
            catch {
                $rowError = "Metadata failed: $($_.Exception.Message)"
                $warnings += (
                    "Cannot read metadata for '{0}': {1}" -f
                    $file.FullName, $_.Exception.Message
                )
            }

            try {
                $hash = (
                    Get-FileHash -LiteralPath $file.FullName `
                        -Algorithm SHA256
                ).Hash
            }
            catch {
                $rowError = "Hash failed: $($_.Exception.Message)"
                $warnings += (
                    "Cannot hash '{0}': {1}" -f
                    $file.FullName, $_.Exception.Message
                )
            }

            if ($file.Extension -in @(".dll", ".exe")) {
                try {
                    $assemblyIdentity = (
                        [Reflection.AssemblyName]::GetAssemblyName(
                            $file.FullName
                        )
                    ).FullName
                }
                catch {
                    $assemblyIdentity = ""

                    if ($file.Name -ne "SQLite.Interop.dll") {
                        if ($rowError) {
                            $rowError += " | "
                        }

                        $rowError += (
                            "Assembly identity failed: {0}" -f
                            $_.Exception.Message
                        )
                    }
                }
            }

            if ($shouldCopy) {
                $destination = Join-Path `
                    (Join-Path $copiedFilesRoot $group.Name) `
                    $relativePath

                try {
                    $destinationParent = Split-Path `
                        -Parent $destination
                    New-Item -ItemType Directory `
                        -Path $destinationParent `
                        -Force |
                        Out-Null

                    Copy-Item `
                        -LiteralPath $file.FullName `
                        -Destination $destination `
                        -ErrorAction Stop

                    $copyStatus = "Copied"
                    $copiedCount++

                    try {
                        $copiedHash = (
                            Get-FileHash `
                                -LiteralPath $destination `
                                -Algorithm SHA256
                        ).Hash

                        if ($hash -and $copiedHash -ne $hash) {
                            $copyStatus = "Copied; hash mismatch"
                            $warnings += (
                                "Copied hash mismatch for '{0}'" -f
                                $file.FullName
                            )
                        }
                    }
                    catch {
                        if ($rowError) {
                            $rowError += " | "
                        }

                        $rowError += (
                            "Copied hash failed: {0}" -f
                            $_.Exception.Message
                        )
                    }
                }
                catch {
                    $copyStatus = "Copy failed"

                    if ($rowError) {
                        $rowError += " | "
                    }

                    $rowError += "Copy failed: $($_.Exception.Message)"
                    $warnings += (
                        "Cannot copy '{0}': {1}" -f
                        $file.FullName, $_.Exception.Message
                    )
                }
            }

            $manifestRows += [PSCustomObject]@{
                Group            = $group.Name
                Kind             = $group.Kind
                SourceFolder     = $group.Path
                RelativePath     = $relativePath
                Length           = $fileLength
                LastWriteTime    = $lastWriteTime
                FileVersion      = $fileVersion
                ProductVersion   = $productVersion
                AssemblyIdentity = $assemblyIdentity
                SHA256           = $hash
                CopiedSHA256     = $copiedHash
                CopyStatus       = $copyStatus
                Error            = $rowError
            }
        }
    }

    if ($manifestRows.Count -gt 0) {
        $manifestRows |
            Sort-Object Group, RelativePath |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "file_manifest.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }

    if ($missingFolders.Count -gt 0) {
        $missingFolders |
            Set-Content `
                -LiteralPath (
                    Join-Path $reportDirectory "missing_folders.txt"
                ) `
                -Encoding UTF8
    }

    if ($warnings.Count -gt 0) {
        $warnings |
            Set-Content `
                -LiteralPath (Join-Path $reportDirectory "warnings.txt") `
                -Encoding UTF8
    }

    $summary = @(
        "AU Order/Result diagnostic file collection",
        "Collected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')",
        "Computer: $env:COMPUTERNAME",
        "User: $env:USERDOMAIN\$env:USERNAME",
        "Copied files: $copiedCount",
        "Manifest rows: $($manifestRows.Count)",
        "Missing source folders: $($missingFolders.Count)",
        "",
        (
            "Only executables, selected interface DLLs, SQLite.Interop.dll, " +
            "and .config files were copied."
        ),
        (
            "VirtualStore files were inventoried with SHA-256 hashes; " +
            "non-selected VirtualStore files were not copied."
        ),
        (
            "No source file was changed, no process was stopped, and no " +
            "registry value was modified."
        ),
        (
            "Review copied .config files before sharing the ZIP because " +
            "they may contain connection information."
        )
    )

    $summary |
        Set-Content `
            -LiteralPath (Join-Path $reportDirectory "summary.txt") `
            -Encoding UTF8

    $zipPath = "{0}.zip" -f $reportDirectory
    Compress-Archive `
        -LiteralPath $reportDirectory `
        -DestinationPath $zipPath `
        -CompressionLevel Optimal

    Write-Host ""
    Write-Host "Diagnostic collection completed." -ForegroundColor Green
    Write-Host "ZIP file: $zipPath"
    Write-Host (
        "No AU program file, process, or registry value was changed."
    )
}
catch {
    $scriptExitCode = 1
    Write-Host ""
    Write-Host "Diagnostic collection failed." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
}
finally {
    Wait-BeforeExit
}

if ($scriptExitCode -ne 0) {
    exit $scriptExitCode
}
