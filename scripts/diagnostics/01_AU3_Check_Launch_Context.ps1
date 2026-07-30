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
    $reportName = "AU3_Launch_Context_{0}" -f $timestamp
    $reportDirectory = New-ReportDirectory `
        -PreferredRoot $OutputRoot `
        -ReportName $reportName

    $summary = @()
    $warnings = @()

    $summary += "AU 3 launch-context diagnostic"
    $summary += "Collected: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $summary += "Computer: $env:COMPUTERNAME"
    $summary += "User: $env:USERDOMAIN\$env:USERNAME"
    $summary += "PowerShell: $($PSVersionTable.PSVersion)"
    $summary += "64-bit OS: $([Environment]::Is64BitOperatingSystem)"
    $summary += "64-bit PowerShell: $([Environment]::Is64BitProcess)"
    $summary += "LOCALAPPDATA: $env:LOCALAPPDATA"

    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    $isAdministrator = $principal.IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
    $summary += "Running as administrator: $isAdministrator"
    $summary += "Process __COMPAT_LAYER: $env:__COMPAT_LAYER"
    $summary += ""

    $installationFolders = @(
        [PSCustomObject]@{
            Area = "AU3_Order"
            Path = "C:\Program Files (x86)\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Area = "AU3_Result"
            Path = "C:\Program Files (x86)\LIS_Interface\AU_3_RSLT"
        }
    )

    $installationRows = @()
    $permissionRows = @()

    foreach ($folder in $installationFolders) {
        if (-not (Test-Path -LiteralPath $folder.Path -PathType Container)) {
            $warnings += "Missing installation folder: $($folder.Path)"
            continue
        }

        try {
            $acl = Get-Acl -LiteralPath $folder.Path
            foreach ($accessRule in $acl.Access) {
                $permissionRows += [PSCustomObject]@{
                    Area              = $folder.Area
                    Folder            = $folder.Path
                    Owner             = $acl.Owner
                    IdentityReference = $accessRule.IdentityReference
                    FileSystemRights  = $accessRule.FileSystemRights
                    AccessControlType = $accessRule.AccessControlType
                    IsInherited       = $accessRule.IsInherited
                }
            }
        }
        catch {
            $warnings += (
                "Cannot read permissions for '{0}': {1}" -f
                $folder.Path, $_.Exception.Message
            )
        }

        try {
            $files = Get-ChildItem -LiteralPath $folder.Path `
                -Recurse -Force -File -ErrorAction Stop

            foreach ($file in $files) {
                $hash = ""
                try {
                    $hash = (
                        Get-FileHash -LiteralPath $file.FullName `
                            -Algorithm SHA256
                    ).Hash
                }
                catch {
                    $warnings += (
                        "Cannot hash '{0}': {1}" -f
                        $file.FullName, $_.Exception.Message
                    )
                }

                $installationRows += [PSCustomObject]@{
                    Area          = $folder.Area
                    Root          = $folder.Path
                    RelativePath  = Get-RelativePath `
                        -Root $folder.Path `
                        -FullName $file.FullName
                    Length        = $file.Length
                    LastWriteTime = $file.LastWriteTime
                    FileVersion   = $file.VersionInfo.FileVersion
                    ProductVersion = $file.VersionInfo.ProductVersion
                    SHA256        = $hash
                }
            }
        }
        catch {
            $warnings += (
                "Cannot inventory '{0}': {1}" -f
                $folder.Path, $_.Exception.Message
            )
        }
    }

    if ($installationRows.Count -gt 0) {
        $installationRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "installation_files.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }

    if ($permissionRows.Count -gt 0) {
        $permissionRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "folder_permissions.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }

    $virtualStoreBase = Join-Path $env:LOCALAPPDATA "VirtualStore"
    $virtualStoreFolders = @(
        [PSCustomObject]@{
            Area = "AU3_Order_VirtualStore_x86"
            Path = Join-Path $virtualStoreBase `
                "Program Files (x86)\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Area = "AU3_Result_VirtualStore_x86"
            Path = Join-Path $virtualStoreBase `
                "Program Files (x86)\LIS_Interface\AU_3_RSLT"
        },
        [PSCustomObject]@{
            Area = "AU3_Order_VirtualStore_x64"
            Path = Join-Path $virtualStoreBase `
                "Program Files\LIS_Interface\AU_3"
        },
        [PSCustomObject]@{
            Area = "AU3_Result_VirtualStore_x64"
            Path = Join-Path $virtualStoreBase `
                "Program Files\LIS_Interface\AU_3_RSLT"
        }
    )

    $virtualStoreRows = @()

    foreach ($folder in $virtualStoreFolders) {
        $exists = Test-Path -LiteralPath $folder.Path -PathType Container
        $summary += "VirtualStore $($folder.Area): $exists"

        if (-not $exists) {
            continue
        }

        try {
            $files = Get-ChildItem -LiteralPath $folder.Path `
                -Recurse -Force -File -ErrorAction Stop

            foreach ($file in $files) {
                $hash = ""
                try {
                    $hash = (
                        Get-FileHash -LiteralPath $file.FullName `
                            -Algorithm SHA256
                    ).Hash
                }
                catch {
                    $warnings += (
                        "Cannot hash '{0}': {1}" -f
                        $file.FullName, $_.Exception.Message
                    )
                }

                $virtualStoreRows += [PSCustomObject]@{
                    Area          = $folder.Area
                    Root          = $folder.Path
                    RelativePath  = Get-RelativePath `
                        -Root $folder.Path `
                        -FullName $file.FullName
                    Length        = $file.Length
                    LastWriteTime = $file.LastWriteTime
                    SHA256        = $hash
                }
            }
        }
        catch {
            $warnings += (
                "Cannot inventory VirtualStore '{0}': {1}" -f
                $folder.Path, $_.Exception.Message
            )
        }
    }

    if ($virtualStoreRows.Count -gt 0) {
        $virtualStoreRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "virtualstore_files.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }

    $shortcutRows = @()
    $shortcutRoots = @(
        [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::DesktopDirectory
        ),
        [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::CommonDesktopDirectory
        ),
        [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::StartMenu
        ),
        [Environment]::GetFolderPath(
            [Environment+SpecialFolder]::CommonStartMenu
        )
    ) |
        Where-Object {
            $_ -and (Test-Path -LiteralPath $_ -PathType Container)
        } |
        Select-Object -Unique

    $wshShell = $null

    try {
        $wshShell = New-Object -ComObject WScript.Shell

        foreach ($shortcutRoot in $shortcutRoots) {
            $shortcutFiles = Get-ChildItem -LiteralPath $shortcutRoot `
                -Recurse -Force -Filter "*.lnk" -File `
                -ErrorAction SilentlyContinue

            foreach ($shortcutFile in $shortcutFiles) {
                $shortcut = $null

                try {
                    $shortcut = $wshShell.CreateShortcut(
                        $shortcutFile.FullName
                    )

                    $searchText = @(
                        $shortcutFile.FullName
                        $shortcut.TargetPath
                        $shortcut.WorkingDirectory
                        $shortcut.Description
                    ) -join "|"

                    if (
                        $searchText -notmatch
                        "(?i)(LIS_Interface|AU[_ -]?3|Ui\.Kumc\.GR\.Interface|AU_RSLT)"
                    ) {
                        continue
                    }

                    $shortcutRows += [PSCustomObject]@{
                        Shortcut         = $shortcutFile.FullName
                        TargetPath       = $shortcut.TargetPath
                        Arguments        = $shortcut.Arguments
                        WorkingDirectory = $shortcut.WorkingDirectory
                        Description      = $shortcut.Description
                        WindowStyle      = $shortcut.WindowStyle
                    }
                }
                catch {
                    $warnings += (
                        "Cannot inspect shortcut '{0}': {1}" -f
                        $shortcutFile.FullName, $_.Exception.Message
                    )
                }
                finally {
                    if (
                        $null -ne $shortcut -and
                        [Runtime.InteropServices.Marshal]::IsComObject(
                            $shortcut
                        )
                    ) {
                        [void][Runtime.InteropServices.Marshal]::
                            ReleaseComObject($shortcut)
                    }
                }
            }
        }
    }
    catch {
        $warnings += "Cannot inspect shortcuts: $($_.Exception.Message)"
    }
    finally {
        if (
            $null -ne $wshShell -and
            [Runtime.InteropServices.Marshal]::IsComObject($wshShell)
        ) {
            [void][Runtime.InteropServices.Marshal]::
                ReleaseComObject($wshShell)
        }
    }

    if ($shortcutRows.Count -gt 0) {
        $shortcutRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "shortcuts.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }
    else {
        $summary += "Matching shortcuts: none found"
    }

    $compatibilityRows = @()
    $compatibilityKeys = @(
        [PSCustomObject]@{
            Scope = "CurrentUser"
            Path = "Registry::HKEY_CURRENT_USER\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
        },
        [PSCustomObject]@{
            Scope = "LocalMachine"
            Path = "Registry::HKEY_LOCAL_MACHINE\Software\Microsoft\Windows NT\CurrentVersion\AppCompatFlags\Layers"
        }
    )

    foreach ($key in $compatibilityKeys) {
        if (-not (Test-Path -LiteralPath $key.Path)) {
            continue
        }

        try {
            $registryKey = Get-Item -LiteralPath $key.Path

            foreach ($valueName in $registryKey.GetValueNames()) {
                $value = $registryKey.GetValue($valueName)

                if ($valueName -notmatch "(?i)\\LIS_Interface\\") {
                    continue
                }

                $compatibilityRows += [PSCustomObject]@{
                    Scope      = $key.Scope
                    Executable = $valueName
                    Layers     = $value
                }
            }
        }
        catch {
            $warnings += (
                "Cannot read compatibility key '{0}': {1}" -f
                $key.Path, $_.Exception.Message
            )
        }
    }

    if ($compatibilityRows.Count -gt 0) {
        $compatibilityRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "compatibility_layers.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }
    else {
        $summary += "LIS_Interface compatibility-layer entries: none found"
    }

    $processRows = @()

    try {
        $processRows = Get-CimInstance Win32_Process `
            -Filter (
                "Name='Ui.Kumc.GR.Interface.exe' OR " +
                "Name='AU_RSLT.exe'"
            ) `
            -ErrorAction Stop |
            Select-Object `
                ProcessId,
                ParentProcessId,
                Name,
                ExecutablePath,
                CommandLine,
                SessionId,
                CreationDate
    }
    catch {
        $warnings += "Cannot query AU processes: $($_.Exception.Message)"
    }

    if (@($processRows).Count -gt 0) {
        $processRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "running_processes.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }
    else {
        $summary += "Running AU Order/Result processes: none found"
    }

    $frameworkRows = @()
    $frameworkRegistryItems = @(
        [PSCustomObject]@{
            Component = ".NET Framework 3.5"
            Path = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\NET Framework Setup\NDP\v3.5"
        },
        [PSCustomObject]@{
            Component = ".NET Framework 4 Full"
            Path = "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full"
        }
    )

    foreach ($item in $frameworkRegistryItems) {
        try {
            $values = Get-ItemProperty -LiteralPath $item.Path `
                -ErrorAction Stop
            $installValue = $values.PSObject.Properties["Install"]
            $versionValue = $values.PSObject.Properties["Version"]
            $releaseValue = $values.PSObject.Properties["Release"]

            $frameworkRows += [PSCustomObject]@{
                Source    = "Registry"
                Component = $item.Component
                State     = if ($installValue) {
                    $installValue.Value
                }
                else {
                    ""
                }
                Version   = if ($versionValue) {
                    $versionValue.Value
                }
                else {
                    ""
                }
                Release   = if ($releaseValue) {
                    $releaseValue.Value
                }
                else {
                    ""
                }
            }
        }
        catch {
            $warnings += (
                "Cannot read framework registry item '{0}': {1}" -f
                $item.Path, $_.Exception.Message
            )
        }
    }

    if ($isAdministrator) {
        foreach ($featureName in @("NetFx3", "NetFx4-AdvSrvs")) {
            try {
                $feature = Get-WindowsOptionalFeature `
                    -Online `
                    -FeatureName $featureName `
                    -ErrorAction Stop

                $frameworkRows += [PSCustomObject]@{
                    Source    = "WindowsOptionalFeature"
                    Component = $featureName
                    State     = $feature.State
                    Version   = ""
                    Release   = ""
                }
            }
            catch {
                $warnings += (
                    "Cannot query Windows feature '{0}': {1}" -f
                    $featureName, $_.Exception.Message
                )
            }
        }
    }
    else {
        $summary += (
            "Windows optional-feature state: not queried " +
            "(administrator rights required)"
        )
    }

    if ($frameworkRows.Count -gt 0) {
        $frameworkRows |
            Export-Csv `
                -LiteralPath (
                    Join-Path $reportDirectory "dotnet_framework.csv"
                ) `
                -NoTypeInformation `
                -Encoding UTF8
    }

    $summary += ""
    $summary += (
        "This script only read system and application metadata. " +
        "It did not stop processes, replace files, or modify the registry."
    )
    $summary |
        Set-Content `
            -LiteralPath (Join-Path $reportDirectory "summary.txt") `
            -Encoding UTF8

    if ($warnings.Count -gt 0) {
        $warnings |
            Set-Content `
                -LiteralPath (Join-Path $reportDirectory "warnings.txt") `
                -Encoding UTF8
    }

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
