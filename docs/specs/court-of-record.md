# Court-of-record governance: read-only self-audit + idempotent rulings

Status: design specification | draft
Date: 2026-08-11
Resolves: #940 · Consumed by: perseus-vault#940
Related: `docs/specs/keystone-suggestions.md` (#683/#889), `docs/operator-review.md`, `src/validity.rs`

## 1. Overview

Keystones (#683) and keystone suggestions (#889) exist, but there is no loop
where a consistency finding compiles into an enforced guard, and no read-only
audit that recommends a winner without mutating. This spec adds the
court-of-record contract: a read-only `perseus_vault_consistency_audit` that
scores each contradiction pair with a deterministic recommendation ladder,
and an idempotent `perseus_vault_audit_ruling` (accept/override/reverse) that
compiles an accepted ruling into the existing supersede guard (link +
valid-period close + status flip, `handle_supersede`) and records the ruling
durably. Model confidence is evidence, never permission: the audit only
recommends; only an operator ruling enforces.

## 2. Data schema — `court_rulings` (schema v38)

```sql
CREATE TABLE IF NOT EXISTS court_rulings (
    id TEXT PRIMARY KEY,               -- rul-<uuid>
    pair_fingerprint TEXT NOT NULL,    -- sha256("winner_id|loser_id" sorted pair)
    winner_id TEXT NOT NULL,
    loser_id TEXT NOT NULL,
    ruling TEXT NOT NULL,              -- accept | override
    rationale TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',  -- active | reversed
    decided_by TEXT NOT NULL DEFAULT '',
    supersede_receipt TEXT NOT NULL DEFAULT '',  -- guard evidence (link/close)
    created_at_unix_ms INTEGER NOT NULL,
    reversed_at_unix_ms INTEGER,
    reversed_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_court_rulings_pair ON court_rulings(pair_fingerprint);
CREATE INDEX idx_court_rulings_status ON court_rulings(status, created_at_unix_ms);
```

Stored fields are ids/digests/statuses only; no bodies are copied into the
ruling. Every ruling is also journaled (`court_ruling_set` /
`court_ruling_reversed`).

## 3. Recommendation ladder (deterministic)

For each contradiction pair, the audit recommends a winner:

1. `importance` (higher wins);
2. source authority rank (higher wins): `curated` 4 > `capture` 3 >
   `agent` 2 > `web_gap_fill` 1 > default 1;
3. recency (`created_at_unix_ms` higher wins);
4. id (lexicographic, deterministic tiebreak).

Rationale text states which rung decided. An active ruling for the pair
short-circuits the ladder: the finding reports `already_ruled` with the
ruling id and winner, and does NOT re-recommend.

## 4. Operational rules

1. `consistency_audit` is strictly read-only (`read_only: true`); it never
   mutates, links, closes periods, or archives.
2. Ruling `accept` applies the recommended winner; `override` applies an
   explicit winner (category/key) chosen by the operator. Both compile the
   guard through the existing supersede path (link winner→loser with
   `relationship="supersedes"`, close the loser's valid period, flip its
   status) and record the ruling with the supersede receipt.
3. Idempotency: an `accept`/`override` whose pair fingerprint + winner +
   ruling match an ACTIVE ruling returns that ruling unchanged (no-op).
   A different winner while active is refused: `reverse` first.
4. `reverse` flips the ruling to `reversed` (journaled). The supersede guard
   already compiled is NOT undone — re-assertion re-opens litigation.
   Reversed pairs are re-auditable and re-rulable.
5. Rulings are never auto-created; every ruling records `decided_by`.
6. The audit surfaces pending keystone suggestions (count + note) since
   approved rulings and keystone approvals share the guard-compilation
   spirit; decision remains via `keystone_suggestion_decide`.
7. Out of slice (documented, not implemented): link-graph dangling-target
   scan (hygiene surface owns it), ruling-driven archival of losers, and
   auto-reversal on superseding re-assert.

## 5. API surface

`perseus_vault_consistency_audit` (read-only):

```json
{"category": "facts", "limit": 50}
```

Response: `{audited_at_unix_ms, findings: [{pair_fingerprint, entity_a,
entity_b, similarity, recommendation: {winner_id, winner_key, reason,
ladder}} | {already_ruled: {ruling_id, winner_id}}], supersession_lag: [...],
keystone_pending, read_only: true}`.

`perseus_vault_audit_ruling` (write, journaled):

```json
{"action": "accept", "category": "facts", "entity_a_key": "k1",
 "entity_b_key": "k2", "rationale": "...", "agent_id": "operator",
 "winner_category": "...", "winner_key": "..."}   // override only
```

Actions: `accept` (winner = recommendation), `override` (explicit winner),
`reverse` (ruling id). Response: ruling record + supersede receipt.

## 6. Implementation slice

1. `SCHEMA_VERSION` 37→38; `court_rulings` DDL + indexes in the v38 block.
2. `db.rs`: `court_ruling_find_active`, `court_ruling_record`,
   `court_ruling_reverse`, `court_ruling_list` + tests (idempotent no-op,
   different-winner refusal, reverse reopens, journaling, receipt).
3. `src/court_audit.rs` (pure): pair fingerprint, recommendation ladder,
   authority rank + unit tests (importance, authority, recency, tiebreak,
   already-ruled short-circuit).
4. `tools.rs`: `handle_consistency_audit`, `handle_audit_ruling` + tests
   (audit recommends; accept compiles guard — loser hidden from recall via
   supersede; idempotent re-accept; override with explicit winner; reverse
   reopens; read-only audit mutates nothing).
5. `mcp.rs`: registry + dispatch (122→124); registry sync
   (README/CLAIMS-AUDIT/glama/manifest/server).
6. Docs: this spec + `docs/court-of-record.md` (ops) + CHANGELOG.
7. Gates: full suite green; benchmark quality gate `release_ready` on the
   branch; registry metadata check + integration conformance green.

## 7. Acceptance criteria

- [ ] Audit recommends deterministically and never mutates (verified by row
      counts before/after).
- [ ] Accept compiles the supersede guard; the loser no longer surfaces in
      recall; the ruling carries the receipt.
- [ ] Idempotent re-ruling returns the existing ruling; a conflicting active
      ruling is refused until reversed.
- [ ] Registry counts synced; all gates green.
