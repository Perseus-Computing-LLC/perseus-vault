# Deterministic Recall, Provenance, and State Digest

This document specifies three contracts that let Mimir serve as the
deterministic backend for Perseus's `@memory` directive — backing the
resolve-before-context reproducibility and auditability claims.

Issues: #254 (determinism), #255 (provenance), #256 (state digest).

## 1. Deterministic recall ordering (#254)

`mimir_recall` (and the `recall` API) returns results in a **stable total
order**. The keyword (FTS5/LIKE) path orders by:

```
ORDER BY retrieval_count DESC, last_accessed_unix_ms DESC, id ASC
```

The trailing `id ASC` is a stable tie-break: when two entities tie on
`retrieval_count` and `last_accessed_unix_ms`, their relative order is fixed by
their immutable `id`, so the result is a total order rather than a
SQLite-scan-order coin flip.

The dense and hybrid paths are likewise deterministic: dense ranking and the
reciprocal-rank fusion use a content-derived id tie-break and issue no
access-state writes, so repeated hybrid recalls are idempotent and byte-stable
(see `recall` and `reciprocal_rank_fusion` in `src/db.rs`, #247).

### Reproducible recall over a frozen DB

For Perseus to assemble byte-identical context across runs, recall must not
perturb its own sort keys. Set `skip_side_effects = true` (MCP: the recall path
that does not bump access state). With side-effects skipped, `retrieval_count`
and `last_accessed_unix_ms` do not change on read, so over an unchanged DB the
ordering — and therefore the rendered `@memory` block — is byte-identical every
time. This is proven by
`db.rs::tests::recall_is_deterministic_on_frozen_db_with_ties`.

### Pinning recall to an instant (`as_of`)

Mimir's bi-temporal `mimir_as_of` returns the version of a fact that was live at
a given transaction-time instant. Recall reproducibility can therefore be
pinned not only to "current frozen state" but to a fixed historical instant:
re-running an `as_of` query reproduces exactly what was believed then, even
after later writes. This is the strongest reproducibility primitive Mimir
offers and directly supports the patent's reproducibility limitation.

## 2. Provenance on recall results (#255)

Every entity returned by `mimir_recall` carries a complete provenance record.
`to_json_expanded()` serializes the full `Entity`, so each result includes:

| Field | Meaning |
|---|---|
| `id` | Stable unique identity of the memory |
| `category` / `key` | Logical address (unique per active entity) |
| `source` | Where the entity came from (`agent`, `user`, connector name, ...) |
| `agent_id` | Which agent wrote it (attribution) |
| `verified` | Whether the entity has been verified |
| `certainty` | Confidence 0.0–1.0 (trust signal; drives ranking boost) |
| `created_at_unix_ms` | When the entity was first recorded |
| `last_accessed_unix_ms` | When it was last recalled |
| `retrieval_count` | How many times it has been recalled |
| `decay_score` | Current Ebbinghaus decay score |
| `workspace_hash` | Workspace scope (empty = global) |
| `visibility` | `private` / `workspace` / `public` |

These fields are stable and serializable, so Perseus can fold them into a
citation/provenance segment of a rendered exhibit: each resolved `@memory`
result is attributable to a specific entity id, source, author, and timestamp.
This supports the "provenance/audit record of each resolved directive"
dependent claim and the §101 auditability argument.

## 3. State digest for cache-keying (#256)

`mimir state-digest --db <path>` (and `Database::state_digest()`) returns a
cheap, deterministic content digest of the recall-visible (non-archived) entity
set:

```json
{ "digest": "9f3a1c77b2e40d51", "entity_count": 842 }
```

### Contract

- **Stable** while relevant state is unchanged: recomputing the digest on an
  unchanged DB yields the same value. Recall access-state bumps
  (`retrieval_count`, `last_accessed_unix_ms`) do **not** change it — the digest
  is over content (`id`, `body_json`), not access metadata.
- **Changes iff relevant state changes**: inserts, in-place edits (including
  edits that preserve body length), and archival (which removes a row from the
  recall scope) all change the digest. A length-only or count-only signal would
  miss same-length edits; this digest does not.
- **Order-independent**: computed as an XOR fold of per-row FNV-1a hashes over
  `(id, NUL, body_json)`, so it does not depend on SQLite scan/return order. The
  entity count is mixed into the final hash to preclude XOR cancellation
  collisions between states of different cardinality.
- **Cheap**: a single sequential scan of `(id, body_json)` — no embedding, no
  network — cheap relative to any recall that embeds a query.

### Use as a Perseus cache key

Perseus can cache a resolved `@memory` output keyed by
`(directive_args, mimir_state_digest)`. While the digest is unchanged the cached
resolution is valid; when any relevant memory changes, the digest changes and
the cache entry is naturally invalidated. This backs the "caching of resolved
directive outputs keyed by source state" dependent claim.

Proven by `db.rs::tests::state_digest_changes_iff_state_changes`.
