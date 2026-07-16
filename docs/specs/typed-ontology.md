# Feature Spec: Typed Memory Ontology — naming the buckets

**Status:** implemented (the `type` field, write param, and recall filter shipped incrementally; #694 named the ontology and surfaced `type` in the rendered context bundle)
**Depends on:** existing `entities.type` column (`src/schema.rs`), the `remember`/`capture` write path, and the recall filter (`RecallArgs.type`)
**Competitive driver:** frozo-vault-mem markets a fixed 7-type ontology (decisions/observations/learnings/todos/entities/questions/summaries) as its #1 differentiator. Mem0 treats every memory as one undifferentiated blob. Perseus Vault has always been typed — it just hadn't named the buckets.

## Problem

Perseus Vault has stored a `type` on every memory since early schema versions
(`entities.type TEXT DEFAULT 'insight'`), it accepts `type` on write, and it
already filters recall by `type`. But the ontology was **undocumented and
invisible**: no page named the vocabulary, and the rendered context bundle
(`## Perseus Vault Context`) never showed a memory's type, so an agent reading
its own recalled context couldn't tell a `decision` from an `observation`.

That is the whole gap #694 identifies — and the issue says it plainly: *"We
already have a richer data model — we just haven't named the buckets. This is
80% marketing, 20% engineering."*

## What ships

1. **This spec** names the ontology and its canonical vocabulary (below).
2. **`type` is surfaced in the rendered context.** Every line in the context
   bundle now carries its type:
   ```
   - [decision] **retrieval-default** — … (type: decision, retrievals: 4, decay: 0.91)
   ```
   so the agent can weigh a durable `decision` differently from a fast-moving
   `observation` without a second lookup. (`src/db.rs`, `context_block`.)

No schema migration. No change to what is stored. No change to recall ranking.

## The ontology

Memories are typed by the free-form `type` field. The **canonical vocabulary**
actually written by the vault today:

| Type | What it captures | Lifecycle intuition |
|---|---|---|
| `insight` (default) | a general learning worth keeping | medium-lived |
| `decision` | a choice made, with rationale | long-lived; supersede, don't delete |
| `architecture` | how something is structured | long-lived |
| `convention` | a standing rule/norm the agent should follow | long-lived |
| `reference` | a pointer to an external resource | as long as the target lives |
| `correction` | a fix that supersedes a prior belief | decays the belief it corrects |
| `lesson` | a takeaway from an outcome | medium-lived |
| `pitfall` / `root-cause` | a failure mode and its cause | medium-lived |
| `pattern` | a recurring shape worth reusing | medium-lived |
| `observation` / `fact` | a point-in-time state of the world | short-lived; often superseded |
| `benchmark` | a measured result | tied to the artifact it measures |
| `contradiction` | a detected conflict between memories | resolved by supersede |
| `document` / `file` | ingested source material | tracks the source |

This **covers and exceeds** the competitor's fixed 7 buckets: their
`decisions`/`observations`/`learnings`/`summaries` map onto
`decision`/`observation`/`lesson`+`insight`/`insight`; their `entities` are the
vault's first-class entity model; `todos`/`questions` are deliberately left to
task trackers rather than pretending memory is a queue.

## Design stance — why free-form, not a hard enum

The vocabulary is **canonical but not enforced**. A hard enum was rejected:

- **Non-breaking.** 1,300+ existing memories carry free-form types. A closed
  enum would either reject or silently rewrite them — data churn for a naming
  exercise.
- **Extensible.** Connectors and the capture distiller mint their own useful
  types (`root-cause`, `takeaway`, `document`, …). A closed set would fight
  those producers.
- **The value is the *named, visible* taxonomy**, not rejection at the write
  boundary. Recall already filters on whatever type was stored.

Writers should prefer the canonical set above; unknown values remain valid and
searchable.

## Deliberately deferred (measurement-gated)

- **Per-type decay curves.** The issue proposes decisions decay slowly and
  observations fast. That is plausible, but decay is a tuned Ebbinghaus curve
  (`Database::compute_decay`) and per-type multipliers must be *measured*
  against recall quality before shipping — we do not tune retrieval on
  intuition (see the perf/recall gauntlet discipline). Tracked as a future
  experiment, not shipped here.
- **`type` filter on `context`/`memories`.** `recall`/`recall_batch`/
  `global_recall` already accept `type`; extending it to the `context` and
  `memories` tools is a small, safe follow-up.

## See also

- [Session Lifecycle Hooks](../lifecycle-hooks.md) — where `remember` (write)
  and `context` (render) sit in the loop.
- [Retrieval modes](../retrieval-modes.md) — how `type` composes with the recall
  filters.
