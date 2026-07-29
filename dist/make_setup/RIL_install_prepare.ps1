param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("client", "server")]
    [string]$Component,

    [Parameter(Mandatory = $true)]
    [string]$InstallDir,

    [Parameter(Mandatory = $true)]
    [string]$ConfigPath,

    [string]$InstalledConfigPath = "",

    [string]$DescriptorPath = ""
)

$ErrorActionPreference = "Stop"

function Read-JsonConfig {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    Get-Content -LiteralPath $Path -Raw -Encoding UTF8 |
        ConvertFrom-Json
}

function Assert-LeafName {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if (
        [string]::IsNullOrWhiteSpace($Value) -or
        $Value.IndexOfAny([char[]]@('\', '/')) -ge 0 -or
        $Value.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or
        $Value.TrimEnd([char[]]@(' ', '.')) -ne $Value
    ) {
        throw "잘못된 설치 파일/폴더 이름입니다: $Name"
    }
}

function Assert-TextValue {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if (
        [string]::IsNullOrWhiteSpace($Value) -or
        $Value -match '[\x00-\x1f"]'
    ) {
        throw "잘못된 설치 식별자입니다: $Name"
    }
}

function Get-ConfigDescriptor {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Config
    )

    $installation = $Config.installation
    if ($null -eq $installation) {
        throw "설정에 installation 항목이 없습니다."
    }

    $descriptor = [ordered]@{
        client_executable = [string]$installation.client_executable
        client_runtime_directory = (
            [string]$installation.client_runtime_directory
        )
        legacy_runtime_directory = (
            [string]$installation.legacy_runtime_directory
        )
        client_ui_file = [string]$installation.client_ui_file
        client_update_recovery_task_name = (
            [string]$installation.client_update_recovery_task_name
        )
        client_startup_ready_filename = (
            [string]$installation.client_startup_ready_filename
        )
        registry_key = [string]$installation.registry_key
        client_install_registry_value = (
            [string]$installation.client_install_registry_value
        )
        legacy_install_registry_value = (
            [string]$installation.legacy_install_registry_value
        )
        server_executable = [string]$installation.server_executable
        server_task_name = [string]$installation.server_task_name
        server_restarter_task_name = (
            [string]$installation.server_restarter_task_name
        )
    }

    foreach ($name in @(
        "client_executable",
        "client_runtime_directory",
        "legacy_runtime_directory",
        "client_ui_file",
        "client_startup_ready_filename",
        "server_executable"
    )) {
        Assert-LeafName -Name $name -Value $descriptor[$name]
    }
    foreach ($name in @(
        "client_update_recovery_task_name",
        "registry_key",
        "client_install_registry_value",
        "legacy_install_registry_value",
        "server_task_name",
        "server_restarter_task_name"
    )) {
        Assert-TextValue -Name $name -Value $descriptor[$name]
    }

    $descriptor
}

function Write-MigrationDescriptor {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$OldValues,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$NewValues
    )

    $parent = Split-Path -Parent $Path
    if ($parent) {
        New-Item -ItemType Directory -Path $parent -Force |
            Out-Null
    }

    $lines = New-Object System.Collections.Generic.List[string]
    foreach ($section in @(
        @{ Name = "old"; Values = $OldValues },
        @{ Name = "new"; Values = $NewValues }
    )) {
        [void]$lines.Add("[$($section.Name)]")
        foreach ($key in $section.Values.Keys) {
            [void]$lines.Add("$key=$($section.Values[$key])")
        }
    }

    $temporaryPath = "$Path.$PID.tmp"
    try {
        Set-Content -LiteralPath $temporaryPath -Value $lines `
            -Encoding Unicode
        Move-Item -LiteralPath $temporaryPath `
            -Destination $Path -Force
    }
    finally {
        Remove-Item -LiteralPath $temporaryPath -Force `
            -ErrorAction SilentlyContinue
    }
}

function Read-MigrationDescriptor {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $result = @{
        old = [ordered]@{}
        new = [ordered]@{}
    }
    $sectionName = ""
    $descriptorLines = Get-Content -LiteralPath $Path
    foreach ($line in $descriptorLines) {
        $trimmed = $line.Trim()
        if (
            [string]::IsNullOrWhiteSpace($trimmed) -or
            $trimmed.StartsWith(";")
        ) {
            continue
        }
        if ($trimmed -match '^\[(old|new)\]$') {
            $sectionName = $Matches[1]
            continue
        }
        if (-not $sectionName -or $trimmed -notmatch '^([^=]+)=(.*)$') {
            throw "클라이언트 마이그레이션 설명자가 손상되었습니다."
        }
        $result[$sectionName][$Matches[1].Trim()] = $Matches[2]
    }

    foreach ($sectionName in @("old", "new")) {
        $values = $result[$sectionName]
        foreach ($name in @(
            "client_executable",
            "client_runtime_directory",
            "legacy_runtime_directory",
            "client_ui_file",
            "client_startup_ready_filename",
            "server_executable"
        )) {
            Assert-LeafName -Name "$sectionName.$name" `
                -Value ([string]$values[$name])
        }
        foreach ($name in @(
            "client_update_recovery_task_name",
            "registry_key",
            "client_install_registry_value",
            "legacy_install_registry_value",
            "server_task_name",
            "server_restarter_task_name"
        )) {
            Assert-TextValue -Name "$sectionName.$name" `
                -Value ([string]$values[$name])
        }
    }

    $result
}

function Get-TargetExecutableNames {
    param(
        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$OldValues,

        [Parameter(Mandatory = $true)]
        [System.Collections.IDictionary]$NewValues,

        [Parameter(Mandatory = $true)]
        [ValidateSet("client", "server")]
        [string]$TargetComponent
    )

    $key = if ($TargetComponent -eq "client") {
        "client_executable"
    }
    else {
        "server_executable"
    }

    @(
        [string]$OldValues[$key]
        [string]$NewValues[$key]
    ) |
        Select-Object -Unique
}

$newConfig = Read-JsonConfig -Path $ConfigPath
$newValues = Get-ConfigDescriptor -Config $newConfig

if ($DescriptorPath -and (Test-Path -LiteralPath $DescriptorPath)) {
    $migration = Read-MigrationDescriptor -Path $DescriptorPath
    $oldValues = $migration.old
    $descriptorNewValues = $migration.new
}
else {
    if (
        $InstalledConfigPath -and
        (Test-Path -LiteralPath $InstalledConfigPath)
    ) {
        $oldConfig = Read-JsonConfig -Path $InstalledConfigPath
    }
    else {
        $oldConfig = $newConfig
    }
    $oldValues = Get-ConfigDescriptor -Config $oldConfig
    $descriptorNewValues = $newValues

    if ($DescriptorPath) {
        Write-MigrationDescriptor -Path $DescriptorPath `
            -OldValues $oldValues -NewValues $descriptorNewValues
    }
}

$installPath = [IO.Path]::GetFullPath($InstallDir)

if ($Component -eq "server") {
    # 설치 중 정전되어도 다음 로그인/주기 실행에서 복구할 수 있도록
    # 작업 정의는 유지하고 현재 서버 작업 인스턴스만 종료한다.
    $taskNames = @(
        [string]$oldValues["server_task_name"]
        [string]$descriptorNewValues["server_task_name"]
        [string]$newValues["server_task_name"]
    ) | Select-Object -Unique
    foreach ($serverTaskName in $taskNames) {
        & "$env:SystemRoot\System32\schtasks.exe" `
            /End /TN $serverTaskName 2>$null | Out-Null
    }
    $processExitTimeout = [double](
        $newConfig.update.server.parent_exit_timeout_seconds
    )
}
else {
    $processExitTimeout = [double](
        $newConfig.update.client.process_exit_timeout_seconds
    )
}

$executableNames = @(
    Get-TargetExecutableNames -OldValues $oldValues `
        -NewValues $descriptorNewValues -TargetComponent $Component
    Get-TargetExecutableNames -OldValues $newValues `
        -NewValues $newValues -TargetComponent $Component
) | Select-Object -Unique

$executablePaths = @{}
foreach ($executableName in $executableNames) {
    $executablePath = [IO.Path]::GetFullPath(
        (Join-Path $installPath $executableName)
    )
    $executablePaths[$executablePath.ToLowerInvariant()] = $true
}

function Get-TargetProcesses {
    @(
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                $_.ExecutablePath -and
                $executablePaths.ContainsKey(
                    [IO.Path]::GetFullPath(
                        $_.ExecutablePath
                    ).ToLowerInvariant()
                )
            }
    )
}

$processes = @(Get-TargetProcesses)
$processes |
    Sort-Object ProcessId -Descending |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force `
            -ErrorAction SilentlyContinue
    }

$deadline = (Get-Date).AddSeconds($processExitTimeout)
do {
    $remaining = @(Get-TargetProcesses)
    if ($remaining.Count -eq 0) {
        exit 0
    }
    Start-Sleep -Milliseconds 250
} while ((Get-Date) -lt $deadline)

throw (
    "$Component 실행 프로세스를 종료하지 못했습니다: " +
    (($executablePaths.Keys | Sort-Object) -join ", ")
)
