# Claude API Pricing — Authoritative Reference

Source: https://platform.claude.com/docs/en/about-claude/pricing
Fetched: 2026-04-11

This is a local snapshot used by `server.py`'s `_compute_cost()` to compute
session cost from the raw token counts stored in `summary.json`. Refresh this
file when Anthropic updates pricing, and mirror the changes into the `PRICING`
dict in `server.py`.

## Per-million-token pricing (standard API tier, USD)

| Model              | Input | 5m Cache Write | 1h Cache Write | Cache Hit (read) | Output |
|--------------------|-------|----------------|----------------|------------------|--------|
| Claude Opus 4.6    | $5    | $6.25          | $10            | $0.50            | $25    |
| Claude Opus 4.5    | $5    | $6.25          | $10            | $0.50            | $25    |
| Claude Opus 4.1    | $15   | $18.75         | $30            | $1.50            | $75    |
| Claude Opus 4      | $15   | $18.75         | $30            | $1.50            | $75    |
| Claude Sonnet 4.6  | $3    | $3.75          | $6             | $0.30            | $15    |
| Claude Sonnet 4.5  | $3    | $3.75          | $6             | $0.30            | $15    |
| Claude Sonnet 4    | $3    | $3.75          | $6             | $0.30            | $15    |
| Claude Haiku 4.5   | $1    | $1.25          | $2             | $0.10            | $5     |
| Claude Haiku 3.5   | $0.80 | $1             | $1.60          | $0.08            | $4     |
| Claude Haiku 3     | $0.25 | $0.30          | $0.50          | $0.03            | $1.25  |

## Prompt-cache pricing derivation

All cache prices are multipliers on the base input price:

| Cache operation | Multiplier | Duration |
|---|---|---|
| 5-minute cache write | 1.25× base input | Cache valid for 5 minutes |
| 1-hour cache write   | 2× base input    | Cache valid for 1 hour |
| Cache read (hit)     | 0.1× base input  | Same duration as the write |

So Opus 4.6 at $5 input → $6.25 (5m write) = 1.25 × $5, $10 (1h write) = 2 × $5,
$0.50 (cache read) = 0.1 × $5.

## Mapping to transcript usage fields

The `usage` object in Claude Code transcripts looks like:

```json
{
  "input_tokens": 10,
  "output_tokens": 171,
  "cache_read_input_tokens": 0,
  "cache_creation_input_tokens": 39706,
  "cache_creation": {
    "ephemeral_5m_input_tokens": 0,
    "ephemeral_1h_input_tokens": 39706
  }
}
```

| Transcript field | Priced at |
|---|---|
| `input_tokens` | base input |
| `output_tokens` | base output |
| `cache_read_input_tokens` | cache-read price |
| `cache_creation.ephemeral_5m_input_tokens` | 5m cache-write price |
| `cache_creation.ephemeral_1h_input_tokens` | 1h cache-write price |

`cache_creation_input_tokens` is the total cache-write count and equals the sum
of the 5m + 1h split. We store the split explicitly in `summary.json` so cost
computation is exact without re-reading the transcript.

## Not included in this table / code

- **Batch API** (50% discount) — auditit doesn't distinguish; assume standard API.
- **Fast mode** (6× on Opus 4.6) — rare feature, auditit ignores.
- **Data residency** (1.1× US-only) — rare feature, auditit ignores.
- **Long-context premium** — Opus/Sonnet 4.5+ include the full 1M window at
  standard pricing, so not applicable.
- **Tool-use overhead** (e.g. 346 tool-use system prompt tokens) — already
  counted by the model in `input_tokens`.
- **Web search** ($10 / 1000 searches) — not computable from usage alone;
  would need to count `server_tool_use.web_search_requests`. Skipped for now.
- **Code execution** container hours — not exposed in usage dict.
