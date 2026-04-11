#!/usr/bin/env python3
"""
server.py — Web backend for the auditit session viewer.

Serves:
  GET /              → Web UI (index.html)
  GET /api/sessions  → List all sessions (grouped by date)
  GET /api/sessions/<date>/<session-id>/events → All events (JSONL or gz)
  GET /api/sessions/<date>/<session-id>/stream → SSE stream (live tail)
  GET /api/sessions/<date>/<session-id>/meta   → metadata + summary

Cost is computed at serve time from summary.json's raw `usage` dict and
`model` name, against the PRICING table below. Keep docs/claude-pricing.md
in sync when pricing changes.
"""

import gzip
import json
import os
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import unquote

AUDIT_DIR = Path.home() / ".claude-audit"


# ── Pricing (per-million-token, USD, standard tier) ──────────────────
#
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# Snapshot: 2026-04-11, mirrored in docs/claude-pricing.md.
#
# Keys are lowercased and matched by substring against the transcript's
# model field (e.g. "claude-haiku-4-5-20251001" matches "claude-haiku-4-5").

PRICING: dict[str, dict[str, float]] = {
    # Opus
    "claude-opus-4-6":   {"in": 5.00,  "out": 25.00, "cw5m": 6.25,  "cw1h": 10.00, "cr": 0.50},
    "claude-opus-4-5":   {"in": 5.00,  "out": 25.00, "cw5m": 6.25,  "cw1h": 10.00, "cr": 0.50},
    "claude-opus-4-1":   {"in": 15.00, "out": 75.00, "cw5m": 18.75, "cw1h": 30.00, "cr": 1.50},
    "claude-opus-4":     {"in": 15.00, "out": 75.00, "cw5m": 18.75, "cw1h": 30.00, "cr": 1.50},
    # Sonnet
    "claude-sonnet-4-6": {"in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    "claude-sonnet-4-5": {"in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    "claude-sonnet-4":   {"in": 3.00,  "out": 15.00, "cw5m": 3.75,  "cw1h": 6.00,  "cr": 0.30},
    # Haiku
    "claude-haiku-4-5":  {"in": 1.00,  "out": 5.00,  "cw5m": 1.25,  "cw1h": 2.00,  "cr": 0.10},
    "claude-haiku-3-5":  {"in": 0.80,  "out": 4.00,  "cw5m": 1.00,  "cw1h": 1.60,  "cr": 0.08},
    "claude-haiku-3":    {"in": 0.25,  "out": 1.25,  "cw5m": 0.30,  "cw1h": 0.50,  "cr": 0.03},
}


def _match_pricing(model: str) -> dict | None:
    if not model:
        return None
    m = model.lower()
    if m in PRICING:
        return PRICING[m]
    # Prefer longest prefix match so "claude-opus-4-6" wins over "claude-opus-4".
    best_key: str | None = None
    for key in PRICING:
        if key in m and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return PRICING[best_key] if best_key else None


def compute_cost(model: str, usage: dict) -> float:
    """Return the dollar cost of a session given its model and cleaned usage.

    Expects the shape hook.sh writes to summary.json:
      {input_tokens, output_tokens, cache_read_input_tokens,
       cache_creation_input_tokens, cache_creation_5m_tokens,
       cache_creation_1h_tokens}
    Falls back to attributing all `cache_creation_input_tokens` to 5m cache
    writes when the 5m/1h split is absent (older sessions).
    Unknown model → 0.0.
    """
    if not isinstance(usage, dict):
        return 0.0
    price = _match_pricing(model)
    if not price:
        return 0.0
    inp  = usage.get("input_tokens", 0) or 0
    out  = usage.get("output_tokens", 0) or 0
    cr   = usage.get("cache_read_input_tokens", 0) or 0
    cw5m = usage.get("cache_creation_5m_tokens", 0) or 0
    cw1h = usage.get("cache_creation_1h_tokens", 0) or 0
    # Old sessions stored only the total; attribute it to 5m as a conservative guess.
    cw_total = usage.get("cache_creation_input_tokens", 0) or 0
    if cw_total and not (cw5m or cw1h):
        cw5m = cw_total
    M = 1_000_000
    cost = (
        inp  * price["in"]   / M +
        out  * price["out"]  / M +
        cr   * price["cr"]   / M +
        cw5m * price["cw5m"] / M +
        cw1h * price["cw1h"] / M
    )
    return round(cost, 6)


def list_sessions() -> dict:
    """Return {date: [{id, prompt, model, cwd, count, cost, turns, duration_ms, status, started_at}]}"""
    result = {}
    if not AUDIT_DIR.exists():
        return result

    for date_dir in sorted(AUDIT_DIR.iterdir(), reverse=True):
        if not date_dir.is_dir() or not date_dir.name.startswith("20"):
            continue
        date_sessions = []
        for session_dir in sorted(date_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            meta = _load_meta(session_dir, sid)
            summary = _load_summary(session_dir)
            count = _count_events(session_dir)

            status = "completed" if summary else "active"
            model = summary.get("model", "") or meta.get("model", "")
            cost = compute_cost(model, summary.get("usage", {}))
            date_sessions.append({
                "id": sid,
                "date": date_dir.name,
                "prompt": meta.get("prompt", ""),
                "model": model,
                "cwd": meta.get("cwd", ""),
                "count": count,
                "turns": summary.get("num_turns", 0),
                "cost": cost,
                "duration_ms": summary.get("duration_ms", 0),
                "status": status,
                "started_at": meta.get("started_at", ""),
            })
        if date_sessions:
            result[date_dir.name] = date_sessions
    return result


def _load_meta(session_dir: Path, sid: str) -> dict:
    meta_path = session_dir / "metadata.json"
    if meta_path.exists():
        with open(meta_path) as f:
            return json.load(f)
    # Fallback: extract from first event
    return _extract_meta_from_events(session_dir, sid)


def _extract_meta_from_events(session_dir: Path, sid: str) -> dict:
    meta = {"prompt": "", "model": "", "cwd": ""}
    jsonl = session_dir / "audit.jsonl"
    gz = session_dir / "audit.jsonl.gz"
    path = jsonl if jsonl.exists() else gz

    if not path.exists():
        return meta

    opener = gzip.open if str(path).endswith(".gz") else open
    mode = "rt" if str(path).endswith(".gz") else "r"
    try:
        with opener(path, mode, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                    d = obj.get("data", {})
                    if obj.get("event") == "SessionStart":
                        meta["model"] = d.get("model", "")
                        meta["cwd"] = d.get("cwd", "")
                        meta["started_at"] = obj.get("ts", "")
                    elif obj.get("event") == "UserPromptSubmit":
                        prompt = d.get("prompt", "")[:200]
                        if prompt and not meta["prompt"]:
                            meta["prompt"] = prompt
                    if meta["model"] and meta["prompt"]:
                        break
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except Exception:
        pass

    # Persist metadata so we don't re-parse next time
    if meta.get("model") or meta.get("prompt"):
        try:
            with open(session_dir / "metadata.json", "w") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    return meta


def _load_summary(session_dir: Path) -> dict:
    summary_path = session_dir / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            return json.load(f)
    return {}


def _count_events(session_dir: Path) -> int:
    jsonl = session_dir / "audit.jsonl"
    gz = session_dir / "audit.jsonl.gz"
    path = jsonl if jsonl.exists() else gz
    if not path.exists():
        return 0
    count = 0
    opener = gzip.open if str(path).endswith(".gz") else open
    mode = "rt" if str(path).endswith(".gz") else "r"
    try:
        with opener(path, mode, encoding="utf-8", errors="replace") as f:
            for _ in f:
                count += 1
    except Exception:
        pass
    return count


def read_events(session_dir: Path) -> list:
    jsonl = session_dir / "audit.jsonl"
    gz = session_dir / "audit.jsonl.gz"
    path = jsonl if jsonl.exists() else gz
    if not path.exists():
        return []

    events = []
    opener = gzip.open if str(path).endswith(".gz") else open
    mode = "rt" if str(path).endswith(".gz") else "r"
    try:
        with opener(path, mode, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
    except Exception:
        pass
    return events


def resolve_session(date: str, session_id: str) -> Path | None:
    """Return the session dir if it exists AND is contained within AUDIT_DIR.

    The containment check defends against path traversal via date/session_id
    components like "..", "/", or absolute paths injected through the URL.
    """
    candidate = (AUDIT_DIR / date / session_id).resolve()
    try:
        candidate.relative_to(AUDIT_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        return candidate
    return None


class AuditHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.web_dir = Path(__file__).parent / "web"
        super().__init__(*args, directory=str(self.web_dir), **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/api/sessions":
            self._json(list_sessions())
        elif path.startswith("/api/sessions/") and path.endswith("/events"):
            parts = path.split("/")
            # /api/sessions/<date>/<session-id>/events → 6 parts
            if len(parts) == 6:
                self._serve_events(parts[3], parts[4])
        elif path.startswith("/api/sessions/") and path.endswith("/stream"):
            parts = path.split("/")
            if len(parts) == 6:
                self._stream_events(parts[3], parts[4])
        elif path.startswith("/api/sessions/") and path.endswith("/meta"):
            parts = path.split("/")
            if len(parts) == 6:
                self._serve_meta(parts[3], parts[4])
        else:
            # Serve static files (web/index.html)
            super().do_GET()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self, date: str, session_id: str):
        session_dir = resolve_session(date, unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return
        events = read_events(session_dir)
        self._json(events)

    def _stream_events(self, date: str, session_id: str):
        session_dir = resolve_session(date, unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return

        jsonl = session_dir / "audit.jsonl"
        gz = session_dir / "audit.jsonl.gz"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # If already gzipped, send all and close
        if gz.exists() and not jsonl.exists():
            events = read_events(session_dir)
            for event in events:
                self._send_sse("event", json.dumps(event, ensure_ascii=False))
            self._send_sse("event", "__DONE__")
            return

        # Live tail of jsonl: hold the handle open and readline() in a loop.
        # readline() returns "" when EOF is reached or when the current line
        # has no trailing newline yet — in the latter case we buffer the
        # partial fragment and retry next tick so no line is ever lost.
        fh = None
        partial = ""
        heartbeat_cycles = 0
        try:
            while True:
                if gz.exists() and not jsonl.exists():
                    if fh is not None:
                        try: fh.close()
                        except Exception: pass
                        fh = None
                    # Session was compressed mid-stream — replay the full gz.
                    self._send_sse("event", "__GZ__")
                    for event in read_events(session_dir):
                        self._send_sse("event", json.dumps(event, ensure_ascii=False))
                    self._send_sse("event", "__DONE__")
                    break

                if fh is None and jsonl.exists():
                    try:
                        fh = open(jsonl, "r", encoding="utf-8", errors="replace")
                    except OSError:
                        fh = None

                sent_any = False
                if fh is not None:
                    while True:
                        chunk = fh.readline()
                        if not chunk:
                            break
                        partial += chunk
                        if not partial.endswith("\n"):
                            # Incomplete line — hook is mid-write. Retry later.
                            break
                        line = partial.strip()
                        partial = ""
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._send_sse("event", json.dumps(event, ensure_ascii=False))
                        sent_any = True

                if sent_any:
                    heartbeat_cycles = 0
                else:
                    heartbeat_cycles += 1
                    # Send an SSE comment heartbeat every ~15s of idle so the
                    # client socket stays alive and we detect client disconnect
                    # via the wfile.write failure below.
                    if heartbeat_cycles >= 30:
                        heartbeat_cycles = 0
                        try:
                            self.wfile.write(b": keep-alive\n\n")
                        except Exception:
                            break

                time.sleep(0.5)
                try:
                    self.wfile.flush()
                except Exception:
                    break
        finally:
            if fh is not None:
                try: fh.close()
                except Exception: pass

    def _serve_meta(self, date: str, session_id: str):
        session_dir = resolve_session(date, unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return
        meta = _load_meta(session_dir, session_id)
        summary = _load_summary(session_dir)
        # Compute cost on the fly so pricing-table updates propagate to
        # historical sessions without rewriting summary.json.
        if summary:
            summary = dict(summary)
            summary["total_cost_usd"] = compute_cost(
                summary.get("model", "") or meta.get("model", ""),
                summary.get("usage", {}),
            )
        self._json({"metadata": meta, "summary": summary})

    def _send_sse(self, event: str, data: str):
        msg = f"event: {event}\ndata: {data}\n\n"
        try:
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    def log_message(self, format, *args):
        pass  # Suppress request logging


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    port = 8765
    server = ThreadedHTTPServer(("127.0.0.1", port), AuditHandler)
    print(f"Audit viewer running at http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
