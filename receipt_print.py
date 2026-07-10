import win32print

from backend import get_store_subtitle


def _split_legacy(name: str) -> tuple[str, str]:
    s = str(name or "")
    if "\\n" in s:
        parts = [p.strip() for p in s.split("\\n")]
    else:
        parts = [p.strip() for p in s.replace("\r", "\n").split("\n")]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:]).strip()


def _wheel_prize_for_sale(sale) -> str:
    notes = str(sale.get("notes", "") or "")
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
    return pm.replace("_", " ").title()


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
    notes = str(sale.get("notes", "") or "")
    parts = [
        ("Cash", _note_float(notes, "PAYMENT_CASH")),
        ("Whish", _note_float(notes, "PAYMENT_WHISH")),
        ("Credit Card", _note_float(notes, "PAYMENT_CARD")),
    ]
    return [(label, amount) for label, amount in parts if amount > 0.005]


def print_receipt(printer_name: str, shop_name: str, sale, items):
    # Header lines
    name1, legacy_sub = _split_legacy(shop_name)
    subtitle = ""
    try:
        subtitle = (get_store_subtitle() or "").strip()
    except Exception:
        subtitle = ""
    if not subtitle:
        subtitle = legacy_sub

    lines: list[str] = []
    if name1:
        lines.append(name1)
    if subtitle:
        lines.append(subtitle)

    lines.append("-" * 32)

    # Sale header
    sid = sale.get("receipt_code") or sale.get("id", "")
    lines.append(f"Receipt #{sid}")
    lines.append(f"Date: {sale.get('created_at', '')}")
    lines.append(f"Pay:  {_payment_label(sale.get('payment_method', 'CASH'))}")
    shift_id = sale.get("shift_id", "")
    if shift_id:
        lines.append(f"Shift:{shift_id}")
    lines.append("-" * 32)

    # Items
    total_calc = 0.0
    pre_subtotal = 0.0
    for it in items:
        name = str(it.get("name", ""))[:20]
        try:
            qty = int(it.get("qty", 1))
        except Exception:
            qty = 1
        try:
            unit = float(it.get("price", it.get("unit_price", it.get("sell_price", 0.0))))
        except Exception:
            unit = 0.0
        try:
            line_total = float(it.get("line_total", qty * unit))
        except Exception:
            line_total = qty * unit

        pre_subtotal += qty * unit
        total_calc += line_total
        # format: NAME xQTY ..... $TOTAL
        left = f"{name} x{qty}"
        right = f"${line_total:.2f}"
        dots = "." * max(1, 32 - len(left) - len(right))
        lines.append(f"{left}{dots}{right}")

    lines.append("-" * 32)

    # Totals (use sale total if present)
    try:
        sale_total = float(sale.get("total_amount", total_calc))
    except Exception:
        sale_total = total_calc

    discount_amt = round(pre_subtotal - total_calc, 2)
    if discount_amt < 0:
        discount_amt = 0.0

    try:
        credit_amt = float(sale.get("store_credit_used", 0.0) or 0.0)
    except Exception:
        credit_amt = 0.0

    if discount_amt > 0.009:
        lines.append(f"SUBTOTAL: ${pre_subtotal:.2f}")
        lines.append(f"DISCOUNT: -${discount_amt:.2f}")
    if credit_amt > 0.009:
        lines.append(f"EXCHANGE CREDIT: -${credit_amt:.2f}")
    lines.append(f"TOTAL: ${sale_total:.2f}")
    payment_breakdown = _payment_breakdown(sale)
    if len(payment_breakdown) > 1:
        for label, amount in payment_breakdown:
            lines.append(f"{label}: ${amount:.2f}")
    wheel_prize = _wheel_prize_for_sale(sale)
    if wheel_prize:
        lines.append("")
        lines.append(f"Wheel prize: {wheel_prize}")
    lines.append("")
    lines.append("Thank you!")
    lines.append("")
    lines.append("")

    text = "\r\n".join(lines)

    hPrinter = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(hPrinter, 1, ("Receipt", None, "RAW"))
        win32print.StartPagePrinter(hPrinter)
        win32print.WritePrinter(hPrinter, text.encode("utf-8", errors="ignore"))
        win32print.EndPagePrinter(hPrinter)
        win32print.EndDocPrinter(hPrinter)
    finally:
        win32print.ClosePrinter(hPrinter)
