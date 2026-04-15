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


def today_dir(home: Path, sid: str) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return home / ".claude-audit" / today / sid


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


def test_subagent_slicing():
    print("test_subagent_slicing")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        sid = "test-sid-003"
        # Replay events: PreToolUse(Task) -> SubagentStart -> Pre/Post inside ->
        # SubagentStop -> SessionEnd. The slice should produce a sub-agent dir.
        events = [
            ("PreToolUse", {"session_id": sid, "tool_name": "Task",
                            "tool_input": {"description": "do work",
                                           "subagent_type": "Explore",
                                           "prompt": "x"}}),
            ("SubagentStart", {"session_id": sid, "agent_id": "agt-1",
                               "agent_type": "Explore"}),
            ("PreToolUse", {"session_id": sid, "tool_name": "Bash"}),
            ("PostToolUse", {"session_id": sid, "tool_name": "Bash"}),
            ("SubagentStop", {"session_id": sid, "agent_id": "agt-1"}),
        ]
        for ev, payload in events:
            rc = run_hook(ev, payload, {}, home)
            assert_eq(f"rc {ev}", rc, 0)

        # SessionEnd with no transcript path — sub-agent slicing still runs.
        rc = run_hook("SessionEnd",
                      {"session_id": sid, "transcript_path": "", "reason": "exit"},
                      {}, home)
        assert_eq("rc SessionEnd", rc, 0)

        d = today_dir(home, sid)
        date_dir = d.parent
        # Find the sub-agent dir: <sid>__agent__agt-1
        sub = date_dir / f"{sid}__agent__agt-1"
        assert_ok("sub dir exists", sub.is_dir(), str(sub))
        meta = json.loads((sub / "meta.json").read_text())
        assert_eq("meta.agent_id", meta["agent_id"], "agt-1")
        assert_eq("meta.parent_session_id", meta["parent_session_id"], sid)
        assert_eq("meta.description", meta["description"], "do work")
        sub_summary = json.loads((sub / "summary.json").read_text())
        assert_eq("sub.is_subagent", sub_summary["is_subagent"], True)
        assert_eq("sub.num_tool_calls", sub_summary["num_tool_calls"], 1)
        # sub-agent jsonl must also be gzipped
        assert_ok("sub plain gone", not (sub / "audit.jsonl").exists())
        assert_ok("sub gz exists", (sub / "audit.jsonl.gz").exists())


def main():
    failed = 0
    for fn in [
        test_basic_append_and_env,
        test_failure_safe_on_bad_input,
        test_session_end_with_transcript_skips_synthetic,
        test_subagent_slicing,
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
