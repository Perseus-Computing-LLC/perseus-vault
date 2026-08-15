# Extraction-Loss Net (#1048)

**Status:** implemented (2026-08-15) · **Scope:** residual-span audit,
refusal-as-signal repair loop, provisional query keys

## Problem

When claim extraction drops a fact, nothing recovers it until the next full
ingest. Coalent's measured answer: a bounded, embedding-only residual-span
channel plus a refusal-as-signal loop (verified dogfood: extraction miss →
refusal → attributed span payload → success → identical repeat query served
first-pass via the confirmed key). This is the Perseus mapping.

## Pieces

### 1. Residual-span audit — `perseus_vault_span_audit`

Splits an entity's text into sentences, extracts claims via the local
deterministic `RuleBasedExtractor`, and retains **verbatim with provenance**
every sentence whose best claim similarity is below `coverage_threshold`
(0.55 default). Similarity is embedding-first (bundled ONNX model — no extra
LLM call) with a deterministic token-containment fallback (`mode: token`),
so the net works air-gapped. Append-only: re-audits never duplicate.

Spans are **regular memory state**, not RAG side channels: they live in
`residual_spans`, carry the entity's source, and are subject to the same
decay/hygiene rules. They are never auto-served into recall.

### 2. Refusal-as-signal — `perseus_vault_report_refusal`

An answerer's refusal over a served payload is evidence. The tool re-scores
the served entities' spans against the original query and returns a **retry
payload**: spans whose query-similarity beats the entity's own similarity by
`ANOMALY_MARGIN` (0.05) and clears `RETRY_FLOOR` (0.10) — the anomaly rule.
Spans returned in a retry are marked `served`. Units with no retry material
accumulate `lossy_count`; at `LOSSY_THRESHOLD` (2) they are flagged in
`lossy_units`.

### 3. Confirmation — `perseus_vault_report_success`

Confirms a retry answered: attaches a **provisional query key**
(query fingerprint → entity ids) in `query_keys`, so an **identical repeat
query serves first-pass** (the recall handler prepends confirmed entities,
marked `confirmed_query_key: true`, deduped against the normal result).
Served spans become `confirmed`; lossy units clear to `repaired`.

### 4. Lossy repair on touch

The next `remember` write to a lossy unit's (category, key) folds its
confirmed/active spans into the body as an append-only
`## Residual spans (lossy repair)` section (before encryption/indexing) and
clears the mark. A re-touch never duplicates.

## Data model (schema v40)

- `residual_spans(id, entity_id, span_text, source, max_coverage,
  coverage_mode, status[active|served|confirmed|repaired], lossy_count,
  created_ms, last_served_ms)`
- `query_keys(fingerprint, query, entity_ids, confirmed_ms, hit_count)`
- `lossy_units(entity_id, lossy_count, marked_at_ms, status[lossy|repaired])`

## Authority & hygiene

The net never bypasses admission: spans are written by explicit tool calls
(they inherit the caller's workspace binding), repair folds only affect the
touched unit's own body, and the query-key first-pass still runs the normal
authority check on recall. There is no auto-serve path.

## Determinism

All similarity in `token` mode is a pure function of text; tests run fully
offline. Embedding mode degrades to token mode on any backend failure.
