# Activation-Gated Sparse Writes

Issue: [#874](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/874) — Activation-gated sparse writes: interference-aware memory updates.
Schema: v34 (`write_quarantine`). Status: shipped (2026-08).

## Grounding

*Continual Learning via Sparse Memory Finetuning* (Lin et al., Meta FAIR + UC
Berkeley, arXiv:2510.15103, Oct 2025): updating **only the memory slots highly
activated by new knowledge** reduces interference with existing capabilities.
On NaturalQuestions, F1 drops **89% after full fine-tuning, 71% with LoRA, but
only 11% with sparse memory fine-tuning** at equal new-knowledge acquisition.

The vault analog is a governed write discipline:

1. every landing write (fresh insert or content-changing update) measures its
   **activation overlap** with the existing corpus — the *interference score*;
2. the commit is gated on that score against a configurable bound,
   **fail-closed to a reviewable write quarantine** (or refusal) instead of
   silently merging into unrelated memory;
3. **sparse update mode** touches only the activated subset of state (the body
   slot, activation-filtered links) and never disturbs neighbors.

## Interference score

For an incoming entity `E`, the score is the weighted mean over the components
that can be measured:

| Component | Signal | Weight | Measured over |
|---|---|---|---|
| token containment | `|tokens(E) ∩ tokens(O)| / |tokens(E)|` for the best-matching existing entity `O` — how much of the incoming fact's vocabulary is already present in existing memory | 0.25 | FTS top-k candidates (same workspace, unarchived) |
| link containment | same containment over link-target sets | 0.25 | the same candidate set |
| embedding similarity | max cosine(`E`, `O`) over stored vectors | 0.5 | bounded scan (default 50k rows, `PERSEUS_VAULT_DENSE_MAX_SCAN` semantics), decodes stored f32/int8 blobs (`vector_quant::decode_stored`); bit-mode blobs have no reconstructible vector and are skipped |

- **Tokens are the body's JSON string values** (`body_tokens`), recursively —
  structural keys (`content`, `note`) and identity fields (category/key) are
  excluded: boilerplate every entity shares would dilute the overlap of the
  fact itself.
- **Missing components do not dilute the score**: weights renormalize over the
  components actually measured (e.g. no embeddings → score is the token/link
  mix at 0.5/0.5).
- **Excluded slots**: the write's own identity (`category,key,workspace`) and
  any `exclude_ids` — memory slots the write INTENTIONALLY updates (cited
  sources, consolidated observations' source sets). Activation overlap is
  measured against the *rest* of the corpus: the paper's "slots activated by
  the new knowledge" are the update targets; interference is disturbance of
  everything else.
- The incoming fact's embedding is computed synchronously **only** when
  `PERSEUS_VAULT_INTERFERENCE_EMBED=1` (default off — #271 kept ONNX inference
  off the default write path). A backend failure degrades the measurement
  (journaled via `components`) rather than blocking the write.

## The gate

`score > bound` fires the gate. Enforcement modes:

| Mode | Behavior |
|---|---|
| `quarantine` (default) | write staged in the `write_quarantine` table — **never served by any read surface** — with the full interference report; operator review via `perseus_vault_write_quarantine` |
| `refuse` | write errors out, nothing stored anywhere |
| `off` | enforcement bypassed; the score is still computed and journaled (operator-only) |

Configuration (env, read at gate time — same pattern as the #619 dense dials):

| Env | Default | Meaning |
|---|---|---|
| `PERSEUS_VAULT_INTERFERENCE_MODE` | `quarantine` | enforcement mode (`quarantine` / `refuse` / `off`) |
| `PERSEUS_VAULT_INTERFERENCE_BOUND` | `0.90` | firing threshold (strictly greater) |
| `PERSEUS_VAULT_INTERFERENCE_TOP_K` | `16` | candidate-set size (FTS top-k) |
| `PERSEUS_VAULT_INTERFERENCE_EMBED` | `0` | compute the incoming embedding synchronously for the gate |
| `PERSEUS_VAULT_SPARSE_ACTIVATION` | `0.30` | sparse link-admission threshold |

Per-write overrides on `perseus_vault_remember` (`interference_mode`,
`interference_bound`) are **fail-closed**: only `refuse`/`quarantine` are
accepted (per-write `off` would let a caller bypass the gate) and a bound
override may only tighten the configured bound. The journal records the
effective mode/bound with every scored write.

### Where the gate runs

- **Fresh inserts** — after the near-duplicate merge check (a write resolved
  by dedup is the activation-targeted update: it strengthens the most-activated
  slot and never gates). `skip_dedup` writes, which would otherwise land as
  near-duplicates, hit the gate directly.
- **Content-changing updates** — the rewrite of a slot to content already
  covered by a *different* entity is held/refused before any mutation.
  Identical re-asserts change nothing and skip the gate.
- **Consolidation** — before a fold/refine, the merged *content* (the summary
  text, not the observation envelope) is probed against the corpus minus the
  fold's source set; an exceeded bound **skips the fold** and journals
  `consolidate_interference_skip` (counted in the report as
  `interference_skips`) instead of quarantining a stray observation.
- **Links** — edges are explicit caller intent (unlink is the correction
  path), so `perseus_vault_link` is telemetry-only: every edge journals its
  endpoint **coherence** (`link_interference_scored`).

## Quarantine lifecycle

`write_quarantine` stores the held write (body encrypted like entities, AAD
bound to category+key). It is a separate table — by construction invisible to
recall/scan/dense/graph/communities. Surfaces:

- `perseus_vault_write_quarantine` — `list` (scoped), `show` (decrypted body +
  interference report), `release` (materializes through the audited remember
  path with the gate bypassed — **the operator review IS the approval**;
  refused fail-closed when the identity is already live), `delete`.
- `perseus_vault_operator_review` gains a `write_quarantine` section.
- Journal events: `interference_scored` (every landing write),
  `interference_quarantined`, `interference_refused`, `interference_released`,
  `interference_deleted`.

## Sparse update mode

`sparse_update=true` on `perseus_vault_remember`:

- **No salience inflation**: a sparse re-assert touches only the body slot —
  no decay boost, no `retrieval_count` increment (the dense path keeps today's
  "being remembered" semantics).
- **Activation-filtered links**: caller links are admitted only when their
  target's content shares ≥ `sparse_activation` of the incoming body's tokens;
  non-activated caller links are dropped and journaled
  (`sparse_update_applied`). Stored links are never removed (unlink is the
  only removal path, #382).
- **No near-duplicate absorption** on insert (sparse writes never disturb
  neighbors).
- Regression-tested: unrelated fixtures' recall is unchanged after repeated
  sparse updates on other topics.

## Design decisions

- **Quarantine is a separate table, not an entity flag**: quarantined memory
  can never leak into any read path by construction; review is explicit; a
  purge of forgotten entities can never destroy pending holds.
- **Deduped writes are not re-scored**: the near-duplicate merge is the
  activation-targeted resolution (it strengthens the most-activated slot and
  changes no content). All *landing* writes are scored and journaled.
- **The harness posture**: `benchmark/quality/run.py` runs its binary with
  `PERSEUS_VAULT_INTERFERENCE_MODE=off` because its templated fixtures are
  near-duplicates by design; the `interference_gate` scenario opts in
  per-write to exercise the full MCP surface deterministically. The default
  fail-closed posture is covered by the unit suite.

## Tests

- Unit: tokenizer/containment/mode parsing, strict-above-bound evaluation,
  fail-closed override validation, env defaults.
- Integration: default quarantine of near-verbatim `skip_dedup` writes (never
  served, journaled, listed), refuse-mode errors, off-mode bypass,
  content-changing update gating (identical re-asserts pass), per-write
  journal shape (components, mode, bound), sparse recall preservation +
  salience non-inflation + link activation filtering + no-dedup inserts,
  quarantine show/release/delete lifecycle + release-into-live refusal,
  workspace scoping, operator-review section, `exclude_ids` source slots,
  tool-surface override rejection, explicit `quarantined:true` tool response,
  link-coherence telemetry, consolidate fold-skip guard.
- Benchmark quality harness v9: `interference_gate` scenario (5 checks across
  4 cases: default quarantine, never-served, refuse override, sparse recall
  preservation, release materialization).

## Pitfalls

- **FTS5 aliases**: `f MATCH ?1` fails ("no such column: f") — MATCH must
  reference the declared table name; `ORDER BY rank` works only without the
  alias.
- **Placeholder numbering**: exclusion clauses shift every later placeholder;
  hardcoding `LIMIT ?3` silently misbinds. Exclusions start at `?3` (after
  MATCH `?1` and workspace `?2`) and the LIMIT index is computed.
- **Nested pool draws**: `journal()` draws a second pooled connection —
  calling it from a write path already holding one reintroduces the #387
  starvation class under concurrency. Write paths use
  `journal_with_conn(conn, …)`.
- **Token-source dilution**: tokenizing the raw body (JSON keys, category,
  key) dilutes containment below the bound — near-verbatim facts scored 0.82.
  Only JSON string values count.
- **Consolidation self-trigger**: an observation body quotes its sources, so
  the fold guard must exclude the source set and compare the extracted
  summary text, not the observation envelope.
