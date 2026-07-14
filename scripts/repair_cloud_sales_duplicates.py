"""Back up and remove duplicate cloud sales for a date range.

The authoritative copy is the local pos.db.  A cloud row is removed only when
its receipt date/code and sale values exactly match that local sale and another
matching cloud row is retained.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tomllib
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DATABASE_ID = "bfd0e938-fedc-4240-8235-3a96f2416e75"


def sale_key(row: dict) -> tuple:
    return (
        str(row.get("receipt_date") or ""),
        str(row.get("receipt_code") or ""),
        str(row.get("created_at") or ""),
        round(float(row.get("total_amount") or 0), 6),
        str(row.get("payment_method") or ""),
    )


def local_keys(start: str, end: str) -> set[tuple]:
    conn = sqlite3.connect(f"file:{(ROOT / 'pos.db').as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT receipt_date, receipt_code, created_at, total_amount, payment_method "
        "FROM sales WHERE created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchall()
    conn.close()
    return {sale_key(dict(row)) for row in rows}


def cloud_config() -> tuple[str, dict[str, str]]:
    cfg = json.loads((ROOT / "cloudflare_pos_config.json").read_text(encoding="utf-8"))
    return cfg["rest_url"], {"Authorization": f"Bearer {cfg['api_token']}"}


def cloudflare_admin() -> tuple[str, dict[str, str]]:
    config_path = Path(os.environ["APPDATA"]) / "xdg.config" / ".wrangler" / "config" / "default.toml"
    cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
    token = cfg.get("oauth_token") or cfg.get("api_token")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.get("https://api.cloudflare.com/client/v4/accounts", headers=headers, timeout=30)
    response.raise_for_status()
    accounts = response.json()["result"]
    if len(accounts) != 1:
        raise RuntimeError(f"Expected one Cloudflare account, found {len(accounts)}")
    return accounts[0]["id"], headers


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-07-06", help="exclusive date")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    rest_url, rest_headers = cloud_config()
    response = requests.get(
        f"{rest_url}/sales",
        headers=rest_headers,
        params={"created_at": f"gte.{args.start}", "order": "created_at.asc", "limit": "5000"},
        timeout=30,
    )
    response.raise_for_status()
    rows = [row for row in response.json() if str(row.get("created_at") or "") < args.end]
    valid_local = local_keys(args.start, args.end)
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        groups[sale_key(row)].append(row)

    delete_rows: list[dict] = []
    for key, matches in groups.items():
        if key not in valid_local or len(matches) < 2:
            continue
        matches.sort(key=lambda row: (str(row.get("cloud_event_id") or "").startswith("backfill:"), int(row["id"])))
        delete_rows.extend(matches[1:])

    unmatched = valid_local - set(groups)
    print(f"cloud rows={len(rows)} local sales={len(valid_local)} duplicates_to_remove={len(delete_rows)}")
    if unmatched:
        raise RuntimeError(f"Refusing repair: {len(unmatched)} local sales have no matching cloud row")
    if not args.apply:
        return

    backup_dir = ROOT / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"cloud_duplicate_sales_{stamp}.json"
    item_response = requests.get(
        f"{rest_url}/sale_items", headers=rest_headers,
        params={"order": "id.asc", "limit": "10000"}, timeout=30,
    )
    item_response.raise_for_status()
    delete_ids = {int(row["id"]) for row in delete_rows}
    backup = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "range": [args.start, args.end],
        "sales": delete_rows,
        "sale_items": [row for row in item_response.json() if int(row.get("sale_id") or 0) in delete_ids],
    }
    backup_path.write_text(json.dumps(backup, indent=2), encoding="utf-8")

    account_id, admin_headers = cloudflare_admin()
    endpoint = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{DATABASE_ID}/query"
    ids = sorted(delete_ids)
    for offset in range(0, len(ids), 80):
        batch = ids[offset:offset + 80]
        values = ",".join(str(value) for value in batch)
        sql = f"DELETE FROM sale_items WHERE sale_id IN ({values}); DELETE FROM sales WHERE id IN ({values});"
        result = requests.post(endpoint, headers=admin_headers, json={"sql": sql}, timeout=30)
        result.raise_for_status()
        body = result.json()
        if not body.get("success"):
            raise RuntimeError(body.get("errors") or "Cloudflare D1 deletion failed")
    print(f"removed={len(ids)} backup={backup_path}")


if __name__ == "__main__":
    main()
