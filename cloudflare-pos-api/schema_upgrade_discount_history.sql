ALTER TABLE sale_items ADD COLUMN original_unit_price REAL NOT NULL DEFAULT 0;
ALTER TABLE sale_items ADD COLUMN line_discount REAL NOT NULL DEFAULT 0;

UPDATE sale_items
SET original_unit_price = CASE
    WHEN qty > 0 AND gross_line_total > 0 THEN gross_line_total / qty
    ELSE price
END
WHERE COALESCE(original_unit_price, 0) = 0;

UPDATE sale_items
SET line_discount = MAX(0, gross_line_total - line_total - COALESCE(discount_allocated, 0))
WHERE COALESCE(line_discount, 0) = 0;
