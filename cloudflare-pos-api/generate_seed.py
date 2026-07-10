from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "pos.db"
CONFIG_PATH = ROOT / "pos_config.json"
DEVICE_PATH = ROOT / "cloud_sync_device.json"
OUTPUT_PATH = Path(__file__).resolve().parent / "seed.sql"


def sql(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def insert(table: str, row: dict) -> str:
    columns = list(row)
    values = ", ".join(sql(row[column]) for column in columns)
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values});"


def load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main() -> None:
    cfg = load_json(CONFIG_PATH)
    device_id = str(load_json(DEVICE_PATH).get("device_id") or "maskpos-migration")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    config_payload = {
        "seasonal_sale_enabled": bool(cfg.get("seasonal_sale_enabled", False)),
        "seasonal_sales_map": cfg.get("seasonal_sales_map") or {},
        "bundle_offers_enabled": bool(cfg.get("bundle_offers_enabled", True)),
        "bundle_offers_map": cfg.get("bundle_offers_map") or {},
        "spin_wheel_prizes": cfg.get("spin_wheel_prizes") or [],
    }

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        lines = [
            "PRAGMA foreign_keys = OFF;",
            "DELETE FROM return_items;",
            "DELETE FROM returns;",
            "DELETE FROM sale_items;",
            "DELETE FROM sales;",
            "DELETE FROM cash_shifts;",
            "DELETE FROM employees;",
            "DELETE FROM products;",
            "DELETE FROM pos_print_jobs;",
            "DELETE FROM pos_sync_events;",
        ]

        for table in ("products", "employees", "cash_shifts", "sales", "sale_items", "returns", "return_items"):
            for raw in conn.execute(f"SELECT * FROM {table} ORDER BY id ASC"):
                row = dict(raw)
                local_id = str(row.get("id") or "")
                if table in ("products", "employees", "cash_shifts", "sales", "returns"):
                    row["cloud_device_id"] = device_id
                    row["cloud_local_id"] = local_id
                if table == "returns":
                    row["cloud_original_sale_local_id"] = str(row.get("original_sale_id") or "")
                lines.append(insert(table, row))

        lines.append(insert("pos_sync_events", {
            "event_id": "migration-config-" + uuid.uuid4().hex,
            "device_id": device_id,
            "event_type": "update",
            "entity_type": "config",
            "entity_id": "pos_config",
            "payload": json.dumps(config_payload, separators=(",", ":")),
            "created_at": now,
            "schema_version": 1,
            "inserted_at": now,
        }))
        lines.append("")
        OUTPUT_PATH.write_text("\n".join(lines), encoding="utf-8")
    finally:
        conn.close()

    print(f"Wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
