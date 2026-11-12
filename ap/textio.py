"""PDF text extraction: embedded text first, OCR fallback, LLM last-resort.

The order mirrors how a real AP team would attack a pile of invoices:
1. Is there a machine-readable text layer? (pdfplumber)
2. No text layer -> scanned image? Render + tesseract OCR.
3. Both empty/low-quality -> optional LLM fallback if the user configured a key.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time

import pdfplumber

from .llm import llm_configured, llm_complete

MIN_TEXT_CHARS = 120  # below this we treat the "text layer" as garbage


def extract_pdf_text(path: str) -> tuple[str, str, int, float]:
    """Return (text, source, seconds, confidence).

    source is one of: 'text-layer' | 'ocr-tesseract' | 'ocr-llm' | 'none'
    confidence in [0, 1] -- how much we trust the field extraction that will
    be run against this text.
    """
    t0 = time.time()
    text = ""
    page_count = 1
    with pdfplumber.open(path) as pdf:
        page_count = len(pdf.pages)
        chunks = []
        for i, page in enumerate(pdf.pages):
            t = page.extract_text() or ""
            chunks.append(t)
            # keep processing: totals often sit on the last page,
            # line items on earlier ones
            if i >= 4:
                break
        text = "\n".join(c for c in chunks if c)

    if len(text.replace("\n", "").strip()) >= MIN_TEXT_CHARS:
        elapsed = time.time() - t0
        return text, "text-layer", elapsed, 0.9

    # OCR fallback ----------------------------------------------------------
    ocr_ok = False
    if shutil.which("tesseract") and shutil.which("pdftoppm"):
        try:
            with tempfile.TemporaryDirectory() as td:
                cmd = ["pdftoppm", "-png", "-r", "300", "-f", "1",
                       "-l", str(min(page_count, 4)), path, f"{td}/pg"]
                subprocess.run(cmd, check=True, capture_output=True, timeout=120)
                imgs = sorted(f for f in os.listdir(td) if f.endswith(".png"))
                if imgs:
                    chunks = []
                    for img in imgs:
                        r = subprocess.run(
                            ["tesseract", f"{td}/{img}", "stdout", "-l", "eng"],
                            capture_output=True, text=True, timeout=300)
                        chunks.append(r.stdout)
                    text = "\n".join(chunks)
                    ocr_ok = len(text.replace("\n", "").strip()) >= MIN_TEXT_CHARS
        except Exception:
            ocr_ok = False

    if ocr_ok:
        elapsed = time.time() - t0
        return text, "ocr-tesseract", elapsed, 0.6

    if llm_configured() and page_count:
        try:
            text, _ = llm_complete(
                "You are an OCR system. Transcribe ALL text in this invoice PDF "
                "verbatim, line by line, including numbers, table cells, headers "
                "and footers. Output raw text only, no commentary.",
                file_path=path)
            if len(text.replace("\n", "").strip()) >= MIN_TEXT_CHARS:
                elapsed = time.time() - t0
                return text, "ocr-llm", elapsed, 0.55
        except Exception:
            pass

    elapsed = time.time() - t0
    return text.strip(), "none", elapsed, 0.0


def letterhead_candidates(path: str) -> list[str]:
    """Lines of the *largest* text on page 1, in order of font size.

    In commercial invoices the vendor's letterhead is almost always the biggest
    block of text on the page — more reliable than raw text heuristics when
    there's no explicit 'Vendor:' label.
    """
    cands: list[tuple[float, int, int, str]] = []  # (size, top, left, text)
    try:
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[0]
            lines: dict[int, list] = {}
            for ch in page.chars:
                key = round(ch["top"] / 6) * 6  # 6pt line buckets
                lines.setdefault(key, []).append(ch)
            for top in sorted(lines):
                chs = sorted(lines[top], key=lambda c: c["x0"])
                parts: list[str] = []
                prev_x1 = None
                for c in chs:
                    if prev_x1 is not None and c["x0"] - prev_x1 > 12:
                        parts.append(" ")  # re-insert lost word space
                    parts.append(c["text"])
                    prev_x1 = c["x1"]
                text = "".join(parts).strip()
                if not text or len(text) < 3:
                    continue
                size = max(float(c.get("size", 10)) for c in chs)
                cands.append((size, top, min(c["x0"] for c in chs), text))
    except Exception:
        return []
    if not cands:
        return []
    cands.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for size, top, left, text in cands[:3]:
        if size < 14:  # letterhead must actually be big
            continue
        t2 = text.upper()
        if t2 in ("INVOICE", "A/R", "VENDOR", "REMITTANCE"):
            continue
        if len(t2) == len("INVOICE") and "INVOICE" in t2:
            continue
        out.append(text)
    return out


def get_page_count(path: str) -> int:
    try:
        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return -1
