# Per-type retrieval context budgets (#1008)

Status: normative. Surface: `budget_profile` on recall args
(`diverse` | `fact_lookup` | `broad`), `fused_trace.truncation.per_type`.

MindCache partitions its RRF candidate pools by memory type (episodic 40 /
knowledge 40 / user 30 / decision 30 / summaries 10) and enforces per-type
caps at final context assembly — diverse evidence consistently beats a wall
of similar high-scoring memories, and a temporary event should never crowd
out a long-term preference because vector similarity alone over-ranks one
type. With typed classes landed (#1000), this is the retrieval-side
counterpart: a SELECTION layer over the fused pool.

## Mechanics

- **Floors first**: for each (class, n) in the profile, the top-scored n
  items of that class are pulled up — even from below the caller `limit`.
  Shortfalls are recorded in the report, never fatal.
- **Caps second**: the ranked walk retains in pool order, skipping any item
  whose class count has reached its cap, until `limit` is reached.
- **Allocation class**: decision-category rows map to the pseudo-class
  `decision` (standing policy gets its own guaranteed lane — the vault's
  MindCache-equivalent of their decision budget); everything else maps by
  `memory_type`, legacy `''` normalized to `semantic` (#1000).
- **Token budget composes**: the #942/#883 token-budget truncation still
  runs after shaping; floor items front-load the order so the token budget
  drops tail items first.
- **Observable**: `FusedTruncationTrace.per_type` reports each class's
  floor/cap/retained/shortfall — allocation is never silent.

## Profiles

| class | diverse f/c | fact_lookup f/c | broad f/c |
|---|---|---|---|
| decision | 3/8 | 2/6 | 4/10 |
| constraint | 2/8 | —/8 | 3/10 |
| semantic | —/15 | —/20 | 3/12 |
| episodic | —/10 | —/5 | —/8 |
| knowledge | —/10 | —/15 | —/12 |
| procedural | —/10 | —/8 | —/6 |
| preference | —/5 | —/8 | —/4 |
| failure | —/5 | —/8 | —/4 |
| reflection | —/5 | —/8 | —/4 |

## Guards

- Opt-in: `budget_profile` defaults to None — a default recall stays
  byte-identical (#247).
- Unknown profile names are a hard validation error (fail-closed, never a
  silent unshaped fallback).
- Deterministic: floors walk pool order; ties resolve stably.

## Evaluation

Composes with the #954 token-budget sweep: `budget × class allocation` as a
swept dimension under equal-token-budget comparisons (#916 harness).
Keystone-backed detection is deferred — decisions are the primary carrier.
