PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS pos_sync_events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    inserted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_pos_sync_events_cursor
    ON pos_sync_events (inserted_at, event_id);
CREATE INDEX IF NOT EXISTS idx_pos_sync_events_entity
    ON pos_sync_events (entity_type, inserted_at DESC, event_id DESC);

CREATE TABLE IF NOT EXISTS pos_print_jobs (
    job_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    claimed_by TEXT,
    claimed_at TEXT,
    printed_at TEXT,
    failed_at TEXT,
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_print_jobs_status_created
    ON pos_print_jobs (status, created_at);

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
    created_at TEXT NOT NULL,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_products_cloud_local
    ON products (cloud_device_id, cloud_local_id);

CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    pin TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);

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
    opening_usd REAL NOT NULL DEFAULT 0,
    opening_lbp REAL NOT NULL DEFAULT 0,
    closing_usd REAL,
    closing_lbp REAL,
    lbp_per_usd REAL NOT NULL DEFAULT 89500,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_shifts_cloud
    ON cash_shifts (cloud_device_id, cloud_local_id);

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
    cloud_event_id TEXT UNIQUE,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_cash_movements_shift
    ON cash_movements (shift_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cash_movements_cloud
    ON cash_movements (cloud_device_id, cloud_local_id);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    total_amount REAL NOT NULL DEFAULT 0,
    payment_method TEXT NOT NULL DEFAULT 'CASH',
    customer_name TEXT NOT NULL DEFAULT '',
    shift_id INTEGER,
    receipt_date TEXT,
    receipt_seq INTEGER,
    receipt_code TEXT,
    subtotal REAL NOT NULL DEFAULT 0,
    discount REAL NOT NULL DEFAULT 0,
    discount_total REAL NOT NULL DEFAULT 0,
    tax REAL NOT NULL DEFAULT 0,
    tax_total REAL NOT NULL DEFAULT 0,
    shipping REAL NOT NULL DEFAULT 0,
    net_sales REAL NOT NULL DEFAULT 0,
    total_sales REAL NOT NULL DEFAULT 0,
    cash_paid REAL NOT NULL DEFAULT 0,
    store_credit_used REAL NOT NULL DEFAULT 0,
    is_exchange INTEGER NOT NULL DEFAULT 0,
    exchange_origin_sale_id INTEGER,
    notes TEXT,
    cloud_event_id TEXT UNIQUE,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_cloud_local
    ON sales (cloud_device_id, cloud_local_id);

CREATE TABLE IF NOT EXISTS sale_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id INTEGER NOT NULL,
    product_id INTEGER,
    name TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    qty INTEGER NOT NULL DEFAULT 0,
    line_total REAL NOT NULL DEFAULT 0,
    gross_line_total REAL NOT NULL DEFAULT 0,
    discount_allocated REAL NOT NULL DEFAULT 0,
    cloud_event_id TEXT UNIQUE,
    cloud_sale_event_id TEXT,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_sale_items_sale ON sale_items (sale_id);

CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_sale_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    total_return_amount REAL NOT NULL DEFAULT 0,
    shift_id INTEGER,
    notes TEXT,
    cash_refund REAL NOT NULL DEFAULT 0,
    credit_refund REAL NOT NULL DEFAULT 0,
    is_voided INTEGER NOT NULL DEFAULT 0,
    voided_at TEXT,
    void_notes TEXT,
    cloud_event_id TEXT UNIQUE,
    cloud_device_id TEXT,
    cloud_local_id TEXT,
    cloud_original_sale_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_returns_sale ON returns (original_sale_id);

CREATE TABLE IF NOT EXISTS return_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL,
    sale_item_id INTEGER,
    product_id INTEGER,
    name TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    qty INTEGER NOT NULL DEFAULT 0,
    line_total REAL NOT NULL DEFAULT 0,
    cloud_event_id TEXT UNIQUE,
    cloud_return_event_id TEXT,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_return_items_return ON return_items (return_id);

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
    cloud_event_id TEXT UNIQUE,
    cloud_device_id TEXT,
    cloud_local_id TEXT,
    cloud_return_local_id TEXT,
    cloud_original_sale_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_bons_code ON bons (code);
CREATE INDEX IF NOT EXISTS idx_bons_return ON bons (return_id);

CREATE TABLE IF NOT EXISTS bon_redemptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bon_id INTEGER NOT NULL,
    sale_id INTEGER,
    created_at TEXT NOT NULL,
    amount REAL NOT NULL DEFAULT 0,
    shift_id INTEGER,
    notes TEXT,
    cloud_event_id TEXT UNIQUE,
    cloud_bon_event_id TEXT,
    cloud_device_id TEXT,
    cloud_local_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_bon_redemptions_bon ON bon_redemptions (bon_id);
