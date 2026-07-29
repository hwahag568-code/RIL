param(
    [switch]$InstallDependencies,
    [switch]$SkipPyInstaller,
    [switch]$SkipClientInstaller,
    [switch]$SkipServerInstaller
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$configFile = "ril_config.json"
$config = Get-Content -LiteralPath $configFile -Raw -Encoding UTF8 |
    ConvertFrom-Json
$buildConfig = $config.build
$releaseConfig = $config.release
$artifacts = $config.artifacts
$installation = $config.installation
$updateConfig = $config.update

$appVersion = [string]$releaseConfig.version
$versionParts = $appVersion.Split(".", 2)
$hotfixNumber = if ($versionParts.Count -eq 2) {
    [int]$versionParts[1]
}
else {
    0
}
$legacyVersion = "{0}{1:D2}" -f $versionParts[0], ($hotfixNumber + 1)
$protocolVersion = [int]$releaseConfig.protocol_version
$clientInstallerName = [string]$artifacts.client_installer_filename
$serverInstallerName = (
    [string]$artifacts.server_installer_filename_template
).Replace("{version}", $appVersion)
$serverInstallerFilter = (
    [string]$artifacts.server_installer_filename_template
).Replace("{version}", "*")
$manifestName = [string]$artifacts.manifest_filename
$legacyVersionName = [string]$artifacts.legacy_version_filename
$updateMutexWaitMilliseconds = [int](
    [double]$updateConfig.mutex_wait_seconds * 1000
)
$clientStartupHealthTimeoutMilliseconds = [int](
    [double]$updateConfig.client.startup_health_timeout_seconds * 1000
)
$clientExecutable = [string]$installation.client_executable
$clientBuildName = [IO.Path]::GetFileNameWithoutExtension(
    $clientExecutable
)
$clientBuildDirectory = Join-Path "dist" $clientBuildName
$serverExecutable = [string]$installation.server_executable
$serverBuildName = [IO.Path]::GetFileNameWithoutExtension(
    $serverExecutable
)
$serverBuildDirectory = Join-Path "dist" $serverBuildName
$legacyServerBuildPath = Join-Path "dist" $serverExecutable

$pythonRuntimeJson = python -c (
    "import json, platform, struct, sys; " +
    "print(json.dumps({" +
    "'platform': sys.platform, " +
    "'machine': platform.machine(), " +
    "'python_major_minor': f'{sys.version_info.major}.{sys.version_info.minor}', " +
    "'architecture_bits': struct.calcsize('P') * 8" +
    "}))"
)
if ($LASTEXITCODE -ne 0) {
    throw "Could not inspect the Python build runtime."
}
$pythonRuntime = $pythonRuntimeJson | ConvertFrom-Json
if (
    [string]$pythonRuntime.platform -ne
        [string]$buildConfig.platform -or
    [string]$pythonRuntime.machine -ne
        [string]$buildConfig.machine -or
    [string]$pythonRuntime.python_major_minor -ne
        [string]$buildConfig.python_major_minor -or
    [int]$pythonRuntime.architecture_bits -ne
        [int]$buildConfig.architecture_bits
) {
    throw (
        "Unsupported build runtime. Expected " +
        "$($buildConfig.platform)/$($buildConfig.machine), Python " +
        "$($buildConfig.python_major_minor), " +
        "$($buildConfig.architecture_bits)-bit; got " +
        "$($pythonRuntime.platform)/$($pythonRuntime.machine), Python " +
        "$($pythonRuntime.python_major_minor), " +
        "$($pythonRuntime.architecture_bits)-bit."
    )
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $hash = python -c `
        "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" `
        $LiteralPath
    if ($LASTEXITCODE -ne 0) {
        throw "Could not calculate SHA-256: $LiteralPath"
    }
    return $hash.Trim()
}

if ($InstallDependencies) {
    python -m pip install -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
}

python scripts\release_validation.py write-version-module `
    --version $appVersion `
    --protocol-version $protocolVersion
if ($LASTEXITCODE -ne 0) {
    throw "Could not generate the binary-fixed version module."
}

python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) {
    throw "Tests failed."
}

if (-not $SkipPyInstaller) {
    Remove-Item -LiteralPath $clientBuildDirectory -Recurse -Force `
        -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $serverBuildDirectory -Recurse -Force `
        -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $legacyServerBuildPath -Force `
        -ErrorAction SilentlyContinue

    python -m PyInstaller --noconfirm --clean RIL_client.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Client build failed."
    }

    python -m PyInstaller --noconfirm --clean RIL_server.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Server build failed."
    }

    python scripts\release_validation.py write-build-info `
        --version $appVersion `
        --protocol-version $protocolVersion
    if ($LASTEXITCODE -ne 0) {
        throw "Could not write binary build information."
    }
} else {
    python scripts\release_validation.py verify-build-info `
        --version $appVersion `
        --protocol-version $protocolVersion
    if ($LASTEXITCODE -ne 0) {
        throw "Existing binaries do not match the current source."
    }
}

New-Item -ItemType Directory -Path "release" -Force | Out-Null

if ($SkipClientInstaller -and $SkipServerInstaller) {
    Write-Output "Binary build/validation complete; installer build skipped."
    exit 0
}

if ($SkipClientInstaller -or $SkipServerInstaller) {
    throw "Client and server installers must be built as one release set."
}

$serverInstallerPath = Join-Path "release" $serverInstallerName
Remove-Item -LiteralPath (Join-Path "release" $clientInstallerName) -Force `
    -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath "release" -File `
    -Filter $serverInstallerFilter |
    Remove-Item -Force
Remove-Item -LiteralPath (Join-Path "release" $manifestName) -Force `
    -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path "release" $legacyVersionName) -Force `
    -ErrorAction SilentlyContinue

$commonNsisArgs = @(
    "/INPUTCHARSET",
    "UTF8",
    "/DAPP_VERSION=$appVersion",
    "/DINSTALL_DIR=$($installation.nsis_install_dir)",
    "/DREGISTRY_KEY=$($installation.registry_key)",
    "/DLEGACY_INSTALL_REGISTRY_VALUE=$($installation.legacy_install_registry_value)",
    "/DCONFIG_FILE=$configFile",
    "/DINSTALL_PREPARE_SCRIPT=dist\make_setup\RIL_install_prepare.ps1",
    "/DPOWER_SHELL_EXECUTABLE=$($updateConfig.server.power_shell_executable)",
    "/DICON_FILE=$($installation.icon_file)",
    "/DCLIENT_BUILD_DIRECTORY=$clientBuildDirectory",
    "/DSERVER_BUILD_DIRECTORY=$serverBuildDirectory",
    "/DCLIENT_RUNTIME_DIRECTORY=$($installation.client_runtime_directory)",
    "/DSERVER_RUNTIME_DIRECTORY=$($installation.server_runtime_directory)",
    "/DLEGACY_RUNTIME_DIRECTORY=$($installation.legacy_runtime_directory)",
    "/DUPDATE_MUTEX_NAME=$($installation.update_mutex_name)",
    "/DUPDATE_MUTEX_WAIT_MILLISECONDS=$updateMutexWaitMilliseconds"
)

$clientNsisArgs = @(
    $commonNsisArgs
    "/DCLIENT_INSTALLER_FILENAME=$clientInstallerName"
    "/DCLIENT_INSTALL_REGISTRY_VALUE=$($installation.client_install_registry_value)"
    "/DCLIENT_EXECUTABLE=$($installation.client_executable)"
    "/DCLIENT_UI_FILE=$($installation.client_ui_file)"
    "/DCLIENT_RECOVERY_TASK_NAME=$($installation.client_update_recovery_task_name)"
    "/DCLIENT_STARTUP_READY_FILENAME=$($installation.client_startup_ready_filename)"
    "/DCLIENT_STARTUP_CHECK_SCRIPT=$($installation.client_startup_check_script)"
    "/DCLIENT_STARTUP_HEALTH_TIMEOUT_MILLISECONDS=$clientStartupHealthTimeoutMilliseconds"
    "RIL_Client_Update.nsi"
)
& makensis @clientNsisArgs
if ($LASTEXITCODE -ne 0) {
    throw "Client update installer build failed."
}

$serverNsisArgs = @(
    $commonNsisArgs
    "/DSERVER_INSTALLER_FILENAME=$serverInstallerName"
    "/DSERVER_INSTALL_REGISTRY_VALUE=$($installation.server_install_registry_value)"
    "/DSERVER_VERSION_REGISTRY_VALUE=$($installation.server_version_registry_value)"
    "/DSERVER_EXECUTABLE=$($installation.server_executable)"
    "/DSERVER_START_SCRIPT=$($installation.server_start_script)"
    "/DSERVER_START_PS1=$($installation.server_start_power_shell_script)"
    "/DSERVER_RESTARTER_SCRIPT=$($installation.server_restarter_script)"
    "/DSERVER_RESTARTER_PS1=$($installation.server_restarter_power_shell_script)"
    "/DSERVER_UPDATE_HELPER=$($installation.server_update_helper_script)"
    "/DSERVER_TASK_NAME=$($installation.server_task_name)"
    "/DSERVER_RESTARTER_TASK_NAME=$($installation.server_restarter_task_name)"
    "/DSERVER_RESTARTER_INTERVAL_HOURS=$($installation.server_restarter_interval_hours)"
    "/DSHORTCUT_DIRECTORY=$($installation.shortcut_directory)"
    "/DSERVER_START_MENU_SHORTCUT=$($installation.server_start_menu_shortcut)"
    "/DSERVER_DESKTOP_SHORTCUT=$($installation.server_desktop_shortcut)"
    "RIL_Server_Setup.nsi"
)
& makensis @serverNsisArgs
if ($LASTEXITCODE -ne 0) {
    throw "Server installer build failed."
}

$clientInstaller = Resolve-Path -LiteralPath (
    Join-Path "release" $clientInstallerName
)
$serverInstaller = Resolve-Path -LiteralPath $serverInstallerPath
$clientHash = Get-Sha256Hex -LiteralPath $clientInstaller.Path
$serverHash = Get-Sha256Hex -LiteralPath $serverInstaller.Path

$tag = "$($releaseConfig.tag_prefix)$appVersion"
$urlTemplate = [string]$updateConfig.artifact_url_template
function Get-ArtifactUrl {
    param([string]$FileName)
    $value = $urlTemplate.Replace(
        "{repository}",
        [string]$releaseConfig.repository
    )
    $value = $value.Replace("{tag}", $tag)
    return $value.Replace("{filename}", $FileName)
}

$manifest = [ordered]@{
    schema_version = 2
    version = $appVersion
    protocol_version = [int]$protocolVersion
    client = [ordered]@{
        url = Get-ArtifactUrl -FileName $clientInstallerName
        sha256 = $clientHash
        size = (Get-Item -LiteralPath $clientInstaller).Length
        automatic_update = [bool]$updateConfig.client.automatic
    }
    server = [ordered]@{
        url = Get-ArtifactUrl -FileName $serverInstallerName
        file = $serverInstallerName
        sha256 = $serverHash
        size = (Get-Item -LiteralPath $serverInstaller).Length
        automatic_update = [bool]$updateConfig.server.automatic
    }
}

$manifestPath = Join-Path "release" $manifestName
$manifestJson = $manifest | ConvertTo-Json -Depth 4
$utf8WithoutBom = [System.Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText(
    $manifestPath,
    $manifestJson + [Environment]::NewLine,
    $utf8WithoutBom
)
[IO.File]::WriteAllText(
    (Join-Path "release" $legacyVersionName),
    $legacyVersion + [Environment]::NewLine,
    [Text.Encoding]::ASCII
)

python scripts\release_validation.py verify-release `
    --version $appVersion `
    --protocol-version $protocolVersion `
    --legacy-version $legacyVersion
if ($LASTEXITCODE -ne 0) {
    throw "Release artifact validation failed."
}

Write-Output "Build complete"
Write-Output "Version: $appVersion"
Write-Output "Legacy update version: $legacyVersion"
Write-Output "Client: $clientInstaller"
Write-Output "Server: $serverInstaller"
