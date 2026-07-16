# Perseus Vault — Noisegate compatibility contract

This documents the **stable output contracts** a downstream compactor (e.g.
[Noisegate](https://github.com/Tosko4/noisegate)) can rely on. The golden
fixture here is a **real capture** from the `perseus-vault` binary, regenerable
with [`regen.sh`](./regen.sh) — not a hand-authored example.

> An earlier version of this file shipped hand-written "byte-exact" samples that
> were inaccurate (wrong context header, `entity_type` instead of the real
> serde-renamed `type`, fabricated char counts, a wrong MCP envelope, and a
> false claim that `recall` output is byte-stable). This version ships only what
> was captured from a real run and verified against source.

## Golden fixture — `prepare --json` (the read-only pre-turn CLI seam)

`perseus-vault prepare --json` is the read-only pre-turn context surface, and the
right seam for a direct-CLI Noisegate rule. Captured from **perseus-vault 2.20.0**;
see [`prepare_output.golden.json`](./prepare_output.golden.json) for the exact
bytes. Regenerate with `./regen.sh` (seeds a throwaway DB with synthetic data —
no real memory).

**Determinism:** output is byte-identical across repeated runs against an
unchanged database (verified: two runs, identical sha256). This holds for the
read-only paths (`prepare`, `scan`, `get`, `context`). It does **NOT** hold for
`recall`, which reinforces access/decay/layer on read.

**Encoding:** pretty-printed JSON (`serde_json::to_string_pretty`), top-level
keys in **alphabetical order** (serde `Value` = BTreeMap; `preserve_order` is
not enabled).

### Top-level contract (verified against `src/main.rs::run_prepare`)

| key | type | notes |
|-----|------|-------|
| `task` | string | echo of `--task` |
| `recall_when` | array&lt;entity&gt; | proactive trigger matches (expanded entities) |
| `recall_when_count` | int | `recall_when.length` |
| `context_markdown` | string | assembled block; **begins with `## Perseus Vault Context`** |
| `context_mode` | string | exactly `"on_demand"` or `"always_inject"` |
| `context_budget_chars` | int | applied char budget (default 1500) |
| `context_entities_injected` | int | entities in the context block |
| `context_warnings` | array&lt;string&gt; | budget/relevance warnings; often `[]` |

The bulk a compactor would reduce under budget pressure is `context_markdown`;
the scalar fields are tiny and worth preserving verbatim.

### `recall_when[]` entity shape (verified against `src/models.rs`)

Serde `Entity` + `to_json_expanded()`. The type field is **`type`** (serde-renamed
from `entity_type`) — **not** `entity_type`. `embedding` and internal parse-cache
fields are `#[serde(skip)]` and never emitted. `to_json_expanded` additionally
merges the parsed `body_json` object's keys up to the top level (excluding
`id`/`category`/`key`/`body_json`/`type`). Always-present fields:
`id, category, key, body_json, status, type, tags, decay_score, retrieval_count,
layer, topic_path, archived, archive_reason, links, verified, source, always_on,
certainty, workspace_hash, agent_id, visibility, created_at_unix_ms,
last_accessed_unix_ms, follow_count, miss_count, follow_rate, efficacy_status`.

## MCP `tools/call` seam (structural — capture before treating as byte-exact)

This is **distinct** from the CLI seam above. An MCP `tools/call` response wraps
the handler output in `result.content[0].text` (a JSON string) plus
`result.structuredContent`, and the server emits **compact** JSON on stdio (not
pretty-printed). Because Noisegate leaves `mcp__*` tool results untouched, the
MCP path generally needs no Perseus-specific rule.

This file intentionally ships **no** hand-authored MCP byte samples: per-tool MCP
payloads should be captured from a live MCP session before being relied on
byte-for-byte. (That was the defect in the previous version.)
