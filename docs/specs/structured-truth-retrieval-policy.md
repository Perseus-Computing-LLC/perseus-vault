# Structured truth retrieval policy

Status: design specification
Date: 2026-07-21
Strategy frame: [perseus-vault-durable-cognition-strategy-2026-07-20](../strategy/perseus-vault-durable-cognition-strategy-2026-07-20.md)
Resolves: #736 (umbrella), #738
Related: `provenance-classes-derived-facts.md` (classes, pipeline),
`incremental-extraction-refresh.md` (freshness), `graph-first-retrieval.md`
(lineage questions), `hybrid-retrieval-ranking.md` (ranking dimensions),
`memory-provenance-and-external-refs.md` (origin fields),
`served-memory-api.md` (serving layer). Cross-repo: Perseus
`docs/composite-retrieval-ranking.md`, perseus#843 (orchestration).

This is the umbrella policy for structured-truth retrieval across Perseus +
Vault. It fixes two things: the **order** in which retrieval substrates are
consulted, and the status of **external structured indexes** (IDE
code-intelligence, domain fact maps such as AFM Facts Map) as first-class
upstream evidence sources. It is a policy contract: it constrains how
agents and renderers compose existing Vault tools; it adds no storage
engine and no new ranking math (ranking lives in the hybrid spec).

## 1. The retrieval order

Agents answering a question from memory MUST consult substrates in this
order, stopping as soon as the question is answered with adequate
confidence:

| Tier | Substrate | Vault surface | Use for |
|---|---|---|---|
| 1. Structured truth | typed, extracted/derived entities with provenance | `mimir_recall` on extracted/derived classes, belief overlay, `mimir_traverse` for lineage | factual, impact, lineage, dependency questions |
| 2. Targeted source fetch | the specific artifact an entity anchors to | `external_refs` / anchors, `mimir_get_entity` drill-down | verification, gap-filling, quoting exactly |
| 3. Broad search | keyword/semantic sweep over the whole corpus | `mimir_recall` fts5/hybrid unfiltered, connector corpora | discovery when tiers 1–2 miss |
| 4. Synthesis | deriving a new answer from evidence | `mimir_dream`, `mimir_synthesize`, agent reasoning | only when no stored truth answers the question |

Rules:

- **Synthesis is last, not first.** Generating prose over stuffed raw
  documents before consulting structured truth is a policy violation; it
  costs tokens, loses provenance, and raises hallucination risk.
- **Tier descent is recorded.** When a workflow descends past tier 1, the
  served explanation (`served-memory-api.md` §2) SHOULD name the tier that
  produced the answer ("tier-3 broad search; no structured truth found").
- **Tier 2 is verification, not substrate.** Raw artifacts are fetched to
  confirm or refresh structured facts, not as the default thing to read.

## 2. Query-type heuristics

| Question shape | Starts at tier | Notes |
|---|---|---|
| "what is X / what is the state of X" | 1 | `mimir_recall` with provenance filter (see provenance spec) |
| "what depends on / supports / follows from X" | 1 (graph) | `mimir_traverse` per `graph-first-retrieval.md` |
| "quote the exact wording / show the source" | 2 | resolve anchor, fetch artifact |
| "what do we know about <new topic>" | 1→3 | tier 1 miss escalates to broad search |
| "summarize / what should we conclude" | 1→4 | synthesize over tier-1 evidence, citing it |

## 3. External structured indexes as first-class sources (#738)

Some environments already expose structured retrieval surfaces that are
better starting points than raw text: IDE code-intelligence indexes
(definition/reference/symbol graphs), domain fact maps (AFM Facts Map),
billing/CRM entity indexes, static-analysis outputs. Policy:

- **Model them as upstream source systems, not rivals.** An external index
  is an `origin.source_system` value (`connector:<name>`, e.g.
  `connector:jetbrains-psix`, `connector:afm-facts-map`) per
  `memory-provenance-and-external-refs.md` §1. Vault does not re-index what
  the external index already resolves; it records facts *obtained from* it.
- **Prefer them before broad grep.** In the retrieval order, an available
  external structured index sits at tier 1 alongside Vault's own structured
  truth: query the index (or facts previously imported from it) before
  scanning raw files or documents.
- **Import selectively.** Bring externally-indexed facts into Vault when
  they need durability, correction, supersession, or cross-workspace
  sharing; otherwise leave them in place and store only an anchor
  (`external_refs` with `ref_type: custom` or the fitting type) pointing at
  the external record.
- **No permission implication.** As with anchors generally, a reference to
  an external index record is a pointer; access is the consumer's problem.

## 4. Provenance recording for externally-sourced facts

A fact derived from an external structured index MUST record:

```json
{
  "origin": {
    "memory_kind": "imported",
    "source_system": "connector:afm-facts-map",
    "capture_method": "import",
    "observed_at": "<when the index asserted it>"
  },
  "external_refs": [{
    "ref_type": "custom",
    "ref_value": "afm:Customer/Acme#outstanding_invoices",
    "source_system": "afm-facts-map",
    "relationship": "derived_from"
  }]
}
```

This distinguishes "extracted from raw transcript" (`memory_kind:
extracted`) from "read from an already-structured index" (`imported` with a
structured `source_system`) — the latter is stronger evidence and ranks
accordingly (provenance spec §3). The `ref_value` must be stable enough to
re-fetch for tier-2 verification and for incremental refresh.

## 5. Debug traces

Retrieval decisions SHOULD be inspectable. When explanation mode is on, the
serving layer records: tiers consulted, tier that answered, why descent
happened (no candidates / low confidence / anchor miss), and any external
index queried. This is the trace vocabulary for perseus#843's debug output.

## 6. Non-goals

- Building an AFM-style code facts engine inside Vault.
- Replacing broad search; tier 3 stays for discovery and cold topics.
- Forcing every workflow through a planner; the order is a default policy
  agents follow, overridable by explicit operator instruction (recorded as
  a correction).

## 7. Implementation slice

- Document the tier order in the operator/agent-facing retrieval guidance
  (this spec is the text); reference it from served-memory explanations.
- Add `origin` / `external_refs` write-path support for connector imports
  per §4 (metadata only, no schema change — provenance spec §3).
- Wire tier-descent naming into the served-memory explanation payload.
- Acceptance: a factual question answered from tier-1 structured truth
  carries `why_served` naming tier 1; a descent to broad search names
  tier 3 and the reason.
