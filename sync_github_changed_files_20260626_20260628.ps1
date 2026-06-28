param(
    [string]$SourceRoot = "C:\Users\Administrator\Desktop\Workflow-PNCECS",
    [string]$DestinationRoot = "C:\Apps\Workflow_PNCECS",
    [ValidateSet("Copy", "Move")]
    [string]$Mode = "Copy",
    [switch]$Execute,
    [switch]$SkipMissing,
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"

$ChangedFiles = @(
    "admin/routes.py",
    "audit/routes.py",
    "instance/tmp/workflow_backup_20260626_173651_41ba48539fdb4088b6eab5d418b89cc2/workflow_backup_20260626_173651.zip",
    "templates/admin/workflow_routing/list.html",
    "templates/workflow/following.html",
    "templates/workflow/templates_admin/edit.html",
    "templates/workflow/templates_admin/list.html",
    "utils/ui_labels.py",
    "workflow/engine.py",
    "workflow/routes.py",
    "workflow/templates_admin.py"
)

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

$source = Resolve-ExistingDirectory -Path $SourceRoot -Name "SourceRoot"
$destination = $DestinationRoot.TrimEnd("\")

if ($source -ieq $destination) {
    throw "SourceRoot and DestinationRoot are the same path."
}

if (-not $ReportPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $ReportPath = Join-Path (Get-Location).Path "github_changed_files_20260626_20260628_$stamp.csv"
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
Write-Host "GitHub changed files list: 2026-06-26 to 2026-06-28"
Write-Host "Source:      $source"
Write-Host "Destination: $destination"
Write-Host "Mode:        $Mode"
Write-Host "Execute:     $($Execute.IsPresent)"
Write-Host "SkipMissing: $($SkipMissing.IsPresent)"
Write-Host "Report:      $ReportPath"
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
    Write-Host "Copy files: .\sync_github_changed_files_20260626_20260628.ps1 -Execute -Mode Copy"
    Write-Host "Move files: .\sync_github_changed_files_20260626_20260628.ps1 -Execute -Mode Move"
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
