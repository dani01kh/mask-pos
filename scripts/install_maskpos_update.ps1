param(
    [Parameter(Mandatory = $true)]
    [string]$PackageZip,

    [Parameter(Mandatory = $true)]
    [string]$AppDir,

    [Parameter(Mandatory = $true)]
    [string]$RestartExe,

    [Parameter(Mandatory = $true)]
    [int]$ParentPid,

    [Parameter(Mandatory = $true)]
    [string]$LogPath,

    [Parameter(Mandatory = $true)]
    [string]$ResultPath,

    [switch]$NonInteractive,
    [string]$ExpectedVersion,
    [string]$ExpectedSha256
)

$ErrorActionPreference = "Stop"

function Write-Log($msg) {
    $time = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogPath -Value "[$time] $msg" -ErrorAction SilentlyContinue
}

Write-Log "--- Mask POS update execution started ---"
Write-Log "PackageZip: $PackageZip"
Write-Log "AppDir: $AppDir"
Write-Log "RestartExe: $RestartExe"
Write-Log "ParentPid: $ParentPid"

# 1. Write initial status ready so the parent app knows it can safely exit
try {
    $resultDir = [System.IO.Path]::GetDirectoryName($ResultPath)
    if (!(Test-Path $resultDir)) {
        New-Item -ItemType Directory -Path $resultDir -Force | Out-Null
    }
    $state = @{ state = "ready"; status = "ready"; message = "Installer script running, waiting for parent." }
    $state | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
    Write-Log "Wrote ready status to $ResultPath"
} catch {
    Write-Log "Failed to write initial state to $ResultPath: $_"
}

# 2. Wait for parent process to exit
Write-Log "Waiting for parent process $ParentPid to exit..."
$timeout = 20 # seconds
$elapsed = 0
while ($elapsed -lt $timeout) {
    $proc = Get-Process -Id $ParentPid -ErrorAction SilentlyContinue
    if (!$proc) {
        Write-Log "Parent process exited."
        break
    }
    Start-Sleep -Seconds 1
    $elapsed++
}
if ($elapsed -ge $timeout) {
    Write-Log "Parent process did not exit in time. Proceeding anyway, but file replacement may fail."
}

# 3. Verify SHA-256 hash if provided
if ($ExpectedSha256) {
    Write-Log "Verifying SHA-256 integrity of zip package..."
    $actualHash = (Get-FileHash -Path $PackageZip -Algorithm SHA256).Hash.ToLower()
    if ($actualHash -ne $ExpectedSha256.ToLower()) {
        $msg = "SHA-256 verification failed. Expected: $ExpectedSha256, Got: $actualHash"
        Write-Log $msg
        @{ state = "failed"; status = "failed"; message = $msg } | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
        throw $msg
    }
    Write-Log "SHA-256 integrity verified successfully."
}

# 4. Extract update package
$tempExtract = Join-Path ([System.IO.Path]::GetTempPath()) ("MaskPOS_Update_" + [guid]::NewGuid().ToString("N"))
Write-Log "Extracting update package to: $tempExtract"
New-Item -ItemType Directory -Path $tempExtract -Force | Out-Null

try {
    Expand-Archive -LiteralPath $PackageZip -DestinationPath $tempExtract -Force
} catch {
    $msg = "Failed to extract update ZIP archive: $_"
    Write-Log $msg
    @{ state = "failed"; status = "failed"; message = $msg } | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
    throw $msg
}

# 5. Locate source build folder containing MaskPOS.exe
$source = $tempExtract
if (Test-Path (Join-Path $tempExtract "MaskPOS.exe")) {
    $source = $tempExtract
} elseif (Test-Path (Join-Path $tempExtract "MaskPOS\MaskPOS.exe")) {
    $source = Join-Path $tempExtract "MaskPOS"
} else {
    $dirs = Get-ChildItem -Path $tempExtract -Directory
    if ($dirs.Count -eq 1) {
        $source = $dirs[0].FullName
    }
}

if (!(Test-Path (Join-Path $source "MaskPOS.exe"))) {
    $msg = "Could not find MaskPOS.exe in the extracted package."
    Write-Log $msg
    @{ state = "failed"; status = "failed"; message = $msg } | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
    throw $msg
}
Write-Log "Found update source directory: $source"

# 6. Back up current installation
$backupRoot = Join-Path $AppDir "update_backups"
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
$backupDir = Join-Path $backupRoot ("update_backup_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
Write-Log "Creating backup of current version in: $backupDir"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

foreach ($name in @("MaskPOS.exe", "_internal", "assets")) {
    $existing = Join-Path $AppDir $name
    if (Test-Path $existing) {
        Move-Item -LiteralPath $existing -Destination (Join-Path $backupDir $name) -Force -ErrorAction SilentlyContinue
    }
}

# 7. Copy new files using Robocopy (excluding databases and configurations)
$protectedFiles = @(
    "pos.db", "pos copy.db", "pos_backup_pre_overwrite.db",
    "pos_config.json", "cloudflare_pos_config.json", "cloud_sync_device.json",
    "config.json", "maskpos.lock", "cashier_recovery.json", "pending_exchange_credit.json"
)
$protectedDirs = @(
    "backups", "data", "receipts", "reports", "update_backups", "__pycache__"
)

Write-Log "Running Robocopy to copy updated files..."
$robocopyParams = @($source, $AppDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
if ($protectedDirs.Count -gt 0) {
    $robocopyParams += "/XD"
    $robocopyParams += $protectedDirs
}
if ($protectedFiles.Count -gt 0) {
    $robocopyParams += "/XF"
    $robocopyParams += $protectedFiles
}

& robocopy @robocopyParams
$exitCode = $LASTEXITCODE
Write-Log "Robocopy exited with code: $exitCode"

if ($exitCode -ge 8) {
    $msg = "Robocopy failed to copy update files (Exit Code: $exitCode)."
    Write-Log $msg
    # Try to restore backup on failure
    Write-Log "Attempting to roll back backup..."
    foreach ($name in @("MaskPOS.exe", "_internal", "assets")) {
        $backed = Join-Path $backupDir $name
        if (Test-Path $backed) {
            Move-Item -LiteralPath $backed -Destination (Join-Path $AppDir $name) -Force -ErrorAction SilentlyContinue
        }
    }
    @{ state = "failed"; status = "failed"; message = $msg } | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
    throw $msg
}

# 8. Clean up temp extraction folder
try {
    Remove-Item -LiteralPath $tempExtract -Recurse -Force
    Write-Log "Cleaned up temporary files."
} catch {
    Write-Log "Warning: Could not remove temporary folder: $_"
}

# 9. Update final result state and relaunch
Write-Log "Relaunching updated Mask POS executable: $RestartExe"
try {
    @{ state = "running"; status = "success"; message = "Update applied successfully. Relaunching." } | ConvertTo-Json | Set-Content -Path $ResultPath -Encoding UTF8 -Force
} catch {}

Start-Process -FilePath $RestartExe -WorkingDirectory $AppDir
Write-Log "--- Update installer completed successfully ---"
