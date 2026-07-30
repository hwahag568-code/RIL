param(
    [ValidateSet("Stage", "Activate")]
    [string]$Phase = "Stage",
    [string]$Repository = "",
    [switch]$AllowVersionClobber,
    [switch]$ResumeStage,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repoRoot

$config = Get-Content -LiteralPath "ril_config.json" -Raw -Encoding UTF8 |
    ConvertFrom-Json
$releaseConfig = $config.release
$artifacts = $config.artifacts
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
$branch = [string]$releaseConfig.branch
$configuredRepository = [string]$releaseConfig.repository
if ([string]::IsNullOrWhiteSpace($Repository)) {
    $Repository = $configuredRepository
}
elseif ($Repository -ne $configuredRepository) {
    throw (
        "-Repository 값은 ril_config.json의 release.repository와 " +
        "같아야 합니다. manifest URL과 업로드 대상이 달라지는 " +
        "배포는 허용하지 않습니다."
    )
}

$tag = "$($releaseConfig.tag_prefix)$appVersion"
$clientInstaller = Join-Path "release" (
    [string]$artifacts.client_installer_filename
)
$serverInstallerName = (
    [string]$artifacts.server_installer_filename_template
).Replace("{version}", $appVersion)
$serverInstaller = Join-Path "release" $serverInstallerName
$manifestFile = Join-Path "release" (
    [string]$artifacts.manifest_filename
)
$legacyVersionFile = Join-Path "release" (
    [string]$artifacts.legacy_version_filename
)
$versionedAssets = @(
    $clientInstaller,
    $serverInstaller,
    $manifestFile,
    $legacyVersionFile
)

function Assert-LocalRelease {
    param([switch]$RequireBuildInfo)

    foreach ($path in $versionedAssets) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing release file: $path"
        }
    }

    if ($RequireBuildInfo) {
        python scripts\release_validation.py verify-build-info `
            --version $appVersion `
            --protocol-version $protocolVersion
        if ($LASTEXITCODE -ne 0) {
            throw "Existing binaries do not match the current source."
        }
    }

    python scripts\release_validation.py verify-release `
        --version $appVersion `
        --protocol-version $protocolVersion `
        --legacy-version $legacyVersion
    if ($LASTEXITCODE -ne 0) {
        throw "Release artifacts failed pre-publish validation."
    }
}

function Invoke-GitHub {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments,
        [Parameter(Mandatory)]
        [string]$FailureMessage
    )

    & gh @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

function Assert-GitHubWriteAccess {
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "GitHub CLI (gh) is not installed or is not on PATH."
    }
    if ([string]::IsNullOrWhiteSpace($env:GH_TOKEN)) {
        throw "GH_TOKEN with contents:write access to $Repository is required."
    }

    $login = (
        & gh api user --jq ".login" 2>$null |
        Out-String
    ).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($login)) {
        throw "GH_TOKEN authentication failed."
    }

    $pushPermission = (
        & gh api "repos/$Repository" --jq ".permissions.push" 2>$null |
        Out-String
    ).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect repository permission for $Repository."
    }
    if ($pushPermission -ne "true") {
        throw "GitHub account '$login' has permissions.push=false for $Repository. Publishing is blocked."
    }

    $escapedBranch = [Uri]::EscapeDataString($branch)
    & gh api "repos/$Repository/branches/$escapedBranch" --silent 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "The required $branch branch is not accessible in $Repository."
    }

    Write-Output "GitHub preflight passed: $login -> $Repository"
}

function Test-ReleaseExists {
    param([Parameter(Mandatory)][string]$ReleaseTag)

    & gh release view $ReleaseTag --repo $Repository *> $null
    return $LASTEXITCODE -eq 0
}

function Get-RemoteReleaseAssetNames {
    param([Parameter(Mandatory)][string]$ReleaseTag)

    $names = @(
        & gh release view $ReleaseTag `
            --repo $Repository `
            --json "assets" `
            --jq ".assets[].name" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect assets in $ReleaseTag."
    }
    return @(
        $names |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
}

function Assert-VersionedReleasePublished {
    $isDraft = (
        & gh release view $tag `
            --repo $Repository `
            --json "isDraft" `
            --jq ".isDraft" 2>$null |
        Out-String
    ).Trim().ToLowerInvariant()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not inspect staged release $tag."
    }
    if ($isDraft -ne "false") {
        throw "Staged release $tag is still a draft and cannot be activated."
    }
}

function Assert-RemoteReleaseAssets {
    param(
        [Parameter(Mandatory)]
        [string]$ReleaseTag,
        [Parameter(Mandatory)]
        [string[]]$LocalPaths
    )

    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $tempDirectory = Join-Path $tempRoot (
        "RIL_release_verify_" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $tempDirectory | Out-Null

    try {
        $downloadArguments = @(
            "release",
            "download",
            $ReleaseTag,
            "--repo",
            $Repository,
            "--dir",
            $tempDirectory,
            "--clobber"
        )
        foreach ($localPath in $LocalPaths) {
            $downloadArguments += @(
                "--pattern",
                (Split-Path -Leaf $localPath)
            )
        }

        Invoke-GitHub `
            -Arguments $downloadArguments `
            -FailureMessage (
                "Could not download staged assets from $ReleaseTag."
            )

        foreach ($localPath in $LocalPaths) {
            $assetName = Split-Path -Leaf $localPath
            $downloadedPath = Join-Path $tempDirectory $assetName
            if (-not (Test-Path -LiteralPath $downloadedPath -PathType Leaf)) {
                throw "Staged release asset is missing: $assetName"
            }

            $localHash = (
                Get-FileHash -Algorithm SHA256 -LiteralPath $localPath
            ).Hash
            $remoteHash = (
                Get-FileHash -Algorithm SHA256 -LiteralPath $downloadedPath
            ).Hash
            if ($localHash -ne $remoteHash) {
                throw "Staged release asset does not match local build: $assetName"
            }
        }
    }
    finally {
        $resolvedTemp = [IO.Path]::GetFullPath($tempDirectory)
        if (
            $resolvedTemp.StartsWith(
                $tempRoot,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force `
                -ErrorAction SilentlyContinue
        }
    }
}

function Test-RemoteReleaseAssetMatches {
    param(
        [Parameter(Mandatory)]
        [string]$ReleaseTag,
        [Parameter(Mandatory)]
        [string]$LocalPath
    )

    $assetName = Split-Path -Leaf $LocalPath
    $remoteNames = @(Get-RemoteReleaseAssetNames -ReleaseTag $ReleaseTag)
    if ($remoteNames -notcontains $assetName) {
        return $false
    }

    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $tempDirectory = Join-Path $tempRoot (
        "RIL_release_compare_" + [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $tempDirectory | Out-Null
    try {
        Invoke-GitHub `
            -Arguments @(
                "release",
                "download",
                $ReleaseTag,
                "--repo",
                $Repository,
                "--dir",
                $tempDirectory,
                "--pattern",
                $assetName
            ) `
            -FailureMessage (
                "Could not compare remote release asset: $assetName"
            )
        $downloadedPath = Join-Path $tempDirectory $assetName
        if (-not (Test-Path -LiteralPath $downloadedPath -PathType Leaf)) {
            throw "Remote release asset is missing after download: $assetName"
        }
        $localHash = (
            Get-FileHash -Algorithm SHA256 -LiteralPath $LocalPath
        ).Hash
        $remoteHash = (
            Get-FileHash -Algorithm SHA256 -LiteralPath $downloadedPath
        ).Hash
        return $localHash -eq $remoteHash
    }
    finally {
        $resolvedTemp = [IO.Path]::GetFullPath($tempDirectory)
        if (
            $resolvedTemp.StartsWith(
                $tempRoot,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            Remove-Item -LiteralPath $resolvedTemp -Recurse -Force `
                -ErrorAction SilentlyContinue
        }
    }
}

function Resume-VersionedReleaseAssets {
    Assert-VersionedReleasePublished
    $remoteNames = @(Get-RemoteReleaseAssetNames -ReleaseTag $tag)
    $presentPaths = @()
    $missingPaths = @()

    foreach ($localPath in $versionedAssets) {
        $assetName = Split-Path -Leaf $localPath
        if ($remoteNames -contains $assetName) {
            $presentPaths += $localPath
        }
        else {
            $missingPaths += $localPath
        }
    }

    if ($presentPaths.Count -gt 0) {
        Assert-RemoteReleaseAssets `
            -ReleaseTag $tag `
            -LocalPaths $presentPaths
    }

    foreach ($missingPath in $missingPaths) {
        Invoke-GitHub `
            -Arguments @(
                "release",
                "upload",
                $tag,
                $missingPath,
                "--repo",
                $Repository
            ) `
            -FailureMessage (
                "Could not resume missing asset in ${tag}: " +
                (Split-Path -Leaf $missingPath)
            )
    }
}

function Get-RepositoryFileBytes {
    param([Parameter(Mandatory)][string]$Path)

    $escapedBranch = [Uri]::EscapeDataString($branch)
    $endpoint = "repos/$Repository/contents/${Path}?ref=$escapedBranch"
    $result = @(
        & gh api $endpoint --jq ".content" 2>&1
    )
    $apiExitCode = $LASTEXITCODE
    if ($apiExitCode -ne 0) {
        $failureText = ($result | Out-String)
        if (
            $failureText -match "(?i)HTTP\s+404" -or
            $failureText -match '"status"\s*:\s*"404"'
        ) {
            return $null
        }
        $firstFailureLine = (
            $result |
                Select-Object -First 1 |
                Out-String
        ).Trim()
        throw (
            "Could not read repository file ${Path}: " +
            $firstFailureLine
        )
    }
    $encoded = ($result | Out-String) -replace "\s", ""
    try {
        return ,([Convert]::FromBase64String($encoded))
    }
    catch {
        throw "Could not decode the repository file: $Path"
    }
}

function Compare-RilVersion {
    param(
        [Parameter(Mandatory)][string]$Left,
        [Parameter(Mandatory)][string]$Right
    )

    $leftMatch = [regex]::Match($Left, "^(\d{6})(?:\.(\d+))?$")
    $rightMatch = [regex]::Match($Right, "^(\d{6})(?:\.(\d+))?$")
    if (-not $leftMatch.Success -or -not $rightMatch.Success) {
        throw "Could not compare RIL versions: $Left / $Right"
    }
    $leftDate = [int64]$leftMatch.Groups[1].Value
    $rightDate = [int64]$rightMatch.Groups[1].Value
    if ($leftDate -ne $rightDate) {
        return $leftDate.CompareTo($rightDate)
    }
    $leftHotfix = if ($leftMatch.Groups[2].Success) {
        [int64]$leftMatch.Groups[2].Value
    }
    else {
        0
    }
    $rightHotfix = if ($rightMatch.Groups[2].Success) {
        [int64]$rightMatch.Groups[2].Value
    }
    else {
        0
    }
    return $leftHotfix.CompareTo($rightHotfix)
}

function Assert-RemoteMarkersDoNotBlockRelease {
    $remoteManifestBytes = Get-RepositoryFileBytes `
        -Path ([string]$artifacts.manifest_filename)
    if ($null -ne $remoteManifestBytes) {
        try {
            $remoteManifest = (
                [Text.Encoding]::UTF8.GetString($remoteManifestBytes)
            ) | ConvertFrom-Json
            $remoteVersion = [string]$remoteManifest.version
        }
        catch {
            throw "The remote update manifest is not valid JSON."
        }

        $comparison = Compare-RilVersion `
            -Left $remoteVersion `
            -Right $appVersion
        if ($comparison -gt 0) {
            throw (
                "Remote RIL version $remoteVersion is newer than " +
                "local version $appVersion. Downgrade publishing is blocked."
            )
        }
        if ($comparison -eq 0) {
            $localManifestBytes = [IO.File]::ReadAllBytes(
                (Resolve-Path -LiteralPath $manifestFile)
            )
            $remoteBase64 = [Convert]::ToBase64String(
                $remoteManifestBytes
            )
            $localBase64 = [Convert]::ToBase64String(
                $localManifestBytes
            )
            if ($remoteBase64 -ne $localBase64) {
                throw (
                    "The remote manifest already uses version $appVersion " +
                    "with different contents. Increase the version."
                )
            }
        }
    }

    $remoteLegacyBytes = Get-RepositoryFileBytes `
        -Path ([string]$artifacts.legacy_version_filename)
    if ($null -ne $remoteLegacyBytes) {
        $remoteLegacy = (
            [Text.Encoding]::ASCII.GetString($remoteLegacyBytes)
        ).Trim()
        if ($remoteLegacy -notmatch "^\d+$") {
            throw "The remote legacy version marker is invalid."
        }
        if ([int64]$remoteLegacy -gt [int64]$legacyVersion) {
            throw (
                "Remote legacy version $remoteLegacy is newer than " +
                "local version $legacyVersion. Downgrade publishing is blocked."
            )
        }
    }
}

function Update-RepositoryFile {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$LocalPath
    )

    $localBytes = [IO.File]::ReadAllBytes(
        (Resolve-Path -LiteralPath $LocalPath)
    )
    $content = [Convert]::ToBase64String($localBytes)
    $escapedBranch = [Uri]::EscapeDataString($branch)
    $contentEndpoint = (
        "repos/$Repository/contents/${Path}?ref=$escapedBranch"
    )
    $sha = (
        & gh api $contentEndpoint --jq ".sha" 2>$null |
        Out-String
    ).Trim()
    $foundExistingFile = (
        $LASTEXITCODE -eq 0 -and
        -not [string]::IsNullOrWhiteSpace($sha)
    )

    $payload = @{
        message = "Activate RIL $appVersion ($Path)"
        content = $content
        branch = $branch
    }
    if ($foundExistingFile) {
        $payload.sha = $sha

        $existingContent = (
            & gh api $contentEndpoint `
                --jq ".content" 2>$null |
            Out-String
        ) -replace "\s", ""
        if ($LASTEXITCODE -ne 0) {
            throw "Could not read the existing repository file: $Path"
        }
        if ($existingContent -eq $content) {
            Write-Output "Repository activation file is already current: $Path"
            return
        }
    }

    $payload | ConvertTo-Json |
        gh api --method PUT `
            "repos/$Repository/contents/$Path" `
            --input -
    if ($LASTEXITCODE -ne 0) {
        throw "Could not update $Path."
    }

    $remoteContent = (
        & gh api $contentEndpoint `
            --jq ".content" 2>$null |
        Out-String
    ) -replace "\s", ""
    if ($LASTEXITCODE -ne 0 -or $remoteContent -ne $content) {
        throw "Repository activation file verification failed: $Path"
    }
}

function Publish-LegacyClientAsset {
    $legacyTag = [string]$artifacts.legacy_client_release_tag
    if (Test-ReleaseExists -ReleaseTag $legacyTag) {
        if (
            Test-RemoteReleaseAssetMatches `
                -ReleaseTag $legacyTag `
                -LocalPath $clientInstaller
        ) {
            Write-Output (
                "Legacy client installer is already current: " +
                (Split-Path -Leaf $clientInstaller)
            )
        }
        else {
            Invoke-GitHub `
                -Arguments @(
                    "release",
                    "upload",
                    $legacyTag,
                    $clientInstaller,
                    "--repo",
                    $Repository,
                    "--clobber"
                ) `
                -FailureMessage (
                    "Could not replace the legacy client installer."
                )
        }
    }
    else {
        Invoke-GitHub `
            -Arguments @(
                "release",
                "create",
                $legacyTag,
                $clientInstaller,
                "--repo",
                $Repository,
                "--title",
                "Legacy client update",
                "--notes",
                "Compatibility release for existing clients"
            ) `
            -FailureMessage (
                "Could not create the legacy client release."
            )
    }

    Assert-RemoteReleaseAssets `
        -ReleaseTag $legacyTag `
        -LocalPaths @($clientInstaller)
}

Assert-LocalRelease -RequireBuildInfo:($Phase -eq "Stage")

if ($Phase -eq "Activate" -and $AllowVersionClobber) {
    throw "-AllowVersionClobber is only valid during the Stage phase."
}
if ($Phase -eq "Activate" -and $ResumeStage) {
    throw "-ResumeStage is only valid during the Stage phase."
}
if ($AllowVersionClobber -and $ResumeStage) {
    throw "-AllowVersionClobber and -ResumeStage cannot be used together."
}

if ($DryRun) {
    Write-Output "Phase: $Phase"
    Write-Output "Repository: $Repository"
    Write-Output "Versioned release: $tag"
    if ($Phase -eq "Stage") {
        Write-Output "Would upload and verify versioned client/server/metadata assets. Update markers would remain unchanged."
        if ($AllowVersionClobber) {
            Write-Warning "Would explicitly allow replacement of an existing $tag release."
        }
        if ($ResumeStage) {
            Write-Output (
                "Would safely resume $tag only when every existing asset " +
                "matches the local release."
            )
        }
    }
    else {
        Write-Output (
            "Would verify staged assets, prepare and verify the legacy " +
            "{0} asset, activate unified client/server {1}, then " +
            "activate legacy {2}." -f
            $artifacts.legacy_client_release_tag,
            $artifacts.manifest_filename,
            $artifacts.legacy_version_filename
        )
    }
    return
}

Assert-GitHubWriteAccess
Assert-RemoteMarkersDoNotBlockRelease

if ($Phase -eq "Stage") {
    $releaseExists = Test-ReleaseExists -ReleaseTag $tag
    if (
        $releaseExists -and
        -not $AllowVersionClobber -and
        -not $ResumeStage
    ) {
        throw "Release $tag already exists. Increase VERSION (for example, use a date.hotfix value) instead of overwriting it. Use -ResumeStage only to continue an identical partial upload, or -AllowVersionClobber for an explicit recovery operation."
    }

    if ($releaseExists -and $ResumeStage) {
        Resume-VersionedReleaseAssets
    }
    elseif ($releaseExists) {
        $uploadArguments = @(
            "release",
            "upload",
            $tag
        ) + $versionedAssets + @(
            "--repo",
            $Repository,
            "--clobber"
        )
        Invoke-GitHub `
            -Arguments $uploadArguments `
            -FailureMessage "Could not replace assets in $tag."
    }
    else {
        $createArguments = @(
            "release",
            "create",
            $tag
        ) + $versionedAssets + @(
            "--repo",
            $Repository,
            "--target",
            $branch,
            "--title",
            "RIL $appVersion (staged)",
            "--notes",
            "Versioned client/server artifacts. Unified update markers are activated separately."
        )
        Invoke-GitHub `
            -Arguments $createArguments `
            -FailureMessage "Could not create staged release $tag."
    }

    Assert-RemoteReleaseAssets `
        -ReleaseTag $tag `
        -LocalPaths $versionedAssets

    Write-Output "Staged and verified RIL $appVersion in $Repository."
    Write-Output "No update marker was changed."
    Write-Output "Run after review: .\scripts\publish_release.ps1 -Phase Activate"
    return
}

if (-not (Test-ReleaseExists -ReleaseTag $tag)) {
    throw "Staged release $tag does not exist. Run the Stage phase first."
}

Assert-VersionedReleasePublished

Assert-RemoteReleaseAssets `
    -ReleaseTag $tag `
    -LocalPaths $versionedAssets

# Prepare the legacy artifact while its old version marker is still active.
Publish-LegacyClientAsset

# New clients are activated only after every installer asset is verified.
Update-RepositoryFile `
    -Path ([string]$artifacts.manifest_filename) `
    -LocalPath $manifestFile

# This is deliberately last: it activates the legacy client population.
Update-RepositoryFile `
    -Path ([string]$artifacts.legacy_version_filename) `
    -LocalPath $legacyVersionFile

Write-Output "Activated unified RIL $appVersion for servers and clients."
