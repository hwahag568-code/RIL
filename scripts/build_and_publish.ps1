param(
    [ValidateSet("Release", "StageOnly", "ActivateOnly")]
    [string]$Mode = "Release",
    [string]$ExpectedVersion = "",
    [switch]$InstallDependencies,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$config = Get-Content -LiteralPath "ril_config.json" -Raw -Encoding UTF8 |
    ConvertFrom-Json
$releaseConfig = $config.release
$appVersion = [string]$releaseConfig.version
$repository = [string]$releaseConfig.repository
$tag = "$($releaseConfig.tag_prefix)$appVersion"
$repositoryOwner = $repository.Split("/", 2)[0]
$buildScript = Join-Path $PSScriptRoot "build_release.ps1"
$publishScript = Join-Path $PSScriptRoot "publish_release.ps1"

if (
    -not [string]::IsNullOrWhiteSpace($ExpectedVersion) -and
    $ExpectedVersion -ne $appVersion
) {
    throw (
        "요청한 버전($ExpectedVersion)과 ril_config.json 버전" +
        "($appVersion)이 다릅니다."
    )
}
if ($Mode -eq "ActivateOnly" -and $InstallDependencies) {
    throw "-InstallDependencies는 ActivateOnly 모드에서 사용할 수 없습니다."
}

$mutex = [Threading.Mutex]::new(
    $false,
    [string]$releaseConfig.publish_mutex_name
)
$mutexOwned = $false
$hadOriginalToken = Test-Path Env:GH_TOKEN
$originalToken = $env:GH_TOKEN

function Invoke-ReleaseScript {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [hashtable]$Parameters = @{}
    )

    & $Path @Parameters
    if ($LASTEXITCODE -ne 0) {
        throw (
            "릴리스 단계가 실패했습니다: $Path " +
            (($Parameters.Keys | Sort-Object) -join ", ")
        )
    }
}

function Test-VersionedReleaseExists {
    & gh release view $tag --repo $repository *> $null
    return $LASTEXITCODE -eq 0
}

try {
    try {
        $mutexOwned = $mutex.WaitOne(0)
    }
    catch [Threading.AbandonedMutexException] {
        $mutexOwned = $true
    }
    if (-not $mutexOwned) {
        throw "다른 RIL 빌드 또는 배포가 이미 실행 중입니다."
    }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI(gh)가 설치되어 있지 않습니다."
    }
    if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN)) {
        $storedToken = (
            & gh auth token --user $repositoryOwner 2>$null |
                Out-String
        ).Trim()
        if (
            $LASTEXITCODE -ne 0 -or
            [string]::IsNullOrWhiteSpace($storedToken)
        ) {
            throw (
                "$repositoryOwner 계정의 GitHub 인증을 찾지 못했습니다. " +
                "gh auth login을 한 번 실행해야 합니다."
            )
        }
        $env:GH_TOKEN = $storedToken
    }

    $login = (& gh api user --jq ".login" 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($login)) {
        throw "GitHub 인증 확인에 실패했습니다."
    }
    $pushPermission = (
        & gh api "repos/$repository" --jq ".permissions.push" 2>$null |
            Out-String
    ).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0 -or $pushPermission -ne "true") {
        throw "$login 계정에는 $repository 쓰기 권한이 없습니다."
    }
    Write-Output "Release account: $login"
    Write-Output "Release target: $repository ($tag)"

    if ($DryRun) {
        Write-Output "Dry run: 빌드와 원격 변경은 수행하지 않습니다."
        if ($Mode -ne "ActivateOnly") {
            Invoke-ReleaseScript -Path $publishScript -Parameters @{
                Phase = "Stage"
                ResumeStage = $true
                DryRun = $true
            }
        }
        if ($Mode -ne "StageOnly") {
            Invoke-ReleaseScript -Path $publishScript -Parameters @{
                Phase = "Activate"
                DryRun = $true
            }
        }
        return
    }

    if ($Mode -ne "ActivateOnly") {
        $releaseExists = Test-VersionedReleaseExists
        if ($releaseExists) {
            Write-Output (
                "$tag 릴리스가 이미 있어 재빌드하지 않고 " +
                "검증된 Stage부터 재개합니다."
            )
        }
        else {
            $reuseVerifiedBuild = $false
            if (-not $InstallDependencies) {
                try {
                    Invoke-ReleaseScript `
                        -Path $publishScript `
                        -Parameters @{
                            Phase = "Stage"
                            ResumeStage = $true
                            DryRun = $true
                        }
                    $reuseVerifiedBuild = $true
                    Write-Output (
                        "현재 소스와 일치하는 검증된 로컬 빌드를 " +
                        "재사용합니다."
                    )
                }
                catch {
                    Write-Output (
                        "재사용 가능한 로컬 빌드가 없어 새로 " +
                        "빌드합니다: $_"
                    )
                }
            }
            if (-not $reuseVerifiedBuild) {
                $buildParameters = @{}
                if ($InstallDependencies) {
                    $buildParameters.InstallDependencies = $true
                }
                Invoke-ReleaseScript `
                    -Path $buildScript `
                    -Parameters $buildParameters
            }
        }

        $stageAttempts = [int]$releaseConfig.stage_attempts
        $stageRetryDelay = [double](
            $releaseConfig.stage_retry_delay_seconds
        )
        for (
            $stageAttempt = 1;
            $stageAttempt -le $stageAttempts;
            $stageAttempt++
        ) {
            try {
                Invoke-ReleaseScript -Path $publishScript -Parameters @{
                    Phase = "Stage"
                    ResumeStage = $true
                }
                break
            }
            catch {
                if ($stageAttempt -eq $stageAttempts) {
                    throw
                }
                Write-Warning (
                    "Stage 실패($stageAttempt/$stageAttempts). " +
                    "$stageRetryDelay초 후 안전 재개를 시도합니다: $_"
                )
                Start-Sleep -Seconds $stageRetryDelay
            }
        }
    }

    if ($Mode -ne "StageOnly") {
        $attempts = [int]$releaseConfig.activation_attempts
        $retryDelay = [double](
            $releaseConfig.activation_retry_delay_seconds
        )
        for ($attempt = 1; $attempt -le $attempts; $attempt++) {
            try {
                Invoke-ReleaseScript -Path $publishScript -Parameters @{
                    Phase = "Activate"
                }
                break
            }
            catch {
                if ($attempt -eq $attempts) {
                    throw
                }
                Write-Warning (
                    "Activate 실패($attempt/$attempts). " +
                    "$retryDelay초 후 검증부터 다시 시도합니다: $_"
                )
                Start-Sleep -Seconds $retryDelay
            }
        }
    }

    Write-Output "RIL $appVersion $Mode 완료"
}
finally {
    if ($hadOriginalToken) {
        $env:GH_TOKEN = $originalToken
    }
    else {
        Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
    }
    if ($mutexOwned) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
