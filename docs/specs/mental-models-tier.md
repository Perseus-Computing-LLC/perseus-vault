# Mental-models tier (#886)

A curated retrieval tier consulted **before** consolidated observations and
raw facts in ask/recall — the Hindsight borrow for frequent queries: an
operator-curated summary answers directly instead of re-deriving from raw
evidence every time.

Status: implemented in `feat/vault-886-mental-models` (PR #920-precedent
pipeline); float32-era behavior is unchanged for stores that never use the
tier (default recall order is byte-identical).

---

## Acceptance criteria

| Criterion | Contract |
|---|---|
| Curated, not auto-generated | `mental_model` entities are written ONLY by `perseus_vault_mental_model_set` (or a review decision); `consolidate` refuses the category fail-closed. No dream/cohere/consolidate pass can create them. |
| Versioned | Every set/review-decision re-assert goes through the audited `remember` path → `entity_history` snapshot; `revision` bumps monotonically. |
| Provenance | `curated_by` (requesting agent or `operator`), `curated_at_unix_ms`, `source_ids` (the raw facts/observations the summary was curated from). |
| Hierarchical retrieval | ask sources and `recall(..., tier_order: true)` order mental models → observations → raw facts (stable within tier). Ask marks stale models `(stale — pending operator review)`; they are flagged, never silently dropped. |
| Refresh/review flow | `perseus_vault_mental_model_review` lists flagged models (reason `age` / `newer_facts:<key>` / `malformed_body`, age_days, newest-fact trace); `approve`/`dismiss` stamps `reviewed_at` (resets the age clock) and records the decision. Flags also ride the `perseus_vault_operator_review` queue (`mental_models` section). |
| recall_when attachment | `recall_when` triggers stored on the model are matched by `perseus_vault_recall_when`/prepare → scheduled re-verification surfaces the model for periodic review. |

## Body schema (v1)

All fields optional except `summary` (1..=4096 chars):

```json
{
  "summary": "stack uses vue for the portal",
  "scope": "tech",
  "source_ids": ["mem-1"],
  "recall_when": ["stack", "portal"],
  "curated_by": "operator",
  "curated_at_unix_ms": 1750000000000,
  "reviewed_at_unix_ms": 1750000000000,
  "review_interval_days": 30,
  "revision": 2,
  "stale": false,
  "stale_reason": "",
  "last_review_decision": "approved"
}
```

Staleness is **derived at read time** (the stored `stale` fields are a
snapshot refreshed by set/review):

- `age` — `now - reviewed_at` (fallback `curated_at`) exceeds
  `review_interval_days` (default 30, validated 1..=3650).
- `newer_facts:<key>` — a raw fact exists in the model's `scope` category,
  created after `curated_at`, whose id is not already a cited `source_id`
  (trace-back via the fact key). Requires a non-empty `scope`; without one,
  age-only.

## Retrieval ordering

- `db.recall(..., tier_order: true)` — stable in-place reorder of the
  RETURNED list by tier (`mental_model` < `observation` < raw). Membership
  and scores are untouched; default (`false`) is byte-identical to pre-#886
  behavior. ask enables it internally.
- ask context: mental models carry a structural prefix
  `[mental model: <key>]`; stale ones add `(stale — pending operator review)`
  and the source's `verification` is `stale_mental_model_pending_review`.
  Unlike the observation gate (#884), stale mental models are never refused —
  curated content is surfaced with its flag, and the review flow resolves it.

## Review flow

1. `perseus_vault_mental_model_review` (action `list`, default) — flagged
   models with `reason`, `age_days`, `review_interval_days`, `revision`,
   `newest_fact_id`/`newest_fact_key`, `summary`. Same flags appear in
   `perseus_vault_operator_review` → `mental_models`.
2. Operator decides: `approve` (reviewed and kept) or `dismiss`
   (acknowledged, kept) — both stamp `reviewed_at = now` (resets the age
   clock), record `last_review_decision`, bump `revision`, and re-assert
   through the audited path. The summary itself only changes via a new
   `perseus_vault_mental_model_set`.
3. `newer_facts` staleness stays derived: a contradicting fact still flags
   the model on the next list — a decision is a review stamp, not a blind.
   Refreshing the summary (or citing the new fact in `source_ids`) clears it.

## Determinism anchors

- Tier order is a stable sort on `tier_of(category)`; ties keep ranking
  order. `recall` default output is untouched (no reorder).
- Staleness reasons are a deterministic function of `(now_ms, body)` +
  the newest-fact query (`created_at_unix_ms DESC, id ASC LIMIT 1`).
- Review list is `created_at_unix_ms ASC` ordered (oldest flagged first).
- The canonical body is produced by the single `serialize_meta` writer
  (struct field order), so re-asserts are byte-stable.

## Fail-closed paths

- `mental_model_set`: empty key, out-of-range summary (1..=4096 chars),
  interval outside 1..=3650, empty/oversized recall_when triggers
  (≤32 × ≤128 chars), or >256 source_ids → rejected before any write.
- `consolidate` with category `mental_model` → error (curated category).
- `mental_model_review` with unknown action or missing key → error.
- A malformed mental-model body (unparseable / missing summary) is
  surfaced in the review list as `malformed_body` — never silently ranked
  or dropped.

## MCP surface

- `perseus_vault_mental_model_set` — create/refresh (key, summary
  required; scope, source_ids, recall_when, review_interval_days,
  workspace_hash, requesting_agent_id optional).
- `perseus_vault_mental_model_review` — list / approve / dismiss.
- `perseus_vault_recall` — new optional `tier_order` arg (default false).
- `perseus_vault_operator_review` — new read-only `mental_models` section.

Registry: 106 → 108 tools (README.md / CLAIMS-AUDIT.md / manifest.json /
server.json / glama.json counts synced; `registry_and_advertised_manifest_are_unique_and_in_sync`
passes).

## Test coverage

15 new tests: 6 unit (`mental_model.rs` — parse/serialize round-trip,
garbage tolerance, age/newer-facts staleness rules, validation bounds,
context prefix, tier classification) + 9 integration (`db.rs` — curated
set with provenance/versioning/entity_history, update-merge + revision,
fail-closed validation leaves no rows, consolidate refusal, recall
tier_order vs default, age flag + approve reset, newer-facts flag +
source-cite clearing, recall_when attachment, ask_sources mental-model
first). Full suite: 777 passed / 0 failed / 11 ignored (baseline 762).
