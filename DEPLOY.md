# Deploying PS-1 to Render (live URL, 24/7)

Everything needed is already in this folder: `Dockerfile`, `requirements.txt`,
`render.yaml`, and a `.gitignore`. The app itself needs no code changes —
the server already reads `PORT` from the environment, and missing OCR tools
degrade gracefully (scanned invoices just fall to HOLD/REVIEW instead of crashing).

## Step 1 — put the code in a repo Render can pull

Render builds from a git repo. Fastest option is a public or private GitHub repo
(making it private is fine, Render asks for a token).

```bash
cd ps1-invoice-processor
git init
git add ap tests data/po_register.json data/vendors.json demo-data.json \
        samples index.html ap-live-replay.html static \
        README.md DEMO_SCRIPT.md requirements.txt Dockerfile render.yaml
git commit -m "PS-1 AP invoice processor + Render deploy files"
# create a repo on github.com, then:
git remote add origin https://github.com/<you>/ap-invoice-processor.git
git push -u origin main
```

Note the `git add` list deliberately skips `data/runs.db` and `data/uploads/*`
(those are runtime state; on Render they live on the attached disk instead).

## Step 2 — create the service on Render

1. Log in at render.com (free account is enough), then **New → Blueprint** and
   pick the `render.yaml` in the repo root. That creates the service with the
   Docker runtime **and** the persistent disk in one go.

   (Alternative: New → Web Service → point it at the repo; Runtime: Docker,
   Root Directory: root of repo, Render will find `Dockerfile`. Then in the
   service's **Disk** tab, attach a disk at mount path `/app/data`.)

2. Render builds the image (~1–2 min) and starts it. No start command is
   needed — the Dockerfile's `CMD` runs `python -m ap.server`, and Render
   injects `PORT` automatically.

## Step 3 — open the live URL

Your service page shows `https://<name>.onrender.com`. That's the live value:
a real public link to the full app (Process + Dashboard + live SSE run view).

Free-tier notes worth knowing:

- **Cold starts** — after ~15 minutes idle the instance sleeps; the first
  request wakes it (a few seconds of delay).
- **Runs history** — `data/` sits on the attached disk, so `runs.db` and
  uploaded PDFs survive restarts and rebuilds. Without the disk, history
  resets on each deploy/sleep (the app itself works fine either way).
- **OCR** — the image includes tesseract + poppler, so sample 05 (the
  scanned one) works on Render without your Mac having brew installed.
- **LLM fallback** — not configured, so unreadable documents HOLD instead of
  calling a model. That's the honest state and the UI shows it ("rules + OCR
  only"). If you later want it, set the `ANTHROPIC_API_KEY` environment
  variable on the service and redeploy — the pipeline picks it up.

## Smoke test after deploy

Open the live URL, pick `06-dummy-metro.pdf` from the sample dropdown, hit
**Run pipeline**. Expected: live stages light up, decision banner **PAY**
(matched PO-1402, $4,000), and the audit trail shows the green
`total-ok` line — "line-item sum 3703.7 + tax 296.3 = printed total 4000.0".
Then re-upload the same file: it should flip to REVIEW (duplicate/re-issue).
