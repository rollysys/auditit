#!/usr/bin/env python3
"""Unit tests for the transcript parser in server.py.

Uses synthetic transcript files — no real data, no network, no server.
Tests read_transcript (streaming merge, isMeta skip, system events,
synthetic messages), _compute_session_usage (streaming dedup for cost),
_scan_transcript_header (metadata extraction), and _find_transcript
(path safety).

Run:  python3 tests/test_transcript.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Nuke any stale .pyc
import shutil
try:
    shutil.rmtree(REPO / "__pycache__")
except OSError:
    pass


def assert_eq(name, got, want):
    if got != want:
        raise AssertionError(f"{name}: got {got!r}, want {want!r}")


def assert_ok(name, cond, msg=""):
    if not cond:
        raise AssertionError(f"{name}: {msg}")


def _write_transcript(tmp: Path, sid: str, lines: list[dict]) -> Path:
    """Write a synthetic transcript file under a fake projects dir."""
    proj_dir = tmp / ".claude" / "projects" / "-test-project"
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{sid}.jsonl"
    with open(path, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def _make_user(text: str, ts: str = "2026-04-17T10:00:00.000Z",
               entrypoint: str = "cli", cwd: str = "/test",
               permission_mode: str = "default", is_meta: bool = False) -> dict:
    d = {
        "type": "user",
        "timestamp": ts,
        "entrypoint": entrypoint,
        "cwd": cwd,
        "permissionMode": permission_mode,
        "sessionId": "test-sid",
        "message": {"role": "user", "content": text},
    }
    if is_meta:
        d["isMeta"] = True
    return d


def _make_user_tool_result(tool_use_id: str, content: str,
                           is_error: bool = False,
                           ts: str = "2026-04-17T10:00:02.000Z") -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "entrypoint": "cli",
        "cwd": "/test",
        "sessionId": "test-sid",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id,
             "content": content, "is_error": is_error},
        ]},
        "toolUseResult": {"stdout": content, "stderr": "", "interrupted": False},
    }


def _make_assistant(content_blocks: list[dict], model: str = "claude-opus-4-6",
                    msg_id: str = "msg_001",
                    usage: dict | None = None,
                    ts: str = "2026-04-17T10:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "test-sid",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": model,
            "content": content_blocks,
            "stop_reason": "end_turn",
            "usage": usage or {"input_tokens": 100, "output_tokens": 50,
                               "cache_read_input_tokens": 200,
                               "cache_creation_input_tokens": 0},
        },
    }


def _make_synthetic(text: str, msg_id: str = "msg_syn_001",
                    ts: str = "2026-04-17T10:00:01.000Z") -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "sessionId": "test-sid",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": text}],
        },
    }


def _make_system(subtype: str, content: str = "",
                 ts: str = "2026-04-17T10:00:05.000Z") -> dict:
    return {
        "type": "system",
        "timestamp": ts,
        "subtype": subtype,
        "content": content,
        "level": "warning" if subtype == "api_error" else "info",
        "sessionId": "test-sid",
    }


def _make_queue_enqueue(prompt: str,
                        ts: str = "2026-04-17T09:59:59.000Z") -> dict:
    return {
        "type": "queue-operation",
        "operation": "enqueue",
        "content": prompt,
        "timestamp": ts,
        "sessionId": "test-sid",
    }


def _make_attachment(ts: str = "2026-04-17T10:00:00.500Z") -> dict:
    return {
        "type": "attachment",
        "timestamp": ts,
        "attachment": {"type": "deferred_tools_delta"},
        "message": None,
        "sessionId": "test-sid",
    }


# ─── Test: streaming merge ───────────────────────────────────────────

def test_streaming_merge_combines_content_blocks():
    """A single assistant message streamed as 3 lines (thinking + text +
    tool_use) with the same message.id must be merged into 3 events,
    not just 1."""
    print("test_streaming_merge_combines_content_blocks")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-streaming-merge"
        lines = [
            _make_queue_enqueue("hello"),
            _make_user("hello"),
            # 3 streaming lines, same msg_id, each with 1 content block
            _make_assistant([{"type": "thinking", "thinking": "Let me think..."}],
                            msg_id="msg_stream_1",
                            usage={"input_tokens": 10, "output_tokens": 0,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}),
            _make_assistant([{"type": "text", "text": "Here is the answer."}],
                            msg_id="msg_stream_1",
                            usage={"input_tokens": 10, "output_tokens": 30,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}),
            _make_assistant([{"type": "tool_use", "id": "toolu_abc",
                              "name": "Bash", "input": {"command": "ls"}}],
                            msg_id="msg_stream_1",
                            usage={"input_tokens": 10, "output_tokens": 50,
                                   "cache_read_input_tokens": 200,
                                   "cache_creation_input_tokens": 0}),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        # Should have: user_text + thinking + text + tool_use = 4 events
        types = [e["type"] for e in events]
        assert_ok("has thinking", "assistant_thinking" in types, str(types))
        assert_ok("has text", "assistant_text" in types, str(types))
        assert_ok("has tool_use", "assistant_tool_use" in types, str(types))
        assert_eq("total events", len(events), 4)

        # Usage should be from the LAST streaming line (cumulative)
        tool_use_event = [e for e in events if e["type"] == "assistant_tool_use"][0]
        assert_eq("usage.output_tokens", tool_use_event["usage"].get("output_tokens"), 50)


def test_streaming_merge_different_ids_not_merged():
    """Two assistant messages with different message.id must produce
    separate events."""
    print("test_streaming_merge_different_ids_not_merged")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-different-ids"
        lines = [
            _make_user("q1"),
            _make_assistant([{"type": "text", "text": "answer 1"}],
                            msg_id="msg_A"),
            _make_user_tool_result("toolu_x", "ok"),
            _make_assistant([{"type": "text", "text": "answer 2"}],
                            msg_id="msg_B"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        text_events = [e for e in events if e["type"] == "assistant_text"]
        assert_eq("two separate texts", len(text_events), 2)
        assert_eq("text 1", text_events[0]["text"], "answer 1")
        assert_eq("text 2", text_events[1]["text"], "answer 2")


# ─── Test: synthetic messages ────────────────────────────────────────

def test_synthetic_messages_rendered_not_skipped():
    """Synthetic assistant messages (model=<synthetic>) must appear as
    events — they carry real info like 'Not logged in'. But their usage
    must NOT be accumulated."""
    print("test_synthetic_messages_rendered_not_skipped")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-synthetic"
        lines = [
            _make_user("hello"),
            _make_synthetic("Not logged in", msg_id="syn_1"),
            _make_user("retry"),
            _make_synthetic("Not logged in", msg_id="syn_2"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        text_events = [e for e in events if e["type"] == "assistant_text"]
        assert_eq("both synthetic rendered", len(text_events), 2)
        assert_eq("text content", text_events[0]["text"], "Not logged in")
        assert_eq("model is synthetic", text_events[0]["model"], "<synthetic>")


# ─── Test: isMeta skip ───────────────────────────────────────────────

def test_is_meta_lines_skipped():
    """Lines with isMeta=true should not produce events."""
    print("test_is_meta_lines_skipped")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-ismeta"
        lines = [
            _make_user("real prompt"),
            _make_user("/clear", is_meta=True),
            _make_assistant([{"type": "text", "text": "real answer"}],
                            msg_id="msg_real"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        user_texts = [e for e in events if e["type"] == "user_text"]
        assert_eq("only non-meta user", len(user_texts), 1)
        assert_eq("text is real", user_texts[0]["text"], "real prompt")


# ─── Test: system events ─────────────────────────────────────────────

def test_system_events_parsed():
    """system lines with relevant subtypes should produce system_event."""
    print("test_system_events_parsed")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-system"
        lines = [
            _make_user("go"),
            _make_system("api_error", "Rate limited"),
            _make_system("compact_boundary", "compacted 50k tokens"),
            _make_system("turn_duration", "1200ms"),  # not in our list → skip
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        sys_events = [e for e in events if e["type"] == "system_event"]
        assert_eq("two system events", len(sys_events), 2)
        assert_eq("first is api_error", sys_events[0]["subtype"], "api_error")
        assert_eq("second is compact", sys_events[1]["subtype"], "compact_boundary")


# ─── Test: tool_result linking ───────────────────────────────────────

def test_tool_result_gets_tool_name():
    """tool_result events should carry the tool_name resolved from the
    preceding tool_use's id→name mapping."""
    print("test_tool_result_gets_tool_name")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-tool-link"
        lines = [
            _make_user("do it"),
            _make_assistant([{"type": "tool_use", "id": "toolu_xyz",
                              "name": "Grep", "input": {"pattern": "foo"}}],
                            msg_id="msg_tool"),
            _make_user_tool_result("toolu_xyz", "found in main.py"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        tr = [e for e in events if e["type"] == "tool_result"]
        assert_eq("one tool_result", len(tr), 1)
        assert_eq("tool_name resolved", tr[0]["tool_name"], "Grep")
        assert_eq("tool_use_id", tr[0]["tool_use_id"], "toolu_xyz")


# ─── Test: attachment with message=None ──────────────────────────────

def test_attachment_with_null_message():
    """attachment lines have message=None. Parser must not crash."""
    print("test_attachment_with_null_message")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-attachment"
        lines = [
            _make_user("start"),
            _make_attachment(),
            _make_assistant([{"type": "text", "text": "ok"}], msg_id="msg_a"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        events = result.get("events", [])

        assert_ok("has events", len(events) >= 2, str(len(events)))
        types = [e["type"] for e in events]
        assert_ok("has user_text", "user_text" in types, str(types))
        assert_ok("has assistant_text", "assistant_text" in types, str(types))


# ─── Test: _compute_session_usage streaming dedup ────────────────────

def test_compute_usage_streaming_takes_last():
    """_compute_session_usage must take the LAST streaming line's usage
    per message.id (cumulative), not the first (partial)."""
    print("test_compute_usage_streaming_takes_last")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-usage-stream"
        lines = [
            _make_user("go"),
            # Streaming: 3 lines, same id, usage grows
            _make_assistant([{"type": "thinking", "thinking": "hmm"}],
                            msg_id="msg_u1",
                            usage={"input_tokens": 100, "output_tokens": 0,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}),
            _make_assistant([{"type": "text", "text": "answer"}],
                            msg_id="msg_u1",
                            usage={"input_tokens": 100, "output_tokens": 80,
                                   "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0}),
            _make_assistant([{"type": "tool_use", "id": "t1",
                              "name": "Read", "input": {}}],
                            msg_id="msg_u1",
                            usage={"input_tokens": 100, "output_tokens": 120,
                                   "cache_read_input_tokens": 500,
                                   "cache_creation_input_tokens": 0}),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        # Clear cost cache
        try:
            shutil.rmtree(server.COST_CACHE_DIR)
        except OSError:
            pass

        result = server._compute_session_usage(path, sid)
        # Should use LAST line's usage (output_tokens=120), not first (0)
        assert_eq("output_tokens", result.get("output_tokens"), 120)
        assert_eq("cache_read", result.get("cache_read_input_tokens"), 500)
        assert_eq("num_turns", result.get("num_turns"), 1)


# ─── Test: _scan_transcript_header ───────────────────────────────────

def test_header_scan_extracts_metadata():
    """_scan_transcript_header should extract cwd, entrypoint, prompt,
    model, timestamps, and permissionMode from the first few lines."""
    print("test_header_scan_extracts_metadata")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-header"
        lines = [
            _make_queue_enqueue("queued prompt"),
            _make_user("actual prompt", cwd="/projects/foo",
                       entrypoint="sdk-cli", permission_mode="bypassPermissions",
                       ts="2026-04-17T09:00:00.000Z"),
            _make_assistant([{"type": "text", "text": "hi"}],
                            model="claude-sonnet-4-6", msg_id="msg_h1",
                            ts="2026-04-17T09:00:05.000Z"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        try:
            shutil.rmtree(server.COST_CACHE_DIR)
        except OSError:
            pass

        header = server._scan_transcript_header(path, sid)
        assert_eq("cwd", header["cwd"], "/projects/foo")
        assert_eq("entrypoint", header["entrypoint"], "sdk-cli")
        assert_ok("first_prompt not empty", bool(header["first_prompt"]))
        assert_eq("model", header["model"], "claude-sonnet-4-6")
        assert_ok("started_at set", header["started_at"].startswith("2026-04-17"))
        assert_eq("is_headless", header["is_headless"], True)
        assert_eq("permission_mode", header["permission_mode"], "bypassPermissions")


def test_header_scan_permission_mode_default():
    """Interactive sessions have permissionMode=default, is_headless=False."""
    print("test_header_scan_permission_mode_default")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-header-interactive"
        lines = [
            _make_user("hello", permission_mode="default", entrypoint="cli"),
            _make_assistant([{"type": "text", "text": "hi"}], msg_id="msg_i1"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        try:
            shutil.rmtree(server.COST_CACHE_DIR)
        except OSError:
            pass

        header = server._scan_transcript_header(path, sid)
        assert_eq("is_headless", header["is_headless"], False)
        assert_eq("permission_mode", header["permission_mode"], "default")


# ─── Test: _find_transcript path safety ──────────────────────────────

def test_find_transcript_rejects_traversal():
    """Session IDs with path traversal characters must be rejected."""
    print("test_find_transcript_rejects_traversal")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        proj_dir = tmp / ".claude" / "projects"
        proj_dir.mkdir(parents=True, exist_ok=True)
        import server
        server.PROJECTS_DIR = proj_dir
        server._transcript_cache.clear()
        for bad_id in ["../../etc/passwd", "foo/bar", "foo\\bar",
                       "test\x00inject", "..", "."]:
            result = server._find_transcript(bad_id)
            assert_eq(f"traversal rejected: {bad_id!r}", result, None)


# ─── Test: queue-operation prompt extraction ─────────────────────────

def test_queue_enqueue_sets_first_prompt():
    """queue-operation with operation=enqueue should set first_prompt
    even if no user text line follows within the header window."""
    print("test_queue_enqueue_sets_first_prompt")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        sid = "test-queue-prompt"
        lines = [
            _make_queue_enqueue("the real first prompt"),
            {"type": "queue-operation", "operation": "dequeue",
             "timestamp": "2026-04-17T10:00:00.000Z", "sessionId": "test-sid"},
            _make_user("<local-command-caveat>ignored</local-command-caveat>"),
        ]
        path = _write_transcript(tmp, sid, lines)

        import server
        server.PROJECTS_DIR = tmp / ".claude" / "projects"
        server._transcript_cache.clear()
        result = server.read_transcript(sid)
        assert_eq("first_prompt from queue", result["first_prompt"],
                  "the real first prompt")


# ─── Main ────────────────────────────────────────────────────────────

def main():
    failed = 0
    for fn in [
        test_streaming_merge_combines_content_blocks,
        test_streaming_merge_different_ids_not_merged,
        test_synthetic_messages_rendered_not_skipped,
        test_is_meta_lines_skipped,
        test_system_events_parsed,
        test_tool_result_gets_tool_name,
        test_attachment_with_null_message,
        test_compute_usage_streaming_takes_last,
        test_header_scan_extracts_metadata,
        test_header_scan_permission_mode_default,
        test_find_transcript_rejects_traversal,
        test_queue_enqueue_sets_first_prompt,
    ]:
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {type(e).__name__}: {e}")
    print(f"\n{'OK' if failed == 0 else 'FAILED'} ({failed} failure{'s' if failed != 1 else ''})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
