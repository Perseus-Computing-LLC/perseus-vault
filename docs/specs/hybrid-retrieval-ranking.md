# Hybrid retrieval ranking: signal dimensions for Vault recall

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #737
Related: `structured-truth-retrieval-policy.md` (retrieval *tiers*),
`graph-first-retrieval.md` (link boosts), `provenance-classes-derived-facts.md`
(trust channel), `memory-taxonomy-and-precedence.md` (precedence rules),
`served-memory-api.md` (evidence-backed ranking, explanations). Cross-repo:
Perseus `docs/composite-retrieval-ranking.md` (**defines the score**),
perseus#831 (its implementation slice).

Semantic-versus-grep is the wrong frame: good retrieval combines lexical,
structural, semantic, and memory signals and ranks them against the
question shape. **The composite score itself is defined once, in Perseus
`docs/composite-retrieval-ranking.md` §1, and is not redefined here.**
This spec is the Vault-side companion: it documents the ranking dimensions
as a first-class concept *separate from* the retrieval tiers, maps each
dimension to the Vault signals and recall knobs that already exist, and
fixes how the dimensions interact per question type.

## 1. Two orthogonal axes: tiers vs dimensions

Confusing these is the design error this spec exists to prevent:

- **Retrieval tiers** (`structured-truth-retrieval-policy.md` §1) answer
  *which substrate do we consult, in what order*: structured truth →
  targeted fetch → broad search → synthesis. Tiers are a policy over
  *where answers come from*.
- **Ranking dimensions** answer *within a candidate pool, in what order do
  results come out*: the eight weighted components of the composite score
  (lexical, structural, semantic, freshness, support, confidence,
  staleness, contradiction). Dimensions are a policy over *how candidates
  sort*.

A query touches both: the tier policy picks the substrate, precedence
rules R1–R5 partition candidates, and the dimensions rank within each
partition (composite spec §2). Documenting them separately keeps "we
should search the graph first" (tier) from being argued as "raise w_sem"
(dimension).

## 2. Dimension inventory: Vault-side signals

Each composite component, and the Vault signal that feeds it today:

| Dimension | Vault signal source | Existing knob / field |
|---|---|---|
| lexical | FTS5 rank; exact identifier/literal hits | `mimir_recall` mode `fts5`; `content_weight` boost |
| structural | scope proximity (repo > workspace > global), topic proximity | `workspace_hash`, `topic_path`, `scope_weight` |
| semantic | embedding cosine similarity; RRF fusion in hybrid | mode `dense`/`hybrid` (`mimir_semantic_search`) |
| freshness | decay + time since reinforcement | `decay_score`, `last_accessed`, `min_decay`, `recency_half_life_secs` |
| support | independent supporting entities (dedup-folded) | belief overlay `support_count`; served-memory §3 |
| confidence | verified × certainty, provenance class | `certainty`, `mimir_score` floors, `trust_weight`; provenance spec §3 |
| staleness | age past `valid_from`; `valid_to` exceeded → exclude | valid-time fields; `valid_at` filters |
| contradiction | live conflict flag | `mimir_conflicts` pairs |

Graph relationships are an eighth input in practice: on lineage questions
the path-proximity boost (`graph-first-retrieval.md` §4) enters through
the structural channel. No new signals are invented here — the spec's
claim is that these existing signals should be *documented and tuned as
one ranking model* instead of per-tool side knobs.

## 3. Interaction by question type

The composite weights are fixed per deployment (composite spec §3 tuning
contract); what varies by question shape is *which candidates reach
scoring* and which boosts apply:

| Question type | Tier entry | Dominant dimensions | Notes |
|---|---|---|---|
| Factual ("what is the gate state") | 1 | lexical, confidence, freshness | exact identifiers and provenance class decide; semantic breaks ties |
| Narrative ("what happened with plutus last week") | 1–3 | freshness, semantic, support | recency weighting (`recency_half_life_secs`) is appropriate here |
| Lineage/impact ("what depends on X") | 1 (graph) | structural (path proximity), support | graph boost active; keyword only finds the hub |
| Verification ("quote the source") | 2 | lexical | artifact text, not entities; ranking barely matters |

Explicit non-goal: per-question weight switching. Weights stay in one
config block (composite spec §3); question shape selects boosts and
candidate pools, not a parallel weight set.

## 4. Score boosts and demotions (Vault conventions)

Consistent with, and subordinate to, the composite spec:

- **Freshness**: reinforcement refreshes `decay_score`/`last_accessed`;
  the freshness dimension rewards it. `min_decay` remains a candidate
  filter, not a ranking input.
- **Supersession**: `valid_to` exceeded or `superseded` status is a hard
  exclusion from current-fact serving (R4), applied *before* scoring —
  never a soft penalty that a high-semantic match could overcome.
  Superseded entities remain rankable in history/audit views.
- **Support**: `support_count` folds near-duplicate merges once (served
  memory §3) so one repeated artifact cannot masquerade as broad support.
- **Provenance**: the class boost/demotion (`provenance-classes-derived-facts.md`
  §3) enters through the confidence channel, not a ninth dimension.
- **Contradiction**: live-conflict pairs are penalized per the composite
  weight (`w_contra`, the only weight > 1 by default) and surfaced to the
  contradictions view (R5) rather than silently resolved.

## 5. Renderer contract

Downstream renderers (Perseus context, `mimir_context`, served views)
SHOULD preserve ranking signals when useful rather than flattening them:

- Served items already carry `matched_on` and confidence (served-memory
  §2); renderers keep those lines intact.
- When a result outranked a higher-similarity rival due to freshness,
  support, or provenance, the explanation names the deciding dimension —
  ranking is debuggable, not just tunable (composite spec §3).
- Renderers MUST NOT re-sort served items by their own similarity
  heuristic; precedence + composite order is the product.

## 6. Success criteria mapping (#737)

- Better ranking on mixed query types: dimensions documented as one model
  with per-question-shape guidance (§1, §3).
- Less single-primitive reliance: every dimension has a named Vault
  signal; lexical-only or semantic-only operation is a degraded mode, not
  the design (§2).
- Storage-to-answer transfer: boosts for freshness, supersession, support,
  and provenance are explicit conventions renderers preserve (§4–§5).

## 7. Implementation slice

- Surface per-dimension component scores in recall's explanation flag
  (composite spec §5 tracks the shared work in perseus#831; Vault adopts
  the same component names so traces join across repos).
- One documented config block for weights, shared vocabulary with the
  composite spec's defaults.
- Golden-vector tests on the Vault side: fresh-local beats stale-global
  regardless of similarity (R2); supported beats one-off within a tier;
  superseded excluded pre-scoring; contradicted penalized.
- Calibration against the memory-benchmarks harness, before/after noted
  in the composite spec's calibration run.
