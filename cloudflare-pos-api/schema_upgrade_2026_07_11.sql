-- Apply once to an existing Mask POS D1 database before deploying this worker.
ALTER TABLE products ADD COLUMN cost_price REAL NOT NULL DEFAULT 0;
ALTER TABLE products ADD COLUMN supplier TEXT NOT NULL DEFAULT '';

ALTER TABLE sales ADD COLUMN is_voided INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sales ADD COLUMN voided_at TEXT;
ALTER TABLE sales ADD COLUMN void_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE sales ADD COLUMN voided_by TEXT NOT NULL DEFAULT '';

ALTER TABLE sale_items ADD COLUMN product_barcode TEXT NOT NULL DEFAULT '';
ALTER TABLE sale_items ADD COLUMN product_category TEXT NOT NULL DEFAULT '';
ALTER TABLE sale_items ADD COLUMN cost_price REAL NOT NULL DEFAULT 0;
ALTER TABLE sale_items ADD COLUMN supplier TEXT NOT NULL DEFAULT '';
