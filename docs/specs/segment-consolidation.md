# Semantic Segment-Level Consolidation (#1088)

Status: implemented · Source: LycheeMemory V2 (arXiv:2608.12990)

## Problem

Eager consolidation invokes the encoding pass after every interaction, so
memory-construction cost grows with conversation length. Coarse
summarization cuts cost but discards fine-grained evidence. Segment-level
consolidation makes construction frequency **segment-count-bound**, not
turn-count-bound, without changing the governance rules.

## Mechanism

`perseus_vault_segment_consolidate` (Ops):

1. **Segment detection** — deterministic boundary detection over the
   category's entities in arrival order (never fixed windows):
   - a new segment starts when the inter-arrival gap exceeds `gap_ms`
     (default 6h), or
   - when adjacent trigram similarity drops below `sim_floor` (default
     0.25) — a semantic discontinuity.
   The same dependency-free trigram family the dedup/consolidate machinery
   already uses.
2. **Segment-level encoding** — ONE bounded consolidate pass per finalized
   segment (≥2 members), scoped to that segment's candidate ids
   (`ConsolidateParams.candidate_ids`, additive — `None` preserves the
   exact pre-#1088 behavior). Singletons are skipped, never padded.
3. **Structured index** — each executed segment plan is durably indexed
   under a `segment_plan.<id>` state key (members, span, boundary reason),
   and the consolidate pass itself emits evidence-linked observations
   (supersedes edges to the members) — query-planned retrieval can find a
   segment's consolidated record without multi-hop LLM calls.
4. **Governance unchanged** — segments feed the existing #1002/#1026
   pipeline: granularity changes the schedule, not the authority rules
   (workspace-scoped ordinary runs, same fail-closed authorization).

## Success criteria vs implementation

- Construction-frequency bound: per-segment passes are asserted by test —
  two segments over five entities produce exactly two consolidation runs,
  and singletons produce zero.
- No recall regression / governance properties unchanged: the only
  consolidate-path change is the additive candidate filter; the full
  no-default suite (recall, provenance, governance) must stay green.
- Semantic-boundary coherence: pinned by the detection tests (time-gap,
  semantic-discontinuity, uniform-stream) plus the end-to-end fixture.
- Segment records provenance-tagged: supersede-linked observations +
  `segment_plan.<id>` state records (asserted).

Paper-scale construction-token reduction (≥50% on LoCoMo/LongMemEval-S)
requires the benchmark track (#1021/#1022) — the mechanism-level
segment-count-bound contract is what ships here.

## Schema

No schema change (state-keyed plans).
