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
import shutil
import subprocess
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from urllib.parse import unquote

AUDIT_DIR = Path.home() / ".claude-audit"
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


def list_sessions() -> dict:
    """Return {date: [session_entry, ...]}.

    Each entry carries is_subagent / parent_session_id / depth fields so
    the web UI can render sub-agents indented under their parent.

    Top-level parent sessions do not contain `__agent__` in their dir
    name; sub-agent directories are named
    `<immediate_parent>__agent__<agent_id>` (recursive, so a
    sub-sub-agent is `<p>__agent__<c>__agent__<gc>`).
    """
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
            summary = _load_summary(session_dir)
            count = _count_events(session_dir)
            status = "active" if _is_session_active(session_dir) else "completed"

            sub_meta = _load_subagent_meta(session_dir)
            is_subagent = sub_meta is not None or SUBAGENT_SEP in sid

            if is_subagent:
                # Sub-agent entry. The "immediate parent" is the dir name
                # preceding the last __agent__ token (so a grandchild's
                # parent is the child dir, not the root session).
                parent_name = sid.rsplit(SUBAGENT_SEP, 1)[0] if SUBAGENT_SEP in sid else ""
                agent_id   = (sub_meta or {}).get("agent_id", "") or sid.rsplit(SUBAGENT_SEP, 1)[-1]
                agent_type = (sub_meta or {}).get("agent_type", "")
                description = (sub_meta or {}).get("description", "")
                start_ts   = (sub_meta or {}).get("start_ts", "") or summary.get("start_ts", "")
                depth      = sid.count(SUBAGENT_SEP)
                # Try to inherit cwd from the root parent for display.
                root_name = sid.split(SUBAGENT_SEP, 1)[0]
                root_meta = _load_meta(date_dir / root_name, root_name) if (date_dir / root_name).is_dir() else {}
                date_sessions.append({
                    "id": sid,
                    "date": date_dir.name,
                    "prompt": description,
                    "model": "",       # sub-agents share parent's model; leave blank
                    "cwd": root_meta.get("cwd", ""),
                    "count": count,
                    "turns": summary.get("num_tool_calls", 0),
                    # Sub-agent cost is not independently computable (usage
                    # lives in the parent's transcript). Return None so the
                    # UI shows "—" instead of $0.0000.
                    "cost": None,
                    "duration_ms": summary.get("duration_ms", 0),
                    "status": status,
                    "started_at": start_ts,
                    "is_subagent": True,
                    "parent_session_id": parent_name,
                    "root_session_id": root_name,
                    "agent_id": agent_id,
                    "agent_type": agent_type,
                    "depth": depth,
                    # reason="unclosed" means hook.sh never saw a matching
                    # SubagentStop for this layer — Claude Code sometimes
                    # skips it. UI flags these with a ⚠ so they stand out.
                    "reason": summary.get("reason", ""),
                })
            else:
                meta = _load_meta(session_dir, sid)
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
                    "is_subagent": False,
                    "parent_session_id": "",
                    "root_session_id": sid,
                    "depth": 0,
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


def detect_provider(model: str) -> str:
    """Infer the API provider from the model name string.

    Pure heuristic — we do not capture ANTHROPIC_BASE_URL or
    CLAUDE_CODE_USE_BEDROCK in summary.json today, so the model string is
    all we have. Good enough when provider correlates with model family.
    """
    if not model:
        return "unknown"
    m = model.lower().strip()
    if m == "<synthetic>":
        return "synthetic"
    # Bedrock: "anthropic.claude-..." or "us.anthropic.claude-..."
    if m.startswith("anthropic.") or ".anthropic." in m:
        return "bedrock"
    # Vertex AI: "claude-opus-4-1@20250805" style
    if "@" in m and "claude-" in m:
        return "vertex"
    if m.startswith("claude-"):
        return "anthropic"
    # Common third-party OpenAI-compatible endpoints (via ANTHROPIC_BASE_URL override)
    if m.startswith("qwen"):     return "qwen"
    if m.startswith("glm"):      return "zhipu"
    if m.startswith("kimi") or m.startswith("moonshot"): return "moonshot"
    if m.startswith("deepseek"): return "deepseek"
    if m.startswith("gpt-") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4"):
        return "openai"
    if m.startswith("gemini"):   return "gemini"
    if m.startswith("grok"):     return "xai"
    return "other"


def build_stats() -> dict:
    """Aggregate across all main sessions for the Dashboard view.

    Sub-agent directories are skipped — their token/cost usage is already
    captured in the parent session's transcript, so counting them would
    double-count.

    Returns:
      totals: totals across all sessions (session count, cost, tokens,
              duration). Tokens are split into input / output / cache_read /
              cache_creation (5m+1h combined).
      by_model: per-model aggregates (same fields as totals minus duration).
      by_date: per-day aggregates (sorted ascending), for trend chart.
      top_by_cost: top 20 sessions by cost.
      top_by_ctx: top 20 sessions by ctx_peak_pct.
    """
    totals = {
        "sessions": 0, "cost": 0.0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "duration_ms": 0, "turns": 0,
    }
    by_model: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    by_date: dict[str, dict] = {}
    all_sessions: list[dict] = []

    if not AUDIT_DIR.exists():
        return {"totals": totals, "by_model": [], "by_provider": [], "by_date": [],
                "top_by_cost": [], "top_by_ctx": []}

    for date_dir in sorted(AUDIT_DIR.iterdir()):
        if not date_dir.is_dir() or not date_dir.name.startswith("20"):
            continue
        for session_dir in sorted(date_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            sid = session_dir.name
            # Skip sub-agent directories — their usage is already in the parent.
            if SUBAGENT_SEP in sid:
                continue
            summary = _load_summary(session_dir)
            if not summary:
                continue

            usage = summary.get("usage", {}) or {}
            inp = int(usage.get("input_tokens", 0) or 0)
            out = int(usage.get("output_tokens", 0) or 0)
            cr  = int(usage.get("cache_read_input_tokens", 0) or 0)
            cw  = int(usage.get("cache_creation_input_tokens", 0) or 0)
            if not cw:
                cw = (int(usage.get("cache_creation_5m_tokens", 0) or 0)
                      + int(usage.get("cache_creation_1h_tokens", 0) or 0))

            meta = _load_meta(session_dir, sid)
            model = summary.get("model", "") or meta.get("model", "") or "unknown"
            provider = detect_provider(model)
            cost = compute_cost(model, usage)
            dur  = int(summary.get("duration_ms", 0) or 0)
            turns = int(summary.get("num_turns", 0) or 0)
            # calls/hr normalised from turns over duration. Clamp tiny durations
            # to 0 to avoid extreme outliers from 0/near-0 ms sessions.
            calls_per_hr = (turns * 3_600_000.0 / dur) if dur >= 1000 else 0.0
            ctx = compute_ctx(model, summary.get("ctx_peak_tokens", 0))

            totals["sessions"] += 1
            totals["cost"] += cost
            totals["input_tokens"] += inp
            totals["output_tokens"] += out
            totals["cache_read_tokens"] += cr
            totals["cache_creation_tokens"] += cw
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
            bm["input_tokens"] += inp
            bm["output_tokens"] += out
            bm["cache_read_tokens"] += cr
            bm["cache_creation_tokens"] += cw
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
            bp["input_tokens"] += inp
            bp["output_tokens"] += out
            bp["cache_read_tokens"] += cr
            bp["cache_creation_tokens"] += cw
            bp["turns"] += turns
            bp["duration_ms"] += dur

            bd = by_date.setdefault(date_dir.name, {
                "date": date_dir.name, "sessions": 0, "cost": 0.0,
                "input_tokens": 0, "output_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "turns": 0, "duration_ms": 0,
            })
            bd["sessions"] += 1
            bd["cost"] += cost
            bd["input_tokens"] += inp
            bd["output_tokens"] += out
            bd["cache_read_tokens"] += cr
            bd["cache_creation_tokens"] += cw
            bd["turns"] += turns
            bd["duration_ms"] += dur

            all_sessions.append({
                "id": sid,
                "date": date_dir.name,
                "model": model,
                "provider": provider,
                "cost": round(cost, 4),
                "turns": turns,
                "duration_ms": dur,
                "calls_per_hr": round(calls_per_hr, 1),
                "prompt": meta.get("prompt", "")[:200],
                "cwd": meta.get("cwd", ""),
                "ctx_peak_pct": ctx["ctx_peak_pct"],
                "ctx_peak_tokens": ctx["ctx_peak_tokens"],
                "ctx_window": ctx["ctx_window"],
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

    by_model_list    = sorted(by_model.values(),    key=lambda x: -x["cost"])
    by_provider_list = sorted(by_provider.values(), key=lambda x: -x["cost"])
    by_date_list     = sorted(by_date.values(),     key=lambda x: x["date"])

    top_by_cost = sorted(all_sessions, key=lambda x: -x["cost"])[:20]
    top_by_ctx  = sorted(all_sessions, key=lambda x: -x["ctx_peak_pct"])[:20]

    return {
        "totals": totals,
        "by_model": by_model_list,
        "by_provider": by_provider_list,
        "by_date": by_date_list,
        "top_by_cost": top_by_cost,
        "top_by_ctx": top_by_ctx,
    }


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
        elif path == "/api/memory":
            self._json(build_memory_index())
        elif path == "/api/memory/file":
            self._serve_memory_file()
        elif path == "/api/version":
            self._json(REPO_VERSION)
        elif path == "/api/stats":
            self._json(build_stats())
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
            # /api/sessions/<date>/<session-id> → 5 parts
            if len(parts) == 5:
                self._delete_session(parts[3], parts[4])
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
        # Compute cost + context pressure on the fly so pricing/window
        # updates propagate to historical sessions without rewriting
        # summary.json.
        if summary:
            summary = dict(summary)
            model = summary.get("model", "") or meta.get("model", "")
            summary["total_cost_usd"] = compute_cost(model, summary.get("usage", {}))
            summary.update(compute_ctx(model, summary.get("ctx_peak_tokens", 0)))
        self._json({"metadata": meta, "summary": summary})

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

    def _delete_session(self, date: str, session_id: str):
        """Delete a session directory. Refuses only if the session is
        genuinely live (audit.jsonl was touched within the last
        ACTIVE_WINDOW_S seconds and hasn't emitted SessionEnd yet).
        Stuck directories from crashed sessions and sub-agent leftovers
        are freely deletable. ?force=1 overrides even a live session.
        """
        session_dir = resolve_session(date, unquote(session_id))
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
        # "<this_dir>__agent__". That covers direct children as well as
        # grandchildren because __agent__ appears in every ancestor path.
        # Note: this also cleans up if we're deleting a sub-agent itself —
        # its own descendants (sub-sub-agents) share the same prefix.
        date_dir = session_dir.parent
        cascade_prefix = session_dir.name + SUBAGENT_SEP
        cascaded: list[str] = []
        try:
            for sibling in date_dir.iterdir():
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
