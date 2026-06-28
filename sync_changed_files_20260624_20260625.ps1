param(
    [string]$SourceRoot = "C:\Users\Administrator\Desktop\Workflow-PNCECS",
    [string]$DestinationRoot = "C:\Apps\Workflow_PNCECS",
    [datetime]$StartDate = "2026-06-24 00:00:00",
    [datetime]$EndDateExclusive = "2026-06-26 00:00:00",
    [ValidateSet("Copy", "Move")]
    [string]$Mode = "Copy",
    [switch]$Execute,
    [switch]$IncludeRuntime,
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

function Test-ExcludedRelativePath {
    param([string]$RelativePath)

    $normalized = $RelativePath -replace "/", "\"
    $parts = $normalized -split "\\"

    $baseExcludes = @(".git", ".idea", "__pycache__")
    if ($parts | Where-Object { $_ -in $baseExcludes }) {
        return $true
    }

    if (-not $IncludeRuntime) {
        $runtimeExcludes = @("instance", "logs", "tmp", "storage")
        if ($parts | Where-Object { $_ -in $runtimeExcludes }) {
            return $true
        }
    }

    return $false
}

$source = Resolve-ExistingDirectory -Path $SourceRoot -Name "SourceRoot"
$destination = $DestinationRoot.TrimEnd("\")

if ($source -ieq $destination) {
    throw "SourceRoot and DestinationRoot are the same path."
}

if (-not $ReportPath) {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $ReportPath = Join-Path (Get-Location).Path "changed_files_20260624_20260625_$stamp.csv"
}

$sourcePrefixLength = $source.Length + 1

$files = Get-ChildItem -LiteralPath $source -Recurse -File -Force |
    Where-Object {
        $_.LastWriteTime -ge $StartDate -and
        $_.LastWriteTime -lt $EndDateExclusive
    } |
    ForEach-Object {
        $relative = $_.FullName.Substring($sourcePrefixLength)
        if (Test-ExcludedRelativePath -RelativePath $relative) {
            return
        }
        [pscustomobject]@{
            LastWriteTime = $_.LastWriteTime
            SizeBytes     = $_.Length
            RelativePath  = $relative
            SourcePath    = $_.FullName
            TargetPath    = Join-Path $destination $relative
        }
    } |
    Sort-Object LastWriteTime, RelativePath

$files | Export-Csv -LiteralPath $ReportPath -NoTypeInformation -Encoding UTF8

Write-Host ""
Write-Host "Changed files from $($StartDate.ToString('yyyy-MM-dd HH:mm:ss')) to before $($EndDateExclusive.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "Source:      $source"
Write-Host "Destination: $destination"
Write-Host "Mode:        $Mode"
Write-Host "Execute:     $($Execute.IsPresent)"
Write-Host "Runtime:     $(if ($IncludeRuntime) { 'included' } else { 'excluded: instance, logs, tmp, storage' })"
Write-Host "Report:      $ReportPath"
Write-Host "Count:       $($files.Count)"
Write-Host ""

if ($files.Count -eq 0) {
    Write-Host "No matching files found."
    exit 0
}

$files | Format-Table LastWriteTime, SizeBytes, RelativePath -AutoSize

if (-not $Execute) {
    Write-Host ""
    Write-Host "Dry run only. To transfer files, rerun with -Execute."
    Write-Host "For copying: .\sync_changed_files_20260624_20260625.ps1 -Execute -Mode Copy"
    Write-Host "For moving:  .\sync_changed_files_20260624_20260625.ps1 -Execute -Mode Move"
    exit 0
}

foreach ($file in $files) {
    $targetDir = Split-Path -Parent $file.TargetPath
    if (-not (Test-Path -LiteralPath $targetDir -PathType Container)) {
        New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
    }

    if ($Mode -eq "Move") {
        Move-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    } else {
        Copy-Item -LiteralPath $file.SourcePath -Destination $file.TargetPath -Force
    }
}

Write-Host ""
Write-Host "$Mode completed for $($files.Count) files."
