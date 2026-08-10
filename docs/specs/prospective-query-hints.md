# Prospective Query Hints (#919)

Status: implemented (schema v37, registry 120 unchanged — `hints` is a
remember-tool argument, not a new tool).

## Problem

Natural queries and stored facts often share no vocabulary: a fact written as
"the API binds to the documented endpoint" is invisible to a query about
"what port does the api listen on". `recall_when` covers *retrospective*
triggers ("recall me when the context looks like X") and learned anticipation
(#875) tunes those triggers; neither helps the *write-time* case where the
writer already knows the phrasings a future query will use.

## Design

Optional **prospective query hints** — 1–3 natural-language phrasings per
entity, supplied at ingestion and indexed into FTS5 alongside the canonical
body. Hints bridge the vocabulary gap: the keyword arm matches them like any
indexed text, with zero ranking changes and zero query-side complexity.

- **Storage**: `entities.hints` (JSON array of strings, schema v37). The
  column is advisory retrieval metadata only — never merged into `body_json`,
  so dedup identity, interference scoring, history snapshots, and body-based
  features are untouched. Not versioned in `entity_history` (current-state
  advisory data).
- **At-rest parity**: when encryption is on, hints are AES-GCM ciphertext
  with the same (category, key) AAD as the body; the FTS index stores the
  decrypted hint text exactly like the decrypted body.
- **Indexing**: every FTS write site that can carry hints routes through
  `Database::fts_indexed_text(body, hints)` — body, then each hint on its own
  line. Empty hints return the body unchanged (byte-identical to pre-hints
  indexing). `reindex_fts` reproduces the same text from the stored column
  (per-row path; the fast bulk copy is kept when no entity carries hints), so
  rebuilds and live writes always agree.
- **Default-off gate**: `PERSEUS_VAULT_HINTS_ENABLED=1` (env, read per call).
  While disabled, `hints` on `perseus_vault_remember` is **rejected with an
  error** — never silently dropped, so an agent that believes its hints are
  stored cannot mis-query later. The gate governs write acceptance only;
  indexing is a pure function of stored data, so toggling the env
  mid-lifecycle cannot silently change recall results.
- **Validation (fail-closed, gate-independent)**: 1–3 hints; each trimmed
  non-empty; ≤200 bytes each.
- **Update semantics**: hints replace any previously stored hints on update
  (an update without hints clears them) — matching the existing remember
  reset semantics for tags/status.
- **Read surface**: `get_entity`, `scan`, recall/search items carry the
  `hints` field (additive; history and time-travel reads resolve to `[]`).

## Contract

- `perseus_vault_remember` gains optional `hints: string[]` (maxItems 3).
- Response shapes gain a `hints` field on entity objects; absent for
  history/`as_of` reads.
- Determinism: with the gate off, recall behavior is byte-identical to
  before the feature (hints never written; indexing path unchanged for
  hint-less rows).

## Recall-delta measurement

`benchmark/recall` is dataset-agnostic (`{"memories": [...], "queries":
[{"q", "relevant": [keys]}]}`). The measurement protocol for enabling by
default:

1. Build a vocabulary-gap dataset: memories stored with terse technical
   wording; queries phrased in plain language whose only match path is the
   hints.
2. Run the harness twice against the same dataset — ingestion with hints
   (gate on) vs without (gate off) — and compare recall@k/MRR per arm.
3. Fold the delta into `benchmark/recall/report.json` alongside
   `benchmark/longmemeval` results before any default-on flip.

## Verification

- Unit/integration: gate rejection at the tool surface; fail-closed
  validation (count/length/emptiness); hint vocabulary retrieves via keyword
  recall; update replaces and clears; encrypted deployments store ciphertext
  at rest while FTS carries plaintext (reindex included); reindex preserves
  hint indexing on the plaintext arm; dedup identity ignores hints.
