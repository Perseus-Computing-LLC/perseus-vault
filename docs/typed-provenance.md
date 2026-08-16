# Typed provenance edges + evidence/execution split

Status: **Implemented** (2026-08-16, #1064). Vocabulary: arXiv:2606.04990
(From Agent Traces to Trust) and the Agent-Sentry parameter-lineage pattern
(arXiv:2603.22868).

## 1. Typed relation set

Five deterministic kinds over the link graph, enforced at the write boundary:

| Kind | Semantics | Write-boundary validation |
|---|---|---|
| `supports` | Evidential support (legacy `derived_from` / `evidence_for` classify here) | target must exist; edge carries the asserting record as evidence anchor |
| `contradicts` | Explicit conflict with the target's content | target must exist; anchor required |
| `invalidates` | Supersession of the target's truth-value (legacy `supersedes`) | target must be a live entity |
| `updates` | Newer revision (legacy `promoted_to`) | target must exist |
| `authorized_by` | Permission provenance | target must be an `authority_manifests` row or an `authorized_actions` receipt — never ordinary content |

Legacy free-form relations classify deterministically at read time via
`classify_relation`; unknown strings fall back to `related` (never silently
upgraded). Typed writes go through `link_typed` (MCP `perseus_vault_link`
gains a `kind` parameter); `link` remains the legacy path.

**Provenance ≠ authorization ≠ truth.** A cited item is not thereby
supporting evidence; a recorded action is not thereby permitted or true.
The typed kinds keep those questions separate.

## 2. Evidence vs execution projections

`perseus_vault_provenance_projection` (mode):

- `evidence` — bounded BFS over the typed edge graph: what supports /
  contradicts / invalidates / updates / authorizes what, with classified
  kinds and evidence anchors.
- `execution` — what the agent DID with the entity: journal events
  referencing it, plus **blocked-action receipts** (denied/revoked/failed/
  expired `authorized_actions` rows) — the AAR shape extended into the
  graph: a blocked action retains intent + failure receipt here, not only
  in the journal.

## 3. Parameter-level lineage (Agent-Sentry pattern)

`perseus_vault_param_lineage` records where a sensitive tool-argument value
came from (`param_lineage` table, schema v52). Every query validates the
source: a dangling `source_ref` returns `resolved: false` — forged lineage
is surfaced, never trusted. Claim/run-level traces for low-risk flows are
the ordinary journal events; parameter-level records are for the
high-risk few.

## 4. Tests

Per-kind write validation (authorized_by target classes, invalidates
liveness), deterministic legacy classification, evidence/execution
separation, blocked-action receipts in the execution projection, lineage
record/query with dangling-source detection.
