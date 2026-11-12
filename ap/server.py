"""Stdlib web server for the AP invoice processor.

Serves:
  /                     index.html (dashboard + live run views)
  /static/*             css/js
  /api/samples          available sample invoices
  /api/runs             run history (dashboard)
  /api/runs/<id>        full record incl. audit-trail events
  /api/runs/<id>/live   SSE stream of stage events while a run is in flight
  /api/upload           multipart PDF upload (or drag-and-drop from a folder)
  /api/run              start a run {invoice_path} -> {run_id}
  /api/llm              LLM fallback config status
  /api/reset            clear run history + processed-invoice register

No dependencies beyond the Python standard library (plus pdfplumber/
reportlab/tesseract used inside the pipeline itself).
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue, Empty

from . import pipeline
from .llm import llm_configured

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(exist_ok=True)

STAGES = ["Ingest & classify", "Text extraction", "Field extraction",
          "Quality gate", "PO matching", "Rule validation", "Decision & persist"]

_LIVE: dict[str, dict] = {}
_LIVE_LOCK = threading.Lock()


def start_run(invoice_path: str, label: str | None = None) -> str:
    run_id = uuid.uuid4().hex[:10]
    q: Queue = Queue()
    entry = {"queue": q, "done": False, "events": []}
    with _LIVE_LOCK:
        _LIVE[run_id] = entry

    def emit(stage, event):
        entry["events"].append(event)
        q.put(event)

    def worker():
        try:
            pipeline.run_invoice(invoice_path, label=label, run_id=run_id, emit=emit)
        finally:
            entry["done"] = True
            q.put(None)  # sentinel

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return run_id


def sse_stream(run_id: str):
    """Yield SSE frames for a run: live events, then persisted events if the
    run finished before we subscribed."""
    with _LIVE_LOCK:
        entry = _LIVE.get(run_id)
    # already finished -> serve the persisted audit trail instead of a stale queue
    if entry is None or entry.get("done"):
        conn = pipeline._db()
        row = conn.execute("SELECT events FROM runs WHERE id=?", (run_id,)).fetchone()
        conn.close()
        events = json.loads(row[0]) if row else []
        for e in events:
            yield f"data: {json.dumps(e)}\n\n".encode()
        yield b"data: {\"type\":\"closed\"}\n\n"
        return
    while True:
        try:
            e = entry["queue"].get(timeout=15)
            if e is None:
                yield f"data: {json.dumps({'type': 'closed', 'status': 'done'})}\n\n".encode()
                break
            yield f"data: {json.dumps(e)}\n\n".encode()
        except Empty:
            yield b": keepalive\n\n"


# ---------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "APProcessor/1.0"

    def log_message(self, *a):  # quiet
        pass

    # -- helpers
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str).encode())

    def _file(self, path: Path, code=200):
        if not path.exists():
            self._json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self._send(code, path.read_bytes(), ctype)

    def _db(self):
        conn = pipeline._db()
        yield conn
        conn.close()

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._file(ROOT / "index.html")
        if p.startswith("/static/"):
            return self._file(STATIC / p[len("/static/"):])
        if p == "/api/samples":
            manifest = []
            mf = pipeline.ROOT / "samples" / "manifest.json"
            if mf.exists():
                manifest = json.loads(mf.read_text())
                for m in manifest:
                    m["path"] = str((pipeline.ROOT / "samples" / m["file"]).relative_to(ROOT))
            up = [f.name for f in sorted(UPLOADS.glob("*.pdf"))]
            manifest += [{"file": f, "path": f"data/uploads/{f}",
                         "description": f"uploaded ({f})", "expect": ""} for f in up]
            return self._json(manifest)
        if p == "/api/runs":
            conn = pipeline._db()
            rows = conn.execute(
                "SELECT id, filename, created_at, status, matched_po, amount, decision, match_type, error "
                "FROM runs ORDER BY created_at DESC LIMIT 200").fetchall()
            conn.close()
            out = []
            for r in rows:
                d = dict(zip(("id", "filename", "created_at", "status", "matched_po",
                              "amount", "decision", "match_type", "error"), r))
                try:
                    full = json.loads(d["decision"]) if isinstance(d["decision"], str) else {}
                except Exception:
                    full = {}
                d["decision"] = full.get("status", d.get("status"))
                out.append(d)
            return self._json(out)
        m = re.fullmatch(r"/api/runs/([a-f0-9]+)/live", p)
        if m:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            for frame in sse_stream(m.group(1)):
                try:
                    self.wfile.write(frame)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            return
        m = re.fullmatch(r"/api/runs/([a-f0-9]+)", p)
        if m:
            conn = pipeline._db()
            row = conn.execute("SELECT * FROM runs WHERE id=?", (m.group(1),)).fetchone()
            conn.close()
            if not row:
                return self._json({"error": "run not found"}, 404)
            d = dict(zip(("id", "filename", "created_at", "status", "matched_po",
                          "amount", "decision", "match_type", "events", "error"), row))
            d["events"] = json.loads(d["events"]) if d["events"] else []
            try:
                d["decision"] = json.loads(d["decision"]) if isinstance(d["decision"], str) else d.get("decision")
            except Exception:
                pass
            return self._json(d)
        if p == "/api/llm":
            return self._json({"configured": llm_configured()})
        if p == "/api/rules":
            return self._json(pipeline.RULES)
        self._json({"error": "no such endpoint"}, 404)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/run":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            ip = body.get("invoice_path", "")
            # resolve relative paths against the project root, not the server cwd
            cand = Path(ip)
            ip = str(cand) if cand.is_absolute() else str(ROOT / cand)
            if not ip or not os.path.exists(ip):
                return self._json({"error": f"file not found: {ip}"}, 400)
            label = body.get("label") or os.path.basename(ip)
            rid = start_run(ip, label)
            return self._json({"run_id": rid, "stages": STAGES})
        if p == "/api/upload":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            ct = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ct:
                return self._json({"error": "expected multipart/form-data"}, 400)
            fname, payload = _parse_multipart(raw, ct)
            if not fname:
                return self._json({"error": "no file field found"}, 400)
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
            out = UPLOADS / f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
            out.write_bytes(payload)
            return self._json({"path": str(out.relative_to(ROOT)), "bytes": len(payload)})
        if p == "/api/reset":
            conn = pipeline._db()
            conn.execute("DELETE FROM runs")
            conn.execute("DELETE FROM processed_invoices")
            conn.commit()
            conn.close()
            return self._json({"ok": True,
                               "note": "run history and processed-invoice register cleared — sample duplicates will no longer trigger"})
        self._json({"error": "no such endpoint"}, 404)


def _parse_multipart(raw: bytes, content_type: str) -> tuple[str, bytes]:
    m = re.search(r'name="([^"]+)"', content_type)
    boundary = m.group(1).encode() if m else b"----"
    parts = raw.split(b"--" + boundary)
    for part in parts:
        if b"filename=" not in part:
            continue
        head, _, body = part.partition(b"\r\n\r\n")
        fm = re.search(rb'filename="([^"]+)"', head)
        if not fm:
            continue
        body = body.rstrip(b"\r\n-")
        return fm.group(1).decode(errors="replace"), body
    return "", b""


def main():
    port = int(os.environ.get("PORT", 8321))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"AP Invoice Processor — http://localhost:{port}")
    print(f"  samples: {list((ROOT / 'samples').glob('*.pdf'))}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
