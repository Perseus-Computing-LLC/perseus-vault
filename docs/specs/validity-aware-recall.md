# Validity-aware recall and projection profile

Status: implementation specification
Date: 2026-08-09
Resolves: #860
Related: `fused-multi-strategy-recall.md` (#883), `retrieval-telemetry.md`
(#864), `epistemic-trust-axis.md` (#880), `graph-utility-gate.md` (#869),
`data-boundaries-retention-lifecycle.md` (#868/#866)

## Motivation

Memory recall should optimize for whether a memory is **valid for the
current task context**, not just semantically similar (contextual
reinstatement / validity-aware retrieval research). Vault already carries
the raw signals — temporal freshness, workspace scope, provenance class,
supersession state, expiry — but the product surface did not expose a mode
that *weighs* them together or *explains* why a hit was included.

This spec adds a first-class **validity-aware recall profile**:

- a deterministic validity multiplier over five signals
  (freshness decay · scope match · provenance class · supersession ·
  expiry proximity),
- an explicit per-hit **validity block** (grade, freshness, scope match,
  provenance, superseded/expiring/expired, multiplier, signal list),
- **context-invalid distractor marking** (`context_invalid: true`) so a
  consuming agent never mistakes a stale memory for a current one,
- an observable **validity trace** on fused recall (weights used, grade
  distribution over the pool, count flagged),
- a benchmark scenario measuring the acceptance criteria.

## Contract

### `perseus_vault_recall` (and `recall_batch`) — new args

| arg | type | meaning |
|---|---|---|
| `profile` | `"default" \| "validity"` | `validity` re-ranks the fused pool by the validity multiplier and annotates every delivered item. Unknown values are rejected fail-closed. |
| `validity_annotate` | bool | Annotate items without re-ranking. Implied by `profile: "validity"`. Default `false` (response bytes stable unless asked). |

`query_time_unix_ms` anchors "now" for deterministic replay: the same DB +
same anchor yields the same grades (#247).

### Per-item `validity` block (delivered hits)

```json
{
  "validity": {
    "grade": "valid | stale | context_invalid",
    "freshness": 0.5,
    "scope_match": "exact | global | none",
    "provenance_class": "verified | corroborated | candidate | rejected | defensively_recalled | unknown",
    "superseded": false,
    "expiring_soon": true,
    "expired": false,
    "expires_at_unix_ms": 1750000060000,
    "multiplier": 0.77,
    "signals": ["freshness:0.999", "scope:exact", "expiring_soon"]
  },
  "context_invalid": true
}
```

`context_invalid: true` is added exactly when `grade == "context_invalid"`.

### `fused_trace.validity` (fused mode with `profile: "validity"`)

```json
{
  "profile": "validity",
  "method": "validity-multiplier-v1",
  "weights": { "freshness_half_life_secs": 2592000, "scope_bonus": 0.1, "provenance_boost": 0.15, "superseded_penalty": 0.35, "expiring_penalty": 0.7, "stale_freshness": 0.5, "context_invalid_freshness": 0.125 },
  "grade_counts": { "valid": 3, "stale": 1 },
  "flagged_context_invalid": 0
}
```

## Scoring (`src/validity.rs`, pure & deterministic)

- **Freshness**: `0.5^(age / half_life)`, half-life = 30 days. 30 days
  old → 0.5; 90 days (3 half-lives) → 0.125, the context-invalid floor.
- **Scope**: `+10%` multiplier when the entity's workspace == query
  workspace (`exact`); global (`""`) entities get no penalty; mismatched
  workspaces never reach recall anyway (read-time scoping).
- **Provenance**: `+15%` for `verified`/`corroborated` (established fact,
  #880). Everything below stays neutral — never penalized, just not
  boosted.
- **Supersession**: entity status `deprecated` → `×0.35` and
  `context_invalid`. (Deprecated rows are excluded from recall entirely by
  the read-time lifecycle, so this grades projection/scan surfaces that
  may surface them; recall already structurally serves zero.)
- **Expiry**: past `expires_at_unix_ms` → `×0.35`, `context_invalid`
  (also excluded from recall at read time); within one half-life of
  expiry → `×0.70`, graded `stale`.
- **Grade**: `context_invalid` if expired ∥ superseded ∥ freshness <
  0.125; `stale` if expiring soon ∥ freshness < 0.5; else `valid`.

The multiplier is a product of factors, always > 0: it re-ranks, it never
hard-excludes (hard exclusion remains the read-time lifecycle's job).

### Fused re-ranking

With `profile: "validity"` the fused pool is re-sorted by
`base_score × multiplier` where `base_score` is the rerank stage's score
when it applied, else `(pool_size − rank) / pool_size` — rank-derived,
scale-free, deterministic. Ties break on entity id (ascending), matching
the existing fused pipeline conventions. The stage runs between rerank
and caller-limit/truncation; it composes with the graph utility gate
(#869) and the supersede recency reorder.

Non-fused modes (`fts5`/`dense`/`hybrid`) accept the profile for
**annotation only**; re-ranking is documented as fused-only.

## Acceptance criteria → evidence

| criterion | mechanism |
|---|---|
| lower rate of stale/context-invalid recalled memories | validity re-rank demotes stale/superseded/expired; deprecated + expired rows are structurally excluded at read time; `validity-recall-*` benchmark cases |
| better task answer quality on scoped recall | `validity-recall-orders-fresh-first` (fresh in-scope entity above older match) |
| clearer MCP output contracts around why a memory was included | per-item `validity` block + `fused_trace.validity` trace; `signals` list is the audit trail |

## Compatibility

Additive: two new optional recall args; new response fields only appear
when requested (`profile`/`validity_annotate`); `MemoryLink`-style
defaulting keeps old clients byte-identical. No schema migration, no new
tables, no new MCP tools (registry count unchanged).
