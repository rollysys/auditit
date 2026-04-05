"""Shared utilities for auditit."""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Derive local timezone from the OS, not hardcoded
TZ_LOCAL = timezone(timedelta(seconds=-_time.timezone if _time.daylight == 0 else -_time.altzone))

# Model context window sizes (tokens)
MODEL_CTX: dict[str, int] = {
    "claude-opus-4-6":            1_000_000,  # Claude Code uses 1M variant
    "claude-sonnet-4-6":            200_000,
    "claude-haiku-4-5":             200_000,
    "claude-haiku-4-5-20251001":    200_000,
    "claude-3-5-sonnet-20241022":   200_000,
    "claude-3-5-haiku-20241022":    200_000,
    "claude-3-opus-20240229":       200_000,
    "claude-3-sonnet-20240229":     200_000,
    "claude-3-haiku-20240307":      200_000,
}

# All Claude Code hook event names
HOOK_EVENTS = [
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Stop",
    "SubagentStart",
    "SubagentStop",
    "Notification",
    "TaskCompleted",
]


def ctx_window(model: str) -> int:
    """Return context window size for a model, defaulting to 200k."""
    for key, size in MODEL_CTX.items():
        if key in model or model in key:
            return size
    return 200_000


def now_local() -> datetime:
    return datetime.now(TZ_LOCAL)


# Aliases expected by render_events.py (borrowed from audit-workbench)
def local_now_str() -> str:
    return now_local().strftime("%H:%M:%S")


def utc_to_local(ts: str) -> str:
    return ts_to_local(ts)


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def ts_to_local(ts: str) -> str:
    """Convert ISO-8601/Z UTC timestamp to HH:MM:SS in local tz."""
    if not ts:
        return now_local().strftime("%H:%M:%S")
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone(TZ_LOCAL).strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return ts[:8] if len(ts) >= 8 else ts


def now_ts() -> str:
    return now_local().strftime("%H:%M:%S")


def session_id() -> str:
    return now_local().strftime("session-%Y%m%d-%H%M%S")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def truncate(s: str, n: int = 60) -> str:
    s = s.replace("\n", " ").strip()
    return s[:n] + "…" if len(s) > n else s


def summarize_tool_input(tool: str, inp: dict | str) -> str:
    """One-line summary of a tool call's input."""
    if not isinstance(inp, dict):
        return truncate(str(inp), 60)
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return inp.get("file_path", inp.get("notebook_path", ""))
    if tool in ("Grep",):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"{pattern!r}" + (f" in {path}" if path else "")
    if tool in ("Glob",):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        return f"{pattern}" + (f" in {path}" if path else "")
    if tool == "Bash":
        cmd = inp.get("command", "")
        return truncate(cmd, 70)
    if tool in ("Agent", "Task"):
        desc = inp.get("description", inp.get("prompt", ""))
        return truncate(desc, 60)
    if tool in ("WebSearch",):
        return truncate(inp.get("query", ""), 60)
    if tool in ("WebFetch",):
        return truncate(inp.get("url", ""), 60)
    # Fallback: first non-empty string value
    for v in inp.values():
        if isinstance(v, str) and v:
            return truncate(v, 60)
    return ""


def read_transcript_meta(transcript_path: str) -> dict:
    """Extract model and latest usage from a Claude Code transcript file.

    Returns dict with keys: model, input_tokens, output_tokens,
    cache_read_input_tokens, cache_creation_input_tokens.
    """
    path = Path(transcript_path)
    if not path.exists():
        return {}
    model = ""
    usage: dict = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "assistant":
                    msg = obj.get("message", {})
                    if msg.get("model"):
                        model = msg["model"]
                    u = msg.get("usage", {})
                    if u:
                        usage = u
    except OSError:
        pass
    return {
        "model": model,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
    }


def summarize_tool_response(tool: str, resp) -> str:
    """Brief summary of a tool call's response."""
    if resp is None:
        return ""
    if isinstance(resp, list):
        # Array of content blocks
        texts = []
        for block in resp:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text", ""))
        resp = "\n".join(texts)
    if not isinstance(resp, str):
        resp = json.dumps(resp, ensure_ascii=False)
    lines = resp.splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    n = len(lines)
    return first + (f" ({n} lines)" if n > 1 else "")
