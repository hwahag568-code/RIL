param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir,

    [string]$ConfigPath = ""
)

$ErrorActionPreference = "Stop"
$serverInstalledConfigName = "ril-server-installed.json"
$installPath = [IO.Path]::GetFullPath($InstallDir)
if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
    $componentConfigPath = Join-Path (
        $installPath
    ) $serverInstalledConfigName
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        $ConfigPath = $componentConfigPath
    }
    else {
        $ConfigPath = Join-Path $installPath "ril_config.json"
    }
}
$configPath = [IO.Path]::GetFullPath($ConfigPath)
$env:RIL_CONFIG_PATH = $configPath
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
$previousConfig = $config
$targetConfig = $config
$registrySnapshots = @()
$runtimeDirectoryName = [string](
    $config.installation.server_runtime_directory
)
$schTasksExecutable = Join-Path $env:SystemRoot "System32\schtasks.exe"
$processQueryTimeoutSeconds = 15
$script:processQueryFallbackActive = $false

function Expand-ConfigPath {
    param([string]$Value)
    return [Environment]::ExpandEnvironmentVariables($Value)
}

function Test-SamePath {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    return [IO.Path]::GetFullPath($Left).Equals(
        [IO.Path]::GetFullPath($Right),
        [StringComparison]::OrdinalIgnoreCase
    )
}

function ConvertTo-ArgumentText {
    param([Parameter(Mandatory = $true)][string]$Value)

    # MS C 런타임 규칙으로 인수 하나를 큰따옴표로 감싼다. 큰따옴표 앞과
    # 끝에 있는 백슬래시는 두 배로, 큰따옴표는 \" 로 바꾼다.
    $escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $escaped = [regex]::Replace($escaped, '(\\+)$', '$1$1')
    return "`"$escaped`""
}

function Invoke-SchTasks {
    param([Parameter(Mandatory = $true)][string]$ArgumentLine)

    # Windows PowerShell 5.1은 네이티브 실행파일 인수 안의 큰따옴표를
    # 그대로 전달하지 않으므로(PowerShell 7.3+와 다름) 명령줄을 직접
    # 구성해 두 버전에서 같은 argv가 만들어지게 한다.
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $schTasksExecutable
    $startInfo.Arguments = $ArgumentLine
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($startInfo)
    try {
        $output = $process.StandardOutput.ReadToEnd()
        $errorOutput = $process.StandardError.ReadToEnd()
        $process.WaitForExit()
        return [PSCustomObject]@{
            exit_code = [int]$process.ExitCode
            output = $output + $errorOutput
        }
    }
    finally {
        $process.Dispose()
    }
}

function ConvertTo-ProcessRecord {
    param([Parameter(Mandatory = $true)][object]$Process)

    $path = $null
    try {
        $path = [string]$Process.Path
    }
    catch {
        $path = $null
    }
    if ([string]::IsNullOrWhiteSpace($path)) {
        $path = $null
        $name = [string]$Process.ProcessName + ".exe"
    }
    else {
        $name = [IO.Path]::GetFileName($path)
    }
    # Get-Process에는 CommandLine과 ParentProcessId가 없으므로 $null로 둔다.
    return [PSCustomObject]@{
        ProcessId = [int]$Process.Id
        Name = $name
        ExecutablePath = $path
        ParentProcessId = $null
        CommandLine = $null
    }
}

function Get-ProcessSnapshot {
    # WMI가 응답하지 않아도 무한 대기하지 않도록 제한시간을 두고, 실패하면
    # Get-Process로 대체한다. WMI 실패를 "실행 중인 프로세스 없음"으로
    # 취급하지 않는다.
    if (-not $script:processQueryFallbackActive) {
        try {
            return @(
                Get-CimInstance Win32_Process `
                    -OperationTimeoutSec $processQueryTimeoutSeconds `
                    -ErrorAction Stop
            )
        }
        catch {
            $script:processQueryFallbackActive = $true
        }
    }
    return @(
        Get-Process -ErrorAction SilentlyContinue |
            ForEach-Object { ConvertTo-ProcessRecord -Process $_ }
    )
}

function Get-ProcessById {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if ($ProcessId -le 0) {
        return $null
    }
    if (-not $script:processQueryFallbackActive) {
        try {
            $found = @(
                Get-CimInstance Win32_Process `
                    -Filter "ProcessId = $ProcessId" `
                    -OperationTimeoutSec $processQueryTimeoutSeconds `
                    -ErrorAction Stop
            )
            if ($found.Count -eq 0) {
                return $null
            }
            return $found[0]
        }
        catch {
            $script:processQueryFallbackActive = $true
        }
    }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    return ConvertTo-ProcessRecord -Process $process
}

function Get-InstalledServerConfigPath {
    $componentConfigPath = Join-Path (
        $installPath
    ) $serverInstalledConfigName
    if (Test-Path -LiteralPath $componentConfigPath -PathType Leaf) {
        return $componentConfigPath
    }
    return Join-Path $installPath "ril_config.json"
}

function Start-InstalledServer {
    param([Parameter(Mandatory = $true)][string]$ServerPath)

    $env:RIL_CONFIG_PATH = Get-InstalledServerConfigPath
    Start-Process -FilePath $ServerPath `
        -WorkingDirectory $installPath
}

function Get-ManagedServerFileNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in @($Configs)) {
        if ($null -eq $configValue) {
            continue
        }
        $installation = $configValue.installation
        $names += @(
            [string]$installation.server_executable
            [string]$installation.server_start_script
            [string]$installation.server_start_power_shell_script
            [string]$installation.server_restarter_script
            [string]$installation.server_restarter_power_shell_script
            [string]$installation.server_update_helper_script
            [string]$installation.icon_file
            $serverInstalledConfigName
        )
    }
    return @($names | Sort-Object -Unique)
}

function Get-ManagedRuntimeNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in @($Configs)) {
        if ($null -ne $configValue) {
            $names += [string](
                $configValue.installation.server_runtime_directory
            )
        }
    }
    return @($names | Sort-Object -Unique)
}

function Get-ManagedTaskNames {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    $names = @()
    foreach ($configValue in @($Configs)) {
        if ($null -ne $configValue) {
            $names += @(
                [string]$configValue.installation.server_task_name
                [string](
                    $configValue.installation.server_restarter_task_name
                )
            )
        }
    }
    return @($names | Sort-Object -Unique)
}

function Get-ServerHealthPath {
    param([Parameter(Mandatory = $true)][object]$Config)

    return Join-Path (
        Expand-ConfigPath (
            [string]$Config.installation.runtime_program_data_dir
        )
    ) ([string]$Config.installation.server_health_filename)
}

function Get-ServerUpdateStatePath {
    param([Parameter(Mandatory = $true)][object]$Config)

    return Join-Path (
        Expand-ConfigPath (
            [string]$Config.installation.runtime_program_data_dir
        )
    ) (
        [string]$Config.installation.server_update_state_relative_path
    )
}

function Remove-ServerTaskDefinitions {
    param([Parameter(Mandatory = $true)][object[]]$Configs)

    foreach ($taskName in @(Get-ManagedTaskNames -Configs $Configs)) {
        & "$env:SystemRoot\System32\schtasks.exe" `
            /End /TN $taskName 2>$null |
            Out-Null
        & "$env:SystemRoot\System32\schtasks.exe" `
            /Delete /TN $taskName /F 2>$null |
            Out-Null
    }
}

function Remove-ObsoleteServerTaskDefinitions {
    param(
        [Parameter(Mandatory = $true)][object]$PreviousConfig,
        [Parameter(Mandatory = $true)][object]$TargetConfig
    )

    $targetNames = @(
        Get-ManagedTaskNames -Configs @($TargetConfig)
    )
    foreach ($taskName in @(
        Get-ManagedTaskNames -Configs @($PreviousConfig)
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
            & "$env:SystemRoot\System32\schtasks.exe" `
                /End /TN $taskName 2>$null |
                Out-Null
            & "$env:SystemRoot\System32\schtasks.exe" `
                /Delete /TN $taskName /F 2>$null |
                Out-Null
        }
    }
}

function Restore-RegistrySnapshots {
    param([Parameter(Mandatory = $true)][object[]]$Snapshots)

    foreach ($snapshot in @($Snapshots)) {
        if ($null -eq $snapshot) {
            continue
        }
        $base = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::LocalMachine,
            [Microsoft.Win32.RegistryView]::Registry64
        )
        try {
            $key = $base.CreateSubKey([string]$snapshot.sub_key)
            try {
                foreach ($item in @(
                    [PSCustomObject]@{
                        name = [string]$snapshot.install_name
                        value = $snapshot.install
                    }
                    [PSCustomObject]@{
                        name = [string]$snapshot.version_name
                        value = $snapshot.version
                    }
                    [PSCustomObject]@{
                        name = [string]$snapshot.legacy_install_name
                        value = $snapshot.legacy_install
                    }
                )) {
                    if ([string]::IsNullOrWhiteSpace($item.name)) {
                        continue
                    }
                    if ([bool]$item.value.exists) {
                        $key.SetValue(
                            [string]$item.name,
                            [string]$item.value.value,
                            [Microsoft.Win32.RegistryValueKind]::String
                        )
                    }
                    else {
                        $key.DeleteValue([string]$item.name, $false)
                    }
                }
            }
            finally {
                $key.Dispose()
            }
        }
        finally {
            $base.Dispose()
        }
    }
}

function Import-MigrationDescriptor {
    param([Parameter(Mandatory = $true)][object]$State)

    if (
        $State.PSObject.Properties.Name -contains "previous_config" -and
        $null -ne $State.previous_config
    ) {
        $script:previousConfig = $State.previous_config
    }
    if (
        $State.PSObject.Properties.Name -contains "target_config" -and
        $null -ne $State.target_config
    ) {
        $script:targetConfig = $State.target_config
    }
    if (
        $State.PSObject.Properties.Name -contains "registry_snapshots" -and
        $null -ne $State.registry_snapshots
    ) {
        $script:registrySnapshots = @($State.registry_snapshots)
    }
}

function Write-RecoveryState {
    param(
        [string]$State,
        [object]$PreviousState,
        [string]$CurrentVersion
    )
    $value = [ordered]@{
        schema_version = 1
        state = $State
        current_version = $CurrentVersion
        target_version = [string]$PreviousState.target_version
        allowed_server_version = $CurrentVersion
        backup_directory = [string]$PreviousState.backup_directory
        updated_at = [DateTime]::UtcNow.ToString("o")
        recovered_by = "server_restarter"
        previous_state = [string]$PreviousState.state
        previous_config = $previousConfig
        target_config = $targetConfig
        registry_snapshots = @($registrySnapshots)
    }
    $statePaths = @(
        $statePath
        (Get-ServerUpdateStatePath -Config $previousConfig)
        (Get-ServerUpdateStatePath -Config $targetConfig)
    )
    foreach ($writePath in @($statePaths | Sort-Object -Unique)) {
        $parent = Split-Path -Parent $writePath
        New-Item -ItemType Directory -Path $parent -Force |
            Out-Null
        $temporary = "$writePath.$PID.tmp"
        $value |
            ConvertTo-Json -Depth 10 |
            Set-Content -LiteralPath $temporary -Encoding UTF8
        Move-Item -LiteralPath $temporary `
            -Destination $writePath -Force
    }
}

function New-ServerTasks {
    param([object]$Config = $config)

    $serverTask = [string]$Config.installation.server_task_name
    $restarterTask = [string](
        $Config.installation.server_restarter_task_name
    )
    $startScript = Join-Path $installPath (
        [string]$Config.installation.server_start_script
    )
    $restarterScript = Join-Path $installPath (
        [string]$Config.installation.server_restarter_script
    )
    $hours = [int](
        $Config.installation.server_restarter_interval_hours
    )
    $result = Invoke-SchTasks -ArgumentLine (
        "/Create /SC ONLOGON /TN " +
        (ConvertTo-ArgumentText $serverTask) +
        " /RL HIGHEST /TR " +
        (ConvertTo-ArgumentText "`"$startScript`"") +
        " /F"
    )
    if ($result.exit_code -ne 0) {
        throw "서버 자동실행 작업을 복구하지 못했습니다."
    }
    $result = Invoke-SchTasks -ArgumentLine (
        "/Create /SC HOURLY /MO $hours /TN " +
        (ConvertTo-ArgumentText $restarterTask) +
        " /RL HIGHEST /TR " +
        (ConvertTo-ArgumentText "`"$restarterScript`"") +
        " /F"
    )
    if ($result.exit_code -ne 0) {
        throw "서버 restarter 작업을 복구하지 못했습니다."
    }
}

function Get-TargetProcesses {
    param([object[]]$Configs = @($config))

    $targets = @()
    foreach ($configValue in @($Configs)) {
        if ($null -eq $configValue) {
            continue
        }
        $name = [string]$configValue.installation.server_executable
        $targets += [PSCustomObject]@{
            name = $name
            path = [IO.Path]::GetFullPath(
                (Join-Path $installPath $name)
            )
        }
    }
    return @(
        Get-ProcessSnapshot |
            Where-Object {
                if (-not $_.ExecutablePath) {
                    return $false
                }
                foreach ($target in $targets) {
                    if (
                        $_.Name -ieq $target.name -and
                        (Test-SamePath `
                            -Left ([string]$_.ExecutablePath) `
                            -Right ([string]$target.path))
                    ) {
                        return $true
                    }
                }
                return $false
            }
    )
}

function Complete-RecoveryIfNeeded {
    if ($null -eq $recoveryState) {
        return
    }

    $deadline = (Get-Date).AddSeconds(
        [double]$config.update.server.health_timeout_seconds
    )
    do {
        if (Test-Path -LiteralPath $healthPath -PathType Leaf) {
            try {
                $health = Get-Content -LiteralPath $healthPath -Raw `
                    -Encoding UTF8 |
                    ConvertFrom-Json
                $readyAt = [DateTimeOffset]::Parse(
                    [string]$health.ready_at
                )
                $worker = Get-ProcessById -ProcessId ([int]$health.pid)
                $parent = Get-ProcessById -ProcessId ([int]$health.parent_pid)
                if (
                    [string]$health.status -eq "ready" -and
                    [string]$health.version -eq $recoveryVersion -and
                    $readyAt -ge $recoveryNotBefore -and
                    $worker -and
                    $parent -and
                    $worker.ExecutablePath -and
                    $parent.ExecutablePath -and
                    [IO.Path]::GetFullPath($worker.ExecutablePath) -eq
                        $serverPath -and
                    [IO.Path]::GetFullPath($parent.ExecutablePath) -eq
                        $serverPath -and
                    (
                        $null -eq $worker.ParentProcessId -or
                        [int]$worker.ParentProcessId -eq
                            [int]$health.parent_pid
                    )
                ) {
                    Write-RecoveryState `
                        -State "failed_rolled_back" `
                        -PreviousState $recoveryState `
                        -CurrentVersion $recoveryVersion
                    return
                }
            }
            catch {
            }
        }
        Start-Sleep -Seconds (
            [double](
                $config.update.server.health_poll_interval_seconds
            )
        )
    } while ((Get-Date) -lt $deadline)

    throw "정전 복구 후 기존 서버 health 확인 시간 초과"
}

$serverName = [string]$config.installation.server_executable
$serverPath = [IO.Path]::GetFullPath(
    (Join-Path $installPath $serverName)
)
$programDataRoot = Expand-ConfigPath (
    [string]$config.installation.runtime_program_data_dir
)
$statePath = Join-Path $programDataRoot (
    [string]$config.installation.server_update_state_relative_path
)
$healthPath = Join-Path $programDataRoot (
    [string]$config.installation.server_health_filename
)
$updatesRoot = [IO.Path]::GetFullPath(
    (Join-Path $programDataRoot (
        [string](
            $config.installation.server_update_stage_relative_directory
        )
    ))
)
$updatesRootPrefix = $updatesRoot.TrimEnd(
    [IO.Path]::DirectorySeparatorChar,
    [IO.Path]::AltDirectorySeparatorChar
) + [IO.Path]::DirectorySeparatorChar
$stateStaleTimeout = [double](
    $config.update.server.state_stale_timeout_seconds
)
$updateStates = @(
    "draining",
    "draining_complete",
    "ready_to_install",
    "installing",
    "health_check",
    "rollback",
    "rollback_starting_previous"
)
$recoveryState = $null
$recoveryVersion = $null
$recoveryNotBefore = $null
$updateMutex = [Threading.Mutex]::new(
    $false,
    [string]$config.installation.update_mutex_name
)
$mutexAcquired = $false
try {
    $mutexAcquired = $updateMutex.WaitOne(0)
}
catch [Threading.AbandonedMutexException] {
    $mutexAcquired = $true
}
if (-not $mutexAcquired) {
    $updateMutex.Dispose()
    exit 3
}

try {
if (Test-Path -LiteralPath $statePath) {
    $state = $null
    try {
        $state = Get-Content -LiteralPath $statePath -Raw `
            -Encoding UTF8 |
            ConvertFrom-Json
    }
    catch {
        # 손상된 상태 파일은 서버 복구를 막지 않는다.
    }
    if ($null -ne $state) {
        Import-MigrationDescriptor -State $state
        $isUpdateState = [string]$state.state -in $updateStates
        try {
            $stateUpdatedAt = [DateTimeOffset]::Parse(
                [string]$state.updated_at
            )
            $stateAge = [DateTimeOffset]::UtcNow - $stateUpdatedAt
            $isFresh = (
                $stateAge.TotalSeconds -ge 0 -and
                $stateAge.TotalSeconds -le $stateStaleTimeout
            )
        }
        catch {
            $isFresh = $false
        }
        if ($isUpdateState -and $isFresh) {
            $helperIsRunning = $false
            try {
                $helperPid = [int]$state.helper_pid
                if ($helperPid -gt 0) {
                    $helperNames = @(
                        [string](
                            $previousConfig.installation.
                                server_update_helper_script
                        )
                        [string](
                            $targetConfig.installation.
                                server_update_helper_script
                        )
                    ) | Sort-Object -Unique
                    $helperProcess = Get-ProcessById -ProcessId $helperPid
                    $helperIsRunning = (
                        $helperProcess -and
                        @($helperNames | Where-Object {
                            [string]$helperProcess.CommandLine -like
                                "*$_*"
                        }).Count -gt 0
                    )
                }
            }
            catch {
                $helperIsRunning = $false
            }
            $serverIsStillRunning = @(
                Get-TargetProcesses `
                    -Configs @($previousConfig, $targetConfig)
            ).Count -gt 0
            if ($helperIsRunning -or $serverIsStillRunning) {
                exit 0
            }
        }

        $backupPathValue = [string]$state.backup_directory
        if (
            $isUpdateState -and
            -not [string]::IsNullOrWhiteSpace($backupPathValue)
        ) {
            $backupPath = [IO.Path]::GetFullPath($backupPathValue)
            $backupServer = Join-Path $backupPath (
                [string]$previousConfig.installation.server_executable
            )
            $backupConfig = Join-Path (
                $backupPath
            ) $serverInstalledConfigName
            $backupLegacyConfig = Join-Path $backupPath "ril_config.json"
            $installedLegacyConfig = Join-Path (
                $installPath
            ) "ril_config.json"
            $allowedBackupPrefixes = @($updatesRootPrefix)
            foreach ($descriptorConfig in @(
                $previousConfig
                $targetConfig
            )) {
                $descriptorRoot = [IO.Path]::GetFullPath(
                    (Join-Path (
                        Expand-ConfigPath (
                            [string](
                                $descriptorConfig.installation.
                                    runtime_program_data_dir
                            )
                        )
                    ) (
                        [string](
                            $descriptorConfig.installation.
                                server_update_stage_relative_directory
                        )
                    ))
                )
                $allowedBackupPrefixes += $descriptorRoot.TrimEnd(
                    [IO.Path]::DirectorySeparatorChar,
                    [IO.Path]::AltDirectorySeparatorChar
                ) + [IO.Path]::DirectorySeparatorChar
            }
            $backupIsAllowed = @(
                $allowedBackupPrefixes |
                    Where-Object {
                        $backupPath.StartsWith(
                            $_,
                            [StringComparison]::OrdinalIgnoreCase
                        )
                    }
            ).Count -gt 0
            if (
                -not $backupIsAllowed -or
                -not (Test-Path -LiteralPath $backupServer -PathType Leaf) -or
                (
                    -not (
                        Test-Path -LiteralPath $backupConfig -PathType Leaf
                    ) -and
                    -not (
                        Test-Path -LiteralPath $backupLegacyConfig `
                            -PathType Leaf
                    ) -and
                    -not (
                        Test-Path -LiteralPath $installedLegacyConfig `
                            -PathType Leaf
                    )
                )
            ) {
                # 부분 설치 파일을 실행하지 않도록 상태를 그대로 둔다.
                exit 2
            }

            $migrationConfigs = @($previousConfig, $targetConfig)
            Get-TargetProcesses -Configs $migrationConfigs |
                Sort-Object ProcessId -Descending |
                ForEach-Object {
                    Stop-Process -Id $_.ProcessId -Force `
                        -ErrorAction SilentlyContinue
                }
            foreach ($runtimeName in @(
                Get-ManagedRuntimeNames -Configs $migrationConfigs
            )) {
                $installedRuntime = Join-Path $installPath $runtimeName
                if (Test-Path -LiteralPath $installedRuntime) {
                    Remove-Item -LiteralPath $installedRuntime `
                        -Recurse -Force
                }
            }
            foreach ($fileName in @(
                Get-ManagedServerFileNames -Configs $migrationConfigs
            )) {
                $installedFile = Join-Path $installPath $fileName
                if (Test-Path -LiteralPath $installedFile -PathType Leaf) {
                    Remove-Item -LiteralPath $installedFile -Force
                }
            }
            foreach ($runtimeName in @(
                Get-ManagedRuntimeNames -Configs @($previousConfig)
            )) {
                $installedRuntime = Join-Path $installPath $runtimeName
                $backupRuntime = Join-Path $backupPath $runtimeName
                if (
                    Test-Path -LiteralPath $backupRuntime `
                        -PathType Container
                ) {
                    Copy-Item -LiteralPath $backupRuntime `
                        -Destination $installedRuntime -Recurse -Force
                }
            }
            Get-ChildItem -LiteralPath $backupPath -File |
                ForEach-Object {
                    Copy-Item -LiteralPath $_.FullName `
                        -Destination (
                            Join-Path $installPath $_.Name
                        ) -Force
                }

            $configPath = Get-InstalledServerConfigPath
            $env:RIL_CONFIG_PATH = $configPath
            $config = Get-Content -LiteralPath $configPath -Raw `
                -Encoding UTF8 |
                ConvertFrom-Json
            $previousConfig = $config
            $serverName = [string](
                $config.installation.server_executable
            )
            $serverPath = [IO.Path]::GetFullPath(
                (Join-Path $installPath $serverName)
            )
            $healthPath = Get-ServerHealthPath -Config $config
            if (@($registrySnapshots).Count -gt 0) {
                Restore-RegistrySnapshots -Snapshots $registrySnapshots
            }
            else {
                $registryPath = (
                    "HKLM:\" + [string]$config.installation.registry_key
                )
                if (-not (Test-Path -LiteralPath $registryPath)) {
                    New-Item -Path $registryPath -Force | Out-Null
                }
                New-ItemProperty -LiteralPath $registryPath `
                    -Name (
                        [string](
                            $config.installation.
                                server_version_registry_value
                        )
                    ) -Value ([string]$config.release.version) `
                    -PropertyType String -Force |
                    Out-Null
            }
            New-ServerTasks -Config $config
            Remove-ObsoleteServerTaskDefinitions `
                -PreviousConfig $targetConfig `
                -TargetConfig $previousConfig
            $recoveryState = $state
            $recoveryVersion = [string]$config.release.version
            $recoveryNotBefore = [DateTimeOffset]::UtcNow
            Remove-Item -LiteralPath $healthPath -Force `
                -ErrorAction SilentlyContinue
            Write-RecoveryState `
                -State "rollback_starting_previous" `
                -PreviousState $state `
                -CurrentVersion $recoveryVersion
        }
        elseif ($isUpdateState) {
            # 백업이 생기기 전 정전/강제 종료 상태에서도 구버전이
            # target_version 검사에 막히지 않도록 먼저 실행을 허용한다.
            $config = $previousConfig
            $serverName = [string]$config.installation.server_executable
            $serverPath = [IO.Path]::GetFullPath(
                (Join-Path $installPath $serverName)
            )
            $healthPath = Get-ServerHealthPath -Config $config
            if (@($registrySnapshots).Count -gt 0) {
                Restore-RegistrySnapshots -Snapshots $registrySnapshots
            }
            New-ServerTasks -Config $previousConfig
            Remove-ObsoleteServerTaskDefinitions `
                -PreviousConfig $targetConfig `
                -TargetConfig $previousConfig
            $recoveryState = $state
            $recoveryVersion = [string]$config.release.version
            $recoveryNotBefore = [DateTimeOffset]::UtcNow
            Remove-Item -LiteralPath $healthPath -Force `
                -ErrorAction SilentlyContinue
            Write-RecoveryState `
                -State "rollback_starting_previous" `
                -PreviousState $state `
                -CurrentVersion $recoveryVersion
        }
    }
}

try {
    $all = @(Get-TargetProcesses)
    $parents = @(
        $all |
            Where-Object {
                [string]$_.CommandLine -notmatch "--multiprocessing-fork"
            }
    )

    if ($null -ne $recoveryState -and $parents.Count -gt 0) {
        # stale update 상태를 복구할 때 기존 서버가 살아 있으면
        # 방금 지운 health 파일을 다시 만들 수 있도록 완전 재시작한다.
        $all |
            Sort-Object ProcessId -Descending |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force `
                    -ErrorAction SilentlyContinue
            }
        Start-Sleep -Milliseconds 500
        Start-InstalledServer -ServerPath $serverPath
        Complete-RecoveryIfNeeded
        exit 0
    }

    if ($parents.Count -eq 0) {
        $all |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force `
                    -ErrorAction SilentlyContinue
            }
        Start-Sleep -Milliseconds 500
        Start-InstalledServer -ServerPath $serverPath
        Complete-RecoveryIfNeeded
        exit 0
    }

    $known = New-Object "System.Collections.Generic.HashSet[int]"
    $parents |
        ForEach-Object {
            [void]$known.Add([int]$_.ProcessId)
        }
    do {
        $added = $false
        foreach ($process in $all) {
            if (
                -not $known.Contains([int]$process.ProcessId) -and
                $known.Contains([int]$process.ParentProcessId)
            ) {
                [void]$known.Add([int]$process.ProcessId)
                $added = $true
            }
        }
    } while ($added)

    $all |
        Where-Object {
            -not $known.Contains([int]$_.ProcessId)
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force `
                -ErrorAction SilentlyContinue
        }
    Complete-RecoveryIfNeeded
}
catch {
    if ($null -ne $recoveryState) {
        throw
    }
    Start-InstalledServer -ServerPath $serverPath
}
}
finally {
    if ($mutexAcquired) {
        try {
            $updateMutex.ReleaseMutex()
        }
        catch {
        }
    }
    $updateMutex.Dispose()
}
