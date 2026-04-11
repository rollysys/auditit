# auditit

**全局 Claude Code 会话审计系统。**

一个常驻的旁观者：通过 Claude Code 的 hook 机制被动采集每一次会话的 prompt、
工具调用、sub-agent、token 与成本，按日期/session 分层落盘，供 Web UI 实时查
看或离线交给 Claude 自己分析。

```
~/.claude/settings.json           ──┐  install.py 一次性注入
                                    │  所有 Claude 会话自动被审
┌───────────────────────────────────┴──┐
│  hook.sh  (每次 hook 事件触发)        │
│    ↓                                  │
│  ~/.claude-audit/YYYY-MM-DD/<sid>/    │
│    audit.jsonl                        │  追加写
│    audit.jsonl.gz    (SessionEnd 后)   │  原子压缩 + 行数校验
│    summary.json                       │  SessionEnd 时生成
│    metadata.json                      │  server 首次解析后缓存
└───────────────────────────────────────┘
              │
              ▼
       server.py  :8765         SSE 实时 tail
              │
              ▼
       web/index.html          按日期分组 | 工具调用折叠 | 审计路径一键拷贝
```

## 设计原则

| 原则 | 说明 |
|---|---|
| **被动只读** | hook 从不阻塞、不修改 Claude 行为，只追加日志 |
| **天然隔离** | 按 `session_id` 分目录，多会话并发无需锁 |
| **零工作流扭曲** | install 一次之后，所有 `claude` 命令自动被审 |
| **单一事实源** | 所有视图（live / replay / 离线分析）都从同一份 `audit.jsonl` 派生 |
| **安全可回滚** | install.py 写 settings.json 前自动备份 + 原子写 + rollback |
| **手动驱动分析** | 需要审计时把 `~/.claude-audit/<date>/<sid>/audit.jsonl` 路径贴给 Claude 自己解读 |

## 前置依赖

- Claude Code (已登录，`~/.claude/` 存在)
- `python3` (标准库即可，无第三方依赖)
- `bash`

缺任一项 `install.py doctor` 会直接报错。

## 安装

```bash
git clone <this-repo> ~/src/auditit
cd ~/src/auditit
python3 install.py doctor     # pre-flight 检查
python3 install.py install    # 写入 ~/.claude/settings.json
```

install.py 会：
1. 运行 pre-flight（Claude 配置目录、settings.json 合法性、hook.sh 可执行、bash 可用）
2. 把当前 `~/.claude/settings.json` 备份到带时间戳的 `.bak` 文件
3. 用 tmp + `os.replace` 原子写入新 settings.json
4. 写入前加 `fcntl` 独占锁，防止两个 installer 并发
5. 每条 hook 命令里埋入 marker `# auditit`，后续 `uninstall`/`status` 靠
   marker 识别 —— 即使你把仓库移到别的路径，卸载仍然有效

## 使用

### 查看

启动 Web UI：

```bash
python3 server.py
# → http://127.0.0.1:8765
```

- 左侧按日期分组列出所有 session（点一下即可展开事件流）
- 右侧顶部有 **📄 审计路径** 按钮：一键拷贝「请审计以下 session：\n审计日志：`~/.claude-audit/.../audit.jsonl`」到剪贴板，直接粘给另一个 Claude 做离线分析
- **📋 Resume** 按钮：拷贝 `cd <cwd> && claude --resume <sid>` 方便恢复会话
- 活跃 session 走 SSE live tail；已 gzip 的历史 session 一次性回放

### 离线交给 Claude 分析

最简工作流：

1. 在 Web UI 上挑一个 session，点 **📄 审计路径**
2. 粘贴到任意 Claude 会话
3. Claude 读 jsonl 做针对性分析（成本优化、工具重复、sub-agent 委派是否合理等）

不做预设模板 —— 每次分析目标不同，交给 LLM 临场决定比固定 Markdown 报告更灵活。

### 卸载

```bash
python3 install.py uninstall           # 真卸载
python3 install.py uninstall --dry-run # 预览
python3 install.py status              # 查看哪些事件被注册
```

卸载只动 settings.json 里含 marker 的条目，不会碰你自定义的其他 hook。带时间
戳的 `.bak` 文件保留，需要时手动删。

## 数据模型

### 目录结构

```
~/.claude-audit/
├── 2026-04-11/
│   ├── 097a326c-d56a-46f7-ac63-e6fb0cbfab29/
│   │   ├── audit.jsonl          # 活跃会话 / 未压缩
│   │   ├── audit.jsonl.gz       # SessionEnd 后原子压缩
│   │   ├── summary.json         # SessionEnd 生成：cost, turns, duration
│   │   └── metadata.json        # server 首次读取时缓存 prompt/model/cwd
│   └── ...
├── 2026-04-12/
│   └── ...
```

### 事件格式

每行一条 JSONL，envelope：

```json
{
  "ts": "2026-04-11T13:18:07Z",
  "event": "PreToolUse",
  "data": { /* 原封不动的 Claude Code hook data */ }
}
```

支持的 event 共 25 种，权威定义见 [`docs/claude-code-hooks.md`](docs/claude-code-hooks.md)
（由 Anthropic 官方 hooks 文档整理）。

## 仓库结构

```
auditit/
├── README.md
├── LICENSE
├── install.py             # 独立安装器：install / uninstall / status / doctor
├── hook.sh                # 全局 hook 脚本：写 audit.jsonl + SessionEnd 原子压缩
├── server.py              # HTTP :8765, SSE tail, 零第三方依赖
├── web/
│   └── index.html         # 单文件 Web UI
└── docs/
    └── claude-code-hooks.md  # 官方 hooks 事件权威参考
```

## 设计决策说明

### 为什么不用全局 `AUDITIT_TARGET` 环境变量门禁？

之前的 workbench 架构用 `AUDITIT_TARGET=1` 做进程级开关，只审指定进程。该
方案在多 claude 并发时会串日志（依赖全局 state file）。新架构改为"所有
claude 会话都写，按 `session_id` 分目录隔离" —— 反正每个会话写自己的文件，
没有竞争。

### 为什么 `hook.sh` 用 `grep -o` 提取 session_id 而不是 python？

hook 在每次工具调用前后各触发一次，对性能敏感。`grep -o` + `sed` 在 bash 内
完成，比启动 python 解释器快一个量级。Claude Code hook 事件里 `session_id`
是外层首字段，grep 取第一个 match 足够稳。

### 为什么 SessionEnd gzip 用 tmpfile + rename？

以前是直接 `gzip.open(gz, "wb")` 写完再删原 jsonl。若 gzip 中途异常但已写入
部分数据，**原 jsonl 会被误删**。改为：写 `.gz.tmp` → 重新打开计算行数 →
行数匹配才 `os.replace` + 删原文件。任何步骤异常都不会破坏现场。

### 为什么 `install.py` 用 marker (`# auditit`) 而不是路径匹配？

如果移动仓库（比如从 `~/auditit` 搬到 `~/src/auditit`），旧条目路径不再匹配，
uninstall 就识别不出来。marker 是 bash 注释，对运行时无影响，但作为 settings.json
字符串里的锚点永远跟着我们走。

## License

MIT. 详见 [LICENSE](LICENSE).
