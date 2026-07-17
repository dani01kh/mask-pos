"""
backend.py - Switchable backend for Mask POS

Modes:
  - standalone: use local SQLite directly (pos_logic)
  - host: start local FastAPI server and use it (shared DB)
  - connect: use FastAPI server on another PC (shared DB)
  - cloud: use local SQLite cache and sync through the hosted Cloudflare database

This module exposes the SAME function names as pos_logic.py so app.py can stay mostly unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import re
import shutil
import sys
import time
import threading
import socket
import uuid

try:
    import supabase_sync
except Exception:
    supabase_sync = None

# --- Windows subprocess helpers (prevent console windows flashing) ---
def _no_window_kwargs():
    """Return subprocess kwargs to prevent console windows flashing on Windows."""
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return {"creationflags": creationflags, "startupinfo": si}
        except Exception:
            return {"creationflags": creationflags}
    return {}
from pathlib import Path
from typing import Tuple

def _runtime_base_dir() -> Path:
    """Folder where persistent files (pos.db, pos_config.json) should live."""
    try:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
    except Exception:
        pass
    return Path(__file__).resolve().parent

BASE_DIR = _runtime_base_dir()
CONFIG_PATH = BASE_DIR / "pos_config.json"
try:
    os.makedirs(BASE_DIR, exist_ok=True)
    os.environ["MASKPOS_DATA_DIR"] = str(BASE_DIR)
    os.environ["MASKPOS_DB_PATH"] = str(BASE_DIR / "pos.db")
except Exception:
    pass


# Runtime state
_MODE = "standalone"   # standalone | host | connect | cloud
BASE_URL = ""          # http://IP:PORT
_SERVER_PROC = None    # subprocess.Popen for host mode
_SERVER_THREAD = None  # threading.Thread for host mode when frozen

# Simple guard for modes that can write to the shared/cloud data.
MODE_ADMIN_PASSWORD = os.environ.get("MASKPOS_ADMIN_PASSWORD", "1234")

# Connection monitoring (JOIN/connect mode)
_CONNECTED = True
_LAST_OK_TS = 0.0
_STATUS_LOCK = threading.Lock()
_STOP_HEARTBEAT = False
_HEARTBEAT_THREAD = None
_BACKUP_STOP_EVENT = threading.Event()
_CLOUD_BACKFILL_STARTED = False
_BACKUP_THREAD = None

# Host discovery (LAN broadcast)
DISCOVERY_PORT = 39555
DISCOVERY_MAGIC = "MASKPOS_DISCOVERY_V1"
_DISCOVERY_STOP = False
_DISCOVERY_THREAD = None



def backend_mode() -> str:
    return _MODE


def connection_role() -> str:
    """Returns 'HOST' | 'JOIN' | 'CLOUD' | '' (standalone)."""
    if _MODE == "host":
        return "HOST"
    if _MODE == "connect":
        return "JOIN"
    if _MODE == "cloud":
        return "CLOUD"
    return ""


def supabase_emergency_enabled() -> bool:
    """Compatibility name retained for app imports; Cloud mode is active when configured."""
    try:
        return bool(supabase_sync is not None and supabase_sync.cloud_configured())
    except Exception:
        return False


def verify_mode_admin_password(password: str) -> bool:
    return str(password or "") == MODE_ADMIN_PASSWORD


def _set_connected(v: bool) -> None:
    global _CONNECTED, _LAST_OK_TS
    with _STATUS_LOCK:
        _CONNECTED = bool(v)
        if v:
            _LAST_OK_TS = time.time()


def is_connected() -> bool:
    """For JOIN (connect) mode: True if host is reachable.
    For HOST/standalone: always True.
    """
    if _MODE != "connect":
        return True
    with _STATUS_LOCK:
        return bool(_CONNECTED)


def last_ok_age_seconds() -> float:
    """Seconds since the last successful /health ping (JOIN mode)."""
    if _MODE != "connect":
        return 0.0
    with _STATUS_LOCK:
        if _LAST_OK_TS <= 0:
            return 1e9
        return max(0.0, time.time() - _LAST_OK_TS)


def _heartbeat_loop() -> None:
    """Background thread: keeps _CONNECTED updated in JOIN mode."""
    http = _http()
    while True:
        with _STATUS_LOCK:
            if _STOP_HEARTBEAT:
                return
        try:
            j = http.get(BASE_URL + "/health", timeout=1.2)
            _set_connected(bool(j.get("ok")))
        except Exception:
            _set_connected(False)
        time.sleep(1.0)


def _start_heartbeat() -> None:
    global _HEARTBEAT_THREAD
    if _MODE != "connect":
        return
    if _HEARTBEAT_THREAD is not None and _HEARTBEAT_THREAD.is_alive():
        return
    with _STATUS_LOCK:
        # reset stop flag in case of restart
        global _STOP_HEARTBEAT, _DISCOVERY_STOP
        _STOP_HEARTBEAT = False
    t = threading.Thread(target=_heartbeat_loop, daemon=True)
    _HEARTBEAT_THREAD = t
    t.start()


# ---------------- Host discovery (LAN) ----------------

def _best_local_ip() -> str:
    """Best effort local LAN IP (not 127.0.0.1)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and ip != "127.0.0.1":
                return ip
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception:
        pass
    return "127.0.0.1"


def _discovery_broadcast_loop(app_title: str, port: int) -> None:
    """Broadcast this host on the LAN so JOIN PCs can auto-find it."""
    global _DISCOVERY_STOP
    ip = _best_local_ip()
    payload = {
        "magic": DISCOVERY_MAGIC,
        "name": str(app_title or "Mask POS"),
        "ip": ip,
        "port": int(port),
        "url": f"http://{ip}:{int(port)}",
        "id": str(uuid.getnode()),
        "ts": int(time.time()),
    }

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        return

    try:
        msg = json.dumps(payload).encode("utf-8")
        while True:
            with _STATUS_LOCK:
                if _DISCOVERY_STOP:
                    return
            try:
                sock.sendto(msg, ("255.255.255.255", DISCOVERY_PORT))
            except Exception:
                pass
            time.sleep(1.0)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _start_discovery_broadcast(app_title: str, port: int) -> None:
    global _DISCOVERY_THREAD, _DISCOVERY_STOP
    if _DISCOVERY_THREAD is not None and _DISCOVERY_THREAD.is_alive():
        return
    with _STATUS_LOCK:
        _DISCOVERY_STOP = False
    t = threading.Thread(target=_discovery_broadcast_loop, args=(app_title, port), daemon=True)
    _DISCOVERY_THREAD = t
    t.start()


def discover_hosts(timeout_sec: float = 1.6) -> list[dict]:
    """Listen briefly for host broadcasts. Returns list of dicts with name/ip/port/url."""
    results: dict[str, dict] = {}
    t_end = time.time() + max(0.2, float(timeout_sec))

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            return []
        sock.settimeout(0.25)
    except Exception:
        return []

    try:
        while time.time() < t_end:
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                continue
            except Exception:
                continue

            try:
                msg = json.loads(data.decode("utf-8", errors="ignore") or "{}")
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue
            if msg.get("magic") != DISCOVERY_MAGIC:
                continue

            url = str(msg.get("url") or "").strip()
            if not url.startswith("http"):
                continue

            msg["from"] = addr[0]
            results[url] = msg
    finally:
        try:
            sock.close()
        except Exception:
            pass

    out = list(results.values())
    out.sort(key=lambda d: (str(d.get("name") or ""), str(d.get("url") or "")))
    return out



# ---------------- CONFIG HELPERS ----------------



def discover_hosts_scan_http(port: int = 8000, timeout_sec: float = 0.35, max_seconds: float = 3.5, limit: int = 12) -> list[dict]:
    """
    Fallback discovery: scan local /24 subnet(s) and probe http://IP:port/health.
    Useful when UDP broadcast is blocked by firewall/router isolation.
    Returns list of dicts {name, ip, port, url}.
    """
    port = int(port or 8000)
    timeout_sec = float(timeout_sec or 0.35)
    max_seconds = float(max_seconds or 3.5)
    limit = int(limit or 12)

    # Local IP best-effort
    def _local_ip() -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Doesn't actually send packets; just picks an outbound interface
                s.connect(("8.8.8.8", 80))
                ip = s.getsockname()[0]
                if ip and ip != "0.0.0.0":
                    return ip
            finally:
                try:
                    s.close()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            ip = socket.gethostbyname(socket.gethostname())
            return ip
        except Exception:
            pass
        # Windows fallback: parse ipconfig to find a real IPv4 (helps on wired LAN with no internet)
        try:
            if os.name == "nt":
                out = subprocess.check_output(["ipconfig"], text=True, errors="ignore")
                ips = re.findall(r"IPv4 Address[^\n:]*:\s*([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", out)
                for cand in ips:
                    if cand and not cand.startswith("127.") and not cand.startswith("169.254."):
                        return cand.strip()
        except Exception:
            pass
        return ""

    ip0 = _local_ip()
    # If we couldn't detect, just return empty
    if not ip0 or "." not in ip0:
        return []

    # Build candidate IPs (assume /24)
    parts = ip0.split(".")
    if len(parts) != 4:
        return []
    base = ".".join(parts[:3]) + "."

    # Probe function
    http = _http()
    found: list[dict] = []
    lock = threading.Lock()
    stop_flag = {"stop": False}

    def probe(i: int):
        if stop_flag["stop"]:
            return
        ip = f"{base}{i}"
        # skip self
        if ip == ip0:
            return
        url = f"http://{ip}:{port}"
        try:
            j = http.get(url + "/health", timeout=timeout_sec)
            if j.get("ok"):
                name = str(j.get("name") or "Host").strip() or "Host"
                with lock:
                    if len(found) < limit and all(x.get("ip") != ip for x in found):
                        found.append({"name": name, "ip": ip, "port": port, "url": url})
                        if len(found) >= limit:
                            stop_flag["stop"] = True
        except Exception:
            return

    # Threaded scan to keep UI snappy
    start_ts = time.time()
    workers = []
    max_workers = 64
    next_i = 1

    while next_i <= 254 and (time.time() - start_ts) < max_seconds and not stop_flag["stop"]:
        # fill worker pool
        while len(workers) < max_workers and next_i <= 254 and not stop_flag["stop"]:
            t = threading.Thread(target=probe, args=(next_i,), daemon=True)
            t.start()
            workers.append(t)
            next_i += 1

        # prune finished
        alive = []
        for t in workers:
            if t.is_alive():
                alive.append(t)
        workers = alive

        time.sleep(0.02)

    # short join
    for t in workers:
        try:
            t.join(timeout=0.05)
        except Exception:
            pass

    return found

def _load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_config(cfg: dict) -> None:
    tmp_path = CONFIG_PATH.with_name(
        f"{CONFIG_PATH.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def get_ai_assistant_config() -> dict:
    """Return local Gemini assistant settings without exposing them to cloud sync."""
    cfg = _load_config()
    return {
        "enabled": bool(cfg.get("ai_assistant_enabled", False)),
        "provider": "gemini_free",
        "model": str(cfg.get("gemini_model") or "gemini-2.5-flash").strip(),
        "api_key": str(cfg.get("gemini_api_key") or "").strip(),
    }


def set_ai_assistant_config(*, enabled: bool, api_key: str, model: str = "gemini-2.5-flash") -> None:
    """Save Gemini settings only in the protected local pos_config.json file."""
    cfg = _load_config()
    cfg["ai_assistant_enabled"] = bool(enabled)
    cfg["gemini_api_key"] = str(api_key or "").strip()
    cfg["gemini_model"] = str(model or "gemini-2.5-flash").strip()
    _save_config(cfg)


DEFAULT_DAILY_REPORT_RECIPIENTS = [
    "dani123khoueiry@gmail.com",
    "assaadmask@gmail.com",
]


def _split_email_recipients(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = []
        for item in value:
            raw.extend(str(item or "").replace(";", ",").split(","))
    else:
        raw = str(value or "").replace(";", ",").split(",")
    out = []
    seen = set()
    for item in raw:
        email = str(item or "").strip()
        if not email or "@" not in email:
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(email)
    return out


def normalize_daily_report_send_time(value, default: str = "19:50") -> str:
    text = str(value or "").strip().lower()
    if not text:
        text = str(default or "19:50").strip().lower()

    text = re.sub(r"\s+", "", text).replace(".", ":")
    suffix = ""
    if text.endswith("am") or text.endswith("pm"):
        suffix = text[-2:]
        text = text[:-2]

    try:
        if ":" in text:
            hh_text, mm_text = text.split(":", 1)
        else:
            hh_text, mm_text = text, "0"
        hh = int(hh_text)
        mm = int(mm_text)

        if suffix:
            if hh < 1 or hh > 12:
                raise ValueError("12-hour time must use 1-12")
            if suffix == "am":
                hh = 0 if hh == 12 else hh
            else:
                hh = 12 if hh == 12 else hh + 12

        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            raise ValueError("time out of range")
        return f"{hh:02d}:{mm:02d}"
    except Exception:
        if str(default or "").strip() != "19:50":
            return normalize_daily_report_send_time(default, "19:50")
        return "19:50"


def get_daily_report_email_config() -> dict:
    cfg = _load_config()
    recipients = _split_email_recipients(cfg.get("daily_report_recipients") or DEFAULT_DAILY_REPORT_RECIPIENTS)
    sender = str(cfg.get("daily_report_sender_email") or os.environ.get("MASKPOS_EMAIL_SENDER", "") or "").strip()
    username = str(cfg.get("daily_report_smtp_username") or os.environ.get("MASKPOS_EMAIL_USERNAME", "") or sender).strip()
    password = str(cfg.get("daily_report_smtp_password") or os.environ.get("MASKPOS_EMAIL_PASSWORD", "") or "").strip()
    try:
        smtp_port = int(cfg.get("daily_report_smtp_port") or 587)
    except Exception:
        smtp_port = 587
    return {
        "enabled": bool(cfg.get("daily_report_email_enabled", True)),
        "recipients": recipients or list(DEFAULT_DAILY_REPORT_RECIPIENTS),
        "sender_email": sender,
        "smtp_server": str(cfg.get("daily_report_smtp_server") or "smtp.gmail.com").strip(),
        "smtp_port": smtp_port,
        "smtp_username": username,
        "smtp_password": password,
        "use_tls": bool(cfg.get("daily_report_smtp_use_tls", True)),
        "send_time": normalize_daily_report_send_time(cfg.get("daily_report_send_time") or "19:50"),
        "last_sent_date": str(cfg.get("daily_report_last_sent_date") or "").strip(),
        "last_sent_at": str(cfg.get("daily_report_last_sent_at") or "").strip(),
        "last_sent_source": str(cfg.get("daily_report_last_sent_source") or "").strip(),
        "last_auto_sent_date": str(cfg.get("daily_report_last_auto_sent_date") or "").strip(),
        "last_auto_sent_at": str(cfg.get("daily_report_last_auto_sent_at") or "").strip(),
    }


def set_daily_report_email_config(
    enabled=True,
    recipients=None,
    sender_email="",
    smtp_server="smtp.gmail.com",
    smtp_port=587,
    smtp_username="",
    smtp_password=None,
    use_tls=True,
    send_time="19:50",
) -> None:
    cfg = _load_config()
    cfg["daily_report_email_enabled"] = bool(enabled)
    cfg["daily_report_recipients"] = _split_email_recipients(recipients) or list(DEFAULT_DAILY_REPORT_RECIPIENTS)
    cfg["daily_report_sender_email"] = str(sender_email or "").strip()
    
    server_cleaned = str(smtp_server or "smtp.gmail.com").strip()
    for prefix in ("smtp://", "smtps://", "http://", "https://"):
        if server_cleaned.lower().startswith(prefix):
            server_cleaned = server_cleaned[len(prefix):]
    server_cleaned = server_cleaned.split("/")[0].split(":")[0]
    cfg["daily_report_smtp_server"] = server_cleaned
    
    try:
        cfg["daily_report_smtp_port"] = int(smtp_port or 587)
    except Exception:
        cfg["daily_report_smtp_port"] = 587
    cfg["daily_report_smtp_username"] = str(smtp_username or "").strip()
    if smtp_password is not None:
        cfg["daily_report_smtp_password"] = str(smtp_password or "").strip()
    cfg["daily_report_smtp_use_tls"] = bool(use_tls)
    cfg["daily_report_send_time"] = normalize_daily_report_send_time(send_time or "19:50")
    _save_config(cfg)


def get_whatsapp_config() -> dict:
    cfg = _load_config()
    recipients = cfg.get("whatsapp_recipients") or []
    if isinstance(recipients, str):
        recipients = [r.strip() for r in re.split(r"[,\s;]+", recipients) if r.strip()]
    return {
        "enabled": bool(cfg.get("whatsapp_enabled", False)),
        "phone_number_id": str(cfg.get("whatsapp_phone_number_id") or "").strip(),
        "access_token": str(cfg.get("whatsapp_access_token") or "").strip(),
        "recipients": recipients,
    }


def set_whatsapp_config(enabled=False, phone_number_id="", access_token="", recipients=None) -> None:
    cfg = _load_config()
    cfg["whatsapp_enabled"] = bool(enabled)
    cfg["whatsapp_phone_number_id"] = str(phone_number_id or "").strip()
    if access_token is not None:
        cfg["whatsapp_access_token"] = str(access_token or "").strip()
    
    if isinstance(recipients, str):
        processed = [r.strip() for r in re.split(r"[,\s;]+", recipients) if r.strip()]
    elif isinstance(recipients, list):
        processed = [str(r).strip() for r in recipients if str(r).strip()]
    else:
        processed = []
    cfg["whatsapp_recipients"] = processed
    _save_config(cfg)


def send_whatsapp_message(message: str, recipients=None) -> tuple[bool, str]:
    config = get_whatsapp_config()
    if not config["enabled"]:
        return False, "WhatsApp sending is disabled in Settings."
        
    phone_id = config["phone_number_id"]
    token = config["access_token"]
    if not phone_id or not token:
        return False, "WhatsApp configuration is incomplete (missing Phone Number ID or Access Token)."
        
    if recipients is None:
        recipients_list = config["recipients"]
    elif isinstance(recipients, str):
        recipients_list = [r.strip() for r in re.split(r"[,\s;]+", recipients) if r.strip()]
    else:
        recipients_list = [str(r).strip() for r in recipients if str(r).strip()]
        
    if not recipients_list:
        return False, "No WhatsApp recipients configured."
        
    # Clean phone numbers (must be international format without +, e.g. 96170992077)
    cleaned_recipients = []
    for num in recipients_list:
        clean = re.sub(r"\D", "", num)
        if clean:
            cleaned_recipients.append(clean)
            
    if not cleaned_recipients:
        return False, "No valid phone numbers found in recipients."

    url = f"https://graph.facebook.com/v17.0/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    success_count = 0
    errors = []
    for phone in cleaned_recipients:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {
                "body": message
            }
        }
        try:
            import requests
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            if response.status_code == 200:
                success_count += 1
            else:
                try:
                    err_json = response.json()
                    err_msg = err_json.get("error", {}).get("message", response.text)
                except Exception:
                    err_msg = response.text
                errors.append(f"Phone {phone}: Code {response.status_code} - {err_msg}")
        except Exception as e:
            errors.append(f"Phone {phone}: {e}")
            
    if success_count > 0:
        if errors:
            return True, f"Sent to {success_count} numbers. Failed on some: {'; '.join(errors)}"
        return True, f"Successfully sent WhatsApp message to {success_count} recipient(s)."
    else:
        return False, f"Failed to send to any recipient. Errors: {'; '.join(errors)}"


def mark_daily_report_email_sent(day_str: str, source: str = "") -> None:
    cfg = _load_config()
    cfg["daily_report_last_sent_date"] = str(day_str or "").strip()
    cfg["daily_report_last_sent_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cfg["daily_report_last_sent_source"] = str(source or "").strip()
    if str(source or "").strip() == "schedule":
        cfg["daily_report_last_auto_sent_date"] = str(day_str or "").strip()
        cfg["daily_report_last_auto_sent_at"] = cfg["daily_report_last_sent_at"]
    _save_config(cfg)


def send_daily_report_email(subject: str, body: str, attachment_path, recipients=None) -> tuple[bool, str]:
    cfg = get_daily_report_email_config()
    if not cfg.get("enabled", True):
        return False, "Daily report email is disabled in Settings."

    final_recipients = _split_email_recipients(recipients or cfg.get("recipients") or [])
    if not final_recipients:
        return False, "No report recipients are configured."

    sender = str(cfg.get("sender_email") or "").strip()
    server = str(cfg.get("smtp_server") or "").strip()
    for prefix in ("smtp://", "smtps://", "http://", "https://"):
        if server.lower().startswith(prefix):
            server = server[len(prefix):]
    server = server.split("/")[0].split(":")[0]

    username = str(cfg.get("smtp_username") or sender).strip()
    password = str(cfg.get("smtp_password") or "").strip()
    try:
        port = int(cfg.get("smtp_port") or 587)
    except Exception:
        port = 587

    if not sender or not server or not username or not password:
        return False, "Email settings are incomplete. Add sender email, SMTP username, and app password in Settings."

    if isinstance(attachment_path, (list, tuple, set)):
        attachment_paths = [Path(str(p)) for p in attachment_path if str(p or "").strip()]
    elif attachment_path:
        attachment_paths = [Path(str(attachment_path))]
    else:
        attachment_paths = []
    attachment_paths = [p for p in attachment_paths if str(p).strip()]
    for path in attachment_paths:
        if not path.exists():
            return False, f"Report file not found: {path}"

    try:
        import mimetypes
        import smtplib
        from email.message import EmailMessage
    except Exception as exc:
        return False, f"Email libraries are unavailable: {exc}"

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(final_recipients)
    msg["Subject"] = str(subject or "Mask POS Daily Sales Report")
    msg.set_content(str(body or "Daily sales report attached."))

    for path in attachment_paths:
        ctype, encoding = mimetypes.guess_type(str(path))
        if ctype is None or encoding is not None:
            ctype = "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        with path.open("rb") as f:
            msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=path.name)

    try:
        if bool(cfg.get("use_tls", True)):
            with smtplib.SMTP(server, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(username, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP_SSL(server, port, timeout=30) as smtp:
                smtp.login(username, password)
                smtp.send_message(msg)
        return True, f"Report emailed to {', '.join(final_recipients)}."
    except Exception as exc:
        text = str(exc)
        if "5.7.8" in text or "Username and Password not accepted" in text:
            return False, (
                "Gmail rejected the username/password. Use a Google App Password, not the normal Gmail password. "
                "Turn on 2-Step Verification for the sender account, create an App Password, then paste that 16-character code in Settings."
            )
        return False, f"Could not send report email: {exc}"


def send_shift_open_email(shift_id):
    try:
        cfg = get_daily_report_email_config()
        if not cfg.get("enabled", True):
            return False, "Daily report email is disabled in Settings."

        import pos_logic as L
        summary = L.shift_summary(shift_id)
        if not summary:
            return False, "Shift not found."

        shift = summary.get("shift", {}) or {}
        emp_name = shift.get("employee_name") or "System"
        opened_at = shift.get("opened_at") or ""
        notes = shift.get("notes") or "No opening check difference."

        opening_cash = summary.get("opening_cash", 0.0)
        opening_usd = summary.get("opening_usd", 0.0)
        opening_lbp = summary.get("opening_lbp", 0.0)
        rate = summary.get("lbp_per_usd", 89500.0)

        subject = f"Mask POS Register Opened - {emp_name}"
        body = (
            f"Register Opened Notification\n"
            f"----------------------------\n"
            f"Employee: {emp_name}\n"
            f"Opened At: {opened_at}\n\n"
            f"Opening Count Details:\n"
            f"  - Counted USD: ${opening_usd:,.2f}\n"
            f"  - Counted LBP: {opening_lbp:,.0f} LBP\n"
            f"  - Exchange Rate: {rate:,.0f} LBP/USD\n"
            f"  - Total Opening Value: ${opening_cash:,.2f}\n\n"
            f"Opening Status / Notes:\n"
            f"  {notes}\n"
        )
        ok, msg = send_daily_report_email(subject, body, [])
        return ok, msg
    except Exception as e:
        return False, f"Failed to send register open email: {e}"


def trigger_daily_report_email_on_host(day: str, source: str = "manual", force: bool = False) -> tuple[bool, str]:
    if _MODE != "connect":
        return False, "Not in connect mode"
    try:
        res = _remote_post("/analytics/send_daily_report_email", {
            "day": day,
            "source": source,
            "force": force
        })
        if res.get("ok"):
            return True, str(res.get("message", "Report emailed successfully (triggered from host)."))
        else:
            return False, str(res.get("error") or res.get("message") or "Failed to trigger email from host.")
    except Exception as e:
        return False, f"Connection to host failed: {e}"


def _cloud_enqueue_config(payload: dict) -> None:
    if supabase_sync is None:
        return
    if _MODE not in ("host", "cloud"):
        return
    try:
        supabase_sync.enqueue_event(BASE_DIR, "update", "config", "pos_config", payload or {})
    except Exception:
        pass


def _offers_config_payload() -> dict:
    cfg = _load_config()
    return {
        "seasonal_sale_enabled": bool(cfg.get("seasonal_sale_enabled", False)),
        "seasonal_sales_map": cfg.get("seasonal_sales_map") or {},
        "bundle_offers_enabled": bool(cfg.get("bundle_offers_enabled", True)),
        "bundle_offers_map": cfg.get("bundle_offers_map") or {},
        "spin_wheel_prizes": cfg.get("spin_wheel_prizes") or [],
    }


def get_backend_config() -> dict:
    """
    Used by Settings page to read current mode/url/port.
    Always returns safe defaults if config is missing.
    """
    cfg = _load_config()
    return {
        "mode": (cfg.get("mode") or "standalone").strip(),
        "server_url": (cfg.get("server_url") or "http://127.0.0.1:8000").strip(),
        "host_port": int(cfg.get("host_port", 8000) or 8000),
    }


def set_backend_config(mode: str, server_url: str = "", host_port: int = 8000) -> None:
    """
    Used by Settings page to save selection, then the app restarts.
    """
    mode = (mode or "standalone").strip()
    server_url = (server_url or "").strip()
    host_port = int(host_port or 8000)

    cfg = _load_config()
    cfg["mode"] = mode
    cfg["server_url"] = server_url
    cfg["host_port"] = host_port
    _save_config(cfg)



# ---------------- STORE NAME ----------------

def _clean_one_line(s: str) -> str:
    # Prevent embedded newlines or literal "\n" from breaking RAW prints.
    s = (s or "")
    s = s.replace("\\n", " ")
    s = s.replace("\r", " ").replace("\n", " ")
    s = " ".join(s.split())
    return s.strip()


def _split_legacy_two_line(s: str) -> tuple[str, str]:
    """Back-compat: if someone saved 'Name\nSubtitle' into one field."""
    if not s:
        return "", ""
    raw = str(s)
    # handle literal backslash-n as well as real newlines
    if "\\n" in raw:
        parts = [p.strip() for p in raw.split("\\n")]
    else:
        parts = [p.strip() for p in raw.replace("\r", "\n").split("\n")]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:]).strip()


def get_store_name() -> str:
    """Return store name (line 1). Defaults to 'Keep'."""
    cfg = _load_config()
    name = str(cfg.get("store_name") or "").strip()
    if not name:
        return "Keep"
    # legacy: store_name may contain 2 lines
    n1, _ = _split_legacy_two_line(name)
    return _clean_one_line(n1) or "Keep"


def set_store_name(name: str) -> None:
    """Save store name (line 1). Newlines are stripped."""
    cfg = _load_config()
    cfg["store_name"] = _clean_one_line(str(name or "Keep")) or "Keep"
    _save_config(cfg)


def get_store_subtitle() -> str:
    """Return store subtitle (line 2). Defaults to 'Sports Wear'."""
    cfg = _load_config()
    sub = str(cfg.get("store_subtitle") or "").strip()
    if sub:
        return _clean_one_line(sub) or "Sports Wear"

    # legacy: try to parse from store_name if it contains two lines
    name = str(cfg.get("store_name") or "").strip()
    _, n2 = _split_legacy_two_line(name)
    return _clean_one_line(n2) or "Sports Wear"


def set_store_subtitle(text: str) -> None:
    """Save store subtitle (line 2). Newlines are stripped."""
    cfg = _load_config()
    cfg["store_subtitle"] = _clean_one_line(str(text or "Sports Wear")) or "Sports Wear"
    _save_config(cfg)


# ---------------- CURRENCY / CASH DRAWER ----------------

def get_lbp_per_usd() -> float:
    """Return configured LBP per 1 USD for drawer counts."""
    cfg = _load_config()
    try:
        rate = float(cfg.get("lbp_per_usd") or 89500)
    except Exception:
        rate = 89500.0
    if rate <= 0:
        rate = 89500.0
    return float(rate)


def set_lbp_per_usd(rate: float) -> None:
    """Save configured LBP per 1 USD for drawer counts."""
    try:
        r = float(rate or 0.0)
    except Exception:
        r = 0.0
    if r <= 0:
        return
    cfg = _load_config()
    cfg["lbp_per_usd"] = float(r)
    _save_config(cfg)


# ---------------- Seasonal Sale ----------------

def get_seasonal_sale_enabled() -> bool:
    """Return whether Seasonal Sale is enabled (global ON/OFF). Stored in pos_config.json."""
    if _MODE == "connect":
        try:
            return bool(_remote_get("/config/offers").get("seasonal_sale_enabled", False))
        except Exception:
            return False
    cfg = _load_config()
    return bool(cfg.get("seasonal_sale_enabled", False))


def set_seasonal_sale_enabled(enabled: bool) -> None:
    """Enable/disable Seasonal Sale globally."""
    if _MODE == "connect":
        _remote_post("/config/offers", {"seasonal_sale_enabled": bool(enabled)})
        return
    cfg = _load_config()
    cfg["seasonal_sale_enabled"] = bool(enabled)
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


def get_seasonal_sales_map() -> dict:
    """Return normalized seasonal offers keyed by barcode.

    Legacy numeric values remain supported as percentage discounts. New values
    may be {"type": "price", "price": 15.0} for a fixed unit sale price.
    """
    if _MODE == "connect":
        try:
            raw = _remote_get("/config/offers").get("seasonal_sales_map") or {}
        except Exception:
            raw = {}
    else:
        cfg = _load_config()
        raw = cfg.get("seasonal_sales_map") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = {}
    for k, v in raw.items():
        bc = str(k or "").strip()
        if not bc:
            continue
        if isinstance(v, dict):
            kind = str(v.get("type") or "").strip().lower()
            if kind == "price" or "price" in v or "sale_price" in v:
                try:
                    price = float(v.get("price", v.get("sale_price", 0.0)) or 0.0)
                except Exception:
                    price = 0.0
                if price > 0.0:
                    out[bc] = {"type": "price", "price": round(price, 2)}
                continue
            try:
                pct = float(v.get("pct", v.get("percent", v.get("value", 0.0))) or 0.0)
            except Exception:
                pct = 0.0
        else:
            try:
                pct = float(v)
            except Exception:
                continue
        pct = max(0.0, min(100.0, pct))
        if pct > 0.0:
            out[bc] = {"type": "percent", "pct": round(pct, 2)}
    return out


def set_seasonal_sale_item(barcode: str, sale_pct: float) -> None:
    """Add/update a barcode in the sale map. If sale_pct<=0, removes it."""
    bc = str(barcode or "").strip()
    if not bc:
        return
    try:
        pct = float(sale_pct)
    except Exception:
        pct = 0.0
    pct = max(0.0, min(100.0, pct))

    if _MODE == "connect":
        m = get_seasonal_sales_map()
        if pct <= 0.0:
            m.pop(bc, None)
        else:
            m[bc] = {"type": "percent", "pct": round(pct, 2)}
        _remote_post("/config/offers", {"seasonal_sales_map": m})
        return

    cfg = _load_config()
    m = cfg.get("seasonal_sales_map") or {}
    if not isinstance(m, dict):
        m = {}

    if pct <= 0.0:
        try:
            m.pop(bc, None)
        except Exception:
            pass
    else:
        m[bc] = {"type": "percent", "pct": round(pct, 2)}

    cfg["seasonal_sales_map"] = m
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


def set_seasonal_sale_price_item(barcode: str, sale_price: float) -> None:
    """Add/update a barcode with a fixed unit sale price."""
    bc = str(barcode or "").strip()
    if not bc:
        return
    try:
        price = round(max(0.0, float(sale_price)), 2)
    except Exception:
        price = 0.0

    if _MODE == "connect":
        m = get_seasonal_sales_map()
        if price <= 0.0:
            m.pop(bc, None)
        else:
            m[bc] = {"type": "price", "price": price}
        _remote_post("/config/offers", {"seasonal_sales_map": m})
        return

    cfg = _load_config()
    m = cfg.get("seasonal_sales_map") or {}
    if not isinstance(m, dict):
        m = {}
    if price <= 0.0:
        m.pop(bc, None)
    else:
        m[bc] = {"type": "price", "price": price}
    cfg["seasonal_sales_map"] = m
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


def remove_seasonal_sale_item(barcode: str) -> None:
    """Remove a barcode from the sale map."""
    set_seasonal_sale_item(barcode, 0.0)


def clear_seasonal_sales() -> None:
    """Clear ALL seasonal sale items."""
    if _MODE == "connect":
        _remote_post("/config/offers", {"seasonal_sales_map": {}})
        return
    cfg = _load_config()
    cfg["seasonal_sales_map"] = {}
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


# ---------------- Bundle Offers ----------------

def get_bundle_offers_enabled() -> bool:
    """Return whether quantity/bundle offers are enabled globally."""
    if _MODE == "connect":
        try:
            return bool(_remote_get("/config/offers").get("bundle_offers_enabled", True))
        except Exception:
            return True
    cfg = _load_config()
    return bool(cfg.get("bundle_offers_enabled", True))


def set_bundle_offers_enabled(enabled: bool) -> None:
    """Enable/disable quantity/bundle offers globally."""
    if _MODE == "connect":
        _remote_post("/config/offers", {"bundle_offers_enabled": bool(enabled)})
        return
    cfg = _load_config()
    cfg["bundle_offers_enabled"] = bool(enabled)
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


def get_bundle_offers_map() -> dict:
    """Return {barcode: {"qty": int, "price": float}} for active bundle offers."""
    if _MODE == "connect":
        try:
            raw = _remote_get("/config/offers").get("bundle_offers_map") or {}
        except Exception:
            raw = {}
    else:
        cfg = _load_config()
        raw = cfg.get("bundle_offers_map") or {}
    if not isinstance(raw, dict):
        raw = {}

    out = {}
    for k, v in raw.items():
        bc = str(k or "").strip()
        if not bc:
            continue
        if not isinstance(v, dict):
            continue
        try:
            qty = int(v.get("qty") or 0)
            price = float(v.get("price") or 0.0)
        except Exception:
            continue
        qty = max(2, qty)
        price = round(max(0.0, price), 2)
        if price <= 0.0:
            continue
        out[bc] = {"qty": qty, "price": price}
    return out


def set_bundle_offer_item(barcode: str, qty: int, bundle_price: float) -> None:
    """Add/update a quantity offer. If qty<2 or price<=0, removes it."""
    bc = str(barcode or "").strip()
    if not bc:
        return
    try:
        q = int(qty or 0)
    except Exception:
        q = 0
    try:
        price = float(bundle_price or 0.0)
    except Exception:
        price = 0.0

    if _MODE == "connect":
        m = get_bundle_offers_map()
        if q < 2 or price <= 0:
            m.pop(bc, None)
        else:
            m[bc] = {"qty": int(q), "price": round(max(0.0, price), 2)}
        _remote_post("/config/offers", {"bundle_offers_map": m})
        return

    cfg = _load_config()
    m = cfg.get("bundle_offers_map") or {}
    if not isinstance(m, dict):
        m = {}

    if q < 2 or price <= 0:
        try:
            m.pop(bc, None)
        except Exception:
            pass
    else:
        m[bc] = {"qty": int(q), "price": round(max(0.0, price), 2)}

    cfg["bundle_offers_map"] = m
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


def remove_bundle_offer_item(barcode: str) -> None:
    """Remove one quantity offer."""
    set_bundle_offer_item(barcode, 0, 0.0)


def clear_bundle_offers() -> None:
    """Clear ALL quantity offers."""
    if _MODE == "connect":
        _remote_post("/config/offers", {"bundle_offers_map": {}})
        return
    cfg = _load_config()
    cfg["bundle_offers_map"] = {}
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())


# ---------------- Spin Wheel ----------------

def _clean_spin_wheel_prizes(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "none").strip().lower()
        if kind not in ("discount", "free_item", "none"):
            continue
        try:
            weight = float(item.get("weight") or 0.0)
        except Exception:
            weight = 0.0
        if weight < 0:
            continue
        prize = {
            "label": _clean_one_line(str(item.get("label") or "Prize"))[:40] or "Prize",
            "type": kind,
            "weight": round(weight, 2),
            "enabled": bool(item.get("enabled", True)),
        }
        if kind == "discount":
            try:
                value = float(item.get("value") or 0.0)
            except Exception:
                value = 0.0
            value = max(0.0, min(100.0, value))
            if value <= 0:
                continue
            prize["value"] = round(value, 2)
        elif kind == "free_item":
            barcode = str(item.get("barcode") or "").strip()
            if not barcode:
                continue
            prize["barcode"] = barcode
        out.append(prize)
    return out


def get_spin_wheel_prizes() -> list[dict]:
    if _MODE == "connect":
        try:
            raw = _remote_get("/config/offers").get("spin_wheel_prizes") or []
        except Exception:
            raw = []
    else:
        raw = _load_config().get("spin_wheel_prizes") or []
    return _clean_spin_wheel_prizes(raw)


def set_spin_wheel_prizes(prizes) -> None:
    clean = _clean_spin_wheel_prizes(prizes)
    if _MODE == "connect":
        _remote_post("/config/offers", {"spin_wheel_prizes": clean})
        return
    cfg = _load_config()
    cfg["spin_wheel_prizes"] = clean
    _save_config(cfg)
    _cloud_enqueue_config(_offers_config_payload())



# ---------------- HTTP ----------------

def _http():
    """
    Tiny HTTP helper. Prefers requests; falls back to urllib.
    Returns an object with .get/.post raising Exception on non-2xx.
    """
    try:
        import requests  # type: ignore

        class R:
            @staticmethod
            def get(url, params=None, timeout=10):
                r = requests.get(url, params=params, timeout=timeout)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
                return r.json()

            @staticmethod
            def post(url, json=None, timeout=20):
                r = requests.post(url, json=json, timeout=timeout)
                if r.status_code >= 400:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text}")
                return r.json()

        return R
    except Exception:
        import urllib.request, urllib.parse
        import json as _json

        class U:
            @staticmethod
            def get(url, params=None, timeout=10):
                if params:
                    url = url + "?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(url, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return _json.loads(body or "{}")

            @staticmethod
            def post(url, json=None, timeout=20):
                data = _json.dumps(json or {}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return _json.loads(body or "{}")

        return U


# ---------------- SERVER START ----------------

# --- Server port helpers (avoid EXE recursion + port-in-use crashes) ---

_HOST_PORT_USED = None  # actual port used by host server (may differ if 8000 busy)

def _port_is_free(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", int(port)))
            return True
        finally:
            try:
                s.close()
            except Exception:
                pass
    except Exception:
        return False

def _server_alive_local(port: int) -> bool:
    """Return True only if a MaskPOS server is alive on this port AND it is using our BASE_DIR.

    This prevents reusing an old server process from another folder (which would point at a different pos.db).
    """
    try:
        http = _http()
        base = f"http://127.0.0.1:{int(port)}"
        j = http.get(base + "/health", timeout=0.8)
        if not bool(j.get("ok")):
            return False
        # Verify the server instance belongs to THIS installation (same BASE_DIR)
        info = http.get(base + "/debug/base_dir", timeout=0.8)
        srv_base = (info.get("base_dir") or "").strip()
        if not srv_base:
            return False
        return Path(srv_base).resolve() == BASE_DIR.resolve()
    except Exception:
        return False

def _pick_free_port(start_port: int, max_tries: int = 50) -> int:
    p0 = int(start_port or 8000)
    for k in range(max_tries):
        p = p0 + k
        if _port_is_free(p):
            return p
        # If port isn't free but server already alive there, reuse it
        if _server_alive_local(p):
            return p
    return p0  # last resort




def _start_server(port: int) -> None:
    """Start FastAPI server on this machine (host mode), safely.

    Fixes:
    - In EXE, never subprocess sys.executable (would relaunch the POS).
    - If port (default 8000) is already in use:
        * If our server is already running there -> reuse it.
        * Else pick the next free port (8001, 8002, ...) and update config.
    """
    global _SERVER_PROC, _SERVER_THREAD, _HOST_PORT_USED

    desired_port = int(port or 8000)

    # If server already alive on desired port, reuse (no restart)
    if _server_alive_local(desired_port):
        _HOST_PORT_USED = desired_port
        _SERVER_PROC = True
        return

    # If we previously marked started, confirm it is still alive; otherwise allow restart
    if _SERVER_PROC is not None and _HOST_PORT_USED is not None:
        if _server_alive_local(int(_HOST_PORT_USED)):
            return
        # stale marker
        _SERVER_PROC = None
        _SERVER_THREAD = None
        _HOST_PORT_USED = None

    # Choose a usable port (handles "only one usage of each socket address")
    chosen_port = _pick_free_port(desired_port, max_tries=50)
    _HOST_PORT_USED = chosen_port

    # If chosen_port differs, persist it so next run uses the working port
    if chosen_port != desired_port:
        try:
            cfg = _load_config()
            cfg["host_port"] = int(chosen_port)
            _save_config(cfg)
        except Exception:
            pass

    # Keep the host server pinned to the same persistent folder as the POS app.
    try:
        os.environ["MASKPOS_DATA_DIR"] = str(BASE_DIR)
        os.environ["MASKPOS_DB_PATH"] = str(BASE_DIR / "pos.db")
    except Exception:
        pass

    # ---------------- Frozen EXE path: run in-process ----------------
    if getattr(sys, "frozen", False):
        try:
            def run():
                try:
                    import importlib
                    import uvicorn

                    try:
                        server_mod = importlib.import_module("server")
                    except Exception:
                        server_mod = importlib.import_module("server_new")

                    _host_log(f"Starting embedded host server on port {int(chosen_port)} using {BASE_DIR / 'pos.db'}")
                    uvicorn.run(
                        server_mod.app,
                        host="0.0.0.0",
                        port=int(chosen_port),
                        loop="asyncio",
                        http="h11",
                        lifespan="off",
                        log_level="warning",
                        access_log=False,
                        log_config=None,
                    )
                except Exception as e:
                    _host_log(f"Embedded host server failed: {type(e).__name__}: {e}")
                    return

            t = threading.Thread(target=run, daemon=True)
            _SERVER_THREAD = t
            t.start()
            _SERVER_PROC = True
            for _ in range(8):
                if _server_alive_local(int(chosen_port)):
                    return
                time.sleep(0.25)
            _host_log(f"Embedded host server is still starting on port {int(chosen_port)}; continuing app startup.")
            return
        except Exception as e:
            _host_log(f"Could not launch embedded host server: {type(e).__name__}: {e}")
            pass

    # ---------------- Normal Python path: spawn python server.py ----------------
    py = sys.executable
    server_py_path = Path(__file__).with_name("server.py")
    if not server_py_path.exists():
        server_py_path = Path(__file__).with_name("server_new.py")

    args = [py, str(server_py_path), "--host", "0.0.0.0", "--port", str(chosen_port)]

    creationflags = 0
    try:
        if os.name == "nt":
            creationflags = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    except Exception:
        creationflags = 0

    env = os.environ.copy()
    env["MASKPOS_DATA_DIR"] = str(BASE_DIR)
    env["MASKPOS_DB_PATH"] = str(BASE_DIR / "pos.db")

    try:
        log_path = BASE_DIR / "server.log"
        log_file = open(log_path, "a", encoding="utf-8")
    except Exception:
        log_file = subprocess.DEVNULL

    popen_kwargs = _no_window_kwargs()
    if creationflags:
        popen_kwargs["creationflags"] = int(popen_kwargs.get("creationflags", 0)) | int(creationflags)

    _SERVER_PROC = subprocess.Popen(
        args,
        cwd=str(BASE_DIR),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        **popen_kwargs,
    )

    # Give the server a short moment to bind before the app starts issuing API calls.
    for _ in range(30):
        if _server_alive_local(int(chosen_port)):
            return
        time.sleep(0.2)


# ---------------- OPTIONAL STARTUP POPUP ----------------

def _tk_choose_mode(app_title: str) -> Tuple[str, str, int]:
    """
    Returns (mode, host_url, port).
    mode: standalone | host | connect | cloud
    """
    import tkinter as tk
    from tkinter import ttk, messagebox, simpledialog

    cfg = _load_config()
    default_mode = cfg.get("mode", "standalone")
    if default_mode == "cloud" and not supabase_emergency_enabled():
        default_mode = "host"
    default_url = cfg.get("server_url", "http://127.0.0.1:8000")
    default_port = int(cfg.get("host_port", 8000) or 8000)

    root = tk.Tk()
    root.title(f"{app_title} - Connection")
    root.geometry("620x500")
    root.minsize(620, 500)
    root.resizable(True, True)

    try:
        style = ttk.Style()
        if os.name == "nt":
            try:
                style.theme_use("vista")
            except Exception:
                pass
    except Exception:
        pass

    mode_var = tk.StringVar(value=default_mode)
    url_var = tk.StringVar(value=default_url)
    port_var = tk.StringVar(value=str(default_port))

    state = {"done": False, "mode": "standalone", "url": "", "port": default_port}

    outer = ttk.Frame(root, padding=14)
    outer.pack(fill="both", expand=True)

    ttk.Label(
        outer,
        text="Choose how this PC should run:",
        font=("Segoe UI", 11, "bold"),
    ).pack(anchor="w", pady=(0, 10))

    rb_frame = ttk.Frame(outer)
    rb_frame.pack(fill="x", pady=(0, 10))
    ttk.Radiobutton(
        rb_frame,
        text="Standalone (local database on this PC)",
        value="standalone",
        variable=mode_var,
    ).pack(anchor="w", pady=2)
    ttk.Radiobutton(
        rb_frame,
        text="Host (main database; share on LAN and mirror to cloud)",
        value="host",
        variable=mode_var,
    ).pack(anchor="w", pady=2)
    ttk.Radiobutton(
        rb_frame,
        text="Join (use the Host main database on Wi-Fi/LAN)",
        value="connect",
        variable=mode_var,
    ).pack(anchor="w", pady=2)
    if supabase_emergency_enabled():
        ttk.Radiobutton(
            rb_frame,
            text="Cloud Cache fallback (use only if Host is unavailable)",
            value="cloud",
            variable=mode_var,
        ).pack(anchor="w", pady=2)

    host_box = ttk.LabelFrame(outer, text="Host settings", padding=10)
    host_box.pack(fill="x", pady=(0, 10))
    ttk.Label(host_box, text="Port:").grid(row=0, column=0, sticky="w")
    ttk.Entry(host_box, textvariable=port_var, width=10).grid(
        row=0, column=1, sticky="w", padx=(8, 0)
    )
    ttk.Label(host_box, text="Tip: 8000 is the default").grid(
        row=1, column=0, columnspan=2, sticky="w", pady=(6, 0)
    )

    conn_box = ttk.LabelFrame(outer, text="Connect settings", padding=10)
    conn_box.pack(fill="x")
    ttk.Label(conn_box, text="Host URL (example: http://192.168.1.10:8000)").pack(
        anchor="w"
    )
    ttk.Entry(conn_box, textvariable=url_var).pack(fill="x", pady=(6, 0))

    btn_bar = ttk.Frame(outer)
    btn_bar.pack(fill="x", pady=(14, 0))

    def finish_ok():
        mode = (mode_var.get() or "standalone").strip()

        if mode == "host":
            pwd = simpledialog.askstring(
                "Protected mode",
                "Enter password to use Host mode:",
                show="*",
                parent=root,
            )
            if not verify_mode_admin_password(pwd):
                messagebox.showerror("Protected mode", "Wrong password.")
                return

        if mode == "host":
            try:
                p = int((port_var.get() or "").strip())
                if p < 1 or p > 65535:
                    raise ValueError()
            except Exception:
                messagebox.showerror("Port", "Please enter a valid port (1-65535).")
                return
            state.update({"done": True, "mode": "host", "url": "", "port": p})
            root.destroy()
            return

        if mode == "connect":
            url = (url_var.get() or "").strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                messagebox.showerror("Host URL", "Host URL must start with http:// or https://")
                return
            state.update({"done": True, "mode": "connect", "url": url, "port": default_port})
            root.destroy()
            return

        if mode == "cloud":
            state.update({"done": True, "mode": "cloud", "url": "", "port": default_port})
            root.destroy()
            return

        state.update({"done": True, "mode": "standalone", "url": "", "port": default_port})
        root.destroy()

    def cancel():
        state.update({"done": False})
        root.destroy()

    ttk.Button(btn_bar, text="Cancel", command=cancel).pack(side="right")
    ttk.Button(btn_bar, text="OK", command=finish_ok).pack(side="right", padx=(0, 10))

    root.bind("<Return>", lambda e: finish_ok())
    root.bind("<Escape>", lambda e: cancel())
    root.protocol("WM_DELETE_WINDOW", cancel)

    try:
        root.lift()
        root.attributes("-topmost", True)
        root.after(50, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    root.mainloop()

    if not state["done"]:
        return "__cancel__", "", default_port

    return state["mode"], state["url"], int(state["port"] or default_port)


# ---------------- INIT (THIS IS WHAT YOU USE) ----------------

def backend_init(app_title: str = "Mask POS", interactive: bool = False) -> None:
    """
    Call once at startup.

    interactive=False:
      - reads pos_config.json and starts host/connect automatically
      - if no config exists, falls back to standalone

    interactive=True:
      - shows the chooser window, then saves config
    """
    global _MODE, BASE_URL

    cfg = _load_config()

    if interactive:
        mode, url, port = _tk_choose_mode(app_title)
        if mode == "__cancel__":
            raise SystemExit(0)
        cfg["mode"] = mode
        cfg["server_url"] = url
        cfg["host_port"] = port
        _save_config(cfg)
    else:
        mode = (cfg.get("mode") or "standalone").strip()
        url = (cfg.get("server_url") or "").strip()
        port = int(cfg.get("host_port", 8000) or 8000)

        if mode not in ("standalone", "host", "connect", "cloud"):
            mode = "standalone"
        if mode == "cloud" and not supabase_emergency_enabled():
            mode = "host"

    _MODE = mode

    if mode == "standalone":
        BASE_URL = ""
        _set_connected(True)
        _start_periodic_backup_if_local()
        return

    if mode == "cloud":
        BASE_URL = ""
        _set_connected(True)
        _start_cloud_sync_if_local()
        _start_periodic_backup_if_local()
        return

    if mode == "host":
        _start_server(port)
        actual_port = int(_HOST_PORT_USED or port)
        BASE_URL = f"http://127.0.0.1:{actual_port}"
        _set_connected(True)
        _start_cloud_sync_if_local()
        _start_periodic_backup_if_local()
        try:
            _start_discovery_broadcast(app_title, int(_HOST_PORT_USED or port))
        except Exception:
            pass
        return

    # connect
    BASE_URL = url.rstrip("/")
    ok = False
    try:
        http = _http()
        j = http.get(BASE_URL + "/health", timeout=2)
        ok = bool(j.get("ok"))
    except Exception:
        ok = False

    if not ok:
        # Stay in CONNECT mode; do not create/switch to a local DB.
        _set_connected(False)
        _start_heartbeat()
        return

    _set_connected(True)
    _start_heartbeat()


# ---------------- Local/Remote dispatch helpers ----------------

def _local():
    import pos_logic as L
    return L


def _use_local_db() -> bool:
    """Standalone, HOST, and CLOUD live on this PC's DB; JOIN uses the remote host."""
    return _MODE != "connect"


def _host_log(message: str) -> None:
    try:
        with open(BASE_DIR / "server.log", "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except Exception:
        pass


def _cloud_enqueue(event_type: str, entity_type: str, entity_id=None, payload=None) -> None:
    if supabase_sync is None:
        return
    if _MODE not in ("host", "cloud"):
        return
    try:
        supabase_sync.enqueue_event(BASE_DIR, event_type, entity_type, entity_id, payload or {})
    except Exception:
        pass


def _start_cloud_backfill_once() -> None:
    global _CLOUD_BACKFILL_STARTED
    if _CLOUD_BACKFILL_STARTED:
        return
    if supabase_sync is None or _MODE not in ("host", "cloud"):
        return
    try:
        if not supabase_sync.cloud_configured():
            return
    except Exception:
        return
    _CLOUD_BACKFILL_STARTED = True

    def _runner(mode_snapshot: str) -> None:
        try:
            time.sleep(3)
            queued = int(supabase_sync.backfill_unsynced_events(BASE_DIR) or 0)
            if queued:
                _host_log(f"Cloud backfill queued {queued} orphaned local events in {mode_snapshot} mode")
        except Exception as e:
            _host_log(f"Cloud backfill failed: {e}")

    threading.Thread(target=_runner, args=(_MODE,), daemon=True).start()


def _start_cloud_sync_if_local() -> None:
    if supabase_sync is None or _MODE not in ("host", "cloud"):
        return
    try:
        supabase_sync.start_background_sync(
            BASE_DIR,
            interval_seconds=5,
            protect_existing_pending=(_MODE == "cloud"),
            host_print_worker=(_MODE == "host"),
            authoritative_host=(_MODE == "host"),
        )
        _start_cloud_backfill_once()
    except Exception:
        pass


def _prepare_shared_local_from_cloud() -> None:
    """Legacy hook retained for older callers. Host SQLite is authoritative."""
    if supabase_sync is None or _MODE != "cloud":
        return
    try:
        supabase_sync.prepare_local_from_cloud(BASE_DIR, protect_existing_pending=True)
    except Exception:
        pass


def cloud_sync_status(probe: bool = False) -> dict:
    if supabase_sync is None:
        return {"enabled": False, "online": False, "pending": 0, "applied": 0, "last_error": "supabase_sync module not loaded"}
    try:
        return supabase_sync.status(BASE_DIR, probe=bool(probe))
    except Exception as e:
        return {"enabled": False, "online": False, "pending": 0, "applied": 0, "last_error": str(e)}


def cloud_sync_now() -> dict:
    if supabase_sync is None:
        return {"enabled": False, "online": False, "pending": 0, "downloaded": 0, "uploaded": 0, "last_error": "supabase_sync module not loaded"}
    try:
        return supabase_sync.sync_now(BASE_DIR)
    except Exception as e:
        return {"enabled": False, "online": False, "pending": 0, "downloaded": 0, "uploaded": 0, "last_error": str(e)}


def send_barcode_labels_to_host(labels: list[dict], title: str = "Mask POS Labels") -> tuple[bool, str]:
    """Queue barcode labels in the hosted cloud so the Host PC prints them locally."""
    if supabase_sync is None:
        return False, "Cloud sync module is not loaded."
    try:
        clean = []
        for line in labels or []:
            if not isinstance(line, dict):
                continue
            barcode = str(line.get("barcode") or "").strip()
            if not barcode:
                continue
            try:
                qty = int(float(line.get("qty") or 1))
            except Exception:
                qty = 1
            clean.append({
                "name": str(line.get("name") or "").strip(),
                "price": float(line.get("price") or 0),
                "barcode": barcode,
                "qty": max(1, qty),
            })
        if not clean:
            return False, "No valid labels to print."
        return supabase_sync.enqueue_print_job(BASE_DIR, "barcode_labels", {
            "title": str(title or "Mask POS Labels"),
            "labels": clean,
        })
    except Exception as e:
        return False, str(e)


def _remote_get(path: str, params: dict | None = None) -> dict:
    http = _http()
    try:
        return http.get(BASE_URL + path, params=params or {}, timeout=5)
    except Exception as e:
        # Never crash UI if server is unreachable
        return {"ok": False, "error": str(e)}


def _remote_post(path: str, payload: dict) -> dict:
    http = _http()
    try:
        return http.post(BASE_URL + path, json=payload, timeout=8)
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------- Barcode preservation helpers ----------------

def _ean13_check_digit(num12: str) -> str:
    """Return the EAN-13 check digit for exactly 12 numeric digits."""
    digits = [int(c) for c in str(num12)]
    odd_sum = sum(digits[0::2])
    even_sum = sum(digits[1::2])
    total = odd_sum + 3 * even_sum
    return str((10 - (total % 10)) % 10)


def _make_valid_generated_ean13(barcode: str) -> str:
    """Normalize a system-generated barcode into a VALID EAN-13 value.

    Existing user/scanned barcodes are left alone elsewhere. This helper is only
    for auto-generated product codes so the printed label matches the stored code.

    Rules:
    - exactly 13 digits in storage
    - never starts with 0
    - final digit is always the correct EAN-13 check digit
    """
    bc = re.sub(r"\D", "", str(barcode or ""))
    if not bc:
        return ""
    if len(bc) > 13:
        return ""

    if len(bc) >= 12:
        base12 = bc[:12]
    else:
        base12 = bc.zfill(12)

    if base12.startswith("0"):
        base12 = "1" + base12[1:]

    return base12 + _ean13_check_digit(base12)


def _clean_user_barcode(barcode: str) -> str:
    """Return an exact user/scanner barcode (digits only), without rewriting it.

    Rules:
    - keep the exact digits the cashier/user provided
    - allow 1..13 digits
    - reject more than 13 digits
    """
    bc = re.sub(r"\D", "", str(barcode or ""))
    if not bc:
        return ""
    if len(bc) > 13:
        return ""
    return bc


def _normalize_storage_barcode(barcode: str, allow_generated: bool = False) -> str:
    """Normalize a barcode to the POS storage rules.

    Storage rules used here:
    - scanned 13-digit codes stay exact
    - scanned 12-digit codes become 0 + code
    - shorter codes are left-padded with zeros to 13
    - more than 13 digits are rejected

    For system-generated codes (allow_generated=True), we also avoid leading zero.
    """
    bc = re.sub(r"\D", "", str(barcode or ""))
    if not bc:
        return ""
    if len(bc) > 13:
        return ""
    if len(bc) == 13:
        out = bc
    elif len(bc) == 12:
        out = "0" + bc
    else:
        out = bc.zfill(13)

    if allow_generated and out.startswith("0"):
        out = "1" + out[1:]
    return out


def _generate_nonzero_barcode_local(product_id: int) -> str:
    """Generate a VALID EAN-13 barcode for locally created products."""
    try:
        import sqlite3
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return ""
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            seed = max(int(product_id or 0), 1)
            for i in range(5000):
                base12 = f"1{(seed + i):011d}"[-12:]
                candidate = base12 + _ean13_check_digit(base12)
                cur.execute("SELECT 1 FROM products WHERE barcode = ? AND id <> ? LIMIT 1", (candidate, int(product_id)))
                if not cur.fetchone():
                    return candidate
            return ""
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return ""


def _try_set_product_barcode_local(product_id: int, barcode: str, *, allow_generated: bool = False) -> bool:
    """Best effort local barcode override for quick/new items.

    This is used when the higher-level add_product flow auto-generates a barcode but
    the cashier scanned an unknown barcode and wants to keep that exact code.
    """
    try:
        import sqlite3
        bc = _normalize_storage_barcode(barcode, allow_generated=True) if allow_generated else _clean_user_barcode(barcode)
        if not bc or not product_id:
            return False
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return False
        conn = sqlite3.connect(str(db_path))
        try:
            cur = conn.cursor()
            # Avoid overwriting another product's barcode
            cur.execute("SELECT id FROM products WHERE barcode = ? AND id <> ? LIMIT 1", (bc, int(product_id)))
            if cur.fetchone():
                return False
            cur.execute("UPDATE products SET barcode = ? WHERE id = ?", (bc, int(product_id)))
            conn.commit()
            return bool(cur.rowcount)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception:
        return False


def _fix_generated_product_barcode_local(product_id: int, current_barcode: str) -> str:
    """Force locally generated product barcodes to be VALID EAN-13 values."""
    normalized = _make_valid_generated_ean13(current_barcode)
    if not normalized:
        normalized = _generate_nonzero_barcode_local(product_id)
    if normalized and _try_set_product_barcode_local(product_id, normalized, allow_generated=True):
        return normalized
    return _make_valid_generated_ean13(current_barcode) or current_barcode or ""


def _migrate_existing_barcodes_local() -> dict:
    """Barcode migration is intentionally disabled.

    Existing barcodes must stay exactly as stored in the restored backup.
    Only newly generated barcodes are forced to avoid a leading zero.
    """
    return {"checked": 0, "updated": 0, "skipped": 0}


def _product_snapshot_local(product_id=None, barcode: str | None = None) -> dict:
    try:
        import sqlite3
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            if barcode:
                cur.execute("SELECT * FROM products WHERE barcode = ? LIMIT 1", (str(barcode),))
            else:
                cur.execute("SELECT * FROM products WHERE id = ? LIMIT 1", (int(product_id),))
            row = cur.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()
    except Exception:
        return {}


def _employee_snapshot_local(employee_id=None, name: str | None = None) -> dict:
    try:
        import sqlite3
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            if employee_id is not None:
                cur.execute("SELECT * FROM employees WHERE id = ? LIMIT 1", (int(employee_id),))
            else:
                cur.execute("SELECT * FROM employees WHERE name = ? LIMIT 1", (str(name or "").strip(),))
            row = cur.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()
    except Exception:
        return {}


def _cart_lines_with_product_barcodes_local(cart_lines) -> list[dict]:
    """Add stable barcode identity to sale events so hosted stock stays accurate."""
    out = []
    for raw in cart_lines or []:
        line = dict(raw) if isinstance(raw, dict) else {}
        if line.get("product_id") and not line.get("barcode"):
            snapshot = _product_snapshot_local(line.get("product_id"))
            if snapshot.get("barcode"):
                line["barcode"] = str(snapshot["barcode"])
        out.append(line)
    return out


def _shift_snapshot_local(shift_id=None) -> dict:
    try:
        import sqlite3
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM cash_shifts WHERE id = ? LIMIT 1", (int(shift_id),))
            row = cur.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()
    except Exception:
        return {}


def _cash_movement_snapshot_local(movement_id=None) -> dict:
    try:
        import sqlite3
        db_path = BASE_DIR / "pos.db"
        if not db_path.exists():
            return {}
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM cash_movements WHERE id = ? LIMIT 1", (int(movement_id),))
            row = cur.fetchone()
            return dict(row) if row is not None else {}
        finally:
            conn.close()
    except Exception:
        return {}

# ---------------- API compatible functions (match pos_logic) ----------------

def add_product(name, category="", brand="", sell_price=0.0, stock_qty=0, low_stock_level=0, barcode=None, location="", cost_price=0.0, supplier=""):
    requested_barcode = _clean_user_barcode(barcode)

    if _use_local_db():
        out = _local().add_product(name, category, brand, sell_price, stock_qty, low_stock_level, requested_barcode or None, location, cost_price, supplier)
        snapshot = {}
        try:
            if isinstance(out, int):
                pid = int(out)
                if requested_barcode:
                    if _try_set_product_barcode_local(pid, requested_barcode):
                        snapshot = _product_snapshot_local(pid)
                        _cloud_enqueue("create", "product", snapshot.get("barcode") or requested_barcode or pid, snapshot or {
                            "barcode": requested_barcode, "name": name, "category": category, "brand": brand,
                            "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
                        })
                        return requested_barcode
                else:
                    fixed = _fix_generated_product_barcode_local(pid, "")
                    if fixed:
                        snapshot = _product_snapshot_local(pid)
                        _cloud_enqueue("create", "product", snapshot.get("barcode") or fixed or pid, snapshot or {
                            "barcode": fixed, "name": name, "category": category, "brand": brand,
                            "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
                        })
                        return fixed
                snapshot = _product_snapshot_local(pid)
                _cloud_enqueue("create", "product", snapshot.get("barcode") or requested_barcode or out, snapshot or {
                    "barcode": requested_barcode or out, "name": name, "category": category, "brand": brand,
                    "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
                })
                return out

            prod = _local().find_product_by_barcode(str(out)) if out else None
            pid = None
            current_barcode = str(out or "")
            try:
                pid = int(prod["id"]) if prod else None
                current_barcode = str((prod["barcode"] if prod else out) or current_barcode)
            except Exception:
                pid = None
            if pid:
                if requested_barcode:
                    if _try_set_product_barcode_local(pid, requested_barcode):
                        snapshot = _product_snapshot_local(pid)
                        _cloud_enqueue("create", "product", snapshot.get("barcode") or requested_barcode or out, snapshot or {
                            "barcode": requested_barcode, "name": name, "category": category, "brand": brand,
                            "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
                        })
                        return requested_barcode
                else:
                    fixed = _fix_generated_product_barcode_local(pid, current_barcode)
                    if fixed:
                        snapshot = _product_snapshot_local(pid)
                        _cloud_enqueue("create", "product", snapshot.get("barcode") or fixed or out, snapshot or {
                            "barcode": fixed, "name": name, "category": category, "brand": brand,
                            "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
                        })
                        return fixed
        except Exception:
            pass
        snapshot = _product_snapshot_local(barcode=str(requested_barcode or out or ""))
        _cloud_enqueue("create", "product", snapshot.get("barcode") or requested_barcode or out, snapshot or {
            "barcode": requested_barcode or out,
            "name": name,
            "category": category,
            "brand": brand,
            "location": location,
            "sell_price": sell_price,
            "stock_qty": stock_qty,
            "low_stock_level": low_stock_level,
        })
        return requested_barcode or out

    payload = {
        "name": name, "category": category, "brand": brand,
        "location": location, "sell_price": sell_price, "stock_qty": stock_qty, "low_stock_level": low_stock_level,
        "cost_price": float(cost_price or 0.0), "supplier": str(supplier or "")
    }
    if requested_barcode:
        payload["barcode"] = requested_barcode
    j = _remote_post("/products/add", payload)
    out = j.get("barcode")

    # In HOST mode we can patch the local DB after the server creates the product.
    if _MODE == "host":
        try:
            pid = None
            current_barcode = str(out or "")
            if isinstance(out, int):
                pid = int(out)
            else:
                prod = find_product_by_barcode(str(out)) if out else None
                if isinstance(prod, dict):
                    pid = prod.get("id") or prod.get("product_id")
                    current_barcode = str(prod.get("barcode") or current_barcode)
            if pid:
                if requested_barcode:
                    if _try_set_product_barcode_local(int(pid), requested_barcode):
                        return requested_barcode
                else:
                    fixed = _fix_generated_product_barcode_local(int(pid), current_barcode)
                    if fixed:
                        return fixed
        except Exception:
            pass

    return requested_barcode or out


def list_products(query=""):
    if _use_local_db():
        return _local().list_products(query)
    return _remote_get("/products/list", {"query": query}).get("items", [])


def find_product_by_barcode(barcode):
    if _use_local_db():
        return _local().find_product_by_barcode(barcode)
    return _remote_get(f"/products/by_barcode/{barcode}").get("item")


def update_product(product_id, name, sell_price, stock_qty, low_stock_level, location="", category="", brand=""):
    if _use_local_db():
        ok = _local().update_product(product_id, name, sell_price, stock_qty, low_stock_level, location, category, brand)
        if ok:
            snapshot = _product_snapshot_local(product_id)
            payload = snapshot or {
                "product_id": int(product_id),
                "name": name,
                "sell_price": sell_price,
                "stock_qty": stock_qty,
                "low_stock_level": low_stock_level,
                "location": location,
                "category": category,
                "brand": brand,
            }
            _cloud_enqueue("update", "product", payload.get("barcode") or product_id, payload)
        return ok
    return bool(_remote_post("/products/update", {
        "product_id": product_id, "name": name, "sell_price": sell_price,
        "stock_qty": stock_qty, "low_stock_level": low_stock_level, "location": location,
        "category": category, "brand": brand
    }).get("ok", True))


def update_product_details(product_id, cost_price=0.0, supplier=""):
    if _use_local_db():
        ok = _local().update_product_details(product_id, cost_price, supplier)
        if ok:
            snapshot = _product_snapshot_local(product_id)
            _cloud_enqueue("update", "product", (snapshot or {}).get("barcode") or product_id, snapshot or {})
        return ok
    return bool(_remote_post("/products/update_details", {
        "product_id": int(product_id),
        "cost_price": float(cost_price or 0.0),
        "supplier": str(supplier or ""),
    }).get("ok", True))


def list_inventory_movements(product_id=None, limit=500):
    if _use_local_db():
        return _local().list_inventory_movements(product_id, limit)
    params = {"limit": int(limit or 500)}
    if product_id not in (None, ""):
        params["product_id"] = int(product_id)
    return _remote_get("/inventory/movements", params).get("items", [])


def adjust_stock(product_id, delta_qty, reason="Stock adjustment", movement_type="ADJUSTMENT", reference_type="", reference_id="", employee_name=""):
    if _use_local_db():
        ok = _local().adjust_stock(product_id, delta_qty, reason, movement_type, reference_type, reference_id, employee_name)
        if ok:
            snapshot = _product_snapshot_local(product_id)
            payload = snapshot or {"product_id": int(product_id)}
            payload.update({
                "product_id": int(product_id),
                "delta_qty": int(delta_qty or 0),
            })
            _cloud_enqueue("adjust_stock", "product", payload.get("barcode") or product_id, payload)
        return ok
    return bool(_remote_post("/products/adjust_stock", {
        "product_id": product_id, "delta_qty": delta_qty,
        "reason": reason, "movement_type": movement_type,
        "reference_type": reference_type, "reference_id": str(reference_id or ""),
        "employee_name": employee_name,
    }).get("ok", True))


def delete_product(product_id):
    if _use_local_db():
        snapshot = _product_snapshot_local(product_id)
        ok = _local().delete_product(product_id)
        if ok:
            payload = snapshot or {"product_id": int(product_id)}
            payload["product_id"] = int(product_id)
            payload["is_deleted"] = 1
            _cloud_enqueue("delete", "product", payload.get("barcode") or product_id, payload)
        return ok
    return bool(_remote_post("/products/delete", {"product_id": product_id}).get("ok", True))


def create_sale(cart_lines, payment_method="CASH", customer_name="", order_discount_total=0.0, notes=""):
    if _use_local_db():
        sale_id = _local().create_sale(cart_lines, payment_method, customer_name, order_discount_total, notes)
        payload = {
            "sale_id": int(sale_id or 0),
            "cart_lines": _cart_lines_with_product_barcodes_local(cart_lines),
            "payment_method": payment_method,
            "customer_name": customer_name,
            "order_discount_total": order_discount_total,
            "notes": notes,
        }
        try:
            sale, items = _local().get_sale_receipt_data(sale_id)
            payload["sale"] = dict(sale) if sale is not None else None
            payload["items"] = [dict(x) for x in (items or [])]
        except Exception:
            pass
        _cloud_enqueue("create", "sale", sale_id, payload)
        return sale_id
    j = _remote_post("/sales/create", {
        "cart_lines": cart_lines,
        "payment_method": payment_method,
        "customer_name": customer_name,
        "order_discount_total": order_discount_total,
        "notes": notes
    })
    return int(j.get("sale_id") or 0)


def get_sale_receipt_data(sale_id):
    if _use_local_db():
        return _local().get_sale_receipt_data(sale_id)
    j = _remote_get(f"/sales/{sale_id}/receipt")
    return j.get("sale"), j.get("items", [])


def list_sales_for_day(day_str, limit=500, include_voided=False):
    if _use_local_db():
        return _local().list_sales_for_day(day_str, limit, include_voided)

    items = _remote_get("/sales/day", {
        "day": day_str, "limit": limit, "include_voided": int(bool(include_voided))
    }).get("items", []) or []

    # ---- Normalize cash_paid for remote rows (host mode) ----
    # Some servers/older DBs return gross totals without a cash_paid field.
    # Rule: cash drawer only tracks NEW CASH collected.
    # - EXCHANGE / STORE_CREDIT / CARD / DEBIT / CREDIT_CARD / WHISH -> 0
    # - Otherwise: prefer row['cash_paid'] if present; else compute:
    #       paid = max(0, gross_total - exchange_credit_used)
    #   where gross_total is best-effort from total_sales/total_amount/total.
    def _parse_exchange_credit(notes):
        try:
            n = str(notes or "")
            for part in n.split(";"):
                part = part.strip()
                if part.startswith("EXCHANGE_CREDIT_APPLIED="):
                    return float(part.split("=", 1)[1] or 0.0)
                if part.startswith("BON_CREDIT_APPLIED="):
                    return float(part.split("=", 1)[1] or 0.0)
        except Exception:
            pass
        return 0.0

    def _get_float(d, *keys, default=0.0):
        for k in keys:
            if k in d and d.get(k) is not None:
                try:
                    return float(d.get(k) or 0)
                except Exception:
                    pass
        return float(default)

    for r in items:
        pm = str(r.get("payment_method", "") or "").strip().upper()
        if pm in ("EXCHANGE", "STORE_CREDIT", "CARD", "DEBIT", "CREDIT_CARD", "WHISH"):
            r["cash_paid"] = 0.0
            continue

        if r.get("cash_paid") is not None:
            try:
                r["cash_paid"] = float(r.get("cash_paid") or 0.0)
                continue
            except Exception:
                pass

        credit_used = _get_float(r, "store_credit_used", default=None)
        if credit_used is None or credit_used == 0.0:
            credit_used = _parse_exchange_credit(r.get("notes") or r.get("note") or "")

        gross_total = _get_float(r, "total_sales", "net_sales", "total_amount", "total", default=0.0)
        paid = gross_total - float(credit_used or 0.0)
        if paid < 0:
            paid = 0.0
        # round to cents
        r["cash_paid"] = round(float(paid), 2)

    return items


def search_sales(query="", include_voided=True, limit=200):
    if _use_local_db():
        return _local().search_sales(query, include_voided, limit)
    return _remote_get("/sales/search", {
        "query": str(query or ""), "include_voided": int(bool(include_voided)),
        "limit": int(limit or 200),
    }).get("items", [])


def list_product_sales(product_id, limit=200, include_voided=True):
    if _use_local_db():
        return _local().list_product_sales(product_id, limit, include_voided)
    return _remote_get(f"/products/{int(product_id)}/sales", {
        "limit": int(limit or 200), "include_voided": int(bool(include_voided)),
    }).get("items", [])


def list_product_price_history(product_id, limit=200):
    if _use_local_db():
        return _local().list_product_price_history(product_id, limit)
    return _remote_get(f"/products/{int(product_id)}/price-history", {
        "limit": int(limit or 200),
    }).get("items", [])


def reorder_suggestions(days=30, target_days=14, supplier="", limit=1000):
    if _use_local_db():
        return _local().reorder_suggestions(days, target_days, supplier, limit)
    return _remote_get("/products/reorder-suggestions", {
        "days": int(days or 30),
        "target_days": int(target_days or 14),
        "supplier": str(supplier or ""),
        "limit": int(limit or 1000),
    }).get("items", [])


def analytics_discount_impact(start_date, end_date, limit=100):
    if _use_local_db():
        return _local().analytics_discount_impact(start_date, end_date, limit)
    return _remote_get("/analytics/discount-impact", {
        "start_date": str(start_date or ""),
        "end_date": str(end_date or ""),
        "limit": int(limit or 100),
    })



def get_sale_detail(sale_id):
    if _use_local_db():
        return _local().get_sale_detail(sale_id)
    j = _remote_get(f"/sales/{sale_id}/detail")
    return j.get("sale"), j.get("items", [])


def get_sale_detail_with_returns(sale_id):
    if _use_local_db():
        return _local().get_sale_detail_with_returns(sale_id)
    j = _remote_get(f"/sales/{sale_id}/detail_with_returns")
    return j.get("sale"), j.get("items", [])


def get_sale_by_receipt_scan(scan_value: str):
    if _use_local_db():
        return _local().get_sale_by_receipt_scan(scan_value)
    j = _remote_get("/sales/by_receipt_scan", {"scan": scan_value})
    return j.get("sale"), j.get("items", [])


def create_return(original_sale_id: int, returned_lines, notes: str = ""):
    """Create a return and return (return_id, total_return_amount).

    The database validates the selected receipt rows and is authoritative for the
    returned value. UI amounts are previews only.
    """

    if _use_local_db():
        rid, backend_total = _local().create_return(original_sale_id, returned_lines, notes)
        validated_lines = returned_lines
        try:
            _return, validated_lines = _local().get_return_detail(rid)
        except Exception:
            pass
        _cloud_enqueue("create", "return", rid, {
            "return_id": int(rid or 0),
            "original_sale_id": int(original_sale_id or 0),
            "returned_lines": validated_lines,
            "notes": notes,
            "expected_total": float(backend_total or 0.0),
        })
        try:
            backend_total = float(backend_total or 0.0)
        except Exception:
            backend_total = 0.0

        return int(rid), float(backend_total)

    j = _remote_post("/returns/create", {
        "original_sale_id": original_sale_id,
        "returned_lines": returned_lines,
        "notes": notes
    })

    rid = int(j.get("return_id") or j.get("id") or 0)
    try:
        backend_total = float(j.get("total_return_amount", 0.0) or 0.0)
    except Exception:
        backend_total = 0.0

    return rid, float(backend_total)


def list_returns_for_sale(original_sale_id: int, include_voided: bool = False):
    if _use_local_db():
        return _local().list_returns_for_sale(original_sale_id, include_voided)
    return _remote_get(
        f"/sales/{int(original_sale_id)}/returns",
        {"include_voided": int(bool(include_voided))}
    ).get("items", [])


def list_recent_returns(limit: int = 20, include_voided: bool = False):
    if _use_local_db():
        return _local().list_recent_returns(limit, include_voided)
    return _remote_get(
        "/returns/recent",
        {"limit": int(limit or 20), "include_voided": int(bool(include_voided))}
    ).get("items", [])


def void_return(return_id: int, notes: str = ""):
    if _use_local_db():
        out = _local().void_return(return_id, notes)
        _cloud_enqueue("void", "return", return_id, {
            "return_id": int(return_id or 0),
            "notes": str(notes or ""),
        })
        return out
    return _remote_post("/returns/void", {"return_id": int(return_id), "notes": str(notes or "")})


def reset_returns_for_sale(original_sale_id: int, notes: str = ""):
    if _use_local_db():
        out = _local().reset_returns_for_sale(original_sale_id, notes)
        _cloud_enqueue("reset_for_sale", "return", original_sale_id, {
            "original_sale_id": int(original_sale_id or 0),
            "notes": str(notes or ""),
        })
        return out
    j = _remote_post("/returns/reset_for_sale", {
        "original_sale_id": int(original_sale_id),
        "notes": str(notes or ""),
    })
    # If HOST mode is running an older in-process server, fall back to local DB.
    if _MODE == "host" and (not j.get("ok")):
        return _local().reset_returns_for_sale(original_sale_id, notes)
    return j


def create_bon(return_id: int | None = None, issued_by_name: str = "", signature_text: str = "", notes: str = "", amount=None):
    if _use_local_db():
        bon = _local().create_bon(return_id, issued_by_name, signature_text, notes, amount)
        try:
            _cloud_enqueue("create", "bon", (bon or {}).get("code") or return_id, bon or {
                "return_id": int(return_id) if return_id not in (None, "") else None,
                "amount": amount,
                "issued_by_name": issued_by_name,
                "signature_text": signature_text,
                "notes": notes,
            })
        except Exception:
            pass
        return bon
    return _remote_post("/bons/create", {
        "return_id": int(return_id) if return_id not in (None, "") else None,
        "amount": amount,
        "issued_by_name": str(issued_by_name or ""),
        "signature_text": str(signature_text or ""),
        "notes": str(notes or ""),
    }).get("bon")


def get_bon_by_code(code: str):
    if _use_local_db():
        return _local().get_bon_by_code(code)
    return _remote_get("/bons/by_code", {"code": str(code or "")}).get("bon")


def list_bons(query: str = "", active_only: bool = False, limit: int = 200):
    if _use_local_db():
        return _local().list_bons(query, active_only, limit)
    return _remote_get("/bons/list", {
        "query": str(query or ""),
        "active_only": int(bool(active_only)),
        "limit": int(limit or 200),
    }).get("items", [])


def void_bon(code: str, notes: str = ""):
    if _use_local_db():
        bon = _local().void_bon(code, notes)
        try:
            _cloud_enqueue("void", "bon", (bon or {}).get("code") or code, {
                "code": (bon or {}).get("code") or code,
                "notes": str(notes or ""),
                "bon": bon,
            })
        except Exception:
            pass
        return bon
    return _remote_post("/bons/void", {"code": str(code or ""), "notes": str(notes or "")}).get("bon")


def delete_sale(sale_id, restore_stock=True):
    if _use_local_db():
        sale_snapshot = None
        item_snapshot = []
        try:
            sale_snapshot, item_snapshot = _local().get_sale_detail(sale_id)
            sale_snapshot = dict(sale_snapshot) if sale_snapshot is not None else None
            item_snapshot = [dict(x) for x in (item_snapshot or [])]
        except Exception:
            pass
        ok = _local().delete_sale(sale_id, restore_stock)
        if ok:
            _cloud_enqueue("delete", "sale", sale_id, {
                "sale_id": int(sale_id or 0),
                "restore_stock": bool(restore_stock),
                "sale": sale_snapshot,
                "items": item_snapshot,
            })
        return ok
    j = _remote_post("/sales/delete", {"sale_id": sale_id, "restore_stock": restore_stock})
    return bool(j.get("ok", True))


def void_sale(sale_id, reason="", voided_by="", restore_stock=True):
    if _use_local_db():
        ok = _local().void_sale(sale_id, reason, voided_by, restore_stock)
        if ok:
            sale, items = _local().get_sale_detail(sale_id)
            _cloud_enqueue("void", "sale", sale_id, {
                "sale_id": int(sale_id),
                "reason": str(reason or ""),
                "voided_by": str(voided_by or ""),
                "restore_stock": bool(restore_stock),
                "sale": dict(sale) if sale else None,
                "items": [dict(x) for x in (items or [])],
            })
        return ok
    j = _remote_post("/sales/void", {
        "sale_id": int(sale_id), "reason": str(reason or ""),
        "voided_by": str(voided_by or ""), "restore_stock": bool(restore_stock),
    })
    return bool(j.get("ok", True))


# Employees / shifts

def list_employees(active_only=True):
    if _use_local_db():
        return _local().list_employees(active_only)
    return _remote_get("/employees/list", {"active_only": int(bool(active_only))}).get("items", [])


def ensure_employee(name, pin=""):
    if _use_local_db():
        employee_id = _local().ensure_employee(name, pin)
        try:
            _cloud_enqueue("upsert", "employee", employee_id, _employee_snapshot_local(employee_id) or {
                "id": int(employee_id or 0),
                "name": name,
                "pin": pin,
                "is_active": 1,
            })
        except Exception:
            pass
        return employee_id
    j = _remote_post("/employees/ensure", {"name": name, "pin": pin})
    return int(j["employee_id"])



def employee_pin_required(name: str) -> bool:
    """True if employee has a non-empty PIN set.

    Notes:
    - standalone: checks local SQLite employees.pin
    - host: prefers the HTTP endpoint; if the endpoint is missing/unavailable, falls back to local SQLite
    - connect: asks the host; if unreachable or endpoint missing, returns False (UI can still block via verify_employee_pin)
    """
    name = (name or "").strip()
    if not name:
        return False

    # CONNECT mode: only host can know pins
    if _MODE == "connect":
        j = _remote_get("/employees/has_pin", {"name": name})
        if isinstance(j, dict) and "has_pin" in j and "error" not in j:
            return bool(j.get("has_pin", False))
        return False

    # HOST mode: try remote first, but fall back if the endpoint is missing/unavailable
    if _MODE == "host":
        j = _remote_get("/employees/has_pin", {"name": name})
        if isinstance(j, dict) and "has_pin" in j and "error" not in j:
            return bool(j.get("has_pin", False))
        # else: fall back to local sqlite below

    # Standalone / host fallback: local SQLite check
    conn = None
    try:
        import sqlite3
        base_dir = BASE_DIR
        db_path = base_dir / "pos.db"
        if not db_path.exists():
            return False

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(employees)")
        cols = [r[1] for r in cur.fetchall()]
        if "pin" not in cols:
            return False

        cur.execute("SELECT pin FROM employees WHERE name = ? LIMIT 1", (name,))
        row = cur.fetchone()
        pin = (row[0] if row else "") or ""
        return bool(str(pin).strip())
    except Exception:
        return False
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

def verify_employee_pin(name: str, pin: str) -> bool:
    """Verify employee PIN.

    Rules:
    - If employee has no PIN set: returns True.
    - standalone: checks local SQLite employees.pin.
    - host: prefers remote endpoint; if endpoint is missing/unavailable, falls back to local SQLite.
    - connect: verifies against host endpoint; if endpoint is missing/unreachable => False (block).
    """
    name = (name or "").strip()
    pin = (pin or "").strip()

    if not name:
        return False

    # CONNECT mode: must verify on host
    if _MODE == "connect":
        j = _remote_post("/employees/verify_pin", {"name": name, "pin": pin})
        return bool(isinstance(j, dict) and j.get("ok", False))

    # HOST mode: remote first, fall back only when the endpoint is missing/unavailable
    if _MODE == "host":
        j = _remote_post("/employees/verify_pin", {"name": name, "pin": pin})
        if isinstance(j, dict) and "error" not in j and "ok" in j:
            return bool(j.get("ok", False))
        # else: fall back to local sqlite below

    # Standalone / host fallback: local SQLite verify
    conn = None
    try:
        import sqlite3
        base_dir = BASE_DIR
        db_path = base_dir / "pos.db"
        if not db_path.exists():
            return False

        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(employees)")
        cols = [r[1] for r in cur.fetchall()]
        if "pin" not in cols:
            return False

        cur.execute("SELECT pin FROM employees WHERE name = ? LIMIT 1", (name,))
        row = cur.fetchone()
        stored = (row[0] if row else "") or ""
        stored = str(stored).strip()

        # No PIN set => allow
        if not stored:
            return True

        return pin == stored
    except Exception:
        return False
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass

def deactivate_employee(name: str) -> bool:
    if _use_local_db():
        ok = _local().deactivate_employee(name)
        if ok:
            try:
                _cloud_enqueue("deactivate", "employee", name, {
                    "name": str(name or "").strip(),
                    "is_active": 0,
                })
            except Exception:
                pass
        return ok
    j = _remote_post("/employees/deactivate", {"name": name})
    return bool(j.get("ok", False))


def get_open_shift():
    if _use_local_db():
        return _local().get_open_shift()
    return _remote_get("/shifts/open").get("shift")


def get_last_closed_shift():
    if _use_local_db():
        return _local().get_last_closed_shift()
    return _remote_get("/shifts/last_closed").get("shift")


def open_shift(opening_cash=0.0, notes="", employee_name="", opening_usd=None, opening_lbp=0.0, lbp_per_usd=None):
    if _use_local_db():
        cfg = _load_config()
        next_num = int(cfg.get("next_shift_number", 1) or 1)
        shift_code = str(next_num)

        shift_id = _local().open_shift(opening_cash, notes, employee_name, opening_usd, opening_lbp, lbp_per_usd, shift_code=shift_code)

        cfg["next_shift_number"] = next_num + 1
        _save_config(cfg)

        payload = _shift_snapshot_local(shift_id) or {
            "id": int(shift_id or 0),
            "shift_id": int(shift_id or 0),
            "opening_cash": opening_cash,
            "notes": notes,
            "employee_name": employee_name,
            "opening_usd": opening_usd,
            "opening_lbp": opening_lbp,
            "lbp_per_usd": lbp_per_usd,
        }
        payload["shift_id"] = int(shift_id or 0)
        _cloud_enqueue("open", "shift", shift_id, payload)
        import threading
        threading.Thread(target=send_shift_open_email, args=(shift_id,), daemon=True).start()
        return shift_id
    j = _remote_post("/shifts/open", {
        "opening_cash": opening_cash,
        "notes": notes,
        "employee_name": employee_name,
        "opening_usd": opening_usd,
        "opening_lbp": opening_lbp,
        "lbp_per_usd": lbp_per_usd,
    })
    return int(j.get("shift_id") or 0)


def reset_next_shift_number():
    if _use_local_db():
        cfg = _load_config()
        cfg["next_shift_number"] = 1
        _save_config(cfg)
        return True
    else:
        try:
            return _remote_post("/config/reset_shift_number", {}).get("ok", False)
        except Exception:
            return False


def close_shift(shift_id, closing_cash=0.0, notes="", closing_usd=None, closing_lbp=0.0, lbp_per_usd=None):
    if _use_local_db():
        ok = _local().close_shift(shift_id, closing_cash, notes, closing_usd, closing_lbp, lbp_per_usd)
        if ok:
            payload = _shift_snapshot_local(shift_id) or {
                "id": int(shift_id or 0),
                "shift_id": int(shift_id or 0),
                "closing_cash": closing_cash,
                "notes": notes,
                "closing_usd": closing_usd,
                "closing_lbp": closing_lbp,
                "lbp_per_usd": lbp_per_usd,
            }
            payload["shift_id"] = int(shift_id or 0)
            _cloud_enqueue("close", "shift", shift_id, payload)
        return ok
    j = _remote_post("/shifts/close", {
        "shift_id": shift_id,
        "closing_cash": closing_cash,
        "notes": notes,
        "closing_usd": closing_usd,
        "closing_lbp": closing_lbp,
        "lbp_per_usd": lbp_per_usd,
    })
    return bool(j.get("ok", True))


def close_shift_with_cash_takeout(
    shift_id,
    closing_cash=0.0,
    notes="",
    closing_usd=None,
    closing_lbp=0.0,
    lbp_per_usd=None,
    takeout_usd=0.0,
    takeout_lbp=0.0,
    employee_name="",
    takeout_reason="End of day close cash removed",
    takeout_notes="",
):
    if _use_local_db():
        result = _local().close_shift_with_cash_takeout(
            shift_id,
            closing_cash,
            notes,
            closing_usd,
            closing_lbp,
            lbp_per_usd,
            takeout_usd,
            takeout_lbp,
            employee_name,
            takeout_reason,
            takeout_notes,
        ) or {}
        movement_id = result.get("movement_id")
        if movement_id:
            payload = _cash_movement_snapshot_local(movement_id)
            payload["movement_id"] = int(movement_id or 0)
            _cloud_enqueue("create", "cash_movement", movement_id, payload)
        payload = _shift_snapshot_local(shift_id) or {
            "id": int(shift_id or 0),
            "shift_id": int(shift_id or 0),
            "closing_cash": closing_cash,
            "notes": notes,
            "closing_usd": closing_usd,
            "closing_lbp": closing_lbp,
            "lbp_per_usd": lbp_per_usd,
        }
        payload["shift_id"] = int(shift_id or 0)
        _cloud_enqueue("close", "shift", shift_id, payload)
        return True
    j = _remote_post("/shifts/close_with_takeout", {
        "shift_id": shift_id,
        "closing_cash": closing_cash,
        "notes": notes,
        "closing_usd": closing_usd,
        "closing_lbp": closing_lbp,
        "lbp_per_usd": lbp_per_usd,
        "takeout_usd": takeout_usd,
        "takeout_lbp": takeout_lbp,
        "employee_name": employee_name,
        "takeout_reason": takeout_reason,
        "takeout_notes": takeout_notes,
    })
    return bool(j.get("ok", True))


def shift_summary(shift_id):
    if _use_local_db():
        return _local().shift_summary(shift_id)
    return _remote_get("/shifts/summary", {"shift_id": shift_id}).get("summary", {})


def list_shifts(limit=60):
    if _use_local_db():
        return _local().list_shifts(limit)
    return _remote_get("/shifts/list", {"limit": limit}).get("items", [])


def record_cash_movement(
    shift_id,
    movement_type="OUT",
    amount_usd=0.0,
    amount_lbp=0.0,
    reason="",
    employee_name="",
    notes="",
    lbp_per_usd=None,
):
    if _use_local_db():
        movement_id = _local().record_cash_movement(
            shift_id,
            movement_type,
            amount_usd,
            amount_lbp,
            reason,
            employee_name,
            notes,
            lbp_per_usd,
        )
        payload = _cash_movement_snapshot_local(movement_id) or {
            "id": int(movement_id or 0),
            "movement_id": int(movement_id or 0),
            "shift_id": int(shift_id or 0),
            "movement_type": movement_type,
            "amount_usd": amount_usd,
            "amount_lbp": amount_lbp,
            "reason": reason,
            "employee_name": employee_name,
            "notes": notes,
            "lbp_per_usd": lbp_per_usd,
        }
        payload["movement_id"] = int(movement_id or 0)
        _cloud_enqueue("create", "cash_movement", movement_id, payload)
        return movement_id
    j = _remote_post("/cash_movements/record", {
        "shift_id": shift_id,
        "movement_type": movement_type,
        "amount_usd": amount_usd,
        "amount_lbp": amount_lbp,
        "reason": reason,
        "employee_name": employee_name,
        "notes": notes,
        "lbp_per_usd": lbp_per_usd,
    })
    if j.get("ok") is False and j.get("error"):
        raise RuntimeError(str(j.get("error")))
    return int(j.get("movement_id") or 0)


def list_cash_movements(shift_id=None, day_str=None, limit=500):
    if _use_local_db():
        return _local().list_cash_movements(shift_id=shift_id, day_str=day_str, limit=limit)
    params = {"limit": int(limit or 500)}
    if shift_id is not None:
        params["shift_id"] = int(shift_id)
    if day_str:
        params["day_str"] = str(day_str)
    return _remote_get("/cash_movements/list", params).get("items", [])


# Analytics

def _range_bounds(which):
    return _local()._range_bounds(which)


def analytics_kpis_range(start_date, end_date):
    if _use_local_db():
        return _local().analytics_kpis_range(start_date, end_date)
    return _remote_get("/analytics/kpis", {"start": start_date, "end": end_date}).get("kpis", {})


def analytics_breakdown_range(start_date, end_date):
    if _use_local_db():
        return _local().analytics_breakdown_range(start_date, end_date)
    return _remote_get("/analytics/breakdown", {"start": start_date, "end": end_date}).get("breakdown", {})


def analytics_series_in_range(start_date, end_date, group="day"):
    if _use_local_db():
        return _local().analytics_series_in_range(start_date, end_date, group)
    return _remote_get("/analytics/series", {"start": start_date, "end": end_date, "group": group}).get("items", [])


def analytics_top_products_range(start_date, end_date, limit=12):
    if _use_local_db():
        return _local().analytics_top_products_range(start_date, end_date, limit)
    return _remote_get("/analytics/top_products", {"start": start_date, "end": end_date, "limit": limit}).get("items", [])


def analytics_low_stock(limit=50):
    if _use_local_db():
        return _local().analytics_low_stock(limit)
    return _remote_get("/analytics/low_stock", {"limit": limit}).get("items", [])


def data_health_summary(sample_limit=8):
    if _use_local_db():
        return _local().data_health_summary(sample_limit)
    return _remote_get("/analytics/data_health", {"sample_limit": int(sample_limit or 8)}).get("health", {})



# ---------------- Backups (local-authoritative modes) ----------------

def _backup_status_path() -> Path:
    backups_dir = BASE_DIR / "backups"
    backups_dir.mkdir(exist_ok=True)
    return backups_dir / "offsite_backup_status.json"


def _write_backup_status(status: dict) -> None:
    path = _backup_status_path()
    tmp_path = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        tmp_path.write_text(json.dumps(status, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _find_rclone() -> str:
    configured = os.environ.get("MASKPOS_RCLONE_PATH", "").strip()
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("rclone")
    if found:
        return found

    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "rclone.exe",
        Path(os.environ.get("USERPROFILE", "")) / "scoop" / "shims" / "rclone.exe",
        Path(os.environ.get("ProgramFiles", "")) / "rclone" / "rclone.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    try:
        packages_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        for path in packages_dir.glob("Rclone.Rclone_*/*/rclone.exe"):
            if path.exists():
                return str(path)
    except Exception:
        pass
    return ""


def get_backup_config() -> dict:
    """Return local/off-site backup settings and the latest upload result."""
    cfg = _load_config()
    remote = str(
        os.environ.get("MASKPOS_BACKUP_RCLONE_REMOTE", "")
        or cfg.get("backup_rclone_remote")
        or ""
    ).strip()
    status = {}
    try:
        path = _backup_status_path()
        if path.exists():
            status = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        status = {}
    return {
        "backup_rclone_remote": remote,
        "rclone_available": bool(_find_rclone()),
        "offsite": status,
    }


def set_backup_rclone_remote(remote: str) -> None:
    """Save an optional rclone destination such as maskpos-drive:MaskPOS Backups."""
    cfg = _load_config()
    cfg["backup_rclone_remote"] = str(remote or "").strip()
    _save_config(cfg)


def _upload_backup_offsite(backup_path: Path) -> bool:
    """Upload a completed snapshot when an rclone destination is configured."""
    remote = str(get_backup_config().get("backup_rclone_remote") or "").strip()
    if not remote:
        return True

    rclone = _find_rclone()
    status = {
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": str(backup_path),
        "destination": remote,
        "ok": False,
        "message": "",
    }
    if not rclone:
        status["message"] = "rclone is not installed or could not be found."
        _write_backup_status(status)
        return False

    destination = f"{remote.rstrip('/')}/{backup_path.name}"
    try:
        completed = subprocess.run(
            [
                rclone,
                "copyto",
                str(backup_path),
                destination,
                "--transfers", "1",
                "--checkers", "1",
                "--contimeout", "10s",
                "--timeout", "60s",
                "--retries", "2",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            **_no_window_kwargs(),
        )
        status["ok"] = completed.returncode == 0
        if status["ok"]:
            status["message"] = f"Uploaded {backup_path.name} to {destination}"
        else:
            detail = (completed.stderr or completed.stdout or "rclone upload failed").strip()
            status["message"] = detail[-500:]
    except Exception as exc:
        status["message"] = f"{type(exc).__name__}: {exc}"[:500]

    _write_backup_status(status)
    return bool(status["ok"])

def backup_pos_db(keep_last: int = 30, upload_offsite: bool = True) -> bool:
    """Create a consistent daily backup of the local-authoritative database.

    Creates backups/pos_YYYY-MM-DD.db (one per day).
    Refreshes today's snapshot and keeps the last N daily backups.
    """
    temp_path = None
    try:
        if backend_mode() == "connect":
            return False

        from datetime import date
        import sqlite3

        base_dir = BASE_DIR
        db_path = base_dir / "pos.db"
        if not db_path.exists():
            return False

        backups_dir = base_dir / "backups"
        backups_dir.mkdir(exist_ok=True)

        today = date.today().isoformat()
        backup_path = backups_dir / f"pos_{today}.db"
        temp_path = backups_dir / f".{backup_path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"

        source = sqlite3.connect(str(db_path), timeout=10)
        target = sqlite3.connect(str(temp_path), timeout=10)
        try:
            source.backup(target)
            target.commit()
            integrity = target.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise RuntimeError("SQLite backup integrity check failed.")
        finally:
            target.close()
            source.close()
        os.replace(temp_path, backup_path)
        temp_path = None

        # Cleanup: keep only last N backups
        files = sorted(
            backups_dir.glob("pos_*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old in files[keep_last:]:
            try:
                old.unlink()
            except Exception:
                pass
        if upload_offsite:
            _upload_backup_offsite(backup_path)
        return True
    except Exception:
        # backups must never crash POS
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass


def _start_periodic_backup_if_local(interval_seconds: int = 6 * 60 * 60) -> None:
    """Refresh today's local snapshot on startup and periodically while open."""
    global _BACKUP_THREAD
    if backend_mode() == "connect":
        return
    try:
        backup_pos_db()
    except Exception:
        pass
    if _BACKUP_THREAD is not None and _BACKUP_THREAD.is_alive():
        return
    _BACKUP_STOP_EVENT.clear()

    def worker() -> None:
        while not _BACKUP_STOP_EVENT.wait(max(60, int(interval_seconds or 0))):
            try:
                backup_pos_db()
            except Exception:
                pass

    _BACKUP_THREAD = threading.Thread(target=worker, daemon=True, name="maskpos-backup")
    _BACKUP_THREAD.start()


def open_backups_folder() -> None:
    """Open backups folder in Explorer (Windows)."""
    try:
        base_dir = BASE_DIR
        backups_dir = base_dir / "backups"
        backups_dir.mkdir(exist_ok=True)

        if os.name == "nt":
            os.startfile(str(backups_dir))
    except Exception:
        return


# ---------------- Printer Settings + Direct Printing ----------------

def get_printer_config() -> dict:
    """Return saved receipt printer configuration."""
    cfg = _load_config()
    return {
        "printer_name": (cfg.get("printer_name") or "").strip(),
        "print_mode": (cfg.get("print_mode") or "raw").strip(),  # raw | sumatra
    }

def set_printer_config(printer_name: str, print_mode: str = "raw") -> None:
    """Save receipt printer configuration to pos_config.json."""
    cfg = _load_config()
    cfg["printer_name"] = (printer_name or "").strip()
    cfg["print_mode"] = (print_mode or "raw").strip()
    _save_config(cfg)

def list_printers() -> list[str]:
    """List installed printers on Windows.

    Uses pywin32 if available. Falls back to PowerShell / WMIC so the
    Settings dropdown still works even if pywin32 isn't installed.
    """
    if os.name != "nt":
        return []

    names: list[str] = []

    # 1) pywin32 (best)
    try:
        import win32print  # type: ignore
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        printers = win32print.EnumPrinters(flags)
        names = [p[2] for p in printers if p and len(p) > 2 and p[2]]
    except Exception:
        names = []

    # 2) PowerShell fallback
    if not names:
        try:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Printer | Select-Object -ExpandProperty Name",
            ]
            out = subprocess.check_output(
                cmd,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                **_no_window_kwargs(),
            )
            names = [ln.strip() for ln in out.splitlines() if ln.strip()]
        except Exception:
            names = []

    # 3) WMIC fallback (older Windows)
    if not names:
        try:
            out = subprocess.check_output(
                ["wmic", "printer", "get", "name"],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                **_no_window_kwargs(),
            )
            names = [
                ln.strip()
                for ln in out.splitlines()
                if ln.strip() and ln.strip().lower() != "name"
            ]
        except Exception:
            names = []

    # de-dup while keeping order
    seen = set()
    out2 = []
    for n in names:
        if n not in seen:
            seen.add(n)
            out2.append(n)
    return out2


def clear_printer_queue(printer_name: str = "") -> bool:
    """Clear pending print jobs.

    Preferred method (Windows): use pywin32 to enumerate and cancel jobs on the given printer.
    Fallback: restart Windows Print Spooler and clear spool files (clears ALL printers).

    Returns True if a clear attempt succeeded.
    """
    if os.name != "nt":
        return False

    printer_name = (printer_name or "").strip()

    # 1) Best: cancel jobs for a specific printer using pywin32
    if printer_name:
        try:
            import win32print  # type: ignore

            hPrinter = win32print.OpenPrinter(printer_name)
            try:
                # EnumJobs: (printer_handle, firstJob, noJobs, level)
                jobs = win32print.EnumJobs(hPrinter, 0, 9999, 1)
                for j in jobs or []:
                    try:
                        job_id = j.get("JobId") if isinstance(j, dict) else None
                        if job_id is None and isinstance(j, (list, tuple)) and len(j) > 0:
                            job_id = j[0]
                        if job_id is not None:
                            win32print.SetJob(hPrinter, int(job_id), 0, None, win32print.JOB_CONTROL_CANCEL)
                    except Exception:
                        pass
            finally:
                win32print.ClosePrinter(hPrinter)
            return True
        except Exception:
            pass

    # 2) Fallback: restart spooler + clear spool files (clears all printers)
    try:
        spool_dir = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "spool" / "PRINTERS"

        # Stop spooler
        subprocess.run(["net", "stop", "spooler"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

        # Delete spool files
        try:
            if spool_dir.exists():
                for p in spool_dir.glob("*"):
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

        # Start spooler
        subprocess.run(["net", "start", "spooler"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False



def hard_reset_printing(printer_name: str = "") -> bool:
    """Hard reset printing so old jobs do not print after reconnect.

    Best effort steps (Windows):
      - Cancel jobs for the selected printer (pywin32 if available)
      - Kill SumatraPDF.exe (PDF printing can keep retrying)
      - Restart Windows Print Spooler
      - Clear spool files (.spl/.shd) for ALL printers

    Returns True if the reset attempt ran without fatal errors.
    """
    if os.name != "nt":
        return False

    printer_name = (printer_name or "").strip()
    ok = True

    # 1) Cancel jobs for a specific printer (best effort)
    if printer_name:
        try:
            import win32print  # type: ignore
            import win32con  # type: ignore

            h = win32print.OpenPrinter(printer_name)
            try:
                jobs = win32print.EnumJobs(h, 0, 999, 1) or []
                for j in jobs:
                    try:
                        win32print.SetJob(h, j.get("JobId"), 0, None, win32con.JOB_CONTROL_CANCEL)
                    except Exception:
                        pass
            finally:
                win32print.ClosePrinter(h)
        except Exception:
            # Do not fail reset if pywin32 is missing
            pass

    # 2) Kill Sumatra (prevents delayed PDF print retries)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "SumatraPDF.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        pass

    # 3) Stop spooler
    try:
        subprocess.run(["net", "stop", "spooler"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        ok = False

    # 4) Clear spool files
    try:
        spool_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "spool" / "PRINTERS"
        if spool_dir.exists():
            for p in spool_dir.glob("*"):
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        ok = False

    # 5) Start spooler
    try:
        subprocess.run(["net", "start", "spooler"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    except Exception:
        ok = False

    return ok

def _raw_print_text(printer_name: str, text: str) -> bool:
    """Send RAW text to Windows printer using pywin32."""
    if os.name != "nt":
        return False
    try:
        import win32print  # type: ignore
        hPrinter = win32print.OpenPrinter(printer_name)
        try:
            hJob = win32print.StartDocPrinter(hPrinter, 1, ("MaskPOS", None, "RAW"))
            win32print.StartPagePrinter(hPrinter)
            win32print.WritePrinter(hPrinter, text.encode("utf-8", errors="ignore"))
            win32print.EndPagePrinter(hPrinter)
            win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)
        return True
    except Exception:
        return False

def test_print_configured() -> bool:
    """Print a simple test slip to the configured printer."""
    cfg = get_printer_config()
    name = cfg.get("printer_name") or ""
    if not name:
        return False
    mode = (cfg.get("print_mode") or "raw").lower()
    if mode == "raw":
        text = "MASK POS TEST PRINT\r\n------------------------------\r\nOK\r\n\r\n\r\n"
        return _raw_print_text(name, text)
    # sumatra pdf test
    try:
        from receipt_pdf import create_temp_receipt_pdf
        dummy_sale = {"id": 0, "created_at": "", "total_amount": 0.0, "payment_method": "CASH", "shift_id": ""}
        dummy_items = []
        pdf_path = str(create_temp_receipt_pdf(get_store_name(), dummy_sale, dummy_items))
        return _sumatra_print_pdf(name, pdf_path)
    except Exception:
        return False

def _sumatra_path() -> str:
    candidates = []
    base_dir = BASE_DIR

    # Source/development and copied-next-to-EXE layouts
    candidates.append(base_dir / "SumatraPDF.exe")
    candidates.append(base_dir / "SumatraPDF" / "SumatraPDF.exe")

    # PyInstaller one-folder layout: datas usually live under _internal
    try:
        if getattr(sys, "frozen", False):
            candidates.append(base_dir / "_internal" / "SumatraPDF.exe")
            candidates.append(base_dir / "_internal" / "SumatraPDF" / "SumatraPDF.exe")
    except Exception:
        pass

    # PyInstaller runtime extraction/bundle folder
    try:
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        if meipass:
            candidates.append(meipass / "SumatraPDF.exe")
            candidates.append(meipass / "SumatraPDF" / "SumatraPDF.exe")
    except Exception:
        pass

    for p in candidates:
        try:
            if p and p.exists():
                return str(p)
        except Exception:
            pass

    # common install locations
    p2 = Path("C:/Program Files/SumatraPDF/SumatraPDF.exe")
    if p2.exists():
        return str(p2)
    p3 = Path("C:/Program Files (x86)/SumatraPDF/SumatraPDF.exe")
    if p3.exists():
        return str(p3)
    return ""

def _sumatra_print_pdf(printer_name: str, pdf_path: str, orientation: str = "portrait") -> bool:
    """Print a PDF via SumatraPDF.

    Some Sumatra builds do not support newer flags. This version uses only
    stable flags.
    """
    if os.name != "nt":
        return False
    exe = _sumatra_path()
    if not exe or not os.path.exists(str(pdf_path)):
        return False
    try:
        # Keep flags minimal for better compatibility with 32-bit SumatraPDF.
        subprocess.Popen(
            [
                exe,
                "-print-to", printer_name,
                "-print-settings", f"noscale,{orientation}",
                "-silent",
                "-exit-when-done",
                pdf_path,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False

def print_configured_receipt(shop_name: str, sale: dict, items: list[dict]) -> bool:
    """Print NORMAL receipt using configured receipt printer. Returns True if print was sent."""
    cfg = get_printer_config()
    printer_name = (cfg.get("printer_name") or "").strip()
    if not printer_name:
        return False

    mode = (cfg.get("print_mode") or "raw").lower()

    # 1) RAW mode (pywin32 direct print) for normal receipts
    if mode == "raw":
        try:
            from receipt_print import print_receipt  # uses pywin32 RAW printing
            print_receipt(printer_name, shop_name, sale, items)
            return True
        except Exception:
            # fallback to PDF if RAW isn't available
            mode = "sumatra"

    # 2) PDF mode via Sumatra
    if mode == "sumatra":
        try:
            from receipt_pdf import create_temp_receipt_pdf
            pdf_path = str(create_temp_receipt_pdf(shop_name, sale, items))
            return _sumatra_print_pdf(printer_name, pdf_path)
        except Exception:
            return False

    # Unknown mode
    return False


def print_configured_weekly_receipt(shop_name: str, rows: list[dict], title: str = "Weekly Receipt") -> bool:
    """Print a custom combined weekly receipt via the configured receipt printer.

    This is print-only and does not change stored sales.
    """
    cfg = get_printer_config()
    printer_name = (cfg.get("printer_name") or "").strip()
    if not printer_name:
        return False

    # Force PDF path for this custom receipt to keep formatting stable.
    try:
        from receipt_pdf import create_weekly_selection_receipt_pdf
        pdf_path = str(create_weekly_selection_receipt_pdf(shop_name, rows, title=title))
    except Exception:
        return False

    mode = (cfg.get("print_mode") or "raw").lower()
    if mode == "raw":
        mode = "sumatra"

    if mode == "sumatra":
        try:
            return _sumatra_print_pdf(printer_name, pdf_path)
        except Exception:
            return False

    try:
        os.startfile(pdf_path)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def print_configured_warehouse_paper(shop_name: str, items: list[dict], title: str = "Warehouse Locations") -> bool:
    """Print a selected product/location list using the configured receipt printer."""
    cfg = get_printer_config()
    printer_name = (cfg.get("printer_name") or "").strip()
    if not printer_name:
        return False

    try:
        from receipt_pdf import create_warehouse_locations_pdf
        pdf_path = str(create_warehouse_locations_pdf(shop_name, items, title=title))
    except Exception:
        return False

    mode = (cfg.get("print_mode") or "raw").lower()
    if mode == "raw":
        mode = "sumatra"

    if mode == "sumatra":
        try:
            return _sumatra_print_pdf(printer_name, pdf_path)
        except Exception:
            return False

    try:
        os.startfile(pdf_path)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def print_configured_bon(shop_name: str, bon: dict) -> bool:
    """Print a store-credit bon slip using the configured receipt printer."""
    cfg = get_printer_config()
    printer_name = (cfg.get("printer_name") or "").strip()
    if not printer_name:
        return False

    try:
        from receipt_pdf import create_bon_receipt_pdf
        pdf_path = str(create_bon_receipt_pdf(shop_name, bon))
    except Exception:
        return False

    mode = (cfg.get("print_mode") or "raw").lower()
    if mode == "raw":
        mode = "sumatra"

    if mode == "sumatra":
        try:
            return _sumatra_print_pdf(printer_name, pdf_path)
        except Exception:
            return False

    try:
        os.startfile(pdf_path)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def print_configured_gift_receipt(shop_name: str, sale: dict, items: list[dict]) -> bool:
    """Print GIFT receipt using the configured receipt printer.

    Gift receipts must be PDF (no prices). If receipt printer is set to RAW, we still print via PDF.
    """
    cfg = get_printer_config()
    printer_name = (cfg.get("printer_name") or "").strip()
    if not printer_name:
        return False

    mode = (cfg.get("print_mode") or "raw").lower()

    # Force PDF path for gift receipts
    if mode == "raw":
        mode = "sumatra"

    if mode == "sumatra":
        try:
            from receipt_pdf import create_gift_receipt_pdf
            pdf_path = str(create_gift_receipt_pdf(shop_name, sale, items))
            return _sumatra_print_pdf(printer_name, pdf_path)
        except Exception:
            return False

    # Last resort: open PDF in default viewer
    try:
        from receipt_pdf import create_gift_receipt_pdf
        pdf_path = str(create_gift_receipt_pdf(shop_name, sale, items))
        os.startfile(pdf_path)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False



# ---------------- Barcode Label Printer Settings + Printing ----------------

def get_barcode_printer_config() -> dict:
    """Return saved barcode-label printer configuration."""
    cfg = _load_config()
    return {
        "barcode_printer_name": (cfg.get("barcode_printer_name") or "").strip(),
        "barcode_print_mode": (cfg.get("barcode_print_mode") or "sumatra").strip(),  # sumatra | open
    }


def set_barcode_printer_config(barcode_printer_name: str, barcode_print_mode: str = "sumatra") -> None:
    """Save barcode-label printer configuration to pos_config.json."""
    cfg = _load_config()
    cfg["barcode_printer_name"] = (barcode_printer_name or "").strip()
    cfg["barcode_print_mode"] = (barcode_print_mode or "sumatra").strip()
    _save_config(cfg)


def print_configured_barcodes(labels: list[dict], title: str = "Mask POS Labels") -> bool:
    """Generate barcode labels PDF and print to configured barcode printer.

    Returns True if the print job was launched.
    """
    try:
        cfg = get_barcode_printer_config()
        printer_name = (cfg.get("barcode_printer_name") or "").strip()
        if not printer_name:
            return False

        mode = (cfg.get("barcode_print_mode") or "sumatra").lower().strip()

        from barcodes_pdf import make_labels_pdf

        pdf_path = str(make_labels_pdf(labels, title=title))

        if mode == "sumatra":
            ok = _sumatra_print_pdf(printer_name, pdf_path, orientation="landscape")
            if ok:
                return True
            return False

        # Explicit manual-preview mode only.
        if mode == "open" and os.name == "nt":
            try:
                os.startfile(pdf_path)
                return True
            except Exception:
                return False

        return False
    except Exception:
        return False


def test_print_barcode_configured() -> bool:
    """Print a single dummy barcode label to the configured barcode printer."""
    try:
        cfg = get_barcode_printer_config()
        printer_name = (cfg.get("barcode_printer_name") or "").strip()
        if not printer_name:
            return False

        labels = [{
            "name": "TEST LABEL",
            "price": 0.0,
            "barcode": "123456789012",  # will become valid EAN-13
            "qty": 1,
        }]
        return bool(print_configured_barcodes(labels, title="Mask POS Test Label"))
    except Exception:
        return False


# ---------------- Shutdown ----------------

def stop_backend() -> None:
    """Stop background threads (used when app exits)."""
    global _STOP_HEARTBEAT, _DISCOVERY_STOP
    with _STATUS_LOCK:
        _STOP_HEARTBEAT = True
        _DISCOVERY_STOP = True
    _BACKUP_STOP_EVENT.set()


# ---------------- DATA HEALTH & REPAIR WRAPPERS ----------------

def get_distinct_categories():
    if _use_local_db():
        return _local().get_distinct_categories()
    return _remote_get("/products/categories").get("items", [])


def get_data_health_stats():
    if _use_local_db():
        return _local().get_data_health_stats()
    return _remote_get("/health/stats").get("stats", {})


def list_health_issues(issue_type):
    if _use_local_db():
        return _local().list_health_issues(issue_type)
    return _remote_get("/health/issues", {"type": issue_type}).get("items", [])


def bulk_update_products(product_ids, category=None, location=None, low_stock=None, brand=None):
    if _use_local_db():
        ok = _local().bulk_update_products(product_ids, category, location, low_stock, brand)
        if ok:
            for pid in product_ids:
                snapshot = _product_snapshot_local(pid)
                if snapshot:
                    _cloud_enqueue("update", "product", snapshot.get("barcode") or pid, snapshot)
        return ok
    
    return bool(_remote_post("/products/bulk_edit", {
        "product_ids": [int(x) for x in product_ids],
        "category": category,
        "location": location,
        "low_stock": low_stock,
        "brand": brand
    }).get("ok", True))


def repair_broken_product_links(sale_item_ids, target_product_id):
    if _use_local_db():
        return _local().repair_broken_product_links(sale_item_ids, target_product_id)
    
    return bool(_remote_post("/sales/repair_links", {
        "sale_item_ids": [int(x) for x in sale_item_ids],
        "target_product_id": int(target_product_id)
    }).get("ok", True))


def recreate_and_repair_product(name, barcode, sell_price, cost_price, supplier, category, brand, location, sale_item_ids):
    if _use_local_db():
        ok = _local().recreate_and_repair_product(name, barcode, sell_price, cost_price, supplier, category, brand, location, sale_item_ids)
        if ok:
            snapshot = _product_snapshot_local(None, barcode)
            if snapshot:
                _cloud_enqueue("create", "product", barcode, snapshot)
        return ok
    
    return bool(_remote_post("/sales/recreate_repair", {
        "name": name,
        "barcode": barcode,
        "sell_price": float(sell_price),
        "cost_price": float(cost_price),
        "supplier": supplier,
        "category": category,
        "brand": brand,
        "location": location,
        "sale_item_ids": [int(x) for x in sale_item_ids]
    }).get("ok", True))
