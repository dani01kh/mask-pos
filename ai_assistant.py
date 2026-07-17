"""Read-only Gemini assistant for Mask POS.

The assistant receives compact business summaries, never the database file. It has
no mutation tools and cannot change products, stock, offers, sales, or settings.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import requests


GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ALLOWED_MODELS = {"gemini-3.5-flash", "gemini-3.1-flash-lite"}

ACTION_DECLARATIONS = [
    {"name": "create_product", "description": "Create a new Mask POS product.", "parameters": {"type": "OBJECT", "properties": {
        "name": {"type": "STRING"}, "barcode": {"type": "STRING"}, "sell_price": {"type": "NUMBER"},
        "stock_qty": {"type": "NUMBER"}, "low_stock_level": {"type": "NUMBER"}, "category": {"type": "STRING"},
        "brand": {"type": "STRING"}, "location": {"type": "STRING"}, "cost_price": {"type": "NUMBER"}, "supplier": {"type": "STRING"}},
        "required": ["name", "sell_price"]}},
    {"name": "update_product", "description": "Update an existing product identified by barcode.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "name": {"type": "STRING"}, "sell_price": {"type": "NUMBER"},
        "stock_qty": {"type": "NUMBER"}, "low_stock_level": {"type": "NUMBER"}, "category": {"type": "STRING"},
        "brand": {"type": "STRING"}, "location": {"type": "STRING"}, "cost_price": {"type": "NUMBER"}, "supplier": {"type": "STRING"}},
        "required": ["barcode"]}},
    {"name": "adjust_stock", "description": "Increase or decrease stock for one barcode by a delta quantity.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "delta_qty": {"type": "NUMBER"}, "reason": {"type": "STRING"}},
        "required": ["barcode", "delta_qty"]}},
    {"name": "set_sale_price", "description": "Set a fixed seasonal sale price for a product barcode.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "sale_price": {"type": "NUMBER"}}, "required": ["barcode", "sale_price"]}},
    {"name": "set_sale_percent", "description": "Set a percentage seasonal discount for a product barcode.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "percent": {"type": "NUMBER"}}, "required": ["barcode", "percent"]}},
    {"name": "remove_sale", "description": "Remove a product barcode from seasonal sale.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}}, "required": ["barcode"]}},
    {"name": "set_bundle", "description": "Set a same-product quantity bundle such as 3 for 25 dollars.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "qty": {"type": "INTEGER"}, "bundle_price": {"type": "NUMBER"}},
        "required": ["barcode", "qty", "bundle_price"]}},
    {"name": "remove_bundle", "description": "Remove a bundle offer for one product barcode.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}}, "required": ["barcode"]}},
    {"name": "print_barcode_labels", "description": "Print barcode labels for one product without changing stock.", "parameters": {"type": "OBJECT", "properties": {
        "barcode": {"type": "STRING"}, "qty": {"type": "INTEGER"}}, "required": ["barcode", "qty"]}},
    {"name": "print_warehouse_sheet", "description": "Print a warehouse location sheet for products matching a search term; blank means all located products.", "parameters": {"type": "OBJECT", "properties": {
        "search": {"type": "STRING"}}}},
]


def _money(value) -> str:
    try:
        return f"${float(value or 0):,.2f}"
    except Exception:
        return "$0.00"


def _query(conn, sql: str, params=()):
    return conn.execute(sql, params).fetchall()


def _period_summary(conn, start_day: str, end_day: str) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(total_amount), 0),
               COALESCE(SUM(discount_total), 0), COALESCE(AVG(total_amount), 0)
        FROM sales
        WHERE COALESCE(is_voided, 0) = 0
          AND date(created_at) >= date(?) AND date(created_at) <= date(?)
        """,
        (start_day, end_day),
    ).fetchone()
    return {"transactions": int(row[0] or 0), "sales": float(row[1] or 0),
            "discounts": float(row[2] or 0), "average": float(row[3] or 0)}


def build_business_context(db_path: str | Path, question: str) -> str:
    """Build a compact, non-customer-identifying context for one question."""
    db_path = Path(db_path)
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        today = date.today()
        periods = [
            ("today", today, today),
            ("last 7 days", today - timedelta(days=6), today),
            ("last 30 days", today - timedelta(days=29), today),
        ]
        lines = [f"Report date: {today.isoformat()}", "Sales summaries:"]
        for label, start, end in periods:
            p = _period_summary(conn, start.isoformat(), end.isoformat())
            lines.append(
                f"- {label}: {p['transactions']} transactions, {_money(p['sales'])} sales, "
                f"{_money(p['discounts'])} discounts, {_money(p['average'])} average ticket"
            )

        inventory = conn.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(stock_qty), 0),
                   COALESCE(SUM(stock_qty * sell_price), 0),
                   SUM(CASE WHEN stock_qty <= low_stock_level THEN 1 ELSE 0 END)
            FROM products WHERE COALESCE(is_deleted, 0) = 0
            """
        ).fetchone()
        lines.append(
            f"Inventory: {int(inventory[0] or 0)} active products, {float(inventory[1] or 0):,.0f} units, "
            f"{_money(inventory[2])} retail value, {int(inventory[3] or 0)} at/below low-stock level."
        )

        lines.append("Top products in the last 30 days:")
        top = _query(conn, """
            SELECT COALESCE(NULLIF(si.name, ''), p.name, si.product_barcode, 'Unknown') AS item_name,
                   COALESCE(si.product_barcode, p.barcode, '') AS barcode,
                   SUM(si.qty) AS units, SUM(si.line_total) AS revenue
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE COALESCE(s.is_voided, 0) = 0
              AND date(s.created_at) >= date(?)
            GROUP BY item_name, barcode ORDER BY revenue DESC LIMIT 20
        """, ((today - timedelta(days=29)).isoformat(),))
        for row in top:
            lines.append(f"- {row['item_name']} [{row['barcode']}]: {float(row['units'] or 0):g} units, {_money(row['revenue'])}")

        low = _query(conn, """
            SELECT name, barcode, stock_qty, low_stock_level, sell_price
            FROM products WHERE COALESCE(is_deleted, 0) = 0 AND stock_qty <= low_stock_level
            ORDER BY stock_qty ASC, name LIMIT 20
        """)
        if low:
            lines.append("Lowest-stock products:")
            for row in low:
                lines.append(
                    f"- {row['name']} [{row['barcode']}]: stock {float(row['stock_qty'] or 0):g}, "
                    f"low level {float(row['low_stock_level'] or 0):g}, price {_money(row['sell_price'])}"
                )

        tokens = [t.lower() for t in re.findall(r"[A-Za-z0-9_-]{3,}", question or "")]
        stop = {"what", "which", "where", "when", "about", "show", "tell", "product", "products",
                "sales", "stock", "price", "today", "yesterday", "last", "this", "that", "with"}
        tokens = [t for t in tokens if t not in stop][:6]
        matches = []
        seen = set()
        for token in tokens:
            rows = _query(conn, """
                SELECT name, barcode, sell_price, stock_qty, category, brand, location
                FROM products WHERE COALESCE(is_deleted, 0) = 0
                  AND (lower(name) LIKE ? OR lower(barcode) LIKE ? OR lower(category) LIKE ? OR lower(brand) LIKE ?)
                ORDER BY name LIMIT 8
            """, tuple([f"%{token}%"] * 4))
            for row in rows:
                key = str(row["barcode"] or row["name"])
                if key not in seen:
                    seen.add(key)
                    matches.append(row)
                if len(matches) >= 20:
                    break
            if len(matches) >= 20:
                break
        if matches:
            lines.append("Products matching words in the question:")
            for row in matches:
                lines.append(
                    f"- {row['name']} [{row['barcode']}]: price {_money(row['sell_price'])}, "
                    f"stock {float(row['stock_qty'] or 0):g}, category {row['category'] or '-'}, "
                    f"brand {row['brand'] or '-'}, location {row['location'] or '-'}"
                )
        return "\n".join(lines)
    finally:
        conn.close()


def ask_gemini(*, api_key: str, model: str, question: str, context: str, timeout: int = 45) -> dict:
    api_key = str(api_key or "").strip()
    if not api_key:
        raise ValueError("Add your free Gemini API key first.")
    model = str(model or "gemini-3.1-flash-lite").strip()
    if model not in ALLOWED_MODELS:
        raise ValueError("Only approved Gemini free-tier models are allowed.")
    prompt = (
        "You are the Mask POS manager assistant. Answer questions using only the local summary. You may propose exactly "
        "one provided function when the manager explicitly asks for an operational action. The application always asks "
        "for confirmation before execution, so never claim an action already happened. Never propose deleting products or "
        "sales, voiding financial records, replacing databases, changing credentials, or inventing missing values. Ask for "
        "missing required details instead. Be concise, use product names before barcodes, and show USD clearly.\n\n"
        f"LOCAL BUSINESS SUMMARY\n{context}\n\nMANAGER QUESTION\n{question}"
    )
    response = requests.post(
        GEMINI_ENDPOINT.format(model=model),
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"functionDeclarations": ACTION_DECLARATIONS}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
        },
        timeout=timeout,
    )
    if response.status_code == 429:
        raise RuntimeError("Free Gemini quota reached. Try again after Google's quota resets. No charge was made.")
    if response.status_code in (401, 403):
        raise RuntimeError("Gemini rejected this API key. Check that it belongs to a free-tier project with billing disabled.")
    if response.status_code >= 400:
        try:
            detail = str((response.json().get("error") or {}).get("message") or "").strip()
        except Exception:
            detail = ""
        raise RuntimeError(detail or f"Gemini request failed (HTTP {response.status_code}).")
    data = response.json() or {}
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
    except Exception as exc:
        raise RuntimeError("Gemini returned no readable answer.") from exc
    for part in parts:
        call = part.get("functionCall") if isinstance(part, dict) else None
        if call:
            name = str(call.get("name") or "").strip()
            if name not in {item["name"] for item in ACTION_DECLARATIONS}:
                raise RuntimeError("Gemini requested an action that Mask POS does not allow.")
            return {"answer": text.strip() or "I prepared this action for your confirmation.",
                    "action": {"name": name, "args": dict(call.get("args") or {})}}
    if not text.strip() or text.strip().lower() in {"none", "null"}:
        raise RuntimeError("Gemini returned an empty answer.")
    return {"answer": text.strip(), "action": None}


def _value(row, key, default=None):
    try:
        return row[key]
    except Exception:
        try:
            return row.get(key, default)
        except Exception:
            return default


def describe_action(action: dict) -> str:
    name = str((action or {}).get("name") or "")
    args = dict((action or {}).get("args") or {})
    labels = {
        "create_product": "Create product", "update_product": "Update product", "adjust_stock": "Adjust stock",
        "set_sale_price": "Set fixed sale price", "set_sale_percent": "Set sale percentage",
        "remove_sale": "Remove seasonal sale", "set_bundle": "Set bundle offer", "remove_bundle": "Remove bundle offer",
        "print_barcode_labels": "Print barcode labels", "print_warehouse_sheet": "Print warehouse sheet",
    }
    details = "\n".join(f"{key}: {value}" for key, value in args.items())
    return f"{labels.get(name, name)}\n\n{details}".strip()


def execute_approved_action(action: dict) -> str:
    """Execute one user-confirmed action through the existing backend rules."""
    from backend import (
        add_product, adjust_stock, find_product_by_barcode, get_store_name, list_products,
        print_configured_barcodes, print_configured_warehouse_paper, remove_bundle_offer_item,
        remove_seasonal_sale_item, set_bundle_offer_item, set_bundle_offers_enabled,
        set_seasonal_sale_enabled, set_seasonal_sale_item, set_seasonal_sale_price_item,
        update_product, update_product_details,
    )
    name = str((action or {}).get("name") or "")
    args = dict((action or {}).get("args") or {})
    barcode = str(args.get("barcode") or "").strip()

    if name == "create_product":
        if not str(args.get("name") or "").strip() or float(args.get("sell_price") or 0) < 0:
            raise ValueError("Product name and a valid price are required.")
        out = add_product(
            name=str(args.get("name") or "").strip(), barcode=barcode or None,
            sell_price=float(args.get("sell_price") or 0), stock_qty=float(args.get("stock_qty") or 0),
            low_stock_level=float(args.get("low_stock_level") or 0), category=str(args.get("category") or ""),
            brand=str(args.get("brand") or ""), location=str(args.get("location") or ""),
            cost_price=float(args.get("cost_price") or 0), supplier=str(args.get("supplier") or ""),
        )
        return f"Product created successfully. Barcode: {out}"

    product = find_product_by_barcode(barcode) if barcode else None
    if name not in {"print_warehouse_sheet"} and not product:
        raise ValueError(f"Product not found for barcode: {barcode}")

    if name == "update_product":
        pid = int(_value(product, "id"))
        def chosen(key, default): return args[key] if key in args else default
        ok = update_product(
            pid, str(chosen("name", _value(product, "name", ""))),
            float(chosen("sell_price", _value(product, "sell_price", 0))),
            float(chosen("stock_qty", _value(product, "stock_qty", 0))),
            float(chosen("low_stock_level", _value(product, "low_stock_level", 0))),
            str(chosen("location", _value(product, "location", ""))),
            str(chosen("category", _value(product, "category", ""))),
            str(chosen("brand", _value(product, "brand", ""))),
        )
        if "cost_price" in args or "supplier" in args:
            update_product_details(pid, float(chosen("cost_price", _value(product, "cost_price", 0))),
                                   str(chosen("supplier", _value(product, "supplier", ""))))
        if not ok:
            raise RuntimeError("Product update failed.")
        return "Product updated successfully."
    if name == "adjust_stock":
        delta = float(args.get("delta_qty") or 0)
        if not delta:
            raise ValueError("Stock adjustment cannot be zero.")
        if not adjust_stock(int(_value(product, "id")), delta, reason=str(args.get("reason") or "AI approved stock adjustment")):
            raise RuntimeError("Stock adjustment failed.")
        return f"Stock adjusted by {delta:g}."
    if name == "set_sale_price":
        set_seasonal_sale_price_item(barcode, float(args.get("sale_price") or 0)); set_seasonal_sale_enabled(True)
        return "Fixed seasonal sale price saved and seasonal sales enabled."
    if name == "set_sale_percent":
        set_seasonal_sale_item(barcode, float(args.get("percent") or 0)); set_seasonal_sale_enabled(True)
        return "Seasonal discount saved and seasonal sales enabled."
    if name == "remove_sale":
        remove_seasonal_sale_item(barcode); return "Seasonal sale removed."
    if name == "set_bundle":
        set_bundle_offer_item(barcode, int(args.get("qty") or 0), float(args.get("bundle_price") or 0)); set_bundle_offers_enabled(True)
        return "Bundle offer saved and bundles enabled."
    if name == "remove_bundle":
        remove_bundle_offer_item(barcode); return "Bundle offer removed."
    if name == "print_barcode_labels":
        qty = max(1, min(500, int(args.get("qty") or 1)))
        labels = [{"name": str(_value(product, "name", "")), "price": float(_value(product, "sell_price", 0)),
                   "barcode": barcode, "location": str(_value(product, "location", "")), "qty": qty}]
        if not print_configured_barcodes(labels, title="Mask POS AI Labels"):
            raise RuntimeError("Labels were not printed. Check the barcode printer settings.")
        return f"Printed {qty} barcode label(s). Stock was not changed."
    if name == "print_warehouse_sheet":
        rows = list_products(str(args.get("search") or "")) or []
        items = [{"barcode": str(_value(r, "barcode", "")), "name": str(_value(r, "name", "")),
                  "location": str(_value(r, "location", "")), "price": float(_value(r, "sell_price", 0))}
                 for r in rows if str(_value(r, "location", "")).strip()]
        if not items:
            raise ValueError("No matching products with warehouse locations were found.")
        if not print_configured_warehouse_paper(get_store_name(), items, title="Warehouse Locations"):
            raise RuntimeError("Warehouse sheet was not printed. Check printer settings.")
        return f"Printed warehouse sheet for {len(items)} product(s)."
    raise ValueError("This action is not allowed.")
