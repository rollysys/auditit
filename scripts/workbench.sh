#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# workbench.sh — auditit tmux orchestrator
#
# Commands:
#   start   [--session-dir DIR]           Open audit pane, generate session settings
#   launch  --prompt STR [options]        Launch claude -p and audit it
#   stop                                  Close audit pane
#   replay  [audit.jsonl]                 Replay an existing audit log
#   status                                Show current session status
#   report  [--session-dir DIR]           Generate post-session report
#
# Key design: generates a per-session settings.json with audit hooks.
# The global ~/.claude/settings.json is NEVER modified.
# The audited claude process must use --settings to load the session settings.
#
# Requires: tmux, python3, rich (pip install rich)
# ─────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

AUDIT_BASE="${AUDITIT_DIR:-/tmp/auditit}"
STATE_FILE="$AUDIT_BASE/current.state"

_log()  { printf "\033[1;36m[auditit]\033[0m %s\n" "$*"; }
_err()  { printf "\033[1;31m[auditit]\033[0m %s\n" "$*" >&2; }
_warn() { printf "\033[1;33m[auditit]\033[0m %s\n" "$*"; }

_latest_session() {
    find "$AUDIT_BASE" -maxdepth 1 -type d -name "session-*" 2>/dev/null | sort | tail -1
}

_ensure_tmux() {
    if [ -z "${TMUX:-}" ]; then
        _err "需要在 tmux 会话中运行。请先启动 tmux。"
        exit 1
    fi
}

_ensure_tmux_installed() {
    if ! command -v tmux &>/dev/null; then
        _err "未找到 tmux。请先安装: brew install tmux"
        exit 1
    fi
}

_ensure_python() {
    if ! command -v python3 &>/dev/null; then
        _err "未找到 python3。"
        exit 1
    fi
}

_current_pane() {
    # $TMUX_PANE is set by tmux in every pane and inherited by child processes,
    # so it always identifies the pane where this script was invoked — unlike
    # `tmux display-message` which returns the tmux-active pane at call time.
    echo "${TMUX_PANE:-$(tmux display-message -p '#{pane_id}')}"
}

_kill_other_panes() {
    # Kill all panes in the current window except the active one
    local self
    self="$(_current_pane)"
    for pid in $(tmux list-panes -F '#{pane_id}'); do
        [ "$pid" = "$self" ] && continue
        tmux kill-pane -t "$pid" 2>/dev/null || true
    done
}

_write_state() {
    local session_dir="$1"
    local audit_pane="${2:-}"
    local auditor_pane="${3:-}"
    local worker_pane="${4:-}"
    mkdir -p "$AUDIT_BASE"
    cat > "$STATE_FILE" <<EOF
SESSION_DIR="${session_dir}"
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
AUDIT_PANE="${audit_pane}"
AUDITOR_PANE="${auditor_pane}"
WORKER_PANE="${worker_pane}"
EOF
}

_read_state() {
    if [ -f "$STATE_FILE" ]; then
        # shellcheck disable=SC1090
        source "$STATE_FILE"
    fi
}

_setup_config_dir() {
    # Create a temporary config directory that mirrors ~/.claude via symlinks,
    # but with our own settings.json containing audit hooks.
    # Usage: _setup_config_dir <session_settings_path>
    # Sets: CONFIG_DIR (caller reads this variable)
    local session_settings="$1"
    local real_claude_dir="${HOME}/.claude"
    CONFIG_DIR="$(dirname "$session_settings")/config"
    mkdir -p "$CONFIG_DIR"

    # Symlink everything from ~/.claude except settings.json
    for item in "$real_claude_dir"/*; do
        local name
        name="$(basename "$item")"
        [ "$name" = "settings.json" ] && continue
        [ -e "$CONFIG_DIR/$name" ] || ln -s "$item" "$CONFIG_DIR/$name"
    done

    # Also link hidden files (credentials etc.) — glob * doesn't match dotfiles
    # Use find to avoid zsh "no matches found" error when no dotfiles exist.
    find "$real_claude_dir" -maxdepth 1 -name '.*' -not -name '.' -not -name '..' | while read -r item; do
        local name
        name="$(basename "$item")"
        [ -e "$CONFIG_DIR/$name" ] || ln -s "$item" "$CONFIG_DIR/$name"
    done

    # Copy our audit-hooked settings.json into the config dir
    cp "$session_settings" "$CONFIG_DIR/settings.json"
    _log "Config dir: $CONFIG_DIR"
}

# ── Commands ──────────────────────────────────────────────────────────

cmd_start() {
    _ensure_python
    _ensure_tmux_installed

    local session_dir="" model="" repo_root="" base_url="" command=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --session-dir) session_dir="$2"; shift 2 ;;
            --model)       model="$2";       shift 2 ;;
            --repo-root)   repo_root="$2";   shift 2 ;;
            --base-url)    base_url="$2";    shift 2 ;;
            --command)     command="$2";      shift 2 ;;
            *) _err "未知参数: $1"; exit 1 ;;
        esac
    done

    # --model and --command are mutually exclusive
    if [ -n "$model" ] && [ -n "$command" ]; then
        _err "--model 和 --command 互斥，只能选一个"
        exit 1
    fi

    # Resolve repo_root (default = current pwd)
    if [ -z "$repo_root" ]; then
        repo_root="$(pwd)"
    fi
    if [ ! -d "$repo_root" ]; then
        _err "目录不存在: $repo_root"
        exit 1
    fi
    repo_root="$(cd "$repo_root" && pwd)"

    # Clear stale state from previous session (pane cleanup left to user via `stop`)
    rm -f "$STATE_FILE"

    # Create session directory
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    if [ -z "$session_dir" ]; then
        session_dir="$AUDIT_BASE/session-${ts}"
    fi
    mkdir -p "$session_dir"
    local audit_log="$session_dir/audit.jsonl"
    local session_settings="$session_dir/settings.json"

    _log "审计会话目录: $session_dir"
    _log "工作目录: $repo_root"

    # Generate per-session settings (global settings + audit hooks)
    python3 "$SCRIPT_DIR/gen_settings.py" generate --output "$session_settings"

    # Target: new tmux session (outside tmux) or new window (inside tmux)
    local sess_name="" win_target
    if [ -z "${TMUX:-}" ]; then
        sess_name="audit-${ts}"
        tmux new-session -d -s "$sess_name" -n "audit" -c "$repo_root"
        win_target="${sess_name}:audit"
    else
        tmux new-window -n "audit-${ts}" -c "$repo_root"
        win_target="$(tmux display-message -p '#{session_name}:#{window_index}')"
    fi

    tmux set-option -w -t "$win_target" pane-border-status top 2>/dev/null || true
    tmux set-option -w -t "$win_target" pane-border-format " #{pane_title} " 2>/dev/null || true

    # Auditor pane (left, full height of new window — only pane at this point)
    local auditor_pane
    auditor_pane=$(tmux list-panes -t "$win_target" -F '#{pane_id}' | head -1)
    tmux select-pane -t "$auditor_pane" -T "AUDITOR"
    # Fresh claude in repo_root, no --settings, no AUDITIT_TARGET → not audited
    tmux send-keys -t "$auditor_pane" "claude" Enter

    # Audit display pane (right, 50% width)
    local audit_pane
    audit_pane=$(tmux split-window -h -t "$auditor_pane" -l 50% -P -F '#{pane_id}' -c "$repo_root" \
        "python3 '$SCRIPT_DIR/display.py' --follow '$audit_log' --compact; echo ''; echo '[auditit 已完成。按回车关闭]'; read")
    tmux select-pane -t "$audit_pane" -T "AUDIT"

    local worker_pane=""
    if [ -n "$model" ] || [ -n "$command" ]; then
        # Create worker pane first (empty shell) — we'll send the command
        # after state is written, so hooks can resolve SESSION_DIR immediately.
        local worker_title="WORKER"
        [ -n "$model" ] && worker_title="WORKER (${model})"
        [ -n "$command" ] && worker_title="WORKER (command)"
        worker_pane=$(tmux split-window -v -t "$audit_pane" -l 50% -P -F '#{pane_id}' -c "$repo_root")
        tmux select-pane -t "$worker_pane" -T "$worker_title"
    fi

    # Write full state with all pane IDs BEFORE worker fires any hook.
    _write_state "$session_dir" "$audit_pane" "$auditor_pane" "$worker_pane"

    if [ -n "$model" ] && [ -n "$worker_pane" ]; then
        # --model mode: launch a single claude instance with --settings
        local claude_cmd="AUDITIT_TARGET=1"
        if [ -n "$base_url" ]; then
            claude_cmd+=" ANTHROPIC_BASE_URL=${base_url}"
        fi
        claude_cmd+=" claude --model ${model} --permission-mode bypassPermissions --settings ${session_settings}"
        tmux send-keys -t "$worker_pane" "$claude_cmd" Enter
        _log "布局: 左=AUDITOR | 右上=AUDIT | 右下=WORKER(${model})"
        [ -n "$base_url" ] && _log "API: ${base_url}"
    elif [ -n "$command" ] && [ -n "$worker_pane" ]; then
        # --command mode: set up CLAUDE_CONFIG_DIR so all child claude processes
        # inherit audit hooks via env var (no --settings needed per process).
        _setup_config_dir "$session_settings"
        local env_prefix="AUDITIT_TARGET=1 CLAUDE_CONFIG_DIR='${CONFIG_DIR}'"
        [ -n "$base_url" ] && env_prefix+=" ANTHROPIC_BASE_URL='${base_url}'"
        tmux send-keys -t "$worker_pane" "${env_prefix} ${command}" Enter
        _log "布局: 左=AUDITOR | 右上=AUDIT | 右下=WORKER(command)"
        _log "命令: ${command}"
        _log "CLAUDE_CONFIG_DIR=${CONFIG_DIR}"
        [ -n "$base_url" ] && _log "API: ${base_url}"
    else
        _log "布局: 左=AUDITOR | 右=AUDIT"
        _log "运行被审计的 claude 时加上环境变量和 --settings："
        local hint="AUDITIT_TARGET=1"
        [ -n "$base_url" ] && hint+=" ANTHROPIC_BASE_URL=${base_url}"
        hint+=" claude --settings $session_settings"
        _log "  $hint"
    fi

    tmux select-pane -t "$auditor_pane"

    _log "Audit session 已就绪 ✓"
    _log "完成后运行: bash $0 stop"

    # Hand over: attach new session (outside tmux) or switch to new window (inside tmux)
    if [ -n "$sess_name" ]; then
        tmux attach-session -t "$sess_name"
    else
        tmux select-window -t "$win_target"
    fi
}


cmd_stop() {
    _read_state

    if [ -z "${SESSION_DIR:-}" ]; then
        _log "无活跃审计会话"
        return 0
    fi

    # Kill audit display + worker panes by ID (precise — claude overwrites pane
    # titles, so title matching is unreliable). Leave the auditor pane alive
    # since the user may still want to review the log, and `stop` itself may
    # be running from inside that pane.
    local killed=0
    for pid in "${AUDIT_PANE:-}" "${WORKER_PANE:-}"; do
        if [ -n "$pid" ] && tmux kill-pane -t "$pid" 2>/dev/null; then
            killed=$((killed + 1))
        fi
    done
    _log "已关闭 ${killed} 个 auditit pane（auditor pane 保留）"

    rm -f "$STATE_FILE"
    _log "Audit 会话已结束"

    if [ -d "${SESSION_DIR:-}" ]; then
        _log "运行以下命令生成分析报告："
        _log "  python3 $SCRIPT_DIR/analyzer.py --session $SESSION_DIR"
    fi
}


cmd_replay() {
    _ensure_tmux
    _ensure_python

    local log_path="${1:-}"
    if [ -z "$log_path" ]; then
        local latest
        latest="$(_latest_session)"
        log_path="${latest:+${latest}/audit.jsonl}"
        if [ -z "$log_path" ]; then
            _err "用法: $0 replay <audit.jsonl>"
            exit 1
        fi
        _log "使用最近的日志: $log_path"
    fi

    if [ ! -f "$log_path" ]; then
        _err "文件不存在: $log_path"
        exit 1
    fi

    local auditor_pane
    auditor_pane="$(_current_pane)"

    local replay_pane
    replay_pane=$(tmux split-window -h -l 66% -P -F '#{pane_id}' \
        "python3 '$SCRIPT_DIR/display.py' --replay '$log_path'; echo ''; echo '[回放完成。按回车关闭]'; read")

    tmux set-option -w pane-border-status top 2>/dev/null || true
    tmux select-pane -t "$replay_pane" -T "REPLAY"
    tmux select-pane -t "$auditor_pane"

    _log "正在回放: $log_path"
}


cmd_launch() {
    _ensure_tmux
    _ensure_python
    _kill_other_panes

    local prompt="" repo_root="." model="sonnet" max_turns="100" session_dir="" base_url=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --prompt)      prompt="$2";      shift 2 ;;
            --repo-root)   repo_root="$2";   shift 2 ;;
            --model)       model="$2";       shift 2 ;;
            --max-turns)   max_turns="$2";   shift 2 ;;
            --session-dir) session_dir="$2"; shift 2 ;;
            --base-url)    base_url="$2";    shift 2 ;;
            *) _err "未知参数: $1"; exit 1 ;;
        esac
    done

    if [ -z "$prompt" ]; then
        _err "必须提供 --prompt"
        exit 1
    fi

    # Create session directory
    local ts
    ts=$(date +%Y%m%d-%H%M%S)
    if [ -z "$session_dir" ]; then
        session_dir="$AUDIT_BASE/session-${ts}"
    fi
    mkdir -p "$session_dir"
    local audit_log="$session_dir/audit.jsonl"
    local session_settings="$session_dir/settings.json"

    _log "审计会话目录: $session_dir"

    # Write state BEFORE starting claude-p so hooks can resolve SESSION_DIR immediately
    _write_state "$session_dir" "pending"

    # Generate session-specific settings (global settings + audit hooks)
    python3 "$SCRIPT_DIR/gen_settings.py" generate --output "$session_settings"

    # Save prompt to file to avoid shell injection via special characters
    local prompt_file="$session_dir/prompt.txt"
    printf '%s' "$prompt" > "$prompt_file"

    local origin_pane
    origin_pane="$(_current_pane)"

    # Single audit pane (right) — agent runs in background
    # After display exits (SessionEnd), open output pane below, then wait
    local audit_pane
    audit_pane=$(tmux split-window -h -l 66% -P -F '#{pane_id}' \
        "python3 '$SCRIPT_DIR/display.py' --follow '$audit_log'; \
         echo '[audit 已完成。按回车关闭]'; read")

    tmux set-option -w pane-border-status top 2>/dev/null || true
    tmux set-option -w pane-border-format " #{pane_title} " 2>/dev/null || true
    tmux select-pane -t "$audit_pane" -T "AUDIT"

    _write_state "$session_dir" "$audit_pane"

    # Launch claude -p in background — hooks write to audit.jsonl, display tails it
    local real_repo_root
    real_repo_root="$(realpath "$repo_root")"
    (
        cd "$real_repo_root"
        export AUDITIT_TARGET=1
        [ -n "$base_url" ] && export ANTHROPIC_BASE_URL="$base_url"
        claude -p \
            --model "$model" \
            --max-turns "$max_turns" \
            --permission-mode bypassPermissions \
            --no-chrome \
            --settings "$session_settings" \
            -- "$(cat "$prompt_file")" \
            > "$session_dir/output.txt" 2>&1
    ) &
    local agent_pid=$!
    echo "AGENT_PID=${agent_pid}" >> "$STATE_FILE"

    # Focus back to origin
    tmux select-pane -t "$origin_pane"

    _log "已启动 claude -p (model=${model}, max-turns=${max_turns}, pid=${agent_pid})"
    [ -n "$base_url" ] && _log "API: ${base_url}"
    _log "Agent 输出将保存至: $session_dir/output.txt"
    _log "完成后运行: bash $0 stop"
}


cmd_status() {
    if [ -f "$STATE_FILE" ]; then
        _read_state
        _log "当前会话目录: ${SESSION_DIR:-未知}"
        _log "Audit pane: ${AUDIT_PANE:-未知}"
        _log "启动时间: ${STARTED_AT:-未知}"
        local session_settings="${SESSION_DIR:-}/settings.json"
        if [ -f "$session_settings" ]; then
            python3 "$SCRIPT_DIR/gen_settings.py" status --settings "$session_settings"
        fi
    else
        _log "无活跃审计会话"
    fi
}


cmd_report() {
    _ensure_python

    local session_dir=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --session-dir) session_dir="$2"; shift 2 ;;
            *) _err "未知参数: $1"; exit 1 ;;
        esac
    done

    if [ -z "$session_dir" ]; then
        _read_state
        session_dir="${SESSION_DIR:-}"
    fi

    if [ -z "$session_dir" ] || [ ! -d "$session_dir" ]; then
        session_dir="$(_latest_session)"
        if [ -z "$session_dir" ]; then
            _err "未找到会话目录。使用 --session-dir 指定路径。"
            exit 1
        fi
        _log "使用最近会话: $session_dir"
    fi

    python3 "$SCRIPT_DIR/analyzer.py" --session "$session_dir"
}


# ── Dispatch ──────────────────────────────────────────────────────────

case "${1:-help}" in
    start)  shift; cmd_start  "$@" ;;
    launch) shift; cmd_launch "$@" ;;
    stop)   cmd_stop ;;
    replay) shift; cmd_replay "$@" ;;
    status) cmd_status ;;
    report) shift; cmd_report "$@" ;;
    help|*)
        cat <<'USAGE'
auditit — Claude Code Session Audit Monitor

Commands:
  start   [--model MODEL | --command CMD] [--base-url URL] [--repo-root DIR] [--session-dir DIR]
      Open audit pane. --model and --command are mutually exclusive.
      --model:    opens interactive claude worker pane with that model.
      --command:  runs an arbitrary command in the worker pane; all claude
                  processes spawned by the command are audited automatically
                  via CLAUDE_CONFIG_DIR (no --settings needed per process).
      --base-url: sets ANTHROPIC_BASE_URL for the worker process.
      Layout: left=auditor(you) | right-top=AUDIT | right-bottom=WORKER

  launch  --prompt STR [--repo-root DIR] [--model MODEL] [--base-url URL] [--max-turns N]
      Run claude -p in background, single AUDIT pane shows progress.
      --base-url sets ANTHROPIC_BASE_URL for the claude -p process.
      Agent output saved to session-dir/output.txt.

  stop
      Close audit pane.

  replay  [audit.jsonl]
      Replay an existing audit log offline.

  status
      Show current session status.

  report  [--session-dir DIR]
      Generate post-session Markdown report.

Environment:
  AUDITIT_DIR    Override base directory (default: /tmp/auditit)
USAGE
        ;;
esac
