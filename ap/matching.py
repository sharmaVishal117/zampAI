"""PO matching and decision engine.

The heart of the case: matching an invoice to a purchase order is rarely
exact. We score candidates on (explicit ref, vendor, amount-within-tolerance)
and then explain *why* whichever decision we reach.

Decisions:
  PAY            -> send to pay queue
  REVIEW         -> human triage; each issue explains why and what to check
  REJECT         -> confident the invoice is wrong/duplicate; don't pay
  HOLD           -> data too degraded to decide (scanned garbage, missing total)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .extract import Extracted, parse_money


@dataclass
class MatchResult:
    matched_po: dict | None = None
    match_type: str = "none"     # exact-ref | ref-implicit | fuzzy | split-bundle | weak
    candidates: list[dict] = field(default_factory=list)
    note: str = ""


def _norm_vendor(vendor: str) -> str:
    v = re.sub(r"[^A-Z0-9 ]", " ", (vendor or "").upper())
    for junk in ("LLC", "INC", "LTD", "CORP", "PLC", "GMBH", "SARL", "LLP", "CO", "COMPANY", "INVOICE"):
        v = re.sub(rf"\b{junk}\b", " ", v)
    return re.sub(r"\s+", " ", v).strip()


def _vendor_tokens(vendor_name: str) -> list[str]:
    """Normalized, comparable tokens for a vendor name (strip legal suffixes)."""
    return [t for t in _norm_vendor(vendor_name or "").split() if t]


def vendor_equivalent(v1: str, v2: str) -> bool:
    a, b = _vendor_tokens(v1), _vendor_tokens(v2)
    if not a or not b:
        return False
    return set(a) == set(b) or (len(a) >= 2 and a == b[:len(a)]) or \
           (len(b) >= 2 and b == a[:len(b)])


def _vendor_match(vendor_name: str, po_vendor: str, vendors_cfg: list) -> tuple[bool, str]:
    if not vendor_name or not po_vendor:
        return False, ""
    a = _norm_vendor(vendor_name)
    b = _norm_vendor(po_vendor)
    if not a or not b:
        return False, ""
    # exact/contained
    if a == b or a in b or b in a:
        return True, f"vendor name \"{vendor_name}\" matches PO vendor \"{po_vendor}\""
    # alias from vendor master
    for v in vendors_cfg:
        if v.get("name", "").lower() == po_vendor.lower() or po_vendor.lower() in [x.lower() for x in v.get("aliases", [])]:
            for alias in [v["name"].lower()] + [x.lower() for x in v.get("aliases", [])]:
                if alias in a.lower() or a.lower() in alias or b in a:
                    return True, f"vendor name \"{vendor_name}\" matches via vendor-master alias for \"{po_vendor}\""
    return False, ""


def _diff_pct(amt, po_amt) -> float | None:
    if amt is None or po_amt in (None, 0):
        return None
    return abs(amt - po_amt) / po_amt * 100.0


def match_po(ex: Extracted, po_cfg: dict, vendors_cfg: list, rules: dict,
             paid_ref: list, run_id: str, ocr_seconds: float = 0.0,
             source: str = "text-layer") -> MatchResult:
    """Score every open PO against the extracted invoice; return the match +
    ranked candidates for the audit trail."""
    tol_pct = rules.get("amount_tolerance_pct", 2.0)
    tol_abs = rules.get("absolute_tolerance", 5.0)
    split_pct = rules.get("split_billing_pct", 5.0)

    open_pos = [p for p in po_cfg["POS"] if p["status"] == "open"]
    vendor_raw = ex.vendor_name

    candidates = []
    for po in open_pos:
        score = 0
        why = []
        _, vmatch = _vendor_match(vendor_raw or "", po["vendor_name"], vendors_cfg)
        ref_exact = any(r.upper() == po["po_id"].upper() for r in ex.po_refs) if ex.po_refs else False
        ref_guess = (not ref_exact and ex.po_refs and any(
            po["po_id"].upper() in r.upper() or r.upper() in po["po_id"].upper()
            for r in ex.po_refs if len(r) >= 3))
        if ref_exact:
            score += 100
            why.append(f"explicit PO reference \"{po['po_id']}\" on the invoice")
        elif ref_guess and ex.po_refs:
            score += 40
            why.append(f"implied PO reference ({', '.join(ex.po_refs)}) points near \"{po['po_id']}\"")
        if vmatch:
            score += 30
            why.append(vmatch)
        # amount — the printed total is what the vendor asks us to pay; the
        # line-item sum is a cross-check (tax separated out explains most diffs)
        amt = ex.total_printed if ex.total_printed is not None else ex.total_from_lines
        diff = _diff_pct(amt, po["amount"])
        if amt is not None and diff is not None:
            exact_ok = (amt + 0.001 >= po["amount"] - 0.001)
            under_ok = po["amount"] - amt <= max(tol_abs, po["amount"] * 0.01)
            within = diff <= tol_pct
            if exact_ok and within:
                score += 25
                why.append(f"invoice total {amt:,.2f} within {tol_pct}% of PO {po['amount']:,.2f}")
            elif under_ok and not exact_ok:
                # split-billing: running invoice total slightly under PO
                score += 15
                why.append(f"total {amt:,.2f} is {diff:.1f}% under PO amount — consistent with split billing of a larger PO")
            else:
                score += 0
        candidates.append({
            "po": po, "score": score, "why": why, "ref_exact": ref_exact,
            "diff_pct": diff, "amount": amt,
        })

    candidates.sort(key=lambda c: -c["score"])
    top = candidates[0] if candidates else None
    res = MatchResult(candidates=candidates[:5])

    if not top or top["score"] == 0:
        res.match_type = "none"
        res.note = "no open PO scored above zero — vendor not found in any open PO"
        return res

    po = top["po"]
    if top["ref_exact"]:
        res.matched_po = po
        res.match_type = "exact-ref"
        res.note = f"PO {po['po_id']} matched by explicit reference on the invoice"
    elif top["score"] >= 60:
        res.matched_po = po
        res.match_type = "fuzzy"
        res.note = f"PO {po['po_id']} matched on vendor + amount (no explicit PO ref on invoice)"
        res.note += "; recommend confirming"
    elif top["score"] >= 30:
        res.matched_po = po
        res.match_type = "weak"
        res.note = f"weak match to PO {po['po_id']} (score {top['score']}) — vendor or amount alone; needs human confirmation"
    else:
        res.match_type = "none"
        res.note = "best candidate below threshold"
    return res


# ------------------------------------------------------------------ decision
@dataclass
class Issue:
    code: str
    severity: str   # "blocker" | "warning"
    message: str


@dataclass
class Decision:
    status: str = "REVIEW"           # PAY | REVIEW | REJECT | HOLD
    reasons: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    matched_po: dict | None = None
    match_type: str = "none"
    po_candidates: list[dict] = field(default_factory=list)
    amount_to_pay: float | None = None
    split_billing: bool = False
    duplicates: list[dict] = field(default_factory=list)
    explanation: str = ""

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["issues"] = [i.__dict__ for i in self.issues]
        if d["matched_po"]:
            d["matched_po"] = {k: d["matched_po"][k] for k in
                               ("po_id", "vendor_name", "amount", "currency", "status") if k in d["matched_po"]}
        return d


def decide(ex: Extracted, match: MatchResult, po_cfg: dict, vendors_cfg: dict,
           rules: dict, paid_ref: list, ocr_seconds: float = 0.0,
           source: str = "text-layer") -> Decision:
    tol_pct = rules.get("amount_tolerance_pct", 2.0)
    tol_abs = rules.get("absolute_tolerance", 5.0)
    split_pct = rules.get("split_billing_pct", 5.0)
    hold_seconds = rules.get("hold_after_seconds", 180.0)

    d = Decision()
    po = match.matched_po
    d.matched_po = po
    d.match_type = match.match_type
    d.po_candidates = match.candidates

    # pay the amount requested (printed total); the line-item sum is used as a
    # cross-check and to explain tax-inclusive layouts
    amt = ex.total_printed if ex.total_printed is not None else ex.total_from_lines

    # ---------- duplicate check (status-aware, alias-aware vendor compare) ----------
    if ex.invoice_number:
        iv = (ex.invoice_number or "").upper()
        def _same_vendor(reg_vendor: str) -> bool:
            if not ex.vendor_name:
                return True  # unknown vendor on the invoice: number match alone is enough signal
            return vendor_equivalent(reg_vendor, ex.vendor_name)
        dup = [p for p in paid_ref if (p.get("invoice_number") or "").upper() == iv
               and _same_vendor(p.get("vendor_name") or "")]
        d.duplicates = dup
        hard_dup = [p for p in dup if p.get("status") in ("paid", "approved", "completed")]
        if hard_dup:
            d.status = "REJECT"
            d.reasons.append(f"DUPLICATE — invoice {ex.invoice_number} was already processed on "
                             f"{hard_dup[0]['issued']} against {hard_dup[0]['po_id']} ({hard_dup[0]['status']})")
            d.issues.append(Issue("DUPLICATE_INVOICE", "blocker", d.reasons[-1]))
            d.explanation = ("Refused to pay: the same invoice number from the same vendor already exists "
                             "in the processed-invoice register in a paid/approved state. A duplicate is the "
                             "single most common AP fraud pattern and the cheapest one to catch — so this rule "
                             "is deliberately hard: it rejects outright instead of routing to review.")
            return d
        if dup:
            d.status = "REVIEW"
            d.reasons.append(f"POSSIBLE RE-ISSUANCE — invoice {ex.invoice_number} exists in the register as "
                             f"'{dup[0]['status']}' ({dup[0]['issued']}); likely a corrected/re-issued invoice")
            d.issues.append(Issue("REISSUE_SUSPECT", "blocker",
                                   d.reasons[-1] + " — payment blocked until the re-issue is confirmed"))

    # ---------- data quality gate ----------
    if ex.status == "failed":
        d.status = "HOLD"
        d.reasons.append("UNREADABLE — no usable text layer and OCR could not recover enough content")
        d.issues.append(Issue("UNREADABLE", "blocker", "Text layer empty/low-quality and OCR produced insufficient text"))
        d.issues.append(Issue("NO_TOTAL", "blocker", "No invoice total extractable — cannot verify any amount"))
        d.explanation = ("Held, not rejected: we could not read the document, so there is no evidence for or "
                         "against it. Holding forces a human to look at the original; auto-rejecting would be "
                         "the failure mode of a tired Friday-afternoon reviewer.")
        return d

    if amt is None:
        d.status = "HOLD"
        d.reasons.append("NO TOTAL — the invoice carries no readable grand total and no itemised lines")
        d.issues.append(Issue("NO_TOTAL", "blocker", "Total amount could not be determined from the document"))
        d.explanation = "Without a total, no amount check is possible — holding for manual verification."
        return d

    if ocr_seconds > hold_seconds and source != "text-layer":
        d.status = "HOLD"
        d.reasons.append(f"SUSPICIOUS — document took {ocr_seconds:.0f}s to process (OCR fallback path)")
        d.issues.append(Issue("SLOW_PROCESS", "warning", f"OCR fallback needed {ocr_seconds:.0f}s; unusual for a normal invoice"))
        d.explanation = "Slow/unusual processing is flagged as a suspicious signal, not proof — held for review."

    # ---------- vendor check ----------
    vendor_ok, vreason = _vendor_match(ex.vendor_name or "", po["vendor_name"] if po else "", vendors_cfg["APPROVED_VENDORS"])
    if ex.vendor_name is None:
        # can't read who sent the invoice — don't guess, don't reject
        d.status = "REVIEW"
        d.reasons.append("VENDOR UNIDENTIFIED — no vendor name readable on the document; the amount matches a PO "
                         f"but the remitter must be confirmed manually before payment")
        d.issues.append(Issue("VENDOR_UNIDENTIFIED", "blocker", "Vendor name not extractable — confirm the remitter manually"))
    elif po and not vendor_ok:
        d.status = "REJECT"
        d.reasons.append(f"VENDOR MISMATCH — invoice from \"{ex.vendor_name}\" but matched PO is for \"{po['vendor_name']}\"")
        d.issues.append(Issue("VENDOR_MISMATCH", "blocker", d.reasons[-1]))
        return d
    # vendor must resolve to an approved-vendor master entry (alias-aware)
    def resolve_vendor(name: str) -> str | None:
        for v in vendors_cfg["APPROVED_VENDORS"]:
            aliases = [v["name"]] + list(v.get("aliases", []))
            if any(vendor_equivalent(name, a) for a in aliases):
                return v["name"]
            # lenient substring fallback for messy OCR
            for a in aliases:
                na, nb = name.lower(), a.lower()
                if len(na) > 5 and len(nb) > 5 and (na in nb or nb in na):
                    return v["name"]
        return None

    resolved = resolve_vendor(ex.vendor_name) if ex.vendor_name else None
    if ex.vendor_name and resolved is None:
        d.issues.append(Issue("UNAPPROVED_VENDOR", "blocker",
                              f"vendor \"{ex.vendor_name}\" not in the approved-vendor master"))
    elif resolved and po and not vendor_equivalent(resolved, po["vendor_name"]) and not vendor_equivalent(ex.vendor_name or "", po["vendor_name"]):
        d.issues.append(Issue("VENDOR_VS_PO", "blocker",
                              f"vendor resolves to approved master entry \"{resolved}\" which does not match the matched PO's vendor \"{po['vendor_name']}\""))

    # ---------- amount check vs PO ----------
    if po:
        diff = _diff_pct(amt, po["amount"])
        split_band = max(tol_abs, po["amount"] * split_pct / 100.0)

        if amt + 0.001 >= po["amount"] - 0.001:
            # invoice total covers the PO amount
            if diff is not None and diff <= tol_pct:
                d.status = "PAY"
                d.amount_to_pay = amt
                d.reasons.append(f"AMOUNT OK — invoice {amt:,.2f} vs PO {po['amount']:,.2f} (diff {diff:.2f}%, tolerance ±{tol_pct}%)")
            else:
                d.status = "REJECT"
                d.amount_to_pay = None
                d.reasons.append(f"OVER PO — invoice {amt:,.2f} exceeds PO {po['amount']:,.2f} by {diff:.2f}% "
                                 f"(tolerance ±{tol_pct}%, absolute cap ±{tol_abs:,.2f})")
                d.issues.append(Issue("OVER_PO", "blocker", d.reasons[-1]))
        else:
            # invoice total is under the PO amount
            short = po["amount"] - amt
            if short <= split_band:
                d.status = "REVIEW"
                d.split_billing = True
                d.amount_to_pay = amt
                d.reasons.append(f"SPLIT BILLING — {amt:,.2f} is within the split band "
                                 f"(≤{split_band:,.2f}) under PO {po['amount']:,.2f}; remainder {short:,.2f} "
                                 f"expected on a later invoice")
                d.issues.append(Issue("SPLIT_BILLING", "warning", d.reasons[-1]))
            else:
                d.status = "REVIEW"
                d.amount_to_pay = amt
                d.reasons.append(f"UNDER PO — invoice {amt:,.2f} is {short:,.2f} ({diff:.1f}%) below PO "
                                 f"{po['amount']:,.2f}; could be a partial/split invoice or an amount error — "
                                 f"confirm the billing schedule before payment")
                d.issues.append(Issue("UNDER_PO", "warning", d.reasons[-1]))
    else:
        d.status = "REVIEW"
        d.reasons.append("NO PO MATCH — no open purchase order matched this vendor/amount; possible unapproved spend")
        d.issues.append(Issue("NO_PO", "blocker", d.reasons[-1]))

    # ---------- missing critical fields ----------
    if not ex.invoice_number:
        if d.status == "PAY":
            d.status = "REVIEW"
        d.issues.append(Issue("MISSING_INVOICE_NO", "blocker" if not ex.invoice_number else "warning",
                              "No invoice number — duplicate detection is weakened; request a corrected invoice or log manually"))
        d.reasons.append("MISSING FIELDS — no invoice number on the document")
    if ex.invoice_date is None:
        d.reasons.append("MISSING FIELDS — no invoice date")
        d.issues.append(Issue("MISSING_DATE", "warning", "No invoice date found"))

    # ---------- final state ----------
    if d.status == "PAY":
        # PAY survives only if no blocker was raised anywhere along the way
        if any(i.severity == "blocker" for i in d.issues):
            d.status = "REVIEW"
            d.reasons.append("Payment blocked: one or more blocker-level issues found during validation")
    # REVIEW / REJECT / HOLD stand as computed.

    # explanation text
    if d.status == "PAY":
        bits = [f"Invoice {ex.invoice_number or '(no number)'} from {ex.vendor_name}, {amt:,.2f} {ex.currency}."]
        bits.append(f"Matched PO {po['po_id']} ({match.match_type}).")
        bits.append("Amount within tolerance, vendor approved, no duplicates, no missing critical fields.")
        d.explanation = " ".join(bits) + " Safe to queue for payment."
    elif d.status == "REVIEW":
        d.explanation = ("Requires human triage: " + " | ".join(i.message for i in d.issues if i.severity in ("blocker", "warning")))
    elif d.status == "HOLD":
        d.explanation = "Document quality too low to decide automatically — manual review of the original required."
    return d
