#!/usr/bin/env python3
"""hook.py — Global audit hook for Claude Code.

Called by Claude Code for every registered hook event.
Usage: hook.py <EventName>
Stdin: full event JSON from Claude Code.

Writes to ~/.claude-audit/YYYY-MM-DD/<sessionId>/audit.jsonl
On SessionEnd: parses transcript for usage/model/turns/duration, writes
summary.json, slices sub-agent layers, then atomically gzips audit.jsonl
(tmpfile + line-count verify).

Failure-safe: every code path is wrapped in a top-level except that exits
0. A buggy hook must NEVER block Claude — at worst we lose one audit row.
"""

from __future__ import annotations

import gzip
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

AUDIT_DIR = Path.home() / ".claude-audit"


def _parse_ts(s):
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


def _write_env_file(session_dir: Path) -> None:
    """Capture provider-relevant env on first event for this session.

    ANTHROPIC_BASE_URL is the authoritative signal for third-party proxies
    (moonshot / zhipu / qwen / deepseek / ...). Bedrock and Vertex use their
    own boolean flags. These live only in the process environment, never in
    the transcript, so hook time is the only chance to capture them.
    """
    env_path = session_dir / "env.json"
    if env_path.exists():
        return
    data = {
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", ""),
        "use_bedrock":        os.environ.get("CLAUDE_CODE_USE_BEDROCK", ""),
        "use_vertex":         os.environ.get("CLAUDE_CODE_USE_VERTEX", ""),
    }
    try:
        with open(env_path, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _parse_transcript(transcript_path: str):
    """Walk transcript jsonl and extract model / turns / usage / duration /
    ctx_peak. Skips Claude Code's client-side "<synthetic>" assistant
    messages so the real underlying model is recorded.
    """
    model = ""
    raw_usage: dict = {}
    num_turns = 0
    first_ts = None
    last_ts = None
    ctx_peak_tokens = 0

    if not transcript_path or not os.path.exists(transcript_path):
        return model, raw_usage, num_turns, first_ts, last_ts, ctx_peak_tokens

    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as tf:
            for line in tf:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(obj.get("timestamp", ""))
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts
                t = obj.get("type", "")
                if t == "user":
                    num_turns += 1
                elif t == "assistant":
                    msg = obj.get("message", {})
                    if not isinstance(msg, dict):
                        continue
                    m = msg.get("model")
                    # Skip Claude Code's client-side synthetic error messages
                    # ("model not found / no access" etc.) entirely — they
                    # carry an all-zero usage block that would overwrite the
                    # real cumulative usage.
                    if m == "<synthetic>":
                        continue
                    if m:
                        model = m
                    u = msg.get("usage")
                    if isinstance(u, dict):
                        raw_usage = u
                        in_now = (
                            (u.get("input_tokens", 0) or 0)
                            + (u.get("cache_read_input_tokens", 0) or 0)
                            + (u.get("cache_creation_input_tokens", 0) or 0)
                        )
                        if in_now > ctx_peak_tokens:
                            ctx_peak_tokens = in_now
    except OSError:
        pass

    return model, raw_usage, num_turns, first_ts, last_ts, ctx_peak_tokens


def _build_usage(raw_usage: dict) -> dict:
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


def _atomic_gzip(jsonl_path: Path) -> bool:
    """Compress jsonl_path → jsonl_path + '.gz' using tmpfile + line-count
    verify + os.replace. On any failure leave the original untouched.
    Returns True iff the original was successfully replaced.
    """
    if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
        return False
    gz = jsonl_path.with_suffix(jsonl_path.suffix + ".gz")
    gz_tmp = gz.with_suffix(gz.suffix + ".tmp")
    try:
        src_bytes = jsonl_path.read_bytes()
        src_lines = src_bytes.count(b"\n")
        with gzip.open(gz_tmp, "wb", compresslevel=6) as fout:
            fout.write(src_bytes)
        with gzip.open(gz_tmp, "rt") as f:
            gz_lines = sum(1 for _ in f)
    except Exception:
        try:
            gz_tmp.unlink()
        except OSError:
            pass
        return False
    if gz_lines != src_lines:
        try:
            gz_tmp.unlink()
        except OSError:
            pass
        return False
    try:
        os.replace(gz_tmp, gz)
        jsonl_path.unlink()
        return True
    except OSError:
        return False


# ===== Sub-agent slicing =====
#
# Walk the parent audit.jsonl; every SubagentStart opens a new sub-agent
# directory at <date>/<immediate_parent_name>__agent__<agent_id>/. Every
# event between a matching Start/Stop is appended to the new layer AND to
# every ancestor layer still open on the stack. On Stop, the layer is
# finalized: meta.json + summary.json + atomic gzip + delete plain jsonl.
# Any layers left unclosed at EOF (crashed sub-agent) are still finalized
# with reason="unclosed".
#
# The parent audit.jsonl itself is never touched here — it remains the
# source of truth and is compressed by _atomic_gzip afterwards.
def _slice_subagents(parent_dir: Path) -> None:
    jsonl = parent_dir / "audit.jsonl"
    if not jsonl.exists() or jsonl.stat().st_size == 0:
        return
    try:
        src_bytes = jsonl.read_bytes()
    except OSError:
        return

    parent_dir_name = parent_dir.name
    date_dir_path = parent_dir.parent
    slicing_stack: list[dict] = []
    last_task_desc = ""

    def _open_layer(agent_id, agent_type, desc, start_ts, immediate_parent_name):
        name = immediate_parent_name + "__agent__" + (agent_id or "unknown")
        dpath = date_dir_path / name
        dpath.mkdir(parents=True, exist_ok=True)
        fh = open(dpath / "audit.jsonl", "ab")
        return {
            "dir":         dpath,
            "name":        name,
            "fh":          fh,
            "agent_id":    agent_id,
            "agent_type":  agent_type,
            "desc":        desc or "",
            "start_ts":    start_ts,
            "immediate_parent_name": immediate_parent_name,
            "tool_count":  0,
            "event_count": 0,
            "first_ts":    None,
            "last_ts":     None,
        }

    def _write_to(entry, raw_line, ts):
        entry["fh"].write(raw_line)
        entry["event_count"] += 1
        if entry["first_ts"] is None:
            entry["first_ts"] = ts
        entry["last_ts"] = ts

    def _close_layer(entry, reason):
        try:
            entry["fh"].flush()
            entry["fh"].close()
        except Exception:
            pass

        meta_obj = {
            "is_subagent":        True,
            "parent_session_id":  entry["immediate_parent_name"],
            "root_session_id":    parent_dir_name,
            "agent_id":           entry["agent_id"],
            "agent_type":         entry["agent_type"],
            "description":        (entry["desc"] or "")[:500],
            "start_ts":           entry["start_ts"],
        }
        try:
            with open(entry["dir"] / "meta.json", "w") as f:
                json.dump(meta_obj, f, indent=2)
        except OSError:
            pass

        duration_ms_sub = 0
        t0 = _parse_ts(entry["first_ts"])
        t1 = _parse_ts(entry["last_ts"])
        if t0 and t1:
            duration_ms_sub = int((t1 - t0).total_seconds() * 1000)
        sub_summary = {
            "is_subagent":       True,
            "reason":            reason,
            "parent_session_id": entry["immediate_parent_name"],
            "agent_type":        entry["agent_type"],
            "agent_id":          entry["agent_id"],
            "description":       (entry["desc"] or "")[:500],
            "num_tool_calls":    entry["tool_count"],
            "num_turns":         0,
            "duration_ms":       duration_ms_sub,
        }
        try:
            with open(entry["dir"] / "summary.json", "w") as f:
                json.dump(sub_summary, f, indent=2)
        except OSError:
            pass

        _atomic_gzip(entry["dir"] / "audit.jsonl")

    for raw_line in src_bytes.splitlines(keepends=True):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        ev  = obj.get("event", "")
        dat = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
        ts  = obj.get("ts", "")

        # Stash Task/Agent description so the next SubagentStart can use it.
        if ev == "PreToolUse" and dat.get("tool_name") in ("Task", "Agent"):
            ti = dat.get("tool_input", {}) if isinstance(dat.get("tool_input"), dict) else {}
            last_task_desc = (ti.get("description") or ti.get("prompt") or "")[:500]
            # Fall through so the event still mirrors into open layers below.

        if ev == "SubagentStart":
            immediate_parent_name = slicing_stack[-1]["name"] if slicing_stack else parent_dir_name
            agent_id   = dat.get("agent_id", "") or ""
            agent_type = dat.get("agent_type", "") or ""
            desc = last_task_desc or dat.get("description") or dat.get("prompt") or ""
            last_task_desc = ""
            try:
                layer = _open_layer(agent_id, agent_type, desc, ts, immediate_parent_name)
            except OSError:
                continue
            for anc in slicing_stack:
                _write_to(anc, raw_line, ts)
            _write_to(layer, raw_line, ts)
            slicing_stack.append(layer)
            continue

        if ev == "SubagentStop":
            for anc in slicing_stack:
                _write_to(anc, raw_line, ts)
            if slicing_stack:
                leaving = slicing_stack.pop()
                _close_layer(leaving, reason="normal")
            continue

        if slicing_stack:
            for anc in slicing_stack:
                _write_to(anc, raw_line, ts)
                if ev in ("PostToolUse", "PostToolUseFailure"):
                    anc["tool_count"] += 1

    while slicing_stack:
        leaving = slicing_stack.pop()
        _close_layer(leaving, reason="unclosed")


def _handle_session_end(session_dir: Path, event_data: dict) -> None:
    transcript_path = event_data.get("transcript_path", "")
    model, raw_usage, num_turns, first_ts, last_ts, ctx_peak = _parse_transcript(transcript_path)

    duration_ms = 0
    if first_ts and last_ts:
        duration_ms = int((last_ts - first_ts).total_seconds() * 1000)

    summary = {
        "reason":          event_data.get("reason", ""),
        "model":           model,
        "num_turns":       num_turns,
        "duration_ms":     duration_ms,
        "usage":           _build_usage(raw_usage),
        "ctx_peak_tokens": ctx_peak,
    }
    try:
        with open(session_dir / "summary.json", "w") as f:
            json.dump(summary, f, indent=2)
    except OSError:
        pass

    _slice_subagents(session_dir)
    _atomic_gzip(session_dir / "audit.jsonl")


def _main():
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    raw = sys.stdin.read()
    # Compact: drop newlines so the original behaviour matches (raw bytes
    # stored verbatim into audit.jsonl as a single-line "data" payload).
    raw = raw.replace("\n", "").replace("\r", "")
    if not raw or not raw.lstrip().startswith("{"):
        return
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    session_id = data.get("session_id", "")
    if not session_id:
        return

    date_dir = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    session_dir = AUDIT_DIR / date_dir / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    _write_env_file(session_dir)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = '{"ts":"' + ts + '","event":"' + event + '","data":' + raw + '}\n'
    try:
        with open(session_dir / "audit.jsonl", "a") as f:
            f.write(line)
    except OSError:
        pass

    if event == "SessionEnd":
        _handle_session_end(session_dir, data)


def _log_safety_net_error(exc: BaseException) -> None:
    """Best-effort log of a swallowed exception to ~/.claude-audit/_hook_errors.log.

    Without this, `except Exception: pass` silently loses every bug forever
    and audit data can quietly degrade for weeks before anyone notices.
    Any failure here is itself swallowed — the safety net must not raise.
    """
    try:
        import traceback
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        log_path = AUDIT_DIR / "_hook_errors.log"
        event = sys.argv[1] if len(sys.argv) > 1 else ""
        with open(log_path, "a") as f:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            f.write(f"{ts} event={event} {type(exc).__name__}: {exc}\n")
            traceback.print_exc(file=f)
            f.write("\n")
    except Exception:
        pass


if __name__ == "__main__":
    # Top-level safety net: any unexpected error must NOT propagate. A
    # non-zero exit from a Claude Code hook can block tool execution
    # globally, so we silently drop bugs at the price of one missed audit.
    # We do log the error to _hook_errors.log so silent failures are at
    # least discoverable after the fact.
    try:
        _main()
    except Exception as exc:
        _log_safety_net_error(exc)
    sys.exit(0)
