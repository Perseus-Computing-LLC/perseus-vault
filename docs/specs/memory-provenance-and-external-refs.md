# Memory origin (provenance) and external entity references

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #729, #728
Related: `memory-taxonomy-and-precedence.md` (classes, rule R3),
`source-anchors-corrections-retention.md` (anchor vocabulary),
`served-memory-api.md` (surfacing)

This spec adds two optional, backwards-compatible metadata contracts to
Vault entities: a normalized **memory-origin** record (how this memory came
to exist) and first-class **external entity references** (what external
objects this memory is about). Both are metadata-layer additions: no
storage-engine change, no forced backfill, no breaking change to
`body_json`.

## 1. Memory origin fields (#729)

### 1.1 Schema

```json
{
  "origin": {
    "memory_kind": "asserted | extracted | inferred | imported | observed",
    "source_system": "user | capture | slack | confluence | jira | connector:<name> | agent",
    "capture_method": "manual | rule_based_extractor | llm_extractor | import | event_feed",
    "observed_at": "unix ms, when the fact was true in the world, if distinct",
    "recorded_at": "unix ms, when it entered the store"
  }
}
```

Definitions:

- `asserted` — a human stated it directly (chat instruction, explicit
  `mimir_remember` by an operator).
- `extracted` — pulled out of transcript/content by a deterministic or LLM
  extractor (`mimir_capture`, `mimir_extract`).
- `inferred` — derived from other memories (dream/consolidate synthesis,
  belief rollup).
- `imported` — brought in from an external system (vault import, connector
  ingest, file ingest).
- `observed` — mechanically recorded from an event/log/feed (journal
  events, health checks).

`recorded_at` already exists implicitly (created_at); `observed_at` is for
facts whose world-time differs from write-time (retroactive imports). Where
`observed_at` is absent, world-time equals `valid_from` as today.

### 1.2 Write-path population

| Path | memory_kind | source_system | capture_method |
|---|---|---|---|
| `mimir_remember` (agent/operator) | asserted | agent/user | manual |
| `mimir_capture` (rule-based) | extracted | capture | rule_based_extractor |
| `mimir_capture --llm` | extracted | capture | llm_extractor |
| `mimir_dream` / `mimir_consolidate` | inferred | agent | llm_extractor / import-free derivation |
| `mimir_ingest` / `mimir_ingest_file` / `mimir_vault_import` | imported | connector:<name> | import |
| `mimir_journal` | observed | agent | event_feed |
| `mimir_correct` | asserted | user | manual |

Defaults: when unknown, `memory_kind` is null — never guessed. Existing
entities are valid unchanged; the mapping above lets a lazy backfill label
the obvious cases by `source`, but backfill is optional by design.

### 1.3 Read-path behavior

- Recall/context tools surface `origin` behind the explanation flag (see
  served-memory spec); default compact output is unchanged.
- Ranking: origin feeds taxonomy rule R3 (direct evidence beats inference):
  asserted/observed outrank inferred on the same subject. Origin does not
  otherwise change ranking weights.
- Trust calibration: `asserted` + verified is the strongest signal;
  `inferred` without supporting `derived_from` is the weakest.

## 2. External entity references (#728)

### 2.1 Schema

```json
{
  "external_refs": [
    {
      "ref_type": "ari | url | jira_key | confluence_page | account_id |
                   repo | pull_request | file | session | custom",
      "ref_value": "canonical string",
      "source_system": "optional",
      "relationship": "about | derived_from | mentions | applies_to | supersedes"
    }
  ]
}
```

- Canonical forms per type are fixed in
  `source-anchors-corrections-retention.md` §1 (anchors are the serving-side
  view of the same refs).
- Multiple refs per entity; order insignificant.
- `relationship` defaults to `about` when omitted.

### 2.2 Behavior

- **Write-time inclusion** on remember/capture/import paths; callers pass
  refs they know, extractors MAY emit them (e.g. a PR URL in a capture).
- **Queryable**: recall filters `ref_type:`/`ref_value:`; consumers can ask
  "everything about this repo/PR/customer" without parsing free text.
- **Backwards compatible**: refs live alongside `body_json`; existing
  free-text identifiers keep working; nothing is rewritten.
- **Workspace isolation unchanged**: refs inherit the entity's scope; a
  workspace-scoped memory's refs are invisible to other workspaces exactly
  as its body is.
- **No permission implication**: a ref is a pointer, not an access grant.

### 2.3 Interop posture

Refs give graph-shaped consumers (ARI/entity-linking style systems) stable
join keys without Vault adopting graph storage — differentiation per the
strategy doc, not parity. Adapters can walk `external_refs` to project
Vault memories into an external graph; Vault remains the durable,
user-steerable substrate.

## 3. API and storage implications

- Both contracts are optional JSON metadata carried on the entity record;
  SQLite schema is unchanged (fields serialize into the existing metadata
  channel, indexed where filtering is offered).
- `mimir_remember` gains optional `origin` and `external_refs` arguments;
  other write paths populate per §1.2.
- FTS index is unaffected; ref filtering is a post-filter over candidate
  sets, so no reindex is required for existing databases.
- Consumers that predate these fields ignore them safely.

## 4. Acceptance checklist traceability

- #729: origin fields (§1.1), write-path population (§1.2), recall
  surfacing + ranking semantics (§1.3), backwards compatibility and no
  forced backfill (§1.2, §3). ✔
- #728: `external_refs` schema (§2.1), write-time inclusion + queryability
  (§2.2), multi-ref support, body_json compatibility, workspace isolation
  (§2.2), interop without graph storage (§2.3). ✔
- Cross-repo render dependency: Perseus-side surfacing is perseus#838.
