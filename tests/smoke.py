#!/usr/bin/env python3
"""Smoke test for hook.py.

Runs hook.py against synthetic stdin payloads in a sandboxed HOME,
verifies the resulting audit.jsonl, env.json, summary.json, gz contents,
and sub-agent slicing layout. Stdlib only — no pytest, no fixtures.

Run:  python3 tests/smoke.py
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hook.py"


def run_hook(event: str, payload: dict, env_overrides: dict, home: Path) -> int:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.update(env_overrides)
    p = subprocess.run(
        ["python3", str(HOOK), event],
        input=json.dumps(payload),
        text=True,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        print(f"  ! hook exited {p.returncode}; stderr={p.stderr!r}")
    return p.returncode


def assert_eq(name, got, want):
    if got != want:
        raise AssertionError(f"{name}: got {got!r}, want {want!r}")


def assert_ok(name, cond, msg=""):
    if not cond:
        raise AssertionError(f"{name}: {msg}")


def session_dir(home: Path, sid: str) -> Path:
    """Resolve the per-session directory under flat layout."""
    return home / ".claude-audit" / sid


# Backwards-compat alias for tests that still reference the old name.
today_dir = session_dir


def test_basic_append_and_env():
    print("test_basic_append_and_env")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-001"
        rc = run_hook(
            "PreToolUse",
            {"session_id": sid, "tool_name": "Bash", "cwd": "/x"},
            {"ANTHROPIC_BASE_URL": "https://example.test"},
            home,
        )
        assert_eq("rc", rc, 0)
        d = today_dir(home, sid)
        assert_ok("dir exists", d.is_dir(), str(d))
        env = json.loads((d / "env.json").read_text())
        assert_eq("env.anthropic_base_url", env["anthropic_base_url"], "https://example.test")
        lines = (d / "audit.jsonl").read_text().splitlines()
        assert_eq("audit lines", len(lines), 1)
        rec = json.loads(lines[0])
        assert_eq("event", rec["event"], "PreToolUse")
        assert_eq("data.session_id", rec["data"]["session_id"], sid)
        # Second call appends, env.json untouched.
        rc = run_hook("PostToolUse", {"session_id": sid}, {"ANTHROPIC_BASE_URL": "OTHER"}, home)
        assert_eq("rc2", rc, 0)
        env2 = json.loads((d / "env.json").read_text())
        assert_eq("env unchanged", env2["anthropic_base_url"], "https://example.test")
        lines = (d / "audit.jsonl").read_text().splitlines()
        assert_eq("audit lines after 2", len(lines), 2)


def test_failure_safe_on_bad_input():
    print("test_failure_safe_on_bad_input")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        # Junk stdin — must still exit 0, must not create anything.
        env = os.environ.copy()
        env["HOME"] = str(home)
        p = subprocess.run(
            ["python3", str(HOOK), "PreToolUse"],
            input="not json at all",
            text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert_eq("rc on junk", p.returncode, 0)
        assert_ok("no audit dir", not (home / ".claude-audit").exists())
        # Empty stdin
        p = subprocess.run(
            ["python3", str(HOOK), "PreToolUse"],
            input="", text=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert_eq("rc on empty", p.returncode, 0)
        # Missing session_id
        p = subprocess.run(
            ["python3", str(HOOK), "PreToolUse"],
            input='{"foo":"bar"}', text=True, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert_eq("rc on no sid", p.returncode, 0)


def test_session_end_with_transcript_skips_synthetic():
    print("test_session_end_with_transcript_skips_synthetic")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-002"
        # Build a fake transcript: real assistant -> synthetic error -> EOF.
        # The summary must record the REAL model, not <synthetic>.
        transcript = home / "fake_transcript.jsonl"
        rows = [
            {"type": "user", "timestamp": "2026-04-15T10:00:00Z"},
            {"type": "assistant",
             "timestamp": "2026-04-15T10:00:05Z",
             "message": {"model": "GLM-5",
                         "usage": {"input_tokens": 100, "output_tokens": 50,
                                   "cache_read_input_tokens": 200,
                                   "cache_creation_input_tokens": 0}}},
            {"type": "user", "timestamp": "2026-04-15T10:00:10Z"},
            {"type": "assistant",
             "timestamp": "2026-04-15T10:00:11Z",
             "message": {"model": "<synthetic>",
                         "usage": {"input_tokens": 0, "output_tokens": 0}}},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

        # Pre-create some prior events so SessionEnd has a jsonl to gzip.
        rc = run_hook("UserPromptSubmit", {"session_id": sid, "prompt": "hi"}, {}, home)
        assert_eq("rc1", rc, 0)
        rc = run_hook(
            "SessionEnd",
            {"session_id": sid, "transcript_path": str(transcript), "reason": "exit"},
            {},
            home,
        )
        assert_eq("rc2", rc, 0)

        d = today_dir(home, sid)
        summary = json.loads((d / "summary.json").read_text())
        assert_eq("model skipped synthetic", summary["model"], "GLM-5")
        assert_eq("turns", summary["num_turns"], 2)
        assert_eq("usage.input_tokens", summary["usage"]["input_tokens"], 100)
        assert_eq("ctx_peak", summary["ctx_peak_tokens"], 300)
        # audit.jsonl should be replaced by .gz
        assert_ok("plain jsonl gone", not (d / "audit.jsonl").exists())
        assert_ok("gz exists", (d / "audit.jsonl.gz").exists())
        # gzip integrity: line count must match
        with gzip.open(d / "audit.jsonl.gz", "rt") as f:
            gz_lines = sum(1 for _ in f)
        assert_eq("gz line count", gz_lines, 2)  # UserPromptSubmit + SessionEnd


def test_subagent_slicing_from_transcript():
    """Post-2026-04-16 reality: SubagentStart never fires; SubagentStop with
    non-empty agent_type is the sole anchor. This test fires a synthetic
    SubagentStop that points at a pre-written transcript + meta.json, and
    verifies _slice_subagents materialises a sub-agent dir by translating
    the transcript into our hook-event format.
    """
    print("test_subagent_slicing_from_transcript")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-sa-transcript"
        aid = "atestsubagent00001"

        # Build a Claude-Code-shaped transcript + meta sidecar.
        transcript_dir = home / ".claude" / "projects" / "-x" / sid / "subagents"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / f"agent-{aid}.jsonl"
        meta_path       = transcript_dir / f"agent-{aid}.meta.json"
        transcript_rows = [
            {"type": "user", "isSidechain": True,
             "timestamp": "2026-04-16T10:00:00.000Z",
             "message": {"role": "user", "content": "find the file"}},
            {"type": "assistant", "isSidechain": True,
             "timestamp": "2026-04-16T10:00:01.500Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "I'll glob for it."},
                 {"type": "tool_use", "id": "tu_abc",
                  "name": "Glob", "input": {"pattern": "**/*.py"}},
             ]}},
            {"type": "user", "isSidechain": True,
             "timestamp": "2026-04-16T10:00:02.000Z",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "tu_abc",
                  "content": "src/main.py"},
             ]}},
            {"type": "assistant", "isSidechain": True,
             "timestamp": "2026-04-16T10:00:03.000Z",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Found it at src/main.py."},
             ]}},
        ]
        transcript_path.write_text("\n".join(json.dumps(r) for r in transcript_rows) + "\n")
        meta_path.write_text(json.dumps({
            "agentType":   "Explore",
            "description": "Find the main entry point",
        }))

        # Feed a SubagentStop event that references this transcript.
        rc = run_hook("SubagentStop", {
            "session_id":            sid,
            "agent_id":              aid,
            "agent_type":            "Explore",
            "agent_transcript_path": str(transcript_path),
            "last_assistant_message": "Found it at src/main.py.",
        }, {}, home)
        assert_eq("rc SubagentStop", rc, 0)
        # SessionEnd triggers slicing + gzip.
        rc = run_hook("SessionEnd",
                      {"session_id": sid, "transcript_path": "", "reason": "exit"},
                      {}, home)
        assert_eq("rc SessionEnd", rc, 0)

        d = today_dir(home, sid)
        siblings_root = d.parent
        sub = siblings_root / f"{sid}__agent__{aid}"
        assert_ok("sub dir exists", sub.is_dir(), str(sub))

        meta = json.loads((sub / "meta.json").read_text())
        assert_eq("meta.agent_id",         meta["agent_id"], aid)
        assert_eq("meta.agent_type",       meta["agent_type"], "Explore")
        assert_eq("meta.description",      meta["description"], "Find the main entry point")
        assert_eq("meta.parent_session_id", meta["parent_session_id"], sid)
        assert_eq("meta.source",           meta["source"], "transcript")

        summary = json.loads((sub / "summary.json").read_text())
        assert_eq("summary.num_tool_calls", summary["num_tool_calls"], 1)  # Glob
        assert_eq("summary.num_turns",      summary["num_turns"], 1)       # user msg

        # Audit events: translate gives UserPromptSubmit + AssistantMessage
        # + PreToolUse(Glob) + PostToolUse(Glob) + AssistantMessage + SubagentStop
        assert_ok("sub gz exists", (sub / "audit.jsonl.gz").exists())
        assert_ok("sub plain gone", not (sub / "audit.jsonl").exists())
        with gzip.open(sub / "audit.jsonl.gz", "rt") as f:
            sub_events = [json.loads(l) for l in f if l.strip()]
        ev_names = [e["event"] for e in sub_events]
        assert_ok("contains PreToolUse",     "PreToolUse" in ev_names, str(ev_names))
        assert_ok("contains PostToolUse",    "PostToolUse" in ev_names, str(ev_names))
        assert_ok("contains UserPromptSubmit","UserPromptSubmit" in ev_names, str(ev_names))
        assert_ok("ends with SubagentStop",  ev_names[-1] == "SubagentStop", str(ev_names))
        pre = [e for e in sub_events if e["event"] == "PreToolUse"][0]
        assert_eq("pre.tool_name", pre["data"]["tool_name"], "Glob")
        post = [e for e in sub_events if e["event"] == "PostToolUse"][0]
        assert_eq("post.tool_name", post["data"]["tool_name"], "Glob")


def test_checkpoint_subagent_stop_not_sliced():
    """Type-B SubagentStop (empty agent_type, no transcript) must NOT
    produce a sub-agent dir — it is a state-summary checkpoint and
    belongs inline in the parent audit stream only.
    """
    print("test_checkpoint_subagent_stop_not_sliced")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-checkpoint"
        rc = run_hook("SubagentStop", {
            "session_id":             sid,
            "agent_id":               "astatesum00000001",
            "agent_type":             "",    # ← type B marker
            "agent_transcript_path":  "/nonexistent",
            "last_assistant_message": "Goal: X. Current: Y. Next: Z.",
        }, {}, home)
        assert_eq("rc", rc, 0)
        rc = run_hook("SessionEnd",
                      {"session_id": sid, "transcript_path": "", "reason": "exit"},
                      {}, home)
        assert_eq("rc SessionEnd", rc, 0)

        siblings_root = today_dir(home, sid).parent
        agent_dirs = [p for p in siblings_root.iterdir()
                      if p.is_dir() and "__agent__" in p.name]
        assert_ok("no sub-agent dir created for type-B stop",
                  agent_dirs == [],
                  f"unexpected: {[p.name for p in agent_dirs]}")


def test_single_session_multi_tool_capture():
    """Realistic single session with several tool types and one failure.

    Verifies that every event is appended in order with correct tool_name,
    that PostToolUseFailure is preserved as a distinct event, and that
    SessionEnd produces a gzip whose line count matches the input.
    """
    print("test_single_session_multi_tool_capture")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-multi-tool"

        events = [
            ("PreToolUse",         {"session_id": sid, "tool_name": "Read",
                                     "tool_input": {"file_path": "/x/a.py"}}),
            ("PostToolUse",        {"session_id": sid, "tool_name": "Read",
                                     "tool_response": {"type": "text", "file": {}}}),
            ("PreToolUse",         {"session_id": sid, "tool_name": "Bash",
                                     "tool_input": {"command": "ls /tmp"}}),
            ("PostToolUse",        {"session_id": sid, "tool_name": "Bash",
                                     "tool_response": {"stdout": "x\n", "stderr": ""}}),
            ("PreToolUse",         {"session_id": sid, "tool_name": "Edit",
                                     "tool_input": {"file_path": "/x/a.py",
                                                    "old_string": "old", "new_string": "new"}}),
            ("PostToolUseFailure", {"session_id": sid, "tool_name": "Edit",
                                     "tool_input": {"file_path": "/x/a.py"}}),
            ("PreToolUse",         {"session_id": sid, "tool_name": "Grep",
                                     "tool_input": {"pattern": "foo"}}),
            ("PostToolUse",        {"session_id": sid, "tool_name": "Grep",
                                     "tool_response": {"numFiles": 2, "numLines": 5}}),
            ("Stop",               {"session_id": sid}),
        ]
        for ev, payload in events:
            assert_eq(f"rc {ev}", run_hook(ev, payload, {}, home), 0)

        d = today_dir(home, sid)
        lines = (d / "audit.jsonl").read_text().splitlines()
        assert_eq("audit lines before SessionEnd", len(lines), len(events))

        # Ordering and event/tool round-trip
        for i, (ev_expected, payload_expected) in enumerate(events):
            rec = json.loads(lines[i])
            assert_eq(f"line {i} event", rec["event"], ev_expected)
            tn = payload_expected.get("tool_name", "")
            if tn:
                assert_eq(f"line {i} tool_name", rec["data"].get("tool_name"), tn)

        # Specifically: PostToolUseFailure preserved as its own event,
        # not folded into PostToolUse.
        events_seen = [json.loads(l)["event"] for l in lines]
        assert_ok("PostToolUseFailure preserved",
                  events_seen.count("PostToolUseFailure") == 1,
                  f"events_seen={events_seen}")

        # SessionEnd with no transcript still triggers gzip.
        rc = run_hook("SessionEnd",
                      {"session_id": sid, "transcript_path": "", "reason": "exit"},
                      {}, home)
        assert_eq("rc SessionEnd", rc, 0)
        assert_ok("plain jsonl gone after SessionEnd",
                  not (d / "audit.jsonl").exists())
        assert_ok("gz exists", (d / "audit.jsonl.gz").exists())
        with gzip.open(d / "audit.jsonl.gz", "rt") as f:
            gz_lines = [l for l in f if l.strip()]
        # Original 9 events + SessionEnd itself = 10
        assert_eq("gz line count", len(gz_lines), len(events) + 1)
        # Per-line event/tool fields survived gzip intact
        gz_events = [json.loads(l)["event"] for l in gz_lines]
        assert_eq("gz events match",
                  gz_events,
                  [ev for ev, _ in events] + ["SessionEnd"])


def _DISABLED_test_nested_subagent_slicing():  # retained for reference; SubagentStart no longer fires
    """Two levels of nested sub-agents.

    Layout (events written to parent audit.jsonl in order):
       1  PreToolUse(Task)        ← parent fires Task to spawn agt-1
       2  SubagentStart(agt-1)
       3  PreToolUse(Bash)        ← inside agt-1
       4  PostToolUse(Bash)
       5  PreToolUse(Task)        ← agt-1 fires Task to spawn agt-2
       6  SubagentStart(agt-2)    ← nested
       7  PreToolUse(Read)        ← inside agt-2
       8  PostToolUse(Read)
       9  SubagentStop(agt-2)
      10  PreToolUse(Edit)        ← back inside agt-1
      11  PostToolUse(Edit)
      12  SubagentStop(agt-1)

    Then SessionEnd. Slicing must produce two sub-agent dirs:
      - <sid>__agent__agt-1            : events 2..12 mirrored (11 lines)
      - <sid>__agent__agt-1__agent__agt-2 : events 6..9 (4 lines)
    Parent gz keeps everything (12 + SessionEnd = 13 lines).
    """
    print("test_nested_subagent_slicing")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-nested"

        events = [
            ("PreToolUse",     {"session_id": sid, "tool_name": "Task",
                                 "tool_input": {"description": "outer work",
                                                "subagent_type": "Explore",
                                                "prompt": "p"}}),
            ("SubagentStart",  {"session_id": sid, "agent_id": "agt-1",
                                 "agent_type": "Explore"}),
            ("PreToolUse",     {"session_id": sid, "tool_name": "Bash",
                                 "tool_input": {"command": "ls"}}),
            ("PostToolUse",    {"session_id": sid, "tool_name": "Bash",
                                 "tool_response": {"stdout": ""}}),
            ("PreToolUse",     {"session_id": sid, "tool_name": "Task",
                                 "tool_input": {"description": "inner work",
                                                "subagent_type": "Plan",
                                                "prompt": "p2"}}),
            ("SubagentStart",  {"session_id": sid, "agent_id": "agt-2",
                                 "agent_type": "Plan"}),
            ("PreToolUse",     {"session_id": sid, "tool_name": "Read",
                                 "tool_input": {"file_path": "/x/y"}}),
            ("PostToolUse",    {"session_id": sid, "tool_name": "Read",
                                 "tool_response": {"type": "text"}}),
            ("SubagentStop",   {"session_id": sid, "agent_id": "agt-2"}),
            ("PreToolUse",     {"session_id": sid, "tool_name": "Edit",
                                 "tool_input": {"file_path": "/x/y",
                                                "old_string": "a", "new_string": "b"}}),
            ("PostToolUse",    {"session_id": sid, "tool_name": "Edit",
                                 "tool_response": {"filePath": "/x/y"}}),
            ("SubagentStop",   {"session_id": sid, "agent_id": "agt-1"}),
        ]
        for ev, payload in events:
            assert_eq(f"rc {ev}", run_hook(ev, payload, {}, home), 0)

        rc = run_hook("SessionEnd",
                      {"session_id": sid, "transcript_path": "", "reason": "exit"},
                      {}, home)
        assert_eq("rc SessionEnd", rc, 0)

        d = today_dir(home, sid)
        date_dir = d.parent

        # ── Parent: audit.jsonl gzipped, contains every event + SessionEnd
        assert_ok("parent plain jsonl gone", not (d / "audit.jsonl").exists())
        assert_ok("parent gz exists", (d / "audit.jsonl.gz").exists())
        with gzip.open(d / "audit.jsonl.gz", "rt") as f:
            parent_lines = [l for l in f if l.strip()]
        assert_eq("parent gz line count", len(parent_lines), len(events) + 1)

        # ── Layer 1: <sid>__agent__agt-1
        l1 = date_dir / f"{sid}__agent__agt-1"
        assert_ok("layer1 dir exists", l1.is_dir(), str(l1))
        assert_ok("layer1 plain gone", not (l1 / "audit.jsonl").exists())
        assert_ok("layer1 gz exists",  (l1 / "audit.jsonl.gz").exists())
        with gzip.open(l1 / "audit.jsonl.gz", "rt") as f:
            l1_lines = [l for l in f if l.strip()]
        # Events 2..12 inclusive = 11 lines
        assert_eq("layer1 line count", len(l1_lines), 11)
        l1_events = [json.loads(l)["event"] for l in l1_lines]
        assert_eq("layer1 first event", l1_events[0], "SubagentStart")
        assert_eq("layer1 last event",  l1_events[-1], "SubagentStop")
        # Layer1 must include the nested layer2's events (mirrored)
        assert_ok("layer1 contains nested SubagentStart",
                  l1_events.count("SubagentStart") == 2,
                  f"events={l1_events}")
        assert_ok("layer1 contains nested SubagentStop",
                  l1_events.count("SubagentStop") == 2)

        l1_meta = json.loads((l1 / "meta.json").read_text())
        assert_eq("l1.meta.agent_id",         l1_meta["agent_id"], "agt-1")
        assert_eq("l1.meta.parent_session_id", l1_meta["parent_session_id"], sid)
        assert_eq("l1.meta.description",      l1_meta["description"], "outer work")

        l1_summary = json.loads((l1 / "summary.json").read_text())
        assert_eq("l1.is_subagent",  l1_summary["is_subagent"], True)
        # Tool calls inside layer1: Bash, Read (from nested mirror), Edit = 3
        assert_eq("l1.num_tool_calls", l1_summary["num_tool_calls"], 3)

        # ── Layer 2 (nested): <sid>__agent__agt-1__agent__agt-2
        l2 = date_dir / f"{sid}__agent__agt-1__agent__agt-2"
        assert_ok("layer2 dir exists", l2.is_dir(), str(l2))
        assert_ok("layer2 gz exists", (l2 / "audit.jsonl.gz").exists())
        with gzip.open(l2 / "audit.jsonl.gz", "rt") as f:
            l2_lines = [l for l in f if l.strip()]
        # Events 6..9 inclusive = 4 lines
        assert_eq("layer2 line count", len(l2_lines), 4)
        l2_events = [json.loads(l)["event"] for l in l2_lines]
        assert_eq("layer2 events sequence", l2_events,
                  ["SubagentStart", "PreToolUse", "PostToolUse", "SubagentStop"])

        l2_meta = json.loads((l2 / "meta.json").read_text())
        assert_eq("l2.meta.agent_id",         l2_meta["agent_id"], "agt-2")
        # parent_session_id of nested layer is the IMMEDIATE parent layer name,
        # not the root sid
        assert_eq("l2.meta.parent_session_id",
                  l2_meta["parent_session_id"],
                  f"{sid}__agent__agt-1")
        assert_eq("l2.meta.description",      l2_meta["description"], "inner work")

        l2_summary = json.loads((l2 / "summary.json").read_text())
        # Only Read inside layer2 = 1 tool call
        assert_eq("l2.num_tool_calls", l2_summary["num_tool_calls"], 1)


def test_resumed_session_merges_into_gz():
    """A session that SessionEnd'd then was resumed produces a (.gz + .jsonl)
    pair until the next SessionEnd merges them. After that next SessionEnd
    the cumulative history must be one .gz containing all events from
    BOTH the original run and the resume.
    """
    print("test_resumed_session_merges_into_gz")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-resume"

        # First run: 2 events then SessionEnd → produces audit.jsonl.gz
        for ev, payload in [
            ("PreToolUse",  {"session_id": sid, "tool_name": "Read"}),
            ("PostToolUse", {"session_id": sid, "tool_name": "Read"}),
        ]:
            assert_eq(f"rc {ev}", run_hook(ev, payload, {}, home), 0)
        assert_eq("rc end1",
                  run_hook("SessionEnd",
                            {"session_id": sid, "transcript_path": "", "reason": "exit"},
                            {}, home),
                  0)
        d = today_dir(home, sid)
        assert_ok("after first SessionEnd: gz only",
                  not (d / "audit.jsonl").exists() and (d / "audit.jsonl.gz").exists())
        with gzip.open(d / "audit.jsonl.gz", "rt") as f:
            first_run_lines = [l for l in f if l.strip()]
        assert_eq("first-run gz lines", len(first_run_lines), 3)  # 2 events + SessionEnd

        # Resume: same sid, more events. hook.py opens audit.jsonl in append
        # so .gz coexists with a fresh .jsonl until next SessionEnd.
        for ev, payload in [
            ("PreToolUse",  {"session_id": sid, "tool_name": "Bash",
                              "tool_input": {"command": "echo"}}),
            ("PostToolUse", {"session_id": sid, "tool_name": "Bash"}),
            ("PreToolUse",  {"session_id": sid, "tool_name": "Edit",
                              "tool_input": {"file_path": "/x"}}),
        ]:
            assert_eq(f"rc resumed {ev}", run_hook(ev, payload, {}, home), 0)

        assert_ok("during resume: both files coexist",
                  (d / "audit.jsonl").exists() and (d / "audit.jsonl.gz").exists())
        resumed_jsonl_lines = (d / "audit.jsonl").read_text().splitlines()
        assert_eq("resume jsonl line count", len(resumed_jsonl_lines), 3)

        # Second SessionEnd: must merge .gz + .jsonl into a single new .gz
        assert_eq("rc end2",
                  run_hook("SessionEnd",
                            {"session_id": sid, "transcript_path": "", "reason": "exit"},
                            {}, home),
                  0)
        assert_ok("after second SessionEnd: gz only again",
                  not (d / "audit.jsonl").exists() and (d / "audit.jsonl.gz").exists())
        with gzip.open(d / "audit.jsonl.gz", "rt") as f:
            merged_lines = [l for l in f if l.strip()]
        # 3 from first run (2 + SessionEnd) + 3 resume tool events + SessionEnd = 7
        assert_eq("merged gz line count", len(merged_lines), 7)

        # Order check: first-run events precede resume events
        first_3_events = [json.loads(l)["event"] for l in merged_lines[:3]]
        rest_events    = [json.loads(l)["event"] for l in merged_lines[3:]]
        assert_eq("history preserved at front", first_3_events,
                  ["PreToolUse", "PostToolUse", "SessionEnd"])
        assert_eq("resume events appended", rest_events,
                  ["PreToolUse", "PostToolUse", "PreToolUse", "SessionEnd"])


def test_flat_layout_no_date_dir():
    """Hook writes directly under ~/.claude-audit/<sid>/ — there must be
    no YYYY-MM-DD intermediate dir. Regression guard for the flat-layout
    refactor (long-running sessions used to fragment by UTC day).
    """
    print("test_flat_layout_no_date_dir")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-flat"
        rc = run_hook("PreToolUse",
                      {"session_id": sid, "tool_name": "Read"},
                      {}, home)
        assert_eq("rc", rc, 0)
        # Direct child of .claude-audit must be the session dir, not a date.
        children = sorted(p.name for p in (home / ".claude-audit").iterdir())
        assert_ok("only session dir present, no date dir",
                  children == [sid],
                  f"unexpected entries: {children}")
        # No nested date dir inside the session dir either.
        sd = home / ".claude-audit" / sid
        for child in sd.iterdir():
            assert_ok(f"child {child.name} is a file (not a date dir)",
                      child.is_file(),
                      f"unexpected directory inside session dir: {child}")


def main():
    failed = 0
    for fn in [
        test_basic_append_and_env,
        test_failure_safe_on_bad_input,
        test_session_end_with_transcript_skips_synthetic,
        test_subagent_slicing_from_transcript,
        test_checkpoint_subagent_stop_not_sliced,
        test_single_session_multi_tool_capture,
        test_resumed_session_merges_into_gz,
        test_flat_layout_no_date_dir,
    ]:
        try:
            fn()
            print("  PASS")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {e}")
    print(f"\n{'OK' if failed == 0 else 'FAILED'} ({failed} failure{'s' if failed != 1 else ''})")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
