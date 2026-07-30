param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$serverInstalledConfigName = "ril-server-installed.json"
$installPath = [IO.Path]::GetFullPath($InstallDir)
$configPath = Join-Path $installPath $serverInstalledConfigName
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    $configPath = Join-Path $installPath "ril_config.json"
}
$configPath = [IO.Path]::GetFullPath($configPath)
$env:RIL_CONFIG_PATH = $configPath
$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
$restarterPath = Join-Path $installPath (
    [string](
        $config.installation.server_restarter_power_shell_script
    )
)

& $restarterPath -InstallDir $installPath -ConfigPath $configPath
exit $LASTEXITCODE
