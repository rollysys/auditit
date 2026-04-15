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


def _parent_cmdline() -> str:
    """Return the ppid's command line (macOS: `ps -p PPID -o command=`).

    The parent of this hook is the Claude Code process itself. Its argv
    is the cleanest signal we have for "was this invoked as `claude -p`"
    (scripted / headless) vs an interactive session. Empty string on any
    failure. Stays best-effort so a weird platform never breaks the hook.
    """
    import subprocess
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(os.getppid()), "-o", "command="],
            text=True, stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.strip()
    except Exception:
        return ""


def _is_headless(parent_cmd: str) -> bool:
    """True iff the parent Claude invocation carries `-p` / `--print`.

    Matches both the short `-p` flag (as its own argv token) and the
    long form `--print`. Does NOT match `-p=foo` style (Claude Code
    does not accept it). Also does not match `-p` embedded inside a
    longer prompt string passed as a separate argv; ps collapses argv
    into a single space-separated line, so we have to be careful about
    false positives from prompt text containing " -p " substrings. As
    a pragmatic compromise we only check the FIRST few tokens after
    the program name — the flag always comes before the prompt.
    """
    if not parent_cmd:
        return False
    parts = parent_cmd.split()
    # Look at everything up to (but not including) the first argv that
    # starts with something that smells like a prompt — heuristically,
    # anything over 40 chars without a leading dash is probably prompt.
    for tok in parts[1:]:
        if not tok.startswith("-") and len(tok) > 40:
            break
        if tok == "-p" or tok == "--print" or tok.startswith("--print="):
            return True
    return False


def _write_env_file(session_dir: Path) -> None:
    """Capture provider + mode signals on first event for this session.

    ANTHROPIC_BASE_URL is the authoritative signal for third-party proxies
    (moonshot / zhipu / qwen / deepseek / ...). Bedrock and Vertex use their
    own boolean flags. These live only in the process environment, never in
    the transcript, so hook time is the only chance to capture them.

    parent_cmd is the argv of the Claude Code process that spawned us —
    used to classify the session as interactive vs scripted (headless).
    claude_code_entrypoint distinguishes SDK vs plain CLI but does not
    by itself tell us interactive vs headless; parent_cmd does.
    """
    env_path = session_dir / "env.json"
    if env_path.exists():
        return
    parent_cmd = _parent_cmdline()
    data = {
        "anthropic_base_url":       os.environ.get("ANTHROPIC_BASE_URL", ""),
        "use_bedrock":              os.environ.get("CLAUDE_CODE_USE_BEDROCK", ""),
        "use_vertex":               os.environ.get("CLAUDE_CODE_USE_VERTEX", ""),
        "claude_code_entrypoint":   os.environ.get("CLAUDE_CODE_ENTRYPOINT", ""),
        "parent_cmd":               parent_cmd,
        "is_headless":              _is_headless(parent_cmd),
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

    Resume case: when the .gz already exists (a previous SessionEnd left
    one behind, then the user resumed via `claude --resume <sid>`), we
    decompress the existing .gz and prepend it to the current jsonl bytes
    so the resulting .gz contains the full session history. Server-side
    read code also tolerates the (.gz + .jsonl) pair until the next
    SessionEnd merges them.

    Returns True iff the .gz was successfully written.
    """
    if not jsonl_path.exists() or jsonl_path.stat().st_size == 0:
        return False
    gz = jsonl_path.with_suffix(jsonl_path.suffix + ".gz")
    gz_tmp = gz.with_suffix(gz.suffix + ".tmp")
    try:
        new_bytes = jsonl_path.read_bytes()
        if gz.exists():
            # Resume merge: existing .gz already holds the older history.
            try:
                with gzip.open(gz, "rb") as f:
                    old_bytes = f.read()
            except OSError:
                old_bytes = b""
            # Ensure the boundary has a newline so a chunk written without
            # a trailing newline does not glue two records together.
            if old_bytes and not old_bytes.endswith(b"\n"):
                old_bytes += b"\n"
            src_bytes = old_bytes + new_bytes
        else:
            src_bytes = new_bytes
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
# Empirical reality of Claude Code hooks (verified 2026-04-16):
#   - `SubagentStart` never fires in practice (0 of >40 observed sub-agents
#     across two live sessions produced a Start hook event).
#   - `SubagentStop` fires in two shapes:
#       A. Explicit spawn (Task / Agent tool) — agent_type non-empty
#          ("Explore" / "general-purpose" / etc.). Claude Code persists
#          the full sub-agent transcript at
#            ~/.claude/projects/<encoded>/<parent_sid>/subagents/agent-<id>.jsonl
#          with a sibling agent-<id>.meta.json of
#            {"agentType": "...", "description": "..."}.
#       B. Internal state-summary agent — agent_type is the empty string,
#          no transcript is persisted, and last_assistant_message is a
#          "Goal / Current / Next" conversation snapshot. These are NOT
#          real sub-agent sessions; they are Claude Code's own turn-end
#          summariser. We leave them in the parent audit stream to be
#          rendered inline as "📸 CHECKPOINT" rows by the web UI.
#
# So: we slice (type A) only. For each SubagentStop with non-empty
# agent_type, we read the transcript + meta.json and synthesise a
# sub-agent dir <siblings_root>/<parent_sid>__agent__<agent_id>/ with
# a converted audit.jsonl.gz + meta.json + summary.json. Type B events
# are left alone.
def _transcript_to_events(transcript_path: Path, parent_sid: str) -> list[dict]:
    """Convert a Claude Code sub-agent transcript into our hook-event format.

    Transcript lines look like:
      {type: "user" | "assistant", isSidechain: true, timestamp: "...",
       message: { role, content: str | [{type: "text"|"tool_use"|"tool_result", ...}] } }

    Translation rules:
      user + content string               → UserPromptSubmit
      user + tool_result block            → PostToolUse (tool_name resolved
                                             via earlier tool_use_id map)
      assistant + tool_use block          → PreToolUse
      assistant + text block              → AssistantMessage (synthetic)
    """
    events: list[dict] = []
    tool_name_by_id: dict[str, str] = {}
    if not transcript_path.exists():
        return events
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = obj.get("timestamp", "") or ""
                # Normalise to our YYYY-MM-DDTHH:MM:SSZ format; transcripts
                # carry ms + optional offset. If parsing fails, keep the
                # original value so ordering is stable.
                parsed = _parse_ts(ts_raw)
                ts = parsed.strftime("%Y-%m-%dT%H:%M:%SZ") if parsed else ts_raw
                t = obj.get("type", "")
                msg = obj.get("message", {}) if isinstance(obj.get("message"), dict) else {}
                content = msg.get("content", "")

                if t == "user":
                    if isinstance(content, str) and content:
                        events.append({
                            "ts": ts, "event": "UserPromptSubmit",
                            "data": {"session_id": parent_sid, "prompt": content},
                        })
                    elif isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            if b.get("type") == "tool_result":
                                tool_use_id = b.get("tool_use_id", "") or ""
                                resp = b.get("content", "")
                                events.append({
                                    "ts": ts, "event": "PostToolUse",
                                    "data": {
                                        "session_id":   parent_sid,
                                        "tool_name":    tool_name_by_id.get(tool_use_id, ""),
                                        "tool_use_id":  tool_use_id,
                                        "tool_response": resp,
                                    },
                                })
                elif t == "assistant":
                    if isinstance(content, list):
                        for b in content:
                            if not isinstance(b, dict):
                                continue
                            bt = b.get("type")
                            if bt == "tool_use":
                                tu_id = b.get("id", "") or ""
                                tu_name = b.get("name", "") or ""
                                tool_name_by_id[tu_id] = tu_name
                                events.append({
                                    "ts": ts, "event": "PreToolUse",
                                    "data": {
                                        "session_id":   parent_sid,
                                        "tool_name":    tu_name,
                                        "tool_input":   b.get("input", {}),
                                        "tool_use_id":  tu_id,
                                    },
                                })
                            elif bt == "text":
                                events.append({
                                    "ts": ts, "event": "AssistantMessage",
                                    "data": {
                                        "session_id": parent_sid,
                                        "text":       b.get("text", "") or "",
                                    },
                                })
    except OSError:
        pass
    return events


def _write_subagent_dir(parent_dir: Path, stop_event: dict) -> None:
    """Materialise a sub-agent dir from a SubagentStop event + transcript.

    Only called for explicit (type A) sub-agents, i.e. agent_type non-empty.
    """
    data = stop_event.get("data", {}) if isinstance(stop_event.get("data"), dict) else {}
    agent_id = data.get("agent_id", "") or ""
    agent_type = data.get("agent_type", "") or ""
    if not agent_id or not agent_type:
        return
    transcript_path = Path(data.get("agent_transcript_path", "") or "")
    parent_sid = parent_dir.name
    siblings_root = parent_dir.parent
    layer_name = parent_sid + "__agent__" + agent_id
    layer_dir = siblings_root / layer_name

    # Neighbouring meta.json (Claude Code's, carries agentType + description).
    description = ""
    if transcript_path:
        meta_sibling = transcript_path.with_suffix("").with_suffix(".meta.json")
        if meta_sibling.exists():
            try:
                with open(meta_sibling) as f:
                    mj = json.load(f) or {}
                description = mj.get("description", "") or ""
                if not agent_type:
                    agent_type = mj.get("agentType", "") or ""
            except (OSError, json.JSONDecodeError):
                pass

    events = _transcript_to_events(transcript_path, parent_sid) if transcript_path else []
    # Always append the SubagentStop event itself so the layer has a clear
    # terminator, and so last_assistant_message is visible inline.
    events.append(stop_event)

    try:
        layer_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return

    # Write audit.jsonl then gzip atomically (line-count verified).
    jsonl_path = layer_dir / "audit.jsonl"
    try:
        with open(jsonl_path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        return

    # Derive summary fields from the translated events.
    num_tool_calls = sum(1 for e in events if e.get("event") == "PreToolUse")
    first_ts = events[0].get("ts", "") if events else ""
    last_ts  = events[-1].get("ts", "") if events else ""
    duration_ms = 0
    t0 = _parse_ts(first_ts)
    t1 = _parse_ts(last_ts)
    if t0 and t1:
        duration_ms = int((t1 - t0).total_seconds() * 1000)

    meta_obj = {
        "is_subagent":        True,
        "parent_session_id":  parent_sid,
        "root_session_id":    parent_sid,
        "agent_id":           agent_id,
        "agent_type":         agent_type,
        "description":        description[:500],
        "start_ts":           first_ts,
        "source":             "transcript",
    }
    try:
        with open(layer_dir / "meta.json", "w") as f:
            json.dump(meta_obj, f, indent=2, ensure_ascii=False)
    except OSError:
        pass

    sub_summary = {
        "is_subagent":       True,
        "reason":            "normal",
        "parent_session_id": parent_sid,
        "agent_type":        agent_type,
        "agent_id":          agent_id,
        "description":       description[:500],
        "num_tool_calls":    num_tool_calls,
        "num_turns":         sum(1 for e in events if e.get("event") == "UserPromptSubmit"),
        "duration_ms":       duration_ms,
    }
    try:
        with open(layer_dir / "summary.json", "w") as f:
            json.dump(sub_summary, f, indent=2, ensure_ascii=False)
    except OSError:
        pass

    _atomic_gzip(jsonl_path)


def _slice_subagents(parent_dir: Path) -> None:
    """For each type-A SubagentStop in the parent audit, materialise a
    sub-agent dir from its transcript. Type-B (empty agent_type) events
    are skipped; they remain inline in the parent stream."""
    jsonl = parent_dir / "audit.jsonl"
    if not jsonl.exists() or jsonl.stat().st_size == 0:
        return
    try:
        src_bytes = jsonl.read_bytes()
    except OSError:
        return
    for raw_line in src_bytes.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if obj.get("event") != "SubagentStop":
            continue
        data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else {}
        if not (data.get("agent_type") or "").strip():
            continue  # type B: state-summary checkpoint, not a real sub-agent
        try:
            _write_subagent_dir(parent_dir, obj)
        except Exception:
            # Defensive — never block the parent SessionEnd gzip on a
            # single broken sub-agent slice.
            continue


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

    # Flat layout: ~/.claude-audit/<sid>/ — date is intentionally NOT in the
    # path. Older releases used ~/.claude-audit/YYYY-MM-DD/<sid>/, which made
    # long-running sessions fragment into multiple directories (one per UTC
    # day they were active). The frontend can still group/sort by start or
    # last-activity timestamps when needed; those live in the events themselves
    # and the audit.jsonl mtime.
    session_dir = AUDIT_DIR / session_id
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
