param(
    [Parameter(Mandatory = $true)]
    [string]$ReadyPath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedVersion,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedExecutable
)

$ErrorActionPreference = "Stop"

try {
    if (-not (Test-Path -LiteralPath $ReadyPath -PathType Leaf)) {
        exit 1
    }

    $ready = Get-Content -LiteralPath $ReadyPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if (
        [string]$ready.status -ne "ready" -or
        [string]$ready.version -ne $ExpectedVersion
    ) {
        exit 2
    }

    $readyPid = [int]$ready.pid
    if ($readyPid -le 0) {
        exit 2
    }

    $process = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $readyPid" `
        -ErrorAction SilentlyContinue
    if (-not $process -or -not $process.ExecutablePath) {
        exit 2
    }

    $actualPath = [IO.Path]::GetFullPath(
        [string]$process.ExecutablePath
    )
    $expectedPath = [IO.Path]::GetFullPath($ExpectedExecutable)
    if (
        -not $actualPath.Equals(
            $expectedPath,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        exit 2
    }

    exit 0
}
catch {
    exit 2
}
