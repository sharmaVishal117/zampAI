"""Generate the 5 sample invoices used as test/demo inputs.

Run:  python3 -m ap.gen_samples
Writes into ../samples/ and refreshes the sample manifest.

Layouts deliberately differ (letterhead style, tax handling, PO ref style,
itemised vs bundled) so the extraction logic actually has work to do.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as rlcanvas

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "samples"
W, H = A4  # 595.28 x 841.89 pt


def _money(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------- helpers
def draw_header(c: rlcanvas.Canvas, vendor: str, tagline: str, right_lines: list[str],
                invoice_word: str = "INVOICE"):
    c.setFont("Helvetica-Bold", 20)
    c.drawString(60, 780, vendor.upper())
    c.setFont("Helvetica", 8.5)
    c.drawString(60, 768, tagline)
    c.setFont("Helvetica-Bold", 18)
    c.drawRightString(W - 60, 780, invoice_word)
    c.setFont("Helvetica", 8.5)
    for i, ln in enumerate(right_lines):
        c.drawRightString(W - 60, 764 - i * 11, ln)
    c.setStrokeColorRGB(0.2, 0.2, 0.6)
    c.line(60, 752, W - 60, 752)
    c.setStrokeColorRGB(0, 0, 0)


def draw_bittor_ship(c, bill_to: list[str], ship_to: list[str] | None = None, y=712):
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "BILL TO")
    c.setFont("Helvetica", 9)
    yy = y - 12
    for ln in bill_to:
        c.drawString(60, yy, ln)
        yy -= 11
    if ship_to:
        sx = 320
        c.setFont("Helvetica-Bold", 9)
        c.drawString(sx, y, "SHIP TO")
        c.setFont("Helvetica", 9)
        sy = y - 12
        for ln in ship_to:
            c.drawString(sx, sy, ln)
            sy -= 11
    return yy


def draw_meta_block(c, meta: list[tuple[str, str]], x=320, y=700):
    c.setFont("Helvetica", 9)
    yy = y
    for label, val in meta:
        c.drawString(x, yy, label)
        c.drawString(x + 105, yy, val)
        yy -= 12
    return yy


def draw_items(c, items: list[tuple[str, str, float, float]], y: float = 560) -> float:
    """items: (desc, qty, unit_price, amount). Draws a simple table."""
    c.setFont("Helvetica-Bold", 9)
    c.drawString(60, y, "DESCRIPTION")
    c.drawRightString(470, y, "UNIT PRICE")
    c.drawRightString(W - 60, y, "AMOUNT")
    c.setFont("Helvetica", 9)
    yy = y - 14
    c.setStrokeColorRGB(0.8, 0.8, 0.85)
    c.line(60, y - 4, W - 60, y - 4)
    c.setStrokeColorRGB(0, 0, 0)
    for desc, qty, up, amt in items:
        c.drawString(60, yy, f"{desc}")
        c.drawString(250, yy, qty)
        c.drawRightString(470, yy, _money(up))
        c.drawRightString(W - 60, yy, _money(amt))
        yy -= 13
    return yy


def draw_totals_block(c, items_y: float, subtotal, tax, total, tax_label="Sales tax (8.875%)",
                      x=330, due_text=None) -> float:
    yy = items_y - 18
    c.setFont("Helvetica", 9)
    rows = [("Subtotal", _money(subtotal)), (tax_label, _money(tax)), ("Total Due", _money(total))]
    c.setFont("Helvetica-Bold", 9)
    for i, (label, val) in enumerate(rows):
        bold = (i == 2)
        c.setFont("Helvetica-Bold" if bold else "Helvetica", 9)
        c.drawString(x + 40, yy, label)
        c.drawRightString(W - 60, yy, val)
        if bold:
            c.line(x + 30, yy + 3, W - 60, yy + 3)
        yy -= 13
    if due_text:
        c.setFont("Helvetica", 8.5)
        c.drawString(x + 40, yy, due_text)
    return yy


def footer(c, note: str = None, y=120):
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(60, y, "Thank you for your business. Payment terms: NET 30 from invoice date.")
    if note:
        c.drawString(60, y - 12, note)


# ---------------------------------------------------------------- the 5 invoices
def inv_happy(path: str):
    """Digital, clean, itemised, embedded tax, explicit PO ref. -> expect PAY"""
    c = rlcanvas.Canvas(path, pagesize=A4)
    draw_header(c, "Acme Industrial Supply Ltd.",
                "1800 Commerce Blvd, Dayton, OH 45402  ·  +1 (937) 555-0142  ·  Tax ID: 98-7654321",
                ["PO Number: PO-1101", "Order Date: 2026-10-01"])
    yy = draw_bittor_ship(c,
                           ["Northgate Manufacturing Co.", "Attn: Accounts Payable", "4400 Industrial Pkwy", "Grand Rapids, MI 49544"])
    draw_meta_block(c, [
        ("Invoice Number:", "INV-2026-0148"),
        ("Invoice Date:", "2026-10-12"),
        ("Due Date:", "2026-11-11"),
        ("Payment Terms:", "NET 30"),
    ], y=yy - 6)
    # itemised amounts sum exactly to the embedded-tax grand total -> clean PAY
    # 492.00 + 720.00 + 22.56 = 1,234.56
    items = [
        ("Steel brackets, 12-gauge (part #A-118)", "120", 4.10, 492.00),
        ("Hex bolts M10x40, zinc, bulk", "2000", 0.36, 720.00),
        ("Anti-vibration mounts, medium", "12", 1.88, 22.56),
    ]
    iy = draw_items(c, items, y=560)
    draw_totals_block(c, iy, subtotal=1234.56, tax=0.0, total=1234.56,
                      tax_label="Tax (embedded)", due_text="Amount Due (USD): $1,234.56")
    footer(c)
    c.save()


def inv_duplicate(path: str):
    """Same invoice number + total as a record already in the processed register.
   -> expect REJECT (duplicate)"""
    c = rlcanvas.Canvas(path, pagesize=A4)
    draw_header(c, "Global Office Supply",
                "PO Box 447, Columbus, OH 43216  ·  www.globalofficesupply.com  ·  Tax ID: 11-2233445",
                ["INVOICE", "No: INV-2026-0142", "Date: 10/21/2026", "PO Ref: PO-1205"])
    yy = draw_bittor_ship(c,
                           ["Northgate Manufacturing Co.", "Accounts Payable", "4400 Industrial Pkwy, Grand Rapids, MI"])
    draw_meta_block(c, [("Terms:", "NET 30"), ("Due:", "11/20/2026"), ("Currency:", "USD")], y=640)
    items = [
        ("Warehouse boxes, corrugated 20x12x10 (pallet)", "1", 540.00, 540.00),
        ("Stretch wrap rolls, 18\"", "8", 24.50, 196.00),
        ("Safety gloves, nitrile L (case of 200)", "1", 120.00, 120.00),
        ("Shrink-sealer refills", "1", 34.00, 34.00),
    ]
    sub = 890.00
    iy = draw_items(c, items, y=560)
    draw_totals_block(c, iy, subtotal=sub, tax=0.0, total=sub, tax_label="Tax (included)",
                      due_text="TOTAL DUE: $890.00")
    footer(c, "Please reference invoice number INV-2026-0142 on your remittance.")
    c.save()


def inv_split(path: str):
    """Split-billing: Metro bills the maintenance contract in 3 tranches against
    PO-1403 ($5,000). This invoice is tranche 2: $3,900.00 -> expect REVIEW
    (under PO, likely split billing)."""
    c = rlcanvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica-Bold", 18)
    c.drawString(60, 780, "METRO FACILITIES CORP")
    c.setFont("Helvetica", 8.5)
    c.drawString(60, 768, "2200 Harbor Drive, Cleveland, OH 44114  ·  Tax ID 22-3344556  ·  billing@metrofac.com")
    c.setFont("Helvetica", 9)
    c.drawString(60, 745, "This is a split invoice. Tranche 2 of 3 against purchase order PO-1403.")
    yy = draw_bittor_ship(c,
                           ["Northgate Manufacturing Co.", "AP Department", "4400 Industrial Pkwy", "Grand Rapids, MI 49544"],
                           y=700)
    draw_meta_block(c, [
        ("Invoice No:", "INV-2026-0231"),
        ("Invoice Date:", "10/27/2026"),
        ("Purchase Order:", "PO-1403 (tranche 2 of 3)"),
        ("Due:", "NET 30 — 11/26/2026"),
    ], y=640)
    items = [
        ("Maintenance — October cycle (parts & labor)", "1", 2450.00, 2450.00),
        ("HVAC filter replacement, 12 units", "12", 79.17, 950.00),
        ("Emergency call-out — chiller #2", "1", 500.00, 500.00),
    ]
    sub = 3900.00
    iy = draw_items(c, items, y=560)
    draw_totals_block(c, iy, subtotal=sub, tax=0.0, total=sub, tax_label="Tax (inclusive)",
                      due_text="TOTAL DUE: $3,900.00   ·   Balance expected on tranche 3: $1,100.00")
    footer(c, "Tranche 3 (December cycle) will be invoiced separately under the same PO reference.")
    c.save()


def inv_missing(path: str):
    """No invoice number, no invoice date, no explicit PO label — only the house
    PO number appears in the narrative. -> expect REVIEW (missing fields)"""
    c = rlcanvas.Canvas(path, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(60, 780, "TECHPARTS INC")
    c.setFont("Helvetica", 8.5)
    c.drawString(60, 768, "Building C, 9400 Innovation Way, San Diego, CA 92101")
    yy = draw_bittor_ship(c,
                           ["Northgate Manufacturing Co.", "Accounts Payable", "4400 Industrial Pkwy, Grand Rapids, MI"],
                           y=700)
    # NO "Invoice Number" line on purpose. Only a date-ish line for terms.
    draw_meta_block(c, [("Supplier:", "TechParts Inc"), ("Tax ID:", "44-5566778"), ("Terms:", "NET 30")], y=640)
    c.setFont("Helvetica", 9)
    c.drawString(60, 600, "Re: Replacement circuit boards, part #4471 — as requested under purchase order PO-1310.")
    items = [
        ("Circuit board assembly #4471", "40", 118.75, 4750.00),
        ("Calibration & test certification", "1", 350.00, 350.00),
    ]
    sub = 5100.00
    iy = draw_items(c, items, y=560)
    draw_totals_block(c, iy, subtotal=sub, tax=0.0, total=sub, tax_label="Tax (included)",
                      due_text="TOTAL DUE: $5,100.00")
    footer(c)
    c.save()


def inv_scanned(path: str):
    """A genuine image-only PDF (no text layer). Nordwind... actually Metro,
    tranche 1 against PO-1402 ($4,000) -> expect PAY via OCR path."""
    # build a temporary digital version first
    with tempfile.TemporaryDirectory() as td:
        tmp = os.path.join(td, "dig.pdf")
        c = rlcanvas.Canvas(tmp, pagesize=A4)
        c.setFont("Helvetica-Bold", 18)
        c.drawString(60, 780, "METRO FACILITIES CORP")
        c.setFont("Helvetica", 8.5)
        c.drawString(60, 768, "2200 Harbor Drive, Cleveland, OH 44114  ·  Tax ID 22-3344556")
        yy = draw_bittor_ship(c,
                               ["Northgate Manufacturing Co.", "AP Department", "4400 Industrial Pkwy, Grand Rapids, MI"],
                               y=700)
        draw_meta_block(c, [
            ("Invoice No:", "INV-2026-0209"),
            ("Invoice Date:", "10/05/2026"),
            ("PO Reference:", "PO-1402 (tranche 1 of 3)"),
            ("Due:", "NET 30 — 11/04/2026"),
        ], y=640)
        items = [
            ("Maintenance — September cycle (labor)", "1", 2800.00, 2800.00),
            ("Roof membrane repair, east wing", "1", 1200.00, 1200.00),
        ]
        sub = 4000.00
        iy = draw_items(c, items, y=560)
        draw_totals_block(c, iy, subtotal=sub, tax=0.0, total=sub, tax_label="Tax (inclusive)",
                          due_text="TOTAL DUE: $4,000.00  ·  Tranches 2 and 3 to follow")
        footer(c, "Scanned copy — original on file at Metro Facilities Corp.")
        c.save()

        # rasterize then embed as image -> true scan, no text layer
        png = os.path.join(td, "pg.png")
        subprocess.run(["pdftoppm", "-png", "-r", "200", tmp, os.path.join(td, "pg")],
                       check=True, capture_output=True)
        png = os.path.join(td, "pg-1.png")
        c2 = rlcanvas.Canvas(path, pagesize=A4)
        from reportlab.lib.utils import ImageReader
        img = ImageReader(png)
        iw, ih = img.getSize()
        # fit to page with 40pt margins
        scale = min((W - 80) / iw, (H - 80) / ih)
        dw, dh = iw * scale, ih * scale
        c2.drawImage(img, (W - dw) / 2, (H - dh) / 2, width=dw, height=dh)
        c2.save()


def main():
    OUT.mkdir(exist_ok=True)
    plan = [
        ("01-happy-path.pdf", inv_happy, "clean digital invoice, itemised, explicit PO ref — expect PAY"),
        ("02-duplicate.pdf", inv_duplicate, "invoice number already in processed register — expect REJECT (duplicate)"),
        ("03-split-billing.pdf", inv_split, "tranche 2 of 3 against a larger PO — expect REVIEW (under PO, split billing)"),
        ("04-missing-fields.pdf", inv_missing, "no invoice number, no date, PO only in narrative — expect REVIEW (missing fields)"),
        ("05-scanned.pdf", inv_scanned, "image-only PDF, no text layer — expect PAY via OCR path"),
    ]
    manifest = []
    for name, fn, desc in plan:
        out = OUT / name
        fn(str(out))
        manifest.append({"file": name, "description": desc,
                         "expect": desc.split("expect ")[-1]})
        print(f"  wrote {name}  ({out.stat().st_size//1024} KB)")
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n{len(manifest)} sample invoices in {OUT}")


if __name__ == "__main__":
    main()
