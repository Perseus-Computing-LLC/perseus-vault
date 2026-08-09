# Retrieval Telemetry & Trust-Concentration Reporting (#872)

Status: implemented (branch `feat/vault-872-retrieval-telemetry`)

## Problem

Recall is a black box: an operator cannot tell whether the store is
concentrating on a few over-served entities, repeatedly serving the same
stale evidence, leaking superseded/quarantined variants through an arm, or
letting a high-similarity low-trust lookalike dominate ranking. Each of
these is a silent retrieval-quality failure. The telemetry module makes
them observable and testable.

## Contract

New MCP tool: `perseus_vault_retrieval_telemetry`.

```
arguments: { since_hours?: int (default 24), workspace_hash?: string }
```

Returns one JSON report with the following top-level shape:

| section | contents |
|---|---|
| `state` | `ok` / `degraded` / `empty` — fail-closed: an empty observation window reports `empty`, never a misleading zero concentration |
| `concentration` | served-event count, distinct entities, HHI, top-1/top-3/top-5 shares, top entity id; `status: empty|ok` |
| `repeated_serving` | repeat_rate (1 − unique/served), distinct ids |
| `low_trust` | count of served events from low-trust entities (unverified ∧ certainty < 0.5) and their `fanout` across distinct query classes (query_class = top-2 non-stopword tokens) |
| `diversity` | diversity_halving invocations, Simpson index + entropy of the served distribution |
| `displacement` | count + sample of `displacement_events` (quota removals), each with `was_sole_evidence` (the entity's dominant keyword had zero representation in the delivered set) |
| `contamination` | served_reentry count (deprecated/expired/quarantined/redacted entities that were ever served), per-arm audit rows (mode/arm/candidates/returned/checked), delivered-id validation, and the `probe` result |
| `probe` | `invariant: bool` — fails closed (`false` + `blocked_reentry >= 1`) whenever the lexical/dense/graph probes observe non-serveable entities that a recall *could* have reached. **Opt-in** via `probe_query` + `probe_mode` arguments (defaults lexical) so ordinary reports stay cheap; `null` when not requested |
| `retrieval_profile` | recalls per mode + per workspace |
| `artifact` | schema_version, binary version, git hash, content hash (sha256 of the report as serialized, excluding the hash itself) |

### Schema v31

Three append-only telemetry tables (all timestamped, bounded by the
`TELEMETRY_MAX_ROWS` pruner — 10k rows per table):

- `served_events(id, ts_unix_ms, entity_id, mode, workspace_hash, query, query_class, verified, certainty)`
- `recall_arm_audits(id, ts_unix_ms, mode, arm, candidates, returned, checked, workspace_hash)`
- `displacement_events(id, ts_unix_ms, entity_id, reason, was_sole_evidence, mode, workspace_hash, query)`

Recording is cheap (indexed INSERTs) and never changes recall results.
`skip_side_effects` recalls record nothing.

## Trust-concentration acceptance

With `trust_weight > 0`, keyword ranking sorts by
`decay_score + trust_weight·trust` where `trust = 1.0` for verified
entities and `trust = certainty` otherwise. A verbatim-match low-trust
entity cannot dominate a verified entity: the fixture seeds a low-trust
entity whose body is exactly the query and asserts the verified entity
ranks first, and the report's top entity is the verified one.

## Cross-arm contamination acceptance

All recall arms now exclude non-serveable statuses
(`deprecated, expired, quarantined, redacted`) at the SQL boundary
(`SERVEABLE_STATUS_SQL` in every SELECT) **and** with a Rust guard in both
hydrators (`NON_SERVEABLE_STATUSES`), so a stale arm can never deliver a
retired entity. The contamination probe independently queries raw arms and
counts non-serveable rows a recall could have reached (`blocked_reentry`);
`invariant` is `false` whenever that count is non-zero — i.e. the telemetry
fails closed on the very condition it exists to expose. Served-event
re-entry (`served_reentry`) must stay 0.

## Diversity displacement acceptance

The diversity quota (halving < 1) can remove entities whose keyword loses
its last representative. Every removal is recorded in `displacement_events`
with `was_sole_evidence`; the report's `displacement.count` is the
operator-visible signal that diversity controls displaced evidence.

## Verification

- `cargo test --bin perseus-vault retrieval_telemetry` — 8 module tests
  (recording, pruning, report shape, empty/degraded/ok states, probe
  fail-closed, fanout/query-class, scoped reports).
- `cargo test --bin perseus-vault telemetry_` — 6 acceptance fixtures
  (low-trust domination, repeated serving + fanout, cross-arm
  contamination, arm audits per mode, displacement, state separation).
- Full suite: 665 passed, 1 metadata-sync assertion (100 canonical tools).
- `python3 benchmark/telemetry/run.py` — end-to-end MCP-stdio harness over
  a real binary: seeds the acceptance fixtures, exercises fts5/hybrid/fused
  recall, asserts every acceptance invariant, prints a small replayable
  report (counts + ids + content hash; prompts and payloads not retained).
