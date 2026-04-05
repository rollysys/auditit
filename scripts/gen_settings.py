#!/usr/bin/env python3
"""
gen_settings.py — Generate a session-specific settings.json with audit hooks.

Reads the user's global ~/.claude/settings.json, merges audit hooks into it,
and writes the result to a session-local file. The global file is NEVER modified.

Usage:
    python gen_settings.py generate --output /tmp/auditit/session-xxx/settings.json
    python gen_settings.py status   --settings /tmp/auditit/session-xxx/settings.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from util import HOOK_EVENTS

GLOBAL_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_SCRIPT = Path(__file__).resolve().parent.parent / "hooks" / "audit_hook.sh"


def _load_global() -> dict:
    if GLOBAL_SETTINGS.exists():
        try:
            return json.loads(GLOBAL_SETTINGS.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def generate(output: Path) -> None:
    """Create a session-specific settings.json = global settings + audit hooks."""
    settings = _load_global()
    hooks = settings.setdefault("hooks", {})

    for event in HOOK_EVENTS:
        cmd = f"bash {HOOK_SCRIPT} {event}"
        group = {"hooks": [{"type": "command", "command": cmd}]}

        event_hooks = hooks.setdefault(event, [])
        # Remove any stale auditit hooks (by command path pattern)
        hooks[event] = [
            h for h in event_hooks
            if not any("audit_hook.sh" in hk.get("command", "") for hk in h.get("hooks", []))
        ]
        hooks[event].append(group)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"[auditit] 已生成 session settings: {output}", file=sys.stderr)


def status(settings_path: Path) -> None:
    """Show whether a settings file contains audit hooks."""
    if not settings_path.exists():
        print(f"[auditit] 文件不存在: {settings_path}")
        return
    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})
    count = sum(
        1 for entries in hooks.values()
        for h in entries
        for hk in h.get("hooks", [])
        if "audit_hook.sh" in hk.get("command", "")
    )
    print(f"[auditit] {settings_path}: {count} audit hooks")


def main() -> None:
    parser = argparse.ArgumentParser(description="auditit settings generator")
    sub = parser.add_subparsers(dest="cmd")

    p_gen = sub.add_parser("generate", help="Generate session settings with audit hooks")
    p_gen.add_argument("--output", required=True, help="Output settings.json path")

    p_st = sub.add_parser("status", help="Check a settings file for audit hooks")
    p_st.add_argument("--settings", required=True, help="Settings file to check")

    args = parser.parse_args()
    if args.cmd == "generate":
        generate(Path(args.output))
    elif args.cmd == "status":
        status(Path(args.settings))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
