# External-source sync contract: derived memories from factual sources

Status: design specification
Date: 2026-07-21
Resolves: #746
Origin: knowledge-kernel bridge discussion on
sowerkoku/knowledge-kernel#2 (provenance/staleness question, comment
4953880243; contract proposal, comment 4961600560; provenance
propagation shipped in the bridge at commit 02c0698)
Related: `memory-provenance-and-external-refs.md` (provenance
*representation* — this spec is the lifecycle *contract*),
`memory-operations-taxonomy.md` (#745 — I1 single factual authority),
`synthesis-hypothesis-lifecycle.md` (supersede machinery, #739).

External factual sources (knowledge-kernel, CMDBs, config databases,
registries) are the authority for their facts; Vault memories derived
from them are *copies at a snapshot*. Without a written contract,
every integration re-derives — or violates — the core invariant:

> **Invariant: memory must not become a second factual authority.**
> A derived memory may inform recall, but an agent needing the
> currently-verified fact must be able to tell that the memory is a
> snapshot copy, of what source state, and where the authority lives.

## 1. Identity = source id + snapshot hash

A derived memory is not "fact X" — it is **"fact X as of snapshot
H."** Its identity carries both:

- `source_entity_id` — the stable identifier in the source system
  (e.g. `kernel_entity_id`).
- `source_snapshot_hash` — the canonical source state that produced
  the memory (e.g. `dataset_hash`).

The snapshot belongs in the identity, not just the metadata: two
memories of the same entity from different snapshots are *different
derived facts*, and dedup must not fold them together. (Bulk-ingest
paths already use `skip_dedup` for the same reason — templated
records are similar by construction.)

## 2. Valid-time anchoring

The source snapshot becomes the memory's **valid-time anchor**: the
derived fact is valid from the snapshot's `observed_at`, not from
when Vault happened to sync it. This lets an agent later ask "what
did the source assert at snapshot H?" through the bitemporal
machinery (`valid_at` / `bitemporal`) and get a coherent answer even
after the underlying fact has moved on.

Transaction time still records when Vault learned the fact; the two
axes stay distinct (as_of = belief history, valid_at = source-asserted
history).

## 3. On change: supersede, never delete

When re-sync detects a changed entity (snapshot-hash diff, or a
source-provided change signal where available):

1. Write the new version with the new snapshot identity (§1).
2. Supersede the old version: link old → new, bound the old version's
   valid-time at the change point. **Never delete.**
3. Let confidence decay age the stale copy out of default recall.

The temporal chain stays intact for audit and debugging; recall
surfaces the current verified state by default; historical versions
remain reachable via `as_of` / `valid_at`. The bridge does not play
garbage collector — decay does.

**Hard-deletion exception.** A compliance-sensitive source may
*demand* erasure rather than decay. That is a source-declared
property, handled by the forget-then-purge path (which is
history-complete: superseded versions are evicted and journal
references redacted), never by the default sync flow. Integrations
must surface this requirement explicitly; the contract does not
guess it.

## 4. Provenance surfaced at recall

Derived memories carry their evidence chain so agents can
distinguish at read time:

- **Source-verified flag** (e.g. `kernel_verified`) — "this memory
  came from a verified external source," distinct from raw semantic
  recall. Rendering: perseus `trust-signal-rendering.md`.
- **Structured provenance** — `observed_at`, evidence hashes,
  `confidence_level` (high/medium/low/unknown), `source_type`
  (declared/discovered/imported/inferred), `validated`, source file
  or record reference — in `body_json.provenance` per
  `memory-provenance-and-external-refs.md`.

The flag travels with the entity; the full chain is available on
drill-down. Recall answers "verified fact from source" vs. "semantic
recall from memory" without a second round-trip.

## 5. Change detection

Two acceptable mechanisms, in preference order:

1. **Source-provided change signal** (event, version bump, etag) —
   precise, cheap; use when the source offers one.
2. **Snapshot-hash diff** — the integration recomputes the source
   snapshot hash and compares against the identity stored at last
   sync. Universal fallback; requires no source cooperation.

An integration MUST document which it uses. Silent drift — a source
changing without the integration noticing — is the failure mode this
clause exists to prevent, because it turns the memory layer into a
*stale* second authority, the worst of both worlds.

## 6. Multi-version reasoning

Default recall surfaces only the current version. Audit and
compliance queries may need to reason across *multiple* historical
versions of the same fact ("what did we assert about this endpoint
during the incident window?"). These are served by the bitemporal
machinery over the supersede chain (§3), not by keeping multiple
versions live in default recall. The contract keeps the common case
narrow and the audit case possible.

## 7. Applicability

This contract applies to any integration that copies externally-
authoritative facts into Vault. The knowledge-kernel bridge is the
reference implementation (provenance propagation at bridge commit
02c0698). New integrations should state compliance with §1–§5 in
their README and link here.
