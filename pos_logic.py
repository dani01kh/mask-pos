# pos_logic.py - Mask POS backend (SQLite, products, sales, analytics, cash shifts + employees)
# Fixes included:
# - Single consistent schema with migrations
# - Prevent "database is locked" using WAL + busy_timeout
# - create_sale inserts required columns (including legacy NOT NULL columns like discount_total)
# - Includes _range_bounds for app.py imports
import os
import sys
from pathlib import Path
import sqlite3
import random
import re
from datetime import datetime, date, timedelta

def _base_dir() -> Path:
    # Allow parent/launcher to override via MASKPOS_DATA_DIR (useful when running the server)
    env = os.environ.get("MASKPOS_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()

    # When packaged as EXE, sys.executable points to the exe path
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent

DB_PATH = str(_base_dir() / "pos.db")
# ---------------- DB HELPERS ----------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _col_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols


def _table_info(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    # columns: cid, name, type, notnull, dflt_value, pk
    return cur.fetchall()


def _table_cols(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _ensure_column(cur, table, col, col_def_sql):
    if not _col_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def_sql}")


def _backfill_cash_shift_daily_numbers(cur):
    """Fill daily display numbers: Shift 1, Shift 2, ... for each opened date."""
    try:
        cur.execute("""
            SELECT id, opened_at
            FROM cash_shifts
            ORDER BY substr(COALESCE(opened_at, ''), 1, 10), datetime(opened_at), id
        """)
        seq_by_day = {}
        for row in cur.fetchall() or []:
            try:
                shift_id = int(row["id"])
                opened_at = str(row["opened_at"] or "")
            except Exception:
                shift_id = int(row[0])
                opened_at = str(row[1] or "")
            shift_date = opened_at[:10] if len(opened_at) >= 10 else date.today().isoformat()
            seq = int(seq_by_day.get(shift_date, 0) or 0) + 1
            seq_by_day[shift_date] = seq
            cur.execute(
                """
                UPDATE cash_shifts
                SET shift_date = ?, shift_seq = ?, shift_code = ?
                WHERE id = ?
                  AND (
                    shift_date IS NULL OR shift_date = ''
                    OR shift_seq IS NULL
                    OR shift_code IS NULL OR shift_code = ''
                  )
                """,
                (shift_date, int(seq), str(seq), int(shift_id)),
            )
    except Exception:
        pass


def init_db():
    conn = None
    try:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        # Base tables (preferred schema)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT,
            brand TEXT,
            location TEXT,
            sell_price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            low_stock_level INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total_amount REAL NOT NULL DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            customer_name TEXT NOT NULL DEFAULT '',
            shift_id INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(sale_id) REFERENCES sales(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            pin TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            opening_cash REAL NOT NULL DEFAULT 0,
            closing_cash REAL,
            notes TEXT,
            employee_id INTEGER,
            shift_date TEXT,
            shift_seq INTEGER,
            shift_code TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_sale_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            total_return_amount REAL NOT NULL DEFAULT 0,
            shift_id INTEGER,
            notes TEXT,
            FOREIGN KEY(original_sale_id) REFERENCES sales(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS return_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id INTEGER NOT NULL,
            sale_item_id INTEGER,
            product_id INTEGER,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(return_id) REFERENCES returns(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            original_amount REAL NOT NULL DEFAULT 0,
            remaining_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            return_id INTEGER,
            original_sale_id INTEGER,
            shift_id INTEGER,
            issued_by_employee_id INTEGER,
            issued_by_name TEXT,
            signature_text TEXT,
            notes TEXT,
            redeemed_at TEXT,
            last_redeemed_at TEXT,
            voided_at TEXT,
            void_notes TEXT,
            FOREIGN KEY(return_id) REFERENCES returns(id),
            FOREIGN KEY(original_sale_id) REFERENCES sales(id),
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
            FOREIGN KEY(issued_by_employee_id) REFERENCES employees(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bon_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bon_id INTEGER NOT NULL,
            sale_id INTEGER,
            created_at TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            shift_id INTEGER,
            notes TEXT,
            FOREIGN KEY(bon_id) REFERENCES bons(id),
            FOREIGN KEY(sale_id) REFERENCES sales(id),
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            shift_id INTEGER,
            movement_type TEXT NOT NULL DEFAULT 'OUT',
            amount_usd REAL NOT NULL DEFAULT 0,
            amount_lbp REAL NOT NULL DEFAULT 0,
            lbp_per_usd REAL NOT NULL DEFAULT 89500,
            amount_value REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            employee_id INTEGER,
            employee_name TEXT,
            notes TEXT,
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """)

        # Migrations / compatibility columns
        _ensure_column(cur, "products", "is_deleted", "INTEGER NOT NULL DEFAULT 0")

        # Employees migrations (older DBs may miss these columns)
        # NOTE: when adding columns via ALTER TABLE, keep them nullable/defaulted for compatibility.
        _ensure_column(cur, "employees", "is_active", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(cur, "employees", "created_at", "TEXT")
        # backfill missing created_at
        try:
            cur.execute("UPDATE employees SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL OR created_at = ''", (_now_iso(),))
        except Exception:
            pass

        # Sales compatibility
        _ensure_column(cur, "products", "location", "TEXT")

        _ensure_column(cur, "sales", "total_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "shift_id", "INTEGER")

        # Customer-facing receipt numbering (keeps internal sale id hidden)
        _ensure_column(cur, "sales", "receipt_date", "TEXT")
        _ensure_column(cur, "sales", "receipt_seq", "INTEGER")
        _ensure_column(cur, "sales", "receipt_code", "TEXT")

        # Common POS/analytics style columns that some of your old DBs used
        _ensure_column(cur, "sales", "subtotal", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "discount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "discount_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "tax", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "tax_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "shipping", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "net_sales", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "total_sales", "REAL NOT NULL DEFAULT 0")

        # Tender/accounting columns (required to keep exchanges + returns from corrupting cash totals)
        _ensure_column(cur, "sales", "cash_paid", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "store_credit_used", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "is_exchange", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "exchange_origin_sale_id", "INTEGER")

        # Sale-item accounting columns for correct prorated returns
        _ensure_column(cur, "sale_items", "gross_line_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sale_items", "discount_allocated", "REAL NOT NULL DEFAULT 0")

        # Return accounting columns (cash vs credit). Existing systems may only have total_return_amount.
        _ensure_column(cur, "returns", "cash_refund", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "credit_refund", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "voided_at", "TEXT")
        _ensure_column(cur, "returns", "void_notes", "TEXT")

        _ensure_column(cur, "bons", "remaining_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "bons", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        _ensure_column(cur, "bons", "signature_text", "TEXT")
        _ensure_column(cur, "bons", "last_redeemed_at", "TEXT")
        _ensure_column(cur, "bons", "voided_at", "TEXT")
        _ensure_column(cur, "bons", "void_notes", "TEXT")

        _ensure_column(cur, "bons", "remaining_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "bons", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        _ensure_column(cur, "bons", "signature_text", "TEXT")
        _ensure_column(cur, "bons", "last_redeemed_at", "TEXT")
        _ensure_column(cur, "bons", "voided_at", "TEXT")
        _ensure_column(cur, "bons", "void_notes", "TEXT")
        _ensure_column(cur, "cash_shifts", "shift_date", "TEXT")
        _ensure_column(cur, "cash_shifts", "shift_seq", "INTEGER")
        _ensure_column(cur, "cash_shifts", "shift_code", "TEXT")
        _ensure_column(cur, "cash_shifts", "opening_usd", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_shifts", "opening_lbp", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_shifts", "closing_usd", "REAL")
        _ensure_column(cur, "cash_shifts", "closing_lbp", "REAL")
        _ensure_column(cur, "cash_shifts", "lbp_per_usd", "REAL NOT NULL DEFAULT 89500")
        _backfill_cash_shift_daily_numbers(cur)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            shift_id INTEGER,
            movement_type TEXT NOT NULL DEFAULT 'OUT',
            amount_usd REAL NOT NULL DEFAULT 0,
            amount_lbp REAL NOT NULL DEFAULT 0,
            lbp_per_usd REAL NOT NULL DEFAULT 89500,
            amount_value REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            employee_id INTEGER,
            employee_name TEXT,
            notes TEXT,
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """)

        _ensure_column(cur, "cash_movements", "created_at", "TEXT")
        _ensure_column(cur, "cash_movements", "shift_id", "INTEGER")
        _ensure_column(cur, "cash_movements", "movement_type", "TEXT NOT NULL DEFAULT 'OUT'")
        _ensure_column(cur, "cash_movements", "amount_usd", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "amount_lbp", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "lbp_per_usd", "REAL NOT NULL DEFAULT 89500")
        _ensure_column(cur, "cash_movements", "amount_value", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(cur, "cash_movements", "employee_id", "INTEGER")
        _ensure_column(cur, "cash_movements", "employee_name", "TEXT")
        _ensure_column(cur, "cash_movements", "notes", "TEXT")

        # Some older DBs had "total" instead of "total_amount"
        sales_cols = set(_table_cols(cur, "sales"))
        if "total" in sales_cols and "total_amount" in sales_cols:
            try:
                cur.execute("""
                    UPDATE sales
                    SET total_amount = COALESCE(total, 0)
                    WHERE (total_amount IS NULL OR total_amount = 0)
                      AND total IS NOT NULL AND total != 0
                """)
            except Exception:
                pass


        # Backfill tender columns for legacy data (best-effort).
        # Non-cash tenders must never inflate the physical cash drawer.
        try:
            cur.execute("""
                UPDATE sales
                SET cash_paid = 0,
                    store_credit_used = COALESCE(store_credit_used, 0),
                    is_exchange = CASE WHEN UPPER(COALESCE(payment_method,'')) = 'EXCHANGE' THEN 1 ELSE COALESCE(is_exchange,0) END
                WHERE UPPER(COALESCE(payment_method,'')) IN ('EXCHANGE','STORE_CREDIT','CARD','DEBIT','CREDIT_CARD','WHISH')
            """)
            cur.execute("""
                UPDATE sales
                SET cash_paid = COALESCE(NULLIF(cash_paid, 0), total_amount),
                    store_credit_used = COALESCE(store_credit_used, 0),
                    is_exchange = CASE WHEN UPPER(COALESCE(payment_method,'')) = 'EXCHANGE' THEN 1 ELSE COALESCE(is_exchange,0) END
                WHERE (cash_paid IS NULL OR cash_paid = 0)
                  AND COALESCE(total_amount,0) > 0
                  AND UPPER(COALESCE(payment_method,'')) NOT IN ('EXCHANGE','STORE_CREDIT','CARD','DEBIT','CREDIT_CARD','WHISH')
            """)
        except Exception:
            pass

        # Indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_created_at ON sales(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_shift_id ON sales(shift_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shift_open ON cash_shifts(closed_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_returns_sale ON returns(original_sale_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_return_items_return ON return_items(return_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_code ON bons(code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_status ON bons(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_return ON bons(return_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bon_redemptions_bon ON bon_redemptions(bon_id)")
        # ---------------- Schema migrations (safe ALTER TABLE) ----------------
        # We keep the base schema minimal for older DBs, then add newer columns if missing.
        # These columns are required for correct exchange/return accounting and analytics.
        try:
            # sales: track both merchandise total (total_sales) and cash collected (cash_paid)
            _ensure_column(cur, "sales", "notes", "TEXT DEFAULT ''")
            _ensure_column(cur, "sales", "subtotal", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "discount_total", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "tax_total", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "shipping", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "net_sales", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "total_sales", "REAL NOT NULL DEFAULT 0")

            # exchange/store-credit tracking
            _ensure_column(cur, "sales", "store_credit_used", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "cash_paid", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "is_exchange", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "sales", "exchange_origin_sale_id", "INTEGER")

            # optional customer-facing receipt fields
            _ensure_column(cur, "sales", "receipt_date", "TEXT")
            _ensure_column(cur, "sales", "receipt_seq", "INTEGER")
            _ensure_column(cur, "sales", "receipt_code", "TEXT")

            # sale_items: store gross/net for accurate discounted returns
            _ensure_column(cur, "sale_items", "gross_line_total", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "sale_items", "discount_allocated", "REAL NOT NULL DEFAULT 0")

            # returns: keep analytics stable even if your DB started without these
            _ensure_column(cur, "returns", "cash_refund", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "credit_refund", "REAL NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "voided_at", "TEXT")
            _ensure_column(cur, "returns", "void_notes", "TEXT")
        except Exception:
            # Migrations are best-effort; app can still run on older DBs.
            pass



        conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# A newer compatibility block below owns import-time initialization.


# ---------------- BARCODE GENERATION ----------------

def _generate_barcode_12():
    return "".join(str(random.randint(0, 9)) for _ in range(12))


def _unique_barcode(cur):
    for _ in range(50):
        code = _generate_barcode_12()
        cur.execute("SELECT 1 FROM products WHERE barcode = ?", (code,))
        if not cur.fetchone():
            return code
    return datetime.now().strftime("%y%m%d%H%M%S")


# ---------------- PRODUCTS ----------------

def add_product(name, category="", brand="", sell_price=0.0, stock_qty=0, low_stock_level=0, barcode=None, location=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        requested_barcode = "".join(ch for ch in str(barcode or "") if ch.isdigit())
        if requested_barcode and len(requested_barcode) > 13:
            raise ValueError("Barcode must be 13 digits or fewer.")
        if requested_barcode:
            cur.execute("SELECT 1 FROM products WHERE barcode = ? LIMIT 1", (requested_barcode,))
            if cur.fetchone():
                raise ValueError(f"Barcode already exists: {requested_barcode}")
        barcode = requested_barcode or _unique_barcode(cur)
        cur.execute("""
            INSERT INTO products (barcode, name, category, brand, location, sell_price, stock_qty, low_stock_level, is_deleted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (
            str(barcode),
            str(name).strip(),
            str(category or "").strip(),
            str(brand or "").strip(),
            str(location or "").strip(),
            float(sell_price or 0),
            int(stock_qty or 0),
            int(low_stock_level or 0),
            _now_iso()
        ))

        conn.commit()
        return barcode
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_products(query=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            cur.execute("""
                SELECT * FROM products
                WHERE is_deleted = 0
                  AND (name LIKE ? OR barcode LIKE ? OR category LIKE ? OR brand LIKE ? OR location LIKE ?)
                ORDER BY name COLLATE NOCASE ASC
            """, (like, like, like, like, like))
        else:
            cur.execute("""
                SELECT * FROM products
                WHERE is_deleted = 0
                ORDER BY name COLLATE NOCASE ASC
            """)

        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def find_product_by_barcode(barcode):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM products
            WHERE barcode = ?
              AND is_deleted = 0
            LIMIT 1
        """, (str(barcode).strip(),))
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


def update_product(product_id, name, sell_price, stock_qty, low_stock_level, location=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET name = ?, sell_price = ?, stock_qty = ?, low_stock_level = ?, location = ?
            WHERE id = ?
        """, (
            str(name).strip(),
            float(sell_price or 0),
            int(stock_qty or 0),
            int(low_stock_level or 0),
            str(location or "").strip(),
            int(product_id)
        ))
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def adjust_stock(product_id, delta_qty):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET stock_qty = stock_qty + ?
            WHERE id = ?
        """, (int(delta_qty), int(product_id)))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def delete_product(product_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET is_deleted = 1
            WHERE id = ?
        """, (int(product_id),))
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ---------------- EMPLOYEES ----------------

def ensure_employee(name, pin=""):
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("Employee name required")

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM employees WHERE name = ? LIMIT 1", (nm,))
        r = cur.fetchone()
        if r:
            return int(r["id"])

        cur.execute("""
            INSERT INTO employees (name, pin, is_active, created_at)
            VALUES (?, ?, 1, ?)
        """, (nm, str(pin or ""), _now_iso()))
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_employees(active_only=True):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if active_only:
            cur.execute("SELECT * FROM employees WHERE is_active = 1 ORDER BY name COLLATE NOCASE ASC")
        else:
            cur.execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE ASC")
        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def deactivate_employee(name: str) -> bool:
    """Soft-remove employee from active list by setting is_active=0.

    Returns True if an employee was updated, False if not found.
    """
    nm = str(name or "").strip()
    if not nm:
        return False

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE employees SET is_active = 0 WHERE name = ?", (nm,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ---------------- CASH SHIFTS ----------------

def get_open_shift():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.closed_at IS NULL
            ORDER BY cs.id DESC
            LIMIT 1
        """)
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


# ---------------- DB HELPERS ----------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _col_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    return col in cols


def _table_info(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    # columns: cid, name, type, notnull, dflt_value, pk
    return cur.fetchall()


def _table_cols(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [r[1] for r in cur.fetchall()]


def _ensure_column(cur, table, col, col_def_sql):
    if not _col_exists(cur, table, col):
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def_sql}")


def _repair_open_shifts(cur) -> int:
    """Keep the newest open shift and close stale rows left by older builds."""
    rows = cur.execute(
        "SELECT id FROM cash_shifts WHERE closed_at IS NULL ORDER BY id DESC"
    ).fetchall()
    if len(rows) <= 1:
        return 0

    keep_id = int(rows[0][0])
    closed_at = _now_iso()
    note = "[Auto-repair] Closed stale open shift after startup detected multiple open shifts."
    cur.execute(
        """
        UPDATE cash_shifts
        SET closed_at = ?,
            closing_cash = COALESCE(closing_cash, opening_cash),
            closing_usd = COALESCE(closing_usd, opening_usd),
            closing_lbp = COALESCE(closing_lbp, opening_lbp),
            notes = TRIM(COALESCE(notes, '') || CASE WHEN COALESCE(notes, '') = '' THEN '' ELSE '\n' END || ?)
        WHERE closed_at IS NULL
          AND id <> ?
        """,
        (closed_at, note, keep_id),
    )
    return int(cur.rowcount or 0)


def _repair_unassigned_sales(cur):
    """Automatically assign sales that have no shift_id to their matching or closest shift."""
    try:
        # Find all sales where shift_id is NULL or not a valid positive integer
        cur.execute("""
            SELECT id, created_at FROM sales 
            WHERE shift_id IS NULL OR shift_id = '' OR shift_id = 0
        """)
        unassigned_sales = cur.fetchall()
        if not unassigned_sales:
            return

        for sale in unassigned_sales:
            sale_id = sale["id"]
            created_at = sale["created_at"]
            if not created_at:
                continue

            # 1. Look for a shift that was open at the exact time of the sale
            cur.execute("""
                SELECT id FROM cash_shifts
                WHERE datetime(opened_at) <= datetime(?)
                  AND (closed_at IS NULL OR datetime(?) <= datetime(closed_at))
                ORDER BY opened_at DESC
                LIMIT 1
            """, (created_at, created_at))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE sales SET shift_id = ? WHERE id = ?", (row["id"], sale_id))
                continue

            # 2. Look for the closest shift on the same day
            day_str = created_at[:10]
            cur.execute("""
                SELECT id FROM cash_shifts
                WHERE substr(opened_at, 1, 10) = ?
                ORDER BY abs(julianday(opened_at) - julianday(?)) ASC
                LIMIT 1
            """, (day_str, created_at))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE sales SET shift_id = ? WHERE id = ?", (row["id"], sale_id))
                continue

            # 3. Look for the closest shift overall
            cur.execute("""
                SELECT id FROM cash_shifts
                ORDER BY abs(julianday(opened_at) - julianday(?)) ASC
                LIMIT 1
            """, (created_at,))
            row = cur.fetchone()
            if row:
                cur.execute("UPDATE sales SET shift_id = ? WHERE id = ?", (row["id"], sale_id))
                continue

    except Exception:
        pass


def init_db():
    conn = None
    try:
        conn = get_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        # Base tables (preferred schema)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            category TEXT,
            brand TEXT,
            location TEXT,
            sell_price REAL NOT NULL DEFAULT 0,
            stock_qty INTEGER NOT NULL DEFAULT 0,
            low_stock_level INTEGER NOT NULL DEFAULT 0,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            total_amount REAL NOT NULL DEFAULT 0,
            payment_method TEXT NOT NULL DEFAULT 'CASH',
            customer_name TEXT NOT NULL DEFAULT '',
            shift_id INTEGER
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS sale_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER NOT NULL,
            product_id INTEGER,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(sale_id) REFERENCES sales(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            pin TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            opening_cash REAL NOT NULL DEFAULT 0,
            closing_cash REAL,
            notes TEXT,
            employee_id INTEGER,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS returns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_sale_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            total_return_amount REAL NOT NULL DEFAULT 0,
            shift_id INTEGER,
            notes TEXT,
            FOREIGN KEY(original_sale_id) REFERENCES sales(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS return_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            return_id INTEGER NOT NULL,
            sale_item_id INTEGER,
            product_id INTEGER,
            name TEXT NOT NULL,
            price REAL NOT NULL DEFAULT 0,
            qty INTEGER NOT NULL DEFAULT 0,
            line_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(return_id) REFERENCES returns(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            original_amount REAL NOT NULL DEFAULT 0,
            remaining_amount REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            return_id INTEGER,
            original_sale_id INTEGER,
            shift_id INTEGER,
            issued_by_employee_id INTEGER,
            issued_by_name TEXT,
            signature_text TEXT,
            notes TEXT,
            redeemed_at TEXT,
            last_redeemed_at TEXT,
            voided_at TEXT,
            void_notes TEXT,
            FOREIGN KEY(return_id) REFERENCES returns(id),
            FOREIGN KEY(original_sale_id) REFERENCES sales(id),
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
            FOREIGN KEY(issued_by_employee_id) REFERENCES employees(id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS bon_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bon_id INTEGER NOT NULL,
            sale_id INTEGER,
            created_at TEXT NOT NULL,
            amount REAL NOT NULL DEFAULT 0,
            shift_id INTEGER,
            notes TEXT,
            FOREIGN KEY(bon_id) REFERENCES bons(id),
            FOREIGN KEY(sale_id) REFERENCES sales(id),
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id)
        )
        """)

        # Migrations / compatibility columns
        _ensure_column(cur, "products", "is_deleted", "INTEGER NOT NULL DEFAULT 0")

        # Employees migrations (older DBs may miss these columns)
        # NOTE: when adding columns via ALTER TABLE, keep them nullable/defaulted for compatibility.
        _ensure_column(cur, "employees", "is_active", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(cur, "employees", "created_at", "TEXT")
        # backfill missing created_at
        try:
            cur.execute("UPDATE employees SET created_at = COALESCE(created_at, ?) WHERE created_at IS NULL OR created_at = ''", (_now_iso(),))
        except Exception:
            pass

        # Sales compatibility
        _ensure_column(cur, "products", "location", "TEXT")

        _ensure_column(cur, "sales", "total_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "shift_id", "INTEGER")

        # Customer-facing receipt numbering (keeps internal sale id hidden)
        _ensure_column(cur, "sales", "receipt_date", "TEXT")
        _ensure_column(cur, "sales", "receipt_seq", "INTEGER")
        _ensure_column(cur, "sales", "receipt_code", "TEXT")

        # Common POS/analytics style columns that some of your old DBs used
        _ensure_column(cur, "sales", "subtotal", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "discount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "discount_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "tax", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "tax_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "shipping", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "net_sales", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "total_sales", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "notes", "TEXT DEFAULT ''")

        # Tender/accounting columns keep exchanges and returns out of cash totals.
        _ensure_column(cur, "sales", "cash_paid", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "store_credit_used", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "is_exchange", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "sales", "exchange_origin_sale_id", "INTEGER")

        # Net line amounts are required for accurate discounted returns.
        _ensure_column(cur, "sale_items", "gross_line_total", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "sale_items", "discount_allocated", "REAL NOT NULL DEFAULT 0")

        _ensure_column(cur, "returns", "cash_refund", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "credit_refund", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "voided_at", "TEXT")
        _ensure_column(cur, "returns", "void_notes", "TEXT")

        _ensure_column(cur, "bons", "remaining_amount", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "bons", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
        _ensure_column(cur, "bons", "signature_text", "TEXT")
        _ensure_column(cur, "bons", "last_redeemed_at", "TEXT")
        _ensure_column(cur, "bons", "voided_at", "TEXT")
        _ensure_column(cur, "bons", "void_notes", "TEXT")

        _ensure_column(cur, "cash_shifts", "shift_date", "TEXT")
        _ensure_column(cur, "cash_shifts", "shift_seq", "INTEGER")
        _ensure_column(cur, "cash_shifts", "shift_code", "TEXT")
        _ensure_column(cur, "cash_shifts", "opening_usd", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_shifts", "opening_lbp", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_shifts", "closing_usd", "REAL")
        _ensure_column(cur, "cash_shifts", "closing_lbp", "REAL")
        _ensure_column(cur, "cash_shifts", "lbp_per_usd", "REAL NOT NULL DEFAULT 89500")
        _backfill_cash_shift_daily_numbers(cur)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            shift_id INTEGER,
            movement_type TEXT NOT NULL DEFAULT 'OUT',
            amount_usd REAL NOT NULL DEFAULT 0,
            amount_lbp REAL NOT NULL DEFAULT 0,
            lbp_per_usd REAL NOT NULL DEFAULT 89500,
            amount_value REAL NOT NULL DEFAULT 0,
            reason TEXT NOT NULL DEFAULT '',
            employee_id INTEGER,
            employee_name TEXT,
            notes TEXT,
            FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """)

        _ensure_column(cur, "cash_movements", "created_at", "TEXT")
        _ensure_column(cur, "cash_movements", "shift_id", "INTEGER")
        _ensure_column(cur, "cash_movements", "movement_type", "TEXT NOT NULL DEFAULT 'OUT'")
        _ensure_column(cur, "cash_movements", "amount_usd", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "amount_lbp", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "lbp_per_usd", "REAL NOT NULL DEFAULT 89500")
        _ensure_column(cur, "cash_movements", "amount_value", "REAL NOT NULL DEFAULT 0")
        _ensure_column(cur, "cash_movements", "reason", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(cur, "cash_movements", "employee_id", "INTEGER")
        _ensure_column(cur, "cash_movements", "employee_name", "TEXT")
        _ensure_column(cur, "cash_movements", "notes", "TEXT")

        # Quick items are temporary catalog rows created for receipt compatibility.
        # Older builds deducted their stock from zero and left them visible forever.
        cur.execute("""
            UPDATE products
            SET stock_qty = 0, is_deleted = 1
            WHERE LOWER(TRIM(COALESCE(category, ''))) = 'quick'
              AND stock_qty <= 0
        """)

        _repair_open_shifts(cur)

        # Some older DBs had "total" instead of "total_amount"
        sales_cols = set(_table_cols(cur, "sales"))
        if "total" in sales_cols and "total_amount" in sales_cols:
            try:
                cur.execute("""
                    UPDATE sales
                    SET total_amount = COALESCE(total, 0)
                    WHERE (total_amount IS NULL OR total_amount = 0)
                      AND total IS NOT NULL AND total != 0
                """)
            except Exception:
                pass

        try:
            cur.execute("""
                UPDATE sales
                SET cash_paid = 0,
                    store_credit_used = COALESCE(store_credit_used, 0),
                    is_exchange = CASE
                        WHEN UPPER(COALESCE(payment_method, '')) = 'EXCHANGE' THEN 1
                        ELSE COALESCE(is_exchange, 0)
                    END
                WHERE UPPER(COALESCE(payment_method, '')) IN ('EXCHANGE','STORE_CREDIT','CARD','DEBIT','CREDIT_CARD','WHISH')
            """)
            cur.execute("""
                UPDATE sales
                SET cash_paid = COALESCE(NULLIF(cash_paid, 0), total_amount),
                    store_credit_used = COALESCE(store_credit_used, 0),
                    is_exchange = CASE
                        WHEN UPPER(COALESCE(payment_method, '')) = 'EXCHANGE' THEN 1
                        ELSE COALESCE(is_exchange, 0)
                    END
                WHERE (cash_paid IS NULL OR cash_paid = 0)
                  AND COALESCE(total_amount, 0) > 0
                  AND UPPER(COALESCE(payment_method, '')) NOT IN ('EXCHANGE','STORE_CREDIT','CARD','DEBIT','CREDIT_CARD','WHISH')
            """)
        except Exception:
            pass

        # Indices
        cur.execute("CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_created_at ON sales(created_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_shift_id ON sales(shift_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sale_items_sale_id ON sale_items(sale_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_shift_open ON cash_shifts(closed_at)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_shifts_one_open ON cash_shifts((1)) WHERE closed_at IS NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_returns_sale ON returns(original_sale_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_return_items_return ON return_items(return_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_code ON bons(code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_status ON bons(status)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_return ON bons(return_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bon_redemptions_bon ON bon_redemptions(bon_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cash_movements_shift ON cash_movements(shift_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cash_movements_created ON cash_movements(created_at)")

        try:
            _repair_unassigned_sales(cur)
        except Exception:
            pass

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# Initialize on import
init_db()


# ---------------- BARCODE GENERATION ----------------

def _generate_barcode_12():
    return "".join(str(random.randint(0, 9)) for _ in range(12))


def _unique_barcode(cur):
    for _ in range(50):
        code = _generate_barcode_12()
        cur.execute("SELECT 1 FROM products WHERE barcode = ?", (code,))
        if not cur.fetchone():
            return code
    return datetime.now().strftime("%y%m%d%H%M%S")


# ---------------- PRODUCTS ----------------

def add_product(name, category="", brand="", sell_price=0.0, stock_qty=0, low_stock_level=0, barcode=None, location=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        requested_barcode = "".join(ch for ch in str(barcode or "") if ch.isdigit())
        if requested_barcode and len(requested_barcode) > 13:
            raise ValueError("Barcode must be 13 digits or fewer.")
        if requested_barcode:
            cur.execute("SELECT 1 FROM products WHERE barcode = ? LIMIT 1", (requested_barcode,))
            if cur.fetchone():
                raise ValueError(f"Barcode already exists: {requested_barcode}")
        barcode = requested_barcode or _unique_barcode(cur)
        cur.execute("""
            INSERT INTO products (barcode, name, category, brand, location, sell_price, stock_qty, low_stock_level, is_deleted, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (
            str(barcode),
            str(name).strip(),
            str(category or "").strip(),
            str(brand or "").strip(),
            str(location or "").strip(),
            float(sell_price or 0),
            int(stock_qty or 0),
            int(low_stock_level or 0),
            _now_iso()
        ))

        conn.commit()
        return barcode
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_products(query=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        q = (query or "").strip()
        if q:
            like = f"%{q}%"
            cur.execute("""
                SELECT * FROM products
                WHERE is_deleted = 0
                  AND (name LIKE ? OR barcode LIKE ? OR category LIKE ? OR brand LIKE ? OR location LIKE ?)
                ORDER BY name COLLATE NOCASE ASC
            """, (like, like, like, like, like))
        else:
            cur.execute("""
                SELECT * FROM products
                WHERE is_deleted = 0
                ORDER BY name COLLATE NOCASE ASC
            """)

        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def find_product_by_barcode(barcode):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM products
            WHERE barcode = ?
              AND is_deleted = 0
            LIMIT 1
        """, (str(barcode).strip(),))
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


def update_product(product_id, name, sell_price, stock_qty, low_stock_level, location=""):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET name = ?, sell_price = ?, stock_qty = ?, low_stock_level = ?, location = ?
            WHERE id = ?
        """, (
            str(name).strip(),
            float(sell_price or 0),
            int(stock_qty or 0),
            int(low_stock_level or 0),
            str(location or "").strip(),
            int(product_id)
        ))
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def adjust_stock(product_id, delta_qty):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET stock_qty = stock_qty + ?
            WHERE id = ?
        """, (int(delta_qty), int(product_id)))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def delete_product(product_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            UPDATE products
            SET is_deleted = 1
            WHERE id = ?
        """, (int(product_id),))
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ---------------- EMPLOYEES ----------------

def ensure_employee(name, pin=""):
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("Employee name required")

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id FROM employees WHERE name = ? LIMIT 1", (nm,))
        r = cur.fetchone()
        if r:
            return int(r["id"])

        cur.execute("""
            INSERT INTO employees (name, pin, is_active, created_at)
            VALUES (?, ?, 1, ?)
        """, (nm, str(pin or ""), _now_iso()))
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_employees(active_only=True):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        if active_only:
            cur.execute("SELECT * FROM employees WHERE is_active = 1 ORDER BY name COLLATE NOCASE ASC")
        else:
            cur.execute("SELECT * FROM employees ORDER BY name COLLATE NOCASE ASC")
        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def deactivate_employee(name: str) -> bool:
    """Soft-remove employee from active list by setting is_active=0.

    Returns True if an employee was updated, False if not found.
    """
    nm = str(name or "").strip()
    if not nm:
        return False

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE employees SET is_active = 0 WHERE name = ?", (nm,))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ---------------- CASH SHIFTS ----------------

def get_open_shift():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.closed_at IS NULL
            ORDER BY cs.id DESC
            LIMIT 1
        """)
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


def get_last_closed_shift():
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.closed_at IS NOT NULL
            ORDER BY datetime(cs.closed_at) DESC, cs.id DESC
            LIMIT 1
        """)
        return cur.fetchone()
    finally:
        if conn:
            conn.close()


def open_shift(opening_cash=0.0, notes="", employee_name="", opening_usd=None, opening_lbp=0.0, lbp_per_usd=None, shift_code=None):
    """Open a new cash shift.

    Cashier-proof rule: only ONE open shift is allowed at a time.
    If a shift is already open, this function raises RuntimeError.
    """
    existing = get_open_shift()
    if existing:
        emp = (existing.get("employee_name") if hasattr(existing, "get") else None) or ""
        opened_at = existing.get("opened_at") if hasattr(existing, "get") else None
        msg = "A shift is already open."
        if emp:
            msg += f" Opened by: {emp}."
        if opened_at:
            msg += f" Opened at: {opened_at}."
        raise RuntimeError(msg)

    emp_id = None
    if str(employee_name or "").strip():
        emp_id = ensure_employee(employee_name)

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            rate = float(lbp_per_usd or 0.0)
        except Exception:
            rate = 0.0
        if rate <= 0:
            rate = 89500.0
        try:
            usd = float(opening_usd if opening_usd is not None else opening_cash or 0.0)
        except Exception:
            usd = 0.0
        try:
            lbp = float(opening_lbp or 0.0)
        except Exception:
            lbp = 0.0
        usd = round(max(0.0, usd))
        lbp = round(max(0.0, lbp))
        opening_total = round(usd + (lbp / rate))
        opened_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        shift_date = opened_at[:10]

        if shift_code is None:
            cur.execute(
                """
                SELECT COUNT(*) + 1 AS next_seq
                FROM cash_shifts
                WHERE COALESCE(NULLIF(shift_date, ''), substr(COALESCE(opened_at, ''), 1, 10)) = ?
                """,
                (shift_date,),
            )
            try:
                shift_seq = int((cur.fetchone() or {})["next_seq"] or 1)
            except Exception:
                shift_seq = 1
            shift_code = str(shift_seq)
            shift_seq_val = shift_seq
        else:
            try:
                shift_seq_val = int(shift_code)
            except Exception:
                shift_seq_val = 1

        cur.execute(
            """INSERT INTO cash_shifts(
                   opened_at, opening_cash, notes, employee_id,
                   shift_date, shift_seq, shift_code,
                   opening_usd, opening_lbp, lbp_per_usd
               )
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opened_at, float(opening_total), str(notes or ""), emp_id,
                shift_date, int(shift_seq_val), shift_code,
                float(usd), float(lbp), float(rate),
            )
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError as e:
        if conn:
            conn.rollback()
        if "idx_cash_shifts_one_open" in str(e) or "UNIQUE constraint failed" in str(e):
            raise RuntimeError("A shift is already open.") from e
        raise
    finally:
        if conn:
            conn.close()

def close_shift(shift_id, closing_cash=0.0, notes="", closing_usd=None, closing_lbp=0.0, lbp_per_usd=None):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT id, closed_at, lbp_per_usd FROM cash_shifts WHERE id = ? LIMIT 1", (int(shift_id),))
        shift_row = cur.fetchone()
        if not shift_row:
            raise ValueError("Shift not found.")
        if shift_row["closed_at"] is not None:
            raise ValueError("Shift is already closed.")
        try:
            rate = float(lbp_per_usd or 0.0)
        except Exception:
            rate = 0.0
        if rate <= 0:
            try:
                rate = float(shift_row["lbp_per_usd"] or 0.0)
            except Exception:
                rate = 0.0
        if rate <= 0:
            rate = 89500.0
        try:
            usd = float(closing_usd if closing_usd is not None else closing_cash or 0.0)
        except Exception:
            usd = 0.0
        try:
            lbp = float(closing_lbp or 0.0)
        except Exception:
            lbp = 0.0
        usd = round(max(0.0, usd))
        lbp = round(max(0.0, lbp))
        closing_total = round(usd + (lbp / rate))

        cur.execute("""
            UPDATE cash_shifts
            SET closed_at = ?, closing_cash = ?, closing_usd = ?, closing_lbp = ?, lbp_per_usd = ?,
                notes = COALESCE(notes,'') || ?
            WHERE id = ?
              AND closed_at IS NULL
        """, (_now_iso(), float(closing_total), float(usd), float(lbp), float(rate),
              (("\n" + notes) if notes else ""), int(shift_id)))
        if cur.rowcount != 1:
            raise ValueError("Shift is already closed.")
        conn.commit()
        return True
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_shifts(limit=60):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            ORDER BY cs.id DESC
            LIMIT ?
        """, (int(limit),))
        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def _employee_id_name_for_cash_movement(cur, employee_name: str, fallback_name: str = "") -> tuple[int | None, str]:
    name = str(employee_name or "").strip() or str(fallback_name or "").strip()
    if not name:
        return None, ""
    cur.execute("SELECT id, name FROM employees WHERE name = ? LIMIT 1", (name,))
    row = cur.fetchone()
    if row:
        return int(row["id"]), str(row["name"] or name)
    return None, name


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
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS shift_employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.id = ?
            LIMIT 1
        """, (int(shift_id),))
        shift = cur.fetchone()
        if not shift:
            raise ValueError("Shift not found.")
        if shift["closed_at"] is not None:
            raise ValueError("Shift is already closed.")

        try:
            rate = float(lbp_per_usd or shift["lbp_per_usd"] or 0.0)
        except Exception:
            rate = 0.0
        if rate <= 0:
            rate = 89500.0

        try:
            usd = float(closing_usd if closing_usd is not None else closing_cash or 0.0)
        except Exception:
            usd = 0.0
        try:
            lbp = float(closing_lbp or 0.0)
        except Exception:
            lbp = 0.0
        try:
            out_usd = float(takeout_usd or 0.0)
            out_lbp = float(takeout_lbp or 0.0)
        except Exception as exc:
            raise ValueError("Cash taken out must be a number.") from exc

        usd = round(max(0.0, usd))
        lbp = round(max(0.0, lbp))
        out_usd = round(max(0.0, out_usd), 2)
        out_lbp = round(max(0.0, out_lbp), 2)
        closing_total = round(usd + (lbp / rate))

        movement_id = None
        if out_usd > 0 or out_lbp > 0:
            employee_id, final_employee_name = _employee_id_name_for_cash_movement(
                cur,
                employee_name,
                shift["shift_employee_name"] or "",
            )
            amount_value = round(out_usd + (out_lbp / rate), 2)
            cur.execute("""
                INSERT INTO cash_movements (
                    created_at, shift_id, movement_type, amount_usd, amount_lbp,
                    lbp_per_usd, amount_value, reason, employee_id, employee_name, notes
                )
                VALUES (?, ?, 'OUT', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                _now_iso(),
                int(shift_id),
                float(out_usd),
                float(out_lbp),
                float(rate),
                float(amount_value),
                str(takeout_reason or "End of day close cash removed").strip(),
                employee_id,
                final_employee_name,
                str(takeout_notes or "").strip(),
            ))
            movement_id = int(cur.lastrowid)

        cur.execute("""
            UPDATE cash_shifts
            SET closed_at = ?, closing_cash = ?, closing_usd = ?, closing_lbp = ?, lbp_per_usd = ?,
                notes = COALESCE(notes,'') || ?
            WHERE id = ?
              AND closed_at IS NULL
        """, (_now_iso(), float(closing_total), float(usd), float(lbp), float(rate),
              (("\n" + notes) if notes else ""), int(shift_id)))
        if cur.rowcount != 1:
            raise ValueError("Shift is already closed.")
        conn.commit()
        return {"ok": True, "movement_id": movement_id}
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def _cash_movement_totals_for_shift_cur(cur, shift_id) -> dict:
    out = {
        "cash_out_value": 0.0,
        "cash_out_usd": 0.0,
        "cash_out_lbp": 0.0,
        "cash_out_count": 0,
        "cash_in_value": 0.0,
        "cash_in_usd": 0.0,
        "cash_in_lbp": 0.0,
        "cash_in_count": 0,
    }
    try:
        cur.execute("""
            SELECT
                UPPER(COALESCE(movement_type, 'OUT')) AS movement_type,
                COALESCE(SUM(amount_value), 0) AS amount_value,
                COALESCE(SUM(amount_usd), 0) AS amount_usd,
                COALESCE(SUM(amount_lbp), 0) AS amount_lbp,
                COUNT(*) AS count
            FROM cash_movements
            WHERE shift_id = ?
            GROUP BY UPPER(COALESCE(movement_type, 'OUT'))
        """, (int(shift_id),))
        for row in cur.fetchall():
            mtype = str(row["movement_type"] or "OUT").upper()
            prefix = "cash_in" if mtype == "IN" else "cash_out"
            out[f"{prefix}_value"] += float(row["amount_value"] or 0.0)
            out[f"{prefix}_usd"] += float(row["amount_usd"] or 0.0)
            out[f"{prefix}_lbp"] += float(row["amount_lbp"] or 0.0)
            out[f"{prefix}_count"] += int(row["count"] or 0)
    except Exception:
        pass
    out["net_movement_value"] = float(out["cash_in_value"] - out["cash_out_value"])
    out["net_movement_usd"] = float(out["cash_in_usd"] - out["cash_out_usd"])
    out["net_movement_lbp"] = float(out["cash_in_lbp"] - out["cash_out_lbp"])
    return out


def cash_movement_totals_for_shift(shift_id) -> dict:
    conn = None
    try:
        conn = get_conn()
        return _cash_movement_totals_for_shift_cur(conn.cursor(), shift_id)
    finally:
        if conn:
            conn.close()


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
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("Reason is required.")

    mtype = str(movement_type or "OUT").strip().upper()
    if mtype not in ("OUT", "IN"):
        raise ValueError("Movement type must be OUT or IN.")

    try:
        usd = float(amount_usd or 0.0)
        lbp = float(amount_lbp or 0.0)
    except Exception as exc:
        raise ValueError("Amount must be a number.") from exc
    usd = round(max(0.0, usd), 2)
    lbp = round(max(0.0, lbp), 2)
    if usd <= 0 and lbp <= 0:
        raise ValueError("Enter a cash amount.")

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT cs.*, e.name AS shift_employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.id = ?
            LIMIT 1
        """, (int(shift_id),))
        shift = cur.fetchone()
        if not shift:
            raise ValueError("Shift not found.")
        if shift["closed_at"] is not None:
            raise ValueError("Cannot record cash movement on a closed shift.")

        try:
            rate = float(lbp_per_usd or shift["lbp_per_usd"] or 0.0)
        except Exception:
            rate = 0.0
        if rate <= 0:
            rate = 89500.0

        employee_id, final_employee_name = _employee_id_name_for_cash_movement(
            cur,
            employee_name,
            shift["shift_employee_name"] or "",
        )
        amount_value = round(usd + (lbp / rate), 2)

        cur.execute("""
            INSERT INTO cash_movements (
                created_at, shift_id, movement_type, amount_usd, amount_lbp,
                lbp_per_usd, amount_value, reason, employee_id, employee_name, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            _now_iso(),
            int(shift_id),
            mtype,
            float(usd),
            float(lbp),
            float(rate),
            float(amount_value),
            reason,
            employee_id,
            final_employee_name,
            str(notes or "").strip(),
        ))
        conn.commit()
        return int(cur.lastrowid)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def list_cash_movements(shift_id=None, day_str=None, limit=500):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        where = []
        params = []
        if shift_id is not None:
            where.append("shift_id = ?")
            params.append(int(shift_id))
        if day_str:
            start, end = _sql_bounds_inclusive(str(day_str), str(day_str))
            where.append("created_at >= ? AND created_at < ?")
            params.extend([start, end])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit or 500))
        cur.execute(f"""
            SELECT *
            FROM cash_movements
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT ?
        """, params)
        return cur.fetchall()
    finally:
        if conn:
            conn.close()



def _parse_exchange_credit_from_notes(notes: str) -> float:
    """Extract store-credit tender from notes; returns 0.0 if missing."""
    try:
        found = 0.0
        n = str(notes or "")
        for part in n.split(";"):
            part = part.strip()
            if part.startswith("EXCHANGE_CREDIT_APPLIED="):
                found = max(found, float(part.split("=", 1)[1] or 0.0))
            elif part.startswith("BON_CREDIT_APPLIED="):
                found = max(found, float(part.split("=", 1)[1] or 0.0))
        return found
    except Exception:
        pass
    return 0.0


def _sales_has_column(cur, col: str) -> bool:
    try:
        info = _table_info(cur, "sales")
        for r in info:
            if str(r[1]) == str(col):
                return True
    except Exception:
        pass
    return False


def _compute_cash_paid_for_sale_row(r: dict, has_cash_paid_col: bool, has_total_sales_col: bool, has_store_credit_used_col: bool) -> float:
    """Compute how much NEW CASH was collected for a sale row.

    Rule:
      - EXCHANGE / STORE_CREDIT / CARD / DEBIT / CREDIT_CARD / WHISH => 0 cash to drawer
      - CASH => amount_due after applying store credit (if any)
    Works even on older DBs that don't have cash_paid/store_credit_used columns by parsing notes.
    """
    try:
        pm = str(r.get("payment_method") or "").strip().upper()
    except Exception:
        pm = ""

    if pm in ("EXCHANGE", "STORE_CREDIT", "CARD", "DEBIT", "CREDIT_CARD", "WHISH"):
        return 0.0

    # Prefer stored cash_paid if available (new schema)
    if has_cash_paid_col:
        try:
            return max(0.0, float(r.get("cash_paid") or 0.0))
        except Exception:
            pass

    # Otherwise compute from totals + parsed credit
    try:
        total_amount = float(r.get("total_amount") or 0.0)
    except Exception:
        total_amount = 0.0

    total_sales = None
    if has_total_sales_col:
        try:
            total_sales = float(r.get("total_sales") or 0.0)
        except Exception:
            total_sales = None

    store_credit_used = None
    if has_store_credit_used_col:
        try:
            store_credit_used = float(r.get("store_credit_used") or 0.0)
        except Exception:
            store_credit_used = None

    if store_credit_used is None:
        store_credit_used = _parse_exchange_credit_from_notes(r.get("notes") or "")

    base_total = total_sales if (total_sales is not None and total_sales > 0) else total_amount
    if base_total < 0:
        base_total = 0.0
    if store_credit_used < 0:
        store_credit_used = 0.0
    if store_credit_used > base_total:
        store_credit_used = base_total

    amount_due = base_total - store_credit_used

    # If DB already stores amount_due into total_amount (newer logic), this will still work:
    # base_total will be total_sales when present; otherwise total_amount (amount_due).
    if (total_sales is None) and (has_total_sales_col is False):
        # In very old schema we can't distinguish merch total vs due.
        # If notes contains exchange credit, assume total_amount was merch total and subtract it.
        if store_credit_used > 0:
            amount_due = max(0.0, total_amount - store_credit_used)
        else:
            amount_due = total_amount

    return max(0.0, float(amount_due))


def _compute_new_money_for_sale_row(r: dict, has_total_sales_col: bool, has_store_credit_used_col: bool) -> float:
    """Compute new money collected by any tender, excluding store credit/bon/exchange credit."""
    try:
        pm = str(r.get("payment_method") or "").strip().upper()
    except Exception:
        pm = ""
    if pm in ("EXCHANGE", "STORE_CREDIT"):
        return 0.0

    try:
        total_amount = float(r.get("total_amount") or 0.0)
    except Exception:
        total_amount = 0.0

    # Current schema stores amount due/new money in total_amount. For older rows
    # where total_amount may still be gross, subtract the recorded credit.
    credit = 0.0
    if has_store_credit_used_col:
        try:
            credit = float(r.get("store_credit_used") or 0.0)
        except Exception:
            credit = 0.0
    if credit <= 0:
        credit = _parse_exchange_credit_from_notes(r.get("notes") or "")

    try:
        gross = float(r.get("total_sales") or 0.0) if has_total_sales_col else 0.0
    except Exception:
        gross = 0.0
    if gross > 0 and credit > 0 and abs(total_amount - gross) < 0.005:
        return max(0.0, gross - credit)
    return max(0.0, total_amount)


def sales_totals_for_shift(shift_id):
    """Return cash sales totals for a shift.

    IMPORTANT: cash drawer totals track ONLY *new cash collected*.
    Store-credit exchanges do NOT add cash to the drawer.
    This function is schema-safe: it works even if older DBs are missing newer columns.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        # Detect schema capabilities
        has_cash_paid = _sales_has_column(cur, "cash_paid")
        has_total_sales = _sales_has_column(cur, "total_sales")
        has_store_credit_used = _sales_has_column(cur, "store_credit_used")

        cur.execute(
            """
            SELECT *
            FROM sales
            WHERE shift_id = ?
            """,
            (int(shift_id),),
        )
        rows = cur.fetchall()

        cash_sales = 0.0
        merchandise_sales = 0.0
        new_money_sales = 0.0
        store_credit_used = 0.0
        orders = 0
        for r in rows:
            rd = dict(r)
            orders += 1
            cash_sales += float(_compute_cash_paid_for_sale_row(rd, has_cash_paid, has_total_sales, has_store_credit_used))
            new_money_sales += float(_compute_new_money_for_sale_row(rd, has_total_sales, has_store_credit_used))
            try:
                credit = float(rd.get("store_credit_used") or 0.0) if has_store_credit_used else _parse_exchange_credit_from_notes(rd.get("notes") or "")
            except Exception:
                credit = 0.0
            try:
                total_sales = float(rd.get("total_sales") or 0.0) if has_total_sales else 0.0
            except Exception:
                total_sales = 0.0
            try:
                total_amount = float(rd.get("total_amount") or 0.0)
            except Exception:
                total_amount = 0.0
            merchandise_sales += total_sales if total_sales > 0 else (total_amount + max(0.0, credit))
            store_credit_used += max(0.0, credit)

        cash_refunds = 0.0
        returns_total = 0.0
        try:
            cur.execute(
                """
                SELECT COALESCE(SUM(cash_refund), 0) AS cash_refunds,
                       COALESCE(SUM(total_return_amount), 0) AS returns_total
                FROM returns
                WHERE shift_id = ?
                  AND COALESCE(is_voided, 0) = 0
                """,
                (int(shift_id),),
            )
            ret_row = cur.fetchone()
            if ret_row:
                cash_refunds = float(ret_row["cash_refunds"] or 0.0)
                returns_total = float(ret_row["returns_total"] or 0.0)
        except Exception:
            pass

        return {
            "cash_sales": float(cash_sales),
            "new_money_sales": float(new_money_sales),
            "merchandise_sales": float(merchandise_sales),
            "store_credit_used": float(store_credit_used),
            "cash_refunds": float(cash_refunds),
            "returns_total": float(returns_total),
            "net_revenue": float(new_money_sales - cash_refunds),
            "orders": int(orders),
        }
    finally:
        if conn:
            conn.close()

def shift_summary(shift_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT cs.*, e.name AS employee_name
            FROM cash_shifts cs
            LEFT JOIN employees e ON e.id = cs.employee_id
            WHERE cs.id = ?
        """, (int(shift_id),))
        s = cur.fetchone()
        if not s:
            return {}

        totals = sales_totals_for_shift(shift_id)
        movements = _cash_movement_totals_for_shift_cur(cur, shift_id)
        opening = float(s["opening_cash"] or 0)
        closing = float(s["closing_cash"] or 0) if s["closing_cash"] is not None else None
        expected = opening + totals["cash_sales"] - float(totals.get("cash_refunds", 0.0) or 0.0) + float(movements.get("net_movement_value", 0.0) or 0.0)
        diff = (closing - expected) if closing is not None else None

        try:
            rate = float(s["lbp_per_usd"] or 0.0)
        except Exception:
            rate = 0.0
        if rate <= 0:
            rate = 89500.0

        opening_usd = float(s["opening_usd"] or 0.0)
        opening_lbp = float(s["opening_lbp"] or 0.0)
        if opening_usd <= 0 and opening_lbp <= 0 and opening > 0:
            opening_usd = opening

        closing_usd = None
        closing_lbp = None
        if closing is not None:
            closing_usd = float(s["closing_usd"] or 0.0)
            closing_lbp = float(s["closing_lbp"] or 0.0)
            if closing_usd <= 0 and closing_lbp <= 0 and closing > 0:
                closing_usd = closing

        expected_usd_cash = (
            opening_usd
            + float(totals["cash_sales"] or 0.0)
            - float(totals.get("cash_refunds", 0.0) or 0.0)
            + float(movements.get("net_movement_usd", 0.0) or 0.0)
        )
        expected_lbp_cash = opening_lbp + float(movements.get("net_movement_lbp", 0.0) or 0.0)
        usd_difference = (closing_usd - expected_usd_cash) if closing_usd is not None else None
        lbp_difference = (closing_lbp - expected_lbp_cash) if closing_lbp is not None else None
        lbp_difference_usd = (lbp_difference / rate) if lbp_difference is not None else None

        return {
            "shift": dict(s),
            "opening_cash": opening,
            "closing_cash": closing,
            "cash_sales": totals["cash_sales"],
            "new_money_sales": float(totals.get("new_money_sales", 0.0) or 0.0),
            "merchandise_sales": float(totals.get("merchandise_sales", 0.0) or 0.0),
            "store_credit_used": float(totals.get("store_credit_used", 0.0) or 0.0),
            "returns_total": float(totals.get("returns_total", 0.0) or 0.0),
            "cash_refunds": float(totals.get("cash_refunds", 0.0) or 0.0),
            "net_revenue": float(totals.get("net_revenue", 0.0) or 0.0),
            "orders": totals["orders"],
            "expected_cash": expected,
            "difference": diff,
            "cash_out_value": float(movements.get("cash_out_value", 0.0) or 0.0),
            "cash_out_usd": float(movements.get("cash_out_usd", 0.0) or 0.0),
            "cash_out_lbp": float(movements.get("cash_out_lbp", 0.0) or 0.0),
            "cash_out_count": int(movements.get("cash_out_count", 0) or 0),
            "cash_in_value": float(movements.get("cash_in_value", 0.0) or 0.0),
            "cash_in_usd": float(movements.get("cash_in_usd", 0.0) or 0.0),
            "cash_in_lbp": float(movements.get("cash_in_lbp", 0.0) or 0.0),
            "cash_in_count": int(movements.get("cash_in_count", 0) or 0),
            "net_movement_value": float(movements.get("net_movement_value", 0.0) or 0.0),
            "lbp_per_usd": rate,
            "opening_usd": opening_usd,
            "opening_lbp": opening_lbp,
            "closing_usd": closing_usd,
            "closing_lbp": closing_lbp,
            "expected_usd_cash": expected_usd_cash,
            "expected_lbp_cash": expected_lbp_cash,
            "usd_difference": usd_difference,
            "lbp_difference": lbp_difference,
            "lbp_difference_usd": lbp_difference_usd,
        }
    finally:
        if conn:
            conn.close()


# ---------------- SALES HISTORY (BY DATE) ----------------

def _sql_bounds_inclusive(start_date, end_date):
    sd = datetime.strptime(start_date, "%Y-%m-%d").date()
    ed = datetime.strptime(end_date, "%Y-%m-%d").date()
    end_next = ed + timedelta(days=1)
    return sd.strftime("%Y-%m-%d"), end_next.strftime("%Y-%m-%d")



def list_sales_for_day(day_str, limit=500):
    """List sales for a specific day (newest first).

    IMPORTANT ACCOUNTING RULE:
      - The "Total" shown in the Cash Drawer / Daily Sales History must be NET DAILY SALES
        (i.e., NEW MONEY COLLECTED), not merchandise value.
      - Exchange credit (store credit used) never increases the drawer.
      - Therefore we compute/return a schema-safe `cash_paid` for every row.

    Returns list of dict rows with at least:
      id, created_at, cash_paid, total_amount, payment_method, shift_id, receipt_code, is_exchange, returns_amount
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        s0, e1 = _sql_bounds_inclusive(day_str, day_str)

        has_cash_paid = _sales_has_column(cur, "cash_paid")
        has_total_sales = _sales_has_column(cur, "total_sales")
        has_store_credit_used = _sales_has_column(cur, "store_credit_used")
        has_is_exchange = _sales_has_column(cur, "is_exchange")
        has_receipt_code = _sales_has_column(cur, "receipt_code")

        cur.execute(
            """
            SELECT
                s.*,
                cs.shift_code AS shift_code,
                COALESCE(rsum.returns_amount, 0) AS returns_amount
            FROM sales s
            LEFT JOIN cash_shifts cs ON cs.id = s.shift_id
            LEFT JOIN (
                SELECT original_sale_id, SUM(total_return_amount) AS returns_amount
                FROM returns
                WHERE COALESCE(is_voided, 0) = 0
                GROUP BY original_sale_id
            ) rsum ON rsum.original_sale_id = s.id
            WHERE datetime(s.created_at) >= datetime(?)
              AND datetime(s.created_at) < datetime(?)
            ORDER BY datetime(s.created_at) DESC
            LIMIT ?
            """,
            (s0, e1, int(limit)),
        )

        out = []
        for row in cur.fetchall():
            d = dict(row)
            # compute schema-safe cash_paid
            d["cash_paid"] = float(_compute_cash_paid_for_sale_row(d, has_cash_paid, has_total_sales, has_store_credit_used))

            # normalize is_exchange (old DBs might not have it)
            if not has_is_exchange:
                pm = str(d.get("payment_method") or "").strip().upper()
                d["is_exchange"] = 1 if pm in ("EXCHANGE", "STORE_CREDIT") else 0
            else:
                try:
                    d["is_exchange"] = int(d.get("is_exchange") or 0)
                except Exception:
                    d["is_exchange"] = 0

            if not has_receipt_code:
                d["receipt_code"] = str(d.get("receipt_code") or "")

            out.append(d)

        return out
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

def get_sale_detail(sale_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM sales WHERE id = ?", (int(sale_id),))
        sale = cur.fetchone()
        if not sale:
            return None, []

        cur.execute("""
            SELECT * FROM sale_items
            WHERE sale_id = ?
            ORDER BY id ASC
        """, (int(sale_id),))
        items = cur.fetchall()

        # Convert sqlite3.Row objects to plain dicts so callers can use .get()
        # (receipt_pdf.py and receipt_print.py expect dict-like objects)
        sale_dict = dict(sale) if isinstance(sale, sqlite3.Row) else sale
        items_dicts = [dict(r) if isinstance(r, sqlite3.Row) else r for r in items]
        return sale_dict, items_dicts
    finally:
        if conn:
            conn.close()




# ---------------- RETURNS / EXCHANGES ----------------

def _strip_receipt_wrappers(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    for p in ["MASKPOS|", "RCPT:", "RECEIPT:", "Receipt:", "receipt:"]:
        if s.startswith(p):
            s = s[len(p):].strip()
    return s


def resolve_receipt_scan_to_sale_id(raw: str):
    """Resolve a scanned receipt barcode to a concrete sale_id.

    Supported formats:
      - R-123                 (sale id)
      - 123                   (sale id)
      - R-YYYYMMDD-0042       (date + 4-digit receipt code)
      - R-YYYY-MM-DD-0042     (date + 4-digit receipt code)
    """
    s = _strip_receipt_wrappers(raw)
    if not s:
        return None

    # 1) Direct sale id
    t = s
    if t.lower().startswith("r-"):
        t = t[2:].strip()
    if t.isdigit():
        try:
            return int(t)
        except Exception:
            return None

    # 2) Date + receipt code
    import re
    # Accept R-20251227-0042 or R-2025-12-27-0042
    m = re.match(r"^r-?(\d{4})-?(\d{2})-?(\d{2})-(\d{1,8})$", s.strip(), flags=re.IGNORECASE)
    if m:
        y, mo, d, code = m.group(1), m.group(2), m.group(3), m.group(4)
        date_str = f"{y}-{mo}-{d}"
        code = str(code).zfill(4)  # keep your 4-digit feel
        conn = None
        try:
            conn = get_conn()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id
                FROM sales
                WHERE substr(created_at, 1, 10) = ?
                  AND receipt_code = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (date_str, code)
            )
            row = cur.fetchone()
            return int(row["id"]) if row else None
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    # 3) last resort: first number group (keeps backward compatibility)
    m2 = re.search(r"(\d+)", s)
    if m2:
        try:
            return int(m2.group(1))
        except Exception:
            return None
    return None


def get_sale_by_receipt_scan(scan_value: str):
    """Fetch sale + items using a scanned receipt barcode."""
    sale_id = resolve_receipt_scan_to_sale_id(scan_value)
    if not sale_id:
        return None, []
    return get_sale_detail_with_returns(sale_id)


def _returned_qty_map_for_sale(conn, sale_id: int) -> dict:
    """Return dict: sale_item_id -> already_returned_qty for a given original sale."""
    cur = conn.cursor()
    try:
        _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(cur, "returns", "voided_at", "TEXT")
        _ensure_column(cur, "returns", "void_notes", "TEXT")
        conn.commit()
    except Exception:
        pass
    cur.execute(
        """
        SELECT ri.sale_item_id AS sale_item_id, COALESCE(SUM(ri.qty), 0) AS returned_qty
        FROM returns r
        JOIN return_items ri ON ri.return_id = r.id
        WHERE r.original_sale_id = ? AND ri.sale_item_id IS NOT NULL
          AND COALESCE(r.is_voided, 0) = 0
        GROUP BY ri.sale_item_id
        """,
        (int(sale_id),)
    )
    out = {}
    for row in cur.fetchall() or []:
        try:
            out[int(row["sale_item_id"])] = int(row["returned_qty"] or 0)
        except Exception:
            pass
    return out


def get_sale_detail_with_returns(sale_id: int):
    """Like get_sale_detail but adds returned_qty and remaining_qty on each sale item."""
    conn = None
    try:
        conn = get_conn()
        sale, items = get_sale_detail(sale_id)
        if not sale:
            return None, []
        rmap = _returned_qty_map_for_sale(conn, int(sale_id))
        enriched = []
        for it in items or []:
            sid = int(it["id"])
            sold = int(it["qty"] or 0)
            returned = int(rmap.get(sid, 0) or 0)
            remaining = max(0, sold - returned)
            d = dict(it)
            d["returned_qty"] = returned
            d["remaining_qty"] = remaining
            enriched.append(d)
        return sale, enriched
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def create_return(original_sale_id: int, returned_lines, notes: str = ""):
    """Create an exchange return (no cash refunds).

    returned_lines: list of dicts like:
      {
        "sale_item_id": int or None,
        "product_id": int or None,
        "name": str,
        "price": float,
        "qty": int,
        "line_total": float
      }

    Returns: (return_id, total_return_amount)
    """
    if not returned_lines:
        raise ValueError("No items selected for return")

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        # Validate requested quantities against what was sold minus what was already returned.
        # This prevents "infinite" returning and stock appearing from nowhere.
        already = _returned_qty_map_for_sale(conn, int(original_sale_id))

        validated_lines = []
        for ln in returned_lines:
            sale_item_id = ln.get("sale_item_id", None)
            if sale_item_id is None:
                raise ValueError("Return line is missing sale_item_id")

            req_qty = int(ln.get("qty") or 0)
            if req_qty <= 0:
                continue

            # Fetch the sale item to confirm it belongs to this sale
            cur.execute(
                "SELECT id, sale_id, product_id, name, price, qty, line_total FROM sale_items WHERE id = ?",
                (int(sale_item_id),)
            )
            si = cur.fetchone()
            if not si or int(si["sale_id"]) != int(original_sale_id):
                raise ValueError("Invalid sale item for this receipt")

            sold_qty = int(si["qty"] or 0)
            prev = int(already.get(int(sale_item_id), 0) or 0)
            remaining = max(0, sold_qty - prev)
            if remaining <= 0:
                raise ValueError(f"{si['name']} is already fully returned.")
            if req_qty > remaining:
                raise ValueError(f"Cannot return {req_qty}. Remaining returnable qty is {remaining}.")

            # Prefer the recorded net line_total when present. It captures real discounts.
            # Exchange credit is a tender, not a discount, so it must not reduce the
            # replacement item's value if that item is exchanged again later.
            unit_price = None
            try:
                # Convert Row to dict to safely access fields with .get()
                si_dict = dict(si) if not isinstance(si, dict) else si
                lt = si_dict.get("line_total", None)
                if lt is not None and sold_qty > 0:
                    unit_price = float(lt) / float(sold_qty)
            except Exception:
                unit_price = None

            if unit_price is None:
                unit_price = float(si["price"] or 0.0)

            # Use the requested quantity to compute returned line total (rounded to cents)
            unit_price = round(unit_price, 2)
            line_total = round(unit_price * req_qty, 2)

            validated_lines.append({
                "sale_item_id": int(si["id"]),
                "product_id": int(si["product_id"]) if si["product_id"] is not None else None,
                "name": str(si["name"] or ""),
                "price": unit_price,
                "qty": req_qty,
                "line_total": line_total,
            })

        if not validated_lines:
            raise ValueError("No valid items selected for return")

        # attach to current open shift (if any)
        cur.execute(
            """
            SELECT id
            FROM cash_shifts
            WHERE closed_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        )
        r = cur.fetchone()
        shift_id = int(r["id"]) if r else None

        if shift_id is None:
            raise RuntimeError("No open shift. Open a shift first.")

        total_return = 0.0
        for ln in validated_lines:
            total_return += float(ln.get("line_total") or 0)

        # Insert return row (schema-safe)
        ret_cols = set(_table_cols(cur, "returns"))
        cols = ["original_sale_id", "created_at", "total_return_amount", "shift_id", "notes"]
        vals = [int(original_sale_id), _now_iso(), float(total_return), shift_id, str(notes or "")]
        if "cash_refund" in ret_cols:
            cols.append("cash_refund")
            vals.append(0.0)
        if "credit_refund" in ret_cols:
            cols.append("credit_refund")
            vals.append(float(total_return))
        cur.execute(
            f"INSERT INTO returns ({', '.join(cols)}) VALUES ({', '.join(['?']*len(cols))})",
            tuple(vals),
        )

        return_id = int(cur.lastrowid)

        for ln in validated_lines:
            pid = ln.get("product_id", None)
            qty = int(ln.get("qty") or 0)
            price = float(ln.get("price") or 0)
            lt = float(ln.get("line_total") or 0)
            name = str(ln.get("name") or "")
            sale_item_id = ln.get("sale_item_id", None)

            cur.execute(
                """
                INSERT INTO return_items (return_id, sale_item_id, product_id, name, price, qty, line_total)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (return_id, sale_item_id, pid, name, price, qty, lt)
            )

            # restock inventory (if product_id exists)
            if pid is not None and qty:
                cur.execute(
                    "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                    (qty, int(pid))
                )

        conn.commit()
        return return_id, float(total_return)

    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_return_detail(return_id: int):
    """Return a saved return and its database-validated line items."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT * FROM returns WHERE id = ?", (int(return_id),))
        ret = cur.fetchone()
        if not ret:
            return None, []
        cur.execute(
            "SELECT * FROM return_items WHERE return_id = ? ORDER BY id ASC",
            (int(return_id),),
        )
        items = cur.fetchall()
        return (
            dict(ret) if isinstance(ret, sqlite3.Row) else ret,
            [dict(row) if isinstance(row, sqlite3.Row) else row for row in items],
        )
    finally:
        if conn:
            conn.close()


def list_returns_for_sale(original_sale_id: int, include_voided: bool = False):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "voided_at", "TEXT")
            _ensure_column(cur, "returns", "void_notes", "TEXT")
            conn.commit()
        except Exception:
            pass
        where = "WHERE original_sale_id = ?"
        params = [int(original_sale_id)]
        if not include_voided:
            where += " AND COALESCE(is_voided, 0) = 0"
        cur.execute(
            f"""
            SELECT id, original_sale_id, created_at, total_return_amount, shift_id, notes,
                   COALESCE(cash_refund, 0) AS cash_refund,
                   COALESCE(credit_refund, 0) AS credit_refund,
                   COALESCE(is_voided, 0) AS is_voided,
                   voided_at,
                   void_notes
            FROM returns
            {where}
            ORDER BY id DESC
            """,
            tuple(params),
        )
        return [dict(r) if isinstance(r, sqlite3.Row) else r for r in (cur.fetchall() or [])]
    finally:
        if conn:
            conn.close()


def list_recent_returns(limit: int = 20, include_voided: bool = False):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "voided_at", "TEXT")
            _ensure_column(cur, "returns", "void_notes", "TEXT")
            conn.commit()
        except Exception:
            pass
        where = ""
        if not include_voided:
            where = "WHERE COALESCE(r.is_voided, 0) = 0"
        cur.execute(
            f"""
            SELECT r.id, r.original_sale_id, r.created_at, r.total_return_amount, r.shift_id, r.notes,
                   COALESCE(r.cash_refund, 0) AS cash_refund,
                   COALESCE(r.credit_refund, 0) AS credit_refund,
                   COALESCE(r.is_voided, 0) AS is_voided,
                   r.voided_at,
                   r.void_notes,
                   s.receipt_code
            FROM returns r
            LEFT JOIN sales s ON s.id = r.original_sale_id
            {where}
            ORDER BY r.id DESC
            LIMIT ?
            """,
            (max(1, int(limit or 20)),),
        )
        return [dict(r) if isinstance(r, sqlite3.Row) else r for r in (cur.fetchall() or [])]
    finally:
        if conn:
            conn.close()


def normalize_bon_code(raw: str) -> str:
    """Normalize a scanned/typed bon code to BON-YYYYMMDD-0001 form."""
    s = str(raw or "").strip().upper()
    if not s:
        return ""
    compact = re.sub(r"[^A-Z0-9]", "", s)
    if compact.startswith("BON") and len(compact) >= 12:
        date_part = compact[3:11]
        seq_part = compact[11:]
        if date_part.isdigit() and seq_part.isdigit():
            return f"BON-{date_part}-{int(seq_part):04d}"
    if s.startswith("BON-"):
        return s
    return ""


def _ensure_bon_runtime_schema(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        original_amount REAL NOT NULL DEFAULT 0,
        remaining_amount REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        return_id INTEGER,
        original_sale_id INTEGER,
        shift_id INTEGER,
        issued_by_employee_id INTEGER,
        issued_by_name TEXT,
        signature_text TEXT,
        notes TEXT,
        redeemed_at TEXT,
        last_redeemed_at TEXT,
        voided_at TEXT,
        void_notes TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS bon_redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bon_id INTEGER NOT NULL,
        sale_id INTEGER,
        created_at TEXT NOT NULL,
        amount REAL NOT NULL DEFAULT 0,
        shift_id INTEGER,
        notes TEXT
    )
    """)
    _ensure_column(cur, "bons", "remaining_amount", "REAL NOT NULL DEFAULT 0")
    _ensure_column(cur, "bons", "status", "TEXT NOT NULL DEFAULT 'ACTIVE'")
    _ensure_column(cur, "bons", "signature_text", "TEXT")
    _ensure_column(cur, "bons", "last_redeemed_at", "TEXT")
    _ensure_column(cur, "bons", "voided_at", "TEXT")
    _ensure_column(cur, "bons", "void_notes", "TEXT")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_code ON bons(code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_status ON bons(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bons_return ON bons(return_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bon_redemptions_bon ON bon_redemptions(bon_id)")


def _bon_select_sql(where_sql: str = "") -> str:
    return f"""
        SELECT b.*,
               r.created_at AS return_created_at,
               r.total_return_amount AS return_total_amount,
               s.receipt_code AS original_receipt_code,
               s.created_at AS original_sale_created_at
        FROM bons b
        LEFT JOIN returns r ON r.id = b.return_id
        LEFT JOIN sales s ON s.id = b.original_sale_id
        {where_sql}
    """


def _bon_dict(row):
    return dict(row) if isinstance(row, sqlite3.Row) else row


def _next_bon_code(cur, created_at: str) -> str:
    day = str(created_at or _now_iso())[:10].replace("-", "")
    prefix = f"BON-{day}-"
    cur.execute("SELECT code FROM bons WHERE code LIKE ? ORDER BY code DESC LIMIT 1", (prefix + "%",))
    row = cur.fetchone()
    next_seq = 1
    if row:
        try:
            next_seq = int(str(row["code"]).rsplit("-", 1)[-1]) + 1
        except Exception:
            next_seq = 1
    for seq in range(next_seq, next_seq + 1000):
        code = f"{prefix}{seq:04d}"
        cur.execute("SELECT 1 FROM bons WHERE code = ? LIMIT 1", (code,))
        if not cur.fetchone():
            return code
    return f"{prefix}{datetime.now().strftime('%H%M%S')}"


def _employee_id_for_bon(cur, shift_id, issued_by_name: str):
    employee_id = None
    employee_name = str(issued_by_name or "").strip()
    if shift_id is not None:
        try:
            cur.execute("""
                SELECT cs.employee_id, e.name
                FROM cash_shifts cs
                LEFT JOIN employees e ON e.id = cs.employee_id
                WHERE cs.id = ?
                LIMIT 1
            """, (int(shift_id),))
            row = cur.fetchone()
            if row:
                employee_id = row["employee_id"]
                if not employee_name:
                    employee_name = str(row["name"] or "").strip()
        except Exception:
            pass
    if employee_name:
        cur.execute("SELECT id FROM employees WHERE name = ? LIMIT 1", (employee_name,))
        row = cur.fetchone()
        if row:
            employee_id = int(row["id"])
        else:
            cur.execute(
                "INSERT INTO employees (name, pin, is_active, created_at) VALUES (?, '', 1, ?)",
                (employee_name, _now_iso()),
            )
            employee_id = int(cur.lastrowid)
    return employee_id, employee_name


def create_bon(return_id: int | None = None, issued_by_name: str = "", signature_text: str = "", notes: str = "", amount=None):
    """Create one digital store-credit bon.

    If return_id is provided, the bon amount comes from that return and only one
    active bon can exist for it. If return_id is blank, amount creates a manual
    store-credit bon that can be issued from Offers.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        _ensure_bon_runtime_schema(cur)

        ret_id = None
        if return_id not in (None, ""):
            try:
                ret_id = int(return_id)
            except Exception:
                raise ValueError("Return id is invalid.")
            if ret_id <= 0:
                ret_id = None

        ret = None
        original_sale_id = None
        shift_id = None
        if ret_id is not None:
            cur.execute("SELECT * FROM returns WHERE id = ?", (ret_id,))
            ret = cur.fetchone()
            if not ret:
                raise ValueError("Return not found.")
            if int(ret["is_voided"] or 0):
                raise ValueError("Cannot issue a bon for a voided return.")

            cur.execute(_bon_select_sql("WHERE b.return_id = ? AND COALESCE(b.status, 'ACTIVE') != 'VOID' LIMIT 1"), (ret_id,))
            existing = cur.fetchone()
            if existing:
                return _bon_dict(existing)

            bon_amount = round(max(0.0, float(ret["total_return_amount"] or 0.0)), 2)
            original_sale_id = int(ret["original_sale_id"])
            shift_id = ret["shift_id"]
            if bon_amount <= 0.005:
                raise ValueError("Return amount must be greater than zero.")
        else:
            try:
                bon_amount = round(max(0.0, float(amount or 0.0)), 2)
            except Exception:
                raise ValueError("Bon amount must be a number.")
            if bon_amount <= 0.005:
                raise ValueError("Bon amount must be greater than zero.")
            try:
                cur.execute("""
                    SELECT cs.id, cs.employee_id, e.name AS employee_name
                    FROM cash_shifts cs
                    LEFT JOIN employees e ON e.id = cs.employee_id
                    WHERE cs.closed_at IS NULL
                    ORDER BY cs.id DESC
                    LIMIT 1
                """)
                shift = cur.fetchone()
                if shift:
                    shift_id = shift["id"]
                    if not str(issued_by_name or "").strip():
                        issued_by_name = str(shift["employee_name"] or "").strip()
            except Exception:
                shift_id = None

        created_at = _now_iso()
        employee_id, employee_name = _employee_id_for_bon(cur, shift_id, issued_by_name)
        if ret_id is None and not employee_name:
            raise ValueError("Enter the person who issued this bon.")
        signature = str(signature_text or employee_name or "").strip()
        code = _next_bon_code(cur, created_at)

        cur.execute(
            """
            INSERT INTO bons (
                code, created_at, original_amount, remaining_amount, status, return_id,
                original_sale_id, shift_id, issued_by_employee_id, issued_by_name,
                signature_text, notes
            )
            VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                created_at,
                bon_amount,
                bon_amount,
                int(ret_id) if ret_id is not None else None,
                int(original_sale_id) if original_sale_id is not None else None,
                int(shift_id) if shift_id is not None else None,
                int(employee_id) if employee_id is not None else None,
                employee_name,
                signature,
                str(notes or ""),
            ),
        )
        conn.commit()
        cur.execute(_bon_select_sql("WHERE b.code = ? LIMIT 1"), (code,))
        return _bon_dict(cur.fetchone())
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_bon_by_code(code: str):
    bon_code = normalize_bon_code(code)
    if not bon_code:
        return None
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        _ensure_bon_runtime_schema(cur)
        cur.execute(_bon_select_sql("WHERE b.code = ? LIMIT 1"), (bon_code,))
        return _bon_dict(cur.fetchone())
    finally:
        if conn:
            conn.close()


def list_bons(query: str = "", active_only: bool = False, limit: int = 200):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        _ensure_bon_runtime_schema(cur)
        where = []
        params = []
        if active_only:
            where.append("COALESCE(b.status, 'ACTIVE') = 'ACTIVE' AND COALESCE(b.remaining_amount, 0) > 0.005")
        q = str(query or "").strip()
        if q:
            like = f"%{q}%"
            where.append("(b.code LIKE ? OR b.issued_by_name LIKE ? OR s.receipt_code LIKE ?)")
            params.extend([like, like, like])
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        cur.execute(
            _bon_select_sql(where_sql) + " ORDER BY b.id DESC LIMIT ?",
            tuple(params + [max(1, int(limit or 200))]),
        )
        return [_bon_dict(r) for r in (cur.fetchall() or [])]
    finally:
        if conn:
            conn.close()


def void_bon(code: str, notes: str = ""):
    bon_code = normalize_bon_code(code)
    if not bon_code:
        raise ValueError("Bon code is invalid.")
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        _ensure_bon_runtime_schema(cur)
        cur.execute("SELECT * FROM bons WHERE code = ? LIMIT 1", (bon_code,))
        bon = cur.fetchone()
        if not bon:
            raise ValueError("Bon not found.")
        if str(bon["status"] or "").upper() == "VOID":
            raise ValueError("Bon is already voided.")
        cur.execute(
            """
            UPDATE bons
            SET status = 'VOID',
                remaining_amount = 0,
                voided_at = ?,
                void_notes = ?
            WHERE id = ?
            """,
            (_now_iso(), str(notes or ""), int(bon["id"])),
        )
        conn.commit()
        return get_bon_by_code(bon_code)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def _bon_codes_from_notes(notes: str):
    codes = []
    for part in str(notes or "").split(";"):
        part = part.strip()
        if not part.startswith("BON_CODES="):
            continue
        raw = part.split("=", 1)[1]
        for bit in raw.split(","):
            code = normalize_bon_code(bit)
            if code and code not in codes:
                codes.append(code)
    return codes


def _note_float(notes: str, key: str, default: float = 0.0) -> float:
    try:
        for part in str(notes or "").split(";"):
            part = part.strip()
            if part.startswith(key + "="):
                return float(part.split("=", 1)[1] or default)
    except Exception:
        pass
    return float(default)


def _redeem_bons_in_tx(cur, bon_codes, sale_id: int, amount: float, created_at: str, shift_id):
    _ensure_bon_runtime_schema(cur)
    codes = []
    for raw in bon_codes or []:
        code = normalize_bon_code(raw)
        if code and code not in codes:
            codes.append(code)
    amount_left = round(max(0.0, float(amount or 0.0)), 2)
    if not codes or amount_left <= 0.005:
        return []

    placeholders = ", ".join(["?"] * len(codes))
    rows = cur.execute(
        f"SELECT * FROM bons WHERE code IN ({placeholders}) ORDER BY id ASC",
        tuple(codes),
    ).fetchall() or []
    by_code = {str(r["code"]).upper(): r for r in rows}
    available = 0.0
    for code in codes:
        row = by_code.get(code.upper())
        if not row:
            raise ValueError(f"Bon not found: {code}")
        status = str(row["status"] or "ACTIVE").upper()
        remaining = round(max(0.0, float(row["remaining_amount"] or 0.0)), 2)
        if status != "ACTIVE" or remaining <= 0.005:
            raise ValueError(f"Bon is not active: {code}")
        available += remaining
    if available + 0.005 < amount_left:
        raise ValueError("Bon balance is not enough for this sale.")

    redemptions = []
    for code in codes:
        if amount_left <= 0.005:
            break
        row = by_code[code.upper()]
        remaining = round(max(0.0, float(row["remaining_amount"] or 0.0)), 2)
        use_amount = round(min(remaining, amount_left), 2)
        new_remaining = round(max(0.0, remaining - use_amount), 2)
        new_status = "USED" if new_remaining <= 0.005 else "ACTIVE"
        cur.execute(
            """
            UPDATE bons
            SET remaining_amount = ?,
                status = ?,
                last_redeemed_at = ?,
                redeemed_at = CASE WHEN ? = 'USED' THEN COALESCE(redeemed_at, ?) ELSE redeemed_at END
            WHERE id = ?
            """,
            (new_remaining, new_status, created_at, new_status, created_at, int(row["id"])),
        )
        cur.execute(
            """
            INSERT INTO bon_redemptions (bon_id, sale_id, created_at, amount, shift_id, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                int(sale_id),
                created_at,
                float(use_amount),
                int(shift_id) if shift_id is not None else None,
                "Sale redemption",
            ),
        )
        redemptions.append({"code": code, "amount": use_amount, "remaining_amount": new_remaining})
        amount_left = round(max(0.0, amount_left - use_amount), 2)
    return redemptions


def _used_bon_code_for_returns(cur, return_ids) -> str:
    ids = [int(x) for x in (return_ids or []) if x is not None]
    if not ids:
        return ""
    _ensure_bon_runtime_schema(cur)
    placeholders = ", ".join(["?"] * len(ids))
    cur.execute(
        f"""
        SELECT b.code, COALESCE(SUM(br.amount), 0) AS redeemed
        FROM bons b
        JOIN bon_redemptions br ON br.bon_id = b.id
        WHERE b.return_id IN ({placeholders})
        GROUP BY b.id, b.code
        HAVING redeemed > 0.005
        ORDER BY b.id ASC
        LIMIT 1
        """,
        tuple(ids),
    )
    row = cur.fetchone()
    return str(row["code"] or "") if row else ""


def void_return(return_id: int, notes: str = ""):
    """Void a return and reverse its inventory movement.

    The return row stays in history with is_voided=1, but it stops counting
    toward returned quantities and analytics. Stock that was added by the
    original return is subtracted back out.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "voided_at", "TEXT")
            _ensure_column(cur, "returns", "void_notes", "TEXT")
            conn.commit()
        except Exception:
            pass

        cur.execute("SELECT * FROM returns WHERE id = ?", (int(return_id),))
        ret = cur.fetchone()
        if not ret:
            raise ValueError("Return not found.")
        if int(ret["is_voided"] or 0):
            raise ValueError("Return is already voided.")
        used_bon = _used_bon_code_for_returns(cur, [int(return_id)])
        if used_bon:
            raise ValueError(f"Cannot void this return because bon {used_bon} was already used.")

        cur.execute(
            """
            SELECT product_id, qty
            FROM return_items
            WHERE return_id = ?
            """,
            (int(return_id),),
        )
        items = cur.fetchall() or []
        for it in items:
            pid = it["product_id"]
            qty = int(it["qty"] or 0)
            if pid is None or qty <= 0:
                continue
            cur.execute(
                "UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                (qty, int(pid)),
            )

        cur.execute(
            """
            UPDATE returns
            SET is_voided = 1,
                voided_at = ?,
                void_notes = ?
            WHERE id = ?
            """,
            (_now_iso(), str(notes or ""), int(return_id)),
        )
        try:
            _ensure_bon_runtime_schema(cur)
            cur.execute(
                """
                UPDATE bons
                SET status = 'VOID',
                    remaining_amount = 0,
                    voided_at = ?,
                    void_notes = ?
                WHERE return_id = ?
                  AND COALESCE(status, 'ACTIVE') != 'VOID'
                """,
                (_now_iso(), str(notes or "Return voided"), int(return_id)),
            )
        except Exception:
            pass
        conn.commit()
        return {
            "ok": True,
            "return_id": int(return_id),
            "original_sale_id": int(ret["original_sale_id"]),
            "total_return_amount": float(ret["total_return_amount"] or 0.0),
        }
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def reset_returns_for_sale(original_sale_id: int, notes: str = ""):
    """Void every active return for a sale and reverse all related stock moves."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            _ensure_column(cur, "returns", "is_voided", "INTEGER NOT NULL DEFAULT 0")
            _ensure_column(cur, "returns", "voided_at", "TEXT")
            _ensure_column(cur, "returns", "void_notes", "TEXT")
            conn.commit()
        except Exception:
            pass

        cur.execute(
            """
            SELECT id, total_return_amount
            FROM returns
            WHERE original_sale_id = ?
              AND COALESCE(is_voided, 0) = 0
            ORDER BY id ASC
            """,
            (int(original_sale_id),),
        )
        rows = cur.fetchall() or []
        if not rows:
            return {
                "ok": True,
                "original_sale_id": int(original_sale_id),
                "voided_count": 0,
                "voided_return_ids": [],
                "total_return_amount": 0.0,
            }

        return_ids = [int(r["id"]) for r in rows]
        total = sum(float(r["total_return_amount"] or 0.0) for r in rows)
        used_bon = _used_bon_code_for_returns(cur, return_ids)
        if used_bon:
            raise ValueError(f"Cannot reset returns for this sale because bon {used_bon} was already used.")

        placeholders = ", ".join(["?"] * len(return_ids))
        cur.execute(
            f"""
            SELECT product_id, COALESCE(SUM(qty), 0) AS qty
            FROM return_items
            WHERE return_id IN ({placeholders})
            GROUP BY product_id
            """,
            tuple(return_ids),
        )
        for it in cur.fetchall() or []:
            pid = it["product_id"]
            qty = int(it["qty"] or 0)
            if pid is None or qty <= 0:
                continue
            cur.execute(
                "UPDATE products SET stock_qty = stock_qty - ? WHERE id = ?",
                (qty, int(pid)),
            )

        cur.execute(
            f"""
            UPDATE returns
            SET is_voided = 1,
                voided_at = ?,
                void_notes = ?
            WHERE id IN ({placeholders})
            """,
            tuple([_now_iso(), str(notes or "")] + return_ids),
        )
        try:
            cur.execute(
                f"""
                UPDATE bons
                SET status = 'VOID',
                    remaining_amount = 0,
                    voided_at = ?,
                    void_notes = ?
                WHERE return_id IN ({placeholders})
                  AND COALESCE(status, 'ACTIVE') != 'VOID'
                """,
                tuple([_now_iso(), str(notes or "Return reset")] + return_ids),
            )
        except Exception:
            pass
        conn.commit()
        return {
            "ok": True,
            "original_sale_id": int(original_sale_id),
            "voided_count": len(return_ids),
            "voided_return_ids": return_ids,
            "total_return_amount": float(total),
        }
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()

# ---------------- DELETE SALE (RESTORE STOCK) ----------------

def delete_sale(sale_id, restore_stock=True):
    """Delete a sale and its items. If restore_stock=True, adds sold qty back to products.stock_qty."""
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        # Make sure sale exists
        cur.execute("SELECT id FROM sales WHERE id = ?", (int(sale_id),))
        if not cur.fetchone():
            return False

        # Fetch items and active returned quantities first. Deleting a sale should
        # restore only stock that is still outstanding; active returns already
        # put their quantity back on the shelf.
        cur.execute("""
            SELECT
                si.id,
                si.product_id,
                si.qty,
                COALESCE(SUM(CASE WHEN COALESCE(r.is_voided, 0) = 0 THEN ri.qty ELSE 0 END), 0) AS returned_qty
            FROM sale_items si
            LEFT JOIN return_items ri ON ri.sale_item_id = si.id
            LEFT JOIN returns r ON r.id = ri.return_id
            WHERE si.sale_id = ?
            GROUP BY si.id, si.product_id, si.qty
        """, (int(sale_id),))
        items = cur.fetchall()

        if restore_stock:
            for it in items:
                pid = it["product_id"]
                qty = max(0, int(it["qty"] or 0) - int(it["returned_qty"] or 0))
                if pid is None:
                    continue
                cur.execute(
                    "UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?",
                    (qty, int(pid))
                )

        try:
            _ensure_bon_runtime_schema(cur)
            redemptions = cur.execute(
                """
                SELECT bon_id, COALESCE(SUM(amount), 0) AS amount
                FROM bon_redemptions
                WHERE sale_id = ?
                GROUP BY bon_id
                """,
                (int(sale_id),),
            ).fetchall() or []
            for red in redemptions:
                cur.execute(
                    """
                    UPDATE bons
                    SET remaining_amount = MIN(original_amount, COALESCE(remaining_amount, 0) + ?),
                        status = CASE
                            WHEN COALESCE(remaining_amount, 0) + ? > 0.005 THEN 'ACTIVE'
                            ELSE status
                        END
                    WHERE id = ?
                    """,
                    (float(red["amount"] or 0.0), float(red["amount"] or 0.0), int(red["bon_id"])),
                )
            cur.execute("DELETE FROM bon_redemptions WHERE sale_id = ?", (int(sale_id),))
        except Exception:
            pass

        # Delete dependent return history, items, then sale.
        try:
            cur.execute(
                """
                DELETE FROM bon_redemptions
                WHERE bon_id IN (
                    SELECT b.id
                    FROM bons b
                    JOIN returns r ON r.id = b.return_id
                    WHERE r.original_sale_id = ?
                )
                """,
                (int(sale_id),),
            )
            cur.execute(
                """
                DELETE FROM bons
                WHERE return_id IN (SELECT id FROM returns WHERE original_sale_id = ?)
                """,
                (int(sale_id),),
            )
        except Exception:
            pass
        cur.execute(
            "DELETE FROM return_items WHERE return_id IN (SELECT id FROM returns WHERE original_sale_id = ?)",
            (int(sale_id),),
        )
        cur.execute("DELETE FROM returns WHERE original_sale_id = ?", (int(sale_id),))
        cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (int(sale_id),))
        cur.execute("DELETE FROM sales WHERE id = ?", (int(sale_id),))

        conn.commit()
        return True

    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ---------------- SALES (FIXED FOR NOT NULL LEGACY COLUMNS) ----------------

def create_sale(cart_lines, payment_method="CASH", customer_name="", order_discount_total=0.0, notes=""):
    if not cart_lines:
        raise ValueError("Cannot create sale with empty cart")

    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        created_at = _now_iso()

        def _line_total(l):
            if l.get("line_total") is not None:
                return float(l.get("line_total") or 0)
            if l.get("subtotal") is not None:
                return float(l.get("subtotal") or 0)
            price_ = float(l.get("price") or 0)
            qty_ = int(l.get("qty") or 0)
            return price_ * qty_

        subtotal = sum(_line_total(l) for l in cart_lines)

        # order_discount_total is a real discount (seasonal/manual). Exchange credit is tracked separately via notes.
        try:
            discount_total = float(order_discount_total or 0)
        except Exception:
            discount_total = 0.0
        if discount_total < 0:
            discount_total = 0.0
        if discount_total > subtotal:
            discount_total = float(subtotal)

        tax_total = 0.0
        shipping = 0.0

        net_sales = subtotal - discount_total
        total_sales = net_sales + shipping + tax_total

        # ---- Exchange/store-credit handling ----
        # The UI encodes exchange credit in notes like: "EXCHANGE_CREDIT_APPLIED=10.00;ORIG_SALE_ID=123"
        exchange_credit = 0.0
        exchange_origin_sale_id = None
        bon_credit = 0.0
        bon_codes = []
        payment_cash = None
        try:
            n = str(notes or "")
            for part in n.split(";"):
                part = part.strip()
                if part.startswith("EXCHANGE_CREDIT_APPLIED="):
                    exchange_credit = float(part.split("=", 1)[1] or 0.0)
                elif part.startswith("BON_CREDIT_APPLIED="):
                    bon_credit = float(part.split("=", 1)[1] or 0.0)
                elif part.startswith("BON_CODES="):
                    raw_codes = part.split("=", 1)[1] or ""
                    for raw_code in raw_codes.split(","):
                        code = normalize_bon_code(raw_code)
                        if code and code not in bon_codes:
                            bon_codes.append(code)
                elif part.startswith("ORIG_SALE_ID="):
                    try:
                        exchange_origin_sale_id = int(part.split("=", 1)[1] or 0) or None
                    except Exception:
                        exchange_origin_sale_id = None
                elif part.startswith("PAYMENT_CASH="):
                    try:
                        payment_cash = float(part.split("=", 1)[1] or 0.0)
                    except Exception:
                        payment_cash = None
        except Exception:
            exchange_credit = 0.0
            exchange_origin_sale_id = None
            bon_credit = 0.0
            bon_codes = []
            payment_cash = None

        if exchange_credit < 0:
            exchange_credit = 0.0
        if bon_credit < 0:
            bon_credit = 0.0
        if bon_codes and bon_credit <= 0:
            bon_credit = exchange_credit

        # ---- Store credit / bon credit used ----
        # Credit reduces the amount due but MUST NOT change item prices.
        store_credit_used = max(float(exchange_credit or 0.0), float(bon_credit or 0.0))
        if store_credit_used < 0:
            store_credit_used = 0.0
        if store_credit_used > float(total_sales):
            store_credit_used = float(total_sales)
        if bon_credit > store_credit_used:
            bon_credit = float(store_credit_used)

        amount_due = float(total_sales) - float(store_credit_used)
        if amount_due < 0:
            amount_due = 0.0

        pm = str(payment_method or "CASH").strip().upper()

        # If store credit fully covers the sale, force payment_method=EXCHANGE
        # so it never inflates cash drawer totals.
        if store_credit_used > 0 and amount_due <= 0.005:
            pm = "EXCHANGE"

        # ---- How much NEW CASH was collected ----
        # Only CASH increases the physical cash drawer.
        if payment_cash is not None:
            try:
                cash_paid = min(max(0.0, float(payment_cash or 0.0)), float(amount_due))
            except Exception:
                cash_paid = 0.0
        elif pm in ("EXCHANGE", "STORE_CREDIT"):
            cash_paid = 0.0
        elif pm in ("CARD", "DEBIT", "CREDIT_CARD", "WHISH"):
            cash_paid = 0.0
        else:
            cash_paid = float(amount_due)
            if cash_paid < 0:
                cash_paid = 0.0

        is_exchange = 1 if pm == "EXCHANGE" else 0


        # ---- Prorate receipt-level discount across items (so returns credit is correct per item) ----
        # We store:
        #   sale_items.gross_line_total = gross line total (before order discount)
        #   sale_items.discount_allocated = allocated share of order-level discount
        #   sale_items.line_total = NET line total (after allocation)
        # This makes returns accurate even after partial returns.
        gross_lines = [float(_line_total(l) or 0.0) for l in cart_lines]
        allocated_discounts = [0.0 for _ in gross_lines]
        if subtotal > 0 and discount_total > 0 and len(gross_lines) > 0:
            # First pass proportional allocation
            running = 0.0
            for i, gl in enumerate(gross_lines):
                if i == len(gross_lines) - 1:
                    alloc = round(float(discount_total) - running, 2)
                else:
                    alloc = round(float(discount_total) * (float(gl) / float(subtotal)), 2)
                    running += alloc
                if alloc < 0:
                    alloc = 0.0
                if alloc > gl:
                    alloc = gl
                allocated_discounts[i] = alloc

        net_line_totals = [round(max(0.0, gross_lines[i] - allocated_discounts[i]), 2) for i in range(len(gross_lines))]



        # Open shift id using same connection
        cur.execute(
            """
            SELECT id
            FROM cash_shifts
            WHERE closed_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """
        )
        r = cur.fetchone()
        shift_id = int(r["id"]) if r else None

        if shift_id is None:
            raise RuntimeError("No open shift. Open a shift first.")

        # Determine columns and NOT NULL requirements
        info = _table_info(cur, "sales")
        sales_cols = {row[1] for row in info}

        # ----- Customer-facing receipt code (resets daily) -----
        receipt_date = date.today().isoformat()
        receipt_seq = None
        receipt_code = None
        if {"receipt_date", "receipt_seq", "receipt_code"}.issubset(sales_cols):
            # next sequence number for the same date
            cur.execute(
                "SELECT COALESCE(MAX(receipt_seq), 0) + 1 AS next_seq FROM sales WHERE receipt_date = ?",
                (receipt_date,),
            )
            receipt_seq = int(cur.fetchone()[0] or 1)
            # Keep it compact: 0001, 0002, ... (expands naturally beyond 9999)
            receipt_code = f"{receipt_seq:04d}" if receipt_seq < 10000 else str(receipt_seq)

        values_map = {
            "created_at": created_at,
            "payment_method": pm,
            "customer_name": str(customer_name or ""),
            "shift_id": shift_id,

            "notes": str(notes or ""),

            "cash_paid": float(cash_paid),
            "store_credit_used": float(store_credit_used),
            "is_exchange": int(is_exchange),
            "exchange_origin_sale_id": exchange_origin_sale_id,

            "subtotal": float(subtotal),

            "discount": float(discount_total),
            "discount_total": float(discount_total),

            "tax": float(tax_total),
            "tax_total": float(tax_total),

            "shipping": float(shipping),

            "net_sales": float(net_sales),
            "total_sales": float(total_sales),

            "total_amount": float(amount_due),
            "total": float(amount_due),  # legacy

            # receipt fields (if present)
            "receipt_date": receipt_date,
            "receipt_seq": receipt_seq,
            "receipt_code": receipt_code,
        }

        insert_cols = [c for c in values_map.keys() if c in sales_cols]

        # Extra safety: supply placeholders for any NOT NULL column with no default
        for row in info:
            name = row[1]
            notnull = int(row[3] or 0)
            dflt = row[4]
            if notnull == 1 and dflt is None and name in sales_cols and name not in insert_cols:
                if "date" in name or name.endswith("_at"):
                    values_map[name] = created_at
                elif "name" in name or "method" in name or "note" in name:
                    values_map[name] = ""
                else:
                    values_map[name] = 0
                insert_cols.append(name)

        placeholders = ", ".join(["?"] * len(insert_cols))
        cols_sql = ", ".join(insert_cols)
        params = [values_map[c] for c in insert_cols]

        cur.execute(f"INSERT INTO sales ({cols_sql}) VALUES ({placeholders})", params)
        sale_id = int(cur.lastrowid)

        if bon_codes and bon_credit > 0.005:
            _redeem_bons_in_tx(cur, bon_codes, sale_id, bon_credit, created_at, shift_id)

        # ---------------- sale_items insert (schema-safe) ----------------
        si_info = _table_info(cur, "sale_items")
        si_cols = {row[1] for row in si_info}

        for i, l in enumerate(cart_lines):
            price = float(l.get("price") or 0)
            qty = int(l.get("qty") or 0)
            gross_lt = float(gross_lines[i] if i < len(gross_lines) else _line_total(l))
            alloc_disc = float(allocated_discounts[i] if i < len(allocated_discounts) else 0.0)
            lt = float(net_line_totals[i] if i < len(net_line_totals) else gross_lt)

            pid = l.get("product_id")
            if pid is None:
                raise ValueError("Missing product_id in cart line")

            values_map_si = {
                "sale_id": sale_id,
                "product_id": int(pid),
                "name": str(l.get("name") or ""),
                "price": float(price),
                "qty": int(qty),
                "line_total": float(lt),
                "gross_line_total": float(gross_lt),
                "discount_allocated": float(alloc_disc),

                # common legacy variants
                "unit_price_used": float(price),
                "unit_price": float(price),
                "discount": float(l.get("discount") or 0.0),
                "discount_total": float(l.get("discount") or 0.0),
                "subtotal": float(lt),
            }

            insert_cols_si = [c for c in values_map_si.keys() if c in si_cols]

            for row in si_info:
                name = row[1]
                notnull = int(row[3] or 0)
                dflt = row[4]
                if notnull == 1 and dflt is None and name in si_cols and name not in insert_cols_si:
                    if "date" in name or name.endswith("_at"):
                        values_map_si[name] = created_at
                    elif "name" in name or "desc" in name:
                        values_map_si[name] = str(l.get("name") or "")
                    elif "price" in name:
                        values_map_si[name] = float(price)
                    elif "qty" in name:
                        values_map_si[name] = int(qty)
                    else:
                        values_map_si[name] = float(lt)
                    insert_cols_si.append(name)

            placeholders_si = ", ".join(["?"] * len(insert_cols_si))
            cols_sql_si = ", ".join(insert_cols_si)
            params_si = [values_map_si[c] for c in insert_cols_si]

            cur.execute(
                f"INSERT INTO sale_items ({cols_sql_si}) VALUES ({placeholders_si})",
                params_si
            )

        conn.commit()
        return sale_id

    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


def get_sale_receipt_data(sale_id):
    return get_sale_detail(sale_id)


# ---------------- DATE RANGE HELPERS (USED BY app.py) ----------------

def _range_bounds(which):
    today = date.today()

    if which == "today":
        s = today
        e = today
    elif which == "week":
        s = today - timedelta(days=today.weekday())
        e = s + timedelta(days=6)
    elif which == "month":
        s = today.replace(day=1)
        if s.month == 12:
            nm = s.replace(year=s.year + 1, month=1, day=1)
        else:
            nm = s.replace(month=s.month + 1, day=1)
        e = nm - timedelta(days=1)
    elif which == "year":
        s = today.replace(month=1, day=1)
        e = today.replace(month=12, day=31)
    else:
        s = today
        e = today

    return s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")


# ---------------- ANALYTICS ----------------


def analytics_kpis_range(start_date, end_date):
    """KPIs for analytics range.

    IMPORTANT: For this POS we treat 'gross' as 'net' (net after returns).
    We keep the key name 'gross_sales' for UI compatibility, but its value is NET sales.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        s0, e1 = _sql_bounds_inclusive(start_date, end_date)

        # Sales totals + orders
        cur.execute(
            """
            SELECT
                COALESCE(SUM(
                    CASE
                        WHEN COALESCE(is_exchange,0)=1 THEN 0
                        WHEN UPPER(COALESCE(payment_method,'')) IN ('EXCHANGE','STORE_CREDIT','CARD','DEBIT','CREDIT_CARD','WHISH') THEN 0
                        ELSE COALESCE(cash_paid, 0)
                    END
                ), 0) AS sales_total,
                COALESCE(SUM(store_credit_used), 0) AS credit_sales_total,
                COALESCE(SUM(COALESCE(NULLIF(total_sales, 0), total_amount + store_credit_used, total_amount)), 0) AS merch_sales_total,
                COUNT(*) AS orders
            FROM sales
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            """,
            (s0, e1),
        )
        row = cur.fetchone()
        row = dict(row) if row else {}  # sqlite3.Row -> dict (EXE-safe)

        sales_total = float(row.get("sales_total", 0.0) or 0.0)
        credit_sales_total = float(row.get("credit_sales_total", 0.0) or 0.0)
        merch_sales_total = float(row.get("merch_sales_total", 0.0) or 0.0)
        orders = int(row.get("orders", 0) or 0)

        # Items sold (does not subtract returns; kept simple and fast)
        cur.execute(
            """
            SELECT COALESCE(SUM(qty), 0) AS items_sold
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            WHERE COALESCE(s.is_exchange,0)=0
              AND datetime(s.created_at) >= datetime(?)
              AND datetime(s.created_at) < datetime(?)
            """,
            (s0, e1),
        )
        items_sold = int(cur.fetchone()["items_sold"] or 0)

        # Returns total (returns table)
        try:
            cur.execute(
                """
                SELECT COALESCE(SUM(total_return_amount), 0) AS returns_total,
                       COALESCE(SUM(cash_refund), 0) AS cash_refunds,
                       COALESCE(SUM(credit_refund), 0) AS credit_refunds
                FROM returns
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                  AND COALESCE(is_voided, 0) = 0
                """,
                (s0, e1),
            )
            rrow = cur.fetchone()
            rrow = dict(rrow) if rrow else {}  # sqlite3.Row -> dict (EXE-safe)
            returns_total = float(rrow.get("returns_total", 0.0) or 0.0)
            cash_refunds = float(rrow.get("cash_refunds", 0.0) or 0.0)
            credit_refunds = float(rrow.get("credit_refunds", 0.0) or 0.0)
        except Exception:
            # If returns table doesn't exist, treat as zero
            returns_total = 0.0
            cash_refunds = 0.0
            credit_refunds = 0.0

        net_sales = merch_sales_total - returns_total
        aov = (net_sales / orders) if orders else 0.0

        # Keep keys stable for the UI
        return {
            "gross_sales": net_sales,   # gross == net (after returns)
            "net_sales": net_sales,
            "orders": orders,
            "cash_sales_total": sales_total,
            "store_credit_sales_total": credit_sales_total,
            "merch_sales_total": merch_sales_total,
            "returns_total": returns_total,
            "cash_refunds_total": cash_refunds,
            "credit_refunds_total": credit_refunds,
            "items_sold": items_sold,
            "avg_order_value": aov,
            "returns": returns_total,
            "discounts": 0.0,
        }
    finally:
        if conn:
            conn.close()


def analytics_breakdown_range(start_date, end_date):
    """Breakdown for analytics range.

    We keep the older keys (gross_sales, discounts, returns, net_sales, shipping, taxes, total_sales)
    so the UI never KeyErrors. Gross is reported as net per user requirement.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        s0, e1 = _sql_bounds_inclusive(start_date, end_date)

        cur.execute(
            """
            SELECT COALESCE(SUM(COALESCE(NULLIF(total_sales, 0), total_amount + store_credit_used, total_amount)), 0) AS sales_total,
                   COALESCE(SUM(discount_total), 0) AS discounts_total,
                   COALESCE(SUM(shipping), 0) AS shipping_total,
                   COALESCE(SUM(tax_total), 0) AS taxes_total
            FROM sales
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            """,
            (s0, e1),
        )
        sales_row = cur.fetchone()
        sales_total = float(sales_row["sales_total"] or 0.0)
        discounts = float(sales_row["discounts_total"] or 0.0)
        shipping = float(sales_row["shipping_total"] or 0.0)
        taxes = float(sales_row["taxes_total"] or 0.0)

        try:
            cur.execute(
                """
                SELECT COALESCE(SUM(total_return_amount), 0) AS returns_total
                FROM returns
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                  AND COALESCE(is_voided, 0) = 0
                """,
                (s0, e1),
            )
            returns_total = float(cur.fetchone()["returns_total"] or 0.0)
        except Exception:
            returns_total = 0.0

        pm_breakdown = {}
        try:
            cur.execute(
                """
                SELECT COALESCE(payment_method, 'CASH') AS pm,
                       COALESCE(SUM(COALESCE(NULLIF(total_sales, 0), total_amount + store_credit_used, total_amount)), 0) AS pm_total
                FROM sales
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                GROUP BY COALESCE(payment_method, 'CASH')
                """,
                (s0, e1),
            )
            for r in cur.fetchall():
                pm_breakdown[str(r["pm"]).upper()] = float(r["pm_total"] or 0.0)
        except Exception:
            pass

        cat_breakdown = {}
        try:
            cur.execute(
                """
                SELECT COALESCE(p.category, 'Uncategorized') AS cat,
                       COALESCE(SUM(si.line_total), 0) AS cat_total
                FROM sale_items si
                JOIN sales s ON si.sale_id = s.id
                LEFT JOIN products p ON si.product_id = p.id
                WHERE datetime(s.created_at) >= datetime(?)
                  AND datetime(s.created_at) < datetime(?)
                GROUP BY COALESCE(p.category, 'Uncategorized')
                ORDER BY cat_total DESC
                """,
                (s0, e1),
            )
            for r in cur.fetchall():
                cat_breakdown[str(r["cat"])] = float(r["cat_total"] or 0.0)
        except Exception:
            pass

        net_sales = sales_total - returns_total
        total_sales = net_sales  # keep it simple

        # 'gross_sales' intentionally equals net_sales
        return {
            "gross_sales": net_sales,
            "discounts": discounts,
            "returns": returns_total,
            "shipping": shipping,
            "taxes": taxes,
            "net_sales": net_sales,
            "total_sales": total_sales,
            "pm_breakdown": pm_breakdown,
            "cat_breakdown": cat_breakdown,
        }
    finally:
        if conn:
            conn.close()


def analytics_series_in_range(start_date, end_date, group="day"):
    """Time-series revenue for analytics charts.

    Revenue is NET (sales total minus returns total) aggregated by hour/day/month.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        s0, e1 = _sql_bounds_inclusive(start_date, end_date)

        if group == "hour":
            label_sales = "strftime('%Y-%m-%d %H:00', created_at)"
            label_returns = "strftime('%Y-%m-%d %H:00', created_at)"
            order_by = "label"
        elif group == "month":
            label_sales = "strftime('%Y-%m', created_at)"
            label_returns = "strftime('%Y-%m', created_at)"
            order_by = "label"
        else:
            label_sales = "strftime('%Y-%m-%d', created_at)"
            label_returns = "strftime('%Y-%m-%d', created_at)"
            order_by = "label"

        # Sales grouped
        cur.execute(
            f"""
            SELECT
                {label_sales} AS label,
                COALESCE(SUM(COALESCE(NULLIF(total_sales, 0), total_amount + store_credit_used, total_amount)), 0) AS sales_total,
                COUNT(*) AS orders
            FROM sales
            WHERE datetime(created_at) >= datetime(?)
              AND datetime(created_at) < datetime(?)
            GROUP BY label
            ORDER BY {order_by} ASC
            """,
            (s0, e1),
        )
        sales_rows = cur.fetchall()
        by_label = {
            r["label"]: {
                "sales_total": float(r["sales_total"] or 0.0),
                "orders": int(r["orders"] or 0),
                "returns_total": 0.0,
            }
            for r in sales_rows
        }

        # Returns grouped (optional)
        try:
            cur.execute(
                f"""
                SELECT
                    {label_returns} AS label,
                    COALESCE(SUM(total_return_amount), 0) AS returns_total
                FROM returns
                WHERE datetime(created_at) >= datetime(?)
                  AND datetime(created_at) < datetime(?)
                  AND COALESCE(is_voided, 0) = 0
                GROUP BY label
                ORDER BY {order_by} ASC
                """,
                (s0, e1),
            )
            ret_rows = cur.fetchall()
            for r in ret_rows:
                lab = r["label"]
                if lab not in by_label:
                    by_label[lab] = {"sales_total": 0.0, "orders": 0, "returns_total": 0.0}
                by_label[lab]["returns_total"] = float(r["returns_total"] or 0.0)
        except Exception:
            pass

        # Return sorted series
        out = []
        for lab in sorted(by_label.keys()):
            sales_total = by_label[lab]["sales_total"]
            returns_total = by_label[lab]["returns_total"]
            net = sales_total - returns_total
            out.append({
                "label": lab,
                "revenue": net,
                "orders": by_label[lab]["orders"],
            })
        return out
    finally:
        if conn:
            conn.close()

def analytics_top_products_range(start_date, end_date, limit=12):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        s0, e1 = _sql_bounds_inclusive(start_date, end_date)

        def key(row):
            pid = row.get("product_id")
            if pid not in (None, "", 0, "0"):
                return f"id:{pid}"
            barcode = str(row.get("barcode") or "").strip()
            if barcode:
                return f"barcode:{barcode}"
            return f"name:{str(row.get('name') or '').strip().lower()}"

        def new_rec(row):
            return {
                "product_id": row.get("product_id"),
                "name": str(row.get("name") or "").strip() or "(Unnamed item)",
                "barcode": str(row.get("barcode") or "").strip(),
                "category": str(row.get("category") or "").strip(),
                "brand": str(row.get("brand") or "").strip(),
                "qty_sold": 0,
                "qty_returned": 0,
                "net_qty": 0,
                "gross_revenue": 0.0,
                "discounts": 0.0,
                "sales_revenue": 0.0,
                "return_amount": 0.0,
                "net_revenue": 0.0,
                "revenue": 0.0,
                "avg_unit": 0.0,
                "sales_share": 0.0,
                "current_stock": row.get("current_stock"),
            }

        cur.execute(
            """
            SELECT
                si.product_id AS product_id,
                si.name AS name,
                p.barcode AS barcode,
                p.category AS category,
                p.brand AS brand,
                p.stock_qty AS current_stock,
                COALESCE(SUM(si.qty), 0) AS qty_sold,
                COALESCE(SUM(COALESCE(NULLIF(si.gross_line_total, 0), si.price * si.qty, si.line_total)), 0) AS gross_revenue,
                COALESCE(SUM(si.discount_allocated), 0) AS discounts,
                COALESCE(SUM(si.line_total), 0) AS sales_revenue
            FROM sale_items si
            JOIN sales s ON s.id = si.sale_id
            LEFT JOIN products p ON p.id = si.product_id
            WHERE datetime(s.created_at) >= datetime(?)
              AND datetime(s.created_at) < datetime(?)
            GROUP BY si.product_id, si.name, p.barcode, p.category, p.brand, p.stock_qty
            """,
            (s0, e1),
        )
        totals = {}
        for raw in cur.fetchall():
            row = dict(raw)
            rec = totals.setdefault(key(row), new_rec(row))
            rec["qty_sold"] += int(row.get("qty_sold") or 0)
            rec["gross_revenue"] += float(row.get("gross_revenue") or 0.0)
            rec["discounts"] += float(row.get("discounts") or 0.0)
            rec["sales_revenue"] += float(row.get("sales_revenue") or 0.0)
            if not rec.get("barcode") and row.get("barcode"):
                rec["barcode"] = str(row.get("barcode") or "").strip()
            if not rec.get("category") and row.get("category"):
                rec["category"] = str(row.get("category") or "").strip()
            if not rec.get("brand") and row.get("brand"):
                rec["brand"] = str(row.get("brand") or "").strip()
            if row.get("current_stock") is not None:
                rec["current_stock"] = row.get("current_stock")

        try:
            cur.execute(
                """
                SELECT
                    ri.product_id AS product_id,
                    ri.name AS name,
                    p.barcode AS barcode,
                    p.category AS category,
                    p.brand AS brand,
                    p.stock_qty AS current_stock,
                    COALESCE(SUM(ri.qty), 0) AS qty_returned,
                    COALESCE(SUM(ri.line_total), 0) AS return_amount
                FROM return_items ri
                JOIN returns r ON r.id = ri.return_id
                LEFT JOIN products p ON p.id = ri.product_id
                WHERE datetime(r.created_at) >= datetime(?)
                  AND datetime(r.created_at) < datetime(?)
                  AND COALESCE(r.is_voided, 0) = 0
                GROUP BY ri.product_id, ri.name, p.barcode, p.category, p.brand, p.stock_qty
                """,
                (s0, e1),
            )
            for raw in cur.fetchall():
                row = dict(raw)
                rec = totals.setdefault(key(row), new_rec(row))
                rec["qty_returned"] += int(row.get("qty_returned") or 0)
                rec["return_amount"] += float(row.get("return_amount") or 0.0)
                if not rec.get("barcode") and row.get("barcode"):
                    rec["barcode"] = str(row.get("barcode") or "").strip()
                if not rec.get("category") and row.get("category"):
                    rec["category"] = str(row.get("category") or "").strip()
                if not rec.get("brand") and row.get("brand"):
                    rec["brand"] = str(row.get("brand") or "").strip()
                if row.get("current_stock") is not None:
                    rec["current_stock"] = row.get("current_stock")
        except Exception:
            pass

        out = []
        total_net_revenue = 0.0
        for rec in totals.values():
            rec["net_qty"] = int(rec["qty_sold"] or 0) - int(rec["qty_returned"] or 0)
            rec["net_revenue"] = float(rec["sales_revenue"] or 0.0) - float(rec["return_amount"] or 0.0)
            rec["revenue"] = rec["net_revenue"]  # legacy UI key
            rec["avg_unit"] = (float(rec["sales_revenue"]) / int(rec["qty_sold"])) if int(rec["qty_sold"] or 0) else 0.0
            total_net_revenue += float(rec["net_revenue"] or 0.0)
            out.append(rec)

        if total_net_revenue:
            for rec in out:
                rec["sales_share"] = float(rec["net_revenue"] or 0.0) / total_net_revenue

        out.sort(key=lambda r: (-float(r.get("net_revenue") or 0.0), -int(r.get("qty_sold") or 0), str(r.get("name") or "").lower()))
        return out[:max(1, int(limit))]
    finally:
        if conn:
            conn.close()


def analytics_low_stock(limit=50):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM products
            WHERE is_deleted = 0
              AND stock_qty <= low_stock_level
            ORDER BY stock_qty ASC, name COLLATE NOCASE ASC
            LIMIT ?
        """, (int(limit),))

        return cur.fetchall()
    finally:
        if conn:
            conn.close()


def cancel_sale_and_restore_stock(sale_id: int):
    """Destructive return: fully cancel a sale.
    - Restores inventory for all items in the sale
    - Deletes sale_items then deletes the sale
    This matches the 'replacement' exchange model (as if the sale never happened).
    """
    conn = get_conn()
    try:
        cur = conn.cursor()

        cur.execute("SELECT product_id, qty FROM sale_items WHERE sale_id = ?", (int(sale_id),))
        rows = cur.fetchall()

        for r in rows:
            pid = r["product_id"]
            qty = int(r["qty"] or 0)
            if pid is None:
                continue
            if qty:
                cur.execute("UPDATE products SET stock_qty = stock_qty + ? WHERE id = ?", (qty, int(pid)))

        cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (int(sale_id),))
        cur.execute("DELETE FROM sales WHERE id = ?", (int(sale_id),))

        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------- DB SELF TEST (DIAGNOSTICS) ----------------

def db_self_test():
    """Quick sanity test for the database.

    Creates a temp product, ensures an open shift exists, creates a sale, reads it back,
    then deletes the sale and soft-deletes the temp product.

    Returns a dict with ok=True/False and details.
    """
    result = {"ok": False, "steps": []}
    try:
        from datetime import datetime as _dt, date as _date

        suffix = _dt.now().strftime("%H%M%S")
        name = f"__TEST_ITEM__{suffix}"

        barcode = add_product(
            name=name,
            category="TEST",
            brand="TEST",
            sell_price=9.99,
            stock_qty=5,
            low_stock_level=0
        )
        result["steps"].append({"add_product": {"name": name, "barcode": barcode}})

        row = find_product_by_barcode(barcode)
        if not row:
            raise RuntimeError("Test product not found after insert")
        pid = int(row["id"])
        result["steps"].append({"find_product_by_barcode": {"product_id": pid}})

        shift = get_open_shift()
        if not shift:
            sid = open_shift(opening_cash=0.0, notes="DB self-test", employee_name="System")
            result["steps"].append({"open_shift": {"shift_id": int(sid)}})
        else:
            result["steps"].append({"open_shift": {"shift_id": int(shift["id"])}})

        cart_lines = [{"product_id": pid, "name": name, "price": 9.99, "qty": 1, "line_total": 9.99}]
        sale_id = create_sale(cart_lines, payment_method="CASH", customer_name="", order_discount_total=0.0, notes="DB self-test sale")
        result["steps"].append({"create_sale": {"sale_id": int(sale_id)}})

        sale, items = get_sale_detail(int(sale_id))
        if not sale or not items:
            raise RuntimeError("Sale could not be read back")
        result["steps"].append({"get_sale_detail": {"items": len(items)}})

        day = _date.today().isoformat()
        rows = list_sales_for_day(day, limit=200)
        found = any(int(r["id"]) == int(sale_id) for r in rows)
        result["steps"].append({"list_sales_for_day": {"found_sale": bool(found), "count": len(rows)}})

        delete_sale(int(sale_id), restore_stock=True)
        result["steps"].append({"delete_sale": {"sale_id": int(sale_id)}})

        delete_product(pid)
        result["steps"].append({"delete_product": {"product_id": pid}})

        result["ok"] = True
        return result

    except Exception as e:
        result["error"] = str(e)
        return result
