param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("ManualTransactional", "UpdatePayload")]
    [string]$Mode,

    [Parameter(Mandatory = $true)]
    [string]$InstallDir,

    [Parameter(Mandatory = $true)]
    [string]$PayloadDir,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$TargetVersion
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0
$serverInstalledConfigName = "ril-server-installed.json"

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    return Get-Content -LiteralPath $LiteralPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
}

function Test-ObjectMember {
    param(
        [Parameter(Mandatory = $true)][object]$Value,
        [Parameter(Mandatory = $true)][string]$Name
    )

    if ($Value -is [System.Collections.IDictionary]) {
        return $Value.Contains($Name)
    }
    return $null -ne $Value.PSObject.Properties[$Name]
}

function Write-JsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][object]$Value
    )

    $parent = Split-Path -Parent $LiteralPath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$LiteralPath.$PID.tmp"
    $Value |
        ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $LiteralPath -Force
}

function Expand-ConfigPath {
    param([Parameter(Mandatory = $true)][string]$Value)

    return [Environment]::ExpandEnvironmentVariables($Value)
}

function Get-InstalledServerConfigPath {
    param([Parameter(Mandatory = $true)][string]$InstalledPath)

    $componentConfigPath = Join-Path (
        [IO.Path]::GetFullPath($InstalledPath)
    ) $serverInstalledConfigName
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        return $componentConfigPath
    }
    return Join-Path ([IO.Path]::GetFullPath($InstalledPath)) "ril_config.json"
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )

    return [IO.Path]::GetFullPath($Left).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar
    ).Equals(
        [IO.Path]::GetFullPath($Right).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar
        ),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-SafeLeafName {
    param(
        [Parameter(Mandatory = $true)][string]$Value,
        [Parameter(Mandatory = $true)][string]$Label
    )

    if (
        [string]::IsNullOrWhiteSpace($Value) -or
        [IO.Path]::GetFileName($Value) -ne $Value -or
        $Value -in @(".", "..")
    ) {
        throw "$Label 값은 단일 파일 또는 폴더 이름이어야 합니다: $Value"
    }
}

function Assert-SafeInstallPath {
    param([Parameter(Mandatory = $true)][string]$Value)

    $full = [IO.Path]::GetFullPath($Value)
    $root = [IO.Path]::GetPathRoot($full)
    if (
        [string]::IsNullOrWhiteSpace($full) -or
        (Test-SamePath -Left $full -Right $root)
    ) {
        throw "안전하지 않은 서버 설치 경로입니다: $full"
    }
    return $full
}

function Get-ServerFileNames {
    param([Parameter(Mandatory = $true)][object]$Config)

    $installation = $Config.installation
    $names = @(
        [string]$installation.server_executable
        [string]$installation.server_start_script
        [string]$installation.server_start_power_shell_script
        [string]$installation.server_restarter_script
        [string]$installation.server_restarter_power_shell_script
        [string]$installation.server_update_helper_script
        $serverInstalledConfigName
        [string]$installation.icon_file
    )
    foreach ($name in $names) {
        Assert-SafeLeafName -Value $name -Label "서버 payload 파일명"
    }
    return @($names | Sort-Object -Unique)
}

function Get-ServerRuntimeName {
    param([Parameter(Mandatory = $true)][object]$Config)

    $name = [string]$Config.installation.server_runtime_directory
    Assert-SafeLeafName -Value $name -Label "서버 런타임 폴더명"
    return $name
}

function Get-CombinedFileNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in $Configs) {
        if ($null -ne $configValue) {
            $names += @(Get-ServerFileNames -Config $configValue)
        }
    }
    return @($names | Sort-Object -Unique)
}

function Get-CombinedRuntimeNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in $Configs) {
        if ($null -ne $configValue) {
            $names += @(Get-ServerRuntimeName -Config $configValue)
        }
    }
    return @($names | Sort-Object -Unique)
}

function Get-ServerTaskNames {
    param([Parameter(Mandatory = $true)][object]$Config)

    return @(
        [string]$Config.installation.server_task_name
        [string]$Config.installation.server_restarter_task_name
    ) | Sort-Object -Unique
}

function Get-CombinedTaskNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in $Configs) {
        if ($null -ne $configValue) {
            $names += @(Get-ServerTaskNames -Config $configValue)
        }
    }
    return @($names | Sort-Object -Unique)
}

function Assert-ServerPayload {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$PayloadPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion
    )

    if ([string]$Config.release.version -ne $ExpectedVersion) {
        throw (
            "설치 대상 버전과 payload 설정 버전이 다릅니다: " +
            "$ExpectedVersion / $($Config.release.version)"
        )
    }
    foreach ($name in @(Get-ServerFileNames -Config $Config)) {
        $source = Join-Path $PayloadPath $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "서버 payload 파일이 없습니다: $source"
        }
    }
    $runtime = Join-Path $PayloadPath (
        Get-ServerRuntimeName -Config $Config
    )
    if (-not (Test-Path -LiteralPath $runtime -PathType Container)) {
        throw "서버 payload 런타임 폴더가 없습니다: $runtime"
    }
}

function Get-RegistryKey {
    param(
        [Parameter(Mandatory = $true)][string]$SubKey,
        [Parameter(Mandatory = $true)][bool]$Writable,
        [Parameter(Mandatory = $true)][bool]$Create
    )

    $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,
        [Microsoft.Win32.RegistryView]::Registry64
    )
    try {
        if ($Create) {
            return $base.CreateSubKey($SubKey)
        }
        return $base.OpenSubKey($SubKey, $Writable)
    }
    finally {
        $base.Dispose()
    }
}

function Get-RegistryValueSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$SubKey,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $key = Get-RegistryKey -SubKey $SubKey -Writable $false -Create $false
    if ($null -eq $key) {
        return [ordered]@{
            exists = $false
            value = $null
        }
    }
    try {
        $exists = @($key.GetValueNames()) -contains $Name
        return [ordered]@{
            exists = $exists
            value = if ($exists) {
                [string]$key.GetValue($Name, $null)
            }
            else {
                $null
            }
        }
    }
    finally {
        $key.Dispose()
    }
}

function Get-RegistrySnapshot {
    param([Parameter(Mandatory = $true)][object]$Config)

    $installation = $Config.installation
    $subKey = [string]$installation.registry_key
    return [ordered]@{
        sub_key = $subKey
        install_name = [string](
            $installation.server_install_registry_value
        )
        version_name = [string](
            $installation.server_version_registry_value
        )
        legacy_install_name = [string](
            $installation.legacy_install_registry_value
        )
        install = Get-RegistryValueSnapshot `
            -SubKey $subKey `
            -Name ([string]$installation.server_install_registry_value)
        version = Get-RegistryValueSnapshot `
            -SubKey $subKey `
            -Name ([string]$installation.server_version_registry_value)
        legacy_install = Get-RegistryValueSnapshot `
            -SubKey $subKey `
            -Name ([string]$installation.legacy_install_registry_value)
    }
}

function Get-RegistrySnapshots {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $snapshots = @()
    $seen = @{}
    foreach ($configValue in $Configs) {
        if ($null -eq $configValue) {
            continue
        }
        $snapshot = Get-RegistrySnapshot -Config $configValue
        $identity = (
            ([string]$snapshot.sub_key).ToLowerInvariant() + "|" +
            ([string]$snapshot.install_name).ToLowerInvariant() + "|" +
            ([string]$snapshot.version_name).ToLowerInvariant() + "|" +
            ([string]$snapshot.legacy_install_name).ToLowerInvariant()
        )
        if (-not $seen.ContainsKey($identity)) {
            $seen[$identity] = $true
            $snapshots += $snapshot
        }
    }
    return @($snapshots)
}

function Set-ServerRegistry {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath,
        [Parameter(Mandatory = $true)][string]$Version
    )

    $installation = $Config.installation
    $key = Get-RegistryKey `
        -SubKey ([string]$installation.registry_key) `
        -Writable $true `
        -Create $true
    try {
        $key.SetValue(
            [string]$installation.server_install_registry_value,
            $InstalledPath,
            [Microsoft.Win32.RegistryValueKind]::String
        )
        $key.SetValue(
            [string]$installation.server_version_registry_value,
            $Version,
            [Microsoft.Win32.RegistryValueKind]::String
        )
        $key.SetValue(
            [string]$installation.legacy_install_registry_value,
            $InstalledPath,
            [Microsoft.Win32.RegistryValueKind]::String
        )
    }
    finally {
        $key.Dispose()
    }
}

function Restore-RegistrySnapshot {
    param([Parameter(Mandatory = $true)][object]$Snapshot)

    $key = Get-RegistryKey `
        -SubKey ([string]$Snapshot.sub_key) `
        -Writable $true `
        -Create $true
    try {
        $items = @(
            [PSCustomObject]@{
                name = [string]$Snapshot.install_name
                snapshot = $Snapshot.install
            }
            [PSCustomObject]@{
                name = [string]$Snapshot.version_name
                snapshot = $Snapshot.version
            }
        )
        if (Test-ObjectMember `
            -Value $Snapshot `
            -Name "legacy_install_name") {
            $items += [PSCustomObject]@{
                name = [string]$Snapshot.legacy_install_name
                snapshot = $Snapshot.legacy_install
            }
        }
        foreach ($item in $items) {
            $name = [string]$item.name
            $valueSnapshot = $item.snapshot
            if ([bool]$valueSnapshot.exists) {
                $key.SetValue(
                    $name,
                    [string]$valueSnapshot.value,
                    [Microsoft.Win32.RegistryValueKind]::String
                )
            }
            else {
                $key.DeleteValue($name, $false)
            }
        }
    }
    finally {
        $key.Dispose()
    }
}

function Restore-RegistrySnapshots {
    param([Parameter(Mandatory = $true)][object[]]$Snapshots)

    foreach ($snapshot in @($Snapshots)) {
        if ($null -ne $snapshot) {
            Restore-RegistrySnapshot -Snapshot $snapshot
        }
    }
}

function Remove-OldServerRegistryValues {
    param(
        [Parameter(Mandatory = $true)][object]$PreviousConfig,
        [Parameter(Mandatory = $true)][object]$TargetConfig
    )

    $targetInstallation = $TargetConfig.installation
    $targetSubKey = [string]$targetInstallation.registry_key
    $targetNames = @(
        [string]$targetInstallation.server_install_registry_value
        [string]$targetInstallation.server_version_registry_value
        [string]$targetInstallation.legacy_install_registry_value
    )
    $previousInstallation = $PreviousConfig.installation
    $previousSubKey = [string]$previousInstallation.registry_key
    $key = Get-RegistryKey `
        -SubKey $previousSubKey `
        -Writable $true `
        -Create $false
    if ($null -eq $key) {
        return
    }
    try {
        foreach ($name in @(
            [string]$previousInstallation.server_install_registry_value
            [string]$previousInstallation.server_version_registry_value
            [string]$previousInstallation.legacy_install_registry_value
        ) | Sort-Object -Unique) {
            $usedByTarget = (
                $previousSubKey.Equals(
                    $targetSubKey,
                    [StringComparison]::OrdinalIgnoreCase
                ) -and
                @($targetNames | Where-Object {
                    $_.Equals(
                        $name,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                }).Count -gt 0
            )
            if (-not $usedByTarget) {
                $key.DeleteValue($name, $false)
            }
        }
    }
    finally {
        $key.Dispose()
    }
}

function Export-ExistingTask {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $xml = @(
        & "$env:SystemRoot\System32\schtasks.exe" `
            /Query /TN $TaskName /XML 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    [IO.File]::WriteAllLines(
        $Destination,
        [string[]]$xml,
        [Text.Encoding]::Unicode
    )
    return $true
}

function Stop-TaskInstance {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    & "$env:SystemRoot\System32\schtasks.exe" `
        /End /TN $TaskName 2>$null |
        Out-Null
}

function Remove-TaskDefinition {
    param([Parameter(Mandatory = $true)][string]$TaskName)

    & "$env:SystemRoot\System32\schtasks.exe" `
        /Delete /TN $TaskName /F 2>$null |
        Out-Null
}

function Remove-ServerTaskDefinitions {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    foreach ($taskName in @(Get-CombinedTaskNames -Configs $Configs)) {
        Stop-TaskInstance -TaskName $taskName
        Remove-TaskDefinition -TaskName $taskName
    }
}

function Remove-ObsoleteServerTaskDefinitions {
    param(
        [Parameter(Mandatory = $true)][object]$PreviousConfig,
        [Parameter(Mandatory = $true)][object]$TargetConfig
    )

    $targetNames = @(
        Get-ServerTaskNames -Config $TargetConfig
    )
    foreach ($taskName in @(
        Get-ServerTaskNames -Config $PreviousConfig
    )) {
        $keptByTarget = @(
            $targetNames |
                Where-Object {
                    $_.Equals(
                        $taskName,
                        [StringComparison]::OrdinalIgnoreCase
                    )
                }
        ).Count -gt 0
        if (-not $keptByTarget) {
            Stop-TaskInstance -TaskName $taskName
            Remove-TaskDefinition -TaskName $taskName
        }
    }
}

function Restore-TaskSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][bool]$Existed,
        [Parameter(Mandatory = $true)][string]$XmlPath
    )

    Remove-TaskDefinition -TaskName $TaskName
    if (-not $Existed) {
        return
    }
    if (-not (Test-Path -LiteralPath $XmlPath -PathType Leaf)) {
        throw "예약 작업 복원 XML이 없습니다: $XmlPath"
    }
    & "$env:SystemRoot\System32\schtasks.exe" `
        /Create /TN $TaskName /XML $XmlPath /F |
        Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "기존 예약 작업을 복원하지 못했습니다: $TaskName"
    }
}

function New-ServerTasks {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    $installation = $Config.installation
    $serverTask = [string]$installation.server_task_name
    $restarterTask = [string]$installation.server_restarter_task_name
    $startScript = Join-Path $InstalledPath (
        [string]$installation.server_start_script
    )
    $restarterScript = Join-Path $InstalledPath (
        [string]$installation.server_restarter_script
    )
    $hours = [int]$installation.server_restarter_interval_hours

    & "$env:SystemRoot\System32\schtasks.exe" /Create `
        /SC ONLOGON /TN $serverTask /RL HIGHEST `
        /TR "`"$startScript`"" /F |
        Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "서버 자동실행 작업을 만들지 못했습니다."
    }
    & "$env:SystemRoot\System32\schtasks.exe" /Create `
        /SC HOURLY /MO $hours /TN $restarterTask /RL HIGHEST `
        /TR "`"$restarterScript`"" /F |
        Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "서버 restarter 작업을 만들지 못했습니다."
    }
}

function Get-InstalledServerProcesses {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    $serverName = [string]$Config.installation.server_executable
    $serverPath = [IO.Path]::GetFullPath(
        (Join-Path $InstalledPath $serverName)
    )
    return @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -ieq $serverName -and
                $_.ExecutablePath -and
                (Test-SamePath `
                    -Left ([string]$_.ExecutablePath) `
                    -Right $serverPath)
            }
    )
}

function Stop-InstalledServer {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    $installation = $Config.installation
    foreach ($taskName in @(
        [string]$installation.server_task_name
        [string]$installation.server_restarter_task_name
    )) {
        Stop-TaskInstance -TaskName $taskName
    }

    @(Get-InstalledServerProcesses `
        -Config $Config `
        -InstalledPath $InstalledPath) |
        Sort-Object ProcessId -Descending |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force `
                -ErrorAction SilentlyContinue
        }

    $deadline = (Get-Date).AddSeconds(
        [double]$Config.update.server.parent_exit_timeout_seconds
    )
    do {
        $remaining = @(
            Get-InstalledServerProcesses `
                -Config $Config `
                -InstalledPath $InstalledPath
        )
        if ($remaining.Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    throw "기존 서버 프로세스를 종료하지 못했습니다."
}

function Stop-InstalledServers {
    param(
        [Parameter(Mandatory = $true)][object[]]$Configs,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    foreach ($taskName in @(Get-CombinedTaskNames -Configs $Configs)) {
        Stop-TaskInstance -TaskName $taskName
    }

    foreach ($configValue in @($Configs)) {
        if ($null -eq $configValue) {
            continue
        }
        @(Get-InstalledServerProcesses `
            -Config $configValue `
            -InstalledPath $InstalledPath) |
            Sort-Object ProcessId -Descending |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force `
                    -ErrorAction SilentlyContinue
            }
    }

    $timeoutConfig = @($Configs | Where-Object { $null -ne $_ })[-1]
    $deadline = (Get-Date).AddSeconds(
        [double]$timeoutConfig.update.server.parent_exit_timeout_seconds
    )
    do {
        $remaining = @()
        foreach ($configValue in @($Configs)) {
            if ($null -ne $configValue) {
                $remaining += @(
                    Get-InstalledServerProcesses `
                        -Config $configValue `
                        -InstalledPath $InstalledPath
                )
            }
        }
        if (@($remaining).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $deadline)

    throw "기존 또는 대상 서버 프로세스를 종료하지 못했습니다."
}

function Remove-ServerPayloadByName {
    param(
        [Parameter(Mandatory = $true)][string[]]$FileNames,
        [Parameter(Mandatory = $true)][string[]]$RuntimeNames,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    foreach ($runtimeName in @($RuntimeNames)) {
        Assert-SafeLeafName -Value $runtimeName -Label "제거 런타임명"
        $installedRuntime = Join-Path $InstalledPath $runtimeName
        if (Test-Path -LiteralPath $installedRuntime) {
            Remove-Item -LiteralPath $installedRuntime -Recurse -Force
        }
    }
    foreach ($fileName in @($FileNames)) {
        Assert-SafeLeafName -Value $fileName -Label "제거 파일명"
        $installedFile = Join-Path $InstalledPath $fileName
        if (Test-Path -LiteralPath $installedFile -PathType Leaf) {
            Remove-Item -LiteralPath $installedFile -Force
        }
    }
}

function Remove-ServerPayload {
    param(
        [Parameter(Mandatory = $true)][object[]]$Configs,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    Remove-ServerPayloadByName `
        -FileNames @(
            Get-CombinedFileNames -Configs $Configs
        ) `
        -RuntimeNames @(
            Get-CombinedRuntimeNames -Configs $Configs
        ) `
        -InstalledPath $InstalledPath
}

function Install-ServerPayload {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][object[]]$RemoveConfigs,
        [Parameter(Mandatory = $true)][string]$InstalledPath,
        [Parameter(Mandatory = $true)][string]$PayloadPath
    )

    New-Item -ItemType Directory -Path $InstalledPath -Force |
        Out-Null
    Remove-ServerPayload `
        -Configs $RemoveConfigs `
        -InstalledPath $InstalledPath

    $runtimeName = Get-ServerRuntimeName -Config $Config
    $sourceRuntime = Join-Path $PayloadPath $runtimeName
    $installedRuntime = Join-Path $InstalledPath $runtimeName
    Copy-Item -LiteralPath $sourceRuntime `
        -Destination $installedRuntime -Recurse -Force
    if (-not (Test-Path -LiteralPath $installedRuntime -PathType Container)) {
        throw "서버 런타임 폴더를 설치하지 못했습니다."
    }

    foreach ($fileName in @(Get-ServerFileNames -Config $Config)) {
        Copy-Item -LiteralPath (Join-Path $PayloadPath $fileName) `
            -Destination (Join-Path $InstalledPath $fileName) -Force
    }
    $serverPath = Join-Path $InstalledPath (
        [string]$Config.installation.server_executable
    )
    if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
        throw "서버 실행파일을 설치하지 못했습니다."
    }
}

function Get-HealthPath {
    param([Parameter(Mandatory = $true)][object]$Config)

    $programData = Expand-ConfigPath (
        [string]$Config.installation.runtime_program_data_dir
    )
    return Join-Path $programData (
        [string]$Config.installation.server_health_filename
    )
}

function Get-UpdateStatePath {
    param([Parameter(Mandatory = $true)][object]$Config)

    $programData = Expand-ConfigPath (
        [string]$Config.installation.runtime_program_data_dir
    )
    return Join-Path $programData (
        [string]$Config.installation.server_update_state_relative_path
    )
}

function Get-ReadyServerHealth {
    param(
        [Parameter(Mandatory = $true)][string]$HealthPath,
        [Parameter(Mandatory = $true)][string]$ServerPath,
        [string]$ExpectedVersion = "",
        [DateTimeOffset]$NotBefore = [DateTimeOffset]::MinValue,
        [int]$ExpectedParentPid = 0
    )

    if (-not (Test-Path -LiteralPath $HealthPath -PathType Leaf)) {
        return $null
    }
    try {
        $health = Read-JsonFile -LiteralPath $HealthPath
        $readyAt = [DateTimeOffset]::Parse([string]$health.ready_at)
        if (
            [string]$health.status -ne "ready" -or
            (
                -not [string]::IsNullOrWhiteSpace($ExpectedVersion) -and
                [string]$health.version -ne $ExpectedVersion
            ) -or
            $readyAt -lt $NotBefore -or
            (
                $ExpectedParentPid -gt 0 -and
                [int]$health.parent_pid -ne $ExpectedParentPid
            )
        ) {
            return $null
        }

        $worker = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $([int]$health.pid)" `
            -ErrorAction SilentlyContinue
        $parent = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $([int]$health.parent_pid)" `
            -ErrorAction SilentlyContinue
        if (
            -not $worker -or
            -not $parent -or
            -not $worker.ExecutablePath -or
            -not $parent.ExecutablePath -or
            -not (
                Test-SamePath `
                    -Left ([string]$worker.ExecutablePath) `
                    -Right $ServerPath
            ) -or
            -not (
                Test-SamePath `
                    -Left ([string]$parent.ExecutablePath) `
                    -Right $ServerPath
            ) -or
            [int]$worker.ParentProcessId -ne [int]$health.parent_pid
        ) {
            return $null
        }
        return $health
    }
    catch {
        return $null
    }
}

function Wait-ServerHealth {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath,
        [Parameter(Mandatory = $true)][string]$ExpectedVersion,
        [Parameter(Mandatory = $true)][DateTimeOffset]$NotBefore,
        [Parameter(Mandatory = $true)][int]$ExpectedParentPid
    )

    $healthPath = Get-HealthPath -Config $Config
    $serverPath = Join-Path $InstalledPath (
        [string]$Config.installation.server_executable
    )
    $deadline = (Get-Date).AddSeconds(
        [double]$Config.update.server.health_timeout_seconds
    )
    do {
        $health = Get-ReadyServerHealth `
            -HealthPath $healthPath `
            -ServerPath $serverPath `
            -ExpectedVersion $ExpectedVersion `
            -NotBefore $NotBefore `
            -ExpectedParentPid $ExpectedParentPid
        if ($null -ne $health) {
            return $health
        }
        Start-Sleep -Seconds (
            [double]$Config.update.server.health_poll_interval_seconds
        )
    } while ((Get-Date) -lt $deadline)

    throw "서버 health 확인 시간 초과: $ExpectedVersion"
}

function Wait-LegacyServerProcess {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath,
        [Parameter(Mandatory = $true)][int]$ExpectedParentPid
    )

    Start-Sleep -Seconds (
        [double]$Config.update.server.health_poll_interval_seconds
    )
    $serverPath = Join-Path $InstalledPath (
        [string]$Config.installation.server_executable
    )
    $process = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $ExpectedParentPid" `
        -ErrorAction SilentlyContinue
    if (
        -not $process -or
        -not $process.ExecutablePath -or
        -not (
            Test-SamePath `
                -Left ([string]$process.ExecutablePath) `
                -Right $serverPath
        )
    ) {
        throw "복원된 단일파일 서버가 실행 상태를 유지하지 못했습니다."
    }
}

function Start-Server {
    param(
        [Parameter(Mandatory = $true)][object]$Config,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    $serverPath = Join-Path $InstalledPath (
        [string]$Config.installation.server_executable
    )
    if (-not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
        throw "실행할 서버 파일이 없습니다: $serverPath"
    }
    $selectedConfigPath = Get-InstalledServerConfigPath `
        -InstalledPath $InstalledPath
    $hadConfigEnvironment = Test-Path Env:RIL_CONFIG_PATH
    $previousConfigEnvironment = $env:RIL_CONFIG_PATH
    try {
        $env:RIL_CONFIG_PATH = $selectedConfigPath
        return Start-Process -FilePath $serverPath `
            -WorkingDirectory $InstalledPath `
            -PassThru
    }
    finally {
        if ($hadConfigEnvironment) {
            $env:RIL_CONFIG_PATH = $previousConfigEnvironment
        }
        else {
            Remove-Item Env:RIL_CONFIG_PATH -ErrorAction SilentlyContinue
        }
    }
}

function Publish-AutomaticMigrationState {
    param(
        [Parameter(Mandatory = $true)][object]$PreviousConfig,
        [Parameter(Mandatory = $true)][object]$TargetConfig,
        [Parameter(Mandatory = $true)][string]$InstalledPath
    )

    $statePath = Get-UpdateStatePath -Config $PreviousConfig
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        throw (
            "자동업데이트 복구 상태가 없어 이름 변경을 안전하게 " +
            "적용할 수 없습니다: $statePath"
        )
    }
    $existingState = Read-JsonFile -LiteralPath $statePath
    $state = [ordered]@{}
    foreach ($property in $existingState.PSObject.Properties) {
        $state[$property.Name] = $property.Value
    }
    $state["install_dir"] = $InstalledPath
    $state["previous_config"] = $PreviousConfig
    $state["target_config"] = $TargetConfig
    $state["managed_file_names"] = @(
        Get-CombinedFileNames -Configs @($PreviousConfig, $TargetConfig)
    )
    $state["managed_runtime_names"] = @(
        Get-CombinedRuntimeNames -Configs @($PreviousConfig, $TargetConfig)
    )
    $state["previous_server_task_name"] = [string](
        $PreviousConfig.installation.server_task_name
    )
    $state["previous_restarter_task_name"] = [string](
        $PreviousConfig.installation.server_restarter_task_name
    )
    $state["new_server_task_name"] = [string](
        $TargetConfig.installation.server_task_name
    )
    $state["new_restarter_task_name"] = [string](
        $TargetConfig.installation.server_restarter_task_name
    )
    $state["registry_snapshots"] = @(
        Get-RegistrySnapshots -Configs @($PreviousConfig, $TargetConfig)
    )
    $state["updated_at"] = [DateTime]::UtcNow.ToString("o")
    $targetStatePath = Get-UpdateStatePath -Config $TargetConfig
    foreach ($writePath in @(
        $statePath
        $targetStatePath
    ) | Sort-Object -Unique) {
        Write-JsonAtomic -LiteralPath $writePath -Value $state
    }
}

function Backup-PreviousInstallation {
    param(
        [Parameter(Mandatory = $true)][object[]]$Configs,
        [Parameter(Mandatory = $true)][string]$InstalledPath,
        [Parameter(Mandatory = $true)][string]$CreatingPath,
        [Parameter(Mandatory = $true)][string]$FinalPath
    )

    Remove-Item -LiteralPath $CreatingPath -Recurse -Force `
        -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $FinalPath) {
        throw "수동 설치 백업 경로가 이미 존재합니다: $FinalPath"
    }
    $payloadBackup = Join-Path $CreatingPath "payload"
    $taskBackup = Join-Path $CreatingPath "tasks"
    New-Item -ItemType Directory -Path $payloadBackup -Force |
        Out-Null
    New-Item -ItemType Directory -Path $taskBackup -Force |
        Out-Null

    $backedUpFiles = @()
    foreach ($fileName in @(Get-CombinedFileNames -Configs $Configs)) {
        $source = Join-Path $InstalledPath $fileName
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            Copy-Item -LiteralPath $source `
                -Destination (Join-Path $payloadBackup $fileName) -Force
            $backedUpFiles += $fileName
        }
    }

    $backedUpRuntimes = @()
    foreach ($runtimeName in @(
        Get-CombinedRuntimeNames -Configs $Configs
    )) {
        $source = Join-Path $InstalledPath $runtimeName
        if (Test-Path -LiteralPath $source -PathType Container) {
            Copy-Item -LiteralPath $source `
                -Destination (Join-Path $payloadBackup $runtimeName) `
                -Recurse -Force
            $backedUpRuntimes += $runtimeName
        }
    }

    $taskSnapshots = @()
    $taskIndex = 0
    foreach ($taskName in @(Get-CombinedTaskNames -Configs $Configs)) {
        $xmlName = "task_$taskIndex.xml"
        $taskSnapshots += [ordered]@{
            name = $taskName
            existed = Export-ExistingTask `
                -TaskName $taskName `
                -Destination (Join-Path $taskBackup $xmlName)
            xml_name = $xmlName
        }
        $taskIndex += 1
    }

    $serverTaskName = [string](
        $Configs[0].installation.server_task_name
    )
    $restarterTaskName = [string](
        $Configs[0].installation.server_restarter_task_name
    )
    $serverTaskSnapshot = @(
        $taskSnapshots |
            Where-Object { [string]$_.name -eq $serverTaskName }
    )[0]
    $restarterTaskSnapshot = @(
        $taskSnapshots |
            Where-Object { [string]$_.name -eq $restarterTaskName }
    )[0]

    Move-Item -LiteralPath $CreatingPath -Destination $FinalPath
    return [ordered]@{
        backed_up_files = @($backedUpFiles)
        backed_up_runtimes = @($backedUpRuntimes)
        task_snapshots = @($taskSnapshots)
        server_task_name = $serverTaskName
        restarter_task_name = $restarterTaskName
        server_task_existed = [bool]$serverTaskSnapshot.existed
        restarter_task_existed = [bool]$restarterTaskSnapshot.existed
    }
}

function Restore-PreviousInstallation {
    param([Parameter(Mandatory = $true)][object]$State)

    $restoreInstallPath = Assert-SafeInstallPath (
        [string]$State.install_dir
    )
    $restoreBackupPath = [IO.Path]::GetFullPath(
        [string]$State.backup_directory
    )
    if (
        -not (
            Test-SamePath `
                -Left $restoreBackupPath `
                -Right $backupDirectory
        ) -or
        -not (Test-Path -LiteralPath $restoreBackupPath -PathType Container)
    ) {
        throw "수동 설치 복구 백업 경로가 올바르지 않습니다."
    }

    $currentConfig = $newConfig
    $currentConfigPath = Get-InstalledServerConfigPath `
        -InstalledPath $restoreInstallPath
    if (Test-Path -LiteralPath $currentConfigPath -PathType Leaf) {
        try {
            $currentConfig = Read-JsonFile `
                -LiteralPath $currentConfigPath
        }
        catch {
        }
    }
    $restoreConfigs = @($currentConfig)
    if (
        (Test-ObjectMember -Value $State -Name "previous_config") -and
        $null -ne $State.previous_config
    ) {
        $restoreConfigs += $State.previous_config
    }
    if (
        (Test-ObjectMember -Value $State -Name "target_config") -and
        $null -ne $State.target_config
    ) {
        $restoreConfigs += $State.target_config
    }
    Stop-InstalledServers `
        -Configs $restoreConfigs `
        -InstalledPath $restoreInstallPath

    Remove-ServerPayloadByName `
        -FileNames @($State.old_file_names) `
        -RuntimeNames @($State.old_runtime_names) `
        -InstalledPath $restoreInstallPath

    $payloadBackup = Join-Path $restoreBackupPath "payload"
    foreach ($fileName in @($State.backed_up_files)) {
        Assert-SafeLeafName -Value $fileName -Label "복구 파일명"
        Copy-Item -LiteralPath (Join-Path $payloadBackup $fileName) `
            -Destination (Join-Path $restoreInstallPath $fileName) -Force
    }
    foreach ($runtimeName in @($State.backed_up_runtimes)) {
        Assert-SafeLeafName -Value $runtimeName -Label "복구 런타임명"
        Copy-Item -LiteralPath (Join-Path $payloadBackup $runtimeName) `
            -Destination (Join-Path $restoreInstallPath $runtimeName) `
            -Recurse -Force
    }

    if (
        (
            Test-ObjectMember `
                -Value $State `
                -Name "registry_snapshots"
        ) -and
        $null -ne $State.registry_snapshots
    ) {
        Restore-RegistrySnapshots -Snapshots @($State.registry_snapshots)
    }
    else {
        Restore-RegistrySnapshot -Snapshot $State.registry
    }
    $taskBackup = Join-Path $restoreBackupPath "tasks"
    if (
        (Test-ObjectMember -Value $State -Name "task_snapshots") -and
        $null -ne $State.task_snapshots
    ) {
        foreach ($taskSnapshot in @($State.task_snapshots)) {
            Restore-TaskSnapshot `
                -TaskName ([string]$taskSnapshot.name) `
                -Existed ([bool]$taskSnapshot.existed) `
                -XmlPath (
                    Join-Path $taskBackup ([string]$taskSnapshot.xml_name)
                )
        }
    }
    else {
        Restore-TaskSnapshot `
            -TaskName ([string]$State.server_task_name) `
            -Existed ([bool]$State.server_task_existed) `
            -XmlPath (Join-Path $taskBackup "server_task.xml")
        Restore-TaskSnapshot `
            -TaskName ([string]$State.restarter_task_name) `
            -Existed ([bool]$State.restarter_task_existed) `
            -XmlPath (Join-Path $taskBackup "restarter_task.xml")
    }
    if (
        (Test-ObjectMember -Value $State -Name "target_config") -and
        $null -ne $State.target_config -and
        (Test-ObjectMember -Value $State -Name "previous_config") -and
        $null -ne $State.previous_config
    ) {
        Remove-ObsoleteServerTaskDefinitions `
            -PreviousConfig $State.target_config `
            -TargetConfig $State.previous_config
    }

    Remove-Item -LiteralPath ([string]$State.previous_health_path) `
        -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath ([string]$State.previous_update_state_path) `
        -Force -ErrorAction SilentlyContinue

    if ([bool]$State.previous_server_exists) {
        $restoredConfigPath = Get-InstalledServerConfigPath `
            -InstalledPath $restoreInstallPath
        if (Test-Path -LiteralPath $restoredConfigPath -PathType Leaf) {
            $restoredConfig = Read-JsonFile `
                -LiteralPath $restoredConfigPath
        }
        elseif ($null -ne $State.previous_config) {
            # 외부 JSON이 없던 기존 단일파일 서버도 동일한 실행명과
            # health 설정으로 복원할 수 있게 설치 전 설정을 보존한다.
            $restoredConfig = $State.previous_config
        }
        else {
            throw "복원할 이전 서버 설정이 없습니다."
        }
        $rollbackStartedAt = [DateTimeOffset]::UtcNow
        $restored = Start-Server `
            -Config $restoredConfig `
            -InstalledPath $restoreInstallPath
        if ([bool]$State.previous_health_verified) {
            Wait-ServerHealth `
                -Config $restoredConfig `
                -InstalledPath $restoreInstallPath `
                -ExpectedVersion ([string]$State.previous_version) `
                -NotBefore $rollbackStartedAt `
                -ExpectedParentPid ([int]$restored.Id) |
                Out-Null
        }
        else {
            Wait-LegacyServerProcess `
                -Config $restoredConfig `
                -InstalledPath $restoreInstallPath `
                -ExpectedParentPid ([int]$restored.Id)
        }
    }
}

function Write-TransactionState {
    param([Parameter(Mandatory = $true)][string]$Phase)

    $transactionState.phase = $Phase
    $transactionState.updated_at = [DateTime]::UtcNow.ToString("o")
    Write-JsonAtomic -LiteralPath $transactionStatePath `
        -Value $transactionState
}

function Recover-InterruptedTransaction {
    if (-not (Test-Path -LiteralPath $transactionStatePath -PathType Leaf)) {
        Remove-Item -LiteralPath $backupCreatingDirectory `
            -Recurse -Force -ErrorAction SilentlyContinue
        return
    }

    $state = Read-JsonFile -LiteralPath $transactionStatePath
    $phase = [string]$state.phase
    if ($phase -in @(
        "committed"
        "failed_rolled_back"
        "backup_complete"
        "preparing"
    )) {
        Remove-Item -LiteralPath $transactionRoot `
            -Recurse -Force -ErrorAction SilentlyContinue
        return
    }
    if ($phase -notin @(
        "installing"
        "health_check"
        "rollback"
        "rollback_failed"
    )) {
        throw "알 수 없는 수동 설치 복구 상태입니다: $phase"
    }

    $script:transactionState = $state
    try {
        Write-TransactionState -Phase "rollback"
        Restore-PreviousInstallation -State $state
        Write-TransactionState -Phase "failed_rolled_back"
        Remove-Item -LiteralPath $transactionRoot `
            -Recurse -Force -ErrorAction SilentlyContinue
    }
    catch {
        Write-TransactionState -Phase "rollback_failed"
        throw
    }
}

$installPath = Assert-SafeInstallPath $InstallDir
$payloadPath = [IO.Path]::GetFullPath($PayloadDir)
$configFile = [IO.Path]::GetFullPath($ConfigPath)
if (-not (Test-Path -LiteralPath $configFile -PathType Leaf)) {
    throw "설치 설정 파일이 없습니다: $configFile"
}
$newConfig = Read-JsonFile -LiteralPath $configFile
Assert-ServerPayload `
    -Config $newConfig `
    -PayloadPath $payloadPath `
    -ExpectedVersion $TargetVersion

if ($Mode -notin @("ManualTransactional", "UpdatePayload")) {
    throw "지원하지 않는 서버 설치 모드입니다: $Mode"
}

if ($Mode -eq "ManualTransactional") {
    $programDataRoot = [IO.Path]::GetFullPath(
        (Expand-ConfigPath (
            [string]$newConfig.installation.runtime_program_data_dir
        ))
    )
    $transactionRoot = Join-Path $programDataRoot (
        [string](
            $newConfig.installation.
                server_manual_transaction_relative_directory
        )
    )
    $transactionStatePath = Join-Path $transactionRoot (
        "server_manual_install_state.json"
    )
    $backupDirectory = Join-Path $transactionRoot "backup"
    $backupCreatingDirectory = "$backupDirectory.creating"
    $transactionState = $null

    # 설치된 config가 정전 중 일부만 기록됐어도 먼저 기존 백업을
    # 복원한 뒤 정상 config를 읽는다.
    Recover-InterruptedTransaction
}

$existingConfig = $null
$installedConfigPath = Get-InstalledServerConfigPath `
    -InstalledPath $installPath
if (Test-Path -LiteralPath $installedConfigPath -PathType Leaf) {
    $existingConfig = Read-JsonFile -LiteralPath $installedConfigPath
}
$descriptorConfigs = @($newConfig)
if ($null -ne $existingConfig) {
    $descriptorConfigs = @($existingConfig, $newConfig)
}
$previousServerConfig = if ($null -ne $existingConfig) {
    $existingConfig
}
else {
    $newConfig
}

if ($Mode -eq "UpdatePayload") {
    Publish-AutomaticMigrationState `
        -PreviousConfig $previousServerConfig `
        -TargetConfig $newConfig `
        -InstalledPath $installPath
    Stop-InstalledServers `
        -Configs $descriptorConfigs `
        -InstalledPath $installPath
    Install-ServerPayload `
        -Config $newConfig `
        -RemoveConfigs $descriptorConfigs `
        -InstalledPath $installPath `
        -PayloadPath $payloadPath
    Set-ServerRegistry `
        -Config $newConfig `
        -InstalledPath $installPath `
        -Version $TargetVersion
    New-ServerTasks -Config $newConfig -InstalledPath $installPath
    Remove-ObsoleteServerTaskDefinitions `
        -PreviousConfig $previousServerConfig `
        -TargetConfig $newConfig
    Remove-OldServerRegistryValues `
        -PreviousConfig $previousServerConfig `
        -TargetConfig $newConfig
    Remove-Item -LiteralPath (Get-HealthPath -Config $newConfig) `
        -Force -ErrorAction SilentlyContinue
    Start-Server -Config $newConfig -InstalledPath $installPath |
        Out-Null
    exit 0
}

$taskConfig = if ($null -ne $existingConfig) {
    $existingConfig
}
else {
    $newConfig
}
$registrySnapshots = @(
    Get-RegistrySnapshots -Configs $descriptorConfigs
)
$registrySnapshot = Get-RegistrySnapshot -Config $previousServerConfig
$previousServerPath = Join-Path $installPath (
    [string]$previousServerConfig.installation.server_executable
)
$previousServerExists = Test-Path -LiteralPath $previousServerPath `
    -PathType Leaf
$previousHealthPath = Get-HealthPath -Config $previousServerConfig
$previousUpdateStatePath = Get-UpdateStatePath `
    -Config $previousServerConfig
$previousVersion = ""
$previousHealthVerified = $false
if ($previousServerExists) {
    $readyHealth = Get-ReadyServerHealth `
        -HealthPath $previousHealthPath `
        -ServerPath $previousServerPath
    if ($null -ne $readyHealth) {
        $previousVersion = [string]$readyHealth.version
        $previousHealthVerified = $true
    }
    elseif ([bool]$registrySnapshot.version.exists) {
        $previousVersion = [string]$registrySnapshot.version.value
    }
    elseif ($null -ne $existingConfig) {
        $previousVersion = [string]$existingConfig.release.version
    }
    if (
        $previousHealthVerified -and
        [string]::IsNullOrWhiteSpace($previousVersion)
    ) {
        throw "기존 서버 버전을 확인할 수 없어 안전한 설치가 불가능합니다."
    }
}

$transactionState = [ordered]@{
    schema_version = 1
    phase = "preparing"
    install_dir = $installPath
    backup_directory = $backupDirectory
    target_version = $TargetVersion
    previous_version = $previousVersion
    previous_server_exists = [bool]$previousServerExists
    previous_health_verified = [bool]$previousHealthVerified
    previous_server_executable = [string](
        $previousServerConfig.installation.server_executable
    )
    previous_config = $previousServerConfig
    target_config = $newConfig
    previous_health_path = $previousHealthPath
    previous_update_state_path = $previousUpdateStatePath
    registry = $registrySnapshot
    registry_snapshots = @($registrySnapshots)
    old_file_names = @(
        Get-CombinedFileNames -Configs $descriptorConfigs
    )
    old_runtime_names = @(
        Get-CombinedRuntimeNames -Configs $descriptorConfigs
    )
    backed_up_files = @()
    backed_up_runtimes = @()
    server_task_name = [string](
        $taskConfig.installation.server_task_name
    )
    restarter_task_name = [string](
        $taskConfig.installation.server_restarter_task_name
    )
    new_server_task_name = [string](
        $newConfig.installation.server_task_name
    )
    new_restarter_task_name = [string](
        $newConfig.installation.server_restarter_task_name
    )
    task_snapshots = @()
    server_task_existed = $false
    restarter_task_existed = $false
    launched_parent_pid = 0
    updated_at = [DateTime]::UtcNow.ToString("o")
}

try {
    Write-TransactionState -Phase "preparing"
    $backup = Backup-PreviousInstallation `
        -Configs $descriptorConfigs `
        -InstalledPath $installPath `
        -CreatingPath $backupCreatingDirectory `
        -FinalPath $backupDirectory
    $transactionState.backed_up_files = @($backup.backed_up_files)
    $transactionState.backed_up_runtimes = @(
        $backup.backed_up_runtimes
    )
    $transactionState.server_task_name = [string](
        $backup.server_task_name
    )
    $transactionState.restarter_task_name = [string](
        $backup.restarter_task_name
    )
    $transactionState.task_snapshots = @($backup.task_snapshots)
    $transactionState.server_task_existed = [bool](
        $backup.server_task_existed
    )
    $transactionState.restarter_task_existed = [bool](
        $backup.restarter_task_existed
    )
    Write-TransactionState -Phase "backup_complete"

    # 이 상태부터 정전 또는 강제 종료 후 다음 수동 설치가 rollback한다.
    Write-TransactionState -Phase "installing"
    Stop-InstalledServers `
        -Configs $descriptorConfigs `
        -InstalledPath $installPath
    Remove-Item -LiteralPath $previousUpdateStatePath `
        -Force -ErrorAction SilentlyContinue
    Install-ServerPayload `
        -Config $newConfig `
        -RemoveConfigs $descriptorConfigs `
        -InstalledPath $installPath `
        -PayloadPath $payloadPath
    Set-ServerRegistry `
        -Config $newConfig `
        -InstalledPath $installPath `
        -Version $TargetVersion
    New-ServerTasks -Config $newConfig -InstalledPath $installPath
    Remove-ObsoleteServerTaskDefinitions `
        -PreviousConfig $previousServerConfig `
        -TargetConfig $newConfig

    $healthPath = Get-HealthPath -Config $newConfig
    Remove-Item -LiteralPath $healthPath -Force `
        -ErrorAction SilentlyContinue
    $installStartedAt = [DateTimeOffset]::UtcNow
    $serverProcess = Start-Server `
        -Config $newConfig `
        -InstalledPath $installPath
    $transactionState.launched_parent_pid = [int]$serverProcess.Id
    Write-TransactionState -Phase "health_check"
    Wait-ServerHealth `
        -Config $newConfig `
        -InstalledPath $installPath `
        -ExpectedVersion $TargetVersion `
        -NotBefore $installStartedAt `
        -ExpectedParentPid ([int]$serverProcess.Id) |
        Out-Null

    Remove-OldServerRegistryValues `
        -PreviousConfig $previousServerConfig `
        -TargetConfig $newConfig
    Write-TransactionState -Phase "committed"
    Remove-Item -LiteralPath $transactionRoot -Recurse -Force `
        -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $failure = $_.Exception.Message
    if (
        $null -eq $transactionState -or
        [string]$transactionState.phase -in @(
            "preparing"
            "backup_complete"
        )
    ) {
        Remove-Item -LiteralPath $transactionRoot -Recurse -Force `
            -ErrorAction SilentlyContinue
        throw
    }
    try {
        Write-TransactionState -Phase "rollback"
        Restore-PreviousInstallation -State $transactionState
        $transactionState.failure = $failure
        Write-TransactionState -Phase "failed_rolled_back"
    }
    catch {
        $rollbackFailure = $_.Exception.Message
        $transactionState.failure = $failure
        $transactionState.rollback_failure = $rollbackFailure
        Write-TransactionState -Phase "rollback_failed"
    }
    throw $failure
}
finally {
    Remove-Item -LiteralPath $backupCreatingDirectory `
        -Recurse -Force -ErrorAction SilentlyContinue
}
