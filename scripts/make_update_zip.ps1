param(
    [Parameter(Mandatory = $true)]
    [string]$Version,

    [string]$DistPath = "dist\MaskPOS",
    [string]$OutputDir = "release"
)

$ErrorActionPreference = "Stop"

if (!(Test-Path $DistPath)) {
    throw "Build output not found: $DistPath"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$zipPath = Join-Path $OutputDir "MaskPOS-v$Version.zip"
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("MaskPOS_release_" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
$stage = Join-Path $temp "MaskPOS"
Copy-Item -LiteralPath $DistPath -Destination $stage -Recurse -Force

$protectedFiles = @(
    "pos.db",
    "pos copy.db",
    "pos_backup_pre_overwrite.db",
    "pos_config.json",
    "cloudflare_pos_config.json",
    "cloud_sync_device.json",
    "config.json",
    "maskpos.lock"
)
$protectedDirs = @("backups", "data", "receipts", "reports", "__pycache__")

foreach ($name in $protectedFiles) {
    $path = Join-Path $stage $name
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Force
    }
}
foreach ($name in $protectedDirs) {
    $path = Join-Path $stage $name
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force
    }
}

Compress-Archive -Path $stage -DestinationPath $zipPath -Force
Remove-Item -LiteralPath $temp -Recurse -Force

Write-Host "Created $zipPath"
