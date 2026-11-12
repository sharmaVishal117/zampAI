"""The 7-stage AP invoice pipeline.

Stages (each one shows live in the UI with its own log lines):
  1. Ingest & classify   — is it a PDF, how many pages, text layer or scan?
  2. Text extraction     — embedded text; OCR fallback; LLM last-resort
  3. Field extraction    — invoice no / date / vendor / PO refs / totals / lines
  4. Quality gate        — what's missing, is the data trustworthy enough?
  5. PO matching         — score every open PO, pick the match, note alternatives
  6. Rule validation     — duplicates, vendor, amount vs PO, split billing
  7. Decision & persist  — PAY / REVIEW / REJECT / HOLD + full audit trail

Every stage appends structured events; the web UI tails these via SSE and
the final events are also stored in SQLite so the dashboard is real history.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
import uuid
from dataclasses import asdict
from pathlib import Path

from . import extract as ex_mod
from .matching import decide, match_po
from .textio import extract_pdf_text, get_page_count, letterhead_candidates

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "runs.db"

RULES = {
    "amount_tolerance_pct": 2.0,      # invoice within 2% of PO amount = within tolerance
    "absolute_tolerance": 5.0,        # ...or within $5 absolute, whichever is looser
    "split_billing_pct": 5.0,         # under by up to 5% AND exact-ref match = split billing
    "hold_after_seconds": 180,        # OCR slower than this -> suspicious, hold
    "duplicate_window_days": 365,
    "require_invoice_number": True,
    "max_line_item_total_delta_pct": 1.0,  # line-item sum vs printed total check
}


def _load_json(name: str):
    with open(DATA / name) as f:
        return json.load(f)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        filename TEXT,
        created_at REAL,
        status TEXT,
        matched_po TEXT,
        amount REAL,
        decision TEXT,
        match_type TEXT,
        events TEXT,
        error TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS processed_invoices (
        id INTEGER PRIMARY KEY,
        invoice_number TEXT,
        vendor_name TEXT,
        po_id TEXT,
        amount REAL,
        currency TEXT,
        issued TEXT,
        status TEXT
    )""")
    # seed the processed-invoice register from the PO dataset on first use
    if conn.execute("SELECT COUNT(*) FROM processed_invoices").fetchone()[0] == 0:
        try:
            idx = _load_json("po_register.json").get("INVOICE_INDEX", {})
            for inv_no, rec in idx.items():
                conn.execute(
                    "INSERT INTO processed_invoices (invoice_number,vendor_name,po_id,amount,currency,issued,status) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (inv_no, rec["vendor_name"], rec["po_id"], rec["amount"],
                     rec["currency"], rec["issued"], rec["status"]))
            conn.commit()
        except Exception:
            pass
    return conn


def save_run(conn, run_id, filename, events, decision, error=None):
    row = {
        "id": run_id, "filename": filename, "created_at": time.time(),
        "status": "error" if error else decision["status"],
        "matched_po": (decision.get("matched_po") or {}).get("po_id"),
        "amount": decision.get("amount_to_pay"),
        "decision": json.dumps(decision),  # full decision payload (status, reasons, issues, extracted fields...)
        "match_type": decision.get("match_type"),
        "events": json.dumps(events),
        "error": error,
    }
    conn.execute(
        "INSERT OR REPLACE INTO runs (id,filename,created_at,status,matched_po,amount,decision,match_type,events,error) "
        "VALUES (:id,:filename,:created_at,:status,:matched_po,:amount,:decision,:match_type,:events,:error)",
        row)
    # record PAY/REJECT outcomes into the processed-invoice register (the "other side"
    # that future duplicate checks read from). REVIEW/HOLD are not booked.
    # Idempotent: skip if the same invoice_number+vendor is already registered so
    # re-running the same invoice never inflates the register.
    if decision and decision["status"] in ("PAY", "REJECT") and decision.get("extracted"):
        e = decision["extracted"]
        inv_no = e.get("invoice_number")
        vendor = e.get("vendor_name")
        po_id = (decision.get("matched_po") or {}).get("po_id")
        if inv_no:
            exists = conn.execute(
                "SELECT 1 FROM processed_invoices WHERE UPPER(invoice_number)=UPPER(?) "
                "AND COALESCE(LOWER(vendor_name),'') = COALESCE(LOWER(?),'') AND COALESCE(po_id,'') = COALESCE(?,'')",
                (inv_no, vendor or "", po_id or "")).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO processed_invoices (invoice_number,vendor_name,po_id,amount,currency,issued,status) VALUES (?,?,?,?,?,?,?)",
                    (inv_no, vendor, po_id, decision.get("amount_to_pay"),
                     e.get("currency", "USD"), e.get("invoice_date"), decision["status"]))
    conn.commit()


def load_processed_ref(conn) -> list:
    rows = conn.execute("SELECT invoice_number, vendor_name, po_id, amount, currency, issued, status FROM processed_invoices").fetchall()
    return [dict(zip(("invoice_number", "vendor_name", "po_id", "amount", "currency", "issued", "status"), r)) for r in rows]


def run_invoice(pdf_path: str, label: str | None = None, conn: sqlite3.Connection | None = None,
                run_id: str | None = None, emit=None) -> dict:
    """Run the full pipeline on one invoice PDF. `emit` is called with
    (stage, event_dict) after every stage; events go into the audit trail."""
    if conn is None:
        conn = _db()
    run_id = run_id or uuid.uuid4().hex[:12]
    label = label or os.path.basename(pdf_path)
    events: list[dict] = []

    def emit_stage(stage: str, event: dict):
        event = {"stage": stage, **event}
        events.append(event)
        if emit:
            emit(stage, event)

    t_start = time.time()
    error = None
    decision: dict = {"status": "ERROR"}
    try:
        # ---- 1. ingest & classify
        ingest_started = time.time()
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"{pdf_path} not found")
        pages = get_page_count(pdf_path)
        emit_stage("ingest", {"type": "stage-start", "name": "Ingest & classify", "detail": label, "size_kb": os.path.getsize(pdf_path) // 1024, "pages": pages})

        # ---- 2. text extraction
        t = time.time()
        text, source, seconds, confidence = extract_pdf_text(pdf_path)
        emit_stage("extract", {"type": "text-source", "source": source, "seconds": round(seconds, 2),
                                "confidence": round(confidence, 2), "chars": len(text)})
        # classify note: which kind of invoice did we get?
        if source == "text-layer":
            emit_stage("ingest", {"type": "classify", "kind": "digital", "detail": "machine-readable text layer found — treat as digital invoice"})
        else:
            emit_stage("ingest", {"type": "classify", "kind": "scanned-or-image", "detail": "no usable text layer — scanned/image invoice; OCR fallback path"})

        # ---- 3. field extraction
        letterhead = letterhead_candidates(pdf_path) if source == "text-layer" else []
        ex_mod_out = ex_mod.extract_fields(text, {"source": source, "seconds": seconds,
                                                   "confidence": confidence, "letterhead": letterhead})
        ex_mod_out.pages = pages
        emit_stage("fields", {"type": "fields", "fields": _summarize_fields(ex_mod_out),
                              "notes": ex_mod_out.notes, "warnings": ex_mod_out.warnings})

        # ---- 4. quality gate
        missing = _missing_critical(ex_mod_out)
        if missing:
            emit_stage("quality", {"type": "gate", "result": "degraded", "missing": missing,
                                   "detail": f"{len(missing)} critical field(s) missing — continuing in degraded mode"})
        else:
            emit_stage("quality", {"type": "gate", "result": "pass", "detail": "all critical fields present"})
        if source in ("ocr-tesseract", "ocr-llm"):
            emit_stage("quality", {"type": "ocr-confidence", "result": "degraded", "detail": f"fields came from OCR ({source}); confidence lowered to {round(confidence,2)}"})
        tp, tl, tax = ex_mod_out.total_printed, ex_mod_out.total_from_lines, ex_mod_out.tax_amount
        if tp and tl and abs(tp - tl) > 0.02:
            # tax-separated layout: the line-item sum is the *subtotal*, so the
            # printed total legitimately exceeds it by the tax amount
            explained = tax is not None and abs((tp - tl) - tax) < 0.02
            if explained:
                emit_stage("quality", {"type": "total-ok", "result": "pass",
                                       "detail": f"line-item sum {tl} + tax {tax} = printed total {tp} — consistent"})
            else:
                emit_stage("quality", {"type": "total-mismatch", "result": "degraded",
                                       "detail": f"printed total {tp} != line-item sum {tl} — possible split PO, hidden tax, or typo"})

        # ---- 5. PO matching
        po_cfg = _load_json("po_register.json")
        vendors_cfg = _load_json("vendors.json")
        match = match_po(ex_mod_out, po_cfg, vendors_cfg["APPROVED_VENDORS"], RULES,
                         paid_ref=load_processed_ref(conn), run_id=run_id,
                         ocr_seconds=seconds, source=source)
        emit_stage("matching", {"type": "match", "matched_po": match.matched_po["po_id"] if match.matched_po else None,
                                "match_type": match.match_type, "note": match.note,
                                "candidates": [
                                    {"po_id": c["po"]["po_id"], "vendor": c["po"]["vendor_name"],
                                     "po_amount": c["po"]["amount"], "score": c["score"], "why": c["why"],
                                     "diff_pct": c["diff_pct"]} for c in match.candidates]})

        # ---- 6. rule validation
        decision = asdict(decide(ex_mod_out, match, po_cfg, vendors_cfg, RULES,
                          paid_ref=load_processed_ref(conn), ocr_seconds=seconds, source=source))
        decision["extracted"] = _full_fields(ex_mod_out)
        decision["rules_applied"] = RULES
        emit_stage("validation", {"type": "rules", "rules": RULES,
                                  "duplicates": decision["duplicates"],
                                  "split_billing": decision["split_billing"]})

        # ---- 7. decision & persist
        elapsed = time.time() - t_start
        emit_stage("decision", {"type": "decision", "status": decision["status"],
                                "reasons": decision["reasons"],
                                "issues": decision["issues"],
                                "explanation": decision["explanation"],
                                "elapsed_s": round(elapsed, 2)})

    except Exception:
        error = traceback.format_exc(limit=4)
        decision = {"status": "ERROR", "reasons": [error.splitlines()[-1]],
                    "issues": [], "po_candidates": [], "explanation": "Pipeline crashed; see error"}
        emit_stage("error", {"type": "error", "detail": error})

    save_run(conn, run_id, label, events, decision, error=error)
    decision["run_id"] = run_id
    decision["events"] = events
    decision["error"] = error
    return decision


def _missing_critical(ex) -> list:
    m = []
    if not ex.invoice_number: m.append("invoice_number")
    if not ex.vendor_name: m.append("vendor_name")
    if ex.invoice_date is None: m.append("invoice_date")
    if ex.total_printed is None and ex.total_from_lines is None: m.append("total")
    return m


def _summarize_fields(ex) -> dict:
    return {
        "text_source": ex.text_source,
        "ocr_seconds": round(ex.ocr_seconds, 2),
        "confidence": round(ex.confidence, 2),
        "pages": ex.pages,
        "invoice_number": ex.invoice_number,
        "invoice_date": ex.invoice_date.isoformat() if ex.invoice_date else None,
        "due_date": ex.due_date.isoformat() if ex.due_date else None,
        "vendor_name": ex.vendor_name,
        "vendor_tax_id": ex.vendor_tax_id,
        "po_refs": ex.po_refs,
        "currency": ex.currency,
        "line_items": len(ex.line_items),
        "total_from_lines": ex.total_from_lines,
        "total_printed": ex.total_printed,
        "subtotal": ex.subtotal,
        "tax_amount": ex.tax_amount,
        "evidence": ex.evidence,
    }


def _full_fields(ex) -> dict:
    d = _summarize_fields(ex)
    d["line_items"] = [asdict(li) for li in ex.line_items][:40]
    return d
