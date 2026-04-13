# auditit

**全局 Claude Code 会话审计系统。**

一个常驻旁观者: 通过 Claude Code 的 hook 机制被动采集每次会话的 prompt、
工具调用、sub-agent、token、成本和上下文压力, 按日期 / session 分目录落盘,
供 Web UI 实时查看, 或让另一个 Claude 按 session id 读日志自己做复盘。

```
~/.claude/settings.json           ──┐  install.py 一次性注入
                                    │  之后所有 claude 会话自动被审
┌───────────────────────────────────┴──┐
│  hook.sh   (每次 hook 事件触发)       │
│    ↓                                  │
│  ~/.claude-audit/YYYY-MM-DD/<sid>/    │
│    audit.jsonl                        │  追加写
│    audit.jsonl.gz   (SessionEnd 后)   │  原子 gzip + 行数校验
│    summary.json                       │  model/turns/cost/ctx_peak
│    metadata.json                      │  首次 server 解析后缓存
└───────────────────────────────────────┘
              │
              ▼
       server.py  :8765         SSE 实时 tail + REST
              │
              ▼
       web/index.html          Sessions | Memory 双视图
```

## 它解决什么问题

一旦装好, 你得到:

- **所有 Claude Code 会话的不可变审计流**: prompt、每个工具的输入输出、
  sub-agent 树结构、token 用量、按官方定价算出的美元成本、整会话的上下文
  压力峰值 (`ctx_peak_tokens`, 含 cache read + cache creation)。
- **一个本地 Web UI** 实时 tail 当前会话、回放历史会话、按日期分组浏览、
  一键删除 stuck session。
- **一个 Memory 视图** 聚合每个项目的 `CLAUDE.md` / `.claude/CLAUDE.md` /
  `~/.claude/projects/<encoded>/memory/*.md` 和全局 `~/.claude/CLAUDE.md`,
  方便检查 Claude 在各项目下拿到了什么上下文。
- **让 Claude 自己审计自己**: 审计日志是一份平铺 JSONL, 把 session id 或
  路径贴给任一 Claude 会话, 它能直接读、自己总结问题 (成本异常、工具重复、
  sub-agent 委派是否合理、context 压力峰值等)。

## 优点

- **零工作流扭曲**: 装完就忘, 所有 `claude` 命令透明被审, 无需包装器、
  无需 env var、无需 opt-in。
- **天然并发安全**: 按 `session_id` 分目录, 多会话并发各写各的文件, 写路径
  零锁零状态文件。
- **纯标准库**: server / hook / installer 全 stdlib, 克隆即跑, 无 pip/venv。
- **被动只读**: hook 从不阻塞也不修改 Claude 行为, 只追加 JSONL; 最坏情况
  下 hook 崩掉也只影响审计, 不影响 Claude 本身工作 (但见"局限"第 2 条)。
- **安全安装**: 写 settings.json 前自动带时间戳备份, 原子写入, `fcntl`
  独占锁, 支持 `--dry-run`; 卸载靠 marker 识别, 即使仓库换路径也能卸干净。
- **历史数据不用回写**: 定价和 context window 尺寸存在 `server.py` 的
  `PRICING` / `CTX_WINDOW` 表里, `/api/*` 每次请求现算, 更新表格会自动
  应用到全部历史 session, 不需要重写 summary.json。

## 局限

- **仅限本机**: 审计流全部落在 `~/.claude-audit/`, 没有远程聚合/多机同步。
- **hook 写坏会阻塞所有 Claude 会话**: 全局 hook 的代价。`hook.sh` 里的
  `python3 -c '...'` 块用 bash 单引号包裹, **任何一个 `'` 都会让 bash 语法
  出错、PreToolUse 全局卡死**。修复方法: 在普通终端 (不是 claude 内部!) 跑
  `python3 install.py uninstall`, 再修 bug, 再 install。
- **SessionEnd 数据极少**: Claude Code 的 SessionEnd hook 只给 `reason`,
  所有 cost / turns / usage 都要从 `transcript_path` 解析。`hook.sh` 已经
  处理了, 但意味着只有正常结束的会话才有 summary.json; 被 `kill -9` 的会话
  只留一个未压缩的 `audit.jsonl`。
- **sub-agent 目录需要父会话 SessionEnd 时切片生成**: 如果父会话没有正常
  结束, sub-agent 的独立视图 (`<parent>__agent__<agent_id>/`) 不会生成。
- **无鉴权**: `server.py` 绑 127.0.0.1, 没鉴权; 共享机器不要暴露到公网。
- **定价表需要人肉维护**: 新模型上线时手动更新 `docs/claude-pricing.md` 和
  `server.py::PRICING`。

## 前置依赖

- Claude Code (已登录, `~/.claude/` 存在)
- `python3` 3.8+ (标准库即可)
- `bash`

缺任一项 `python3 install.py doctor` 会直接报错退出。

## 安装

```bash
git clone https://github.com/rollysys/auditit.git ~/src/auditit
cd ~/src/auditit
python3 install.py doctor      # pre-flight 检查
python3 install.py install     # 写入 ~/.claude/settings.json
python3 server.py &            # 常驻后台也行, 或单开终端前台
open http://127.0.0.1:8765
```

install.py 做的事:

1. pre-flight: Claude 配置目录、settings.json JSON 合法性、`hook.sh` 可执行位、bash 可用
2. 备份当前 settings.json 到带时间戳的 `.bak`
3. `tempfile` + `os.replace` 原子写入
4. 写前加 `fcntl` 独占锁, 防止并发 install
5. 每条 hook 命令里埋 `# auditit` marker, 卸载/查状态靠 marker 识别 ——
   仓库换路径依旧能卸干净

**新开的 Claude 会话才会被审**; 已经在跑的会话不会回溯加载 hook。

## 使用

### A. Web UI 查看

```bash
python3 server.py
```

- **Sessions Tab**: 左侧按日期分组, 每条显示时间 / cwd / 提示 / 模型 / turns / cost /
  时长 / 上下文压力 (70% 黄, 85% 红)。点开事件流实时 tail (活跃会话) 或一次性
  回放 (已 gzip)。sub-agent 缩进嵌套展示。右上 **📄 审计路径** 按钮一键拷贝
  "请审计以下 session + 路径" 到剪贴板, **✕** 按钮删 session。
- **Memory Tab**: 每个项目列出它能看到的全部记忆文件 —— 全局 `CLAUDE.md` /
  项目 `CLAUDE.md` / `.claude/CLAUDE.md` / `~/.claude/projects/<encoded>/memory/*.md`,
  点开看内容, 方便检查 Claude 在某项目下拿到了什么上下文。

### B. 让 Claude 按 session id 自动读日志

把下面这段加到你的全局 `~/.claude/CLAUDE.md` (本仓库不会自动写):

````markdown
## Claude Code session audit logs (auditit)

所有 Claude Code 会话被 auditit 全局 hook 采集, 固定落盘路径约定:

- `~/.claude-audit/YYYY-MM-DD/<session_id>/audit.jsonl` (进行中)
- `~/.claude-audit/YYYY-MM-DD/<session_id>/audit.jsonl.gz` (SessionEnd 后压缩)
- 同目录下 `summary.json` (cost/turns/duration/usage/ctx_peak_tokens) 和
  `metadata.json` (prompt/model/cwd)
- Sub-agent 独立目录命名 `<parent_sid>__agent__<agent_id>/`, 多层嵌套
  `<p>__agent__<c>__agent__<gc>/`, 含 `meta.json` (parent_session_id /
  agent_type / description)

当用户给一个 session id (或其前缀) 并要求审计 / 分析 / 复盘会话时:

1. 用 Glob 定位: `~/.claude-audit/*/<sid-prefix>*/audit.jsonl*`
2. `.gz` 用 `gunzip -c <path>` 或 `zcat` 读取, `.jsonl` 直接 Read
3. 需要统计时参考 `summary.json`, 需要 prompt/cwd 参考 `metadata.json`
4. 要看 sub-agent 时 Glob `~/.claude-audit/*/<parent_sid>__agent__*/`

session log 是不可变的审计证据。**绝对不允许修改 `~/.claude-audit/` 下任何
文件** (audit.jsonl / .gz / summary.json / metadata.json / meta.json), 只能读。
要删除 session 必须通过 auditit Web UI 或 `DELETE /api/sessions/...`, 禁止
直接 `rm`。
````

加好之后, 在任意 Claude 会话里说 **"审计一下 097a326c 这个 session"**, Claude
就会自己 Glob 到文件、`gunzip -c` 读出来、给你分析, 无需人工拷贝路径。

### 卸载

```bash
python3 install.py uninstall           # 真卸
python3 install.py uninstall --dry-run # 预览
python3 install.py status              # 看哪些事件被注册
```

只动 settings.json 里含 `# auditit` marker 的条目, 不碰你自定义的其他 hook。
`.bak` 文件保留, 手动删即可。

## 数据模型

### 目录结构

```
~/.claude-audit/
├── 2026-04-11/
│   ├── 097a326c-d56a-46f7-ac63-e6fb0cbfab29/          # 主会话
│   │   ├── audit.jsonl                                # 活跃 / 未压缩
│   │   ├── audit.jsonl.gz                             # SessionEnd 后
│   │   ├── summary.json                               # model/turns/cost/ctx_peak
│   │   └── metadata.json                              # server 首次解析后缓存
│   ├── 097a326c-..__agent__code-researcher-abc/        # sub-agent 切片
│   │   ├── audit.jsonl.gz
│   │   ├── summary.json
│   │   └── meta.json                                  # parent_session_id / description
│   └── ...
```

### 事件格式

每行一条 JSONL:

```json
{
  "ts": "2026-04-11T13:18:07Z",
  "event": "PreToolUse",
  "data": { /* 原封不动的 Claude Code hook data */ }
}
```

支持 25 个 hook 事件 (`FileChanged` 需要显式 matcher, 跳过), 权威定义见
[`docs/claude-code-hooks.md`](docs/claude-code-hooks.md)。

## 仓库结构

```
auditit/
├── README.md
├── LICENSE
├── install.py                  # 独立安装器: install / uninstall / status / doctor
├── hook.sh                     # 全局 hook: 写 audit.jsonl + SessionEnd 原子 gzip + sub-agent 切片
├── server.py                   # HTTP :8765, SSE tail, Sessions/Memory REST
├── web/
│   └── index.html              # 单文件 Web UI, Sessions | Memory 双 Tab
└── docs/
    ├── claude-code-hooks.md    # 官方 hooks 事件权威参考
    ├── claude-pricing.md       # per-MTok 定价快照
    └── claude-context-windows.md  # 各模型上下文窗口尺寸
```

## 关键设计决策

### 为什么按 session_id 分目录, 而不是按进程/终端门禁

旧 workbench 架构用 `AUDITIT_TARGET=1` 环境变量决定哪个进程写日志, 多 claude
并发时要维护 state file 防串写。新架构反过来: **所有 claude 会话都写, 靠
`session_id` 分目录天然隔离**, 写路径零锁零状态。

### 为什么 SessionEnd 要解析 transcript

Claude Code 的 SessionEnd hook data 字段只有 `session_id / transcript_path /
cwd / hook_event_name / reason` 五个 —— **没有** `usage / cost / num_turns`。
所有成本/用量数据必须从 `transcript_path` 指向的 jsonl 走一遍 assistant 消息
抽 `usage` 块。顺带算出 `ctx_peak_tokens = max(input + cache_read + cache_creation)`
表示整会话上下文压力峰值 (auto-compact 会重置运行时计数, 必须取 max 才准)。

### 为什么 gzip 用 tempfile + rename

旧版 `gzip.open(gz, "wb")` 写完删原 jsonl, 一旦中途异常且已写入部分数据, 原
jsonl 会被误删。改为: 写 `.gz.tmp` → 重新打开校验行数 → 行数匹配才
`os.replace` + 删原。任何一步异常都不破坏现场。

### 为什么 install.py 用 marker 而不是路径匹配

仓库搬家后路径匹配失效, marker (`# auditit`, bash 注释, 运行时无害) 永远跟着
我们走。

## License

MIT. 详见 [LICENSE](LICENSE)。
