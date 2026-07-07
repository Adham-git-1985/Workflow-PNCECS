param(
    [string]$SourceRoot = "C:\Users\Administrator\Desktop\Workflow-PNCECS",
    [string]$DestinationRoot = "C:\Apps\Workflow_PNCECS",
    [string]$Remote = "origin",
    [string]$Branch = "main",
    [string]$Since = "2026-06-28 00:00:00",
    [string]$Until = "2026-07-01 00:00:00",
    [ValidateSet("Copy", "Move")]
    [string]$Mode = "Copy",
    [switch]$Execute,
    [switch]$SkipMissing,
    [switch]$NoFetch,
    [switch]$UpdateSource,
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

function Resolve-ExistingDirectory {
    param([string]$Path, [string]$Name)

    if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
        throw "$Name does not exist or is not a directory: $Path"
    }

    return (Resolve-Path -LiteralPath $Path).Path.TrimEnd("\")
}

function Get-NormalizedRelativePath {
    param([string]$RelativePath)

    return ($RelativePath -replace "/", "\").TrimStart("\")
}

function Invoke-Git {
    param(
        [string]$RepoRoot,
        [string[]]$Arguments
    )

    $output = & git -C $RepoRoot @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed:`n$output"
    }

    return $output
}

$source = Resolve-ExistingDirectory -Path $SourceRoot -Name "SourceRoot"
$destination = $DestinationRoot.TrimEnd("\")
$remoteRef = "$Remote/$Branch"

if ($source -ieq $destination) {
    throw "SourceRoot and DestinationRoot are the same path."
}

Invoke-Git -RepoRoot $source -Arguments @("rev-parse", "--is-inside-work-tree") | Out-Null

if (-not $NoFetch) {
    Invoke-Git -RepoRoot $source -Arguments @("fetch", $Remote, "--prune") | Out-Null
}

Invoke-Git -RepoRoot $source -Arguments @("rev-parse", "--verify", $remoteRef) | Out-Null

if ($UpdateSource) {
    $dirty = @(
        Invoke-Git -RepoRoot $source -Arguments @("status", "--porcelain") | Where-Object { $_ -and $_.Trim() }
    )

    if ($dirty.Count -gt 0) {
        throw "SourceRoot has local changes. Commit/stash them first, or run without -UpdateSource."
    }

    $currentBranch = (Invoke-Git -RepoRoot $source -Arguments @("branch", "--show-current") | Select-Object -First 1).Trim()
    if ($currentBranch -ne $Branch) {
        Invoke-Git -RepoRoot $source -Arguments @("switch", $Branch) | Out-Null
    }

    Invoke-Git -RepoRoot $source -Arguments @("pull", "--ff-only", $Remote, $Branch) | Out-Null
}

$commits = @(
    Invoke-Git -RepoRoot $source -Arguments @(
        "log",
        $remoteRef,
        "--since=$Since",
        "--until=$Until",
        "--pretty=format:%H"
    ) | Where-Object { $_ -and $_.Trim() }
)

$ChangedFiles = @(
    foreach ($commit in $commits) {
        Invoke-Git -RepoRoot $source -Arguments @(
            "diff-tree",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=ACMRT",
            "-r",
            "-m",
            $commit.Trim()
        ) | Where-Object { $_ -and $_.Trim() }
    }
) | Sort-Object -Unique

if (-not $ReportPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $ReportPath = Join-Path (Get-Location).Path "github_changed_files_20260628_20260630_$stamp.csv"
}

$files = foreach ($relativePath in $ChangedFiles) {
    $relative = Get-NormalizedRelativePath -RelativePath $relativePath
    $sourcePath = Join-Path $source $relative
    $targetPath = Join-Path $destination $relative
    $exists = Test-Path -LiteralPath $sourcePath -PathType Leaf
    $sourceItem = if ($exists) { Get-Item -LiteralPath $sourcePath } else { $null }

    [pscustomobject]@{
        Exists        = $exists
        LastWriteTime = if ($sourceItem) { $sourceItem.LastWriteTime } else { $null }
        SizeBytes     = if ($sourceItem) { $sourceItem.Length } else { $null }
        RelativePath  = $relative
        SourcePath    = $sourcePath
        TargetPath    = $targetPath
    }
}

$files | Export-Csv -LiteralPath $ReportPath -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "GitHub changed files list: 2026-06-28 to 2026-06-30"
Write-Host "RemoteRef:   $remoteRef"
Write-Host "Since:       $Since"
Write-Host "Until:       $Until"
Write-Host "Source:      $source"
Write-Host "Destination: $destination"
Write-Host "Mode:        $Mode"
Write-Host "Execute:     $($Execute.IsPresent)"
Write-Host "UpdateSource:$($UpdateSource.IsPresent)"
Write-Host "SkipMissing: $($SkipMissing.IsPresent)"
Write-Host "Report:      $ReportPath"
Write-Host "Commits:     $($commits.Count)"
Write-Host "Count:       $($files.Count)"
Write-Host ""

$files | Format-Table Exists, LastWriteTime, SizeBytes, RelativePath -AutoSize

$missingFiles = @($files | Where-Object { -not $_.Exists })
if ($missingFiles.Count -gt 0) {
    Write-Host ""
    Write-Warning "Missing source files: $($missingFiles.Count)"
    $missingFiles | ForEach-Object { Write-Warning $_.SourcePath }

    if (-not $SkipMissing) {
        Write-Host ""
        Write-Host "Add -SkipMissing to continue without missing files."
        exit 1
    }
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "Dry run only. To transfer files, rerun with -Execute."
    Write-Host "Copy files: .\sync_github_changed_files_20260628_20260630.ps1 -Execute -Mode Copy"
    Write-Host "Move files: .\sync_github_changed_files_20260628_20260630.ps1 -Execute -Mode Move"
    Write-Host "Copy after updating source: .\sync_github_changed_files_20260628_20260630.ps1 -UpdateSource -Execute -Mode Copy"
    exit 0
}

$transferred = 0
foreach ($file in ($files | Where-Object { $_.Exists })) {
    $targetDir = Split-Path -Parent $file.TargetPath
    if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    if ($Mode -eq "Move") {
        Move-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    } else {
        Copy-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    }

    $transferred += 1
}

Write-Host ""
Write-Host "$Mode completed for $transferred files."
