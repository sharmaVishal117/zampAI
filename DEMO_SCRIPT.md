# 5-Minute Demo Script

Screen-record with Loom (or anything). No editing, no slides — just you, the
browser, and the process running. Keep it under 5:00.

**Before recording:** `python3 -m ap.server` (leave it running), open
http://localhost:8321, and click "Reset data" so the run history and duplicate
register start clean. Have the sample dropdown on "01-happy-path".

---

### 0:00–0:30 · What this is (intro)

"AP gets hundreds of invoices a month. Someone reads each one, finds the PO,
checks the numbers, and decides what to do — and it's the kind of work where a
tired Friday-afternoon reviewer makes expensive mistakes. I built a process
that takes an invoice PDF and produces a *reasoned* decision: pay, review,
reject, or hold — with every step visible. Let me show it running."

### 0:30–1:50 · Happy path (01-happy-path.pdf)

Run it. Watch the pipeline strip light up. Narrate the stages as they finish:
classification (digital, has text layer), extraction (text layer, 0.9
confidence, fields found — invoice number, date, PO-1101, total 1,234.56),
quality gate passes, matching (exact PO reference, amount within tolerance),
rules (no duplicates, approved vendor, no over-PO), decision **PAY**.

"The whole point is the audit trail — see this? Every number has the raw line
it came from. I made the decision explainable because that's what a buyer
actually needs."

### 1:50–2:40 · Edge case 1: duplicate (02-duplicate.pdf)

"It says PAY-worthy — but this invoice number is already in the processed
register from October 21, status *paid*. Duplicates are the single most common
AP fraud pattern and the cheapest one to catch, so this rule is deliberately
hard: **REJECT**, and it tells you exactly why. Note the status-awareness —
if that register entry had been *pending*, this would route to review as a
likely corrected re-issue instead."

### 2:40–3:30 · Edge case 2: split billing (03-split-billing.pdf)

"This invoice is $3,900 against a $5,000 PO — 22% under. A naive system reads
'under PO' and either pays blind or rejects it. I made the call that being
*under* a PO is usually split billing — vendors tranche big contracts — while
being *over* is the fraud signal. So under → **REVIEW** with the specific
question: 'is tranche 3 on its way?' over → reject. That asymmetry is the most
important judgment in this pipeline."

### 3:30–4:15 · Edge case 3: scanned invoice (05-scanned.pdf)

"This one has no machine-readable text — it's a true scan. Watch the
classification: 'scanned, no text layer.' It drops to the OCR branch, runs
tesseract, lowers confidence to 0.6, and *still* lands a PAY because the OCR'd
total matches the PO. But if OCR had failed, this would HOLD — not reject —
because unreadable isn't the same as fraudulent."

### 4:15–4:40 · Missing fields + dashboard (04-missing-fields, then Dashboard tab)

"This one has no invoice number and no date, and the PO reference is buried in
the narrative. The process flags both, downgrades itself to REVIEW, and says
*exactly* which fields it couldn't find. And here's the dashboard — every run,
every decision, persisted to SQLite, each one openable into its full audit
trail. That's the artifact you'd hand a finance team."

### 4:40–5:00 · Close

"Everything runs locally, no cloud dependency, 19 tests cover the decision
logic. Happy to walk through any of the choices."

---

**Tips for the live interview demo:**
- Rehearse: reset data → 01 → 02 → 03 → 05 (the four that show range).
- If something breaks live, narrate the failure the same way you narrate
  success — "here's what it caught and how it degraded" is a good answer.
- Keep the browser zoom at 100–110% so the audit-trail log is readable.
