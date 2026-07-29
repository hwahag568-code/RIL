param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath,

    [Parameter(Mandatory = $true)]
    [string]$TargetVersion,

    [Parameter(Mandatory = $true)]
    [string]$InstallDir,

    [Parameter(Mandatory = $true)]
    [int]$ParentPid,

    [Parameter(Mandatory = $true)]
    [string]$StatePath,

    [Parameter(Mandatory = $true)]
    [string]$HealthPath,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [Parameter(Mandatory = $true)]
    [string]$ReadyPath
)

$ErrorActionPreference = "Stop"
$previousConfig = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
$targetConfig = $previousConfig
$config = $previousConfig
$installPath = [IO.Path]::GetFullPath($InstallDir)
$installer = [IO.Path]::GetFullPath($InstallerPath)
$stageDirectory = Split-Path -Parent $installer
$runtimeDirectoryName = [string](
    $config.installation.server_runtime_directory
)
$backupDirectory = Join-Path $stageDirectory (
    "previous_server_" + $PID + "_" +
    [Guid]::NewGuid().ToString("N")
)
$backupCreatingDirectory = "$backupDirectory.creating"
$previousVersion = [string]$config.release.version
$registrySnapshots = @()
$backupComplete = $false
$mutexAcquired = $false
$readyPublished = $false
$updateMutex = [Threading.Mutex]::new(
    $false,
    [string]$config.installation.update_mutex_name
)
$mutexWaitMilliseconds = [int](
    [double]$config.update.mutex_wait_seconds * 1000
)

try {
    function Write-UpdateState {
        param(
            [string]$State,
            [hashtable]$Details = @{}
        )

        $value = [ordered]@{
            schema_version = 1
            state = $State
            current_version = $previousVersion
            target_version = $TargetVersion
            updated_at = [DateTime]::UtcNow.ToString("o")
            helper_pid = $PID
            install_dir = $installPath
            previous_config = $previousConfig
            target_config = $targetConfig
            registry_snapshots = @($registrySnapshots)
        }
        foreach ($key in $Details.Keys) {
            $value[$key] = $Details[$key]
        }
        $statePaths = @($StatePath)
        if ($null -ne $targetConfig) {
            $targetProgramData = [Environment]::ExpandEnvironmentVariables(
                [string](
                    $targetConfig.installation.runtime_program_data_dir
                )
            )
            $statePaths += Join-Path $targetProgramData (
                [string](
                    $targetConfig.installation.
                        server_update_state_relative_path
                )
            )
        }
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
                "ril_config.json"
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

    function Update-MigrationDescriptor {
        if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
            return
        }
        try {
            $publishedState = Get-Content -LiteralPath $StatePath -Raw `
                -Encoding UTF8 |
                ConvertFrom-Json
            if (
                $publishedState.PSObject.Properties.Name -contains
                    "previous_config" -and
                $null -ne $publishedState.previous_config
            ) {
                $script:previousConfig = $publishedState.previous_config
            }
            if (
                $publishedState.PSObject.Properties.Name -contains
                    "target_config" -and
                $null -ne $publishedState.target_config
            ) {
                $script:targetConfig = $publishedState.target_config
            }
            if (
                $publishedState.PSObject.Properties.Name -contains
                    "registry_snapshots" -and
                $null -ne $publishedState.registry_snapshots
            ) {
                $script:registrySnapshots = @(
                    $publishedState.registry_snapshots
                )
            }
        }
        catch {
            throw "서버 이름 변경 복구 정보를 읽지 못했습니다."
        }
    }

    function New-ServerTasks {
        param([object]$Config = $previousConfig)

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

        & "$env:SystemRoot\System32\schtasks.exe" /Create `
            /SC ONLOGON /TN $serverTask /RL HIGHEST `
            /TR "`"$startScript`"" /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "서버 자동실행 작업 복구 실패"
        }
        & "$env:SystemRoot\System32\schtasks.exe" /Create `
            /SC HOURLY /MO $hours /TN $restarterTask /RL HIGHEST `
            /TR "`"$restarterScript`"" /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "서버 restarter 작업 복구 실패"
        }
    }

    function Set-StagedRecoveryTasks {
        $serverTask = [string]$config.installation.server_task_name
        $restarterTask = [string](
            $config.installation.server_restarter_task_name
        )
        $hours = [int](
            $config.installation.server_restarter_interval_hours
        )
        $stagedRestarter = Join-Path $stageDirectory (
            [string](
                $config.installation.server_restarter_power_shell_script
            )
        )
        if (
            -not (
                Test-Path -LiteralPath $stagedRestarter -PathType Leaf
            )
        ) {
            throw "정전 복구용 restarter가 staging에 없습니다."
        }
        $powerShell = [string](
            $config.update.server.power_shell_executable
        )
        $recoveryCommand = (
            "`"$powerShell`" -NoProfile -NonInteractive " +
            "-ExecutionPolicy Bypass -File `"$stagedRestarter`" " +
            "-InstallDir `"$installPath`" " +
            "-ConfigPath `"$ConfigPath`""
        )

        # 설치 파일이 덮어써지는 중 전원이 꺼져도 staging의 독립
        # restarter와 config로 rollback에 진입할 수 있게 한다.
        & "$env:SystemRoot\System32\schtasks.exe" /Create `
            /SC ONLOGON /TN $serverTask /RL HIGHEST `
            /TR $recoveryCommand /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "서버 정전 복구 작업 준비 실패"
        }
        & "$env:SystemRoot\System32\schtasks.exe" /Create `
            /SC HOURLY /MO $hours /TN $restarterTask /RL HIGHEST `
            /TR $recoveryCommand /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "서버 restarter 정전 복구 작업 준비 실패"
        }
    }

    function Get-InstalledServerProcesses {
        param(
            [object[]]$Configs = @($previousConfig, $targetConfig)
        )

        $targets = @()
        foreach ($configValue in @($Configs)) {
            if ($null -eq $configValue) {
                continue
            }
            $serverName = [string](
                $configValue.installation.server_executable
            )
            $targets += [PSCustomObject]@{
                name = $serverName
                path = [IO.Path]::GetFullPath(
                    (Join-Path $installPath $serverName)
                )
            }
        }
        return @(
            Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
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

    function Stop-InstalledServer {
        param(
            [object[]]$Configs = @($previousConfig, $targetConfig)
        )

        foreach ($taskName in @(Get-ManagedTaskNames -Configs $Configs)) {
            & "$env:SystemRoot\System32\schtasks.exe" `
                /End /TN $taskName 2>$null |
                Out-Null
        }
        Get-InstalledServerProcesses -Configs $Configs |
            Sort-Object ProcessId -Descending |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force `
                    -ErrorAction SilentlyContinue
            }
    }

    function Backup-ServerFiles {
        Remove-Item -LiteralPath $backupCreatingDirectory `
            -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $backupDirectory) {
            throw "현재 업데이트 백업 경로가 이미 존재합니다."
        }
        New-Item -ItemType Directory `
            -Path $backupCreatingDirectory -Force |
            Out-Null
        $fileNames = @(
            [string]$config.installation.server_executable,
            [string]$config.installation.server_start_script,
            [string](
                $config.installation.server_start_power_shell_script
            ),
            [string]$config.installation.server_restarter_script,
            [string](
                $config.installation.server_restarter_power_shell_script
            ),
            [string](
                $config.installation.server_update_helper_script
            ),
            [string]$config.installation.icon_file,
            "ril_config.json"
        )
        foreach ($fileName in $fileNames) {
            $source = Join-Path $installPath $fileName
            if (Test-Path -LiteralPath $source -PathType Leaf) {
                Copy-Item -LiteralPath $source `
                    -Destination (
                        Join-Path $backupCreatingDirectory $fileName
                ) -Force
            }
        }
        $runtimeSource = Join-Path $installPath $runtimeDirectoryName
        $runtimeBackup = Join-Path (
            $backupCreatingDirectory
        ) $runtimeDirectoryName
        if (Test-Path -LiteralPath $runtimeSource -PathType Container) {
            Copy-Item -LiteralPath $runtimeSource `
                -Destination $runtimeBackup -Recurse -Force
            if (
                -not (
                    Test-Path -LiteralPath $runtimeBackup `
                        -PathType Container
                )
            ) {
                throw "기존 서버 런타임을 백업하지 못했습니다."
            }
        }
        $requiredServer = Join-Path $backupCreatingDirectory (
            [string]$config.installation.server_executable
        )
        $requiredConfig = Join-Path $backupCreatingDirectory (
            "ril_config.json"
        )
        if (
            -not (Test-Path -LiteralPath $requiredServer -PathType Leaf) -or
            -not (Test-Path -LiteralPath $requiredConfig -PathType Leaf)
        ) {
            throw "복구에 필요한 기존 서버 파일을 백업하지 못했습니다."
        }
        Move-Item -LiteralPath $backupCreatingDirectory `
            -Destination $backupDirectory
    }

    function Set-ServerRegistryVersion {
        param(
            [string]$Version,
            [object]$Config = $previousConfig
        )
        $registryPath = (
            "HKLM:\" + [string]$Config.installation.registry_key
        )
        if (-not (Test-Path -LiteralPath $registryPath)) {
            New-Item -Path $registryPath -Force | Out-Null
        }
        New-ItemProperty -LiteralPath $registryPath `
            -Name (
                [string](
                    $Config.installation.server_version_registry_value
                )
            ) -Value $Version -PropertyType String -Force |
            Out-Null
    }

    function Get-ServerHealthPath {
        param([Parameter(Mandatory = $true)][object]$Config)

        $programData = [Environment]::ExpandEnvironmentVariables(
            [string]$Config.installation.runtime_program_data_dir
        )
        return Join-Path $programData (
            [string]$Config.installation.server_health_filename
        )
    }

    function Test-ServerHealth {
        param(
            [Parameter(Mandatory = $true)][object]$Config,
            [string]$ExpectedVersion,
            [DateTimeOffset]$NotBefore
        )

        $healthPathValue = Get-ServerHealthPath -Config $Config
        if (-not (Test-Path -LiteralPath $healthPathValue -PathType Leaf)) {
            return $false
        }
        try {
            $health = Get-Content -LiteralPath $healthPathValue -Raw `
                -Encoding UTF8 |
                ConvertFrom-Json
            $readyAt = [DateTimeOffset]::Parse([string]$health.ready_at)
            if (
                [string]$health.status -ne "ready" -or
                [string]$health.version -ne $ExpectedVersion -or
                $readyAt -lt $NotBefore
            ) {
                return $false
            }

            $serverPath = [IO.Path]::GetFullPath(
                (Join-Path $installPath (
                    [string]$Config.installation.server_executable
                ))
            )
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
                [IO.Path]::GetFullPath($worker.ExecutablePath) -ne
                    $serverPath -or
                [IO.Path]::GetFullPath($parent.ExecutablePath) -ne
                    $serverPath -or
                [int]$worker.ParentProcessId -ne [int]$health.parent_pid
            ) {
                return $false
            }

            $registryPath = (
                "HKLM:\" + [string]$Config.installation.registry_key
            )
            $registryValue = Get-ItemPropertyValue `
                -LiteralPath $registryPath `
                -Name (
                        [string](
                            $Config.installation.server_version_registry_value
                        )
                ) -ErrorAction Stop
            return [string]$registryValue -eq $ExpectedVersion
        }
        catch {
            return $false
        }
    }

    function Wait-ServerHealth {
        param(
            [Parameter(Mandatory = $true)][object]$Config,
            [string]$ExpectedVersion,
            [DateTimeOffset]$NotBefore
        )
        $deadline = (Get-Date).AddSeconds(
            [double]$Config.update.server.health_timeout_seconds
        )
        do {
            if (
                Test-ServerHealth `
                    -Config $Config `
                    -ExpectedVersion $ExpectedVersion `
                    -NotBefore $NotBefore
            ) {
                return $true
            }
            Start-Sleep -Seconds (
                [double](
                    $Config.update.server.health_poll_interval_seconds
                )
            )
        } while ((Get-Date) -lt $deadline)
        return $false
    }

    function Restore-PreviousServer {
        param([bool]$RestoreFiles)

        $migrationConfigs = @($previousConfig, $targetConfig)
        Stop-InstalledServer -Configs $migrationConfigs
        if ($RestoreFiles) {
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
                $backupRuntime = Join-Path $backupDirectory $runtimeName
                if (
                    Test-Path -LiteralPath $backupRuntime `
                        -PathType Container
                ) {
                    Copy-Item -LiteralPath $backupRuntime `
                        -Destination $installedRuntime -Recurse -Force
                }
            }
            Get-ChildItem -LiteralPath $backupDirectory -File |
                ForEach-Object {
                    Copy-Item -LiteralPath $_.FullName `
                        -Destination (
                            Join-Path $installPath $_.Name
                        ) -Force
                }
            if (@($registrySnapshots).Count -gt 0) {
                Restore-RegistrySnapshots -Snapshots $registrySnapshots
            }
            else {
                Set-ServerRegistryVersion `
                    -Version $previousVersion `
                    -Config $previousConfig
            }
        }

        New-ServerTasks -Config $previousConfig
        Remove-ObsoleteServerTaskDefinitions `
            -PreviousConfig $targetConfig `
            -TargetConfig $previousConfig
        Write-UpdateState "rollback_starting_previous" @{
            allowed_server_version = $previousVersion
            backup_directory = if ($RestoreFiles) {
                $backupDirectory
            }
            else {
                $null
            }
        }
        if (
            (
                Get-InstalledServerProcesses `
                    -Configs @($previousConfig)
            ).Count -eq 0
        ) {
            $serverPath = Join-Path $installPath (
                [string]$previousConfig.installation.server_executable
            )
            Start-Process -FilePath $serverPath `
                -WorkingDirectory $installPath
        }
    }

    try {
        $mutexAcquired = $updateMutex.WaitOne(
            $mutexWaitMilliseconds
        )
    }
    catch [Threading.AbandonedMutexException] {
        $mutexAcquired = $true
    }
    if (-not $mutexAcquired) {
        throw "다른 설치 작업의 완료 대기시간이 초과됐습니다."
    }

    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "검증된 서버 설치파일이 없습니다: $installer"
    }

    Write-UpdateState "ready_to_install" @{
        installer_path = $installer
        helper_ready_path = $ReadyPath
    }
    $readyParent = Split-Path -Parent $ReadyPath
    New-Item -ItemType Directory -Path $readyParent -Force |
        Out-Null
    $readyTemporary = "$ReadyPath.$PID.tmp"
    [ordered]@{
        schema_version = 1
        helper_pid = $PID
        target_version = $TargetVersion
        ready_at = [DateTime]::UtcNow.ToString("o")
    } |
        ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath $readyTemporary -Encoding UTF8
    Move-Item -LiteralPath $readyTemporary `
        -Destination $ReadyPath -Force
    $readyPublished = $true

    $parentDeadline = (Get-Date).AddSeconds(
        [double]$config.update.server.parent_exit_timeout_seconds
    )
    while (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        if ((Get-Date) -ge $parentDeadline) {
            throw "기존 서버 부모 프로세스 종료 대기시간 초과"
        }
        Start-Sleep -Milliseconds 250
    }

    Backup-ServerFiles
    $backupComplete = $true
    Write-UpdateState "installing" @{
        installer_path = $installer
        backup_directory = $backupDirectory
    }
    Set-StagedRecoveryTasks

    Remove-Item -LiteralPath $HealthPath -Force `
        -ErrorAction SilentlyContinue
    $installStartedAt = [DateTimeOffset]::UtcNow
    $arguments = @(
        $config.update.server.installer_arguments |
            ForEach-Object { [string]$_ }
    )
    # NSIS의 /D= 설치경로 인수는 반드시 마지막이어야 한다.
    $arguments += "/D=$installPath"
    $process = Start-Process -FilePath $installer `
        -ArgumentList $arguments `
        -WorkingDirectory $stageDirectory `
        -WindowStyle Hidden `
        -PassThru
    if (-not $process.WaitForExit(
        [int]$config.update.server.installer_timeout_seconds * 1000
    )) {
        Stop-Process -Id $process.Id -Force `
            -ErrorAction SilentlyContinue
        throw "서버 설치 제한시간 초과"
    }
    Update-MigrationDescriptor
    if ($process.ExitCode -ne 0) {
        throw "서버 설치 실패 (종료 코드: $($process.ExitCode))"
    }

    Write-UpdateState "health_check" @{
        backup_directory = $backupDirectory
        install_started_at = $installStartedAt.ToString("o")
    }
    if (
        -not (
            Wait-ServerHealth -Config $targetConfig `
                -ExpectedVersion $TargetVersion `
                -NotBefore $installStartedAt
        )
    ) {
        throw "새 서버 health 확인 시간 초과"
    }

    $targetHealthPath = Get-ServerHealthPath -Config $targetConfig
    $health = Get-Content -LiteralPath $targetHealthPath -Raw `
        -Encoding UTF8 |
        ConvertFrom-Json
    Write-UpdateState "committed" @{
        current_version = $TargetVersion
        server_pid = [int]$health.parent_pid
        worker_pid = [int]$health.pid
        backup_directory = $backupDirectory
    }
    Remove-Item -LiteralPath $backupDirectory -Recurse -Force `
        -ErrorAction SilentlyContinue
    exit 0
}
catch {
    $failure = $_.Exception.Message
    if (-not $readyPublished) {
        exit 31
    }
    try {
        Update-MigrationDescriptor
        if ($backupComplete) {
            Write-UpdateState "rollback" @{
                error = $failure
                backup_directory = $backupDirectory
            }
        }
        else {
            Write-UpdateState "rollback_starting_previous" @{
                error = $failure
                allowed_server_version = $previousVersion
                backup_directory = $null
            }
        }
        $originalParentStillRunning = @(
            Get-InstalledServerProcesses `
                -Configs @($previousConfig) |
                Where-Object {
                    [int]$_.ProcessId -eq $ParentPid
                }
        ).Count -gt 0
        if ($originalParentStillRunning) {
            Stop-Process -Id $ParentPid -Force `
                -ErrorAction SilentlyContinue
            Start-Sleep -Milliseconds 500
        }
        $rollbackStartedAt = [DateTimeOffset]::UtcNow
        foreach ($healthConfig in @($previousConfig, $targetConfig)) {
            Remove-Item -LiteralPath (
                Get-ServerHealthPath -Config $healthConfig
            ) -Force -ErrorAction SilentlyContinue
        }
        Restore-PreviousServer -RestoreFiles $backupComplete
        if (
            -not (
                Wait-ServerHealth `
                    -Config $previousConfig `
                    -ExpectedVersion $previousVersion `
                    -NotBefore $rollbackStartedAt
            )
        ) {
            throw "복원된 서버 health 확인 시간 초과"
        }
        Write-UpdateState "failed_rolled_back" @{
            error = $failure
            backup_directory = if ($backupComplete) {
                $backupDirectory
            }
            else {
                $null
            }
        }
    }
    catch {
        Write-UpdateState "failed" @{
            error = $failure
            rollback_error = $_.Exception.Message
            backup_directory = if ($backupComplete) {
                $backupDirectory
            }
            else {
                $null
            }
        }
    }
    exit 1
}
finally {
    if ($mutexAcquired -and $null -ne $updateMutex) {
        try {
            $updateMutex.ReleaseMutex()
        }
        catch {
        }
    }
    if ($null -ne $updateMutex) {
        $updateMutex.Dispose()
    }
    if (
        -not [string]::IsNullOrWhiteSpace(
            [string]$readyTemporary
        )
    ) {
        Remove-Item -LiteralPath $readyTemporary -Force `
            -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $backupCreatingDirectory `
        -Recurse -Force -ErrorAction SilentlyContinue
}
