"""Optional inventory intelligence and reversible action history for Mask POS."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path


LIFECYCLE_TYPES = {"", "CORE", "SEASONAL", "ONE_TIME"}


def _connect(db_path: str | Path):
    conn = sqlite3.connect(str(db_path), timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(db_path: str | Path) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS inventory_product_settings (
                barcode TEXT PRIMARY KEY,
                lifecycle_type TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS inventory_saved_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                filter_key TEXT NOT NULL DEFAULT 'ALL',
                created_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS inventory_action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action_type TEXT NOT NULL,
                product_id INTEGER,
                barcode TEXT NOT NULL DEFAULT '',
                product_name TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                actor TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'APPLIED',
                reversed_at TEXT,
                reversal_id INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_inventory_action_created
                ON inventory_action_history(created_at DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_inventory_action_barcode
                ON inventory_action_history(barcode, created_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()


def set_lifecycle(db_path: str | Path, barcode: str, lifecycle_type: str) -> None:
    ensure_schema(db_path)
    bc = str(barcode or "").strip()
    kind = str(lifecycle_type or "").strip().upper().replace("-", "_").replace(" ", "_")
    if kind not in LIFECYCLE_TYPES:
        raise ValueError("Lifecycle must be blank, Core, Seasonal, or One-Time.")
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO inventory_product_settings(barcode, lifecycle_type, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(barcode) DO UPDATE SET lifecycle_type=excluded.lifecycle_type,
                                                  updated_at=excluded.updated_at""",
            (bc, kind, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def get_lifecycle(db_path: str | Path, barcode: str) -> str:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT lifecycle_type FROM inventory_product_settings WHERE barcode=?", (str(barcode or "").strip(),)
        ).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        conn.close()


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def inventory_metrics(db_path: str | Path, days: int = 60, coverage_days: int = 14) -> list[dict]:
    """Return explainable recommendation metrics; never changes inventory."""
    ensure_schema(db_path)
    days = max(7, min(int(days or 60), 365))
    coverage_days = max(1, min(int(coverage_days or 14), 90))
    today = date.today()
    start = today - timedelta(days=days - 1)
    conn = _connect(db_path)
    try:
        products = [dict(r) for r in conn.execute(
            """SELECT p.*, COALESCE(s.lifecycle_type, '') AS lifecycle_type
               FROM products p
               LEFT JOIN inventory_product_settings s ON s.barcode=p.barcode
               WHERE COALESCE(p.is_deleted,0)=0
               ORDER BY p.name COLLATE NOCASE"""
        ).fetchall()]
        sales_rows = conn.execute(
            """SELECT si.product_id, date(s.created_at) AS sale_day,
                      SUM(CASE WHEN COALESCE(s.is_voided,0)=0 THEN si.qty ELSE 0 END) AS qty,
                      SUM(CASE WHEN COALESCE(s.is_voided,0)=0 THEN si.line_total ELSE 0 END) AS revenue,
                      MAX(CASE WHEN COALESCE(s.is_voided,0)=0 THEN s.created_at END) AS last_sale
               FROM sale_items si JOIN sales s ON s.id=si.sale_id
               WHERE date(s.created_at)>=date(?)
               GROUP BY si.product_id, date(s.created_at)""",
            (start.isoformat(),),
        ).fetchall()
        daily_by_product: dict[int, dict[str, dict]] = {}
        for row in sales_rows:
            pid = int(row["product_id"] or 0)
            if not pid:
                continue
            daily_by_product.setdefault(pid, {})[str(row["sale_day"])] = {
                "qty": float(row["qty"] or 0), "revenue": float(row["revenue"] or 0),
                "last_sale": str(row["last_sale"] or ""),
            }
        returns_by_product = {}
        if _table_exists(conn, "return_items") and _table_exists(conn, "returns"):
            for row in conn.execute(
                """SELECT ri.product_id, COALESCE(SUM(ri.qty),0) AS qty
                   FROM return_items ri JOIN returns r ON r.id=ri.return_id
                   WHERE date(r.created_at)>=date(?) AND COALESCE(r.is_voided,0)=0
                   GROUP BY ri.product_id""", (start.isoformat(),)
            ).fetchall():
                returns_by_product[int(row["product_id"] or 0)] = float(row["qty"] or 0)
    finally:
        conn.close()

    preliminary = []
    for product in products:
        pid = int(product.get("id") or 0)
        by_day = daily_by_product.get(pid, {})
        quantities = []
        cursor = start
        while cursor <= today:
            quantities.append(float((by_day.get(cursor.isoformat()) or {}).get("qty") or 0))
            cursor += timedelta(days=1)
        gross_sold = sum(quantities)
        returned = returns_by_product.get(pid, 0.0)
        net_sold = max(0.0, gross_sold - returned)
        avg_daily = net_sold / float(days)
        demand_std = statistics.pstdev(quantities) if len(quantities) > 1 else 0.0
        demand_buffer = demand_std * math.sqrt(coverage_days)
        recommended_floor = int(math.ceil(avg_daily * coverage_days + demand_buffer))
        stock = int(product.get("stock_qty") or 0)
        low_level = int(product.get("low_stock_level") or 0)
        revenue = sum(float(v.get("revenue") or 0) for v in by_day.values())
        last_sale_text = max((str(v.get("last_sale") or "") for v in by_day.values()), default="")
        try:
            last_day = datetime.fromisoformat(last_sale_text.replace("Z", "+00:00")).date() if last_sale_text else None
        except Exception:
            last_day = None
        days_since = (today - last_day).days if last_day else None
        available = max(0, stock)
        days_cover = (available / avg_daily) if avg_daily > 0 else None
        sell_through = (net_sold / (net_sold + available) * 100.0) if (net_sold + available) > 0 else 0.0
        cost = float(product.get("cost_price") or 0.0)
        price = float(product.get("sell_price") or 0.0)
        margin_pct = ((price - cost) / price * 100.0) if price > 0 and cost > 0 else None
        preliminary.append({
            **product,
            "analysis_days": days, "coverage_days": coverage_days,
            "units_sold": round(net_sold, 2), "returned_units": round(returned, 2),
            "avg_daily_units": round(avg_daily, 3), "demand_std": round(demand_std, 3),
            "demand_buffer": round(demand_buffer, 2), "recommended_floor": recommended_floor,
            "suggested_qty": (0 if str(product.get("lifecycle_type") or "") == "ONE_TIME" else max(0, recommended_floor - stock)),
            "days_cover": round(days_cover, 1) if days_cover is not None else None,
            "sell_through_pct": round(sell_through, 1), "revenue": round(revenue, 2),
            "last_sale": last_sale_text, "days_since_sale": days_since, "margin_pct": round(margin_pct, 1) if margin_pct is not None else None,
        })

    max_velocity = max((r["avg_daily_units"] for r in preliminary), default=0.0) or 1.0
    max_revenue = max((r["revenue"] for r in preliminary), default=0.0) or 1.0
    for rec in preliminary:
        days_since = rec["days_since_sale"]
        activity = 25.0 if days_since is not None and days_since <= 7 else 18.0 if days_since is not None and days_since <= 30 else 8.0 if days_since is not None and days_since <= 60 else 0.0
        sell = min(25.0, rec["sell_through_pct"] / 4.0)
        velocity = min(25.0, (rec["avg_daily_units"] / max_velocity) * 25.0)
        revenue = min(15.0, (rec["revenue"] / max_revenue) * 15.0)
        margin = 5.0 if rec["margin_pct"] is None else min(10.0, max(0.0, rec["margin_pct"] / 5.0))
        score = int(round(activity + sell + velocity + revenue + margin))
        stock = int(rec.get("stock_qty") or 0)
        floor = max(int(rec.get("low_stock_level") or 0), int(rec["recommended_floor"] or 0))
        lifecycle = str(rec.get("lifecycle_type") or "")
        flags = []
        if stock < 0:
            flags.append("NEGATIVE")
        if stock == 0:
            flags.append("OUT")
        if stock > 0 and stock <= floor:
            flags.append("LOW")
        if stock > floor and rec["days_cover"] is not None and rec["days_cover"] <= rec["coverage_days"] * 1.5:
            flags.append("REORDER_SOON")
        if stock > 0 and ((rec["days_cover"] is not None and rec["days_cover"] > rec["coverage_days"] * 4) or (rec["avg_daily_units"] == 0)):
            flags.append("OVERSTOCK")
        if stock > 0 and (score < 40 or days_since is None or days_since >= 60):
            flags.append("LOW_PERFORMING")
        if days_since is None or days_since >= 90:
            flags.append("NO_SALE_90")
        elif days_since >= 60:
            flags.append("NO_SALE_60")
        elif days_since >= 30:
            flags.append("NO_SALE_30")
        rec["performance_score"] = max(0, min(100, score))
        rec["score_components"] = {
            "recent_activity": round(activity, 1), "sell_through": round(sell, 1),
            "sales_velocity": round(velocity, 1), "revenue": round(revenue, 1),
            "margin": round(margin, 1),
        }
        rec["flags"] = flags
        rec["reorder_recommended"] = bool(rec["suggested_qty"] > 0 and lifecycle != "ONE_TIME")
        reasons = []
        if "OUT" in flags:
            reasons.append("stock is zero")
        if "NEGATIVE" in flags:
            reasons.append("negative stock needs review")
        if "LOW" in flags:
            reasons.append(f"stock is at/below floor {floor}")
        if "LOW_PERFORMING" in flags:
            reasons.append(f"performance score {rec['performance_score']}/100")
        if days_since is None:
            reasons.append(f"no sale found in {days} days")
        elif days_since >= 30:
            reasons.append(f"last sale {days_since} days ago")
        if lifecycle == "ONE_TIME":
            reasons.append("one-time item: reorder excluded")
        rec["explanation"] = "; ".join(reasons) or "No immediate inventory concern."
    return preliminary


def save_view(db_path: str | Path, name: str, filter_key: str) -> None:
    ensure_schema(db_path)
    clean = str(name or "").strip()
    if not clean:
        raise ValueError("View name is required.")
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO inventory_saved_views(name, filter_key, created_at) VALUES (?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET filter_key=excluded.filter_key""",
            (clean, str(filter_key or "ALL"), datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def list_views(db_path: str | Path) -> list[dict]:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM inventory_saved_views ORDER BY name COLLATE NOCASE").fetchall()]
    finally:
        conn.close()


def delete_view(db_path: str | Path, name: str) -> None:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute("DELETE FROM inventory_saved_views WHERE name=?", (str(name or "").strip(),))
        conn.commit()
    finally:
        conn.close()


def record_action(db_path: str | Path, *, action_type: str, product_id=None, barcode: str = "",
                  product_name: str = "", description: str = "", before=None, after=None,
                  actor: str = "") -> int:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """INSERT INTO inventory_action_history
               (created_at, action_type, product_id, barcode, product_name, description,
                before_json, after_json, actor, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'APPLIED')""",
            (datetime.now().astimezone().isoformat(timespec="seconds"), str(action_type or ""),
             int(product_id) if product_id not in (None, "") else None, str(barcode or ""),
             str(product_name or ""), str(description or ""),
             json.dumps(before or {}, ensure_ascii=False, default=str),
             json.dumps(after or {}, ensure_ascii=False, default=str), str(actor or "")),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_actions(db_path: str | Path, barcode: str = "", limit: int = 500) -> list[dict]:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        params = []
        where = ""
        if str(barcode or "").strip():
            where = "WHERE barcode=?"
            params.append(str(barcode).strip())
        params.append(max(1, min(int(limit or 500), 2000)))
        rows = conn.execute(
            f"SELECT * FROM inventory_action_history {where} ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        out = []
        for row in rows:
            rec = dict(row)
            for key in ("before_json", "after_json"):
                try:
                    rec[key[:-5]] = json.loads(rec.get(key) or "{}")
                except Exception:
                    rec[key[:-5]] = {}
            out.append(rec)
        return out
    finally:
        conn.close()


def mark_reversed(db_path: str | Path, action_id: int, reversal_id: int) -> None:
    ensure_schema(db_path)
    conn = _connect(db_path)
    try:
        conn.execute(
            "UPDATE inventory_action_history SET status='REVERSED', reversed_at=?, reversal_id=? WHERE id=? AND status='APPLIED'",
            (datetime.now().astimezone().isoformat(timespec="seconds"), int(reversal_id), int(action_id)),
        )
        conn.commit()
    finally:
        conn.close()
