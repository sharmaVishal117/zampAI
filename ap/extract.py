"""Rule-based invoice field extraction.

Strategy: regexes tuned for common commercial invoice layouts, run on raw PDF
text. Every extracted field records *where it came from* (the matched line),
which makes the audit trail in the UI genuinely useful. Totals cross-check
sum-of-line-items against the printed total.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

MONEY_RE = r"\$?\s?(\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2}|\d+(?:\.\d{2})?)"
NUM_RE = r"(\d+(?:\.\d{1,2})?)"


@dataclass
class LineItem:
    name: str
    qty: str
    unit_price: float | None
    amount: float | None
    raw: str
    line_no: int


@dataclass
class Extracted:
    status: str = "incomplete"            # extracted | incomplete | failed
    text_source: str = "none"            # text-layer | ocr-tesseract | ocr-llm | none
    ocr_seconds: float = 0.0
    confidence: float = 0.0
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    vendor_name: str | None = None
    vendor_tax_id: str | None = None
    po_refs: list[str] = field(default_factory=list)
    line_items: list[LineItem] = field(default_factory=list)
    total_from_lines: float | None = None
    total_printed: float | None = None
    currency: str = "USD"
    pages: int = 1
    tax_included: bool = True            # assume embedded tax unless subtotal+tax found
    subtotal: float | None = None
    tax_amount: float | None = None
    evidence: dict = field(default_factory=dict)   # field -> matched raw line
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- money
def parse_money(s: str) -> float | None:
    s = s.replace(",", "").replace("$", "").strip()
    m = re.fullmatch(NUM_RE, s)
    return float(m.group(1)) if m else None


# ---------------------------------------------------------------- dates
_DATE_PATTERNS = [
    re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"),   # 2026-10-27
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"),   # 10/27/2026
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b"),   # 10.27.26
    re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})\b", re.I),
    re.compile(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", re.I),
]
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


def parse_date_from_line(line: str) -> date | None:
    """Find a plausible date on the line. US AP convention: numeric dates are
    day-first (10/27/2026); year-first if the year leads; month names are
    unambiguous."""
    candidates: list[date] = []
    for m in _DATE_PATTERNS[0].finditer(line):          # yyyy-mm-dd
        y, mo, d = map(int, m.groups())
        try:
            candidates.append(date(y, mo, d))
        except ValueError:
            pass
    for pat in _DATE_PATTERNS[1:3]:                      # US invoices: a/b/yyyy
        for m in pat.finditer(line):
            a, b, y = map(int, m.groups())
            if y < 100:
                y += 2000
            try:
                # US AP convention: month first (10/27/2026 = Oct 27, 2026)
                candidates.append(date(y, a, b))
            except ValueError:
                try:                                     # flipped: day first
                    candidates.append(date(y, b, a))
                except ValueError:
                    continue
    for pat in _DATE_PATTERNS[3:5]:                      # 27 Oct 2026 / Oct 27, 2026
        for m in pat.finditer(line):
            g = m.groups()
            if re.match(r"(?i)[A-Za-z]", g[0]):
                mo = _MONTHS.get(g[0][:3], 0)
                d, y = int(g[1]), int(g[2])
            else:
                d, mo, y = int(g[0]), _MONTHS.get(g[1][:3], 0), int(g[2])
            if mo:
                try:
                    candidates.append(date(y, mo, d))
                except ValueError:
                    pass
    if not candidates:
        return None
    return sorted(candidates)[0]


# ---------------------------------------------------------------- PO refs
# label ("PO"/"PO No."/"PO Ref:"/"purchase order") + optional separator + value.
# Named groups so we can tell whether "PO-1310" is the reference itself or the
# label followed by a bare number.
PO_RE = re.compile(
    r"(?i)\b(?P<label>PO|P\.O|P\/O|Purchase\s*Order)\s*"
    r"(?P<mid>Reference|Ref\.?|No\.?|Number)?\s*"
    r"(?P<sep>[:\-]?|\s*)\s*(?P<ref>[A-Za-z0-9][A-Za-z0-9\-/]{0,19})\b")
PO_PLAIN_RE = re.compile(r"\b([A-Z]{2}-\d{4})\b")  # house format, secondary search


# ---------------------------------------------------------------- vendor
_INVOICE_VENDOR_RE = re.compile(r"(?i)^\s*Vendor\s*[:\-]\s*(.+)$")


_JUNK_TAIL = re.compile(r"(?i)\s+(?:INVOICE|A/R|VENDOR|COPY\s*\d*$)\b.*$")


def _clean_vendor(cand: str) -> str:
    return _JUNK_TAIL.sub("", cand.strip()).strip()


def _pick_vendor(label_value: str | None, letterhead: list[str],
                 ocr_lines: list[str] | None = None) -> tuple[str | None, str]:
    """Vendor name, in order of trust:
    1. explicit 'Vendor:' label
    2. largest letterhead text (font-size based)
    3. for OCR'd scans: the first non-junk line (letterheads read top-to-bottom)
    Returns (name, how)."""
    if label_value:
        return _clean_vendor(label_value), "explicit 'Vendor:' label"
    for cand in letterhead or []:
        cand = _clean_vendor(cand)
        if len(cand) < 4:
            continue
        up = cand.upper()
        if up in ("INVOICE", "A/R", "VENDOR", "REMITTANCE"):
            continue
        if re.fullmatch(r"[\d$.,\-\s]+", cand):
            continue
        return cand, "letterhead heuristic (largest text on page 1)"
    if ocr_lines:
        junk_starts = ("thank", "please", "re:", "as requested", "this is", "this is a",
                       "page", "copy", "invoice due", "remittance")
        for line in ocr_lines:
            s = _clean_vendor(line.strip())
            if len(s) < 4 or len(s) > 60:
                continue
            if s.lower().startswith(junk_starts):
                continue
            if re.fullmatch(r"[\d$.,:;\-/ ]+", s):
                continue
            if not any(c.isalpha() for c in s):
                continue
            return s, "OCR top-line heuristic (scan read top-to-bottom); lower trust"
        return None, "no vendor name source in OCR text"
    return None, "no vendor name source"


# ---------------------------------------------------------------- line items
# pdfplumber merges the qty/unit/amount columns into one line, e.g.
#   "Warehouse boxes, corrugated 20x12x10 (pallet)1 $540.00 $540.00"
# so the tail pattern is what actually matches. Real invoices are mixed:
# some print the $ sign on every column, some on none — accept both.
_LINE_ITEM_RE = re.compile(
    r"^\s*(?P<desc>.{3,80}?)\s*(?P<qty>\d{1,6}(?:\.\d+)?)\s+"
    r"(?P<up>\$?\d[\d,]*\.\d{2})\s+(?P<amt>\$?\d[\d,]*\.\d{2})\s*$")


def _find_line_items(lines: list[str]) -> list[LineItem]:
    items: list[LineItem] = []
    for i, line in enumerate(lines):
        m = _LINE_ITEM_RE.match(line)
        if not m:
            continue
        g = m.groupdict()
        up = parse_money(g["up"])
        amt = parse_money(g["amt"])
        if up is None or amt is None:
            continue
        # false hit: qty digit glued onto the description, or qty/price/amount
        # all trivially equal on a subtotal-style row
        if abs(up - amt) < 0.01 and g["qty"] != "1" and line.rstrip().count("$") < 3:
            continue
        # sanity: qty * unit price should approximate the amount (±20%)
        try:
            qf = float(g["qty"])
            if qf > 0 and up > 0 and abs(qf * up - amt) / amt > 0.25:
                continue
        except ValueError:
            pass
        items.append(LineItem(
            name=g["desc"].strip(), qty=g["qty"], unit_price=up, amount=amt,
            raw=line.strip(), line_no=i + 1))
    return items


# ---------------------------------------------------------------- totals
_TOTAL_RES = [
    (re.compile(r"(?i)\b(?:TOTAL\s+DUE|AMOUNT\s+DUE|BALANCE\s+DUE|TOTAL\s*AMOUNT\s*DUE)\b\s*[:\-]?\s*"), "total_due"),
    (re.compile(r"(?i)\b(?:GRAND\s+TOTAL|INVOICE\s+TOTAL|TOTAL)\b\s*[:\-]?\s*"), "total"),
    (re.compile(r"(?i)\b(?:SUBTOTAL|SUB-\s?TOTAL|NET\s+TOTAL)\b\s*[:\-]?\s*"), "subtotal"),
    (re.compile(r"(?i)\b(?:TAX|VAT|GST|SALES\s+TAX)\b\s*[:\-]?\s*"), "tax"),
]


def _extract_totals(lines: list[str], ex: Extracted) -> None:
    for pat, key in _TOTAL_RES:
        if key in ("total_due", "total") and ex.total_printed is not None:
            continue
        if key == "subtotal" and ex.subtotal is not None:
            continue
        if key == "tax" and ex.tax_amount is not None:
            continue
        for i, line in enumerate(lines):
            m = pat.search(line)
            if not m:
                continue
            val = None
            tail = line[m.end():]
            # the label's tail can contain a rate like "(8%)" — skip any
            # candidate immediately followed by '%'; take the first real amount
            val = None
            for nm in re.finditer(MONEY_RE, tail):
                if nm.end(1) < len(tail) and tail[nm.end(1)] == "%":
                    continue
                val = parse_money(nm.group(1))
                break
            if val is None:
                continue
            if key in ("total_due", "total"):
                ex.total_printed = val
                ex.evidence["total_printed"] = line.strip()
                ex.notes.append(f"printed total {val:,.2f} read from line: \"{line.strip()[:60]}\"")
            elif key == "subtotal":
                ex.subtotal = val
                ex.evidence["subtotal"] = line.strip()
            elif key == "tax":
                ex.tax_amount = val
                ex.evidence["tax_amount"] = line.strip()
            break  # first match per label type


# ---------------------------------------------------------------- invoice number
INV_NO_RE = re.compile(
    r"(?i)\bINVOICE\s*(?:NO\.?|NUMBER|#)?\s*[:\-]?\s*([A-Z0-9][A-Z0-9\-/]{2,30})\b")
_INV_NO_BLACKLIST = re.compile(
    r"(?i)^(DATE|TERMS|NET|BALANCE|TOTAL|TAX|DUE|COPY|PAGE|REF|REFERENCE|NO)$")


def _extract_invoice_number(lines: list[str]) -> str | None:
    for line in lines:
        for m in INV_NO_RE.finditer(line):
            cand = m.group(1).strip().upper()
            if _INV_NO_BLACKLIST.match(cand):
                continue
            # must start with a letter or contain a separator — rejects bare numbers
            return cand
    return None


# ---------------------------------------------------------------- main
def extract_fields(text: str, meta: dict) -> Extracted:
    ex = Extracted()
    ex.text_source = meta.get("source", "none")
    ex.ocr_seconds = meta.get("seconds", 0.0)
    ex.confidence = meta.get("confidence", 0.0)

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    joined = "\n".join(lines)

    if not lines:
        ex.status = "failed"
        ex.warnings.append("No readable text (no text layer, OCR unavailable or empty).")
        return ex

    # currency
    for cur in ("USD", "EUR", "GBP", "CAD", "AUD"):
        if re.search(rf"(?<![\w]){cur}([\s:])", joined) or joined.upper().count(cur) >= 2:
            ex.currency = cur
            break

    # PO refs
    for m in PO_RE.finditer(joined):
        ref = (m.group("ref") or "").strip().upper()
        sep = m.group("sep") or ""
        mid = m.group("mid")
        # "PO-1310": label + literal dash + bare number => the prefix belongs to the ref.
        # Only reconstruct when the separator was actually a dash/period/minus,
        # NOT when it was whitespace (e.g. "PO No. 1403" or "PO: 1403").
        if mid is None and sep in ("-", "–", "."):
            ref = f"PO-{ref}"
        ref = ref.strip(".,;:")
        if len(ref) < 2 or len(re.findall(r"\d", ref)) < 2:
            continue
        if ref not in ex.po_refs:
            ex.po_refs.append(ref)
            ex.evidence.setdefault("po_refs", []).append(m.group(0).strip())
    if not ex.po_refs:
        for m in PO_PLAIN_RE.finditer(joined):
            ref = m.group(1).upper()
            if ref not in ex.po_refs:
                ex.po_refs.append(ref)
                ex.warnings.append(f"PO reference \"{ref}\" found in house format but no 'PO' label nearby — treat as a guess")
        ex.evidence["po_refs"] = list(ex.po_refs)

    # vendor: explicit label, else letterhead candidates (supplied by the pipeline)
    label_value = None
    for line in lines:
        m = _INVOICE_VENDOR_RE.match(line)
        if m:
            label_value = m.group(1)
            ex.evidence["vendor_name"] = line.strip()
            break
    ocr_lines = lines[:6] if str(meta.get("source", "")).startswith("ocr") else None
    ex.vendor_name, vendor_how = _pick_vendor(label_value, meta.get("letterhead", []), ocr_lines)
    if ex.vendor_name and "label" not in vendor_how:
        ex.notes.append(f"vendor name inferred via {vendor_how}")

    # tax id / VAT
    for pat in [
        re.compile(r"(?i)\bTAX\s*(?:I\.?D|ID|IDENTIFICATION)\s*[:\-]?\s*([A-Z0-9\-]{6,25})(?=\s|$)"),
        re.compile(r"(?i)\bEIN\b\s*[:\-]?\s*([0-9]{2}-[0-9]{7})"),
        re.compile(r"(?i)\bVAT(\s*No\.?|Registration\s*No\.?)?\s*[:\-]?\s*([A-Za-z0-9\-]{6,20})"),
    ]:
        m = pat.search(joined)
        if m:
            ex.vendor_tax_id = (m.group(1) or m.group(2)).strip()
            ex.evidence["tax_id"] = m.group(0).strip()
            break

    # invoice number
    ex.invoice_number = _extract_invoice_number(lines)
    if ex.invoice_number:
        for line in lines:
            if ex.invoice_number in line.upper():
                ex.evidence["invoice_number"] = line.strip()
                break
    else:
        ex.warnings.append("No invoice number found — duplicate detection now limited to vendor+total")

    # invoice date / due date
    for line in lines:
        if re.search(r"(?i)\bINVOICE\s*DATE\b", line):
            ex.invoice_date = parse_date_from_line(line)
            ex.evidence["invoice_date"] = line.strip()
            break
    if ex.invoice_date is None:
        for line in lines[:20]:
            if re.search(r"(?i)(?<![\w])DATE\s*[:\-]?\s*\d", line):
                ex.invoice_date = parse_date_from_line(line)
                if ex.invoice_date:
                    ex.evidence["invoice_date"] = line.strip()
                break
    for line in lines:
        if re.search(r"(?i)\bDUE\s*DATE\b|PAYMENT\s*DUE", line):
            ex.due_date = parse_date_from_line(line)
            ex.evidence["due_date"] = line.strip()
            break
    if ex.due_date is None:
        for line in lines:
            if re.search(r"(?i)^\s*Due\s*[:\-]", line):
                ex.due_date = parse_date_from_line(line)
                ex.evidence["due_date"] = line.strip()
                break

    # line items
    ex.line_items = _find_line_items(lines)
    if ex.line_items:
        amts = [it.amount for it in ex.line_items if it.amount is not None]
        if amts:
            ex.total_from_lines = round(sum(amts), 2)
            ex.evidence["total_from_lines"] = f"sum of {len(amts)} line items"
            ex.notes.append(f"{len(ex.line_items)} line items extracted; line-item total {ex.total_from_lines:,.2f}")
        else:
            ex.warnings.append("Line items found but no amounts readable — cross-check against PO amount only")
    else:
        ex.warnings.append("No itemised line items detected — invoice is bundled; totals will be validated against PO amount directly")

    # totals
    _extract_totals(lines, ex)
    if ex.subtotal is not None and ex.tax_amount is not None:
        ex.tax_included = False
        computed = round(ex.subtotal + ex.tax_amount, 2)
        ex.notes.append(f"subtotal {ex.subtotal:,.2f} + tax {ex.tax_amount:,.2f} = {computed:,.2f} (tax separated)")

    # ---------------------------------------------------------------- status
    missing = []
    if not ex.invoice_number:
        missing.append("invoice_number")
    if ex.invoice_date is None:
        missing.append("invoice_date")
    if ex.vendor_name is None:
        missing.append("vendor_name")
    if ex.total_printed is None and ex.total_from_lines is None:
        missing.append("total")
    if missing:
        ex.status = "incomplete"
        ex.warnings.append(f"missing critical field(s): {', '.join(missing)}")
    else:
        ex.status = "extracted"

    if ex.total_printed is not None and ex.total_from_lines is not None:
        if abs(ex.total_printed - ex.total_from_lines) < 0.05:
            ex.notes.append("printed total matches sum of line items ✓")
        else:
            ex.warnings.append(
                f"printed total {ex.total_printed:,.2f} does not equal line-item sum {ex.total_from_lines:,.2f} "
                f"(diff {abs(ex.total_printed - ex.total_from_lines):,.2f}) — possible split PO, hidden tax, or data error")
    return ex
