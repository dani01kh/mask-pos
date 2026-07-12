from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


def _runtime_cloud_config() -> dict:
    paths = []
    configured = os.environ.get("MASKPOS_CLOUD_CONFIG", "").strip()
    if configured:
        paths.append(Path(configured))
    try:
        if getattr(sys, "frozen", False):
            paths.append(Path(sys.executable).resolve().parent / "cloudflare_pos_config.json")
    except Exception:
        pass
    paths.append(Path(__file__).resolve().parent / "cloudflare_pos_config.json")
    for path in paths:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


_CLOUD_CONFIG = _runtime_cloud_config()
SUPABASE_REST_URL = os.environ.get(
    "MASKPOS_CLOUD_REST_URL",
    str(_CLOUD_CONFIG.get("rest_url") or ""),
).strip()
SUPABASE_KEY = os.environ.get(
    "MASKPOS_CLOUD_API_TOKEN",
    str(_CLOUD_CONFIG.get("api_token") or ""),
).strip()
SYNC_TABLE = "pos_sync_events"
PRODUCT_TABLE = "products"
PRINT_JOB_TABLE = "pos_print_jobs"

_SYNC_THREAD = None
_STOP = False
_WAKE_SYNC = threading.Event()
_PRINT_WORKER_ENABLED = False
_AUTHORITATIVE_HOST = False
_LAST_PRINT_POLL_AT = 0.0
PRINT_POLL_INTERVAL_SECONDS = 10
_LAST_STATUS = {
    "last_upload_at": "",
    "last_download_at": "",
    "last_error": "",
}
_PROTECT_EXISTING_PENDING = False
_STARTUP_PROTECT_DONE = False
_CLOUD_WRITE_PAUSED_REASON = ""
_CLOUD_WRITE_PAUSED_AT = 0.0
_LAST_FULL_PULL_AT = 0.0
FULL_PULL_INTERVAL_SECONDS = max(60, int(os.environ.get("MASKPOS_FULL_PULL_INTERVAL_SECONDS", "90") or 90))
UPLOAD_RETRY_DELAY_SECONDS = max(5, int(os.environ.get("MASKPOS_UPLOAD_RETRY_DELAY_SECONDS", "30") or 30))
REMOTE_CURSOR_TS_KEY = "remote_event_inserted_at"
REMOTE_CURSOR_ID_KEY = "remote_event_id"


def cloud_configured() -> bool:
    return bool(SUPABASE_REST_URL.startswith("https://") and SUPABASE_KEY)


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _iso_after(seconds: int | float) -> str:
    return datetime.utcfromtimestamp(time.time() + max(0, float(seconds or 0))).replace(microsecond=0).isoformat() + "Z"


def _device_id(base_dir: Path) -> str:
    path = base_dir / "cloud_sync_device.json"
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if data.get("device_id"):
                return str(data["device_id"])
    except Exception:
        pass

    did = str(uuid.uuid4())
    try:
        path.write_text(json.dumps({"device_id": did}, indent=2), encoding="utf-8")
    except Exception:
        pass
    return did


def init_sync(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_sync_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id TEXT,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                synced_at TEXT,
                last_error TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_sync_pending ON cloud_sync_events(synced_at, created_at)")
        try:
            conn.execute("ALTER TABLE cloud_sync_events ADD COLUMN trusted_upload INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        try:
            conn.execute("ALTER TABLE cloud_sync_events ADD COLUMN retry_after TEXT")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cloud_sync_retry ON cloud_sync_events(synced_at, retry_after, created_at)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_applied_events (
                event_id TEXT PRIMARY KEY,
                device_id TEXT,
                event_type TEXT,
                entity_type TEXT,
                entity_id TEXT,
                applied_at TEXT NOT NULL,
                apply_note TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_seed_state (
                seed_name TEXT PRIMARY KEY,
                seeded_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cloud_sync_state (
                state_key TEXT PRIMARY KEY,
                state_value TEXT NOT NULL
            )
        """)
        # Add cloud tracking columns to core tables if they don't exist
        for table in ("sales", "returns", "cash_shifts", "cash_movements"):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN cloud_device_id TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN cloud_local_id TEXT")
            except sqlite3.OperationalError:
                pass
        conn.commit()
    finally:
        conn.close()


def _sync_state_values(db_path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        placeholders = ", ".join(["?"] * len(keys))
        rows = conn.execute(
            f"SELECT state_key, state_value FROM cloud_sync_state WHERE state_key IN ({placeholders})",
            list(keys),
        ).fetchall()
        return {str(row[0]): str(row[1] or "") for row in rows}
    finally:
        conn.close()


def _set_sync_state_values(db_path: Path, values: dict[str, str]) -> None:
    if not values:
        return
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO cloud_sync_state (state_key, state_value) VALUES (?, ?)",
            [(str(key), str(value or "")) for key, value in values.items()],
        )
        conn.commit()
    finally:
        conn.close()


def _remote_event_cursor(db_path: Path) -> tuple[str, str]:
    values = _sync_state_values(db_path, (REMOTE_CURSOR_TS_KEY, REMOTE_CURSOR_ID_KEY))
    return values.get(REMOTE_CURSOR_TS_KEY, ""), values.get(REMOTE_CURSOR_ID_KEY, "")


def _set_remote_event_cursor(db_path: Path, inserted_at: str, event_id: str) -> None:
    if not inserted_at:
        return
    _set_sync_state_values(db_path, {
        REMOTE_CURSOR_TS_KEY: inserted_at,
        REMOTE_CURSOR_ID_KEY: event_id,
    })


def enqueue_event(base_dir: Path, event_type: str, entity_type: str, entity_id=None, payload=None) -> None:
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return

    try:
        init_sync(db_path)
        event_id = str(uuid.uuid4())
        device_id = _device_id(base_dir)
        body = json.dumps(payload or {}, ensure_ascii=False, default=str)
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            conn.execute("""
                INSERT INTO cloud_sync_events
                    (event_id, device_id, event_type, entity_type, entity_id, payload, created_at, trusted_upload)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """, (
                event_id,
                device_id,
                str(event_type or ""),
                str(entity_type or ""),
                "" if entity_id is None else str(entity_id),
                body,
                _now_iso(),
            ))
            conn.commit()
        finally:
            conn.close()
        _WAKE_SYNC.set()
    except Exception:
        pass


def seed_local_reference_events(base_dir: Path) -> int:
    """Queue small reference tables that may have existed before cloud sync."""
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    init_sync(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT 1 FROM cloud_seed_state WHERE seed_name = ? LIMIT 1",
            ("employees_v1",),
        ).fetchone()
        if row:
            return 0

        employees = [dict(emp) for emp in conn.execute("SELECT * FROM employees").fetchall()]

        conn.execute(
            "INSERT OR REPLACE INTO cloud_seed_state (seed_name, seeded_at) VALUES (?, ?)",
            ("employees_v1", _now_iso()),
        )
        conn.commit()
    except Exception:
        return 0
    finally:
        conn.close()

    count = 0
    for payload in employees:
        enqueue_event(base_dir, "upsert", "employee", payload.get("id"), payload)
        count += 1
    return count


def _pending_events(db_path: Path, limit: int = 50) -> list[dict]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT *
            FROM cloud_sync_events
            WHERE synced_at IS NULL
              AND (retry_after IS NULL OR retry_after = '' OR retry_after <= ?)
            ORDER BY created_at ASC
            LIMIT ?
        """, (_now_iso(), int(limit))).fetchall()
        out = []
        for r in rows:
            payload = {}
            try:
                payload = json.loads(r["payload"] or "{}")
            except Exception:
                payload = {"raw": r["payload"] or ""}
            out.append({
                "event_id": r["event_id"],
                "device_id": r["device_id"],
                "event_type": r["event_type"],
                "entity_type": r["entity_type"],
                "entity_id": r["entity_id"],
                "payload": payload,
                "created_at": r["created_at"],
                "schema_version": 1,
            })
        return out
    finally:
        conn.close()


def _mark_synced(db_path: Path, event_ids: list[str]) -> None:
    if not event_ids:
        return
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.executemany(
            "UPDATE cloud_sync_events SET synced_at = ?, last_error = NULL, retry_after = NULL WHERE event_id = ?",
            [(_now_iso(), eid) for eid in event_ids],
        )
        conn.commit()
    finally:
        conn.close()


def _mark_error(db_path: Path, event_ids: list[str], error: str, retry_delay_seconds: int | float | None = None) -> None:
    _LAST_STATUS["last_error"] = str(error or "")[:500]
    if not event_ids:
        return
    retry_after = _iso_after(UPLOAD_RETRY_DELAY_SECONDS if retry_delay_seconds is None else retry_delay_seconds)
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.executemany(
            "UPDATE cloud_sync_events SET synced_at = NULL, last_error = ?, retry_after = ? WHERE event_id = ?",
            [(str(error or "")[:500], retry_after, eid) for eid in event_ids],
        )
        conn.commit()
    finally:
        conn.close()


def _is_cloud_capacity_error(status_code=None, body: str = "") -> bool:
    text = str(body or "").lower()
    if int(status_code or 0) in (402, 413, 507):
        return True
    needles = (
        "quota",
        "billing",
        "storage limit",
        "database size",
        "exceeded",
        "insufficient storage",
        "project limit",
        "plan limit",
    )
    return any(n in text for n in needles)


def _pause_cloud_writes(reason: str) -> None:
    global _CLOUD_WRITE_PAUSED_REASON, _CLOUD_WRITE_PAUSED_AT
    _CLOUD_WRITE_PAUSED_REASON = str(reason or "Cloud writes paused")[:500]
    _CLOUD_WRITE_PAUSED_AT = time.time()
    _LAST_STATUS["last_error"] = _CLOUD_WRITE_PAUSED_REASON


def _clear_cloud_write_pause() -> None:
    global _CLOUD_WRITE_PAUSED_REASON, _CLOUD_WRITE_PAUSED_AT
    _CLOUD_WRITE_PAUSED_REASON = ""
    _CLOUD_WRITE_PAUSED_AT = 0.0


def _cloud_writes_paused() -> bool:
    if not _CLOUD_WRITE_PAUSED_REASON:
        return False
    # Retry periodically so cloud starts working again after the plan is upgraded.
    return (time.time() - float(_CLOUD_WRITE_PAUSED_AT or 0.0)) < 300


def quarantine_existing_pending(base_dir: Path, reason: str = "") -> int:
    """Block old local events from being uploaded when this PC joins cloud sync."""
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    init_sync(db_path)
    msg = (reason or "Blocked old local pending event before cloud baseline").strip()
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        rows = conn.execute("""
            SELECT event_id
            FROM cloud_sync_events
            WHERE synced_at IS NULL
              AND coalesce(trusted_upload, 0) = 0
        """).fetchall()
        ids = [str(r[0]) for r in rows if r and r[0]]
        if not ids:
            return 0
        conn.executemany(
            "UPDATE cloud_sync_events SET synced_at = ?, last_error = ? WHERE event_id = ?",
            [(_now_iso(), msg[:500], eid) for eid in ids],
        )
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def upload_pending(base_dir: Path, batch_size: int = 50) -> int:
    if _cloud_writes_paused():
        return 0
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    init_sync(db_path)
    events = _pending_events(db_path, batch_size)
    if not events:
        return 0

    ids = [e["event_id"] for e in events]
    try:
        import requests
        url = _cloud_table_url(SYNC_TABLE)
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        }
        resp = requests.post(url, params={"on_conflict": "event_id"}, headers=headers, json=events, timeout=30)
        if 200 <= resp.status_code < 300:
            _mark_synced(db_path, ids)
            _clear_cloud_write_pause()
            _LAST_STATUS["last_upload_at"] = _now_iso()
            _LAST_STATUS["last_error"] = ""
            return len(ids)
        err = f"{resp.status_code}: {resp.text[:300]}"
        if _is_cloud_capacity_error(resp.status_code, resp.text):
            _pause_cloud_writes(f"Cloud storage/plan limit reached. Local Host DB continues working; cloud upload paused. {err}")
            _mark_error(db_path, ids, _CLOUD_WRITE_PAUSED_REASON)
            return 0
        _mark_error(db_path, ids, err)
        return 0
    except Exception as e:
        _mark_error(db_path, ids, str(e))
        return 0


def enqueue_print_job(base_dir: Path, job_type: str, payload=None) -> tuple[bool, str]:
    """Send a hosted print job for the Host PC to print locally."""
    payload = payload or {}
    job_id = str(uuid.uuid4())
    try:
        import requests
        body = {
            "job_id": job_id,
            "device_id": _device_id(base_dir),
            "job_type": str(job_type or "").strip(),
            "payload": payload,
            "status": "pending",
            "created_at": _now_iso(),
        }
        headers = _supabase_headers()
        headers["Prefer"] = "return=minimal"
        resp = requests.post(_cloud_table_url(PRINT_JOB_TABLE), headers=headers, json=body, timeout=30)
        if 200 <= resp.status_code < 300:
            _WAKE_SYNC.set()
            return True, job_id
        return False, f"{resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return False, str(e)


def _mark_print_job(job_id: str, status: str, error: str = "") -> None:
    try:
        import requests
        body = {"status": status}
        if status == "processing":
            body.update({"claimed_at": _now_iso(), "claimed_by": "host"})
        elif status == "printed":
            body.update({"printed_at": _now_iso(), "last_error": None})
        elif status == "failed":
            body.update({"failed_at": _now_iso(), "last_error": str(error or "")[:500]})
        requests.patch(
            _cloud_table_url(PRINT_JOB_TABLE),
            params={"job_id": f"eq.{job_id}"},
            headers=_supabase_headers(),
            json=body,
            timeout=30,
        )
    except Exception:
        pass


def _process_print_job(job: dict) -> None:
    job_id = str(job.get("job_id") or "").strip()
    if not job_id:
        return
    _mark_print_job(job_id, "processing")
    try:
        payload = job.get("payload") or {}
        if isinstance(payload, str):
            payload = json.loads(payload or "{}")
        job_type = str(job.get("job_type") or "").strip()
        if job_type == "barcode_labels":
            labels = payload.get("labels") or []
            title = str(payload.get("title") or "Mask POS Labels")
            if not labels:
                raise ValueError("No labels in print job.")
            import backend  # loaded in the desktop app; imported lazily to avoid startup cycles
            ok = bool(backend.print_configured_barcodes(labels, title=title))
            if not ok:
                raise RuntimeError("Host barcode printer is not configured or print failed.")
            _mark_print_job(job_id, "printed")
            return
        raise ValueError(f"Unsupported print job type: {job_type}")
    except Exception as e:
        _mark_print_job(job_id, "failed", str(e))


def poll_and_process_print_jobs(base_dir: Path, limit: int = 5) -> int:
    """Host-only worker: fetch pending cloud print jobs and print them locally."""
    try:
        import requests
        resp = requests.get(
            _cloud_table_url(PRINT_JOB_TABLE),
            params={
                "select": "*",
                "status": "eq.pending",
                "order": "created_at.asc",
                "limit": int(limit or 5),
            },
            headers=_supabase_headers(),
            timeout=30,
        )
        if resp.status_code == 404:
            return 0
        resp.raise_for_status()
        rows = resp.json() or []
        count = 0
        for job in rows:
            if isinstance(job, dict):
                _process_print_job(job)
                count += 1
        return count
    except Exception:
        return 0


def _supabase_headers() -> dict:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def _cloud_table_url(table: str) -> str:
    if not cloud_configured():
        raise RuntimeError("Cloud API is not configured.")
    return f"{SUPABASE_REST_URL.rstrip('/')}/{table}"


def _product_payload_for_cloud(payload: dict) -> dict:
    barcode = str(payload.get("barcode") or payload.get("entity_id") or "").strip()
    if not barcode:
        return {}
    category = str(payload.get("category") or "").strip()
    stock_qty = max(0, int(payload.get("stock_qty") or 0))
    is_deleted = int(payload.get("is_deleted") or 0)

    out = {
        "barcode": barcode,
        "name": str(payload.get("name") or "Cloud item").strip() or "Cloud item",
        "category": category,
        "brand": str(payload.get("brand") or "").strip(),
        "location": str(payload.get("location") or "").strip(),
        "sell_price": float(payload.get("sell_price") or 0),
        "stock_qty": stock_qty,
        "low_stock_level": int(payload.get("low_stock_level") or 0),
        "is_deleted": is_deleted,
        "created_at": str(payload.get("created_at") or _now_iso()),
    }
    return out


def _cloud_location_schema_error(text: str) -> bool:
    msg = str(text or "").lower()
    return "location" in msg and any(bit in msg for bit in ("column", "schema", "not found", "unknown", "invalid"))


def _without_location(payload: dict) -> dict:
    out = dict(payload or {})
    out.pop("location", None)
    return out


def _patch_cloud_product(requests, barcode: str, payload: dict) -> bool:
    headers = _supabase_headers() | {"Prefer": "return=representation"}
    resp = requests.patch(
        _cloud_table_url(PRODUCT_TABLE),
        params={"barcode": f"eq.{str(barcode)}"},
        headers=headers,
        json=payload,
        timeout=30,
    )
    if not (200 <= resp.status_code < 300) and payload.get("location") and _cloud_location_schema_error(resp.text):
        payload = _without_location(payload)
        resp = requests.patch(
            _cloud_table_url(PRODUCT_TABLE),
            params={"barcode": f"eq.{str(barcode)}"},
            headers=headers,
            json=payload,
            timeout=30,
        )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"products patch {resp.status_code}: {resp.text[:300]}")
    try:
        return bool(resp.json() or [])
    except Exception:
        return False


def _post_cloud_product(requests, payload: dict) -> None:
    resp = requests.post(
        _cloud_table_url(PRODUCT_TABLE),
        headers=_supabase_headers() | {"Prefer": "return=minimal"},
        json=payload,
        timeout=30,
    )
    if not (200 <= resp.status_code < 300) and payload.get("location") and _cloud_location_schema_error(resp.text):
        payload = _without_location(payload)
        resp = requests.post(
            _cloud_table_url(PRODUCT_TABLE),
            headers=_supabase_headers() | {"Prefer": "return=minimal"},
            json=payload,
            timeout=30,
        )
    if 200 <= resp.status_code < 300:
        return
    if resp.status_code == 409 and payload.get("barcode"):
        _patch_cloud_product(requests, str(payload["barcode"]), payload)
        return
    raise RuntimeError(f"products insert {resp.status_code}: {resp.text[:300]}")


def _upsert_cloud_products_batch(requests, products: list[dict]) -> int:
    clean = [p for p in products if isinstance(p, dict) and p.get("barcode")]
    if not clean:
        return 0
    headers = _supabase_headers() | {"Prefer": "resolution=merge-duplicates,return=minimal"}
    resp = requests.post(
        _cloud_table_url(PRODUCT_TABLE),
        params={"on_conflict": "barcode"},
        headers=headers,
        json=clean,
        timeout=30,
    )
    if not (200 <= resp.status_code < 300) and any(p.get("location") for p in clean) and _cloud_location_schema_error(resp.text):
        clean = [_without_location(p) for p in clean]
        resp = requests.post(
            _cloud_table_url(PRODUCT_TABLE),
            params={"on_conflict": "barcode"},
            headers=headers,
            json=clean,
            timeout=30,
        )
    if 200 <= resp.status_code < 300:
        return len(clean)
    raise RuntimeError(f"products batch upsert {resp.status_code}: {resp.text[:300]}")


def mirror_product_to_cloud(event: dict) -> None:
    if str(event.get("entity_type") or "").lower().strip() != "product":
        return
    payload = event.get("payload") or {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if event.get("entity_id") and not payload.get("barcode"):
        payload["entity_id"] = event.get("entity_id")

    cloud_product = _product_payload_for_cloud(payload)
    if not cloud_product:
        return
    if str(event.get("event_type") or "").lower().strip() == "delete":
        cloud_product["is_deleted"] = 1

    import requests
    barcode = str(cloud_product["barcode"])
    if not _patch_cloud_product(requests, barcode, cloud_product):
        _post_cloud_product(requests, cloud_product)


def _cloud_products_empty() -> bool:
    import requests
    resp = requests.get(
        _cloud_table_url(PRODUCT_TABLE),
        headers=_supabase_headers(),
        params={"select": "barcode", "limit": 1},
        timeout=30,
    )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"products check {resp.status_code}: {resp.text[:300]}")
    return not bool(resp.json() or [])


def seed_cloud_products_if_empty(base_dir: Path) -> int:
    db_path = base_dir / "pos.db"
    if not db_path.exists() or not _cloud_products_empty():
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM products WHERE barcode IS NOT NULL AND barcode <> ''").fetchall()
    finally:
        conn.close()

    import requests
    products = [_product_payload_for_cloud(dict(row)) for row in rows]
    products = [p for p in products if p]
    count = 0
    device_id = _device_id(base_dir)
    for i in range(0, len(products), 100):
        batch = [
            {
                "event_id": str(uuid.uuid4()),
                "device_id": device_id,
                "event_type": "update",
                "entity_type": "product",
                "entity_id": str(p["barcode"]),
                "payload": p,
                "created_at": _now_iso(),
                "schema_version": 1,
            }
            for p in products[i:i + 100]
        ]
        try:
            resp = requests.post(
                _cloud_table_url(SYNC_TABLE),
                headers=_supabase_headers() | {"Prefer": "return=minimal"},
                json=batch,
                timeout=30,
            )
            if not (200 <= resp.status_code < 300):
                raise RuntimeError(f"seed events {resp.status_code}: {resp.text[:300]}")
            count += len(batch)
        except Exception:
            raise
    return count


def fetch_cloud_products(limit: int | None = None) -> list[dict]:
    """Fetch the complete cloud catalog, optionally capped to ``limit`` rows.

    The hosted REST API returns at most 1,000 rows per request.  A single
    request silently truncated larger catalogs, leaving valid products absent
    from newly prepared cloud-mode caches.  Reuse the paginated table reader
    so every product is downloaded in stable barcode order.
    """
    rows = _fetch_cloud_table(PRODUCT_TABLE, order="barcode.asc", page_size=1000, max_pages=100)
    if limit is None:
        return rows
    try:
        cap = max(0, int(limit))
    except (TypeError, ValueError):
        cap = 0
    return rows[:cap]


def _fetch_cloud_table(table: str, order: str = "id.asc", page_size: int = 1000, max_pages: int = 20) -> list[dict]:
    import requests
    out: list[dict] = []
    for page in range(max_pages):
        resp = requests.get(
            _cloud_table_url(table),
            headers=_supabase_headers(),
            params={
                "select": "*",
                "order": order,
                "limit": int(page_size),
                "offset": int(page * page_size),
            },
            timeout=15,
        )
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"{table} fetch {resp.status_code}: {resp.text[:300]}")
        rows = resp.json() or []
        if not isinstance(rows, list):
            return out
        out.extend([r for r in rows if isinstance(r, dict)])
        if len(rows) < page_size:
            break
    return out


def _local_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(r[1]) for r in rows if r and r[1]]


def _upsert_local_row(conn: sqlite3.Connection, table: str, row: dict, key_columns: list[str] | None = None) -> bool:
    columns = _local_columns(conn, table)
    if not columns:
        return False
    clean = {c: row.get(c) for c in columns if c in row}
    if not clean:
        return False

    keys = [k for k in (key_columns or ["id"]) if k in clean and clean.get(k) not in (None, "")]
    if not keys:
        return False

    where = " AND ".join([f"{k} = ?" for k in keys])
    key_vals = [clean[k] for k in keys]
    exists = conn.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", key_vals).fetchone()

    if exists:
        update_cols = [c for c in clean.keys() if c not in keys]
        if table == "cash_shifts":
            existing_row = conn.execute(f"SELECT * FROM {table} WHERE {where} LIMIT 1", key_vals).fetchone()
            existing_cols = _local_columns(conn, table)
            existing = dict(zip(existing_cols, existing_row)) if existing_row is not None else {}
            protected = {"closed_at", "closing_cash", "closing_usd", "closing_lbp"}
            update_cols = [
                c for c in update_cols
                if not (
                    c in protected
                    and clean.get(c) in (None, "")
                    and existing.get(c) not in (None, "")
                )
            ]
        if update_cols:
            set_sql = ", ".join([f"{c} = ?" for c in update_cols])
            vals = [clean[c] for c in update_cols] + key_vals
            conn.execute(f"UPDATE {table} SET {set_sql} WHERE {where}", vals)
        return True

    insert_cols = list(clean.keys())
    placeholders = ", ".join(["?"] * len(insert_cols))
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({', '.join(insert_cols)}) VALUES ({placeholders})",
        [clean[c] for c in insert_cols],
    )
    return True


def _normalize_cash_shift_rows(rows: list[dict]) -> list[dict]:
    """Cloud can contain stale open shifts from older builds/devices.

    Local SQLite has a partial unique index that allows only one open shift.
    If we insert cloud rows in id order while multiple rows are still open, the
    oldest stale open shift wins and the real current register is ignored.
    Keep the newest open shift open and close older stale rows for the local mirror.
    """
    clean = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    open_rows = [r for r in clean if r.get("closed_at") in (None, "")]
    if len(open_rows) <= 1:
        return clean

    def _open_sort_key(row: dict):
        return (str(row.get("opened_at") or ""), int(row.get("id") or 0))

    keep = max(open_rows, key=_open_sort_key)
    keep_id = keep.get("id")
    keep_opened = str(keep.get("opened_at") or _now_iso())
    note = "[Cloud sync repair] Closed stale open shift while mirroring cloud register state."
    for row in clean:
        if row.get("closed_at") not in (None, ""):
            continue
        if row.get("id") == keep_id:
            continue
        row["closed_at"] = keep_opened
        row["closing_cash"] = row.get("closing_cash") if row.get("closing_cash") is not None else row.get("opening_cash", 0)
        row["closing_usd"] = row.get("closing_usd") if row.get("closing_usd") is not None else row.get("opening_usd", row.get("opening_cash", 0))
        row["closing_lbp"] = row.get("closing_lbp") if row.get("closing_lbp") is not None else row.get("opening_lbp", 0)
        existing_note = str(row.get("notes") or "").strip()
        row["notes"] = (existing_note + "\n" + note).strip() if existing_note else note
    return clean


def _normalize_sales_rows(rows: list[dict]) -> tuple[list[dict], set[str]]:
    """Collapse duplicate hosted sales before mirroring them into cloud-mode SQLite.

    Old host/device migrations can leave the same local sale uploaded under two
    cloud device IDs. The UI should mirror one sale, not both duplicate cloud
    rows, so keep the newest hosted row for matching sale identity.
    """
    clean = [dict(r) for r in (rows or []) if isinstance(r, dict)]
    by_key: dict[tuple, dict] = {}
    for row in clean:
        local_id = str(row.get("cloud_local_id") or "").strip()
        if local_id:
            try:
                amt = float(row.get("total_amount") if row.get("total_amount") is not None else row.get("total_sales") or 0.0)
                amt_str = f"{amt:.2f}"
            except Exception:
                amt_str = "0.00"
            created_at_normalized = str(row.get("created_at") or "").strip()
            if len(created_at_normalized) >= 19:
                created_at_normalized = created_at_normalized[:19]
            key = (
                "local",
                local_id,
                created_at_normalized,
                str(row.get("receipt_code") or "").strip(),
                amt_str,
            )
        else:
            key = ("cloud", str(row.get("id") or ""))
        existing = by_key.get(key)
        if existing is None or int(row.get("id") or 0) >= int(existing.get("id") or 0):
            by_key[key] = row
    out = sorted(by_key.values(), key=lambda r: int(r.get("id") or 0))
    kept_ids = {str(r.get("id")) for r in out if r.get("id") not in (None, "")}
    return out, kept_ids


def download_core_tables_from_cloud(base_dir: Path) -> int:
    """Replace the local cloud-cache transaction tables with hosted state."""
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0

    tables = [
        ("employees", "id.asc", ["id"], False),
        ("cash_shifts", "id.asc", ["id"], False),
        ("cash_movements", "id.asc", ["id"], True),
        ("sales", "id.asc", ["id"], False),
        ("sale_items", "id.asc", ["id"], False),
        ("returns", "id.asc", ["id"], False),
        ("return_items", "id.asc", ["id"], False),
        ("bons", "id.asc", ["id"], True),
        ("bon_redemptions", "id.asc", ["id"], True),
    ]
    delete_order = [
        "bon_redemptions",
        "bons",
        "return_items",
        "returns",
        "sale_items",
        "sales",
        "cash_movements",
        "cash_shifts",
        "employees",
    ]

    hosted_rows = []
    fetched_tables = set()
    kept_sale_cloud_ids: set[str] | None = None
    for table, order, keys, optional in tables:
        try:
            rows = _fetch_cloud_table(table, order=order)
        except Exception:
            if optional:
                continue
            else:
                raise
        if table == "cash_shifts":
            rows = _normalize_cash_shift_rows(rows)
        elif table == "sales":
            rows, kept_sale_cloud_ids = _normalize_sales_rows(rows)
        elif table == "sale_items" and kept_sale_cloud_ids is not None:
            rows = [r for r in rows if str(r.get("sale_id") or "") in kept_sale_cloud_ids]
        hosted_rows.append((table, keys, rows))
        fetched_tables.add(table)
    total = 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in delete_order:
            if table in fetched_tables:
                conn.execute(f"DELETE FROM {table}")
        for table, keys, rows in hosted_rows:
            for row in rows:
                if _upsert_local_row(conn, table, row, keys):
                    total += 1
        conn.commit()
        _LAST_STATUS["last_download_at"] = _now_iso()
        _LAST_STATUS["last_error"] = ""
        return total
    finally:
        conn.close()


def download_all_from_cloud(base_dir: Path) -> int:
    count = download_products_from_cloud(base_dir)
    if not _AUTHORITATIVE_HOST:
        count += download_core_tables_from_cloud(base_dir)
    return count


def download_products_from_cloud(base_dir: Path) -> int:
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    count = 0
    for row in fetch_cloud_products():
        if isinstance(row, dict):
            _apply_product_event(db_path, {"event_type": "update"}, row)
            count += 1
    return count


def _fetch_remote_events(
    base_dir: Path,
    limit: int = 250,
    after_inserted_at: str = "",
    after_event_id: str = "",
) -> list[dict]:
    db_path = base_dir / "pos.db"
    init_sync(db_path)
    import requests

    url = f"{SUPABASE_REST_URL.rstrip('/')}/{SYNC_TABLE}"
    params = {
        "select": "*",
        "order": "inserted_at.asc,event_id.asc",
        "limit": int(limit or 250),
    }
    if after_inserted_at:
        if after_event_id:
            params["or"] = (
                f"(inserted_at.gt.{after_inserted_at},"
                f"and(inserted_at.eq.{after_inserted_at},event_id.gt.{after_event_id}))"
            )
        else:
            params["inserted_at"] = f"gt.{after_inserted_at}"
    resp = requests.get(url, headers=_supabase_headers(), params=params, timeout=12)
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    _LAST_STATUS["last_download_at"] = _now_iso()
    _LAST_STATUS["last_error"] = ""
    rows = resp.json() or []
    return rows if isinstance(rows, list) else []


def _latest_remote_event_cursor(base_dir: Path) -> tuple[str, str]:
    import requests
    resp = requests.get(
        f"{SUPABASE_REST_URL.rstrip('/')}/{SYNC_TABLE}",
        headers=_supabase_headers(),
        params={
            "select": "inserted_at,event_id",
            "order": "inserted_at.desc,event_id.desc",
            "limit": 1,
        },
        timeout=12,
    )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    rows = resp.json() or []
    if not isinstance(rows, list) or not rows:
        return "", ""
    row = rows[0] if isinstance(rows[0], dict) else {}
    return str(row.get("inserted_at") or ""), str(row.get("event_id") or "")


def _applied_event_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        rows = conn.execute("SELECT event_id FROM cloud_applied_events").fetchall()
        return {str(r[0]) for r in rows if r and r[0]}
    finally:
        conn.close()


def _mark_applied(db_path: Path, event: dict, note: str = "") -> None:
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("""
            INSERT OR IGNORE INTO cloud_applied_events
                (event_id, device_id, event_type, entity_type, entity_id, applied_at, apply_note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            str(event.get("event_id") or ""),
            str(event.get("device_id") or ""),
            str(event.get("event_type") or ""),
            str(event.get("entity_type") or ""),
            str(event.get("entity_id") or ""),
            _now_iso(),
            str(note or "")[:500],
        ))
        conn.commit()
    finally:
        conn.close()


def _apply_config_event(base_dir: Path, payload: dict) -> str:
    path = base_dir / "pos_config.json"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    changed = 0
    for key in ("seasonal_sale_enabled", "seasonal_sales_map", "bundle_offers_enabled", "bundle_offers_map", "spin_wheel_prizes"):
        if key in payload:
            cfg[key] = payload.get(key)
            changed += 1
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception as e:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        return f"config write error: {e}"
    return f"updated config keys={changed}"


def _download_latest_config_from_cloud(base_dir: Path) -> str:
    """Apply the latest small config event without replaying cloud history."""
    import requests
    resp = requests.get(
        f"{SUPABASE_REST_URL.rstrip('/')}/{SYNC_TABLE}",
        headers=_supabase_headers(),
        params={
            "select": "payload",
            "entity_type": "eq.config",
            "order": "inserted_at.desc,event_id.desc",
            "limit": 1,
        },
        timeout=12,
    )
    if not (200 <= resp.status_code < 300):
        raise RuntimeError(f"{resp.status_code}: {resp.text[:300]}")
    rows = resp.json() or []
    if not isinstance(rows, list) or not rows:
        return "no cloud config"
    payload = rows[0].get("payload") if isinstance(rows[0], dict) else {}
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    return _apply_config_event(base_dir, payload if isinstance(payload, dict) else {})


def _apply_sale_delete_event(db_path: Path, payload: dict) -> str:
    sale_id = None
    try:
        sale_id = int(payload.get("sale_id") or payload.get("id") or 0)
    except Exception:
        sale_id = 0
    if not sale_id:
        sale = payload.get("sale") if isinstance(payload.get("sale"), dict) else {}
        try:
            sale_id = int(sale.get("id") or 0)
        except Exception:
            sale_id = 0
    if not sale_id:
        return "skipped sale delete without id"

    restore_stock = bool(payload.get("restore_stock", False))
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        rows = cur.execute("""
            SELECT
                si.product_id,
                MAX(0, si.qty - COALESCE(SUM(CASE WHEN COALESCE(r.is_voided, 0) = 0 THEN ri.qty ELSE 0 END), 0)) AS qty
            FROM sale_items si
            LEFT JOIN return_items ri ON ri.sale_item_id = si.id
            LEFT JOIN returns r ON r.id = ri.return_id
            WHERE si.sale_id = ?
            GROUP BY si.id, si.product_id, si.qty
        """, (sale_id,)).fetchall()
        if restore_stock:
            for r in rows:
                pid = r["product_id"]
                if pid is None:
                    continue
                cur.execute(
                    "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                    (int(r["qty"] or 0), int(pid)),
                )
        cur.execute("DELETE FROM return_items WHERE return_id IN (SELECT id FROM returns WHERE original_sale_id = ?)", (sale_id,))
        cur.execute("DELETE FROM returns WHERE original_sale_id = ?", (sale_id,))
        cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (sale_id,))
        cur.execute("DELETE FROM sales WHERE id = ?", (sale_id,))
        conn.commit()
        return "deleted sale"
    finally:
        conn.close()


def _row_by_barcode(cur, barcode: str):
    cur.execute("SELECT * FROM products WHERE barcode = ? LIMIT 1", (str(barcode or "").strip(),))
    return cur.fetchone()


def _apply_product_event(db_path: Path, event: dict, payload: dict) -> str:
    barcode = str(payload.get("barcode") or "").strip()
    if not barcode:
        return "skipped product event without barcode"

    event_type = str(event.get("event_type") or "").lower().strip()
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        row = _row_by_barcode(cur, barcode)
        if event_type == "delete":
            if row:
                cur.execute("UPDATE products SET is_deleted = 1 WHERE id = ?", (int(row["id"]),))
                conn.commit()
                return "deleted product"
            return "delete skipped missing product"

        if event_type == "adjust_stock" and row and payload.get("stock_qty") in (None, ""):
            cur.execute(
                "UPDATE products SET stock_qty = MAX(0, stock_qty + ?), is_deleted = 0 WHERE id = ?",
                (int(payload.get("delta_qty") or 0), int(row["id"])),
            )
            conn.commit()
            return "adjusted stock"

        name = str(payload.get("name") or (row["name"] if row else "Cloud item")).strip() or "Cloud item"
        category = str(payload.get("category") or (row["category"] if row else "") or "").strip()
        brand = str(payload.get("brand") or (row["brand"] if row else "") or "").strip()
        location = str(payload.get("location") if payload.get("location") is not None else (row["location"] if row and "location" in row.keys() else "")).strip()
        sell_price = float(payload.get("sell_price") if payload.get("sell_price") is not None else (row["sell_price"] if row else 0))
        stock_qty = max(0, int(payload.get("stock_qty") if payload.get("stock_qty") is not None else (row["stock_qty"] if row else 0)))
        low_stock_level = int(payload.get("low_stock_level") if payload.get("low_stock_level") is not None else (row["low_stock_level"] if row else 0))
        cost_price = max(0.0, float(payload.get("cost_price") if payload.get("cost_price") is not None else (row["cost_price"] if row and "cost_price" in row.keys() else 0)))
        supplier = str(payload.get("supplier") if payload.get("supplier") is not None else (row["supplier"] if row and "supplier" in row.keys() else "")).strip()
        is_deleted = int(payload.get("is_deleted") if payload.get("is_deleted") is not None else 0)


        if row:
            cur.execute("""
                UPDATE products
                SET name = ?, category = ?, brand = ?, sell_price = ?, stock_qty = ?,
                    low_stock_level = ?, is_deleted = ?, location = ?, cost_price = ?, supplier = ?
                WHERE id = ?
            """, (name, category, brand, sell_price, stock_qty, low_stock_level, is_deleted, location, cost_price, supplier, int(row["id"])))
            conn.commit()
            return "updated product"

        cur.execute("""
            INSERT INTO products
                (barcode, name, category, brand, location, sell_price, stock_qty, low_stock_level, cost_price, supplier, is_deleted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (barcode, name, category, brand, location, sell_price, stock_qty, low_stock_level, cost_price, supplier, is_deleted, _now_iso()))
        conn.commit()
        return "created product"
    finally:
        conn.close()


def _apply_sale_stock_event(db_path: Path, payload: dict) -> str:
    """Apply only the stock movement from a remote sale, not the full sale record."""
    lines = payload.get("cart_lines") or payload.get("items") or []
    if not isinstance(lines, list) or not lines:
        return "skipped sale without lines"

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    changed = 0
    try:
        cur = conn.cursor()
        for line in lines:
            if not isinstance(line, dict):
                continue
            if bool(line.get("is_quick", False)):
                continue
            barcode = str(line.get("barcode") or "").strip()
            qty = int(line.get("qty") or 0)
            if not barcode or qty <= 0:
                continue
            row = _row_by_barcode(cur, barcode)
            if not row:
                continue
            cur.execute("UPDATE products SET stock_qty = MAX(0, stock_qty - ?) WHERE id = ?", (qty, int(row["id"])))
            changed += 1
        conn.commit()
        return f"applied sale stock lines={changed}"
    finally:
        conn.close()


def download_and_apply(base_dir: Path, limit: int = 250, full_pull: bool = False) -> int:
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    init_sync(db_path)
    if full_pull:
        try:
            download_all_from_cloud(base_dir)
        except Exception:
            pass
    device_id = _device_id(base_dir)
    applied = _applied_event_ids(db_path)
    count = 0
    snapshot_refresh_needed = False

    page_size = max(25, min(int(limit or 250), 500))
    cursor_inserted_at, cursor_event_id = _remote_event_cursor(db_path)
    for _page in range(20):
        previous_cursor = (cursor_inserted_at, cursor_event_id)
        batch = _fetch_remote_events(
            base_dir,
            limit=page_size,
            after_inserted_at=cursor_inserted_at,
            after_event_id=cursor_event_id,
        )
        if not batch:
            break

        for event in batch:
            event_id = str(event.get("event_id") or "")
            event_inserted_at = str(event.get("inserted_at") or "")
            if event_id and event_id not in applied:
                if str(event.get("device_id") or "") == device_id:
                    _mark_applied(db_path, event, "own event")
                    applied.add(event_id)
                else:
                    payload = event.get("payload") or {}
                    if isinstance(payload, str):
                        try:
                            payload = json.loads(payload or "{}")
                        except Exception:
                            payload = {}
                    if not isinstance(payload, dict):
                        payload = {}

                    try:
                        entity_type = str(event.get("entity_type") or "").lower().strip()
                        event_type = str(event.get("event_type") or "").lower().strip()
                        if entity_type == "product":
                            note = _apply_product_event(db_path, event, payload)
                        elif entity_type in ("employee", "shift", "cash_movement", "sale", "return", "bon"):
                            # D1 applies these events before exposing them to other devices.
                            # Refresh the mirrored tables together so related foreign keys and
                            # return history stay consistent across Host and cloud-cache PCs.
                            snapshot_refresh_needed = True
                            note = "queued cloud snapshot refresh"
                        elif entity_type == "config":
                            note = _apply_config_event(base_dir, payload)
                        else:
                            note = "ignored entity"
                        _mark_applied(db_path, event, note)
                        applied.add(event_id)
                        count += 1
                    except Exception as e:
                        _mark_applied(db_path, event, f"apply error: {e}")
                        applied.add(event_id)

            if event_inserted_at:
                cursor_inserted_at = event_inserted_at
                cursor_event_id = event_id

        if (cursor_inserted_at, cursor_event_id) == previous_cursor:
            break
        _set_remote_event_cursor(db_path, cursor_inserted_at, cursor_event_id)
        if len(batch) < page_size:
            break
    if snapshot_refresh_needed:
        download_all_from_cloud(base_dir)
    return count


def sync_now(base_dir: Path) -> dict:
    global _LAST_FULL_PULL_AT
    downloaded = 0
    uploaded = 0
    seeded = 0
    products = 0
    core = 0
    try:
        db_path = base_dir / "pos.db"
        init_sync(db_path)
        uploaded = upload_pending(base_dir)
        if _AUTHORITATIVE_HOST:
            downloaded = download_and_apply(base_dir)
        else:
            baseline_cursor = _latest_remote_event_cursor(base_dir)
            products = download_products_from_cloud(base_dir)
            core = download_core_tables_from_cloud(base_dir)
            _download_latest_config_from_cloud(base_dir)
            if baseline_cursor[0]:
                _set_remote_event_cursor(db_path, baseline_cursor[0], baseline_cursor[1])
            _LAST_FULL_PULL_AT = time.time()
            downloaded = download_and_apply(base_dir)
        return status(base_dir, probe=True) | {
            "downloaded": downloaded,
            "uploaded": uploaded,
            "seeded_products": seeded,
            "cloud_products_applied": products,
            "cloud_core_applied": core,
        }
    except Exception as e:
        _LAST_STATUS["last_error"] = str(e)
        s = status(base_dir, probe=False)
        s.update({
            "downloaded": downloaded,
            "uploaded": uploaded,
            "seeded_products": seeded,
            "cloud_products_applied": products,
            "cloud_core_applied": core,
            "online": False,
            "last_error": str(e),
        })
        return s


def status(base_dir: Path, probe: bool = False) -> dict:
    db_path = base_dir / "pos.db"
    out = {
        "enabled": True,
        "online": None,
        "pending": 0,
        "applied": 0,
        "local_events": 0,
        "last_upload_at": _LAST_STATUS.get("last_upload_at", ""),
        "last_download_at": _LAST_STATUS.get("last_download_at", ""),
        "last_error": _LAST_STATUS.get("last_error", ""),
        "cloud_write_paused": bool(_CLOUD_WRITE_PAUSED_REASON),
        "cloud_write_pause_reason": _CLOUD_WRITE_PAUSED_REASON,
    }
    try:
        init_sync(db_path)
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            out["pending"] = int(conn.execute("SELECT count(*) FROM cloud_sync_events WHERE synced_at IS NULL").fetchone()[0])
            out["local_events"] = int(conn.execute("SELECT count(*) FROM cloud_sync_events").fetchone()[0])
            out["applied"] = int(conn.execute("SELECT count(*) FROM cloud_applied_events").fetchone()[0])
            out["trusted_pending"] = int(conn.execute("SELECT count(*) FROM cloud_sync_events WHERE synced_at IS NULL AND coalesce(trusted_upload,0)=1").fetchone()[0])
            out["untrusted_pending"] = int(conn.execute("SELECT count(*) FROM cloud_sync_events WHERE synced_at IS NULL AND coalesce(trusted_upload,0)=0").fetchone()[0])
        finally:
            conn.close()
    except Exception as e:
        out["last_error"] = str(e)

    if probe:
        try:
            import requests
            url = f"{SUPABASE_REST_URL.rstrip('/')}/{SYNC_TABLE}"
            resp = requests.get(
                url,
                headers=_supabase_headers(),
                params={"select": "event_id", "limit": 1},
                timeout=5,
            )
            out["online"] = bool(200 <= resp.status_code < 300)
            if not out["online"]:
                out["last_error"] = f"{resp.status_code}: {resp.text[:200]}"
            elif not out.get("last_error"):
                _LAST_STATUS["last_error"] = ""
        except Exception as e:
            out["online"] = False
            out["last_error"] = str(e)
    return out


def prepare_local_from_cloud(base_dir: Path, protect_existing_pending: bool = True) -> dict:
    """Make local SQLite match cloud before this PC is allowed to upload."""
    global _STARTUP_PROTECT_DONE, _LAST_FULL_PULL_AT
    db_path = base_dir / "pos.db"
    init_sync(db_path)
    cursor_inserted_at, cursor_event_id = _latest_remote_event_cursor(base_dir)
    pulled = download_all_from_cloud(base_dir)
    _download_latest_config_from_cloud(base_dir)
    _set_remote_event_cursor(db_path, cursor_inserted_at, cursor_event_id)
    _LAST_FULL_PULL_AT = time.time()
    quarantined = 0
    if protect_existing_pending:
        quarantined = quarantine_existing_pending(
            base_dir,
            "Blocked old local pending event before cloud baseline",
        )
    _STARTUP_PROTECT_DONE = True
    return {"pulled": pulled, "quarantined": quarantined}


def _ensure_startup_protect(base_dir: Path) -> bool:
    global _STARTUP_PROTECT_DONE
    if not _PROTECT_EXISTING_PENDING or _STARTUP_PROTECT_DONE:
        return True
    try:
        prepare_local_from_cloud(base_dir, protect_existing_pending=True)
        return True
    except Exception as e:
        _LAST_STATUS["last_error"] = f"Cloud baseline failed; upload paused: {e}"
        return False


def _sync_loop(base_dir: Path, interval_seconds: int) -> None:
    global _LAST_FULL_PULL_AT, _LAST_PRINT_POLL_AT
    while not _STOP:
        loop_delay = max(5, int(interval_seconds or 5))
        try:
            if not _ensure_startup_protect(base_dir):
                _WAKE_SYNC.wait(loop_delay)
                _WAKE_SYNC.clear()
                continue
            now = time.time()
            upload_pending(base_dir)
            if not _AUTHORITATIVE_HOST and now - float(_LAST_FULL_PULL_AT or 0.0) >= FULL_PULL_INTERVAL_SECONDS:
                try:
                    baseline_cursor = _latest_remote_event_cursor(base_dir)
                    download_all_from_cloud(base_dir)
                    _download_latest_config_from_cloud(base_dir)
                    if baseline_cursor[0]:
                        _set_remote_event_cursor(base_dir / "pos.db", baseline_cursor[0], baseline_cursor[1])
                    _LAST_FULL_PULL_AT = now
                except Exception:
                    pass
            download_and_apply(base_dir, full_pull=False)
            upload_pending(base_dir)
            if _PRINT_WORKER_ENABLED:
                now = time.time()
                if now - float(_LAST_PRINT_POLL_AT or 0.0) >= PRINT_POLL_INTERVAL_SECONDS:
                    poll_and_process_print_jobs(base_dir)
                    _LAST_PRINT_POLL_AT = now
        except Exception:
            pass
        _WAKE_SYNC.wait(loop_delay)
        _WAKE_SYNC.clear()


def backfill_unsynced_events(base_dir: Path) -> int:
    """Queue cloud sync events for any sales/shifts/movements in the local DB
    that were never queued or successfully uploaded. This is safe to call
    multiple times because backfill event IDs are deterministic.

    Useful when a fresh or replaced pos.db is placed on the host and the
    cloud_sync_events table is empty or incomplete.
    """
    db_path = base_dir / "pos.db"
    if not db_path.exists():
        return 0
    if not cloud_configured():
        return 0
    try:
        init_sync(db_path)
    except Exception:
        return 0
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        device_id = _device_id(base_dir)
        count = 0

        def _local_only_clause(alias: str, table: str) -> str:
            cols = set(_local_columns(conn, table))
            if "cloud_device_id" not in cols:
                return ""
            return f" AND ({alias}.cloud_device_id IS NULL OR {alias}.cloud_device_id = '')"

        def _has_event_clause(alias: str, entity_type: str) -> str:
            return f"""
                NOT EXISTS (
                    SELECT 1 FROM cloud_sync_events e
                    WHERE e.entity_type = '{entity_type}'
                      AND e.entity_id = CAST({alias}.id AS TEXT)
                )
            """

        def _event_id(entity_type: str, entity_id) -> str:
            return f"backfill:{device_id}:{entity_type}:{entity_id}"

        def _insert_event(event_type: str, entity_type: str, entity_id, payload: dict) -> int:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO cloud_sync_events
                    (event_id, device_id, event_type, entity_type, entity_id, payload, created_at, trusted_upload)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    _event_id(entity_type, entity_id),
                    device_id,
                    str(event_type or ""),
                    str(entity_type or ""),
                    str(entity_id),
                    json.dumps(payload or {}, ensure_ascii=False, default=str),
                    _now_iso(),
                ),
            )
            return int(cur.rowcount or 0)

        # ---- sales --------------------------------------------------------
        sale_ids = [
            r[0]
            for r in conn.execute(
                f"""
                SELECT s.id FROM sales s
                WHERE {_has_event_clause('s', 'sale')}
                {_local_only_clause('s', 'sales')}
                ORDER BY s.id
                """
            ).fetchall()
        ]
        # ---- shifts -------------------------------------------------------
        shift_ids = [
            r[0]
            for r in conn.execute(
                f"""
                SELECT cs.id FROM cash_shifts cs
                WHERE {_has_event_clause('cs', 'shift')}
                {_local_only_clause('cs', 'cash_shifts')}
                ORDER BY cs.id
                """
            ).fetchall()
        ]
        # ---- cash movements -----------------------------------------------
        movement_ids = [
            r[0]
            for r in conn.execute(
                f"""
                SELECT cm.id FROM cash_movements cm
                WHERE {_has_event_clause('cm', 'cash_movement')}
                {_local_only_clause('cm', 'cash_movements')}
                ORDER BY cm.id
                """
            ).fetchall()
        ]

        # ---- enqueue sales ---------------------------------------------------
        for sale_id in sale_ids:
            try:
                sale_row = conn.execute(
                    "SELECT * FROM sales WHERE id = ?", (sale_id,)
                ).fetchone()
                if sale_row is None:
                    continue
                item_rows = conn.execute(
                    """
                    SELECT si.*, p.barcode AS barcode
                    FROM sale_items si
                    LEFT JOIN products p ON p.id = si.product_id
                    WHERE si.sale_id = ?
                    ORDER BY si.id
                    """,
                    (sale_id,),
                ).fetchall()
                sale = dict(sale_row)
                items = [dict(i) for i in (item_rows or [])]
                payload = {
                    "sale_id": int(sale_id or 0),
                    "sale": sale,
                    "items": items,
                    "cart_lines": items,
                    "payment_method": sale.get("payment_method"),
                    "customer_name": sale.get("customer_name"),
                    "notes": sale.get("notes"),
                }
                count += _insert_event("create", "sale", sale_id, payload)
            except Exception:
                pass

        # ---- enqueue shifts --------------------------------------------------
        for shift_id in shift_ids:
            try:
                row = conn.execute(
                    "SELECT * FROM cash_shifts WHERE id = ?", (shift_id,)
                ).fetchone()
                if row is None:
                    continue
                payload = dict(row)
                payload["shift_id"] = int(shift_id or 0)
                event_type = "close" if payload.get("closed_at") not in (None, "") else "open"
                count += _insert_event(event_type, "shift", shift_id, payload)
            except Exception:
                pass

        # ---- enqueue cash movements ------------------------------------------
        for mv_id in movement_ids:
            try:
                row = conn.execute(
                    "SELECT * FROM cash_movements WHERE id = ?", (mv_id,)
                ).fetchone()
                if row is None:
                    continue
                payload = dict(row)
                payload["movement_id"] = int(mv_id or 0)
                count += _insert_event("create", "cash_movement", mv_id, payload)
            except Exception:
                pass

        conn.commit()
    finally:
        conn.close()

    _WAKE_SYNC.set()
    return count


def start_background_sync(base_dir: Path, interval_seconds: int = 5, protect_existing_pending: bool = False, host_print_worker: bool = False, authoritative_host: bool = False) -> None:
    global _SYNC_THREAD, _STOP, _PROTECT_EXISTING_PENDING, _STARTUP_PROTECT_DONE, _PRINT_WORKER_ENABLED, _AUTHORITATIVE_HOST
    _PROTECT_EXISTING_PENDING = bool(protect_existing_pending)
    _PRINT_WORKER_ENABLED = bool(host_print_worker)
    _AUTHORITATIVE_HOST = bool(authoritative_host)
    try:
        init_sync(base_dir / "pos.db")
        if _PROTECT_EXISTING_PENDING and not _STARTUP_PROTECT_DONE:
            prepare_local_from_cloud(base_dir, protect_existing_pending=True)
    except Exception:
        pass
    if _SYNC_THREAD is not None and _SYNC_THREAD.is_alive():
        return
    _STOP = False
    t = threading.Thread(target=_sync_loop, args=(base_dir, interval_seconds), daemon=True)
    _SYNC_THREAD = t
    t.start()
    # If this is the authoritative host, run a one-time backfill in the
    # background to queue any sales / shifts / movements that exist in the
    # local DB but were never uploaded (e.g. after a pos.db replacement).
    if authoritative_host and cloud_configured():
        def _do_backfill():
            try:
                time.sleep(3)   # let the sync thread settle first
                backfill_unsynced_events(base_dir)
            except Exception:
                pass
        threading.Thread(target=_do_backfill, daemon=True).start()
