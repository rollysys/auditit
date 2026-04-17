#!/usr/bin/env python3
"""
server.py — Web backend for the auditit session viewer.

Serves:
  GET /              → Web UI (index.html)
  GET /api/sessions  → List all sessions
  GET /api/sessions/<session-id>/events → All events (JSONL or gz)
  GET /api/sessions/<session-id>/stream → SSE stream (live tail)
  GET /api/sessions/<session-id>/meta   → metadata + summary

Layout: ~/.claude-audit/<session-id>/audit.jsonl(.gz) — flat, no date
partitioning. Sub-agent dirs live as siblings: <session-id>__agent__<id>/.
The frontend groups/sorts by started_at and last_active_at as needed.

Cost is computed at serve time from summary.json's raw `usage` dict and
`model` name, against the PRICING table below. Keep docs/claude-pricing.md
in sync when pricing changes.
"""

import gzip
import json
import os
import shutil
import subprocess
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import unquote

AUDIT_DIR = Path.home() / ".claude-audit"
PROJECTS_DIR = Path.home() / ".claude" / "projects"
SKILLS_DIR = Path.home() / ".claude" / "skills"
REPO_DIR = Path(__file__).resolve().parent


def _read_repo_version():
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(REPO_DIR), "rev-parse", "--short=7", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        date = subprocess.check_output(
            ["git", "-C", str(REPO_DIR), "log", "-1", "--format=%cI", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return {"commit": commit, "date": date}
    except Exception:
        return {"commit": "", "date": ""}


REPO_VERSION = _read_repo_version()

# A session is considered "live" only if its audit.jsonl was touched within
# this many seconds. Beyond that, any stuck directory (sub-agent leftover,
# killed main session, whatever) is safe to delete.
ACTIVE_WINDOW_S = 60


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


def _is_session_active(session_dir: Path) -> bool:
    """Return True only if a session directory is still being written to.

    Not-active covers every case where we can safely delete:
      - summary.json exists (normal SessionEnd completion)
      - audit.jsonl.gz exists (atomic compress already done)
      - audit.jsonl missing (empty dir, nothing to lose)
      - audit.jsonl contains a SessionEnd event (hook fired but compress
        step failed for some reason)
      - audit.jsonl is stale — last mtime older than ACTIVE_WINDOW_S.
        This is the catch-all for sub-agent leftovers (sub-agents have
        their own session_id but never fire SessionStart/SessionEnd) and
        for main sessions killed with -9 before they could reach SessionEnd.
    """
    if (session_dir / "summary.json").exists():
        return False
    if (session_dir / "audit.jsonl.gz").exists():
        return False
    jsonl = session_dir / "audit.jsonl"
    if not jsonl.exists():
        return False
    try:
        age = time.time() - jsonl.stat().st_mtime
    except OSError:
        return False
    if age > ACTIVE_WINDOW_S:
        return False
    # Young file — look for a SessionEnd event we just failed to compress.
    try:
        with open(jsonl, "rb") as f:
            data = f.read()
        if b'"event":"SessionEnd"' in data:
            return False
    except OSError:
        pass
    return True


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


# ── Context window sizes ─────────────────────────────────────────────
#
# Source: https://platform.claude.com/docs/en/about-claude/models
# Snapshot: 2026-04-11, mirrored in docs/claude-context-windows.md.
# Opus 4.6 and Sonnet 4.6 ship a 1M context at standard pricing; every
# other listed model is 200k. Unknown models fall back to 200k.

DEFAULT_CTX_WINDOW = 200_000
CTX_WINDOW: dict[str, int] = {
    "claude-opus-4-6":   1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-5":     200_000,
    "claude-opus-4-1":     200_000,
    "claude-opus-4":       200_000,
    "claude-sonnet-4-5":   200_000,
    "claude-sonnet-4":     200_000,
    "claude-haiku-4-5":    200_000,
    "claude-haiku-3-5":    200_000,
    "claude-haiku-3":      200_000,
}


def _match_ctx_window(model: str) -> int:
    if not model:
        return DEFAULT_CTX_WINDOW
    m = model.lower()
    if m in CTX_WINDOW:
        return CTX_WINDOW[m]
    best_key: str | None = None
    for key in CTX_WINDOW:
        if key in m and (best_key is None or len(key) > len(best_key)):
            best_key = key
    return CTX_WINDOW[best_key] if best_key else DEFAULT_CTX_WINDOW


def compute_ctx(model: str, ctx_peak_tokens: int) -> dict:
    """Return a dict with ctx_peak_tokens / ctx_window / ctx_peak_pct.

    All three are always present so the Web UI can conditionally render.
    ctx_peak_pct is a float in [0, 1+]; values > 1.0 are clamped by the
    UI display but kept raw here for diagnostics.
    """
    peak = int(ctx_peak_tokens or 0)
    window = _match_ctx_window(model)
    pct = (peak / window) if window else 0.0
    return {
        "ctx_peak_tokens": peak,
        "ctx_window":      window,
        "ctx_peak_pct":    round(pct, 4),
    }


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


SUBAGENT_SEP = "__agent__"


def _load_subagent_meta(session_dir: Path) -> dict | None:
    """Return the meta.json written by hook.sh for sub-agent dirs, or None."""
    meta_path = session_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        with open(meta_path) as f:
            m = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(m, dict) or not m.get("is_subagent"):
        return None
    return m


def _last_active_iso(session_dir: Path) -> str:
    """ISO-8601 UTC of the LAST EVENT in the session's audit log.

    Reads the last line of audit.jsonl (or .gz) and parses its `ts`. This
    is the truth: file mtimes can be bumped by migrations, cp, tar, or any
    write — but every event line carries its own timestamp.

    Cached into metadata.json keyed by the audit file's current size so we
    only re-read when the file actually grew. For finished sessions (.gz)
    the cache is permanent because the file size never changes again.
    Returns "" when the session has no readable events.
    """
    jsonl = session_dir / "audit.jsonl"
    gz    = session_dir / "audit.jsonl.gz"
    # Resume case: both files coexist. Prefer .jsonl (newer events) but
    # fall back to .gz if .jsonl is empty (resume just started, no event
    # written yet). For a single-file session, this picks the only one.
    if jsonl.exists() and jsonl.stat().st_size > 0:
        src = jsonl
    elif gz.exists():
        src = gz
    elif jsonl.exists():
        src = jsonl
    else:
        return ""
    try:
        size = src.stat().st_size
    except OSError:
        return ""

    # Cache lookup
    meta_path = session_dir / "metadata.json"
    cached = None
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                cached = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            cached = None
    if cached and cached.get("_last_active_src") == src.name \
            and cached.get("_last_active_size") == size \
            and cached.get("last_active_at"):
        return cached["last_active_at"]

    # Recompute: pull the LAST non-empty line from the audit log.
    last_line = b""
    try:
        if src.name.endswith(".gz"):
            with gzip.open(src, "rb") as f:
                for line in f:
                    if line.strip():
                        last_line = line
        else:
            # Tail read — seek backward in 8KB chunks until we have at least
            # one full newline-terminated line. Avoids decompressing or
            # walking very long files.
            with open(src, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                if not end:
                    return ""
                chunk = 8192
                tail = b""
                pos = end
                while pos > 0 and tail.count(b"\n") < 2:
                    pos = max(0, pos - chunk)
                    f.seek(pos)
                    tail = f.read(end - pos)
                # Last non-empty line of tail
                for line in reversed(tail.splitlines()):
                    if line.strip():
                        last_line = line
                        break
    except OSError:
        return ""

    if not last_line:
        return ""
    try:
        obj = json.loads(last_line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    ts = obj.get("ts", "") or ""

    # Write through the cache (best-effort; never raise).
    if ts and meta_path.parent.exists():
        try:
            new_meta = dict(cached) if isinstance(cached, dict) else {}
            new_meta["last_active_at"]    = ts
            new_meta["_last_active_src"]  = src.name
            new_meta["_last_active_size"] = size
            tmp = meta_path.with_name(meta_path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(new_meta, f, indent=2)
            os.replace(tmp, meta_path)
        except OSError:
            pass
    return ts


_session_index_cache: dict[str, dict] = {}
_session_index_ts: float = 0
_SESSION_INDEX_TTL = 30.0  # seconds

COST_CACHE_DIR = AUDIT_DIR / "_cost_cache"


def _compute_session_usage(path: Path, sid: str) -> dict:
    """Full-parse a transcript to sum ALL assistant usage blocks.

    Claude transcript usage is per-turn, not cumulative. Reading only the
    tail gives the last turn's tokens, severely underestimating cost for
    multi-turn sessions. This function walks the entire file once.

    Results are cached to ~/.claude-audit/_cost_cache/<sid>.json keyed by
    the transcript file's size. Cache hit = O(1) disk read. Cache miss =
    full parse (can take 10-100ms for large transcripts). For completed
    sessions (file size stable) the cache is permanent.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return {}

    cache_path = COST_CACHE_DIR / f"{sid}.json"
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                cached = json.load(f)
            if isinstance(cached, dict) and cached.get("_file_size") == file_size:
                return cached
        except (OSError, json.JSONDecodeError):
            pass

    cum: dict = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_tokens": 0,
        "cache_creation_1h_tokens": 0,
    }
    model = ""
    num_turns = 0
    seen_msg_ids: set[str] = set()
    ctx_peak = 0

    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if obj.get("type") == "user":
                    msg = obj.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        num_turns += 1
                elif obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    m = msg.get("model", "")
                    if m and m != "<synthetic>":
                        model = m
                    mid = msg.get("id", "")
                    if mid and mid in seen_msg_ids:
                        continue
                    if mid:
                        seen_msg_ids.add(mid)
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    cum["input_tokens"] += (u.get("input_tokens", 0) or 0)
                    cum["output_tokens"] += (u.get("output_tokens", 0) or 0)
                    cum["cache_read_input_tokens"] += (u.get("cache_read_input_tokens", 0) or 0)
                    cum["cache_creation_input_tokens"] += (u.get("cache_creation_input_tokens", 0) or 0)
                    cc = u.get("cache_creation") or {}
                    if isinstance(cc, dict):
                        cum["cache_creation_5m_tokens"] += (cc.get("ephemeral_5m_input_tokens", 0) or 0)
                        cum["cache_creation_1h_tokens"] += (cc.get("ephemeral_1h_input_tokens", 0) or 0)
                    ctx_now = (
                        (u.get("input_tokens", 0) or 0)
                        + (u.get("cache_read_input_tokens", 0) or 0)
                        + (u.get("cache_creation_input_tokens", 0) or 0)
                    )
                    if ctx_now > ctx_peak:
                        ctx_peak = ctx_now
    except OSError:
        return {}

    result = dict(cum)
    result["model"] = model
    result["num_turns"] = num_turns
    result["ctx_peak_tokens"] = ctx_peak
    result["_file_size"] = file_size

    try:
        COST_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(result, f)
        os.replace(tmp, cache_path)
    except OSError:
        pass

    return result


def _scan_transcript_header(path: Path, sid: str = "") -> dict:
    """Read transcript header (first ~50 lines) for metadata + full usage.

    Metadata (cwd, entrypoint, prompt, timestamps): from header lines only.
    Usage and cost: from _compute_session_usage (full parse, disk-cached).
    """
    cwd = entrypoint = first_prompt = model = ""
    started_at = last_ts = ""
    usage: dict = {}
    num_user_turns = 0

    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 50:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = obj.get("timestamp", "")
                if ts and not started_at:
                    started_at = ts
                if ts:
                    last_ts = ts
                if not cwd:
                    cwd = obj.get("cwd", "") or ""
                if not entrypoint:
                    entrypoint = obj.get("entrypoint", "") or ""
                t = obj.get("type", "")
                if t == "queue-operation" and obj.get("operation") == "enqueue":
                    content = obj.get("content", "")
                    if not first_prompt and content:
                        first_prompt = content[:200]
                elif t == "user":
                    num_user_turns += 1
                    msg = obj.get("message", {})
                    content = msg.get("content", "") if isinstance(msg, dict) else ""
                    if isinstance(content, str) and not first_prompt:
                        if content and not content.startswith("<local-command"):
                            first_prompt = content[:200]
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item.get("text", "")
                                if not first_prompt and text and not text.startswith("<local-command"):
                                    first_prompt = text[:200]
                                    break
                elif t == "assistant":
                    msg = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
                    m = msg.get("model", "")
                    if m and m != "<synthetic>":
                        model = m
    except OSError:
        pass

    # Tail read for last_ts only (timestamps, not usage).
    if not path.name.endswith(".gz"):
        try:
            with open(path, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                chunk = min(8192, end)
                f.seek(max(0, end - chunk))
                tail = f.read()
            for raw_line in reversed(tail.splitlines()):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                ts = obj.get("timestamp", "")
                if ts:
                    last_ts = ts
                    break
        except OSError:
            pass

    # Full usage: sum ALL assistant turns (disk-cached, so only expensive
    # on first access per session). This is the only way to get accurate
    # cost — transcript usage is per-turn, not cumulative.
    full = _compute_session_usage(path, sid) if sid else {}
    if full.get("model"):
        model = full["model"]

    return {
        "cwd": cwd,
        "entrypoint": entrypoint,
        "first_prompt": first_prompt,
        "model": model,
        "started_at": started_at,
        "last_active_at": last_ts,
        "usage": full,
        "num_turns": full.get("num_turns", 0),
        "ctx_peak_tokens": full.get("ctx_peak_tokens", 0),
        "is_headless": entrypoint == "sdk-cli",
    }


def list_sessions() -> dict:
    """Return {sessions: [...]} by scanning transcript files.

    Data source: ~/.claude/projects/*/<sid>.jsonl — Claude Code's native
    transcript files. No dependency on hook-written audit data. Each
    transcript is scanned for header metadata (first ~50 lines + tail)
    to extract cwd, model, prompt, timestamps, and usage for cost.

    Results are cached for SESSION_INDEX_TTL seconds to avoid re-scanning
    thousands of transcript files on every /api/sessions call.
    """
    global _session_index_cache, _session_index_ts
    now = time.time()
    if _session_index_cache and (now - _session_index_ts) < _SESSION_INDEX_TTL:
        return {"sessions": list(_session_index_cache.values())}

    sessions_by_id: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return {"sessions": []}

    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        proj_name = proj_dir.name
        if proj_name.startswith(".") or proj_name == "memory":
            continue
        for f in proj_dir.iterdir():
            if f.is_dir():
                continue
            name = f.name
            # Match <uuid>.jsonl or <uuid>.jsonl.gz
            sid = ""
            if name.endswith(".jsonl.gz"):
                sid = name[:-9]
            elif name.endswith(".jsonl"):
                sid = name[:-6]
            if not sid or not _validate_session_id(sid):
                continue
            # Skip sub-agent dirs / names
            if SUBAGENT_SEP in sid:
                continue
            # Avoid re-scanning if we already have this sid from another
            # project dir (shouldn't happen, but defensive).
            if sid in sessions_by_id:
                continue

            header = _scan_transcript_header(f, sid)
            model = header.get("model", "")
            usage = header.get("usage", {})
            cost = compute_cost(model, _build_usage(usage) if usage else {})
            is_headless = header.get("is_headless", False)
            mode = "scripted" if is_headless else "interactive"

            # Determine active/completed: if the file was modified in the
            # last ACTIVE_WINDOW_S seconds, it's likely still being written.
            try:
                mtime = f.stat().st_mtime
                status = "active" if (now - mtime) < ACTIVE_WINDOW_S else "completed"
            except OSError:
                status = "completed"

            # Duration from timestamps
            duration_ms = 0
            s_ts = header.get("started_at", "")
            l_ts = header.get("last_active_at", "")
            if s_ts and l_ts:
                try:
                    from datetime import datetime as _dt, timezone as _tz
                    t0 = _dt.fromisoformat(s_ts.replace("Z", "+00:00"))
                    t1 = _dt.fromisoformat(l_ts.replace("Z", "+00:00"))
                    duration_ms = max(0, int((t1 - t0).total_seconds() * 1000))
                except (ValueError, TypeError):
                    pass

            sessions_by_id[sid] = {
                "id": sid,
                "prompt": header.get("first_prompt", ""),
                "model": model,
                "cwd": header.get("cwd", ""),
                "count": 0,
                "turns": header.get("num_turns", 0),
                "cost": round(cost, 4) if cost else 0,
                "duration_ms": duration_ms,
                "status": status,
                "started_at": header.get("started_at", ""),
                "last_active_at": header.get("last_active_at", ""),
                "is_subagent": False,
                "parent_session_id": "",
                "root_session_id": sid,
                "depth": 0,
                "mode": mode,
                "is_headless": is_headless,
                "project": proj_name,
            }

    _session_index_cache = sessions_by_id
    _session_index_ts = now
    return {"sessions": list(sessions_by_id.values())}


def _load_meta(session_dir: Path, sid: str) -> dict:
    """Return metadata.json if parseable, otherwise extract from events.

    metadata.json may exist but only contain the last_active_at cache
    without the SessionStart fields (if _last_active_iso ran before
    anyone else populated the file). In that case we still need to
    extract from events to recover prompt/model/cwd.

    Concurrent-write tolerance: the file can be rewritten under us by
    either _last_active_iso (atomic via os.replace) or the older
    non-atomic path in _extract_meta_from_events. Any parse failure
    falls through to the event-based extractor.
    """
    meta_path = session_dir / "metadata.json"
    cached: dict | None = None
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                cached = json.load(f)
            if not isinstance(cached, dict):
                cached = None
        except (OSError, json.JSONDecodeError):
            pass
    if cached is not None and (
        cached.get("model") or cached.get("prompt") or cached.get("cwd")
    ):
        # Cache exists and has some real metadata — but it may be incomplete
        # (e.g. _last_active_iso wrote prompt before _extract_meta_from_events
        # could fill cwd/started_at/model from a .gz SessionStart). Fill in any
        # missing core fields from the event stream rather than returning early
        # with a partial cache.
        missing = not cached.get("cwd") or not cached.get("started_at")
        if missing:
            from_events = _extract_meta_from_events(session_dir, sid)
            for k in ("model", "cwd", "started_at", "prompt"):
                if not cached.get(k) and from_events.get(k):
                    cached[k] = from_events[k]
            # Persist the merged result
            try:
                tmp = meta_path.with_suffix(".json.tmp")
                with open(tmp, "w") as f:
                    json.dump(cached, f, ensure_ascii=False, indent=2)
                os.replace(tmp, meta_path)
            except OSError:
                pass
        return cached
    # Fallback: extract from events (also covers the "only last_active"
    # shell case created by _last_active_iso on a session that has not
    # yet had its real metadata extracted).
    return _extract_meta_from_events(session_dir, sid)


def _extract_meta_from_events(session_dir: Path, sid: str) -> dict:
    """Walk the audit log(s) for the SessionStart + first UserPromptSubmit.

    For resumed sessions we must read BOTH the .gz (original SessionStart
    lives there) and the .jsonl (resume delta). Reading only one would
    miss either the start event or any newer prompt.
    """
    meta = {"prompt": "", "model": "", "cwd": "", "started_at": ""}

    # _audit_sources returns gz first then jsonl, chronological for our
    # purposes — which is what we want here (earliest SessionStart wins).
    sources = _audit_sources(session_dir)
    if not sources:
        return meta

    for path in sources:
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    d = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
                    if obj.get("event") == "SessionStart":
                        if not meta["model"]:
                            meta["model"] = d.get("model", "") or ""
                        if not meta["cwd"]:
                            meta["cwd"] = d.get("cwd", "") or ""
                        if not meta["started_at"]:
                            meta["started_at"] = obj.get("ts", "") or ""
                    elif obj.get("event") == "UserPromptSubmit":
                        prompt = (d.get("prompt", "") or "")[:200]
                        if prompt and not meta["prompt"]:
                            meta["prompt"] = prompt
                    if meta["model"] and meta["prompt"]:
                        break
        except Exception:
            continue
        if meta["model"] and meta["prompt"]:
            break

    # Persist so we don't re-parse next time. Merge with any cache that
    # _last_active_iso already dropped in — preserve its fields.
    if meta.get("model") or meta.get("prompt"):
        meta_path = session_dir / "metadata.json"
        merged = {}
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    merged = dict(existing)
            except (OSError, json.JSONDecodeError):
                pass
        for k, v in meta.items():
            if v and not merged.get(k):
                merged[k] = v
        # Atomic write — protects concurrent readers from mid-write tearing.
        try:
            tmp = meta_path.with_name(meta_path.name + ".tmp")
            with open(tmp, "w") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
            os.replace(tmp, meta_path)
        except OSError:
            pass
        return merged
    return meta


# Known third-party proxy hostnames → canonical provider name. Matched by
# substring against the netloc of ANTHROPIC_BASE_URL (captured at hook time
# into <session>/env.json). Keep this short — when the host is not matched
# we fall back to the raw host for display.
PROVIDER_HOSTS: list[tuple[str, str]] = [
    ("moonshot.cn",        "moonshot"),
    ("moonshot.ai",        "moonshot"),
    ("kimi.moonshot",      "moonshot"),
    ("z.ai",               "zhipu"),
    ("bigmodel.cn",        "zhipu"),
    ("dashscope.aliyuncs", "qwen"),
    ("dashscope-intl",     "qwen"),
    ("deepseek.com",       "deepseek"),
    ("api.openai.com",     "openai"),
    ("bedrock-runtime",    "bedrock"),
    ("bedrock.",           "bedrock"),
    ("aiplatform.google",  "vertex"),
    ("api.x.ai",           "xai"),
    ("api.anthropic.com",  "anthropic"),
]


def _host_of(url: str) -> str:
    if not url:
        return ""
    u = url.strip()
    # Strip scheme
    if "://" in u:
        u = u.split("://", 1)[1]
    # Keep only netloc (drop path/query)
    for sep in ("/", "?", "#"):
        if sep in u:
            u = u.split(sep, 1)[0]
    # Drop port
    if ":" in u:
        u = u.split(":", 1)[0]
    return u.lower()


def _load_env(session_dir: Path) -> dict:
    """Return env.json content (ANTHROPIC_BASE_URL / Bedrock / Vertex flags).

    Written by hook.sh on the first event of each session. Missing file
    means a pre-env-capture session (historical) — caller falls back to
    model-name heuristics in that case.
    """
    p = session_dir / "env.json"
    if not p.exists():
        return {}
    try:
        with open(p) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def detect_mode(env: dict | None = None, session_id: str = "") -> str:
    """Return "scripted" or "interactive" based on env.json + transcript.

    Primary source: env.json is_headless flag.
    Fallback: transcript entrypoint == "sdk-cli" means scripted.
    Historical sessions without either default to "interactive".
    """
    env = env or {}
    if env.get("is_headless") is True:
        return "scripted"
    # Fallback to transcript entrypoint when env.json lacks is_headless
    if session_id and "is_headless" not in env:
        t = _find_transcript(session_id)
        if t:
            try:
                opener = gzip.open if t.name.endswith(".gz") else open
                with opener(t, "rt", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if obj.get("entrypoint") == "sdk-cli":
                            return "scripted"
                        if obj.get("entrypoint"):
                            break
            except Exception:
                pass
    return "interactive"


def detect_provider(model: str, env: dict | None = None) -> str:
    """Infer the API provider, preferring env-based signals over model name.

    Priority:
      1. env.json flags: use_bedrock / use_vertex → bedrock / vertex
      2. env.json anthropic_base_url → known-host map or raw host
      3. model name prefix (historical sessions with no env.json)
    """
    env = env or {}
    if str(env.get("use_bedrock", "")).lower() in ("1", "true", "yes"):
        return "bedrock"
    if str(env.get("use_vertex", "")).lower() in ("1", "true", "yes"):
        return "vertex"
    base_url = env.get("anthropic_base_url", "") or ""
    host = _host_of(base_url)
    if host:
        for needle, name in PROVIDER_HOSTS:
            if needle in host:
                return name
        # Unmapped third-party host — return the host as the provider label
        # so the UI still renders something meaningful (e.g. "api.foo.com").
        return host

    # Historical fallback: model-name prefix heuristic
    if not model:
        return "unknown"
    m = model.lower().strip()
    if m == "<synthetic>":
        return "synthetic"
    if m.startswith("anthropic.") or ".anthropic." in m:
        return "bedrock"
    if "@" in m and "claude-" in m:
        return "vertex"
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith("qwen"):     return "qwen"
    if m.startswith("glm"):      return "zhipu"
    if m.startswith("kimi") or m.startswith("moonshot"): return "moonshot"
    if m.startswith("deepseek"): return "deepseek"
    if m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if m.startswith("gemini"):   return "gemini"
    if m.startswith("grok"):     return "xai"
    return "other"


def build_stats(exclude_scripted: bool = False) -> dict:
    """Aggregate across all sessions for the Dashboard view.

    Reuses list_sessions()'s cached transcript index — no separate scan.
    Sub-agents are excluded (list_sessions already filters them).

    Cost comes from the transcript header's tail-read usage + model →
    compute_cost(). This is the same data source as the Sessions list,
    so totals are consistent.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now_utc = _dt.now(_tz.utc)

    def _parse_ts(s):
        if not isinstance(s, str):
            return None
        try:
            return _dt.fromisoformat(s.replace("Z", "+00:00"))
        except (ValueError, TypeError, AttributeError):
            return None

    totals = {
        "sessions": 0, "cost": 0.0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "duration_ms": 0, "turns": 0,
        "last_1h_cost":  0.0,
        "last_24h_cost": 0.0,
        "last_7d_cost":  0.0,
    }
    by_model: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    by_date: dict[str, dict] = {}
    by_hour: dict[str, dict] = {}
    by_week: dict[str, dict] = {}
    all_sessions: list[dict] = []

    empty = {"totals": totals, "by_model": [], "by_provider": [],
             "by_date": [], "by_hour": [], "by_week": [],
             "top_by_cost": [], "top_by_ctx": [], "top_cost_windows": []}

    # Pull the session index (cached, cheap).
    idx = list_sessions()
    sessions = idx.get("sessions", [])
    if not sessions:
        return empty

    for s in sessions:
        if s.get("is_subagent"):
            continue
        if exclude_scripted and s.get("mode") == "scripted":
            continue
        model = s.get("model", "") or "unknown"
        cost  = s.get("cost", 0) or 0
        dur   = s.get("duration_ms", 0) or 0
        turns = s.get("turns", 0) or 0
        provider = detect_provider(model)

        date_bucket = (s.get("started_at", "") or s.get("last_active_at", ""))[:10] or "unknown"
        calls_per_hr = (turns * 3_600_000.0 / dur) if dur >= 1000 else 0.0

        totals["sessions"] += 1
        totals["cost"] += cost
        totals["duration_ms"] += dur
        totals["turns"] += turns

        bm = by_model.setdefault(model, {
            "model": model, "provider": provider, "sessions": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "turns": 0, "duration_ms": 0,
        })
        bm["sessions"] += 1
        bm["cost"] += cost
        bm["turns"] += turns
        bm["duration_ms"] += dur

        bp = by_provider.setdefault(provider, {
            "provider": provider, "sessions": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "turns": 0, "duration_ms": 0,
        })
        bp["sessions"] += 1
        bp["cost"] += cost
        bp["turns"] += turns
        bp["duration_ms"] += dur

        bd = by_date.setdefault(date_bucket, {
            "date": date_bucket, "sessions": 0, "cost": 0.0,
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "turns": 0, "duration_ms": 0,
        })
        bd["sessions"] += 1
        bd["cost"] += cost
        bd["turns"] += turns
        bd["duration_ms"] += dur

        started_at = s.get("started_at", "") or s.get("last_active_at", "")
        bucket_dt = _parse_ts(started_at)
        if bucket_dt is not None:
            hour_key = bucket_dt.strftime("%Y-%m-%dT%H:00Z")
            bh = by_hour.setdefault(hour_key, {
                "hour": hour_key, "sessions": 0, "cost": 0.0,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "turns": 0, "duration_ms": 0,
            })
            bh["sessions"] += 1
            bh["cost"] += cost
            bh["turns"] += turns
            bh["duration_ms"] += dur

            iso_year, iso_week, _ = bucket_dt.isocalendar()
            week_key = f"{iso_year}-W{iso_week:02d}"
            bw = by_week.setdefault(week_key, {
                "week": week_key, "sessions": 0, "cost": 0.0,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "turns": 0, "duration_ms": 0,
            })
            bw["sessions"] += 1
            bw["cost"] += cost
            bw["turns"] += turns
            bw["duration_ms"] += dur

            # Rolling totals: attribute to "last N" windows if the session
            # started within that window. Same coarse-attribution caveat.
            age = now_utc - bucket_dt
            if age <= _td(hours=1):
                totals["last_1h_cost"] += cost
            if age <= _td(hours=24):
                totals["last_24h_cost"] += cost
            if age <= _td(days=7):
                totals["last_7d_cost"] += cost

        all_sessions.append({
            "id": s.get("id", ""),
            "date": date_bucket,
            "model": model,
            "provider": provider,
            "mode": s.get("mode", "interactive"),
            "cost": round(cost, 4),
            "turns": turns,
            "duration_ms": dur,
            "calls_per_hr": round(calls_per_hr, 1),
            "prompt": s.get("prompt", "")[:200],
            "cwd": s.get("cwd", ""),
            "ctx_peak_pct": 0,
            "ctx_peak_tokens": 0,
            "ctx_window": 0,
        })

    # Round totals for cleaner JSON; compute calls/hr from aggregated duration
    def _cph(turns: int, dur_ms: int) -> float:
        return round(turns * 3_600_000.0 / dur_ms, 1) if dur_ms >= 1000 else 0.0

    totals["cost"] = round(totals["cost"], 4)
    totals["calls_per_hr"] = _cph(totals["turns"], totals["duration_ms"])
    for m in by_model.values():
        m["cost"] = round(m["cost"], 4)
        m["calls_per_hr"] = _cph(m["turns"], m["duration_ms"])
    for p in by_provider.values():
        p["cost"] = round(p["cost"], 4)
        p["calls_per_hr"] = _cph(p["turns"], p["duration_ms"])
    for d in by_date.values():
        d["cost"] = round(d["cost"], 4)
        d["calls_per_hr"] = _cph(d["turns"], d["duration_ms"])
    for h in by_hour.values():
        h["cost"] = round(h["cost"], 4)
        h["calls_per_hr"] = _cph(h["turns"], h["duration_ms"])
    for w in by_week.values():
        w["cost"] = round(w["cost"], 4)
        w["calls_per_hr"] = _cph(w["turns"], w["duration_ms"])
    totals["last_1h_cost"]  = round(totals["last_1h_cost"],  4)
    totals["last_24h_cost"] = round(totals["last_24h_cost"], 4)
    totals["last_7d_cost"]  = round(totals["last_7d_cost"],  4)

    # Trim to display windows — last 30 days / last 72h / last 12 weeks.
    # Ascending by key so the UI can plot left-to-right.
    cutoff_day  = (now_utc - _td(days=30)).strftime("%Y-%m-%d")
    cutoff_hour = (now_utc - _td(hours=72)).strftime("%Y-%m-%dT%H:00Z")
    by_date_list = sorted(
        (d for d in by_date.values() if d["date"] >= cutoff_day),
        key=lambda x: x["date"],
    )
    by_hour_list = sorted(
        (h for h in by_hour.values() if h["hour"] >= cutoff_hour),
        key=lambda x: x["hour"],
    )
    by_week_list = sorted(by_week.values(), key=lambda x: x["week"])[-12:]

    by_model_list    = sorted(by_model.values(),    key=lambda x: -x["cost"])
    by_provider_list = sorted(by_provider.values(), key=lambda x: -x["cost"])

    top_by_cost = sorted(all_sessions, key=lambda x: -x["cost"])[:20]
    top_by_ctx  = sorted(all_sessions, key=lambda x: -x["ctx_peak_pct"])[:20]

    # Most expensive hours: catches "many cheap sessions add up" which
    # top_by_cost (individual max) misses.
    top_cost_windows = sorted(
        by_hour.values(), key=lambda x: -x["cost"],
    )[:10]
    # Only include hours with non-trivial cost to avoid a list of zeros.
    top_cost_windows = [h for h in top_cost_windows if h["cost"] >= 0.001]

    return {
        "totals": totals,
        "by_model": by_model_list,
        "by_provider": by_provider_list,
        "by_date": by_date_list,
        "by_hour": by_hour_list,
        "by_week": by_week_list,
        "top_by_cost": top_by_cost,
        "top_by_ctx": top_by_ctx,
        "top_cost_windows": top_cost_windows,
    }


def _load_summary(session_dir: Path) -> dict:
    summary_path = session_dir / "summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            return json.load(f)
    return {}


def _count_events(session_dir: Path) -> int:
    """Sum events across all audit sources (handles resumed sessions)."""
    count = 0
    for path in _audit_sources(session_dir):
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    count += 1
        except Exception:
            continue
    return count


def _audit_sources(session_dir: Path) -> list[Path]:
    """Return the audit log files for a session in chronological order.

    A "resumed" session has BOTH audit.jsonl.gz (history from a previous
    SessionEnd) AND audit.jsonl (events since the resume). Always read
    .gz first when present so the timeline reads start-to-now even after
    one or more resumes. The next SessionEnd will merge them back into
    a single .gz; until then we just concatenate at read time.
    """
    sources = []
    gz = session_dir / "audit.jsonl.gz"
    jsonl = session_dir / "audit.jsonl"
    if gz.exists():
        sources.append(gz)
    if jsonl.exists():
        sources.append(jsonl)
    return sources


def read_events(session_dir: Path) -> list:
    events: list = []
    for path in _audit_sources(session_dir):
        opener = gzip.open if path.name.endswith(".gz") else open
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
        except Exception:
            continue
    return events


def resolve_session(session_id: str) -> Path | None:
    """Return the session dir if it exists AND is contained within AUDIT_DIR.

    The containment check defends against path traversal via session_id
    components like "..", "/", or absolute paths injected through the URL.
    """
    candidate = (AUDIT_DIR / session_id).resolve()
    try:
        candidate.relative_to(AUDIT_DIR.resolve())
    except ValueError:
        return None
    if candidate.is_dir():
        return candidate
    return None


# ── Transcript support ───────────────────────────────────────────────
#
# Claude Code writes a full transcript for every session under
# ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl  (or .jsonl.gz).
# This is the authoritative record of the conversation: assistant text,
# thinking, tool calls, tool results, per-message usage, etc.
#
# The functions below locate and parse these transcripts, normalizing
# them into a simple event stream that the frontend can render.

_transcript_cache: dict[str, tuple[Path, float]] = {}  # sid → (path, cache_time)
_TRANSCRIPT_CACHE_TTL = 60.0  # seconds before a cache entry is re-verified


def _validate_session_id(session_id: str) -> bool:
    """Reject session_ids that could cause path traversal or shenanigans."""
    if not session_id:
        return False
    if "/" in session_id or "\\" in session_id or "\x00" in session_id:
        return False
    if ".." in session_id:
        return False
    return True


def _build_usage(raw_usage: dict) -> dict:
    """Build a flat usage dict from accumulated transcript usage.

    Converts the nested cache_creation dict into flat _5m/_1h fields
    that compute_cost() expects.
    """
    cc = raw_usage.get("cache_creation") or {}
    if not isinstance(cc, dict):
        cc = {}
    return {
        "input_tokens":                raw_usage.get("input_tokens", 0) or 0,
        "output_tokens":               raw_usage.get("output_tokens", 0) or 0,
        "cache_read_input_tokens":     raw_usage.get("cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": raw_usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_creation_5m_tokens":    cc.get("ephemeral_5m_input_tokens", 0) or 0,
        "cache_creation_1h_tokens":    cc.get("ephemeral_1h_input_tokens", 0) or 0,
    }


def _find_transcript(session_id: str) -> Path | None:
    """Locate the transcript file for a given session_id.

    Searches in order:
      1. Top-level <session_id>.jsonl/.jsonl.gz under any project dir
      2. Sub-agent: <any_session>/subagents/agent-<id>.jsonl under any project dir
         (session_id must start with "agent-")

    Security: session_id is validated to prevent path traversal (no "/",
    "..", or null bytes). The resolved candidate is checked to stay under
    PROJECTS_DIR as defense-in-depth.

    Caching: results are cached for TRANSCRIPT_CACHE_TTL seconds. Cache
    entries whose file has been deleted/moved are evicted on access.
    """
    if not _validate_session_id(session_id):
        return None

    now = time.time()
    if session_id in _transcript_cache:
        p, cached_at = _transcript_cache[session_id]
        if (now - cached_at) < _TRANSCRIPT_CACHE_TTL and p.exists():
            return p
        del _transcript_cache[session_id]

    projects_resolved = PROJECTS_DIR.resolve()
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        # Top-level transcript
        for suffix in (".jsonl", ".jsonl.gz"):
            candidate = proj_dir / (session_id + suffix)
            if not candidate.exists():
                continue
            # Defense-in-depth: must stay under PROJECTS_DIR
            try:
                candidate.resolve().relative_to(projects_resolved)
            except ValueError:
                continue
            _transcript_cache[session_id] = (candidate, now)
            return candidate
        # Sub-agent transcript: session_id starts with "agent-"
        if session_id.startswith("agent-"):
            for sid_dir in proj_dir.iterdir():
                if not sid_dir.is_dir():
                    continue
                sub_dir = sid_dir / "subagents"
                if not sub_dir.is_dir():
                    continue
                for suffix in (".jsonl", ".jsonl.gz"):
                    candidate = sub_dir / (session_id + suffix)
                    if not candidate.exists():
                        continue
                    try:
                        candidate.resolve().relative_to(projects_resolved)
                    except ValueError:
                        continue
                    _transcript_cache[session_id] = (candidate, now)
                    return candidate
    return None


def read_transcript(session_id: str) -> dict:
    """Read a Claude transcript and return a dict with events + session meta.

    Returns: {"events": [...], "entrypoint": str, "is_headless": bool,
              "cwd": str, "first_prompt": str, "model": str, "usage": dict}

    The transcript contains raw Claude API-level messages. We normalize
    them into a flat event list with types the frontend understands:
      - {type: "user_text", text, timestamp}
      - {type: "tool_result", tool_use_id, content, is_error, timestamp}
      - {type: "assistant_text", text, model, usage, timestamp}
      - {type: "assistant_thinking", text, model, usage, timestamp}
      - {type: "assistant_tool_use", name, id, input, model, usage, timestamp}
    """
    path = _find_transcript(session_id)
    if not path:
        return {}

    # Map tool_use_id → tool name, for enriching tool_result events
    tool_name_map: dict[str, str] = {}

    events: list = []
    entrypoint = ""
    is_headless = False
    cwd = ""
    first_prompt = ""
    model = ""
    cum_usage: dict = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation": {"ephemeral_5m_input_tokens": 0,
                           "ephemeral_1h_input_tokens": 0},
    }
    seen_msg_ids: set[str] = set()

    opener = gzip.open if path.name.endswith(".gz") else open
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Session-level metadata (from any line that has it)
                if not entrypoint and obj.get("entrypoint"):
                    entrypoint = obj["entrypoint"]
                if not cwd and obj.get("cwd"):
                    cwd = obj["cwd"]

                # Detect headless: entrypoint=sdk-cli means claude -p
                t = obj.get("type", "")
                if t == "queue-operation" and obj.get("operation") == "enqueue":
                    content = obj.get("content", "")
                    if not first_prompt and content:
                        first_prompt = content[:200]

                ts = obj.get("timestamp", "")
                msg = obj.get("message", {})

                if t == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str):
                        if not first_prompt and content and not content.startswith("<local-command"):
                            first_prompt = content[:200]
                        events.append({"type": "user_text", "text": content,
                                       "timestamp": ts})
                    elif isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            it = item.get("type", "")
                            if it == "text":
                                text = item.get("text", "")
                                if not first_prompt and text and not text.startswith("<local-command"):
                                    first_prompt = text[:200]
                                events.append({"type": "user_text",
                                               "text": text,
                                               "timestamp": ts})
                            elif it == "tool_result":
                                tid = item.get("tool_use_id", "")
                                events.append({
                                    "type": "tool_result",
                                    "tool_use_id": tid,
                                    "tool_name": tool_name_map.get(tid, ""),
                                    "content": item.get("content", ""),
                                    "is_error": bool(item.get("is_error")),
                                    "timestamp": ts,
                                })

                elif t == "assistant":
                    m = msg.get("model", "")
                    is_synthetic = (m == "<synthetic>")
                    # Deduplicate
                    mid = msg.get("id", "")
                    if mid and mid in seen_msg_ids:
                        continue
                    if mid:
                        seen_msg_ids.add(mid)
                    if m and not is_synthetic:
                        model = m
                    # Accumulate usage from real (non-synthetic) messages only.
                    # Synthetic messages are Claude Code's client-side error
                    # responses (e.g. "Not logged in") — they carry no API usage.
                    usage = msg.get("usage", {}) if not is_synthetic else {}
                    if not is_synthetic and isinstance(usage, dict):
                            cum_usage["input_tokens"] += (usage.get("input_tokens", 0) or 0)
                            cum_usage["output_tokens"] += (usage.get("output_tokens", 0) or 0)
                            cum_usage["cache_read_input_tokens"] += (usage.get("cache_read_input_tokens", 0) or 0)
                            cum_usage["cache_creation_input_tokens"] += (usage.get("cache_creation_input_tokens", 0) or 0)
                            cc = usage.get("cache_creation") or {}
                            if isinstance(cc, dict):
                                cum_usage["cache_creation"]["ephemeral_5m_input_tokens"] += (cc.get("ephemeral_5m_input_tokens", 0) or 0)
                                cum_usage["cache_creation"]["ephemeral_1h_input_tokens"] += (cc.get("ephemeral_1h_input_tokens", 0) or 0)

                    content = msg.get("content", [])
                    if isinstance(content, str) and content:
                        events.append({"type": "assistant_text",
                                       "text": content,
                                       "model": m,
                                       "usage": {},
                                       "timestamp": ts})
                        continue
                    if not isinstance(content, list):
                        continue
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        it = item.get("type", "")
                        if it == "text":
                            events.append({"type": "assistant_text",
                                           "text": item.get("text", ""),
                                           "model": m,
                                           "usage": usage,
                                           "timestamp": ts})
                        elif it == "thinking":
                            events.append({"type": "assistant_thinking",
                                           "text": item.get("thinking", ""),
                                           "model": m,
                                           "usage": usage,
                                           "timestamp": ts})
                        elif it == "tool_use":
                            tid = item.get("id", "")
                            name = item.get("name", "")
                            tool_name_map[tid] = name
                            events.append({
                                "type": "assistant_tool_use",
                                "name": name,
                                "id": tid,
                                "input": item.get("input", {}),
                                "model": m,
                                "usage": usage,
                                "timestamp": ts,
                            })
                # Skip: system, file-history-snapshot, attachment, etc.
    except Exception as exc:
        import traceback
        try:
            with open(AUDIT_DIR / "_server_errors.log", "a") as ef:
                ef.write(f"read_transcript({session_id}): {type(exc).__name__}: {exc}\n")
                traceback.print_exc(file=ef)
        except OSError:
            pass

    # Detect headless from entrypoint — done ONCE after parsing, not
    # inside the per-line loop where it was previously misplaced.
    if entrypoint == "sdk-cli":
        is_headless = True

    return {
        "events": events,
        "entrypoint": entrypoint,
        "is_headless": is_headless,
        "cwd": cwd,
        "first_prompt": first_prompt,
        "model": model,
        "usage": cum_usage,
    }


# ── Memory view helpers ──────────────────────────────────────────────
#
# The Memory view in the Web UI shows CLAUDE.md-family files for every
# project Claude Code has ever touched, plus the global user file and
# auto-memory markdown under ~/.claude/projects/<encoded>/memory/.
#
# Every file path returned to the client is validated against a strict
# allow-list before being read, so the /api/memory/file endpoint cannot
# be used to exfiltrate arbitrary files via crafted URLs.

CLAUDE_HOME_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_HOME_DIR / "projects"
GLOBAL_CLAUDE_MD = CLAUDE_HOME_DIR / "CLAUDE.md"


def _resolve_project_cwd(project_dir: Path) -> str:
    """Return the real cwd for a ~/.claude/projects/<encoded>/ directory.

    Claude Code's encoding is `/` → `-`, which is lossy for paths that
    contain `-`, so we don't decode the name; we instead read the `cwd`
    field from the most recent transcript file in the dir. Falls back to
    a naive decode if no transcript has a usable cwd.
    """
    jsonls = sorted(
        (p for p in project_dir.glob("*.jsonl") if p.is_file()),
        key=lambda p: -p.stat().st_mtime,
    )
    for jsonl in jsonls[:3]:
        try:
            with open(jsonl, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict) and obj.get("cwd"):
                        return obj["cwd"]
        except OSError:
            continue
    # Fallback: naive decode. Correct for paths with no `-`, wrong
    # otherwise; only used as a last resort label for display.
    name = project_dir.name
    if name.startswith("-"):
        return name.replace("-", "/")
    return name


def _collect_memory_files(cwd: str, project_dir: Path) -> list[dict]:
    """Collect the CLAUDE.md + .claude/CLAUDE.md + auto-memory/*.md files
    for a given project, with size + mtime metadata. Missing files are
    silently skipped.
    """
    files: list[dict] = []

    def _stat_entry(p: Path, category: str) -> None:
        try:
            st = p.stat()
        except OSError:
            return
        files.append({
            "category": category,
            "file_path": str(p),
            "size": st.st_size,
            "mtime": st.st_mtime,
        })

    cwd_p = Path(cwd)
    if (cwd_p / "CLAUDE.md").is_file():
        _stat_entry(cwd_p / "CLAUDE.md", "CLAUDE.md")
    # Skip <cwd>/.claude/CLAUDE.md when it actually IS the global file —
    # happens when someone runs claude from their home directory so
    # /Users/foo/.claude/CLAUDE.md is both "global" and the project's
    # .claude/CLAUDE.md. The global entry already covers it.
    local_claude = cwd_p / ".claude" / "CLAUDE.md"
    try:
        is_global_alias = local_claude.resolve() == GLOBAL_CLAUDE_MD.resolve()
    except OSError:
        is_global_alias = False
    if local_claude.is_file() and not is_global_alias:
        _stat_entry(local_claude, ".claude/CLAUDE.md")

    memory_dir = project_dir / "memory"
    if memory_dir.is_dir():
        for md in sorted(memory_dir.glob("*.md")):
            _stat_entry(md, "auto-memory")

    return files


def build_memory_index() -> dict:
    """Return the full memory index for the /api/memory endpoint."""
    result: dict = {"global": None, "projects": []}

    if GLOBAL_CLAUDE_MD.is_file():
        try:
            st = GLOBAL_CLAUDE_MD.stat()
            result["global"] = {
                "category": "global",
                "file_path": str(GLOBAL_CLAUDE_MD),
                "size": st.st_size,
                "mtime": st.st_mtime,
            }
        except OSError:
            pass

    if CLAUDE_PROJECTS_DIR.is_dir():
        for p in CLAUDE_PROJECTS_DIR.iterdir():
            if not p.is_dir():
                continue
            cwd = _resolve_project_cwd(p)
            files = _collect_memory_files(cwd, p)
            if not files:
                continue
            latest_mtime = max(f["mtime"] for f in files)
            result["projects"].append({
                "name": os.path.basename(cwd.rstrip("/")) or cwd,
                "cwd": cwd,
                "encoded": p.name,
                "files": files,
                "latest_mtime": latest_mtime,
            })

    # Secondary sort on `encoded` so ties (files with identical mtime,
    # common after bulk copies or fresh installs) give a stable order.
    result["projects"].sort(key=lambda x: (-x["latest_mtime"], x["encoded"]))
    return result


def is_memory_path_allowed(target: Path) -> bool:
    """Allow-list check for /api/memory/file reads.

    Accepts only:
      1. ~/.claude/CLAUDE.md (exact)
      2. ~/.claude/projects/<encoded>/memory/<name>.md (2 levels deep)
      3. Any CLAUDE.md whose parent project has a corresponding entry
         under ~/.claude/projects/, i.e. Claude Code has actually been
         used in that project at least once.

    Defense-in-depth: reject any raw path that is itself a symlink.
    .resolve() below would follow it and the resulting canonical path
    is already checked by the allow-list, so this is belt-and-braces —
    it just makes the intent explicit and gives the attacker no way to
    reason about follow-then-check gaps.
    """
    try:
        if target.is_symlink():
            return False
        target = target.resolve()
    except OSError:
        return False
    home = Path.home().resolve()

    if target == (home / ".claude" / "CLAUDE.md"):
        return True

    projects_root = (home / ".claude" / "projects").resolve()
    try:
        rel = target.relative_to(projects_root)
        parts = rel.parts
        # rel must be <encoded>/memory/<name>.md
        if (len(parts) == 3
            and parts[1] == "memory"
            and target.suffix == ".md"
            and target.is_file()):
            return True
    except ValueError:
        pass

    if target.name == "CLAUDE.md" and target.is_file():
        project_cwd = target.parent
        if project_cwd.name == ".claude":
            project_cwd = project_cwd.parent
        # Project must be known to Claude Code: an encoded dir exists.
        # Naive encoding is correct for paths without `-`; for the others
        # we fall back to comparing against resolved cwds in the index.
        encoded = str(project_cwd).replace("/", "-")
        if (home / ".claude" / "projects" / encoded).is_dir():
            return True
        # Lossy-encoded fallback: scan the index for a matching cwd.
        try:
            for p in (home / ".claude" / "projects").iterdir():
                if not p.is_dir():
                    continue
                if _resolve_project_cwd(p) == str(project_cwd):
                    return True
        except OSError:
            pass

    return False


def list_skills():
    """Enumerate user-level skills at ~/.claude/skills/<name>/.

    Returns a list of {name, path, description, files: [rel_path, ...]}.
    Description is parsed best-effort from SKILL.md YAML frontmatter.
    """
    out = []
    if not SKILLS_DIR.is_dir():
        return out
    for d in sorted(SKILLS_DIR.iterdir()):
        # Follow top-level symlinks — user skill dirs are commonly symlinks
        # into a dotfiles repo or another project. Read-only serve only.
        if not d.is_dir():
            continue
        description = ""
        skill_md = d / "SKILL.md"
        if skill_md.is_file():
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end > 0:
                        for line in text[3:end].splitlines():
                            line = line.strip()
                            if line.lower().startswith("description:"):
                                description = line.split(":", 1)[1].strip().strip('"').strip("'")
                                break
            except OSError:
                pass
        files = []
        for f in sorted(d.rglob("*")):
            if f.is_symlink() or not f.is_file():
                continue
            try:
                rel = f.relative_to(d)
            except ValueError:
                continue
            files.append({"path": str(rel), "size": f.stat().st_size})
        out.append({
            "name": d.name,
            "path": str(d),
            "description": description,
            "files": files,
        })
    return out


def resolve_skill_file(name: str, rel_path: str) -> Path | None:
    """Safely resolve ~/.claude/skills/<name>/<rel_path> — reject any
    path that escapes the skill directory or follows a symlink.
    """
    if not name or "/" in name or name.startswith(".") or "\x00" in name:
        return None
    if "\x00" in rel_path or rel_path.startswith("/"):
        return None
    # Anchor must be a direct child of ~/.claude/skills — block names that
    # try to escape via ".." etc. (already partially blocked by the "/" check).
    anchor = SKILLS_DIR / name
    if anchor.parent.resolve() != SKILLS_DIR.resolve():
        return None
    # Follow the top-level symlink (dotfiles-style skills).
    try:
        base = anchor.resolve()
    except OSError:
        return None
    if not base.is_dir():
        return None
    target = base / rel_path
    try:
        if target.is_symlink():
            return None
        target_resolved = target.resolve()
    except OSError:
        return None
    # Resolved target must stay within the resolved skill directory.
    if not str(target_resolved).startswith(str(base) + os.sep):
        return None
    if not target_resolved.is_file():
        return None
    return target_resolved


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
            # /api/sessions/<session-id>/events → 5 parts
            if len(parts) == 5:
                self._serve_events(parts[3])
        elif path.startswith("/api/sessions/") and path.endswith("/stream"):
            parts = path.split("/")
            if len(parts) == 5:
                self._stream_events(parts[3])
        elif path.startswith("/api/sessions/") and path.endswith("/meta"):
            parts = path.split("/")
            # /api/sessions/<session-id>/meta → 5 parts
            if len(parts) == 5:
                self._serve_meta(parts[3])
        elif path.startswith("/api/sessions/") and path.endswith("/transcript"):
            parts = path.split("/")
            # /api/sessions/<session-id>/transcript → 5 parts
            if len(parts) == 5:
                self._serve_transcript(parts[3])
        elif path.startswith("/api/sessions/") and path.endswith("/subagents"):
            parts = path.split("/")
            # /api/sessions/<session-id>/subagents → 5 parts
            if len(parts) == 5:
                self._serve_subagents(parts[3])
        elif path == "/api/memory":
            self._json(build_memory_index())
        elif path == "/api/memory/file":
            self._serve_memory_file()
        elif path == "/api/version":
            self._json(REPO_VERSION)
        elif path == "/api/stats":
            from urllib.parse import parse_qs
            qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            exclude = (qs.get("exclude_scripted") or ["0"])[0] in ("1", "true", "yes")
            self._json(build_stats(exclude_scripted=exclude))
        elif path == "/api/skills":
            self._json(list_skills())
        elif path == "/api/skills/file":
            self._serve_skill_file()
        else:
            # Serve static files (web/index.html)
            super().do_GET()

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/sessions/"):
            parts = path.split("/")
            # /api/sessions/<session-id> → 4 parts
            if len(parts) == 4:
                self._delete_session(parts[3])
                return
        self.send_response(404)
        self.end_headers()

    def _json(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self, session_id: str):
        session_dir = resolve_session(unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return
        events = read_events(session_dir)
        self._json(events)

    def _stream_events(self, session_id: str):
        session_dir = resolve_session(unquote(session_id))
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
                        # SSE is a live tail, NOT a replay. The frontend
                        # already fetched everything via /events before
                        # opening this stream — replaying the existing
                        # content would render every line twice. Seek to
                        # end so we only emit appended events.
                        fh.seek(0, 2)
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

    def _serve_transcript(self, session_id: str):
        session_id = unquote(session_id)
        data = read_transcript(session_id)
        if not data:
            self.send_response(404)
            self.end_headers()
            return
        self._json(data)

    def _serve_subagents(self, session_id: str):
        """List sub-agents for a session, combining audit dirs + transcript dirs.

        Returns a list of {id, agent_type, description, source} dicts.
        source is "audit" (from __agent__ dirs) or "transcript" (from subagents/).
        """
        session_id = unquote(session_id)
        seen: dict[str, dict] = {}

        # Source 1: audit __agent__ dirs
        if SUBAGENT_SEP in session_id:
            # This IS a sub-agent; no further nesting
            self._json([])
            return
        prefix = session_id + SUBAGENT_SEP
        for d in AUDIT_DIR.iterdir():
            if not d.is_dir():
                continue
            if not d.name.startswith(prefix):
                continue
            agent_id = d.name[len(prefix):]
            meta = _load_subagent_meta(d)
            if agent_id not in seen:
                seen[agent_id] = {
                    "id": f"agent-{agent_id}",
                    "agent_type": (meta or {}).get("agent_type", ""),
                    "description": (meta or {}).get("description", ""),
                    "source": "audit",
                }

        # Source 2: transcript subagents/ dirs
        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            sid_dir = proj_dir / session_id
            sub_dir = sid_dir / "subagents"
            if not sub_dir.is_dir():
                continue
            for f in sub_dir.iterdir():
                if not f.name.startswith("agent-") or not f.name.endswith(".jsonl"):
                    continue
                agent_id = f.name[6:-6]  # strip "agent-" and ".jsonl"
                if agent_id in seen:
                    continue  # audit dir already has it
                # Read meta.json sibling
                meta_path = f.with_suffix("").with_suffix(".meta.json")
                agent_type = ""
                description = ""
                if meta_path.exists():
                    try:
                        with open(meta_path) as mf:
                            mj = json.load(mf) or {}
                        agent_type = mj.get("agentType", "")
                        description = mj.get("description", "")
                    except Exception:
                        pass
                seen[agent_id] = {
                    "id": f"agent-{agent_id}",
                    "agent_type": agent_type,
                    "description": description,
                    "source": "transcript",
                }

        self._json(list(seen.values()))

    def _serve_meta(self, session_id: str):
        session_dir = resolve_session(unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return
        meta = _load_meta(session_dir, session_id)
        summary = _load_summary(session_dir)

        # Fix buggy old summary.json usage: the old _parse_transcript only
        # stored the LAST assistant message's usage (not accumulated).
        # Detect this by checking if output_tokens is suspiciously low.
        # We correct IN MEMORY only (never write back to summary.json —
        # audit data is immutable). The corrected values are returned to
        # the client for display but the on-disk file stays untouched.
        if summary and session_id:
            usage = summary.get("usage", {})
            turns = summary.get("num_turns", 0)
            out_tok = usage.get("output_tokens", 0)
            if turns > 2 and 0 < out_tok < 1000:
                try:
                    t_data = read_transcript(session_id)
                    if t_data and t_data.get("events"):
                        cum = t_data.get("usage", {})
                        t_model = t_data.get("model", "")
                        fixed_usage = _build_usage(cum)
                        if fixed_usage.get("output_tokens", 0) > out_tok:
                            summary = dict(summary)
                            summary["usage"] = fixed_usage
                            if t_model:
                                summary["model"] = t_model
                except Exception:
                    pass

        # Compute cost + context pressure on the fly so pricing/window
        # updates propagate to historical sessions without rewriting
        # summary.json.
        if summary:
            summary = dict(summary)
            model = summary.get("model", "") or meta.get("model", "")
            summary["total_cost_usd"] = compute_cost(model, summary.get("usage", {}))
            summary.update(compute_ctx(model, summary.get("ctx_peak_tokens", 0)))
        # death.json: written by watchdog when claude is killed (SIGKILL/OOM)
        death = None
        death_path = session_dir / "death.json"
        if death_path.exists():
            try:
                with open(death_path) as f:
                    death = json.load(f)
            except (OSError, json.JSONDecodeError):
                pass
        self._json({"metadata": meta, "summary": summary, "death": death})

    def _serve_memory_file(self):
        """GET /api/memory/file?path=<url-encoded abs path>

        Reads a single memory file and returns it as plain text. The
        path must pass is_memory_path_allowed; anything else returns 404.
        Size is capped at 2 MiB to keep the endpoint bounded — any real
        CLAUDE.md or auto-memory file is tiny, so this caps a misuse.
        """
        from urllib.parse import parse_qs
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        qs = parse_qs(query)
        raw = (qs.get("path") or [""])[0]
        if not raw:
            self.send_response(400)
            self.end_headers()
            return
        target = Path(raw).expanduser()
        if not is_memory_path_allowed(target):
            self.send_response(404)
            self.end_headers()
            return
        try:
            size = target.stat().st_size
            if size > 2 * 1024 * 1024:
                self.send_response(413)
                self.end_headers()
                return
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_skill_file(self):
        """GET /api/skills/file?name=<skill>&path=<rel path>"""
        from urllib.parse import parse_qs
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        qs = parse_qs(query)
        name = (qs.get("name") or [""])[0]
        rel = (qs.get("path") or [""])[0]
        target = resolve_skill_file(name, rel)
        if target is None:
            self.send_response(404)
            self.end_headers()
            return
        try:
            size = target.stat().st_size
            if size > 2 * 1024 * 1024:
                self.send_response(413)
                self.end_headers()
                return
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self.send_response(500)
            self.end_headers()
            return
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _delete_session(self, session_id: str):
        """Delete a session directory. Refuses only if the session is
        genuinely live (audit.jsonl was touched within the last
        ACTIVE_WINDOW_S seconds and hasn't emitted SessionEnd yet).
        Stuck directories from crashed sessions and sub-agent leftovers
        are freely deletable. ?force=1 overrides even a live session.
        """
        session_dir = resolve_session(unquote(session_id))
        if not session_dir:
            self.send_response(404)
            self.end_headers()
            return

        force = "force=1" in (self.path.split("?", 1)[1] if "?" in self.path else "")
        if _is_session_active(session_dir) and not force:
            body = json.dumps({
                "error": "session is currently being written to",
                "hint": f"wait {ACTIVE_WINDOW_S}s for it to go idle, or retry with ?force=1",
            }).encode("utf-8")
            self.send_response(409)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Cascade: also remove every sub-agent dir whose name starts with
        # "<this_dir>__agent__". Under flat layout these are siblings at
        # AUDIT_DIR root, not under a date dir. Covers direct children
        # plus grandchildren since __agent__ appears in every ancestor path.
        siblings_root = session_dir.parent
        cascade_prefix = session_dir.name + SUBAGENT_SEP
        cascaded: list[str] = []
        try:
            for sibling in siblings_root.iterdir():
                if sibling.is_dir() and sibling.name.startswith(cascade_prefix):
                    try:
                        shutil.rmtree(sibling)
                        cascaded.append(sibling.name)
                    except OSError:
                        pass
        except OSError:
            pass

        try:
            shutil.rmtree(session_dir)
        except OSError as e:
            body = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Success. 204 has no body by spec, so cascade info is just logged.
        self.send_response(204)
        self.end_headers()

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
    server = ThreadedHTTPServer(("0.0.0.0", port), AuditHandler)
    print(f"Audit viewer running at http://0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
