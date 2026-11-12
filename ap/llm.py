"""Optional LLM fallback.

If the user has an Anthropic API key (ANTHROPIC_API_KEY env var or
~/.config/ap/llm.json), an unreadable invoice is sent to the model for
transcription / field extraction instead of being hard-failed.

Without a key, the pipeline still runs end-to-end — the LLM stage simply
reports "not configured" in the audit trail, which is the honest state.
"""
from __future__ import annotations

import json
import os
import pathlib
import time
import urllib.request


def _cfg() -> dict:
    path = pathlib.Path("~/.config/ap/llm.json").expanduser()
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except Exception:
            data = {}
    key = os.environ.get("ANTHROPIC_API_KEY", data.get("api_key", ""))
    model = os.environ.get("LLM_MODEL", data.get("model", "claude-3-5-sonnet-latest"))
    return {"key": key, "model": model}


def llm_configured() -> bool:
    return bool(_cfg()["key"])


def llm_complete(prompt: str, file_path: str | None = None, timeout: int = 120) -> tuple[str, float]:
    """Call Anthropic messages API. Returns (text, seconds). Raises on error."""
    c = _cfg()
    if not c["key"]:
        raise RuntimeError("LLM not configured — set ANTHROPIC_API_KEY or ~/.config/ap/llm.json")
    t0 = time.time()
    messages = [{"role": "user", "content": prompt}]
    if file_path:
        # Anthropic supports PDF content blocks (base64) — keeps it a single call.
        import base64
        with open(file_path, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        messages[0]["content"].append({
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
        })
    body = json.dumps({
        "model": c["model"], "max_tokens": 8000,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": c["key"],
            "anthropic-version": "2023-06-01",
        })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return text, time.time() - t0
