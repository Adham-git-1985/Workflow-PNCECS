param(
    [string]$SourceRoot = "C:\Users\Administrator\Desktop\Workflow-PNCECS",
    [string]$DestinationRoot = "C:\Apps\Workflow_PNCECS",
    [switch]$Execute,
    [string]$BackupRoot = "",
    [string]$ReportPath = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Files added or changed in Git from 2026-08-02 through 2026-08-05.
# Keep these paths relative so their directory structure is preserved.
$ChangedFiles = @(
    "README.md",
    "admin/routes.py",
    "app.py",
    "archive/permissions.py",
    "archive/queries.py",
    "archive/routes.py",
    "assistant/__init__.py",
    "assistant/knowledge.py",
    "assistant/routes.py",
    "assistant/service.py",
    "config.py",
    "migrations/versions/6f1a2b3c4d5e_add_correspondence_procedure.py",
    "migrations/versions/7a2b3c4d5e6f_add_corr_template_and_confidential_access.py",
    "migrations/versions/8b3c4d5e6f70_preserve_corr_secrecy_in_workflow.py",
    "models.py",
    "portal/corr_deadlines_job.py",
    "portal/hr_alerts_job.py",
    "portal/perm_defs.py",
    "portal/routes.py",
    "serve.py",
    "services/correspondence_procedure.py",
    "services/workflow_confidentiality.py",
    "static/css/chat_assistant.css",
    "static/js/chat_assistant.js",
    "templates/help/corr_guide.html",
    "templates/layout.html",
    "templates/partials/chat_assistant.html",
    "templates/portal/corr/_procedure_fields.html",
    "templates/portal/corr/_procedure_panel.html",
    "templates/portal/corr/inbound_edit.html",
    "templates/portal/corr/inbound_new.html",
    "templates/portal/corr/inbound_view.html",
    "templates/portal/corr/index.html",
    "templates/portal/corr/outbound_edit.html",
    "templates/portal/corr/outbound_new.html",
    "templates/portal/corr/outbound_view.html",
    "templates/portal/corr/work_dashboard.html",
    "templates/portal/index.html",
    "templates/portal/layout.html",
    "templates/workflow/view_request.html",
    "tests/test_assistant_service.py",
    "tests/test_backup_restore.py",
    "tests/test_correspondence_procedure.py",
    "tests/test_portal_access_request_links.py",
    "tests/test_workflow_confidentiality.py",
    "utils/request_audit.py",
    "utils/ui_labels.py",
    "workflow/engine.py",
    "workflow/routes.py"
)

function Resolve-Directory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name,
        [switch]$AllowMissing
    )

    if (Test-Path -LiteralPath $Path) {
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            throw "$Name is not a directory: $Path"
        }
        return (Resolve-Path -LiteralPath $Path).Path.TrimEnd("\")
    }

    if (-not $AllowMissing) {
        throw "$Name does not exist: $Path"
    }

    return [System.IO.Path]::GetFullPath($Path).TrimEnd("\")
}

function Get-SafeChildPath {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$RelativePath
    )

    if ([System.IO.Path]::IsPathRooted($RelativePath)) {
        throw "Expected a relative path, but received: $RelativePath"
    }

    $normalized = ($RelativePath -replace "/", "\").TrimStart("\")
    $fullRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd("\")
    $fullPath = [System.IO.Path]::GetFullPath((Join-Path $fullRoot $normalized))
    $rootPrefix = "$fullRoot\"

    if (-not $fullPath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Path leaves the selected root: $RelativePath"
    }

    return $fullPath
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

$source = Resolve-Directory -Path $SourceRoot -Name "SourceRoot"
$destination = Resolve-Directory -Path $DestinationRoot -Name "DestinationRoot" -AllowMissing

if ($source -ieq $destination) {
    throw "SourceRoot and DestinationRoot cannot be the same directory."
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"

if (-not $BackupRoot) {
    $destinationParent = Split-Path -Parent $destination
    $destinationName = Split-Path -Leaf $destination
    $BackupRoot = Join-Path $destinationParent "${destinationName}_backup_$stamp"
}
$backup = [System.IO.Path]::GetFullPath($BackupRoot).TrimEnd("\")

if (-not $ReportPath) {
    $ReportPath = Join-Path (Get-Location).Path "weekly_changed_files_20260802_20260805_$stamp.csv"
}
$report = [System.IO.Path]::GetFullPath($ReportPath)

$files = @(
    foreach ($relativePath in $ChangedFiles) {
        $sourcePath = Get-SafeChildPath -Root $source -RelativePath $relativePath
        $targetPath = Get-SafeChildPath -Root $destination -RelativePath $relativePath
        $backupPath = Get-SafeChildPath -Root $backup -RelativePath $relativePath
        $sourceExists = Test-Path -LiteralPath $sourcePath -PathType Leaf
        $targetExists = Test-Path -LiteralPath $targetPath -PathType Leaf
        $sourceHash = if ($sourceExists) { Get-Sha256 -Path $sourcePath } else { $null }
        $targetHash = if ($targetExists) { Get-Sha256 -Path $targetPath } else { $null }

        [pscustomobject]@{
            RelativePath = $relativePath
            SourcePath   = $sourcePath
            TargetPath   = $targetPath
            BackupPath   = $backupPath
            SourceExists = $sourceExists
            TargetExists = $targetExists
            SourceHash   = $sourceHash
            TargetHash   = $targetHash
            Status       = if (-not $sourceExists) {
                "MissingSource"
            } elseif ($sourceHash -eq $targetHash) {
                "Identical"
            } else {
                "CopyRequired"
            }
        }
    }
)

$reportDirectory = Split-Path -Parent $report
if (-not (Test-Path -LiteralPath $reportDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $reportDirectory -Force | Out-Null
}
$files | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding UTF8

$missing = @($files | Where-Object { -not $_.SourceExists })
$identical = @($files | Where-Object { $_.Status -eq "Identical" })
$copyRequired = @($files | Where-Object { $_.Status -eq "CopyRequired" })

Write-Host ""
Write-Host "Weekly changed-files deployment: 2026-08-02 through 2026-08-05"
Write-Host "Source:        $source"
Write-Host "Destination:   $destination"
Write-Host "Backup:        $backup"
Write-Host "Report:        $report"
Write-Host "Execute:       $($Execute.IsPresent)"
Write-Host "Total files:   $($files.Count)"
Write-Host "Copy required: $($copyRequired.Count)"
Write-Host "Identical:     $($identical.Count)"
Write-Host "Missing:       $($missing.Count)"
Write-Host ""

$files | Select-Object Status, RelativePath | Format-Table -AutoSize

if ($missing.Count -gt 0) {
    Write-Host ""
    foreach ($file in $missing) {
        Write-Error "Missing source file: $($file.SourcePath)" -ErrorAction Continue
    }
    throw "Deployment stopped because every listed source file must exist."
}

if (-not $Execute) {
    Write-Host ""
    Write-Host "Dry run completed. No destination files were changed."
    Write-Host "Run again with -Execute to copy and verify the files."
    exit 0
}

$copied = 0
$backedUp = 0

foreach ($file in $copyRequired) {
    if ($file.TargetExists) {
        $backupDirectory = Split-Path -Parent $file.BackupPath
        if (-not (Test-Path -LiteralPath $backupDirectory -PathType Container)) {
            New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
        }
        Copy-Item -LiteralPath $file.TargetPath -Destination $file.BackupPath -Force
        $backedUp += 1
    }

    $targetDirectory = Split-Path -Parent $file.TargetPath
    if (-not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    }

    Copy-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    $copied += 1

    $verifiedHash = Get-Sha256 -Path $file.TargetPath
    if ($verifiedHash -ne $file.SourceHash) {
        throw "SHA-256 verification failed after copying: $($file.RelativePath)"
    }

    $file.TargetHash = $verifiedHash
    $file.Status = "CopiedAndVerified"
}

# Re-verify files that were already identical as part of the final result.
foreach ($file in $identical) {
    $verifiedHash = Get-Sha256 -Path $file.TargetPath
    if ($verifiedHash -ne $file.SourceHash) {
        throw "Final SHA-256 verification failed: $($file.RelativePath)"
    }
    $file.Status = "IdenticalAndVerified"
}

$files | Export-Csv -LiteralPath $report -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Deployment completed successfully."
Write-Host "Copied and verified: $copied"
Write-Host "Already identical:  $($identical.Count)"
Write-Host "Backed up:           $backedUp"
Write-Host "All $($files.Count) files match their source SHA-256 hashes."
