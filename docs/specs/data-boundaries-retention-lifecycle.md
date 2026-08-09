# Data Boundaries & Retention Lifecycle Contract

> **Version:** 1 — 2026-08-09
> **Status:** accepted (implementation: `perseus_vault_expire`, `perseus_vault_redact`, `perseus_vault_erase`)
> **Issues:** #866 (data boundaries, consent, retention, deletion across derived stores) · #868 (retention semantics)
> **Supersedes:** ad-hoc `archive`/`purge` behavior documented piecemeal in README and tool docs.
> **Depends on:** #881/#882 (pre-write rejection gate + decoupled governance overlay), #849 (rejected-value tombstones), #854/#855 (workspace scope + attribution), #417 (workspace-scoped journal redaction).

## 1. Purpose and scope

This contract makes Perseus Vault's local-first and encrypted-memory posture
operationally legible: it names **every store, derived artifact, and processor
boundary**, defines the **distinct lifecycle operations** (expiry, supersession,
forget, redaction, logical deletion, physical erasure), specifies **deletion
propagation** to derived layers, and states **exactly what audit evidence may
survive content erasure**.

The contract is versioned; any change to the semantics below is a new version
of this document and ships with a migration/test change in the same PR.

## 2. Data-flow matrix: stores, copies, and processor boundaries

| # | Store / artifact | Table / file | Content retained | Lifecycle coupling | Erase propagation |
|---|---|---|---|---|---|
| S1 | Primary entity rows | `entities` | body_json (encrypted at rest when keyed), metadata | source of record | DELETE row (content + embedding + emb_sig columns) |
| S2 | Live FTS index | `entities_fts` (fts5, content_rowid) | body text of live rows | derived from S1 | DELETE matching rowids |
| S3 | Bi-temporal history | `entity_history` | full snapshots of every prior version | derived from S1; the audit substrate for `as_of`/`valid_at` | DELETE rows + FTS rowids (S4) |
| S4 | History FTS index | `entity_history_fts` (fts5) | body text of superseded versions | derived from S3 | DELETE matching rowids |
| S5 | Embedding vectors | `entities.embedding`, `entities.emb_sig` | float32 vector + sign-bit signature | column on S1; no separate table | removed with the row (S1) |
| S6 | Entity links (outbound) | `entities.links` (JSON array) | link edges (target_id, relationship, weight) | column on S1 | removed with the row; **inbound edges in other rows are swept** (§5.4) |
| S7 | Graph communities | `communities` | member_ids JSON, member_digest, summary, summary_entity_id | derived from S1 | member removed from `member_ids`; empty community deleted; `member_digest` invalidated (forces recompute); `summary_entity_id` cleared if it pointed at the erased entity |
| S8 | Dedup signatures | `dedup_signatures`, `dedup_signature_blobs` | trigram set + freshness guard | derived from S1; `ON DELETE CASCADE` | cascade (verified: pool opens `foreign_keys=ON`) |
| S9 | Evidence artifacts | `artifacts`, `artifact_bindings` | hash-addressed immutable evidence records | operator-registered, not derived from S1 | **preserved by design** (immutable evidence; see §6.4) |
| S10 | Audit journal | `journal` | append-only event log with cryptographic chain | audit substrate | content redacted to hash-only (§6.2); chain tuple preserved |
| S11 | Rejected-value tombstones | `rejected_value_tombstones` (primary DB) | value sha256 + scope + expiry | write/read gate | NOT deleted — **added** on erase (re-ingest guard) |
| S12 | Governance overlay | `{db}.governance.db` (sidecar) | permanent erasure mandates keyed by value digest | write/read gate; survives primary-DB rollback | NOT deleted — **added** on erase (§5.5) |
| S13 | Derived belief/observation entities | `entities` rows in categories `beliefs`, `observation`, `synthesis`, `memories` | consolidated/derived knowledge | derived from S1 via `derived_from`/evidence refs | affected derived rows are **quarantined**, never silently orphaned (§5.6) |
| S14 | Claim cards / evidence capsules | none (computed projection) | deterministic digest-bound projection | view over S1/S13 | recomputed on read; nothing to delete |
| S15 | Op/run state | `state` | bounded status/progress of long-running ops | ephemeral | untouched (no content) |
| S16 | Keystones | `keystones` | mandatory policy rules | policy, not memory | untouched |
| S17 | Exports / backups | files outside the DB (`vault_export`, operator backups) | materialized copies | outside process boundary | **cannot be retroactively erased**; documented obligation §6.5 |
| S18 | Encryption canary | `encryption_canary` | marker for fail-fast key check | operational | untouched |

**Processor boundaries:**

- **In-process plaintext:** bodies are decrypted only inside the `Database`
  layer for recall/redaction/erase operations; the journal is hashed, not
  content-stored, for tamper evidence.
- **Local-first:** with bundled embeddings (default), dense/hybrid recall,
  graph, temporal, and maintenance paths make **zero network calls** (§8).
- **Provider paths:** `perseus_vault_ask`/`perseus_vault_embed` with an external endpoint and
  connector sync are explicit opt-ins; each is gated by the deployment profile
  (#870) and the AAR control plane where it mutates state.
- **Workspace isolation:** every store above carries `workspace_hash` where
  content-bearing (S1, S3, S7, S9, S10, S11, S12); recall, graph, maintenance,
  projection, and export paths are workspace-scoped (enforced since #854/#855).

## 3. Lifecycle states (one vocabulary)

`status` on `entities` is the lifecycle axis, orthogonal to the epistemic
trust axis (`candidate/verified/corroborated/rejected/defensively_recalled`,
#880). Legal lifecycle values and their semantics:

| State | Meaning | Reachable via | Content still served? |
|---|---|---|---|
| `active` | live, recallable | write | yes |
| `proposed` | admission proposal awaiting evidence/authority | admission gate (#863) | explicit opt-in only |
| `superseded` | replaced by a newer version; history preserved | `perseus_vault_supersede` / valid-time close | historical/audit modes only |
| `resolved` | question/issue closed; record kept | `perseus_vault_resolve`-equivalent (status write) | yes, ranked lower |
| `quarantined` | suspected harmful/untrusted; withheld | admission gate, erase revocation (§5.6) | never (unless explicit audit mode) |
| `expired` | passed its `expires_at_unix_ms`; retained for history | `perseus_vault_expire` sweep (§5.1) | no (explicit historical mode only) |
| `redacted` | content scrubbed to hash-only record; row metadata kept, hidden from recall (archived) | `perseus_vault_redact` (§5.2) | metadata only, no body |
| `logically_deleted` | user/policy delete; row kept WITH content intact but hidden (recoverable) | `perseus_vault_forget` (existing) | no |
| `physically_erased` | content + row + derived layers removed; hash-only evidence remains | `perseus_vault_erase` (§5.3) | no — not even metadata |

**Distinctness rule (#868):** these operations are **not interchangeable**.
- `expire` = time-based lifecycle transition, content retained for history.
- `supersede` = correctness replacement, content retained in history.
- `forget` = user-initiated logical delete; row + FTS removed from service;
  history retained.
- `redact` = content removal with metadata retention; re-ingest of the same
  value is **allowed**.
- `erase` = physical removal across all derived layers + permanent re-ingest
  suppression; re-ingest is **denied** by the governance gate.
- Read-time diversity/ranking controls are **not** lifecycle cleanup and do
  not substitute for it (a low-ranked entity is still stored and still
  propagates to derived layers).

## 4. Consent / no-persist controls

- `capture --consume` / `--prune-source` (#562/#563) already prevent source
  persistence for ingested streams.
- **No-persist boundary test** (acceptance #866): an entity written with
  `expires_at_unix_ms` in the near future is served until expiry, then
  transitions to `expired` on the next sweep and is excluded from recall
  without a delete. The sweep is exercised in `tests` before any ingestion
  path relies on it.
- Providers are invoked only when the deployment profile permits (§8);
  provider-side retention is a documented opt-in, never the default.

## 5. Operation semantics

### 5.1 Expiry — `perseus_vault_expire`

- **Setting expiry:** the write path derives `entities.expires_at_unix_ms`
  from the body `expires_at` field on every remember/update — an integer
  (unix ms), a numeric string, or an ISO 8601 UTC timestamp
  (`2026-08-09T12:00:00Z`; offsets accepted). A body without `expires_at`
  clears a prior expiry (the body is the source of truth). Admission bounds
  (`max_expiry`) still constrain the value on the governed write path.
- Sweep: `UPDATE entities SET status='expired', archive_reason='expired' WHERE
  expires_at_unix_ms <= now AND status='active' [AND workspace_hash=?]`.
- History rows are **not** deleted; FTS entries are retained so historical
  recall (`include_archived`-style flags, `as_of`) can still find them.
- `dry_run` reports the affected count with identical predicates.
- The sweep is idempotent and re-runnable; a `state` entry records the last
  sweep timestamp for observability.

### 5.2 Redaction — `perseus_vault_redact`

- Content digest `d = sha256(body_json)` is computed **before** any write.
- Transaction:
  1. `entities.body_json` ← `{"redacted": true, "redacted_at_unix_ms": <now>, "value_sha256": d}`; `status='redacted'`, `archive_reason='redacted'`.
  2. `DELETE FROM entities_fts` for the row.
  3. `DELETE FROM entity_history` + `entity_history_fts` for the entity's history (all prior versions — content-bearing).
  4. Journal: append `event_type='redacted'` carrying `{entity_id, value_sha256}` (hash-only evidence).
- Metadata (id, category, key, links, community membership, provenance fields)
  is **retained** — redaction removes content, not existence.
- Re-ingest of the same value is allowed (no tombstone, no mandate).

### 5.3 Physical erasure — `perseus_vault_erase`

- Scope: exact `(category, key, workspace_hash)` — never a bare `(category,
  key)` across workspaces (#854 fail-closed rule). `dry_run` reports the exact
  rows and derived-layer counts the real run will touch.
- Transaction per target row (all in one transaction per workspace scope):
  1. `d = sha256(body_json)` (content digest) recorded for evidence.
  2. **Governance first:** `reject_value(workspace, subject, key, value, reason='erased', expires_at=NULL)` **and** a permanent mandate in the governance overlay (`mark_value_erased`). The write gate therefore blocks re-ingest even if the primary DB is rolled back (§5.5).
  3. `DELETE FROM entities_fts` rowids; `DELETE FROM entities` row.
  4. `DELETE FROM entity_history` + `entity_history_fts` for `(id, cat, key, ws)` (S3/S4).
  5. **Communities (S7):** remove the id from every `member_ids` JSON array in the same workspace; delete communities left with zero members; clear `summary_entity_id` when it points at the erased row; invalidate `member_digest` (forces recompute of summaries on the next maintenance pass — summaries are derived, regenerable artifacts).
  6. **Inbound links (S6):** sweep `entities.links` of all remaining rows for `target_id == erased id` in the same workspace and remove those edges (bounded, same transaction).
  7. **Derived beliefs (S13):** any entity in a derived category whose evidence/`derived_from` refs include the erased id → `status='quarantined'`, `archive_reason='source_erased:<digest>'`. Derived content must not keep serving as if its source still existed (#876 revocation semantics, forward-compatible).
  8. **Journal (S10):** redact matching journal rows (existing #417 pattern: content → `{}`, preserving the audit-chain hashed tuple), then append `event_type='erased'` with hash-only evidence `{entity_id_sha256, value_sha256, workspace_hash, agent_id}`.
  9. Dedup side tables (S8) cascade via FK.
- Report: `EraseReport { entities_erased, history_deleted, fts_cleaned, community_memberships_cleaned, inbound_links_cleaned, derived_quarantined, journal_redacted, value_sha256, dry_run, completed_at_unix_ms }`.
- `VACUUM` is **not** run inside erase (caller may run `perseus_vault_purge`-style
  maintenance for space reclamation); erase is about reachability, purge is
  about space.

### 5.4 Inbound-link sweep

Links are stored as JSON arrays on the source row. Erasing a target leaves
dangling edges in other rows. The sweep removes edges pointing at the erased
id **within the same workspace**, preserving unrelated edges. Cross-workspace
edges cannot exist under the scoping rules (#391/#684); any found are logged
as an anomaly in the report (`cross_workspace_edges_found`).

### 5.5 Re-ingest guard and rollback resistance

- The pre-write gate (`is_value_rejected` + `is_value_erased`) is consulted by
  `remember`/`ingest`/`capture` **before** the write transaction begins
  (#881). Erase installs both a permanent primary tombstone and a governance
  overlay mandate, so:
  - re-ingest of the erased value fails closed with a deterministic
    `SuppressionError`-style rejection (never a silent write), and
  - restoring/rolling back the primary DB cannot resurrect serveability —
    the sidecar mandate still suppresses (regression test ships with this
    contract).
- **Governed exception:** an operator may lift a mandate explicitly via the
  governance surface (same authority class as rejection), which is journaled;
  there is no silent path.

### 5.6 Derived-store revocation

`erase` quarantines derived entities citing the erased source (§5.3 step 7).
Quarantine (not deletion) is chosen because derived content may aggregate
multiple sources; an operator reviews quarantined derived entities via
`perseus_vault_operator_review` and decides keep/refine/delete. `ask`/recall never
serve quarantined content except in explicit audit mode.

## 6. Audit evidence after erasure (minimized, explicit)

1. **Never retained:** body content, embeddings, history bodies, FTS text,
   community summaries referencing the content (recomputed without it).
2. **Retained (hash-only):**
   - `value_sha256` = sha256 of the erased body (in journal `erased`/`redacted`
     events and the governance mandate). Hashes are non-reversible digests;
     they exist to make re-ingest suppression deterministic and to let an
     auditor confirm *which* value was erased without recovering it.
   - Journal chain tuples (id, created_at, workspace_hash, audit_hash) are
     preserved so the tamper-evidence chain keeps verifying (#433 M2).
   - Governance overlay mandates (digest + scope + reason) — permanent.
3. **Documented retention:** evidence artifacts (S9) are immutable by design;
   exports/backups (S17) outside the DB cannot be retroactively erased —
   operators with erasure obligations must include backups in their retention
   policy. This is a documented limitation, not a silent gap.
4. **Minimization rule:** no retained field may contain the erased value or a
   reversible encoding of it. `entity_id` in journal events is replaced by its
   sha256 in `erased` events (the `redacted` event type keeps the id because
   redaction preserves metadata by definition).

## 7. Cross-scope isolation (recall, graph, maintenance, projection, export)

- All five surfaces filter by `workspace_hash`; the erase/redact/expire ops
  accept an explicit workspace and refuse to guess (`''` only on explicit
  request for legacy global rows).
- `consolidate`/`dream` are workspace-bound since #854; `prune`/`purge`/
  `forget`/`expire`/`redact`/`erase` follow the same rule (workspace
  parameter, fail-closed when ambiguous).
- Export honors the same scope; `vault_export` emits a manifest listing the
  included workspace so a partial export cannot be mistaken for global.

## 8. Deployment profile distinction (acceptance #866)

| Claim | Meaning | Where enforced |
|---|---|---|
| Local-first | default profile: bundled embeddings, no network for recall/graph/temporal/maintenance | profile resolution + offline test (#870) |
| Offline | no network calls of any kind; provider paths disabled | `offline` profile test suite |
| Encrypted-at-rest | AES-256-GCM on entity bodies, history, journal payloads when keyed | encryption module + canary |
| Provider retention | external LLM/embedding/connector provider sees only what is explicitly sent | per-call opt-in, AAR-gated |
| End-to-end encryption | **not offered** (no client-held-key channel) — documented as out of scope | this doc, §8 |

## 9. Test map (acceptance traceability)

| Acceptance (#866/#868) | Test |
|---|---|
| Distinct ops | `expire_vs_forget_vs_redact_vs_erase_are_distinct` |
| Expiry sweep semantics | `expire_due_transitions_status_and_excludes_from_recall` |
| Redaction removes content everywhere | `redact_scrubs_body_history_fts_and_journal_is_hash_only` |
| Erase propagation: vectors/FTS/history | `erase_propagates_to_fts_history_and_removes_row` |
| Erase propagation: graph | `erase_cleans_community_membership_and_invalidates_digest` |
| Erase propagation: links | `erase_sweeps_inbound_links_within_workspace` |
| Erase propagation: derived | `erase_quarantines_derived_beliefs_citing_source` |
| Re-ingest guard | `erase_blocks_reingest_via_gate` + `erase_survives_primary_rollback` |
| Journal evidence | `erase_journal_is_hash_only_and_chain_verifies` |
| Workspace isolation | `erase_is_workspace_scoped_and_fail_closed_on_ambiguity` |
| Dry-run parity | `erase_dry_run_counts_match_real_run` |

## 10. Changelog

- **v1 (2026-08-09):** initial contract; ships with `perseus_vault_expire`,
  `perseus_vault_redact`, `perseus_vault_erase` and the propagation matrix above.
