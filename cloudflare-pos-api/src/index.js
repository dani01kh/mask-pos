const TABLES = {
  pos_sync_events: {
    columns: ["event_id", "device_id", "event_type", "entity_type", "entity_id", "payload", "created_at", "schema_version", "inserted_at"],
    json: ["payload"],
  },
  pos_print_jobs: {
    columns: ["job_id", "device_id", "job_type", "payload", "status", "created_at", "claimed_by", "claimed_at", "printed_at", "failed_at", "last_error"],
    json: ["payload"],
  },
  products: {
    columns: ["id", "barcode", "name", "category", "brand", "location", "sell_price", "stock_qty", "low_stock_level", "cost_price", "supplier", "is_deleted", "created_at", "cloud_device_id", "cloud_local_id"],
  },
  employees: {
    columns: ["id", "name", "pin", "is_active", "created_at", "cloud_device_id", "cloud_local_id"],
  },
  cash_shifts: {
    columns: ["id", "opened_at", "closed_at", "opening_cash", "closing_cash", "notes", "employee_id", "shift_date", "shift_seq", "shift_code", "opening_usd", "opening_lbp", "closing_usd", "closing_lbp", "lbp_per_usd", "cloud_device_id", "cloud_local_id"],
  },
  cash_movements: {
    columns: ["id", "created_at", "shift_id", "movement_type", "amount_usd", "amount_lbp", "lbp_per_usd", "amount_value", "reason", "employee_id", "employee_name", "notes", "cloud_event_id", "cloud_device_id", "cloud_local_id"],
  },
  sales: {
    columns: ["id", "created_at", "total_amount", "payment_method", "customer_name", "shift_id", "receipt_date", "receipt_seq", "receipt_code", "subtotal", "discount", "discount_total", "tax", "tax_total", "shipping", "net_sales", "total_sales", "cash_paid", "store_credit_used", "is_exchange", "exchange_origin_sale_id", "notes", "is_voided", "voided_at", "void_reason", "voided_by", "cloud_event_id", "cloud_device_id", "cloud_local_id"],
  },
  sale_items: {
    columns: ["id", "sale_id", "product_id", "name", "price", "qty", "line_total", "gross_line_total", "discount_allocated", "original_unit_price", "line_discount", "product_barcode", "product_category", "cost_price", "supplier", "product_brand", "product_location", "cloud_event_id", "cloud_sale_event_id", "cloud_device_id", "cloud_local_id"],
  },
  returns: {
    columns: ["id", "original_sale_id", "created_at", "total_return_amount", "shift_id", "notes", "cash_refund", "credit_refund", "is_voided", "voided_at", "void_notes", "cloud_event_id", "cloud_device_id", "cloud_local_id", "cloud_original_sale_local_id"],
  },
  return_items: {
    columns: ["id", "return_id", "sale_item_id", "product_id", "name", "price", "qty", "line_total", "cloud_event_id", "cloud_return_event_id", "cloud_device_id", "cloud_local_id"],
  },
  bons: {
    columns: ["id", "code", "created_at", "original_amount", "remaining_amount", "status", "return_id", "original_sale_id", "shift_id", "issued_by_employee_id", "issued_by_name", "signature_text", "notes", "redeemed_at", "last_redeemed_at", "voided_at", "void_notes", "cloud_event_id", "cloud_device_id", "cloud_local_id", "cloud_return_local_id", "cloud_original_sale_local_id"],
  },
  bon_redemptions: {
    columns: ["id", "bon_id", "sale_id", "created_at", "amount", "shift_id", "notes", "cloud_event_id", "cloud_bon_event_id", "cloud_device_id", "cloud_local_id"],
  },
};

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, apikey, content-type, prefer",
  "access-control-allow-methods": "GET, POST, PATCH, OPTIONS",
};

const now = () => new Date().toISOString();
const text = (value, fallback = "") => value === null || value === undefined ? fallback : String(value);
const num = (value, fallback = 0) => {
  if (value === null || value === undefined || value === "") return fallback;
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
};
const integer = (value, fallback = 0) => {
  if (value === null || value === undefined || value === "") return fallback;
  return Math.trunc(num(value, fallback));
};
const nullable = (value) => value === "" || value === undefined ? null : value;

function response(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function parseJson(value, fallback = {}) {
  if (value && typeof value === "object") return value;
  try {
    return JSON.parse(value || "");
  } catch {
    return fallback;
  }
}

function encodeValue(table, column, value) {
  if ((TABLES[table].json || []).includes(column) && value !== null && typeof value !== "string") {
    return JSON.stringify(value);
  }
  return value;
}

function decodeRows(table, rows) {
  return (rows || []).map((raw) => {
    const row = { ...raw };
    for (const column of TABLES[table].json || []) {
      if (Object.prototype.hasOwnProperty.call(row, column)) row[column] = parseJson(row[column], {});
    }
    return row;
  });
}

function requireTable(table) {
  if (!TABLES[table]) throw new Error("Unknown table");
  return TABLES[table];
}

function selectedColumns(table, value) {
  const allowed = requireTable(table).columns;
  if (!value || value === "*") return allowed;
  const selected = value.split(",").map((x) => x.trim()).filter(Boolean);
  if (!selected.length || selected.some((x) => !allowed.includes(x))) throw new Error("Invalid select");
  return selected;
}

function orderClause(table, value) {
  if (!value) return "";
  const allowed = requireTable(table).columns;
  const bits = value.split(",").map((part) => {
    const [column, direction = "asc"] = part.trim().split(".");
    if (!allowed.includes(column) || !["asc", "desc"].includes(direction.toLowerCase())) throw new Error("Invalid order");
    return `${column} ${direction.toUpperCase()}`;
  });
  return bits.length ? ` ORDER BY ${bits.join(", ")}` : "";
}

function whereClause(table, params) {
  const allowed = requireTable(table).columns;
  const clauses = [];
  const values = [];
  for (const [column, raw] of params.entries()) {
    if (["select", "order", "limit", "offset", "on_conflict", "or"].includes(column)) continue;
    if (!allowed.includes(column)) throw new Error("Invalid filter");
    const value = String(raw);
    const dot = value.indexOf(".");
    const op = dot >= 0 ? value.slice(0, dot) : "eq";
    const operand = dot >= 0 ? value.slice(dot + 1) : value;
    if (op === "is" && operand === "null") {
      clauses.push(`${column} IS NULL`);
      continue;
    }
    const sqlOp = { eq: "=", neq: "!=", gt: ">", gte: ">=", lt: "<", lte: "<=" }[op];
    if (!sqlOp) throw new Error("Invalid filter operator");
    clauses.push(`${column} ${sqlOp} ?`);
    values.push(operand);
  }

  const or = params.get("or");
  if (or) {
    const match = /^\(inserted_at\.gt\.(.+),and\(inserted_at\.eq\.(.+),event_id\.gt\.(.+)\)\)$/.exec(or);
    if (!match) throw new Error("Invalid cursor filter");
    clauses.push("(inserted_at > ? OR (inserted_at = ? AND event_id > ?))");
    values.push(match[1], match[2], match[3]);
  }
  return { sql: clauses.length ? ` WHERE ${clauses.join(" AND ")}` : "", values };
}

async function getRows(db, table, url) {
  const columns = selectedColumns(table, url.searchParams.get("select"));
  const where = whereClause(table, url.searchParams);
  const limit = Math.max(1, Math.min(5000, integer(url.searchParams.get("limit"), 1000)));
  const offset = Math.max(0, integer(url.searchParams.get("offset"), 0));
  const sql = `SELECT ${columns.join(", ")} FROM ${table}${where.sql}${orderClause(table, url.searchParams.get("order"))} LIMIT ? OFFSET ?`;
  const result = await db.prepare(sql).bind(...where.values, limit, offset).all();
  return decodeRows(table, result.results);
}

async function patchRows(db, table, url, body, prefer) {
  const allowed = requireTable(table).columns;
  const entries = Object.entries(body || {}).filter(([column]) => allowed.includes(column) && column !== "id");
  if (!entries.length) return [];
  const where = whereClause(table, url.searchParams);
  if (!where.sql) throw new Error("PATCH requires a filter");
  const sql = `UPDATE ${table} SET ${entries.map(([column]) => `${column} = ?`).join(", ")}${where.sql}`;
  await db.prepare(sql).bind(...entries.map(([column, value]) => encodeValue(table, column, value)), ...where.values).run();
  if (!prefer.includes("return=representation")) return [];
  return getRows(db, table, url);
}

async function upsertProduct(db, raw, event = null) {
  const p = raw || {};
  const barcode = text(p.barcode || p.entity_id).trim();
  if (!barcode) return;
  const category = text(p.category).trim();
  let deleted = integer(p.is_deleted, 0);
  const stock = Math.max(0, integer(p.stock_qty, 0));
  if (category.toLowerCase() === "quick" && stock <= 0) deleted = 1;
  await db.prepare(`
    INSERT INTO products (
      barcode, name, category, brand, location, sell_price, stock_qty, low_stock_level,
      cost_price, supplier, is_deleted, created_at, cloud_device_id, cloud_local_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(barcode) DO UPDATE SET
      name = excluded.name, category = excluded.category, brand = excluded.brand,
      location = excluded.location,
      sell_price = excluded.sell_price, stock_qty = excluded.stock_qty,
      low_stock_level = excluded.low_stock_level, cost_price = excluded.cost_price,
      supplier = excluded.supplier, is_deleted = excluded.is_deleted,
      cloud_device_id = COALESCE(NULLIF(excluded.cloud_device_id, ''), products.cloud_device_id),
      cloud_local_id = COALESCE(NULLIF(excluded.cloud_local_id, ''), products.cloud_local_id)
  `).bind(
    barcode, text(p.name, "Cloud item").trim() || "Cloud item", category, text(p.brand).trim(),
    text(p.location).trim(), num(p.sell_price), stock, integer(p.low_stock_level),
    Math.max(0, num(p.cost_price)), text(p.supplier).trim(), deleted, text(p.created_at, now()),
    text(p.cloud_device_id || (event && event.device_id)), text(p.cloud_local_id || p.id),
  ).run();
}

async function applyProduct(db, event, payload) {
  const p = { ...payload, entity_id: event.entity_id };
  if (event.event_type === "delete") p.is_deleted = 1;
  await upsertProduct(db, p, event);
}

async function applyEmployee(db, event, payload) {
  const name = text(payload.name || event.entity_id).trim();
  if (!name) return;
  await db.prepare(`
    INSERT INTO employees (name, pin, is_active, created_at, cloud_device_id, cloud_local_id)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(name) DO UPDATE SET
      pin = excluded.pin, is_active = excluded.is_active,
      cloud_device_id = excluded.cloud_device_id, cloud_local_id = excluded.cloud_local_id
  `).bind(
    name, text(payload.pin), integer(payload.is_active, event.event_type === "deactivate" ? 0 : 1),
    text(payload.created_at, now()), event.device_id, text(event.entity_id || payload.id),
  ).run();
}

async function mappedId(db, table, deviceId, localId) {
  if (localId === null || localId === undefined || localId === "") return null;
  const row = await db.prepare(`SELECT id FROM ${table} WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1`)
    .bind(deviceId, text(localId)).first();
  return row ? row.id : null;
}

async function productIdForLine(db, event, line) {
  if (!line || typeof line !== "object") return null;
  if (line.barcode) {
    const byBarcode = await db.prepare("SELECT id FROM products WHERE barcode = ? LIMIT 1").bind(text(line.barcode)).first();
    if (byBarcode) return byBarcode.id;
  }
  if (line.product_id !== null && line.product_id !== undefined && line.product_id !== "") {
    const mapped = await mappedId(db, "products", event.device_id, line.product_id);
    if (mapped) return mapped;
    const byId = await db.prepare("SELECT id FROM products WHERE id = ? LIMIT 1").bind(integer(line.product_id)).first();
    if (byId) return byId.id;
  }
  return null;
}

async function adjustProductStock(db, productId, delta) {
  if (!productId || !delta) return;
  await db.prepare("UPDATE products SET stock_qty = MAX(0, stock_qty + ?) WHERE id = ?").bind(integer(delta), productId).run();
}

async function closeStaleOpenShifts(db, event, localId, openedAt) {
  await db.prepare(`
    UPDATE cash_shifts
    SET closed_at = ?,
        closing_cash = COALESCE(closing_cash, opening_cash),
        closing_usd = COALESCE(closing_usd, opening_usd),
        closing_lbp = COALESCE(closing_lbp, opening_lbp),
        notes = TRIM(COALESCE(notes, '') || CASE WHEN COALESCE(notes, '') = '' THEN '' ELSE '\n' END || ?)
    WHERE closed_at IS NULL
      AND NOT (cloud_device_id = ? AND cloud_local_id = ?)
      AND datetime(COALESCE(opened_at, '')) <= datetime(?)
  `).bind(
    text(openedAt, now()),
    "[Cloud repair] Closed stale open shift after a newer register opened.",
    event.device_id,
    localId,
    text(openedAt, now()),
  ).run();
}

async function applyShift(db, event, payload) {
  const localId = text(event.entity_id || payload.shift_id || payload.id);
  if (!localId) return;
  const existing = await mappedId(db, "cash_shifts", event.device_id, localId);
  const employeeId = nullable(payload.employee_id);
  if (!existing) {
    await db.prepare(`
      INSERT INTO cash_shifts (
        opened_at, closed_at, opening_cash, closing_cash, notes, employee_id,
        shift_date, shift_seq, shift_code, opening_usd, opening_lbp, closing_usd,
        closing_lbp, lbp_per_usd, cloud_device_id, cloud_local_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      text(payload.opened_at, now()), nullable(payload.closed_at), num(payload.opening_cash),
      nullable(payload.closing_cash), text(payload.notes), employeeId, nullable(payload.shift_date),
      nullable(payload.shift_seq), text(payload.shift_code), num(payload.opening_usd), num(payload.opening_lbp),
      nullable(payload.closing_usd), nullable(payload.closing_lbp), num(payload.lbp_per_usd, 89500),
      event.device_id, localId,
    ).run();
    if (nullable(payload.closed_at) === null && text(event.event_type).toLowerCase() === "open") {
      await closeStaleOpenShifts(db, event, localId, payload.opened_at);
    }
    return;
  }
  await db.prepare(`
    UPDATE cash_shifts SET
      closed_at = COALESCE(?, closed_at), closing_cash = COALESCE(?, closing_cash),
      closing_usd = COALESCE(?, closing_usd), closing_lbp = COALESCE(?, closing_lbp),
      lbp_per_usd = COALESCE(?, lbp_per_usd), notes = CASE WHEN ? = '' THEN notes ELSE ? END
    WHERE id = ?
  `).bind(
    nullable(payload.closed_at), nullable(payload.closing_cash), nullable(payload.closing_usd),
    nullable(payload.closing_lbp), nullable(payload.lbp_per_usd), text(payload.notes), text(payload.notes), existing,
  ).run();

  if (nullable(payload.closed_at) === null && text(event.event_type).toLowerCase() === "open") {
    await closeStaleOpenShifts(db, event, localId, payload.opened_at);
  }
}

async function applyCashMovement(db, event, payload) {
  if (event.event_type !== "create") return;
  const raw = payload.movement && typeof payload.movement === "object" ? payload.movement : payload;
  const localId = text(event.entity_id || raw.movement_id || raw.id);
  if (!localId) return;
  const existing = await db.prepare("SELECT id FROM cash_movements WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1")
    .bind(event.device_id, localId).first();
  if (existing) return;
  const shiftId = await mappedId(db, "cash_shifts", event.device_id, raw.shift_id) || nullable(raw.shift_id);
  const employeeId = await mappedId(db, "employees", event.device_id, raw.employee_id) || nullable(raw.employee_id);
  await db.prepare(`
    INSERT INTO cash_movements (
      created_at, shift_id, movement_type, amount_usd, amount_lbp, lbp_per_usd,
      amount_value, reason, employee_id, employee_name, notes,
      cloud_event_id, cloud_device_id, cloud_local_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).bind(
    text(raw.created_at, now()), shiftId, text(raw.movement_type, "OUT").toUpperCase(),
    num(raw.amount_usd), num(raw.amount_lbp), num(raw.lbp_per_usd, 89500),
    num(raw.amount_value), text(raw.reason), employeeId, text(raw.employee_name),
    text(raw.notes), event.event_id, event.device_id, localId,
  ).run();
}

async function deleteSale(db, event, payload) {
  const snapshot = payload.sale && typeof payload.sale === "object" ? payload.sale : {};
  const localId = text(event.entity_id || payload.sale_id || snapshot.id);
  let saleId = await mappedId(db, "sales", event.device_id, localId);
  if (!saleId) saleId = integer(payload.sale_id || snapshot.id || event.entity_id, 0);
  if (!saleId) return;
  if (payload.restore_stock) {
    const items = await db.prepare(`
      SELECT
        si.product_id,
        MAX(0, si.qty - COALESCE(SUM(CASE WHEN COALESCE(r.is_voided, 0) = 0 THEN ri.qty ELSE 0 END), 0)) AS qty
      FROM sale_items si
      LEFT JOIN return_items ri ON ri.sale_item_id = si.id
      LEFT JOIN returns r ON r.id = ri.return_id
      WHERE si.sale_id = ?
      GROUP BY si.id, si.product_id, si.qty
    `).bind(saleId).all();
    for (const item of items.results || []) await adjustProductStock(db, item.product_id, integer(item.qty));
  }
  await db.prepare("DELETE FROM return_items WHERE return_id IN (SELECT id FROM returns WHERE original_sale_id = ?)").bind(saleId).run();
  await db.prepare("DELETE FROM returns WHERE original_sale_id = ?").bind(saleId).run();
  await db.prepare("DELETE FROM sale_items WHERE sale_id = ?").bind(saleId).run();
  await db.prepare("DELETE FROM sales WHERE id = ?").bind(saleId).run();
}

async function applySale(db, event, payload) {
  if (event.event_type === "delete") {
    await deleteSale(db, event, payload);
    return;
  }
  if (event.event_type === "update_payment") {
    const localId = text(event.entity_id || payload.sale_id);
    const snapshot = payload.sale && typeof payload.sale === "object" ? payload.sale : {};
    let sale = await db.prepare(
      "SELECT id FROM sales WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1"
    ).bind(event.device_id, localId).first();
    if (!sale && snapshot.receipt_date && snapshot.receipt_code) {
      sale = await db.prepare(
        "SELECT id FROM sales WHERE receipt_date = ? AND receipt_code = ? LIMIT 1"
      ).bind(text(snapshot.receipt_date), text(snapshot.receipt_code)).first();
    }
    if (sale) {
      await db.prepare(
        "UPDATE sales SET payment_method = ?, cash_paid = ?, notes = ? WHERE id = ?"
      ).bind(
        text(snapshot.payment_method || payload.payment_method, "CASH").toUpperCase(),
        num(snapshot.cash_paid),
        text(snapshot.notes),
        sale.id,
      ).run();
    }
    return;
  }
  if (event.event_type === "void") {
    const localId = text(event.entity_id || payload.sale_id);
    const sale = await db.prepare(
      "SELECT id, is_voided FROM sales WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1"
    ).bind(event.device_id, localId).first();
    if (sale && !integer(sale.is_voided)) {
      if (payload.restore_stock) {
        const items = await db.prepare("SELECT product_id, qty FROM sale_items WHERE sale_id = ?").bind(sale.id).all();
        for (const item of items.results || []) await adjustProductStock(db, item.product_id, integer(item.qty));
      }
      await db.prepare(
        "UPDATE sales SET is_voided = 1, voided_at = ?, void_reason = ?, voided_by = ? WHERE id = ?"
      ).bind(now(), text(payload.reason), text(payload.voided_by), sale.id).run();
    }
    return;
  }
  if (event.event_type !== "create") return;
  const s = payload.sale && typeof payload.sale === "object" ? payload.sale : {};
  let existing = await db.prepare("SELECT id FROM sales WHERE cloud_event_id = ? LIMIT 1").bind(event.event_id).first();
  if (!existing && event.device_id && text(event.entity_id || payload.sale_id)) {
    existing = await db.prepare("SELECT id FROM sales WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1")
      .bind(event.device_id, text(event.entity_id || payload.sale_id)).first();
  }
  // A host may receive a new device id after a reinstall/database recovery. In
  // that case backfill events have new IDs even though the receipt already
  // exists. Receipt numbers are date-scoped, so this is the stable sale key.
  if (!existing && s.receipt_date && s.receipt_code) {
    existing = await db.prepare(
      "SELECT id FROM sales WHERE receipt_date = ? AND receipt_code = ? LIMIT 1"
    ).bind(text(s.receipt_date), text(s.receipt_code)).first();
  }
  if (existing) return;
  let saleId = existing && existing.id;
  const shiftId = await mappedId(db, "cash_shifts", event.device_id, s.shift_id);
  if (!saleId) {
    const result = await db.prepare(`
      INSERT INTO sales (
        created_at, total_amount, payment_method, customer_name, shift_id, receipt_date, receipt_seq,
        receipt_code, subtotal, discount, discount_total, tax, tax_total, shipping, net_sales,
        total_sales, cash_paid, store_credit_used, is_exchange, exchange_origin_sale_id, notes,
        is_voided, voided_at, void_reason, voided_by,
        cloud_event_id, cloud_device_id, cloud_local_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      text(s.created_at, now()), num(s.total_amount ?? s.total_sales), text(s.payment_method || payload.payment_method, "CASH"),
      text(s.customer_name ?? payload.customer_name), shiftId, nullable(s.receipt_date), nullable(s.receipt_seq),
      text(s.receipt_code), num(s.subtotal), num(s.discount), num(s.discount_total), num(s.tax), num(s.tax_total),
      num(s.shipping), num(s.net_sales ?? s.total_sales), num(s.total_sales ?? s.total_amount), num(s.cash_paid),
      num(s.store_credit_used), integer(s.is_exchange), nullable(s.exchange_origin_sale_id), text(s.notes ?? payload.notes),
      integer(s.is_voided), nullable(s.voided_at), text(s.void_reason), text(s.voided_by),
      event.event_id, event.device_id, text(event.entity_id || payload.sale_id),
    ).run();
    saleId = result.meta.last_row_id;
  }
  await db.prepare("DELETE FROM sale_items WHERE cloud_sale_event_id = ?").bind(event.event_id).run();
  const items = Array.isArray(payload.items) ? payload.items : [];
  const cartLines = Array.isArray(payload.cart_lines) ? payload.cart_lines : [];
  for (let i = 0; i < items.length; i += 1) {
    const item = items[i] || {};
    const cartLine = cartLines[i] || item;
    const productId = await productIdForLine(db, event, cartLine);
    await db.prepare(`
      INSERT INTO sale_items (
        sale_id, product_id, name, price, qty, line_total, gross_line_total,
        discount_allocated, original_unit_price, line_discount, product_barcode, product_category, cost_price, supplier,
        product_brand, product_location,
        cloud_event_id, cloud_sale_event_id, cloud_device_id, cloud_local_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      saleId, productId, text(item.name), num(item.price), integer(item.qty), num(item.line_total),
      num(item.gross_line_total ?? item.line_total), num(item.discount_allocated),
      num(item.original_unit_price ?? item.price), num(item.line_discount),
      text(item.product_barcode ?? cartLine.barcode), text(item.product_category),
      Math.max(0, num(item.cost_price)), text(item.supplier),
      text(item.product_brand ?? cartLine.brand), text(item.product_location ?? cartLine.location),
      `${event.event_id}:${i + 1}`, event.event_id, event.device_id, text(item.id),
    ).run();
  }
  await redeemBonsForSale(db, saleId, shiftId, text(s.notes ?? payload.notes), text(s.created_at, now()));
}

async function applyReturn(db, event, payload) {
  const localId = text(event.entity_id || payload.return_id);
  if (event.event_type === "void") {
    const ret = await db.prepare("SELECT id, is_voided FROM returns WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1")
      .bind(event.device_id, localId).first();
    if (ret && !integer(ret.is_voided)) {
      const items = await db.prepare("SELECT product_id, qty FROM return_items WHERE return_id = ?").bind(ret.id).all();
      for (const item of items.results || []) await adjustProductStock(db, item.product_id, -integer(item.qty));
      await db.prepare("UPDATE returns SET is_voided = 1, voided_at = ?, void_notes = ? WHERE id = ? AND is_voided = 0")
        .bind(now(), text(payload.notes), ret.id).run();
    }
    return;
  }
  if (event.event_type === "reset_for_sale") {
    const saleId = await mappedId(db, "sales", event.device_id, payload.original_sale_id);
    if (saleId) {
      const items = await db.prepare(`
        SELECT ri.product_id, SUM(ri.qty) AS qty
        FROM return_items ri JOIN returns r ON r.id = ri.return_id
        WHERE r.original_sale_id = ? AND r.is_voided = 0
        GROUP BY ri.product_id
      `).bind(saleId).all();
      for (const item of items.results || []) await adjustProductStock(db, item.product_id, -integer(item.qty));
      await db.prepare("UPDATE returns SET is_voided = 1, voided_at = ?, void_notes = ? WHERE original_sale_id = ? AND is_voided = 0")
        .bind(now(), text(payload.notes), saleId).run();
    }
    return;
  }
  if (event.event_type !== "create") return;
  const saleId = await mappedId(db, "sales", event.device_id, payload.original_sale_id) || integer(payload.original_sale_id);
  let existing = await db.prepare("SELECT id FROM returns WHERE cloud_event_id = ? LIMIT 1").bind(event.event_id).first();
  if (!existing && event.device_id && localId) {
    existing = await db.prepare("SELECT id FROM returns WHERE cloud_device_id = ? AND cloud_local_id = ? LIMIT 1")
      .bind(event.device_id, localId).first();
  }
  if (existing) return;
  let returnId = existing && existing.id;
  if (!returnId) {
    const result = await db.prepare(`
      INSERT INTO returns (
        original_sale_id, created_at, total_return_amount, shift_id, notes, cash_refund,
        credit_refund, is_voided, cloud_event_id, cloud_device_id, cloud_local_id, cloud_original_sale_local_id
      ) VALUES (?, ?, ?, NULL, ?, 0, ?, 0, ?, ?, ?, ?)
    `).bind(
      saleId, now(), num(payload.expected_total), text(payload.notes), num(payload.expected_total),
      event.event_id, event.device_id, localId, text(payload.original_sale_id),
    ).run();
    returnId = result.meta.last_row_id;
  }
  await db.prepare("DELETE FROM return_items WHERE cloud_return_event_id = ?").bind(event.event_id).run();
  const lines = Array.isArray(payload.returned_lines) ? payload.returned_lines : [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] || {};
    let saleItem = null;
    if (line.sale_item_id !== null && line.sale_item_id !== undefined) {
      saleItem = await db.prepare(`
        SELECT id, product_id FROM sale_items
        WHERE cloud_device_id = ? AND cloud_local_id = ?
        LIMIT 1
      `).bind(event.device_id, text(line.sale_item_id)).first();
      if (!saleItem) saleItem = await db.prepare("SELECT id, product_id FROM sale_items WHERE id = ? LIMIT 1")
        .bind(integer(line.sale_item_id)).first();
    }
    const productId = saleItem && saleItem.product_id || await productIdForLine(db, event, line);
    await adjustProductStock(db, productId, integer(line.qty));
    await db.prepare(`
      INSERT INTO return_items (
        return_id, sale_item_id, product_id, name, price, qty, line_total,
        cloud_event_id, cloud_return_event_id, cloud_device_id, cloud_local_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      returnId, saleItem && saleItem.id || null, productId, text(line.name), num(line.price), integer(line.qty), num(line.line_total),
      `${event.event_id}:${i + 1}`, event.event_id, event.device_id, text(line.id || line.sale_item_id),
    ).run();
  }
}

function normalizeBonCode(value) {
  const compact = text(value).toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (compact.startsWith("BON") && compact.length >= 12) {
    const day = compact.slice(3, 11);
    const seq = compact.slice(11);
    if (/^\d{8}$/.test(day) && /^\d+$/.test(seq)) return `BON-${day}-${String(integer(seq)).padStart(4, "0")}`;
  }
  return text(value).toUpperCase().trim();
}

function noteValue(notes, key) {
  for (const part of text(notes).split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(`${key}=`)) return trimmed.slice(key.length + 1);
  }
  return "";
}

function noteBonCodes(notes) {
  const raw = noteValue(notes, "BON_CODES");
  const out = [];
  for (const bit of raw.split(",")) {
    const code = normalizeBonCode(bit);
    if (code && !out.includes(code)) out.push(code);
  }
  return out;
}

async function redeemBonsForSale(db, saleId, shiftId, notes, createdAt) {
  const codes = noteBonCodes(notes);
  let amountLeft = num(noteValue(notes, "BON_CREDIT_APPLIED"));
  if (!codes.length || amountLeft <= 0) return;
  for (const code of codes) {
    if (amountLeft <= 0.005) break;
    const bon = await db.prepare("SELECT * FROM bons WHERE code = ? LIMIT 1").bind(code).first();
    if (!bon || text(bon.status, "ACTIVE").toUpperCase() !== "ACTIVE") continue;
    const remaining = Math.max(0, num(bon.remaining_amount));
    if (remaining <= 0.005) continue;
    const used = Math.min(remaining, amountLeft);
    const nextRemaining = Math.max(0, remaining - used);
    const nextStatus = nextRemaining <= 0.005 ? "USED" : "ACTIVE";
    await db.prepare(`
      UPDATE bons
      SET remaining_amount = ?, status = ?, last_redeemed_at = ?,
          redeemed_at = CASE WHEN ? = 'USED' THEN COALESCE(redeemed_at, ?) ELSE redeemed_at END
      WHERE id = ?
    `).bind(nextRemaining, nextStatus, createdAt, nextStatus, createdAt, bon.id).run();
    await db.prepare(`
      INSERT INTO bon_redemptions (
        bon_id, sale_id, created_at, amount, shift_id, notes,
        cloud_event_id, cloud_bon_event_id, cloud_device_id, cloud_local_id
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      bon.id, saleId, createdAt, used, shiftId, "Sale redemption",
      `sale:${saleId}:bon:${bon.id}:${createdAt}`, text(bon.cloud_event_id), text(bon.cloud_device_id), "",
    ).run();
    amountLeft -= used;
  }
}

async function applyBon(db, event, payload) {
  const raw = payload.bon && typeof payload.bon === "object" ? payload.bon : payload;
  const code = normalizeBonCode(raw.code || event.entity_id);
  if (!code) return;
  if (event.event_type === "void") {
    await db.prepare(`
      UPDATE bons
      SET status = 'VOID', remaining_amount = 0, voided_at = ?, void_notes = ?
      WHERE code = ?
    `).bind(now(), text(payload.notes || raw.void_notes), code).run();
    return;
  }
  if (event.event_type !== "create" && event.event_type !== "update") return;
  const returnId = await mappedId(db, "returns", event.device_id, raw.return_id) || nullable(raw.return_id);
  const saleId = await mappedId(db, "sales", event.device_id, raw.original_sale_id) || nullable(raw.original_sale_id);
  const shiftId = await mappedId(db, "cash_shifts", event.device_id, raw.shift_id) || nullable(raw.shift_id);
  const employeeId = await mappedId(db, "employees", event.device_id, raw.issued_by_employee_id) || nullable(raw.issued_by_employee_id);
  await db.prepare(`
    INSERT INTO bons (
      code, created_at, original_amount, remaining_amount, status, return_id,
      original_sale_id, shift_id, issued_by_employee_id, issued_by_name,
      signature_text, notes, redeemed_at, last_redeemed_at, voided_at, void_notes,
      cloud_event_id, cloud_device_id, cloud_local_id, cloud_return_local_id,
      cloud_original_sale_local_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(code) DO UPDATE SET
      remaining_amount = excluded.remaining_amount,
      status = excluded.status,
      issued_by_name = excluded.issued_by_name,
      signature_text = excluded.signature_text,
      notes = excluded.notes,
      redeemed_at = excluded.redeemed_at,
      last_redeemed_at = excluded.last_redeemed_at,
      voided_at = excluded.voided_at,
      void_notes = excluded.void_notes
  `).bind(
    code, text(raw.created_at, now()), num(raw.original_amount), num(raw.remaining_amount ?? raw.original_amount),
    text(raw.status, "ACTIVE"), returnId, saleId, shiftId, employeeId, text(raw.issued_by_name),
    text(raw.signature_text), text(raw.notes), nullable(raw.redeemed_at), nullable(raw.last_redeemed_at),
    nullable(raw.voided_at), text(raw.void_notes), event.event_id, event.device_id,
    text(event.entity_id || raw.id || code), text(raw.return_id), text(raw.original_sale_id),
  ).run();
}

async function applyEvent(db, event) {
  const payload = parseJson(event.payload, {});
  if (event.entity_type === "product") return applyProduct(db, event, payload);
  if (event.entity_type === "employee") return applyEmployee(db, event, payload);
  if (event.entity_type === "shift") return applyShift(db, event, payload);
  if (event.entity_type === "cash_movement") return applyCashMovement(db, event, payload);
  if (event.entity_type === "sale") return applySale(db, event, payload);
  if (event.entity_type === "return") return applyReturn(db, event, payload);
  if (event.entity_type === "bon") return applyBon(db, event, payload);
}

async function postEvents(db, body) {
  const events = Array.isArray(body) ? body : [body];
  for (const event of events) {
    if (!event || !event.event_id || !event.device_id) continue;
    const payload = typeof event.payload === "string" ? event.payload : JSON.stringify(event.payload || {});
    const insertedAt = text(event.inserted_at, now());
    const result = await db.prepare(`
      INSERT OR IGNORE INTO pos_sync_events (
        event_id, device_id, event_type, entity_type, entity_id, payload, created_at, schema_version, inserted_at
      ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      text(event.event_id), text(event.device_id), text(event.event_type), text(event.entity_type),
      text(event.entity_id), payload, text(event.created_at, now()), integer(event.schema_version, 1), insertedAt,
    ).run();
    if (result.meta.changes) await applyEvent(db, { ...event, payload });
  }
}

async function postProducts(db, body) {
  for (const product of Array.isArray(body) ? body : [body]) await upsertProduct(db, product);
}

async function postPrintJobs(db, body) {
  for (const job of Array.isArray(body) ? body : [body]) {
    await db.prepare(`
      INSERT OR IGNORE INTO pos_print_jobs (job_id, device_id, job_type, payload, status, created_at)
      VALUES (?, ?, ?, ?, ?, ?)
    `).bind(
      text(job.job_id), text(job.device_id), text(job.job_type), JSON.stringify(job.payload || {}),
      text(job.status, "pending"), text(job.created_at, now()),
    ).run();
  }
}

async function counts(db) {
  const out = {};
  for (const table of Object.keys(TABLES)) {
    const row = await db.prepare(`SELECT count(*) AS n FROM ${table}`).first();
    out[table] = row.n;
  }
  return out;
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: JSON_HEADERS });
    const url = new URL(request.url);
    if (url.pathname === "/health") return response({ ok: true, service: "maskpos-cloud-api" });

    const auth = request.headers.get("authorization") || "";
    if (!env.POS_API_TOKEN || auth !== `Bearer ${env.POS_API_TOKEN}`) return response({ error: "Unauthorized" }, 401);
    if (url.pathname === "/admin/counts" && request.method === "GET") return response(await counts(env.DB));

    const match = /^\/rest\/v1\/([a-z_]+)$/.exec(url.pathname);
    if (!match) return response({ error: "Not found" }, 404);
    const table = match[1];
    try {
      requireTable(table);
      if (request.method === "GET") return response(await getRows(env.DB, table, url));
      const body = await request.json();
      if (request.method === "PATCH") {
        return response(await patchRows(env.DB, table, url, body, request.headers.get("prefer") || ""));
      }
      if (request.method !== "POST") return response({ error: "Method not allowed" }, 405);
      if (table === "pos_sync_events") await postEvents(env.DB, body);
      else if (table === "products") await postProducts(env.DB, body);
      else if (table === "pos_print_jobs") await postPrintJobs(env.DB, body);
      else return response({ error: "Writes are not allowed for this table" }, 405);
      return response([], 201);
    } catch (error) {
      return response({ error: text(error && error.message, "Request failed") }, 400);
    }
  },
};
