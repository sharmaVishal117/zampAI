# PS-1 · Invoice Processing — From PDF to Decision

**AI Solutions Analyst case study.** A working, runnable automated process that takes a
vendor invoice PDF as input, matches it against a purchase-order register, validates it
against procurement rules, and produces a clear, *reasoned* decision — PAY, REVIEW,
REJECT, or HOLD — with every intermediate step visible in a live run view and a
dashboard of past runs.

Everything runs locally with no cloud dependency. No build step. No account.

---

## Run it

```bash
cd ps1-invoice-processor
python3 -m ap.server          # web UI + API on http://localhost:8321
```

Open <http://localhost:8321>. Pick a sample invoice (or drag in your own PDF), click
**Run pipeline**, and watch the seven stages light up with a live audit trail, then read
the decision with its full reasoning.

One-time setup, only needed if you don't already have these:

```bash
pip install pdfplumber reportlab
# tesseract + poppler-utils (OCR fallback for scanned PDFs)
#   macOS:  brew install tesseract poppler
#   ubuntu: sudo apt-get install tesseract-ocr poppler-utils
```

Python 3.10+. The web server and API are pure standard library — the only third-party
imports are `pdfplumber` (PDF text) and `reportlab` (only used to *generate* the sample
invoices, not at runtime).

---

## What it does

Seven stages, each emitting structured events that the UI renders live:

1. **Ingest & classify** — page count, file size, and a *digital vs scanned*
   classification (does a usable text layer exist?). This branches the whole pipeline.
2. **Text extraction** — read the embedded text layer; if absent or too small,
   fall back to `pdftoppm` + `tesseract` OCR; if still nothing, optionally call an
   LLM. Records *which source* won and a confidence score.
3. **Field extraction** — regex extraction of invoice number, dates, vendor, tax ID,
   PO references, line items, and totals. Every field keeps the raw line it came from
   (the "evidence") so the decision is explainable, not just assertive.
4. **Quality gate** — which critical fields are missing, whether the data came from
   OCR (lower confidence), and a sum-of-line-items vs printed-total cross-check.
5. **PO matching** — score every open PO on (explicit ref, implied ref, vendor,
   amount-within-tolerance), return the best match *and* the ranked candidates so a
   reviewer can see what else was considered.
6. **Rule validation** — duplicates, approved-vendor check, over/under-PO, split
   billing, missing critical fields. Each rule produces a typed issue with a severity.
7. **Decision & persist** — combine everything into PAY / REVIEW / REJECT / HOLD,
   write a full audit-trail record to SQLite, and book PAY/REJECT outcomes into the
   processed-invoice register (which future duplicate checks read from).

### The decision vocabulary

- **PAY** — cleared every gate; safe to queue for payment.
- **REVIEW** — a human must triage; each blocker/warning explains exactly *what* to
  check and *why* it matters.
- **REJECT** — confident the invoice is wrong (duplicate, over-PO, vendor mismatch);
  do not pay.
- **HOLD** — data too degraded to decide (unreadable scan, no total). Holding, not
  rejecting, is deliberate: no evidence ≠ evidence of fraud, and the failure mode we're
  protecting against is a tired reviewer auto-approving something they couldn't read.

---

## The four edge cases I designed (and why they're non-trivial)

The brief asks for 2–4 realistic scenarios where the process must behave differently
from the happy path. I picked four that map to the genuinely hard parts of AP:

| # | Sample file | Scenario | Why it's non-trivial | Expected |
|---|-------------|----------|----------------------|----------|
| 1 | `01-happy-path.pdf` | Clean digital invoice, explicit PO ref | Baseline everything else is measured against | **PAY** |
| 2 | `02-duplicate.pdf` | Same invoice number already in the register | Duplicate detection is the single most common AP fraud pattern and the cheapest to catch — it must *reject*, not review | **REJECT** |
| 3 | `03-split-billing.pdf` | Tranche 2 of 3 against a larger PO (invoice under PO amount) | A "under PO" amount is *not* an error by itself — vendors split big POs across months. The logic must distinguish split-billing from a typo, and route to human confirmation rather than pay or reject | **REVIEW** |
| 4 | `04-missing-fields.pdf` | No invoice number, no date, PO only in narrative | Duplicate detection and dating are weakened; the process must degrade gracefully and say *so* rather than guess | **REVIEW** |
| 5 | `05-scanned.pdf` | Image-only PDF — no machine-readable text at all | Tests the entire OCR branch: classification, tesseract fallback, lowered confidence, and the top-of-page vendor heuristic | **PAY** (via OCR path) |

Plus, deliberately, a **status-aware duplicate rule**: a register entry in
`pending` is treated as a probable *re-issue* (route to review), while a `paid` entry is
a hard duplicate (reject). The distinction matters because the same invoice number
appearing a second time is only fraudulent if the first one already went out the door.

---

## Design decisions and assumptions (call these out in the interview)

The case explicitly says "make an assumption, note it, and move on." These are the ones:

- **Tolerances are defaults, not truths.** `amount_tolerance_pct = 2.0`,
  `absolute_tolerance = $5`, `split_billing_pct = 5.0`. I picked them so the happy path
  is clean and the split case lands in the under-PO band; they live in one
  `RULES` dict so a finance team can tune them without touching code.
- **US date convention.** Numeric dates are read month-first (`10/27/2026` = Oct 27),
  with an automatic flip if the month is invalid. State this out loud — it's a real AP
  assumption.
- **Explicit PO reference outranks vendor + amount.** If an invoice names PO-1101, we
  match PO-1101 even if the amount looks like another vendor's. A mismatched
  vendor/amount on an explicitly-referenced PO is caught downstream as a *vendor
  mismatch* (reject/review), which is the correct way to surface it. Matching
  proposes; validation disposes.
- **"Under PO" is split-billing until proven otherwise.** Being below a PO amount is
  normal when a vendor bills in tranches; being *above* it (past tolerance) is the
  actual fraud/overbill signal. So under → REVIEW, over → REJECT. This asymmetry is
  the most important judgment call in the whole pipeline.
- **Scanned invoices lower confidence but don't fail.** OCR success drops the
  confidence score and flags the run, but a correct OCR'd total that matches a PO is
  still a defensible PAY. A genuinely unreadable scan → HOLD.
- **No real AP data.** Per the brief, inputs are realistic test fixtures I generated:
  a PO register (`data/po_register.json`), an approved-vendor master
  (`data/vendors.json`), and five invoice PDFs (`samples/`). The processed-invoice
  register is seeded from the PO file so the duplicate case is reproducible from a
  clean checkout.

---

## Where each piece lives

```
ap/
  server.py       stdlib HTTP server + SSE live-stream + REST API + dashboard
  pipeline.py     the 7-stage orchestrator; SQLite persistence; rules config
  textio.py       PDF text layer + OCR fallback + letterhead (font-size) reader
  extract.py      field extraction (regex + heuristics) and cross-checks
  matching.py     PO scoring, vendor equivalence, the decision engine
  llm.py          optional LLM fallback (only used if a key is configured)
  gen_samples.py  regenerates the 5 sample invoice PDFs
data/
  po_register.json   open POs + the processed-invoice seed
  vendors.json       approved-vendor master (with aliases)
  runs.db            created at runtime — history + register
samples/              the 5 test invoices + manifest.json
tests/
  test_ap.py        19 unit + end-to-end tests
  run_tests.py      zero-dependency runner: python3 tests/run_tests.py
index.html          the dashboard + live run view
```

## API (if you'd rather drive it headlessly)

```
GET  /api/samples                 # available invoices
POST /api/run   {"invoice_path"}  # -> {run_id}
GET  /api/runs/<id>/live          # SSE stream of stage events
GET  /api/runs/<id>              # full record incl. audit trail
GET  /api/runs                   # history (dashboard data)
POST /api/upload                 # multipart PDF
GET  /api/llm                    # is the AI fallback configured?
POST /api/reset                  # clear history + register
```

---

## Honest limitations (what I'd build next)

- The regex extraction is tuned to a handful of layouts. Real AP sees hundreds; I'd add
  a lightweight LLM field-extraction pass as a second opinion and *reconcile* the two
  (rule-extraction is fast and cheap, LLM is flexible; agreement is high confidence,
  disagreement routes to review). The hooks for that are already in `llm.py`.
- One invoice ↔ one PO. True many-to-many (a single invoice spanning multiple POs)
  would need a set-partitioning step I intentionally didn't build.
- Tax handling assumes embedded tax unless a subtotal+tax block is present; multi-rate
  tax (state + local) isn't modeled.
- The register is a single SQLite file. Production would live in the real AP/ERP
  system; I modeled its *interface* (a processed-invoice read + a booking write) so the
  swap is mechanical.
