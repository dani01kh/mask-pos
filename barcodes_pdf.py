# barcodes_pdf.py
# Label size: 5.9 cm (W) x 2.7 cm (H)
# EAN-13 barcode (scanner-friendly)
# Polished layout:
# - Bigger product name
# - Bigger, clearer price
# - NO barcode numbers printed under the bars

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import tempfile

from reportlab.pdfgen import canvas
from reportlab.lib.units import cm, mm
from reportlab.graphics.shapes import Drawing
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.eanbc import Ean13BarcodeWidget


LABEL_W = 5.9 * cm
LABEL_H = 2.7 * cm


def _digits_only(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def _ean13_check_digit(num12: str) -> str:
    digits = [int(c) for c in num12]
    odd_sum = sum(digits[0::2])
    even_sum = sum(digits[1::2])
    total = odd_sum + 3 * even_sum
    return str((10 - (total % 10)) % 10)


def to_ean13(code: str) -> str:
    d = _digits_only(code)
    if len(d) == 13:
        return d
    base12 = d.zfill(12)[:12]
    return base12 + _ean13_check_digit(base12)


def make_labels_pdf(labels, title="Mask POS Labels"):
    out_dir = Path(tempfile.gettempdir()) / "mask_pos_pdfs"
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = out_dir / f"labels_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"

    c = canvas.Canvas(str(pdf_path), pagesize=(LABEL_W, LABEL_H))
    c.setTitle(title)

    # Margins
    margin_x = 0.18 * cm
    margin_top = 0.15 * cm
    margin_bottom = 0.12 * cm

    # Typography (tuned for readability)
    name_font = 11          # BIGGER product name
    price_font = 16         # VERY visible price

    for item in labels:
        name = str(item.get("name", "")).strip()
        try:
            price = float(item.get("price", 0.0))
        except Exception:
            price = 0.0

        raw_code = str(item.get("barcode", "")).strip()
        ean13 = to_ean13(raw_code)

        try:
            qty = int(item.get("qty", 1))
        except Exception:
            qty = 1
        if qty < 1:
            qty = 1

        for _ in range(qty):
            # -------- TOP: PRODUCT NAME --------
            top_y = LABEL_H - margin_top

            c.setFont("Helvetica-Bold", name_font)
            c.drawCentredString(
                LABEL_W / 2.0,
                top_y - name_font,
                name[:30]
            )

            # -------- PRICE (BIG & CLEAR) --------
            c.setFont("Helvetica-Bold", price_font)
            c.drawCentredString(
                LABEL_W / 2.0,
                top_y - name_font - price_font - 1,
                f"${price:.2f}"
            )

            # -------- BARCODE AREA --------
            barcode_top = top_y - name_font - price_font - 6
            barcode_bottom = margin_bottom
            box_h = max(1, barcode_top - barcode_bottom)
            box_w = LABEL_W - 2 * margin_x

            widget = Ean13BarcodeWidget(ean13)
            widget.barHeight = min(14 * mm, box_h)
            widget.barWidth = 0.35 * mm

            x0, y0, x1, y1 = widget.getBounds()
            w = (x1 - x0) or 1
            h = (y1 - y0) or 1

            scale = min(box_w / w, box_h / h)
            draw_w = w * scale
            draw_h = h * scale

            x = (LABEL_W - draw_w) / 2.0
            y = barcode_bottom + (box_h - draw_h) / 2.0

            d = Drawing(draw_w, draw_h)
            widget.x = -x0
            widget.y = -y0
            d.add(widget)
            d.scale(scale, scale)
            renderPDF.draw(d, c, x, y)

            # ❌ NO barcode numbers printed

            c.showPage()

    c.save()
    return pdf_path
