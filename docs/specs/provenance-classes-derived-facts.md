# Provenance classes for derived facts

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #733
Related: `memory-provenance-and-external-refs.md` (origin fields this spec
builds on), `structured-truth-retrieval-policy.md` (retrieval order),
`incremental-extraction-refresh.md` (refresh of derived classes),
`memory-taxonomy-and-precedence.md` (rule R3), `served-memory-api.md`
(surfacing)

`memory-provenance-and-external-refs.md` defines the `origin.memory_kind`
vocabulary (asserted / extracted / inferred / imported / observed). This
spec groups those kinds into four **provenance classes**, defines the
extraction pipeline that produces them, and fixes how retrieval scoring
prefers trustworthy substrate. Nothing here redefines the origin schema;
the classes are a coarser, query-friendly roll-up over it.

## 1. Provenance classes

| Class | Definition | origin.memory_kind | Taxonomy classes (#720) | Examples |
|---|---|---|---|---|
| `source_human` | A human-authored source artifact: transcript, note, page, doc — evidence in its original form | `asserted`, `imported` (raw) | episode, anchor targets | meeting transcript, operator-authored runbook |
| `fact_extracted` | A fact mechanically or LLM-extracted from one source artifact, still traceable to it | `extracted`, `observed` | episode, semantic | "deploy ran 21:55Z→23:19Z" from the ops log |
| `fact_derived` | A stable statement derived from multiple extracted facts or artifacts, evidence-linked | `inferred` (with `derived_from`) | observation, semantic | "deploy windows drop Stripe webhooks" (2 sources) |
| `inference_agent` | An agent synthesis or judgment not reducible to a stored evidence set | `inferred` (no evidence set), dream output | insight | "we should gate on #4 before enabling" |

Class is recorded as `provenance_class` in body metadata alongside
`origin`; it is derived deterministically from `memory_kind` + presence of
`derived_from`/`evidence_for` links, so a lazy backfill can label existing
entities without operator input. When undeterminable, the class is null —
never guessed (same rule as origin fields).

Rules:

- **Evidence linkage defines the derived/inference boundary.** A derived
  fact MUST carry `derived_from` citations (or `evidence_for` links per
  `mimir_link`); without them it is `inference_agent` regardless of
  author intent. This is what keeps synthesis auditable.
- **Classes only move up-trust by adding evidence.** Reclassifying
  `inference_agent` → `fact_derived` means attaching the evidence set, not
  editing a label.
- The class is orthogonal to memory class (#720): an `instruction` can be
  `source_human`; an `observation` is by construction `fact_derived`.

## 2. The extraction pipeline

Recommended pipeline for artifact-backed knowledge; each stage's output is
the next stage's input and a first-class, recallable entity:

1. **Ingest** the raw artifact (`mimir_ingest_file`, `mimir_ingest`,
   `mimir_capture`) → `source_human` record with anchor + artifact hash
   (see `incremental-extraction-refresh.md`).
2. **Extract** facts from it (`mimir_extract`, capture classifiers) →
   `fact_extracted` entities, each citing the artifact via `derived_from`
   and carrying the artifact's `source_hash`.
3. **Derive** stable observations across facts (`mimir_consolidate`) →
   `fact_derived` with the full evidence set folded in.
4. **Synthesize** only when needed (`mimir_dream`, agent reasoning) →
   `inference_agent` insights over the derived layer.

Guidance:

- **Stop at the lowest sufficient stage.** If extracted facts answer the
  question, do not synthesize (retrieval policy §1, tier 4 last).
- Each stage links to its inputs; the chain artifact → extracted → derived
  → synthesized is walkable with `mimir_traverse` and is the audit trail.
- Skipping stages is allowed (an operator can assert a derived fact
  directly) but the write path must still attach evidence to claim
  `fact_derived`.

## 3. Retrieval scoring by provenance class

For **factual queries**, retrieval prefers the most trustworthy substrate
first. This refines taxonomy rule R3 (direct evidence beats inference):

- Candidate generation is unchanged; provenance class enters the composite
  ranking (Perseus `composite-retrieval-ranking.md`) through the
  `confidence`/trust channel: `fact_extracted` and `fact_derived` with
  intact evidence links get a class boost; `inference_agent` without an
  evidence set gets a class demotion; `source_human` artifacts are ranked
  for verification queries, not as first answers to factual ones.
- A factual query SHOULD prefer a derived fact over the large raw artifact
  it came from; the artifact is fetched at tier 2 for verification.
- Filters: recall SHOULD accept a `provenance_class` filter (post-filter
  over candidate metadata, like ref filtering — no reindex) so agents can
  ask for `fact_extracted`/`fact_derived` only.
- Scoring stays explainable: the class boost/demotion appears in
  `explanation.matched_on` ("provenance: fact_derived, evidence-linked ×2").

## 4. Surfacing provenance in context blocks

`mimir_context` and served views render the provenance class inline, one
token per item, so the agent can reason about confidence without expanding
the payload:

```
- Gate closed in prod 2026-07-20.            [fact_extracted · conf 0.72]
- Deploy windows drop webhooks.              [fact_derived · evidence ×2]
- Enable PLUTUS_SUBSCRIPTIONS_ENABLED next.  [inference_agent]
```

Items without a class render nothing (backwards compatible). When a served
`inference_agent` item carries no evidence set, the renderer SHOULD mark it
("ungrounded") rather than silently upgrade its apparent trust.

## 5. Success criteria mapping (#733)

- Lower hallucination risk: factual answers come from evidence-linked
  classes; ungrounded synthesis is visually and structurally marked (§4).
- Clear evidence/inference separation: the boundary is the presence of
  `derived_from`/`evidence_for`, machine-checkable (§1).
- Reusable structured memory: pipeline stages are recallable entities, not
  transient prompt artifacts (§2).
- Better summarization: synthesizers consume tier-1 derived facts with
  citations instead of raw corpora (retrieval policy §1).

## 6. Implementation slice

- Add `provenance_class` derivation (memory_kind + evidence links) at
  write/read time; optional lazy backfill by `source` mapping.
- `mimir_recall`: optional `provenance_class` post-filter; class signal
  into the trust channel of the composite score.
- Context/serving renderers: one-token class suffix per §4, "ungrounded"
  marker for evidence-less `inference_agent`.
- Golden tests: evidence-less dream insight classed `inference_agent`;
  consolidate output with sources classed `fact_derived`.
