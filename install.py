#!/usr/bin/env python3
"""
install.py — Safe, standalone installer for the auditit Claude Code hook.

Registers a shell hook script for every Claude Code hook event in
~/.claude/settings.json. Designed to be safe to run on any machine:

  * Single file, standard library only. Copy it anywhere and run it.
  * Pre-flight checks before touching settings.json.
  * Timestamped backup on every destructive action (kept, not auto-cleaned).
  * Atomic write (tempfile + os.replace) so settings.json is never half-written.
  * fcntl advisory lock guards against concurrent installers racing.
  * Our hooks are identified by an embedded marker, not by path equality —
    so uninstall still finds us after the repo is moved.
  * --dry-run prints the diff instead of writing.
  * On any exception, the pre-action backup is automatically restored.

Usage:
    install.py install   [--hook PATH] [--dry-run] [--force]
    install.py uninstall [--dry-run]
    install.py status
    install.py doctor

By default --hook is the `hook.sh` next to this script. Passing --hook lets
you point at any other script (e.g. a vendored copy on a remote host).

The authoritative hook-event list lives in docs/claude-code-hooks.md and is
derived from https://code.claude.com/docs/en/hooks (fetched 2026-04-11).
FileChanged is intentionally skipped because it requires a filename pattern;
registering it with an empty matcher has undefined behavior per the docs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
    HAVE_FCNTL = True
except ImportError:
    HAVE_FCNTL = False

# ── Constants ────────────────────────────────────────────────────────

# Marker embedded in every hook command we install. Used by uninstall/status
# to recognize our hooks regardless of the hook script's current path, so
# moving the repo does not orphan the settings.json entries.
MARKER = "# auditit"

SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# 25 events, derived from docs/claude-code-hooks.md (Claude Code hooks ref).
# See that file for the full table of fields and matcher support.
# FileChanged is deliberately omitted — see module docstring.
HOOK_EVENTS: list[str] = [
    "SessionStart",
    "InstructionsLoaded",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PermissionDenied",
    "PostToolUse",
    "PostToolUseFailure",
    "Notification",
    "SubagentStart",
    "SubagentStop",
    "TaskCreated",
    "TaskCompleted",
    "Stop",
    "StopFailure",
    "TeammateIdle",
    "ConfigChange",
    "CwdChanged",
    "WorktreeCreate",
    "WorktreeRemove",
    "PreCompact",
    "PostCompact",
    "Elicitation",
    "ElicitationResult",
    "SessionEnd",
]


# ── IO helpers ───────────────────────────────────────────────────────

def _err(msg: str) -> None:
    print(f"install.py: {msg}", file=sys.stderr)


def _log(msg: str) -> None:
    print(f"install.py: {msg}")


def _load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise SystemExit(
            f"install.py: {SETTINGS_PATH} is not valid JSON ({e}). "
            f"Refusing to touch it — please fix by hand first."
        )


def _atomic_write(path: Path, data: str) -> None:
    """Write `data` to `path` via tempfile + os.replace so the target is
    never observed in a partial state.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Tempfile in same dir so os.replace is a rename, not a cross-device copy.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try: os.unlink(tmp_name)
        except OSError: pass
        raise


def _save_settings(data: dict) -> None:
    payload = json.dumps(data, indent=4, ensure_ascii=False) + "\n"
    _atomic_write(SETTINGS_PATH, payload)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _backup_settings() -> Path | None:
    """Copy current settings.json to a timestamped sibling. Returns the
    backup path, or None if no settings.json exists yet.
    """
    if not SETTINGS_PATH.exists():
        return None
    bak = SETTINGS_PATH.with_name(f"settings.json.auditit.{_timestamp()}.bak")
    bak.write_bytes(SETTINGS_PATH.read_bytes())
    return bak


@contextmanager
def _settings_lock():
    """Advisory file lock on settings.json's parent dir so two concurrent
    installers can't interleave. Falls back to a no-op on systems without
    fcntl (e.g. native Windows).
    """
    if not HAVE_FCNTL:
        yield
        return
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path = SETTINGS_PATH.parent / ".auditit-install.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError: pass
        fh.close()


# ── Hook matching (marker-based) ─────────────────────────────────────

def _is_our_command(cmd: str) -> bool:
    """Return True if a hook command string is one we installed.

    Matches on the embedded MARKER, not on the script path, so a moved
    repo doesn't orphan the existing entries.
    """
    return isinstance(cmd, str) and MARKER in cmd


def _has_our_hook(entry_list) -> bool:
    if not isinstance(entry_list, list):
        return False
    for item in entry_list:
        if not isinstance(item, dict):
            continue
        # Wrapped: {"matcher": ..., "hooks": [{"type": "command", "command": ...}]}
        if "hooks" in item:
            for h in item["hooks"]:
                if isinstance(h, dict) and _is_our_command(h.get("command", "")):
                    return True
        # Flat: {"type": "command", "command": "..."}
        elif _is_our_command(item.get("command", "")):
            return True
    return False


def _strip_our_hooks(entry_list) -> list:
    """Return entry_list with our hooks removed. Empty wrappers are dropped."""
    if not isinstance(entry_list, list):
        return []
    out: list = []
    for item in entry_list:
        if not isinstance(item, dict):
            out.append(item)
            continue
        if "hooks" in item:
            cleaned = [
                h for h in item["hooks"]
                if not (isinstance(h, dict) and _is_our_command(h.get("command", "")))
            ]
            if cleaned:
                out.append({"matcher": item.get("matcher", ""), "hooks": cleaned})
            # else: drop wrapper entirely
        elif not _is_our_command(item.get("command", "")):
            out.append(item)
    return out


def _hook_entry(event: str, hook_script: str) -> dict:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": f"bash {hook_script} {event} {MARKER}",
            }
        ],
    }


# ── Pre-flight / doctor ──────────────────────────────────────────────

def _resolve_hook_path(cli_hook: str | None) -> Path:
    if cli_hook:
        return Path(cli_hook).expanduser().resolve()
    return (Path(__file__).resolve().parent / "hook.sh").resolve()


def _preflight(hook_path: Path, *, require_hook: bool = True) -> list[str]:
    """Return a list of human-readable problems (empty = all good)."""
    issues: list[str] = []

    # 1. Claude config dir
    claude_dir = SETTINGS_PATH.parent
    if not claude_dir.exists():
        issues.append(
            f"Claude config dir missing: {claude_dir} — is Claude Code installed?"
        )

    # 2. settings.json parses if it exists
    if SETTINGS_PATH.exists():
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                json.load(f)
        except json.JSONDecodeError as e:
            issues.append(f"{SETTINGS_PATH} is not valid JSON: {e}")
        except OSError as e:
            issues.append(f"cannot read {SETTINGS_PATH}: {e}")

    # 3. Hook script exists and is executable
    if require_hook:
        if not hook_path.exists():
            issues.append(f"hook script not found: {hook_path}")
        elif not hook_path.is_file():
            issues.append(f"hook script is not a regular file: {hook_path}")
        elif not os.access(hook_path, os.X_OK):
            issues.append(
                f"hook script is not executable: {hook_path} "
                f"(fix: chmod +x {hook_path})"
            )

    # 4. python3 is obviously present (we're running in it), skip.

    # 5. bash is on PATH (the hook command is `bash <path> <event>`)
    import shutil
    if shutil.which("bash") is None:
        issues.append("`bash` not found on PATH — the hook command will fail.")

    return issues


# ── Commands ─────────────────────────────────────────────────────────

def cmd_install(args: argparse.Namespace) -> int:
    hook_path = _resolve_hook_path(args.hook)
    problems = _preflight(hook_path)
    if problems and not args.force:
        _err("pre-flight checks failed:")
        for p in problems:
            _err(f"  - {p}")
        _err("re-run with --force to override (not recommended)")
        return 2

    with _settings_lock():
        settings = _load_settings()
        hooks = settings.setdefault("hooks", {})

        plan_add: list[str] = []
        plan_replace: list[str] = []
        plan_skip: list[str] = []
        desired_entry = lambda ev: _hook_entry(ev, str(hook_path))

        for ev in HOOK_EVENTS:
            existing = hooks.get(ev, [])
            if _has_our_hook(existing):
                # We're already installed for this event — update the command
                # (in case hook_path has moved) unless it's byte-for-byte equal.
                cleaned = _strip_our_hooks(existing)
                new_list = cleaned + [desired_entry(ev)]
                if new_list != existing:
                    hooks[ev] = new_list
                    plan_replace.append(ev)
                else:
                    plan_skip.append(ev)
            else:
                hooks[ev] = existing + [desired_entry(ev)] if existing else [desired_entry(ev)]
                plan_add.append(ev)

        settings["hooks"] = hooks

        _log(f"plan: +{len(plan_add)} add, ~{len(plan_replace)} replace, "
             f"={len(plan_skip)} unchanged (total events {len(HOOK_EVENTS)})")
        if plan_add:
            _log(f"  add:     {', '.join(plan_add)}")
        if plan_replace:
            _log(f"  replace: {', '.join(plan_replace)}")
        _log(f"  hook:    bash {hook_path} <Event> {MARKER}")
        _log(f"  target:  {SETTINGS_PATH}")

        if args.dry_run:
            _log("dry-run: settings.json NOT written")
            return 0

        bak = _backup_settings()
        if bak:
            _log(f"backup:  {bak}")
        try:
            _save_settings(settings)
        except Exception as e:
            _err(f"write failed ({e}) — rolling back")
            if bak and bak.exists():
                SETTINGS_PATH.write_bytes(bak.read_bytes())
                _err(f"restored from {bak}")
            return 3

    _log(f"ok: {len(plan_add) + len(plan_replace)} hooks written")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    problems = _preflight(Path("/dev/null"), require_hook=False)
    if problems:
        # Non-fatal for uninstall; we just print.
        for p in problems:
            _log(f"warn: {p}")

    with _settings_lock():
        if not SETTINGS_PATH.exists():
            _log(f"no settings.json at {SETTINGS_PATH} — nothing to do")
            return 0

        settings = _load_settings()
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            _log("settings.json has no hooks map — nothing to do")
            return 0

        removed_events: list[str] = []
        # Walk every event (not just HOOK_EVENTS) so we clean up entries the
        # user may have previously registered under names we no longer track.
        for ev, entry_list in list(hooks.items()):
            if not _has_our_hook(entry_list):
                continue
            cleaned = _strip_our_hooks(entry_list)
            if cleaned:
                hooks[ev] = cleaned
            else:
                del hooks[ev]
            removed_events.append(ev)

        if not removed_events:
            _log("no auditit hooks found in settings.json — nothing to do")
            return 0

        _log(f"plan: remove auditit hooks from {len(removed_events)} events")
        _log(f"  events: {', '.join(removed_events)}")
        _log(f"  target: {SETTINGS_PATH}")

        if args.dry_run:
            _log("dry-run: settings.json NOT written")
            return 0

        bak = _backup_settings()
        if bak:
            _log(f"backup: {bak}")
        try:
            _save_settings(settings)
        except Exception as e:
            _err(f"write failed ({e}) — rolling back")
            if bak and bak.exists():
                SETTINGS_PATH.write_bytes(bak.read_bytes())
                _err(f"restored from {bak}")
            return 3

    _log(f"ok: removed from {len(removed_events)} events")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    if not SETTINGS_PATH.exists():
        _log(f"no settings.json at {SETTINGS_PATH}")
        return 0
    settings = _load_settings()
    hooks = settings.get("hooks", {})
    installed = 0
    for ev in HOOK_EVENTS:
        present = _has_our_hook(hooks.get(ev, []))
        installed += int(present)
        marker = "[x]" if present else "[ ]"
        print(f"  {marker} {ev}")
    print(f"\n{installed}/{len(HOOK_EVENTS)} auditit hooks installed")

    # Also report any of our hooks under events not in our current list —
    # useful when upgrading to a version that drops an event.
    stale = [ev for ev in hooks if ev not in HOOK_EVENTS and _has_our_hook(hooks.get(ev, []))]
    if stale:
        print(f"\nstale (auditit hooks under events not in current list): {', '.join(stale)}")
        print("run `uninstall` to clean up")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    hook_path = _resolve_hook_path(args.hook)
    print(f"settings.json: {SETTINGS_PATH}")
    print(f"hook script:   {hook_path}")
    print(f"marker:        {MARKER}")
    print(f"events:        {len(HOOK_EVENTS)} (FileChanged skipped — needs explicit matcher)")
    print(f"fcntl lock:    {'yes' if HAVE_FCNTL else 'NO (concurrent installs unsafe)'}")
    print()
    problems = _preflight(hook_path)
    if problems:
        print("pre-flight: FAIL")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("pre-flight: OK")
    return 0


# ── Entry point ──────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Safe installer for the auditit Claude Code hook.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_i = sub.add_parser("install", help="Install auditit hook into ~/.claude/settings.json")
    p_i.add_argument("--hook", help="Path to hook.sh (default: hook.sh next to install.py)")
    p_i.add_argument("--dry-run", action="store_true", help="Show plan without writing")
    p_i.add_argument("--force", action="store_true", help="Ignore pre-flight failures")
    p_i.set_defaults(func=cmd_install)

    p_u = sub.add_parser("uninstall", help="Remove auditit hook from ~/.claude/settings.json")
    p_u.add_argument("--dry-run", action="store_true", help="Show plan without writing")
    p_u.set_defaults(func=cmd_uninstall)

    p_s = sub.add_parser("status", help="Show which events have auditit hooks")
    p_s.set_defaults(func=cmd_status)

    p_d = sub.add_parser("doctor", help="Run pre-flight checks only")
    p_d.add_argument("--hook", help="Path to hook.sh to validate")
    p_d.set_defaults(func=cmd_doctor)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
