param(
    [string]$Source = "C:\Apps\Workflow-PNCECS_del",
    [string]$Destination = "C:\Apps\Workflow_PNCECS",
    [string[]]$FolderNames = @(),
    [switch]$Apply,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"

function Resolve-FullPath {
    param([string]$Path)
    $executionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
}

$SourceFull = Resolve-FullPath $Source
$DestinationFull = Resolve-FullPath $Destination

Write-Host "Source:      $SourceFull"
Write-Host "Destination: $DestinationFull"

if (-not (Test-Path -LiteralPath $SourceFull -PathType Container)) {
    throw "Source folder does not exist: $SourceFull"
}

if (-not (Test-Path -LiteralPath $DestinationFull -PathType Container)) {
    if ($Apply) {
        New-Item -ItemType Directory -Path $DestinationFull -Force | Out-Null
        Write-Host "Created destination folder."
    } else {
        Write-Host "[DRY RUN] Would create destination folder."
    }
}

if ($FolderNames.Count -gt 0) {
    $Folders = foreach ($Name in $FolderNames) {
        $Path = Join-Path $SourceFull $Name
        if (Test-Path -LiteralPath $Path -PathType Container) {
            Get-Item -LiteralPath $Path
        } else {
            Write-Warning "Folder not found in source, skipping: $Name"
        }
    }
} else {
    $Folders = Get-ChildItem -LiteralPath $SourceFull -Directory -Force
}

if (-not $Folders -or $Folders.Count -eq 0) {
    Write-Host "No folders to move."
    exit 0
}

$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"

foreach ($Folder in $Folders) {
    $Target = Join-Path $DestinationFull $Folder.Name

    if (Test-Path -LiteralPath $Target) {
        if (-not $ReplaceExisting) {
            Write-Warning "Destination already exists, skipping: $Target"
            continue
        }

        $BackupTarget = Join-Path $DestinationFull "$($Folder.Name)_before_move_$Stamp"
        if ($Apply) {
            Move-Item -LiteralPath $Target -Destination $BackupTarget
            Write-Host "Existing destination moved to: $BackupTarget"
        } else {
            Write-Host "[DRY RUN] Would move existing destination to: $BackupTarget"
        }
    }

    if ($Apply) {
        Move-Item -LiteralPath $Folder.FullName -Destination $Target
        Write-Host "Moved: $($Folder.FullName) -> $Target"
    } else {
        Write-Host "[DRY RUN] Would move: $($Folder.FullName) -> $Target"
    }
}

if (-not $Apply) {
    Write-Host ""
    Write-Host "Dry run only. Re-run with -Apply to perform the move."
}
