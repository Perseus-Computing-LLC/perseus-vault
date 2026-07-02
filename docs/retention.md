# Retention, Decay, and Forgetting

Perseus Vault forgets on purpose. This page documents exactly when a memory
fades, when it is archived, when it is deleted, and how to opt a memory out of
each stage. All numbers below are the shipped constants in `src/db.rs`; if this
page and the code disagree, the code wins and this page has a bug.

## The lifecycle at a glance

```
remember ──▶ active (buffer) ──▶ working ──▶ core        promotion by USE
                │
                │ idle time (Ebbinghaus decay)
                ▼
        decay_score < 0.05  ──▶ archived (auto)          forgetting by DISUSE
                                    │
                                    │ explicit `purge`
                                    ▼
                                 deleted (permanent)
```

Nothing is ever deleted automatically. Automatic forgetting stops at
**archived**, which is reversible; only an explicit `purge` deletes rows.

## Decay: forgetting by disuse

Every entity carries a `decay_score` in `[0.0, 1.0]` recomputed from idle time:

```
decay_score = e^(−idle / 7 days)
```

(`DECAY_HALF_LIFE_MS = 7 days` — the name is historical; the curve is `e^-x`,
so the score is ~0.37 after 7 idle days, not 0.5.)

Reference points:

| Idle time | decay_score |
|---|---|
| just accessed | 1.0 |
| 7 days | ~0.37 |
| 14 days | ~0.14 |
| ~21 days | 0.05 → **auto-archived** |

Being recalled resets the clock and additionally boosts the stored score by
`DECAY_BOOST = 0.25` (capped at 1.0), so memories that keep getting used stay
comfortably above the archive line.

## The archive threshold — one number, everywhere

`ARCHIVE_DECAY_THRESHOLD = 0.05`. An entity whose recomputed score falls below
it is archived with an `archive_reason` explaining why. The same constant is
shared by every path that forgets:

- `decay_tick` (the explicit decay pass),
- `cohere` (the coherence groomer's gentle ×0.95 decay step),
- `autocohere`'s compact step.

This is deliberate: before v2.12.x, `autocohere` compacted at a hardcoded 0.1
(~16 idle days) while the individual tools used 0.05 (~21 days), so running
"everything" forgot ~5 days sooner than any single tool.

## Exemptions: how a memory opts out of forgetting

| Mechanism | Effect |
|---|---|
| `verified: true` | `decay_score` floored at `VERIFIED_DECAY_FLOOR = 0.2` — a verified fact can fade but is **never auto-archived**. |
| `always_on: true` | Injected into `context`/`prepare` blocks regardless of decay; being injected does not itself bump retrieval stats. Under the recall-first default (see below) the always-on set is hard-capped at 5 entities and counts against the context budget — overflow truncates and warns. Reserve it for identity-critical facts; prefer `recall_when` triggers. |
| `mimir_score` (importance) | The explicit score is stored as a persistent `importance` floor: `decay_tick` and `cohere` never recompute `decay_score` below it, so a scored memory survives idle time indefinitely (fidelity beats recency). Re-score with `0.0` to clear. |
| regular use | Every recall boosts the score by 0.25 and resets the idle clock. |

The verified floor exists because curated facts match few queries and are
rarely recall-boosted; without it they decayed below 0.05 and were silently
forgotten while chatty low-value memories that match everything stayed hot
(#298).

## Layers: promotion by use

Layer is a function of `retrieval_count`, shared by the recall side-effect
path and `cohere`'s promotion step (unified in v2.12.x — cohere previously
promoted at 3 while recall promoted at 5, so 3–4-retrieval entities
oscillated):

| Layer | Threshold |
|---|---|
| `buffer` | fewer than 5 retrievals (`WORKING_THRESHOLD`) |
| `working` | ≥ 5 retrievals |
| `core` | ≥ 20 retrievals (`CORE_THRESHOLD`) |

Layers affect ranking and `recall_layer` filtering; they do not change the
decay math.

## Archived is not deleted

Archived entities keep their row, body, links, and history. They are excluded
from recall (unless `include_archived` is set) and from `context`/`prepare`
injection. Recovery is a `remember` to the same `(category, key)` or manual
un-archiving.

Deletion is explicit and two-step:

- **`prune`** — archive (not delete) entities matching filters you choose
  (category, `decay_score` below a cutoff, older than N days).
- **`purge`** — permanently delete entities that are **already archived**.
  Supports `dry_run`. This is the only way memory leaves the database.

## Consolidation ("local dreaming")

Decay forgets one memory at a time; consolidation compresses instead of
losing. `mimir_consolidate` merges overlapping same-category entities into a
single evidence-tracked *observation* (category `observation`, linked to each
source via `evidence_for`, carrying a `proof_count`). Two opt-in flags shape
it into background forgetting:

- `cold_first: true` scans the longest-idle entities first — the ones decay
  is about to claim — so fading knowledge is compressed before it is lost.
- `archive_sources: true` retires the merged sources once the observation
  exists (`archive_reason` names the observation, so the merge is traceable
  and reversible). **Verified or importance-floored sources are never
  archived** — the same exemption promise decay makes.

`mimir_autocohere` runs a bounded pass automatically (a few observations per
category per run, cold-first, archiving sources), skipping the `observation`
category (no meta-observations) and `memories` (files from the /memories
adapter are never similarity-merged).

## Recall-first injection (the context/prepare default)

Retention decides what the vault *keeps*; injection decides what a turn
*sees*. Since #356/#366, `mimir_context` and `perseus-vault prepare` are
**recall-first** (`mode: on_demand`) by default:

- Only entities topically relevant to the supplied `query` (the current
  task/message) are injected — matched via `recall_when` triggers and
  stopword-filtered keyword search, workspace-scoped when a
  `workspace_hash` is supplied. A high `retrieval_count` still ranks
  entities *within* the matched set, but can no longer push a topically
  unrelated memory into context at all.
- Without a `query`, no topical entities are injected — the block is a
  compact retrieval pointer, byte-stable across unrelated vault writes.
- Output is clamped to a per-model character budget: 1500 chars by default,
  6000 for large-window ("opus") hosts, `max_context_chars` to override.
- The always-on set is hard-capped at 5 entities (see the exemptions table);
  overflow truncates and emits a warning.
- Injected blocks are framed as *informational* memory, not authoritative
  instructions.

The legacy unconditional top-N dump remains available as an explicit opt-in
(`mode: "always_inject"` on `mimir_context`, `--legacy-context` on
`prepare`) and is unclamped unless a budget is passed. The gRPC `context`
RPC keeps the legacy semantics for wire compatibility.
## Dreaming (LLM consolidation, episodic → semantic)

Consolidation compresses *duplicates*; `mimir_dream` goes one step further and
**reasons** over clusters of merely *related* memories. It batches the coldest
entities per category (cold-first by default — consolidate fading memories
before decay claims them), sends each trigram-neighborhood cluster to the
configured LLM ("given these N memories, what stable pattern / preference /
fact do they collectively imply?"), and writes the answer back as a durable
**semantic insight** (category `insight`, `working` layer — the canonical
storage layer for the `semantic` biomimetic alias). Properties:

- **Full provenance** — every insight links `evidence_for` to each source
  entity, and its body carries `derived: true`, `derivation: "dream"`, and the
  source ids, so it is auditable and reversible.
- **Never fabricates** — insights need at least two cited sources; clusters
  that support no durable generalization are a no-op. LLM output is parsed
  strictly (unknown types, empty summaries, out-of-range evidence indices are
  dropped, never repaired into a write).
- **Idempotent** — insights are keyed by a hash of their evidence set, so
  re-dreaming an unchanged cluster dedupes instead of duplicating.
- **Contradiction-aware** — disagreeing sources become a flagged
  `contradiction` insight (sources always stay live), never a silent merge.
- **Bounded** — `max_entities` caps the scan, `max_clusters` caps LLM calls.
- **Same archive safety rules** — opt-in `archive_sources` retires dreamed
  sources (`archive_reason` names the insight), but **verified or
  importance-floored sources are never archived**.

Dreaming requires `--llm-endpoint` (fully local via Ollama). Without one it
returns a clean error — or, with `fallback_consolidate: true`, degrades to the
mechanical `mimir_consolidate` cold-first pass. `dry_run: true` previews the
candidate insights and their evidence sets without writing anything (not even
a journal entry).

## Semantic recall and reinforcement

By default, retrieval reinforcement fires only on the keyword (`fts5`) recall
path; the hybrid/dense paths are side-effect-free so recall over a frozen DB
stays byte-deterministic (#247, see
`deterministic-recall-and-provenance.md`). A memory that is only ever found
semantically therefore decays as if unused — unless you opt in:

- **`reinforce: true`** on `mimir_recall` with `mode: 'dense'`/`'hybrid'`
  applies the standard side-effects (retrieval-count bump, recency reset,
  +0.25 decay boost, layer promotion) to the returned hits. This trades
  byte-determinism of *subsequent* recalls for "used memories resist decay" —
  the recall that carries the flag still returns the same ranking it would
  have without it.
- Alternatively, mark load-bearing memories `verified` (decay floor) or
  `always_on` (unconditional injection) and keep semantic recall pure.

`skip_side_effects` always wins over `reinforce`: a caller that asked for a
pure read never mutates.
