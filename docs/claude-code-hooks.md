# Claude Code Hooks — Authoritative Reference

Source: https://code.claude.com/docs/en/hooks
(previously: https://docs.claude.com/en/docs/claude-code/hooks, 301 → code.claude.com)
Fetched: 2026-04-11

This is a local snapshot used by `install.py` to drive the `HOOK_EVENTS`
registration list. When Claude Code adds or removes events, refresh this file
and update `install.py`.

## Complete Hook Event List (26 events)

| Event Name | When It Fires | Key Fields | Matcher Support |
|---|---|---|---|
| `SessionStart` | Session begins or resumes | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `source`, `model`, optional `agent_type` | Yes: `startup`, `resume`, `clear`, `compact` |
| `InstructionsLoaded` | CLAUDE.md or `.claude/rules/*.md` loaded into context | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `file_path`, `memory_type`, `load_reason`, optional `globs`, `trigger_file_path`, `parent_file_path` | Yes: `session_start`, `nested_traversal`, `path_glob_match`, `include`, `compact` |
| `UserPromptSubmit` | User submits a prompt, before Claude processes it | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `prompt` | No matcher; always fires |
| `PreToolUse` | Before a tool call executes; can block it | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id` | Yes: tool names (`Bash`, `Edit`, `Write`, `Read`, `Glob`, `Grep`, `Agent`, `WebFetch`, `WebSearch`, `AskUserQuestion`, `ExitPlanMode`, MCP tools) |
| `PermissionRequest` | Permission dialog appears | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, optional `permission_suggestions` | Yes: tool names (same as PreToolUse) |
| `PermissionDenied` | Tool call denied by auto-mode classifier | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `reason` | Yes: tool names (same as PreToolUse) |
| `PostToolUse` | Tool call succeeds | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_response`, `tool_use_id` | Yes: tool names (same as PreToolUse) |
| `PostToolUseFailure` | Tool call fails | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, `error`, optional `is_interrupt` | Yes: tool names (same as PreToolUse) |
| `Notification` | Claude Code emits a notification | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `message`, optional `title`, `notification_type` | Yes: `permission_prompt`, `idle_prompt`, `auth_success`, `elicitation_dialog` |
| `SubagentStart` | A subagent is spawned | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `agent_id`, `agent_type` | Yes: agent type names (`Bash`, `Explore`, `Plan`, custom) |
| `SubagentStop` | A subagent finishes | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message` | Yes: agent type names |
| `TaskCreated` | Task being created via `TaskCreate` | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `task_id`, `task_subject`, optional `task_description`, `teammate_name`, `team_name` | No matcher; always fires |
| `TaskCompleted` | Task being marked as completed | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `task_id`, `task_subject`, optional `task_description`, `teammate_name`, `team_name` | No matcher; always fires |
| `Stop` | Claude finishes responding | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name` | No matcher; always fires |
| `StopFailure` | Turn ends due to API error | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `error_type` | Yes: `rate_limit`, `authentication_failed`, `billing_error`, `invalid_request`, `server_error`, `max_output_tokens`, `unknown` |
| `TeammateIdle` | Agent team teammate is about to go idle | `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `teammate_name`, optional `team_name` | No matcher; always fires |
| `ConfigChange` | A configuration file changes during a session | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `source` | Yes: `user_settings`, `project_settings`, `local_settings`, `policy_settings`, `skills` |
| `CwdChanged` | Working directory changes (e.g., `cd` command) | `session_id`, `transcript_path`, `cwd`, `hook_event_name` | No matcher; always fires |
| `FileChanged` | Watched file changes on disk | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `file_path`, `change_type` | Yes: literal filenames/patterns (required to be useful) |
| `WorktreeCreate` | Worktree created via `--worktree` or `isolation: "worktree"` | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `worktree_id`, `parent_path`, `worktree_name` | No matcher; always fires |
| `WorktreeRemove` | Worktree removed | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `worktree_id`, `worktree_path` | No matcher; always fires |
| `PreCompact` | Before context compaction | `session_id`, `transcript_path`, `cwd`, `hook_event_name` | Yes: `manual`, `auto` |
| `PostCompact` | After context compaction completes | `session_id`, `transcript_path`, `cwd`, `hook_event_name` | Yes: `manual`, `auto` |
| `Elicitation` | MCP server requests user input during a tool call | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `mcp_server_name`, `request` | Yes: MCP server names |
| `ElicitationResult` | After user responds to MCP elicitation | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `mcp_server_name`, `response` | Yes: MCP server names |
| `SessionEnd` | Session terminates | `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `reason` | Yes: `clear`, `resume`, `logout`, `prompt_input_exit`, `bypass_permissions_disabled`, `other` |

## Hook Configuration Structure

All hooks use three levels of nesting:

```json
{
  "hooks": {
    "EventName": [
      {
        "matcher": "MatcherValue",
        "hooks": [
          {
            "type": "command|http|prompt|agent",
            "command": "...",
            "if": "optional permission-rule",
            "timeout": 600,
            "statusMessage": "optional spinner message",
            "once": false
          }
        ]
      }
    ]
  }
}
```

**Common handler fields:**

- `type` (required): `"command"`, `"http"`, `"prompt"`, or `"agent"`
- `if` (optional, tool events only): permission-rule syntax
- `timeout` (optional): seconds; defaults are 600 command, 30 prompt, 60 agent
- `statusMessage` (optional): spinner message
- `once` (optional, skills only): fire once per session then remove

## auditit Registration Policy

`install.py` registers all hook events **except**:

- `FileChanged` — requires a filename pattern to be useful. An empty matcher has
  undefined behavior per the docs, so we skip it. Users who want file-watch audit
  can register it themselves with an explicit pattern.

All other 25 events are registered with `"matcher": ""` (match-all where
supported; ignored where matcher is not supported).

## Session ID Field Name

All events carry `session_id` (snake_case). The UI layer must use this name;
`sessionId` (camelCase) does not appear in any event.
