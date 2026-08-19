# Portable scoped-memory capability contract

This fixture implements issue #1103 as a provider-free, synthetic contract over the
existing Vault memory operations. It is a **capability boundary**, not a second
memory API.

## Contract version

`perseus-vault-scoped-memory-contract/v1`

The fixture binds four trusted scope dimensions:

- `user_id`
- `workspace_hash`
- `agent_id`
- `session_id`

The host supplies this scope out of band. Model-authored arguments cannot supply
or override any of those fields. Scope and policy filtering happens before a
ranker receives candidate IDs.

## Covered operations and outcomes

The same synthetic records exercise bounded:

- search/recall
- context projection
- inspect
- authorized store
- correction with explicit successor lineage
- supersession with observable `active`/`superseded` states

Expected outcomes are explicit and stable: `allow`, `deny`, `scope_mismatch`,
`stale_conflict`, `abstain`, and `unavailable`. A missing semantic provider or
surface is represented as `unavailable`; it is never converted to a fabricated
zero or pass.

Writes require a trusted authority whose capability set includes the operation.
Stale expected versions fail closed. Corrections and supersessions retain the
prior record and expose a deterministic successor relationship.

## Surfaces

The runner supports both:

1. `InProcessSurface`, a deterministic reference surface used by unit tests.
2. `McpSurface`, an adapter over the existing `VaultClient` and canonical MCP
   tools (`recall`, `context`, `get_entity`, `remember`, `correct`, and
   `supersede`). Admission-bearing writes reuse `benchmark/admission_fixture.py`.

The MCP run uses a checkout-built binary and a fresh temporary database. It does
not use a global binary or the user's database.

## Hash-only publication

`ContractRun.projection` is a deterministic, content-addressed projection. It
contains outcome codes, assertion booleans, counters, and SHA-256 commitments;
it does not contain prompts, queries, memory bodies, `body_json`, credentials,
or host paths. `publish_run()` routes the result through the existing common
publication validator. Failed/unavailable metric paths omit numerator, denominator,
and rate fields rather than reporting zeroes.

## Commands

From the repository root:

```bash
# Pure deterministic surface and publication tests
python3 -m unittest discover -s benchmark/scoped_memory -p 'test_*.py' -v

# Hash-only report from the in-process surface
python3 -m benchmark.scoped_memory.run \
  --surface inprocess \
  --out /tmp/scoped-memory-contract-report.json

# Real MCP replay (checkout-built binary only)
python3 -m benchmark.scoped_memory.run \
  --surface mcp \
  --bin /path/to/target/debug/perseus-vault \
  --out /tmp/scoped-memory-mcp-report.json
```

The committed `fixture.json` is synthetic and digest-bound. It is safe contract
input, not a production memory export.
