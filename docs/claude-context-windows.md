# Claude Model Context Windows — Authoritative Reference

Source: https://platform.claude.com/docs/en/about-claude/models
Fetched: 2026-04-11

Local snapshot used by `server.py`'s `CTX_WINDOW` map to compute context
pressure. Refresh this file when Anthropic adds or changes a model, and
mirror the changes into `CTX_WINDOW` in `server.py`.

## Context windows (tokens)

| Model              | API ID             | Context |
|--------------------|--------------------|---------|
| Claude Opus 4.6    | `claude-opus-4-6`  | **1M**  |
| Claude Sonnet 4.6  | `claude-sonnet-4-6`| **1M**  |
| Claude Haiku 4.5   | `claude-haiku-4-5` | 200k    |
| Claude Sonnet 4.5  | `claude-sonnet-4-5`| 200k    |
| Claude Opus 4.5    | `claude-opus-4-5`  | 200k    |
| Claude Opus 4.1    | `claude-opus-4-1`  | 200k    |
| Claude Sonnet 4    | `claude-sonnet-4`  | 200k    |
| Claude Opus 4      | `claude-opus-4`    | 200k    |
| Claude Haiku 3.5   | `claude-haiku-3-5` | 200k    |
| Claude Haiku 3     | `claude-haiku-3`   | 200k    |

## How auditit computes context pressure

From each assistant entry in a Claude Code transcript:

```python
ctx_tokens = usage.input_tokens
           + usage.cache_read_input_tokens
           + usage.cache_creation_input_tokens
```

This represents the total prompt size the model saw on that request. The
**peak** across all assistant messages in the session is stored as
`ctx_peak_tokens` in `summary.json`. Context pressure at serve time is:

```
ctx_peak_pct = ctx_peak_tokens / CTX_WINDOW[model]
```

Thresholds used by the Web UI:
- `< 70%`: green (healthy)
- `70% – 85%`: yellow (consider compacting)
- `≥ 85%`: red (hot — /compact recommended)

## Caveats

- Claude Code auto-compacts at around 80-90% full. After a compact, the
  next assistant usage resets to a much lower number. **Peak tokens ≠
  current tokens** — we report peak because it reflects "how close this
  session got to the limit", which is the actionable signal.
- Multi-modal (image/vision) tokens are reflected in `input_tokens` by
  Claude Code, so no special handling is needed.
- Extended thinking (`thinking_tokens`) is counted separately in the
  usage dict but does NOT consume the shared context window — thinking
  is ephemeral per request. We intentionally do not add it to ctx.
