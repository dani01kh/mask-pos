-- Add brand and location snapshot columns to sale_items in the D1 cloud DB.
ALTER TABLE sale_items ADD COLUMN product_brand TEXT NOT NULL DEFAULT '';
ALTER TABLE sale_items ADD COLUMN product_location TEXT NOT NULL DEFAULT '';
