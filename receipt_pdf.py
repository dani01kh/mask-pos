# receipt_pdf.py - receipt + gift receipt PDF
from pathlib import Path
from datetime import datetime
import tempfile

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm

# Safe margins for 80mm thermal paper
LEFT_SAFE = 2 * mm
RIGHT_SAFE = 10 * mm
from reportlab.graphics.barcode import code128

from backend import get_store_subtitle


def _row_get(obj, key, default=None):
    """Safe getter for dict or sqlite3.Row."""
    try:
        if obj is None:
            return default
        if hasattr(obj, "keys") and key in obj.keys():
            return obj[key]
        if isinstance(obj, dict):
            return obj.get(key, default)
    except Exception:
        pass
    return default


def _barcode_value_for_sale(sale) -> str:
    created_at = str(_row_get(sale, "created_at", "") or "")
    sale_date = ""
    try:
        sale_date = created_at.split(" ")[0].replace("-", "")
    except Exception:
        sale_date = ""
    rc = str(_row_get(sale, "receipt_code", "") or "").strip()
    if sale_date and rc:
        return f"R-{sale_date}-{rc}"
    sid = _row_get(sale, "id", "")
    return f"R-{sid}"


def _wheel_prize_for_sale(sale) -> str:
    notes = str(_row_get(sale, "notes", "") or "")
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("WHEEL_PRIZE="):
            return part.split("=", 1)[1].strip()
    return ""


def _payment_label(value) -> str:
    pm = str(value or "CASH").strip().upper()
    labels = {
        "CASH": "Cash",
        "WHISH": "Whish",
        "CREDIT_CARD": "Credit Card",
        "CARD": "Credit Card",
        "DEBIT": "Debit Card",
        "EXCHANGE": "Exchange / Store Credit",
        "STORE_CREDIT": "Store Credit",
        "CASH+WHISH": "Cash + Whish",
        "CASH+CARD": "Cash + Credit Card",
        "CASH+CREDIT_CARD": "Cash + Credit Card",
    }
    if pm in labels:
        return labels[pm]
    if "+" in pm:
        return " + ".join(labels.get(part, part.replace("_", " ").title()) for part in pm.split("+"))
    return labels.get(pm, pm.replace("_", " ").title())


def _note_float(notes: str, key: str) -> float:
    prefix = f"{key}="
    try:
        for part in str(notes or "").split(";"):
            part = part.strip()
            if part.startswith(prefix):
                return max(0.0, float(part.split("=", 1)[1] or 0.0))
    except Exception:
        pass
    return 0.0


def _payment_breakdown(sale) -> list[tuple[str, float]]:
    notes = str(_row_get(sale, "notes", "") or "")
    parts = [
        ("Cash", _note_float(notes, "PAYMENT_CASH")),
        ("Whish", _note_float(notes, "PAYMENT_WHISH")),
        ("Credit Card", _note_float(notes, "PAYMENT_CARD")),
    ]
    return [(label, amount) for label, amount in parts if amount > 0.005]


def create_temp_receipt_pdf(shop_name: str, sale, items) -> Path:
    """Normal receipt sized for thermal printers (80mm)."""
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)

    sale_id = _row_get(sale, "id", "0")
    pdf_path = out_dir / f"receipt_{sale_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    # Thermal paper sizing (80mm width) with dynamic height so it doesn't print tiny at the bottom.
    paper_w = 80 * mm
    line_h = 5.0 * mm

    # Estimate needed height
    header_lines = 8
    item_lines = max(0, len(items)) * 2
    footer_lines = 6
    barcode_block = 32 * mm
    min_h = 140 * mm
    est_h = (header_lines + item_lines + footer_lines) * line_h + barcode_block + 20 * mm
    paper_h = max(min_h, min(800 * mm, est_h))
    TOP_SAFE = 8 * mm  # safe top margin to avoid first-line clipping on thermal printers

    c = canvas.Canvas(str(pdf_path), pagesize=(paper_w, paper_h))

    left = 4 * mm
    right = paper_w - 4 * mm
    center = paper_w / 2

    # Start as close to the top as possible (some printers still add a small hardware margin).
    y = paper_h - TOP_SAFE

    def draw_left(text, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= line_h

    def draw_center(text, bold=False, size=12):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawCentredString(center, y, str(text))
        y -= line_h

    def draw_right(text, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawRightString(paper_w - RIGHT_SAFE, y, str(text))
        y -= line_h

    receipt_code = str(_row_get(sale, "receipt_code", "") or "").strip() or str(sale_id)
    created_at = _row_get(sale, "created_at", "")
    payment = _payment_label(_row_get(sale, "payment_method", "CASH"))

    # Header (two-line brand)
    if shop_name:
        draw_center(shop_name, bold=True, size=16)
    try:
        sub = get_store_subtitle()
    except Exception:
        sub = ""
    if sub:
        draw_center(sub, bold=False, size=11)
    draw_center(f"Receipt #: {receipt_code}", size=11)
    draw_center(f"Date: {created_at}", size=10)
    draw_center(f"Payment: {payment}", size=10)
    draw_left("-" * 42, size=10)

    subtotal = 0.0
    pre_subtotal = 0.0
    for it in items:
        name = str(_row_get(it, "name", "")) or ""
        qty = int(_row_get(it, "qty", 0) or 0)
        price = float(_row_get(it, "price", 0.0) or 0.0)
        original_price = float(_row_get(it, "original_unit_price", price) or price)
        lt = float(_row_get(it, "line_total", price * qty) or 0.0)
        pre_subtotal += float(max(price, original_price) * qty)
        subtotal += lt

        # Wrap product name to fit thermal width
        max_chars = 28
        name_lines = [name[i:i + max_chars] for i in range(0, len(name), max_chars)] or [""]
        draw_left(name_lines[0], bold=True, size=10)
        if len(name_lines) > 1:
            draw_left(name_lines[1], bold=False, size=10)

        effective_unit = (lt / qty) if qty else price
        if original_price - effective_unit > 0.005:
            pct = ((original_price - effective_unit) / original_price * 100.0) if original_price > 0 else 0.0
            draw_left(f"{qty} x ${effective_unit:.2f} = ${lt:.2f}", size=10)
            draw_left(f"  DISCOUNT: was ${original_price:.2f}  (-${original_price-effective_unit:.2f}, {pct:.0f}%)", size=9)
        else:
            draw_left(f"{qty} x ${price:.2f} = ${lt:.2f}", size=10)

        # If we're getting close to the bottom, start a new (thermal-sized) page
        if y < 40 * mm:
            c.showPage()
            y = paper_h - TOP_SAFE
            c.setPageSize((paper_w, paper_h))

    draw_left("-" * 42, size=10)
    total_due = float(_row_get(sale, "total_amount", subtotal) or subtotal)

    # Compute discount from item lines (difference between price*qty and line_total)
    discount_amt = round(pre_subtotal - subtotal, 2)
    if discount_amt < 0:
        discount_amt = 0.0

    # Exchange credit (stored historically in discount_total/order_discount_total)
    credit_amt = 0.0
    try:
        credit_amt = float(_row_get(sale, "store_credit_used", 0.0) or 0.0)
    except Exception:
        credit_amt = 0.0
    if credit_amt <= 0:
        try:
            credit_amt = float(_row_get(sale, "discount_total", 0.0) or 0.0)
        except Exception:
            credit_amt = 0.0
    if credit_amt <= 0:
        try:
            credit_amt = float(_row_get(sale, "order_discount_total", 0.0) or 0.0)
        except Exception:
            credit_amt = 0.0
    if credit_amt < 0:
        credit_amt = 0.0

    # Totals: show discount line ONLY when a discount is applied
    draw_right(f"SUBTOTAL: ${pre_subtotal:.2f}", size=11)
    if discount_amt > 0.009:
        draw_right(f"DISCOUNT: -${discount_amt:.2f}", size=11)
    if credit_amt > 0.009:
        draw_right(f"EXCHANGE CREDIT: -${credit_amt:.2f}", size=11)
    draw_right(f"TOTAL: ${total_due:.2f}", bold=True, size=13)
    payment_breakdown = _payment_breakdown(sale)
    if len(payment_breakdown) > 1:
        for label, amount in payment_breakdown:
            draw_right(f"{label.upper()}: ${amount:.2f}", size=10)
    wheel_prize = _wheel_prize_for_sale(sale)
    if wheel_prize:
        draw_left("")
        draw_center(f"Wheel prize: {wheel_prize}", bold=True, size=10)

    draw_left("")
    draw_center("Scan barcode below for exchange", size=9)

    # Barcode
    barcode_value = _barcode_value_for_sale(sale)
    bc = code128.Code128(barcode_value, barHeight=18 * mm, humanReadable=True)

    target_w = paper_w - 2 * left
    scale = target_w / max(1.0, float(bc.width))
    scale = min(1.6, max(0.9, scale))

    draw_y = max(10 * mm, y - 26 * mm)
    c.saveState()
    c.translate((paper_w - bc.width * scale) / 2.0, draw_y)
    c.scale(scale, 1.0)
    bc.drawOn(c, 0, 0)
    c.restoreState()

    y = draw_y - 6 * mm
    draw_center("Thank you!", size=11)

    c.save()
    return pdf_path

def create_gift_receipt_pdf(shop_name: str, sale, items) -> Path:
    """Gift receipt sized for thermal printers (80mm)."""
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)

    sale_id = _row_get(sale, "id", "0")
    pdf_path = out_dir / f"gift_receipt_{sale_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    paper_w = 80 * mm
    line_h = 5.0 * mm

    header_lines = 8
    item_lines = max(0, len(items)) * 1
    footer_lines = 6
    barcode_block = 32 * mm
    min_h = 130 * mm
    est_h = (header_lines + item_lines + footer_lines) * line_h + barcode_block + 18 * mm
    paper_h = max(min_h, min(800 * mm, est_h))
    TOP_SAFE = 8 * mm  # safe top margin to avoid first-line clipping on thermal printers

    c = canvas.Canvas(str(pdf_path), pagesize=(paper_w, paper_h))

    left = 4 * mm
    center = paper_w / 2
    y = paper_h - TOP_SAFE

    def draw_left(text, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= line_h

    def draw_center(text, bold=False, size=12):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawCentredString(center, y, str(text))
        y -= line_h

    receipt_code = str(_row_get(sale, "receipt_code", "") or "").strip() or str(sale_id)
    created_at = _row_get(sale, "created_at", "")

    if shop_name:
        draw_center(shop_name, bold=True, size=16)
    draw_center("GIFT RECEIPT", bold=True, size=12)
    draw_center(f"Receipt #: {receipt_code}", size=10)
    draw_center(f"Date: {created_at}", size=10)
    draw_left("-" * 42, size=10)

    max_chars = 30
    for it in items:
        name = str(_row_get(it, "name", "")) or ""
        qty = int(_row_get(it, "qty", 0) or 0)
        line = f"{qty} x {name}"
        draw_left(line[:max_chars], size=10)

        if y < 40 * mm:
            c.showPage()
            y = paper_h - TOP_SAFE
            c.setPageSize((paper_w, paper_h))

    draw_left("-" * 42, size=10)
    draw_center("For Exchange only ·", size=9)
    draw_center("")

    barcode_value = _barcode_value_for_sale(sale)
    bc = code128.Code128(barcode_value, barHeight=18 * mm, humanReadable=True)

    target_w = paper_w - 2 * left
    scale = target_w / max(1.0, float(bc.width))
    scale = min(1.6, max(0.9, scale))

    draw_y = max(10 * mm, y - 26 * mm)
    c.saveState()
    c.translate((paper_w - bc.width * scale) / 2.0, draw_y)
    c.scale(scale, 1.0)
    bc.drawOn(c, 0, 0)
    c.restoreState()

    c.save()
    return pdf_path



def create_bon_receipt_pdf(shop_name: str, bon) -> Path:
    """Store-credit bon sized for 80mm thermal printers."""
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)

    code = str(_row_get(bon, "code", "") or "BON")
    safe_code = "".join(ch for ch in code if ch.isalnum() or ch in ("-", "_")) or "BON"
    pdf_path = out_dir / f"bon_{safe_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    paper_w = 80 * mm
    paper_h = 145 * mm
    line_h = 5.2 * mm
    top_safe = 8 * mm
    left = 4 * mm
    right = paper_w - 4 * mm
    center = paper_w / 2

    c = canvas.Canvas(str(pdf_path), pagesize=(paper_w, paper_h))
    y = paper_h - top_safe

    def draw_left(text, bold=False, size=10):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= line_h

    def draw_center(text, bold=False, size=12):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawCentredString(center, y, str(text))
        y -= line_h

    try:
        amount = float(_row_get(bon, "remaining_amount", _row_get(bon, "original_amount", 0.0)) or 0.0)
    except Exception:
        amount = 0.0
    try:
        original_amount = float(_row_get(bon, "original_amount", amount) or amount)
    except Exception:
        original_amount = amount

    created_at = _row_get(bon, "created_at", "")
    employee = str(_row_get(bon, "issued_by_name", "") or "").strip()
    signature = str(_row_get(bon, "signature_text", "") or "").strip()
    return_id = _row_get(bon, "return_id", "")
    receipt_code = str(_row_get(bon, "original_receipt_code", "") or "").strip()

    if shop_name:
        draw_center(shop_name, bold=True, size=16)
    try:
        sub = get_store_subtitle()
    except Exception:
        sub = ""
    if sub:
        draw_center(sub, size=10)

    draw_center("STORE CREDIT BON", bold=True, size=14)
    draw_center(code, bold=True, size=11)
    draw_left("-" * 42, size=10)
    draw_center(f"VALUE: ${amount:.2f}", bold=True, size=18)
    if abs(original_amount - amount) > 0.005:
        draw_center(f"Original: ${original_amount:.2f}", size=10)
    draw_left("")
    draw_left(f"Date: {created_at}", size=10)
    if receipt_code:
        draw_left(f"Original receipt: {receipt_code}", size=10)
    if return_id not in (None, ""):
        draw_left(f"Return #: {return_id}", size=10)
    draw_left(f"Issued by: {employee or '-'}", size=10)
    if signature:
        draw_left(f"Signature: {signature}", size=10)
    else:
        draw_left("Signature: __________________", size=10)
    draw_left("-" * 42, size=10)
    draw_center("Scan this bon at checkout", size=9)

    bc = code128.Code128(code, barHeight=18 * mm, humanReadable=True)
    target_w = paper_w - 2 * left
    scale = target_w / max(1.0, float(bc.width))
    scale = min(1.55, max(0.85, scale))

    draw_y = max(12 * mm, y - 27 * mm)
    c.saveState()
    c.translate((paper_w - bc.width * scale) / 2.0, draw_y)
    c.scale(scale, 1.0)
    bc.drawOn(c, 0, 0)
    c.restoreState()

    y = draw_y - 7 * mm
    draw_center("Valid for store credit only", size=9)
    draw_center("Thank you!", size=10)

    c.save()
    return pdf_path


def create_weekly_selection_receipt_pdf(shop_name: str, rows, title: str = "Weekly Receipt") -> Path:
    """Create a custom combined receipt from selected sales.

    Each row may override the printed time and printed amount without changing the database.
    When present, ``print_receipt_no`` is used as the print-only receipt number.
    """
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"weekly_receipt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    selected = list(rows or [])

    item_line_count = 0
    for r in selected:
        items = list(_row_get(r, "items", []) or [])
        for it in items:
            name = str(_row_get(it, "name", "") or "")
            item_line_count += max(1, (len(name) // 28) + 1)

    paper_w = 80 * mm
    line_h = 5.0 * mm
    small_line_h = 4.0 * mm
    row_count = max(1, len(selected))
    date_header_count = len({str(_row_get(r, "date_label", "") or "") for r in selected if str(_row_get(r, "date_label", "") or "").strip()})
    header_lines = 8
    footer_lines = 5
    est_h = (header_lines + row_count * 2 + date_header_count + footer_lines) * line_h + item_line_count * small_line_h + 25 * mm
    paper_h = max(140 * mm, min(1200 * mm, est_h))
    top_safe = 8 * mm

    c = canvas.Canvas(str(pdf_path), pagesize=(paper_w, paper_h))
    left = 4 * mm
    right = paper_w - RIGHT_SAFE
    center = paper_w / 2
    y = paper_h - top_safe

    def _new_page():
        nonlocal y
        c.showPage()
        c.setPageSize((paper_w, paper_h))
        y = paper_h - top_safe

    def _ensure_space(min_y=18 * mm):
        nonlocal y
        if y < min_y:
            _new_page()

    def draw_left(text, bold=False, size=10):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= line_h

    def draw_left_small(text, bold=False, size=8):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left + 3 * mm, y, str(text))
        y -= small_line_h

    def draw_center(text, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawCentredString(center, y, str(text))
        y -= line_h

    def draw_right(text, bold=False, size=10):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawRightString(right, y, str(text))
        y -= line_h

    if shop_name:
        draw_center(shop_name, bold=True, size=16)
    try:
        sub = get_store_subtitle()
    except Exception:
        sub = ""
    if sub:
        draw_center(sub, size=10)
    draw_center(title, bold=True, size=12)

    dates = [str(_row_get(r, "date_label", "") or "") for r in selected if str(_row_get(r, "date_label", "") or "").strip()]
    if dates:
        draw_center(f"{dates[0]} to {dates[-1]}", size=9)
    draw_left("-" * 42, size=10)

    total = 0.0
    current_day = None
    for r in selected:
        date_label = str(_row_get(r, "date_label", "") or "")
        printed_time = str(_row_get(r, "printed_time", "") or "")
        receipt_code = str(
            _row_get(r, "printed_receipt_code", "")
            or _row_get(r, "print_receipt_no", "")
            or _row_get(r, "receipt_code", "")
            or _row_get(r, "sale_id", "")
        )
        try:
            amount = float(_row_get(r, "printed_amount", 0.0) or 0.0)
        except Exception:
            amount = 0.0
        total += amount

        if date_label != current_day:
            _ensure_space(28 * mm)
            if current_day is not None:
                draw_left("")
            draw_left(date_label, bold=True, size=10)
            current_day = date_label

        _ensure_space(24 * mm)
        left_text = f"{printed_time}".strip()
        right_text = f"${amount:.2f}"
        dots = "." * max(1, 34 - len(left_text) - len(right_text))
        draw_left(f"{left_text}{dots}{right_text}", size=10)
        draw_left(f"Receipt #{receipt_code}", size=9)

        items = list(_row_get(r, "items", []) or [])
        for it in items:
            try:
                qty = int(_row_get(it, "qty", 0) or 0)
            except Exception:
                qty = 0
            qty = max(1, qty)
            name = str(_row_get(it, "name", "") or "").strip()
            if not name:
                continue
            prefix = f"{qty}x "
            max_chars = 28
            wrapped = [name[i:i + max_chars] for i in range(0, len(name), max_chars)] or [""]
            draw_left_small(prefix + wrapped[0], size=8)
            for extra in wrapped[1:]:
                draw_left_small("   " + extra, size=8)
            _ensure_space(18 * mm)

    draw_left("-" * 42, size=10)
    draw_right(f"TOTAL: ${total:.2f}", bold=True, size=12)
    draw_left("")
    draw_center("Custom print-only receipt", size=9)
    draw_center("Does not change saved sales", size=9)
    c.save()
    return pdf_path


def create_warehouse_locations_pdf(shop_name: str, items, title: str = "Warehouse Locations") -> Path:
    """Create a receipt-style product list with location/section and price."""
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / f"warehouse_locations_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    selected = list(items or [])
    paper_w = 80 * mm
    line_h = 5.0 * mm
    small_h = 4.0 * mm
    item_lines = 0
    for item in selected:
        name = str(_row_get(item, "name", "") or "")
        location = str(_row_get(item, "location", "") or "")
        item_lines += max(1, (len(name) // 28) + 1)
        item_lines += max(1, (len(location) // 28) + 1)
        barcode = str(_row_get(item, "barcode", "") or "")
        if barcode:
            item_lines += 1

    header_lines = 8
    footer_lines = 3
    est_h = (header_lines + footer_lines) * line_h + max(1, item_lines) * small_h + 20 * mm
    paper_h = max(120 * mm, min(1200 * mm, est_h))

    c = canvas.Canvas(str(pdf_path), pagesize=(paper_w, paper_h))
    left = 4 * mm
    right = paper_w - RIGHT_SAFE
    center = paper_w / 2
    y = paper_h - 8 * mm

    def ensure_space(min_y=18 * mm):
        nonlocal y
        if y < min_y:
            c.showPage()
            c.setPageSize((paper_w, paper_h))
            y = paper_h - 8 * mm

    def draw_center(text, bold=False, size=11):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawCentredString(center, y, str(text))
        y -= line_h

    def draw_left(text="", bold=False, size=9):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= line_h

    def draw_pair(left_text, right_text, bold=False, size=9):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(left_text))
        c.drawRightString(right, y, str(right_text))
        y -= line_h

    def draw_wrapped(label, text, bold_label=True):
        nonlocal y
        text = str(text or "")
        chunks = [text[i:i + 32] for i in range(0, len(text), 32)] or [""]
        c.setFont("Helvetica-Bold" if bold_label else "Helvetica", 8)
        c.drawString(left, y, str(label))
        c.setFont("Helvetica", 8)
        c.drawString(left + 18 * mm, y, chunks[0])
        y -= small_h
        for chunk in chunks[1:]:
            c.drawString(left + 18 * mm, y, chunk)
            y -= small_h

    def draw_location_box(text):
        nonlocal y
        text = str(text or "").strip() or "NO LOCATION"
        chunks = [text[i:i + 25] for i in range(0, len(text), 25)] or [text]
        box_h = (7 + max(1, len(chunks)) * 5) * mm
        ensure_space(box_h + 12 * mm)
        c.setFillColorRGB(0.90, 0.96, 1.0)
        c.setStrokeColorRGB(0.12, 0.36, 0.80)
        c.rect(left, y - box_h + 2 * mm, right - left, box_h, fill=1, stroke=1)
        c.setFillColorRGB(0.08, 0.18, 0.36)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(left + 2 * mm, y - 3 * mm, "LOCATION / SECTION")
        y -= 8 * mm
        c.setFillColorRGB(0.03, 0.20, 0.55)
        c.setFont("Helvetica-Bold", 16)
        for chunk in chunks:
            c.drawString(left + 2 * mm, y, chunk)
            y -= 5 * mm
        c.setFillColorRGB(0, 0, 0)
        c.setStrokeColorRGB(0, 0, 0)
        y -= 2 * mm

    if shop_name:
        draw_center(shop_name, bold=True, size=15)
    try:
        sub = get_store_subtitle()
    except Exception:
        sub = ""
    if sub:
        draw_center(sub, size=9)
    draw_center(title, bold=True, size=12)
    draw_center("LOCATION-FIRST PICK LIST", bold=True, size=9)
    draw_center(datetime.now().strftime("%Y-%m-%d %I:%M %p"), size=8)
    draw_left("-" * 42, size=9)

    total = 0.0
    for index, item in enumerate(selected, 1):
        ensure_space()
        name = str(_row_get(item, "name", "") or "Product").strip() or "Product"
        barcode = str(_row_get(item, "barcode", "") or "").strip()
        location = str(_row_get(item, "location", "") or "").strip()
        try:
            price = float(_row_get(item, "price", 0) or 0)
        except Exception:
            price = 0.0
        total += price

        draw_pair(f"{index}. {name[:26]}", f"${price:.2f}", bold=True, size=9)
        draw_location_box(location)
        if barcode:
            draw_wrapped("Barcode", barcode)
        draw_left("-" * 42, size=8)

    draw_pair("Items", str(len(selected)), bold=True, size=9)
    draw_pair("Price total", f"${total:.2f}", bold=True, size=9)
    draw_center("Warehouse copy", size=8)
    c.save()
    return pdf_path
