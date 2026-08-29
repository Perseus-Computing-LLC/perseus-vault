# Non-authoritative experience projections

Issue #1173 adds a derived projection layer without changing the canonical entity model.

## Contract

`experience_projections` stores one current projection for an `(experience_id,
workspace_hash, principal_id)` scope. `workspace_hash` is also the current tenant
partition, so the stored `tenant_id` is equal to it. The source agent is derived
from the canonical sources and must be the same for every source in a projection.
The principal is the transport-stamped MCP `clientInfo.name`; callers cannot set it.

A projection row contains:

- `schema_version` and `projection_version`;
- `projection_revision`, explicit `observed_at_unix_ms`, and storage timestamps;
- the experience ID, tenant/workspace/principal/agent scope, graph side, and layer;
- canonical source IDs, serving-event IDs, and preload pulse IDs;
- bounded `activation`, `utility`, `preference`, and `confidence` signals;
- source and projection SHA-256 digests; and
- a derived lifecycle state: `active`, `stale`, or `quarantined`.

The normalized `experience_projection_sources` table makes dependency invalidation
exact. `experience_projection_events` records deterministic rebuild events and
contains only IDs, digests, scope, and timestamps.

No projection table or response contains entity bodies, raw prompts, credentials,
authorization material, `verified` claims, or caller-provided ranking values.
The projection has no authority or admission state of its own.

The provider-neutral transfer benchmark at `benchmark/experience_transfer/`
exercises the complementary question: whether a historical projection or experience
remains valid for current reuse after state, evidence, lineage, or authority changes.

## Relationship basis

A rebuild must name at least one canonical source entity and at least one accepted
Vault telemetry reference:

- `source_event_ids` must resolve to `served_events`, belong to the requested
  workspace, reference one of the canonical source IDs, and come from one serving
  batch. A non-empty event profile must match the transport principal.
- `pulse_ids` must resolve to `preload_events`, belong to the requested workspace,
  reference one of the canonical source IDs, and come from one preload session.

This prevents an arbitrary caller-supplied `experience_id` from merging unrelated
users, agents, workspaces, or conversations. The rebuild also rejects mixed source
agents and a layer label that does not match every source, unless `layer` is
`mixed`.

## Read path

`perseus_vault_experience_projection` is read-only. It first finds the exact
principal/workspace projection, then resolves every source through the ordinary
canonical requester-aware reader. Visibility, lifecycle admission, validity,
expiry, and supersession checks therefore remain owned by the canonical store.

If the row is missing, stale, quarantined, scope-mismatched, digest-mismatched, or
any source or accepted event is unavailable, the response has
`read_mode: "canonical_fallback"`, no projection signals or resolved sources, and
`fallback.mode: "canonical_retrieval"`. Consumers must perform ordinary canonical
recall instead of treating stale projection metadata as evidence.

Successful responses identify themselves as
`derived_experience_retrieval_projection` and set
`evidence_authority: "canonical_source_resolution"`. Source metadata is limited
to IDs, category/key, scope, lifecycle status, and validity grade. Bodies are not
copied into the projection response.

## Rebuild and determinism

`perseus_vault_experience_projection_rebuild` is Ops-scoped and writes the current
projection, normalized source links, and one idempotent rebuild ledger event in a
single SQLite transaction. Metrics are deterministic functions of canonical source
state and accepted reference count:

- activation is the bounded accepted-reference count;
- utility is the mean canonical follow rate;
- preference is the fraction of canonical sources typed as preferences; and
- confidence is the minimum bounded epistemic-state rank.

The caller supplies a fixed `query_time_unix_ms` replay anchor. Given the same
canonical source state, scope, configuration, accepted reference IDs, and anchor,
the source and projection digests and rebuild event ID are stable. A changed source
or configuration increments `projection_revision`; duplicate rebuilds do not add
ledger rows.

## Lifecycle and retention

Canonical content, validity, graph-edge, and status changes mark dependent
projections stale. Forget, prune, compact, decay, deduplication, and expiry quarantine
dependent projections. Physical erase and purge delete only dependent projection
rows and their derived ledger/relation rows. They do not delete canonical history
or alter the existing journal retention contract. History retention marks active
projections stale conservatively, requiring an explicit rebuild before projection
signals are served.

The existing recall, direct lookup, graph traversal, context injection, and export
paths do not read these tables. This keeps projection relevance separate from
ordinary recall and prevents a projection from becoming an unresolvable evidence
source.

## Verification

The module tests cover:

- transactional rebuild and canonical source resolution;
- stable digests and duplicate-event idempotence;
- cross-workspace fallback;
- unrelated event and unsupported schema rejection; and
- canonical source change detection without relying on an invalidation hook.

The MCP argument structs use `deny_unknown_fields`; forged `confidence`, `verified`,
body, prompt, credential, and authority fields are rejected before dispatch.
