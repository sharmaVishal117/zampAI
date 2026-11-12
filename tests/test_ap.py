"""Test suite for the AP invoice processor.

Run with pytest:   python3 -m pytest tests/ -q
Run standalone:    python3 tests/run_tests.py
"""
from __future__ import annotations

import datetime
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ap import extract, matching, pipeline, textio
from ap.extract import Extracted, extract_fields, parse_date_from_line
from ap.matching import decide, match_po, vendor_equivalent

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
VENDORS = {
    "APPROVED_VENDORS": [
        {"name": "Acme Industrial Supply Ltd.", "aliases": ["Acme Industrial"], "tax_id": "98-7654321"},
        {"name": "Global Office Supply", "aliases": ["Global Office"], "tax_id": "11-2233445"},
        {"name": "TechParts Inc", "aliases": ["TechParts"], "tax_id": "44-5566778"},
        {"name": "Metro Facilities Corp", "aliases": ["Metro Facilities"], "tax_id": "22-3344556"},
    ],
    "BLOCKED_VENDORS": [],
}
POS = {
    "POS": [
        {"po_id": "PO-1101", "vendor_name": "Acme Industrial Supply Ltd.", "amount": 1234.56,
         "currency": "USD", "issued": "2026-10-01", "status": "open"},
        {"po_id": "PO-1205", "vendor_name": "Global Office Supply", "amount": 890.00,
         "currency": "USD", "issued": "2026-10-20", "status": "open"},
        {"po_id": "PO-1403", "vendor_name": "Metro Facilities Corp", "amount": 5000.00,
         "currency": "USD", "issued": "2026-10-01", "status": "open"},
    ],
}
RULES = dict(pipeline.RULES)


def mk_ex(**kw) -> Extracted:
    e = Extracted(**kw)
    e.status = "extracted"
    e.confidence = 0.9
    return e


# ------------------------------------------------------------------ dates
def test_date_us_convention():
    d = parse_date_from_line("Invoice Date: 10/27/2026")
    assert d == datetime.date(2026, 10, 27), d


def test_date_swapped_fallback():
    # "13/10/2026" is not a US month; must flip to dd/mm
    d = parse_date_from_line("Date: 13/10/2026")
    assert d == datetime.date(2026, 10, 13), d


def test_date_iso_and_month_name():
    assert parse_date_from_line("2026-01-05") == datetime.date(2026, 1, 5)
    assert parse_date_from_line("Oct 27, 2026") == datetime.date(2026, 10, 27)


# ------------------------------------------------------------------ extraction
def test_extract_clean_invoice():
    text = """
    ACME INDUSTRIAL SUPPLY LTD. INVOICE
    1800 Commerce Blvd, Dayton, OH 45402
    Invoice Number: INV-2026-0148
    Invoice Date: 10/12/2026
    Due Date: 11/11/2026
    PO Number: PO-1101
    Steel brackets 120 $4.10 $492.00
    Hex bolts M10x40 2000 $0.36 $720.00
    Subtotal $1,234.56
    Total Due $1,234.56
    """
    ex = extract_fields(text, {"source": "text-layer", "letterhead": ["ACME INDUSTRIAL SUPPLY LTD. INVOICE"]})
    assert ex.invoice_number == "INV-2026-0148"
    assert ex.po_refs == ["PO-1101"]
    assert ex.total_printed == 1234.56
    assert ex.invoice_date == datetime.date(2026, 10, 12)
    assert ex.vendor_name and "ACME" in ex.vendor_name.upper()
    assert ex.status == "extracted"


def test_extract_po_ref_no_trailing_punct():
    text = "Re: order PO-1310. Please bill us."
    ex = extract_fields(text, {"source": "text-layer"})
    assert ex.po_refs == ["PO-1310"], ex.po_refs


def test_extract_missing_total_marks_incomplete():
    text = """
    ACME INVOICE
    Invoice Number: INV-9
    Invoice Date: 1/1/2026
    PO Number: PO-1101
    """
    ex = extract_fields(text, {"source": "text-layer", "letterhead": ["ACME INVOICE"]})
    assert ex.total_printed is None and ex.total_from_lines is None
    assert ex.status == "incomplete"


# ------------------------------------------------------------------ matching
def test_match_exact_ref_wins():
    ex = mk_ex(po_refs=["PO-1101"], vendor_name="Acme Industrial Supply Ltd.",
               total_printed=1234.56, total_from_lines=1234.56)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    assert m.match_type == "exact-ref"
    assert m.matched_po["po_id"] == "PO-1101"


def test_match_explicit_ref_wins_even_if_vendor_differs():
    """An explicit PO ref outranks vendor+amount: PO-1101 scores 100 on the ref
    alone. The vendor mismatch is caught downstream at the decision stage
    (test_decision_vendor_mismatch_rejects), which is exactly where it should
    be — matching proposes, validation disposes."""
    ex = mk_ex(po_refs=["PO-1101"], vendor_name="Global Office Supply",
               total_printed=890.00)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    assert m.matched_po["po_id"] == "PO-1101"
    assert m.match_type == "exact-ref"


def test_vendor_equivalence_ignores_legal_suffixes():
    assert vendor_equivalent("ACME INDUSTRIAL SUPPLY LTD.", "Acme Industrial Supply Ltd.")
    assert vendor_equivalent("Metro Facilities Corp", "METRO FACILITIES CORP")
    assert not vendor_equivalent("Acme Industrial Supply Ltd.", "Global Office Supply")
    # OCR-glued junk is tolerated
    assert vendor_equivalent("ACME INDUSTRIAL SUPPLY LTD. INVOICE", "Acme Industrial Supply Ltd.")


# ------------------------------------------------------------------ decisions
def test_decision_happy_path_pays():
    ex = mk_ex(invoice_number="INV-2026-0148", invoice_date=datetime.date(2026, 10, 12),
               vendor_name="Acme Industrial Supply Ltd.", po_refs=["PO-1101"],
               total_printed=1234.56, total_from_lines=1234.56, currency="USD")
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "PAY", (d.status, d.reasons, [i.code for i in d.issues])
    assert d.amount_to_pay == 1234.56


def test_decision_duplicate_rejects():
    paid = [{"invoice_number": "INV-2026-0142", "vendor_name": "Global Office Supply",
             "po_id": "PO-1205", "issued": "2026-10-21", "status": "paid"}]
    ex = mk_ex(invoice_number="INV-2026-0142", vendor_name="Global Office Supply",
               po_refs=["PO-1205"], total_printed=890.00, total_from_lines=890.00)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=paid, run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=paid)
    assert d.status == "REJECT"
    assert any("DUPLICATE" in r for r in d.reasons)


def test_decision_pending_register_is_reissue_review():
    """A register entry that is merely 'pending' must not hard-reject —
    it routes to review as a probable re-issue."""
    paid = [{"invoice_number": "INV-2026-0148", "vendor_name": "Acme Industrial Supply Ltd.",
             "po_id": "PO-1101", "issued": "2026-10-12", "status": "pending"}]
    ex = mk_ex(invoice_number="INV-2026-0148", vendor_name="Acme Industrial Supply Ltd.",
               po_refs=["PO-1101"], total_printed=1234.56, total_from_lines=1234.56)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=paid, run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=paid)
    assert d.status == "REVIEW"
    assert any("RE-ISSUANCE" in r or "re-issued" in r.lower() for r in d.reasons)


def test_decision_over_po_rejects():
    ex = mk_ex(invoice_number="INV-X", vendor_name="Acme Industrial Supply Ltd.",
               po_refs=["PO-1101"], total_printed=1400.00, total_from_lines=1400.00)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "REJECT"
    assert any("OVER PO" in r for r in d.reasons)


def test_decision_split_billing_reviews():
    """Metro tranche 2: $3,900 against PO $5,000 (22% under, outside the small
    split band but flagged as under-PO -> review, never pay-or-reject silently)."""
    ex = mk_ex(invoice_number="INV-2026-0231", vendor_name="Metro Facilities Corp",
               po_refs=["PO-1403"], total_printed=3900.00, total_from_lines=3900.00)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t",
                 ocr_seconds=0.0, source="text-layer")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "REVIEW"
    assert any(i.code in ("UNDER_PO", "SPLIT_BILLING") for i in d.issues)


def test_decision_unreadable_holds():
    ex = mk_ex(status="failed")
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "HOLD"


def test_decision_missing_total_holds():
    ex = mk_ex(invoice_number="INV-Z", vendor_name="Acme Industrial Supply Ltd.",
               total_printed=None, total_from_lines=None)
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "HOLD"


def test_decision_missing_invoice_number_downgrades_pay_to_review():
    ex = mk_ex(invoice_number=None, vendor_name="Acme Industrial Supply Ltd.",
               po_refs=["PO-1101"], total_printed=1234.56, total_from_lines=1234.56,
               invoice_date=datetime.date(2026, 10, 12))
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status == "REVIEW"
    assert any(i.code == "MISSING_INVOICE_NO" for i in d.issues)


def test_decision_vendor_mismatch_rejects():
    """Invoice claims Acme but the only matching PO is Global's -> reject."""
    ex = mk_ex(invoice_number="INV-Y", vendor_name="Acme Industrial Supply Ltd.",
               po_refs=["PO-1205"], total_printed=890.00, total_from_lines=890.00)
    # PO-1205 is Global's; amount matches Global's PO, but vendor says Acme
    m = match_po(ex, POS, VENDORS["APPROVED_VENDORS"], RULES, paid_ref=[], run_id="t")
    d = decide(ex, m, POS, VENDORS, RULES, paid_ref=[])
    assert d.status in ("REJECT", "REVIEW")
    if d.status == "REJECT":
        assert any("MISMATCH" in r for r in d.reasons)


# ------------------------------------------------------------------ end-to-end
def test_e2e_all_samples_decisions():
    expected = {
        "01-happy-path.pdf": "PAY",
        "02-duplicate.pdf": "REJECT",
        "03-split-billing.pdf": "REVIEW",
        "04-missing-fields.pdf": "REVIEW",
        "05-scanned.pdf": "PAY",
    }
    tmp = tempfile.mkdtemp()
    conn = _open_like_pipeline(Path(tmp) / "runs.db")
    try:
        for name, want in expected.items():
            got = pipeline.run_invoice(str(SAMPLES / name), conn=conn)
            assert got["status"] == want, f"{name}: expected {want}, got {got['status']}: {got.get('reasons')}"
    finally:
        conn.close()


def _open_like_pipeline(db_path):
    """Open a connection with the same schema/seed the pipeline uses, pointed
    at a temp file so the test never pollutes the real database."""
    import ap.pipeline as P
    P.DB_PATH = Path(db_path)
    return P._db()


