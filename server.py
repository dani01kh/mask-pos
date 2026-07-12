"""
server.py - MaskPOS local network server (FastAPI)

This is the "host" process. All PCs connect to this server so they share ONE database (pos.db).

Run manually:
  python server.py --host 0.0.0.0 --port 8000

Or use Host mode inside the app (backend.py starts this server automatically).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading

# Ensure server uses a persistent working directory (next to EXE when frozen)
# Allow parent to override via MASKPOS_DATA_DIR for consistent host/shared DB.
from pathlib import Path

def data_dir() -> Path:
    env = os.environ.get("MASKPOS_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent

BASE_DIR = data_dir()
DB_PATH = BASE_DIR / "pos.db"

# Ensure dir exists and start in BASE_DIR; also export MASKPOS_DATA_DIR for child processes
try:
    os.makedirs(BASE_DIR, exist_ok=True)
    os.chdir(BASE_DIR)
    os.environ["MASKPOS_DATA_DIR"] = str(BASE_DIR)
except Exception:
    pass

from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import pos_logic as L  # single source of truth for DB schema + logic
try:
    import supabase_sync
except Exception:
    supabase_sync = None

# Force pos_logic to use the same DB file as the server (pos.db next to the EXE).
# Different builds of pos_logic may look for different knobs, so we try several safely.
try:
    os.environ["MASKPOS_DB_PATH"] = str(DB_PATH)
except Exception:
    pass

try:
    if hasattr(L, "set_db_path"):
        L.set_db_path(str(DB_PATH))
except Exception:
    pass

for _attr in ("DB_PATH", "DB_FILE", "DB_FILENAME", "DB", "DBNAME", "DB_NAME", "SQLITE_PATH"):
    try:
        if hasattr(L, _attr):
            setattr(L, _attr, str(DB_PATH))
    except Exception:
        pass


app = FastAPI(title="MaskPOS Server", version="1.0")


@app.get("/health")
def health():
    # Extra fields help JOIN PCs discover/verify the correct host.
    import socket
    return {
        "ok": True,
        "name": os.environ.get("MASKPOS_HOSTNAME") or socket.gethostname() or "Host",
        "db_path": str(DB_PATH),
    }

@app.get('/debug/base_dir')
def debug_base_dir():
    # Helps the desktop app confirm it is talking to the correct host instance.
    return {
        'base_dir': str(BASE_DIR),
        'db_path': str(DB_PATH),
        'cwd': os.getcwd(),
        'frozen': bool(getattr(sys, 'frozen', False)),
    }



# ---------------- Pydantic models ----------------

class ProductAddIn(BaseModel):
    name: str
    category: str | None = ""
    brand: str | None = ""
    location: str | None = ""
    sell_price: float = 0.0
    stock_qty: int = 0
    low_stock_level: int = 0
    barcode: str | None = None
    cost_price: float = 0.0
    supplier: str | None = ""

class ProductUpdateIn(BaseModel):
    product_id: int
    name: str
    sell_price: float
    stock_qty: int
    low_stock_level: int
    location: str | None = ""
    category: str | None = ""
    brand: str | None = ""

class StockAdjustIn(BaseModel):
    product_id: int
    delta_qty: int
    reason: str = "Stock adjustment"
    movement_type: str = "ADJUSTMENT"
    reference_type: str = ""
    reference_id: str = ""
    employee_name: str = ""

class ProductDetailsIn(BaseModel):
    product_id: int
    cost_price: float = 0.0
    supplier: str = ""

class ProductDeleteIn(BaseModel):
    product_id: int

class SaleCreateIn(BaseModel):
    cart_lines: List[dict]
    payment_method: str = "CASH"
    customer_name: str = ""
    order_discount_total: float = 0.0
    notes: str = ""

class SaleDeleteIn(BaseModel):
    sale_id: int
    restore_stock: bool = True

class SaleVoidIn(BaseModel):
    sale_id: int
    reason: str
    voided_by: str = ""
    restore_stock: bool = True

class ReturnCreateIn(BaseModel):
    original_sale_id: int
    returned_lines: List[dict]
    notes: str = ""

class ReturnVoidIn(BaseModel):
    return_id: int
    notes: str = ""

class ReturnResetSaleIn(BaseModel):
    original_sale_id: int
    notes: str = ""

class BonCreateIn(BaseModel):
    return_id: int | None = None
    amount: float | None = None
    issued_by_name: str | None = ""
    signature_text: str | None = ""
    notes: str | None = ""

class BonVoidIn(BaseModel):
    code: str
    notes: str | None = ""

class EnsureEmployeeIn(BaseModel):
    name: str
    pin: str | None = ""

class DeactivateEmployeeIn(BaseModel):
    name: str

class EmployeePinIn(BaseModel):
    name: str
    pin: str | None = ""

class OpenShiftIn(BaseModel):
    opening_cash: float = 0.0
    notes: str | None = ""
    employee_name: str | None = ""
    opening_usd: float | None = None
    opening_lbp: float = 0.0
    lbp_per_usd: float | None = None

class CloseShiftIn(BaseModel):
    shift_id: int
    closing_cash: float = 0.0
    notes: str | None = ""
    closing_usd: float | None = None
    closing_lbp: float = 0.0
    lbp_per_usd: float | None = None


class CloseShiftWithTakeoutIn(CloseShiftIn):
    takeout_usd: float = 0.0
    takeout_lbp: float = 0.0
    employee_name: str | None = ""
    takeout_reason: str | None = "End of day close cash removed"
    takeout_notes: str | None = ""


class CashMovementIn(BaseModel):
    shift_id: int
    movement_type: str = "OUT"
    amount_usd: float = 0.0
    amount_lbp: float = 0.0
    reason: str
    employee_name: str | None = ""
    notes: str | None = ""
    lbp_per_usd: float | None = None

class OffersConfigIn(BaseModel):
    seasonal_sale_enabled: bool | None = None
    seasonal_sales_map: dict | None = None
    bundle_offers_enabled: bool | None = None
    bundle_offers_map: dict | None = None
    spin_wheel_prizes: list | None = None

class SendDailyReportEmailIn(BaseModel):
    day: str
    source: str = "manual"
    force: bool = False



# ---------------- Helpers ----------------

def _as_dict_row(x):
    if x is None:
        return None
    try:
        return dict(x)
    except Exception:
        return x

def _as_list(rows):
    return [dict(r) for r in (rows or [])]


def _config_path() -> Path:
    return BASE_DIR / "pos_config.json"


def _load_config() -> dict:
    try:
        p = _config_path()
        return json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    path = _config_path()
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp_path.write_text(json.dumps(cfg or {}, indent=2), encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def _offers_config_payload() -> dict:
    cfg = _load_config()
    return {
        "seasonal_sale_enabled": bool(cfg.get("seasonal_sale_enabled", False)),
        "seasonal_sales_map": cfg.get("seasonal_sales_map") or {},
        "bundle_offers_enabled": bool(cfg.get("bundle_offers_enabled", True)),
        "bundle_offers_map": cfg.get("bundle_offers_map") or {},
        "spin_wheel_prizes": cfg.get("spin_wheel_prizes") or [],
    }


def _cloud_enqueue(event_type: str, entity_type: str, entity_id=None, payload=None) -> None:
    if supabase_sync is None:
        return
    if os.environ.get("MASKPOS_DISABLE_HOST_CLOUD_SYNC", "").strip() == "1":
        return
    try:
        if not supabase_sync.cloud_configured():
            return
    except Exception:
        return
    try:
        supabase_sync.enqueue_event(BASE_DIR, event_type, entity_type, entity_id, payload or {})
    except Exception:
        pass


def _product_snapshot(product_id=None, barcode=None):
    try:
        row = None
        if barcode:
            row = L.find_product_by_barcode(str(barcode))
        elif product_id is not None:
            for r in L.list_products(""):
                try:
                    if int(r["id"]) == int(product_id):
                        row = r
                        break
                except Exception:
                    continue
        return dict(row) if row is not None else None
    except Exception:
        return None


def _sale_payload(sale_id, cart_lines=None, **extra):
    payload = {"sale_id": int(sale_id or 0)}
    payload.update(extra)
    if cart_lines is not None:
        payload["cart_lines"] = _cart_lines_with_product_barcodes(cart_lines)
    try:
        sale, items = L.get_sale_receipt_data(sale_id)
        payload["sale"] = dict(sale) if sale is not None else None
        payload["items"] = [dict(x) for x in (items or [])]
    except Exception:
        pass
    return payload


def _cart_lines_with_product_barcodes(cart_lines):
    """Add stable product identity before a LAN Host uploads sale events."""
    out = []
    for raw in cart_lines or []:
        line = dict(raw) if isinstance(raw, dict) else {}
        if line.get("product_id") and not line.get("barcode"):
            snap = _product_snapshot(product_id=line.get("product_id")) or {}
            if snap.get("barcode"):
                line["barcode"] = str(snap["barcode"])
        out.append(line)
    return out


def _employee_snapshot(employee_id=None, name=None):
    try:
        for r in L.list_employees(active_only=False):
            d = dict(r)
            if employee_id is not None and int(d.get("id") or 0) == int(employee_id):
                return d
            if name and str(d.get("name") or "").strip().lower() == str(name).strip().lower():
                return d
    except Exception:
        pass
    return None


def _shift_snapshot(shift_id):
    try:
        for r in L.list_shifts(500):
            d = dict(r)
            if int(d.get("id") or 0) == int(shift_id):
                return d
    except Exception:
        pass
    return None


def _cash_movement_snapshot(movement_id):
    try:
        rows = L.list_cash_movements(limit=1000) or []
        for r in rows:
            d = dict(r)
            if int(d.get("id") or 0) == int(movement_id):
                return d
    except Exception:
        pass
    return None


# ---------------- Shared config / offers ----------------

@app.get("/config/offers")
def api_get_offers_config():
    return _offers_config_payload()


@app.post("/config/offers")
def api_set_offers_config(c: OffersConfigIn):
    cfg = _load_config()
    if c.seasonal_sale_enabled is not None:
        cfg["seasonal_sale_enabled"] = bool(c.seasonal_sale_enabled)
    if c.seasonal_sales_map is not None:
        cfg["seasonal_sales_map"] = c.seasonal_sales_map if isinstance(c.seasonal_sales_map, dict) else {}
    if c.bundle_offers_enabled is not None:
        cfg["bundle_offers_enabled"] = bool(c.bundle_offers_enabled)
    if c.bundle_offers_map is not None:
        cfg["bundle_offers_map"] = c.bundle_offers_map if isinstance(c.bundle_offers_map, dict) else {}
    if c.spin_wheel_prizes is not None:
        cfg["spin_wheel_prizes"] = c.spin_wheel_prizes if isinstance(c.spin_wheel_prizes, list) else []
    _save_config(cfg)
    payload = _offers_config_payload()
    _cloud_enqueue("update", "config", "pos_config", payload)
    return {"ok": True, "config": payload}


# ---------------- Products ----------------

@app.post("/products/add")
def api_add_product(p: ProductAddIn):
    barcode = L.add_product(
        p.name,
        p.category or "",
        p.brand or "",
        p.sell_price,
        p.stock_qty,
        p.low_stock_level,
        p.barcode,
        p.location or "",
        p.cost_price,
        p.supplier or "",
    )
    snap = _product_snapshot(barcode=barcode)
    _cloud_enqueue("create", "product", (snap or {}).get("barcode") or barcode, snap or {
        "barcode": barcode,
        "name": p.name,
        "category": p.category or "",
        "brand": p.brand or "",
        "location": p.location or "",
        "sell_price": p.sell_price,
        "stock_qty": p.stock_qty,
        "low_stock_level": p.low_stock_level,
        "cost_price": p.cost_price,
        "supplier": p.supplier or "",
    })
    return {"barcode": barcode}

@app.get("/products/list")
def api_list_products(query: str = ""):
    rows = L.list_products(query)
    return {"items": _as_list(rows)}


@app.get("/products/list")
def api_list_products(query: str = ""):
    rows = L.list_products(query)
    return {"items": _as_list(rows)}

@app.get("/products/by_barcode/{barcode}")
def api_find_by_barcode(barcode: str):
    row = L.find_product_by_barcode(barcode)
    return {"item": _as_dict_row(row)}

@app.get("/products/reorder-suggestions")
def api_reorder_suggestions(days: int = 30, target_days: int = 14, supplier: str = "", limit: int = 1000):
    return {"items": _as_list(L.reorder_suggestions(days, target_days, supplier, limit))}

@app.get("/products/{product_id}/sales")
def api_product_sales(product_id: int, limit: int = 200, include_voided: int = 1):
    return {"items": _as_list(L.list_product_sales(product_id, limit, bool(include_voided)))}

@app.get("/products/{product_id}/price-history")
def api_product_price_history(product_id: int, limit: int = 200):
    return {"items": _as_list(L.list_product_price_history(product_id, limit))}

@app.post("/products/update")
def api_update_product(p: ProductUpdateIn):
    L.update_product(p.product_id, p.name, p.sell_price, p.stock_qty, p.low_stock_level, p.location or "", p.category or "", p.brand or "")
    snap = _product_snapshot(product_id=p.product_id)
    _cloud_enqueue("update", "product", (snap or {}).get("barcode") or p.product_id, snap or {
        "product_id": p.product_id,
        "name": p.name,
        "sell_price": p.sell_price,
        "stock_qty": p.stock_qty,
        "low_stock_level": p.low_stock_level,
        "location": p.location or "",
        "category": p.category or "",
        "brand": p.brand or "",
    })
    return {"ok": True}

@app.post("/products/adjust_stock")
def api_adjust_stock(p: StockAdjustIn):
    ok = L.adjust_stock(
        p.product_id, p.delta_qty, p.reason, p.movement_type,
        p.reference_type, p.reference_id, p.employee_name,
    )
    if ok:
        snap = _product_snapshot(product_id=p.product_id) or {"product_id": p.product_id}
        snap["delta_qty"] = int(p.delta_qty or 0)
        _cloud_enqueue("adjust_stock", "product", snap.get("barcode") or p.product_id, snap)
    return {"ok": bool(ok)}

@app.post("/products/update_details")
def api_update_product_details(p: ProductDetailsIn):
    ok = L.update_product_details(p.product_id, p.cost_price, p.supplier)
    snap = _product_snapshot(product_id=p.product_id)
    if ok and snap:
        _cloud_enqueue("update", "product", snap.get("barcode") or p.product_id, snap)
    return {"ok": bool(ok)}

@app.get("/inventory/movements")
def api_inventory_movements(product_id: int | None = None, limit: int = 500):
    return {"items": _as_list(L.list_inventory_movements(product_id, limit))}

@app.post("/products/delete")
def api_delete_product(p: ProductDeleteIn):
    snap = _product_snapshot(product_id=p.product_id) or {"product_id": p.product_id}
    L.delete_product(p.product_id)
    snap["is_deleted"] = 1
    _cloud_enqueue("delete", "product", snap.get("barcode") or p.product_id, snap)
    return {"ok": True}


# ---------------- Sales ----------------

@app.post("/sales/create")
def api_create_sale(s: SaleCreateIn):
    try:
        sale_id = L.create_sale(
            s.cart_lines,
            s.payment_method,
            s.customer_name,
            s.order_discount_total,
            s.notes,
        )
        _cloud_enqueue("create", "sale", sale_id, _sale_payload(
            sale_id,
            cart_lines=s.cart_lines,
            payment_method=s.payment_method,
            customer_name=s.customer_name,
            order_discount_total=s.order_discount_total,
            notes=s.notes,
        ))
        return {"sale_id": int(sale_id)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/sales/search")
def api_search_sales(query: str = "", include_voided: int = 1, limit: int = 200):
    return {"items": _as_list(L.search_sales(query, bool(include_voided), limit))}

@app.get("/sales/{sale_id}/receipt")
def api_sale_receipt(sale_id: int):
    sale, items = L.get_sale_receipt_data(sale_id)
    return {"sale": sale, "items": items}

@app.get("/sales/day")
def api_sales_day(day: str, limit: int = 500, include_voided: int = 0):
    rows = L.list_sales_for_day(day, limit, bool(include_voided))
    return {"items": _as_list(rows)}

@app.get("/sales/{sale_id}/detail")
def api_sale_detail(sale_id: int):
    sale, items = L.get_sale_detail(sale_id)
    return {"sale": sale, "items": items}

@app.get("/sales/{sale_id}/detail_with_returns")
def api_sale_detail_wr(sale_id: int):
    sale, items = L.get_sale_detail_with_returns(sale_id)
    return {"sale": sale, "items": items}

@app.get("/sales/by_receipt_scan")
def api_by_receipt_scan(scan: str):
    sale, items = L.get_sale_by_receipt_scan(scan)
    return {"sale": sale, "items": items}

@app.post("/sales/delete")
def api_delete_sale(d: SaleDeleteIn):
    sale_snapshot = None
    item_snapshot = []
    try:
        sale_snapshot, item_snapshot = L.get_sale_detail(d.sale_id)
        sale_snapshot = dict(sale_snapshot) if sale_snapshot is not None else None
        item_snapshot = [dict(x) for x in (item_snapshot or [])]
    except Exception:
        pass
    ok = L.delete_sale(d.sale_id, d.restore_stock)
    if ok:
        _cloud_enqueue("delete", "sale", d.sale_id, {
            "sale_id": int(d.sale_id or 0),
            "restore_stock": bool(d.restore_stock),
            "sale": sale_snapshot,
            "items": item_snapshot,
        })
    return {"ok": bool(ok)}

@app.post("/sales/void")
def api_void_sale(d: SaleVoidIn):
    try:
        ok = L.void_sale(d.sale_id, d.reason, d.voided_by, d.restore_stock)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if ok:
        sale, items = L.get_sale_detail(d.sale_id)
        _cloud_enqueue("void", "sale", d.sale_id, {
            "sale_id": int(d.sale_id), "reason": d.reason,
            "voided_by": d.voided_by, "restore_stock": bool(d.restore_stock),
            "sale": dict(sale) if sale else None,
            "items": [dict(x) for x in (items or [])],
        })
    return {"ok": bool(ok)}


# ---------------- Returns ----------------

@app.post("/returns/create")
def api_create_return(r: ReturnCreateIn):
    try:
        rid, total = L.create_return(r.original_sale_id, r.returned_lines, r.notes or "")
        validated_lines = r.returned_lines
        try:
            _return, validated_lines = L.get_return_detail(rid)
        except Exception:
            pass
        _cloud_enqueue("create", "return", rid, {
            "return_id": int(rid or 0),
            "original_sale_id": int(r.original_sale_id or 0),
            "returned_lines": validated_lines,
            "notes": r.notes or "",
            "expected_total": float(total or 0.0),
        })
        return {"return_id": int(rid), "total_return_amount": float(total)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/sales/{sale_id}/returns")
def api_sale_returns(sale_id: int, include_voided: int = 0):
    return {"items": _as_list(L.list_returns_for_sale(sale_id, bool(include_voided)))}

@app.get("/returns/recent")
def api_recent_returns(limit: int = 20, include_voided: int = 0):
    return {"items": _as_list(L.list_recent_returns(limit, bool(include_voided)))}

@app.post("/returns/void")
def api_void_return(r: ReturnVoidIn):
    try:
        out = L.void_return(r.return_id, r.notes or "")
        _cloud_enqueue("void", "return", r.return_id, {
            "return_id": int(r.return_id or 0),
            "notes": r.notes or "",
        })
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/returns/reset_for_sale")
def api_reset_returns_for_sale(r: ReturnResetSaleIn):
    try:
        out = L.reset_returns_for_sale(r.original_sale_id, r.notes or "")
        _cloud_enqueue("reset_for_sale", "return", r.original_sale_id, {
            "original_sale_id": int(r.original_sale_id or 0),
            "notes": r.notes or "",
        })
        return out
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- Bons ----------------

@app.post("/bons/create")
def api_create_bon(b: BonCreateIn):
    try:
        bon = L.create_bon(
            b.return_id,
            b.issued_by_name or "",
            b.signature_text or "",
            b.notes or "",
            b.amount,
        )
        _cloud_enqueue("create", "bon", (bon or {}).get("code") or b.return_id, bon or {
            "return_id": int(b.return_id) if b.return_id not in (None, "") else None,
            "amount": b.amount,
            "issued_by_name": b.issued_by_name or "",
            "signature_text": b.signature_text or "",
            "notes": b.notes or "",
        })
        return {"bon": _as_dict_row(bon)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/bons/by_code")
def api_bon_by_code(code: str):
    return {"bon": _as_dict_row(L.get_bon_by_code(code))}

@app.get("/bons/list")
def api_bons_list(query: str = "", active_only: int = 0, limit: int = 200):
    return {"items": _as_list(L.list_bons(query, bool(active_only), limit))}

@app.post("/bons/void")
def api_void_bon(b: BonVoidIn):
    try:
        bon = L.void_bon(b.code, b.notes or "")
        _cloud_enqueue("void", "bon", (bon or {}).get("code") or b.code, {
            "code": (bon or {}).get("code") or b.code,
            "notes": b.notes or "",
            "bon": bon,
        })
        return {"bon": _as_dict_row(bon)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- Employees ----------------

@app.get("/employees/list")
def api_list_employees(active_only: int = 1):
    rows = L.list_employees(bool(active_only))
    return {"items": _as_list(rows)}

@app.post("/employees/ensure")
def api_ensure_employee(e: EnsureEmployeeIn):
    emp_id = L.ensure_employee(e.name, e.pin or "")
    _cloud_enqueue("upsert", "employee", emp_id, _employee_snapshot(employee_id=emp_id) or {
        "id": int(emp_id or 0),
        "name": e.name,
        "pin": e.pin or "",
        "is_active": 1,
    })
    return {"employee_id": int(emp_id)}

@app.get("/employees/has_pin")
def api_employee_has_pin(name: str):
    conn = L.get_conn()
    try:
        row = conn.execute(
            "SELECT pin FROM employees WHERE name = ? LIMIT 1",
            (str(name or "").strip(),),
        ).fetchone()
        return {"has_pin": bool(row and str(row["pin"] or "").strip())}
    finally:
        conn.close()

@app.post("/employees/verify_pin")
def api_verify_employee_pin(e: EmployeePinIn):
    conn = L.get_conn()
    try:
        row = conn.execute(
            "SELECT pin FROM employees WHERE name = ? LIMIT 1",
            (str(e.name or "").strip(),),
        ).fetchone()
        if not row:
            return {"ok": False}
        stored = str(row["pin"] or "").strip()
        return {"ok": (not stored) or str(e.pin or "").strip() == stored}
    finally:
        conn.close()

@app.post("/employees/deactivate")
def api_deactivate_employee(d: DeactivateEmployeeIn):
    ok = L.deactivate_employee(d.name)
    if ok:
        snap = _employee_snapshot(name=d.name) or {"name": d.name, "is_active": 0}
        snap["is_active"] = 0
        _cloud_enqueue("deactivate", "employee", d.name, snap)
    return {"ok": bool(ok)}


# ---------------- Shifts ----------------

@app.get("/shifts/open")
def api_get_open_shift():
    s = L.get_open_shift()
    return {"shift": _as_dict_row(s)}

@app.get("/shifts/last_closed")
def api_get_last_closed_shift():
    s = L.get_last_closed_shift()
    return {"shift": _as_dict_row(s)}

@app.post("/shifts/open")
def api_open_shift(s: OpenShiftIn):
    try:
        cfg = _load_config()
        next_num = int(cfg.get("next_shift_number", 1) or 1)
        shift_code = str(next_num)

        sid = L.open_shift(s.opening_cash, s.notes or "", s.employee_name or "", s.opening_usd, s.opening_lbp, s.lbp_per_usd, shift_code=shift_code)

        cfg["next_shift_number"] = next_num + 1
        _save_config(cfg)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    _cloud_enqueue("open", "shift", sid, _shift_snapshot(sid) or {
        "id": int(sid or 0),
        "opening_cash": s.opening_cash,
        "notes": s.notes or "",
        "employee_name": s.employee_name or "",
    })
    import threading
    import backend
    threading.Thread(target=backend.send_shift_open_email, args=(sid,), daemon=True).start()
    return {"shift_id": int(sid)}


@app.post("/config/reset_shift_number")
def api_reset_shift_number():
    try:
        cfg = _load_config()
        cfg["next_shift_number"] = 1
        _save_config(cfg)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/shifts/close")
def api_close_shift(s: CloseShiftIn):
    try:
        ok = L.close_shift(s.shift_id, s.closing_cash, s.notes or "", s.closing_usd, s.closing_lbp, s.lbp_per_usd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if ok:
        _cloud_enqueue("close", "shift", s.shift_id, _shift_snapshot(s.shift_id) or {
            "id": int(s.shift_id or 0),
            "closing_cash": s.closing_cash,
            "notes": s.notes or "",
        })
    return {"ok": bool(ok)}


@app.post("/shifts/close_with_takeout")
def api_close_shift_with_takeout(s: CloseShiftWithTakeoutIn):
    try:
        result = L.close_shift_with_cash_takeout(
            s.shift_id,
            s.closing_cash,
            s.notes or "",
            s.closing_usd,
            s.closing_lbp,
            s.lbp_per_usd,
            s.takeout_usd,
            s.takeout_lbp,
            s.employee_name or "",
            s.takeout_reason or "End of day close cash removed",
            s.takeout_notes or "",
        ) or {}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    movement_id = result.get("movement_id")
    if movement_id:
        _cloud_enqueue("create", "cash_movement", movement_id, _cash_movement_snapshot(movement_id) or {
            "id": int(movement_id or 0),
            "movement_id": int(movement_id or 0),
            "shift_id": int(s.shift_id or 0),
            "movement_type": "OUT",
            "amount_usd": s.takeout_usd,
            "amount_lbp": s.takeout_lbp,
            "reason": s.takeout_reason or "End of day close cash removed",
            "employee_name": s.employee_name or "",
            "notes": s.takeout_notes or "",
            "lbp_per_usd": s.lbp_per_usd,
        })
    _cloud_enqueue("close", "shift", s.shift_id, _shift_snapshot(s.shift_id) or {
        "id": int(s.shift_id or 0),
        "shift_id": int(s.shift_id or 0),
        "closing_cash": s.closing_cash,
        "notes": s.notes or "",
        "closing_usd": s.closing_usd,
        "closing_lbp": s.closing_lbp,
        "lbp_per_usd": s.lbp_per_usd,
    })
    return {"ok": True, "movement_id": movement_id}

@app.get("/shifts/list")
def api_list_shifts(limit: int = 60):
    rows = L.list_shifts(limit)
    return {"items": _as_list(rows)}

@app.get("/shifts/summary")
def api_shift_summary(shift_id: int):
    summ = L.shift_summary(shift_id)
    return {"summary": summ}


@app.post("/cash_movements/record")
def api_record_cash_movement(m: CashMovementIn):
    try:
        movement_id = L.record_cash_movement(
            m.shift_id,
            m.movement_type,
            m.amount_usd,
            m.amount_lbp,
            m.reason,
            m.employee_name or "",
            m.notes or "",
            m.lbp_per_usd,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    _cloud_enqueue("create", "cash_movement", movement_id, _cash_movement_snapshot(movement_id) or {
        "id": int(movement_id or 0),
        "movement_id": int(movement_id or 0),
        "shift_id": int(m.shift_id or 0),
        "movement_type": m.movement_type,
        "amount_usd": m.amount_usd,
        "amount_lbp": m.amount_lbp,
        "reason": m.reason,
        "employee_name": m.employee_name or "",
        "notes": m.notes or "",
        "lbp_per_usd": m.lbp_per_usd,
    })
    return {"movement_id": int(movement_id)}


@app.get("/cash_movements/list")
def api_list_cash_movements(shift_id: int | None = None, day_str: str = "", limit: int = 500):
    rows = L.list_cash_movements(shift_id=shift_id, day_str=day_str or None, limit=limit)
    return {"items": _as_list(rows)}


# ---------------- Diagnostics ----------------

@app.get("/debug/db_test")
def api_db_test():
    if os.environ.get("MASKPOS_ENABLE_DB_TEST", "").strip() != "1":
        raise HTTPException(status_code=404, detail="Not found")
    return L.db_self_test()


# ---------------- Analytics ----------------

@app.get("/analytics/kpis")
def api_kpis(start: str, end: str):
    return {"kpis": L.analytics_kpis_range(start, end)}

@app.get("/analytics/breakdown")
def api_breakdown(start: str, end: str):
    return {"breakdown": L.analytics_breakdown_range(start, end)}

@app.get("/analytics/series")
def api_series(start: str, end: str, group: str = "day"):
    return {"items": L.analytics_series_in_range(start, end, group)}

@app.get("/analytics/top_products")
def api_top_products(start: str, end: str, limit: int = 12):
    rows = L.analytics_top_products_range(start, end, limit)
    return {"items": _as_list(rows)}

@app.get("/analytics/low_stock")
def api_low_stock(limit: int = 50):
    rows = L.analytics_low_stock(limit)
    return {"items": _as_list(rows)}

@app.get("/analytics/data_health")
def api_data_health(sample_limit: int = 8):
    return {"health": L.data_health_summary(sample_limit)}

@app.get("/analytics/discount-impact")
def api_discount_impact(start_date: str, end_date: str, limit: int = 100):
    return L.analytics_discount_impact(start_date, end_date, limit)

@app.post("/analytics/send_daily_report_email")
def api_send_daily_report_email(p: SendDailyReportEmailIn):
    day = p.day
    source = p.source
    force = p.force

    import backend
    import daily_report
    from datetime import datetime

    day = str(day or "").strip() or datetime.now().strftime("%Y-%m-%d")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        return {"ok": False, "message": "Invalid report date."}

    cfg = backend.get_daily_report_email_config()
    if not bool(cfg.get("enabled", True)):
        return {"ok": False, "message": "Daily report email is disabled in Settings."}

    if (not force) and source == "schedule" and str(cfg.get("last_auto_sent_date") or "") == day:
        return {"ok": True, "message": "Already sent today."}
    if (not force) and source not in ("close", "schedule") and str(cfg.get("last_sent_date") or "") == day:
        return {"ok": True, "message": f"The daily report for {day} was already emailed."}

    missing = []
    if not str(cfg.get("sender_email") or "").strip():
        missing.append("sender email")
    if not str(cfg.get("smtp_username") or "").strip():
        missing.append("SMTP username")
    if not str(cfg.get("smtp_password") or "").strip():
        missing.append("SMTP app password")
    if missing:
        return {"ok": False, "message": "Email settings are incomplete on the host: " + ", ".join(missing)}

    try:
        d = datetime.strptime(day, "%Y-%m-%d")
        reports_folder = os.path.join(str(backend.BASE_DIR), "reports")
        result = daily_report.build_sales_report_excel(str(backend.BASE_DIR / "pos.db"), reports_folder, d.year, d.month, str(d.day))
        drawer_pdf = daily_report.build_cash_drawer_pdf(str(backend.BASE_DIR / "pos.db"), reports_folder, d.year, d.month, str(d.day))
    except Exception as ex:
        return {"ok": False, "message": f"Could not generate the daily reports on host: {ex}"}

    try:
        summary = result.get("summary", {}) or {}
        drawer_summary = drawer_pdf.get("summary", {}) or {}
        
        def _money(x):
            try:
                return f"${float(x):.2f}"
            except Exception:
                return "$0.00"

        drawer_net = float(summary.get("drawer_net_change", 0.0) or 0.0)
        subject = f"Mask POS Daily Sales Report - {day}"
        body = (
            f"Mask POS Daily Report - {day}\n\n"
            f"Total sales / new money: {_money(summary.get('merchandise_sales', 0.0))}\n"
            f"Cash collected: {_money(summary.get('cash_collected', 0.0))}\n"
            f"Returns / refunds: {_money(summary.get('returns', 0.0))} total, {_money(summary.get('cash_refunds', 0.0))} cash\n"
            f"Drawer movement: +{_money(summary.get('cash_added', 0.0))} in, -{_money(summary.get('cash_removed', 0.0))} out\n"
            f"Drawer net cash change: {_money(drawer_net)}\n"
            f"Orders: {int(summary.get('orders', 0) or 0)}\n\n"
            "Attached: Excel details + PDF with sales list and drawer summary."
        )

        attachments = [result.get("path", ""), drawer_pdf.get("path", "")]
        ok, msg = backend.send_daily_report_email(subject, body, attachments, cfg.get("recipients"))
        if ok:
            try:
                backend.mark_daily_report_email_sent(day, source=source)
            except Exception:
                pass
            return {"ok": True, "message": msg}
        return {"ok": False, "message": msg}
    except Exception as exc:
        return {"ok": False, "message": f"SMTP sending failed on host: {exc}"}


class BulkEditIn(BaseModel):
    product_ids: list[int]
    category: str | None = None
    location: str | None = None
    low_stock: int | None = None
    brand: str | None = None

class RepairLinksIn(BaseModel):
    sale_item_ids: list[int]
    target_product_id: int

class RecreateRepairIn(BaseModel):
    name: str
    barcode: str
    sell_price: float
    cost_price: float
    supplier: str | None = ""
    category: str | None = ""
    brand: str | None = ""
    location: str | None = ""
    sale_item_ids: list[int]

@app.get("/products/categories")
def api_get_distinct_categories():
    return {"items": L.get_distinct_categories()}

@app.get("/health/stats")
def api_get_data_health_stats():
    return {"stats": L.get_data_health_stats()}

@app.get("/health/issues")
def api_list_health_issues(type: str):
    return {"items": _as_list(L.list_health_issues(type))}

@app.post("/products/bulk_edit")
def api_bulk_edit_products(p: BulkEditIn):
    ok = L.bulk_update_products(p.product_ids, p.category, p.location, p.low_stock, p.brand)
    if ok:
        for pid in p.product_ids:
            snap = _product_snapshot(product_id=pid)
            if snap:
                _cloud_enqueue("update", "product", snap.get("barcode") or pid, snap)
    return {"ok": ok}

@app.post("/sales/repair_links")
def api_repair_broken_links(p: RepairLinksIn):
    ok = L.repair_broken_product_links(p.sale_item_ids, p.target_product_id)
    return {"ok": ok}

@app.post("/sales/recreate_repair")
def api_recreate_repair(p: RecreateRepairIn):
    ok = L.recreate_and_repair_product(
        p.name, p.barcode, p.sell_price, p.cost_price, p.supplier or "",
        p.category or "", p.brand or "", p.location or "", p.sale_item_ids
    )
    if ok:
        snap = _product_snapshot(barcode=p.barcode)
        if snap:
            _cloud_enqueue("create", "product", p.barcode, snap)
    return {"ok": ok}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    import uvicorn
    # run the app object directly so filename never matters
    uvicorn.run(app, host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
