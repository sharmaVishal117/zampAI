#!/usr/bin/env bash
# PS-1 one-shot deploy: commit -> push to GitHub -> print the Render step.
# Run from inside the ps1-invoice-processor folder:  ./deploy.sh
set -euo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

git init -q 2>/dev/null || true
git branch -M main 2>/dev/null || true

git add ap tests data/po_register.json data/vendors.json demo-data.json \
        samples index.html ap-live-replay.html static \
        README.md DEMO_SCRIPT.md requirements.txt Dockerfile render.yaml \
        .gitignore DEPLOY.md deploy.sh

git commit -q -m "PS-1 AP invoice processor (Render deploy)" || true

# Repo: use the gh CLI if it's authenticated, else ask you to create one in the browser.
if ! git remote get-url origin >/dev/null 2>&1; then
  if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
    echo "Creating repo via gh CLI (change the name if 'ap-invoice-processor' is taken)..."
    REPO_URL=$(gh repo create ap-invoice-processor --private --clone=false 2>/dev/null || \
                gh repo create ap-invoice-processor --public --clone=false)
    git remote add origin "$REPO_URL"
    echo "  -> $REPO_URL"
  else
    echo ""
    echo "Step 1: on github.com, New repository -> name it ap-invoice-processor"
    echo "         (private is fine, don't add a README)."
    read -r -p "Now paste its URL here (https://github.com/you/ap-invoice-processor): " REPO_URL
    git remote add origin "$REPO_URL"
  fi
fi

echo "Pushing..."
git push -u origin main

echo ""
echo "Pushed. Step 2: render.com -> New -> Blueprint -> pick your repo -> Deploy."
echo "render.yaml wires the Docker service + persistent disk automatically."
echo "Once the build finishes (~2 min) your live link is https://<service-name>.onrender.com"
echo "Smoke test: open it, pick 06-dummy-metro.pdf, Run pipeline -> PAY (PO-1402, \$4,000)."
