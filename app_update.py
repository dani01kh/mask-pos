import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import requests


APP_VERSION = "1.3.2"
GITHUB_REPO = "dani01kh/mask-pos"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

PROTECTED_FILES = {
    "pos.db",
    "pos copy.db",
    "pos_backup_pre_overwrite.db",
    "pos_config.json",
    "cloudflare_pos_config.json",
    "cloud_sync_device.json",
    "config.json",
    "maskpos.lock",
    "cashier_recovery.json",
}
PROTECTED_DIRS = {
    "backups",
    "data",
    "receipts",
    "reports",
    "__pycache__",
}


def current_version() -> str:
    return APP_VERSION


def _version_parts(value: str) -> tuple:
    text = str(value or "").strip().lstrip("vV")
    parts = []
    for piece in re.split(r"[.\-+_]", text):
        if piece.isdigit():
            parts.append(int(piece))
        elif piece:
            parts.append(piece.lower())
    return tuple(parts or [0])


def is_newer_version(remote: str, local: str | None = None) -> bool:
    return _version_parts(remote) > _version_parts(local or APP_VERSION)


def _asset_score(asset: dict) -> tuple:
    name = str(asset.get("name") or "").lower()
    is_zip = name.endswith(".zip")
    has_maskpos = "maskpos" in name or "mask-pos" in name
    return (1 if is_zip else 0, 1 if has_maskpos else 0, -len(name))


def check_for_update(timeout: int = 10) -> dict:
    response = requests.get(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"MaskPOS/{APP_VERSION}",
        },
        timeout=timeout,
    )
    if response.status_code == 404:
        return {
            "available": False,
            "current_version": APP_VERSION,
            "version": APP_VERSION,
            "tag_name": "",
            "release_name": "",
            "notes": "No releases found on GitHub.",
            "asset_name": "",
            "download_url": "",
            "html_url": "",
        }
    response.raise_for_status()
    release = response.json() or {}
    tag = str(release.get("tag_name") or release.get("name") or "").strip()
    version = tag.lstrip("vV")
    assets = release.get("assets") or []
    zip_assets = [
        a for a in assets
        if str(a.get("browser_download_url") or "").strip()
        and str(a.get("name") or "").lower().endswith(".zip")
    ]
    zip_assets.sort(key=_asset_score, reverse=True)
    asset = zip_assets[0] if zip_assets else None
    return {
        "available": bool(version and asset and is_newer_version(version)),
        "current_version": APP_VERSION,
        "version": version,
        "tag_name": tag,
        "release_name": str(release.get("name") or tag or "").strip(),
        "notes": str(release.get("body") or "").strip(),
        "asset_name": str((asset or {}).get("name") or "").strip(),
        "download_url": str((asset or {}).get("browser_download_url") or "").strip(),
        "html_url": str(release.get("html_url") or "").strip(),
    }


def download_update(info: dict, progress=None) -> Path:
    url = str(info.get("download_url") or "").strip()
    if not url:
        raise RuntimeError("This GitHub release does not have a ZIP update asset.")

    update_dir = Path(tempfile.gettempdir()) / "MaskPOS_Update"
    update_dir.mkdir(parents=True, exist_ok=True)
    zip_path = update_dir / (str(info.get("asset_name") or "MaskPOS-update.zip").strip() or "MaskPOS-update.zip")

    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        tmp_path = zip_path.with_suffix(zip_path.suffix + ".part")
        with open(tmp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(tmp_path, zip_path)

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("Downloaded update is not a valid ZIP file.")
    return zip_path


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def launch_installer(zip_path: Path, app_dir: str, restart_exe: str | None = None, parent_pid: int | None = None) -> None:
    zip_path = Path(zip_path).resolve()
    app_dir_path = Path(app_dir).resolve()
    restart = Path(restart_exe or sys.executable).resolve()
    script_dir = Path(tempfile.gettempdir()) / "MaskPOS_Update"
    script_dir.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / "install_maskpos_update.ps1"
    log_path = script_dir / "install_maskpos_update.log"
    extract_dir = script_dir / f"extract_{int(time.time())}"

    protected_files = ", ".join(_ps_quote(v) for v in sorted(PROTECTED_FILES))
    protected_dirs = ", ".join(_ps_quote(v) for v in sorted(PROTECTED_DIRS))

    script = f"""
$ErrorActionPreference = "Stop"
$zipPath = {_ps_quote(str(zip_path))}
$appDir = {_ps_quote(str(app_dir_path))}
$restartExe = {_ps_quote(str(restart))}
$extractDir = {_ps_quote(str(extract_dir))}
$logPath = {_ps_quote(str(log_path))}
$parentPid = {int(parent_pid or os.getpid())}
$protectedFiles = @({protected_files})
$protectedDirs = @({protected_dirs})

Start-Sleep -Seconds 1
try {{
  $proc = Get-Process -Id $parentPid -ErrorAction SilentlyContinue
  if ($proc) {{ $proc.WaitForExit(120000) }}
}} catch {{ }}

if (Test-Path $extractDir) {{ Remove-Item -LiteralPath $extractDir -Recurse -Force }}
New-Item -ItemType Directory -Path $extractDir | Out-Null
Expand-Archive -LiteralPath $zipPath -DestinationPath $extractDir -Force

$source = $extractDir
if (Test-Path (Join-Path $extractDir "MaskPOS.exe")) {{
  $source = $extractDir
}} elseif (Test-Path (Join-Path $extractDir "MaskPOS\\MaskPOS.exe")) {{
  $source = Join-Path $extractDir "MaskPOS"
}} else {{
  $dirs = @(Get-ChildItem -LiteralPath $extractDir -Directory)
  if ($dirs.Count -eq 1) {{ $source = $dirs[0].FullName }}
}}

if (!(Test-Path (Join-Path $source "MaskPOS.exe"))) {{
  throw "Update ZIP must contain MaskPOS.exe at the top level or inside one folder."
}}

foreach ($name in $protectedFiles) {{
  $candidate = Join-Path $source $name
  if (Test-Path $candidate) {{ Remove-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue }}
}}
foreach ($name in $protectedDirs) {{
  $candidate = Join-Path $source $name
  if (Test-Path $candidate) {{ Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction SilentlyContinue }}
}}

$backupDir = Join-Path $appDir ("update_backup_" + (Get-Date -Format "yyyyMMdd_HHmmss"))
New-Item -ItemType Directory -Path $backupDir | Out-Null
foreach ($name in @("MaskPOS.exe", "_internal", "assets")) {{
  $existing = Join-Path $appDir $name
  if (Test-Path $existing) {{
    Move-Item -LiteralPath $existing -Destination (Join-Path $backupDir $name) -Force
  }}
}}

robocopy $source $appDir /E /NFL /NDL /NJH /NJS /NP /XD $protectedDirs /XF $protectedFiles | Out-Null
$code = $LASTEXITCODE
if ($code -ge 8) {{ throw "File copy failed with robocopy code $code." }}

Start-Process -FilePath $restartExe -WorkingDirectory $appDir
"""[1:]

    script_path.write_text(script, encoding="utf-8")
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(f"\n--- Mask POS update started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")

    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ],
        cwd=str(app_dir_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
    )


def describe_update(info: dict) -> str:
    version = str(info.get("version") or "").strip() or "unknown"
    asset = str(info.get("asset_name") or "").strip()
    text = f"Version {version} is available."
    if asset:
        text += f"\n\nDownload: {asset}"
    notes = str(info.get("notes") or "").strip()
    if notes:
        text += "\n\n" + notes[:900]
    return text
