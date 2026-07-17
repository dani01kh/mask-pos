import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests


APP_VERSION = "3.15.4"
GITHUB_REPO = "dani01kh/mask-pos"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_SCRIPT_NAME = "install_maskpos_update.ps1"

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
    "pending_exchange_credit.json",
}
PROTECTED_DIRS = {
    "backups",
    "data",
    "receipts",
    "reports",
    "update_backups",
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
        raise RuntimeError(
            "The Mask POS GitHub release repository is not publicly reachable, "
            "or it does not exist. Make the repository public and publish a non-draft "
            "release with a MaskPOS ZIP asset."
        )
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
    asset_digest = str((asset or {}).get("digest") or "").strip().lower()
    asset_sha256 = asset_digest.split(":", 1)[1] if asset_digest.startswith("sha256:") else ""
    return {
        "available": bool(version and asset and is_newer_version(version)),
        "current_version": APP_VERSION,
        "version": version,
        "tag_name": tag,
        "release_name": str(release.get("name") or tag or "").strip(),
        "notes": str(release.get("body") or "").strip(),
        "asset_name": str((asset or {}).get("name") or "").strip(),
        "asset_sha256": asset_sha256,
        "download_url": str((asset or {}).get("browser_download_url") or "").strip(),
        "html_url": str(release.get("html_url") or "").strip(),
    }


def download_update(info: dict, progress=None) -> Path:
    url = str(info.get("download_url") or "").strip()
    if not url:
        raise RuntimeError("This GitHub release does not have a ZIP update asset.")

    update_dir = _update_root() / "downloads"
    update_dir.mkdir(parents=True, exist_ok=True)
    raw_name = str(info.get("asset_name") or "MaskPOS-update.zip").strip() or "MaskPOS-update.zip"
    asset_name = Path(raw_name.replace("\\", "/")).name
    if not asset_name.lower().endswith(".zip"):
        raise RuntimeError("The selected GitHub update asset is not a ZIP file.")
    zip_path = update_dir / asset_name
    expected_sha256 = str(info.get("asset_sha256") or "").strip().lower()
    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RuntimeError("GitHub returned an invalid SHA-256 digest for this update.")

    with requests.get(url, stream=True, timeout=30) as response:
        response.raise_for_status()
        total = int(response.headers.get("content-length") or 0)
        done = 0
        digest = hashlib.sha256()
        tmp_path = zip_path.with_suffix(zip_path.suffix + ".part")
        with open(tmp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                handle.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(tmp_path, zip_path)

    actual_sha256 = digest.hexdigest().lower()
    if expected_sha256 and actual_sha256 != expected_sha256:
        try:
            zip_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("The downloaded update failed its SHA-256 integrity check.")

    _validate_update_zip(zip_path)
    return zip_path


def _update_root() -> Path:
    base = str(os.environ.get("LOCALAPPDATA") or "").strip()
    root = Path(base) if base else Path(tempfile.gettempdir())
    return root / "MaskPOS" / "updates"


def _validate_update_zip(zip_path: Path) -> int:
    """Validate a release payload before the POS is allowed to close.

    Returns the total uncompressed payload size. The supported layout is either
    MaskPOS.exe at the ZIP root or one top-level folder containing MaskPOS.exe.
    """
    zip_path = Path(zip_path).resolve()
    if not zip_path.is_file() or not zipfile.is_zipfile(zip_path):
        raise RuntimeError("Downloaded update is not a valid ZIP file.")

    exe_roots = set()
    total_size = 0
    file_count = 0
    normalized_files = []
    seen_paths = set()
    with zipfile.ZipFile(zip_path, "r") as archive:
        for item in archive.infolist():
            raw = str(item.filename or "").replace("\\", "/")
            if not raw:
                continue
            if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
                raise RuntimeError("Update ZIP contains an unsafe absolute path.")
            parts = [part for part in raw.split("/") if part not in ("", ".")]
            if any(part == ".." for part in parts):
                raise RuntimeError("Update ZIP contains an unsafe parent path.")
            if item.is_dir():
                continue
            normalized = "/".join(parts)
            normalized_key = normalized.casefold()
            if normalized_key in seen_paths:
                raise RuntimeError("Update ZIP contains duplicate file paths.")
            seen_paths.add(normalized_key)
            normalized_files.append(parts)
            file_count += 1
            if file_count > 10000:
                raise RuntimeError("Update ZIP contains too many files.")
            total_size += max(0, int(item.file_size or 0))
            if total_size > 2 * 1024 * 1024 * 1024:
                raise RuntimeError("Update ZIP expands beyond the 2 GB safety limit.")
            compressed = max(1, int(item.compress_size or 0))
            if int(item.file_size or 0) > 8 * 1024 * 1024 and (int(item.file_size or 0) / compressed) > 250:
                raise RuntimeError("Update ZIP contains a suspiciously compressed file.")
            if parts and parts[-1].lower() == "maskpos.exe":
                if len(parts) == 1:
                    exe_roots.add("")
                elif len(parts) == 2:
                    exe_roots.add(parts[0])
                else:
                    raise RuntimeError("MaskPOS.exe is nested too deeply inside the update ZIP.")

    if len(exe_roots) != 1:
        raise RuntimeError("Update ZIP must contain exactly one MaskPOS.exe at the top level or inside one folder.")
    selected_root = next(iter(exe_roots))
    if selected_root:
        root_key = selected_root.casefold()
        if any(not parts or parts[0].casefold() != root_key for parts in normalized_files):
            raise RuntimeError("Update ZIP contains files outside its MaskPOS package folder.")
    if total_size <= 0:
        raise RuntimeError("Update ZIP is empty.")
    return total_size


def _find_installer_script() -> Path:
    candidates = []
    bundle_dir = getattr(sys, "_MEIPASS", "")
    if bundle_dir:
        candidates.extend([
            Path(bundle_dir) / INSTALLER_SCRIPT_NAME,
            Path(bundle_dir) / "scripts" / INSTALLER_SCRIPT_NAME,
        ])
    source_dir = Path(__file__).resolve().parent
    candidates.extend([
        source_dir / "scripts" / INSTALLER_SCRIPT_NAME,
        source_dir / INSTALLER_SCRIPT_NAME,
    ])
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "The safe update installer is missing from this app build. "
        "Use the USB update package instead."
    )


def _powershell_executable() -> str:
    found = shutil.which("powershell.exe") or shutil.which("powershell")
    if found:
        return found
    system_root = str(os.environ.get("SystemRoot") or r"C:\Windows")
    candidate = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError("Windows PowerShell is required to install this update.")


def _preflight_install(zip_path: Path, app_dir: Path, restart_exe: Path) -> int:
    payload_size = _validate_update_zip(zip_path)
    if not app_dir.is_dir():
        raise RuntimeError(f"Mask POS app folder was not found: {app_dir}")
    if not restart_exe.is_file():
        raise RuntimeError(f"MaskPOS.exe was not found: {restart_exe}")
    expected_restart = (app_dir / "MaskPOS.exe").resolve()
    if restart_exe != expected_restart:
        raise RuntimeError("The updater can only restart MaskPOS.exe from its current app folder.")

    probe = app_dir / f".maskpos_update_write_test_{os.getpid()}"
    try:
        with open(probe, "xb") as handle:
            handle.write(b"update-write-test")
        probe.unlink()
    except Exception as exc:
        try:
            probe.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError(
            "Mask POS cannot update its current folder. Close other copies and run it as administrator, then try again."
        ) from exc

    free_bytes = int(shutil.disk_usage(app_dir).free)
    required = max(200 * 1024 * 1024, (payload_size * 2) + (64 * 1024 * 1024))
    if free_bytes < required:
        raise RuntimeError(
            f"Not enough free disk space for a safe update. Need about {required / (1024 * 1024):.0f} MB free."
        )
    _powershell_executable()
    _find_installer_script()
    return payload_size


def launch_installer(
    zip_path: Path,
    app_dir: str,
    restart_exe: str | None = None,
    parent_pid: int | None = None,
    expected_version: str = "",
    expected_sha256: str = "",
) -> dict:
    zip_path = Path(zip_path).resolve()
    app_dir_path = Path(app_dir).resolve()
    restart = Path(restart_exe or sys.executable).resolve()
    _preflight_install(zip_path, app_dir_path, restart)

    update_root = _update_root()
    update_root.mkdir(parents=True, exist_ok=True)
    job_id = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}"
    job_dir = update_root / f"job_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=False)
    script_path = job_dir / INSTALLER_SCRIPT_NAME
    log_path = update_root / "install_maskpos_update.log"
    process_log_path = job_dir / "powershell_output.log"
    result_path = job_dir / "result.json"
    shutil.copy2(_find_installer_script(), script_path)

    args = [
        _powershell_executable(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-PackageZip",
        str(zip_path),
        "-AppDir",
        str(app_dir_path),
        "-RestartExe",
        str(restart),
        "-ParentPid",
        str(int(parent_pid or os.getpid())),
        "-LogPath",
        str(log_path),
        "-ResultPath",
        str(result_path),
        "-NonInteractive",
    ]
    expected_version = str(expected_version or "").strip().lstrip("vV")
    if expected_version:
        args.extend(["-ExpectedVersion", expected_version])
    expected_sha256 = str(expected_sha256 or "").strip().lower()
    if expected_sha256:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise RuntimeError("Invalid expected update SHA-256 value.")
        args.extend(["-ExpectedSha256", expected_sha256])

    with open(process_log_path, "ab", buffering=0) as process_log:
        process = subprocess.Popen(
            args,
            cwd=str(app_dir_path),
            stdin=subprocess.DEVNULL,
            stdout=process_log,
            stderr=subprocess.STDOUT,
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            ),
        )

    ready = False
    # Windows PowerShell 5.1 can take several seconds to initialize on older
    # store PCs or while antivirus scans the copied script.
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if result_path.is_file():
            try:
                # Windows PowerShell 5.1's `Set-Content -Encoding UTF8` writes
                # a BOM. utf-8-sig accepts that output as well as BOM-less JSON.
                state = json.loads(result_path.read_text(encoding="utf-8-sig")) or {}
                marker = str(state.get("state") or state.get("status") or "").strip().lower()
                if marker in {"waiting_for_parent", "ready", "running"}:
                    ready = True
                    break
                if marker in {"failed", "error"}:
                    raise RuntimeError(str(state.get("message") or "The update installer failed to start."))
            except RuntimeError:
                raise
            except Exception:
                pass
        exit_code = process.poll()
        if exit_code is not None:
            detail = ""
            try:
                detail = process_log_path.read_text(encoding="utf-8", errors="replace")[-800:].strip()
            except Exception:
                pass
            raise RuntimeError(
                f"The update installer exited before Mask POS closed (code {exit_code})."
                + (f"\n\n{detail}" if detail else "")
            )
        time.sleep(0.1)

    if not ready:
        try:
            process.terminate()
        except Exception:
            pass
        raise RuntimeError(
            "The update installer did not confirm that it was ready. "
            f"Mask POS stayed open. See {process_log_path}"
        )

    return {
        "job_dir": str(job_dir),
        "log_path": str(log_path),
        "result_path": str(result_path),
        "download_path": str(zip_path),
    }


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
