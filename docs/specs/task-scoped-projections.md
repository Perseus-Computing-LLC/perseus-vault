# Task-scoped projections

Status: implementation specification
Date: 2026-08-09
Resolves: #859
Related: `served-memory-api.md` (context tool), `validity-aware-recall.md`
(#860), `memory-provenance-and-external-refs.md` (#728/#729),
`multi-agent-scoping.md` (#338/#339), `deterministic-recall-and-provenance.md`
(#247)

## Motivation

Vault is a durable memory + provenance system, but its MCP surfaces did not
distinguish **live context references** from **durable recalled memory** and
**derived inferences** — clients stitching raw recall dumps had to re-derive
that separation themselves. This spec adds a task-scoped projection layer
inspired by the system-of-work framing: a compact, one-call artifact for a
single task, with permission scope, freshness, provenance, and trust class
visible in the result contract.

## Tool

`perseus_vault_project_task` (read-only; registered in the read-tool scope
list for authority enforcement).

### Args

| arg | type | default | meaning |
|---|---|---|---|
| `task_title` | string | — (required) | The task the projection is scoped to; also the recall query when `query` is omitted |
| `task_description` | string | — | Optional task context (advisory) |
| `query` | string | task_title | Explicit retrieval query |
| `category` | string | — | Restrict the recall pool to one category |
| `workspace_hash` | string | — | Permission scope (recall scoping, #338/#339) |
| `limit` | int | 12 | Max items **per section** (1–100) |
| `freshness_window_days` | int | — | Only entities created within N days are projected; older hits are counted, not dropped silently |
| `min_trust` | enum | `candidate` | `candidate` \| `corroborated` \| `verified` floor; `rejected` entities never project |
| `include_sections` | string[] | all | Subset of `live` \| `durable` \| `derived` |
| `query_time_unix_ms` | int | server now | Freshness anchor; deterministic replay (#247) |

All args fail closed: unknown `min_trust`, unknown section names, empty
`task_title`, out-of-range `limit` are errors, not silent defaults.

## Result contract

```json
{
  "task": { "title": "...", "projection_id": "sha256(…)[0:16]" },
  "generated_at_unix_ms": 1750000000000,
  "scope": { "workspace_hash": "…", "category": "…", "permission": "workspace_scoped|global" },
  "sections": {
    "live_references": [ /* items */ ],
    "durable_memories": [ /* items */ ],
    "derived_inferences": [ /* items */ ]
  },
  "contract": {
    "separates": ["live", "durable", "derived"],
    "permission": "workspace_scoped",
    "freshness_anchor_unix_ms": 1750000000000,
    "trust_classes": ["candidate", "verified"],
    "counts": { "live": 1, "durable": 3, "derived": 1 },
    "excluded": { "rejected": 0, "below_min_trust": 1, "outside_freshness_window": 0 }
  },
  "trace": { "method": "task-projection-v1", "pool_size": 9, "recall_modes": ["fused", "fts5", "temporal"] }
}
```

### Item shape (compact — never a raw recall dump)

```json
{
  "id": "mem-…",
  "key": "deploy-window",
  "section": "derived",
  "summary": "deploy windows drop Stripe webhooks…",
  "trust_class": "verified",
  "freshness": { "grade": "valid", "value": 0.97, "created_at_unix_ms": …, "age_days": 2.1 },
  "scope": "exact",
  "provenance": {
    "memory_kind": "inferred",
    "source_system": "connector:confluence",
    "capture_method": "rule_based_extractor",
    "external_refs": [{ "ref_type": "confluence_page", "ref_value": "…", "relationship": "derived_from" }],
    "evidence_hash": "sha256…"
  },
  "source_of_truth_hint": "live_external|memory_internal"
}
```

- `summary`: the body's `note` (else the body), truncated at 280 chars
  deterministically.
- `trust_class`: the entity's epistemic state (#880); `unclassified` when
  absent. `rejected` entities are hard-excluded before trust filtering.
- `freshness`: validity scorer from #860 (grade `valid|stale|context_invalid`,
  value `0.5^(age/30d)`), anchored at `query_time_unix_ms`.
- `scope`: `exact` (entity workspace == query workspace), `global` (empty
  workspace), `none`.
- `provenance`: reserved `origin` / `external_refs` keys read from
  `body_json` (#728/#729) + the evidence envelope's `content_sha256` when
  present.
- `source_of_truth_hint`: `live_external` for live references (pointers into
  a live system of record), `memory_internal` otherwise.

## Sectioning (deterministic)

| section | predicate |
|---|---|
| `derived` | `origin.memory_kind == "inferred"` OR `entity_type == "fact_derived"` OR any `external_ref.relationship == "derived_from"` |
| `live` | `external_refs` non-empty (first-class pointer to an external system of record) |
| `durable` | everything else |

Derived takes precedence over live (an inference citing live sources is
still an inference). Unparseable bodies classify as durable — never guessed.
Lifecycle-suppressed rows (archived/deprecated/expired) never enter the pool
(read-time exclusion, #868/#866) and are therefore not counted in
`excluded`.

## Retrieval + ordering

- Pool: `fused_recall` with `fts5` + `temporal` arms (no embeddings
  required), `limit × 3` capped at 300, `min_decay 0`, no side effects.
- Within a section: freshness value desc, then id asc (deterministic).
- `projection_id`: sha256 over the request inputs (workspace, category,
  title, query, limit, window, min_trust, anchor) — identical requests
  replay to the same id, per #247.

## Success criteria → evidence

| criterion | mechanism |
|---|---|
| clients request a compact projection instead of stitching raw recall | one-call `perseus_vault_project_task`; `task-projection-*` benchmark cases |
| outputs distinguish recalled memory / live references / derived inferences | three labeled sections + `source_of_truth_hint`; `task-projection-separates-and-hints` |
| permission, freshness, provenance visible in the result contract | `contract` block + per-item fields; `task-projection-contract-compact-replay` |

## Compatibility

Additive: one new read-only MCP tool (registry 105 → 106); no changes to
existing tools or schemas; response bytes of existing surfaces unchanged.
