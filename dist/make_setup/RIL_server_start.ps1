param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"
$installPath = [IO.Path]::GetFullPath($InstallDir)
$config = Get-Content -LiteralPath (
    Join-Path $installPath "ril_config.json"
) -Raw -Encoding UTF8 | ConvertFrom-Json
$restarterPath = Join-Path $installPath (
    [string](
        $config.installation.server_restarter_power_shell_script
    )
)

& $restarterPath -InstallDir $installPath
exit $LASTEXITCODE
