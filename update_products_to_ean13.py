import os
import sqlite3

try:
    from db_config import DB_FILE as DB_PATH
except Exception:
    DB_PATH = os.path.join(os.path.dirname(__file__), "pos.db")


def digits_only(s: str) -> str:
    return "".join(ch for ch in str(s) if ch.isdigit())


def ean13_check_digit(num12: str) -> str:
    digits = [int(c) for c in num12]
    odd_sum = sum(digits[0::2])
    even_sum = sum(digits[1::2])
    total = odd_sum + 3 * even_sum
    return str((10 - (total % 10)) % 10)


def to_ean13(code: str) -> str:
    d = digits_only(code)
    if len(d) == 13:
        return d
    base12 = d.zfill(12)[:12]
    return base12 + ean13_check_digit(base12)


def main():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"pos.db not found at: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Read current products
    cur.execute("SELECT id, barcode FROM products WHERE barcode IS NOT NULL AND barcode != ''")
    rows = cur.fetchall()

    updated = 0
    for pid, bc in rows:
        new_bc = to_ean13(bc)
        if str(bc) != new_bc:
            cur.execute("UPDATE products SET barcode=? WHERE id=?", (new_bc, pid))
            updated += 1

    conn.commit()
    conn.close()
    print(f"Done. Updated {updated} product barcodes to EAN-13.")


if __name__ == "__main__":
    main()
