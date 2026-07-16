# Chancery + Perseus Vault — Provable Authority + Provable Content

Chancery provides MCP identity (who called what, under whose authority).
Perseus Vault provides memory (what was read/written, crypto-chained).
Together: the only agent memory stack with end-to-end verifiable provenance.

## Prerequisites

- Chancery CLI installed (`brew install chanceryhq/tap/chancery` or download from releases)
- Perseus Vault binary on PATH (`perseus-vault`)
- Both tested on Chancery v0.1.0 + Perseus Vault latest

## 1. Create the reader writ

```sh
chancery writ grant --for user:admin@acme.com --to memory-reader \
  --cap "call:perseus/perseus_vault_get_entity" \
  --cap "call:perseus/perseus_vault_scan" \
  --cap "call:perseus/perseus_vault_context" \
  --cap "call:perseus/perseus_vault_recall" \
  --cap "call:perseus/perseus_vault_as_of" \
  --cap "call:perseus/perseus_vault_bitemporal" \
  --ttl 8h
```

## 2. Create the writer writ (separate, narrower scope)

```sh
chancery writ grant --for user:admin@acme.com --to memory-writer \
  --cap "call:perseus/perseus_vault_remember" \
  --cap "call:perseus/perseus_vault_forget" \
  --cap "call:perseus/perseus_vault_prune" \
  --ttl 4h
```

## 3. Wrap Perseus Vault with Chancery enforcement

```sh
# Reader agent
chancery mcp wrap --agent memory-reader --writ <reader-writ-id> \
  --server-name perseus -- perseus-vault serve

# Writer agent
chancery mcp wrap --agent memory-writer --writ <writer-writ-id> \
  --server-name perseus -- perseus-vault serve
```

## 4. Audit chain cross-reference (future)

Planned integration pattern:
- Chancery stores `perseus_audit_hash` in its authority event
- Perseus stores `chancery_writ_id` in its audit entry
- Neither depends on the other — independent chains, cross-referenced
- Walk the full chain in either direction: Authority → Action → Content → Verification

Status: spec drafted, implementation pending Chancery API stability.

## 5. CLI ergonomics notes (for anee769)

Feedback from wiring this up:
- `--server-name` + `--` separator pattern is clean — unambiguous where Chancery args end and server args begin
- Would be useful to have `chancery writ list` show TTL remaining in human-readable format alongside the raw seconds
- `chancery mcp wrap` could benefit from a `--dry-run` flag that prints what tools would be filtered without actually starting the server
- The `verb:resource` pattern is intuitive once you've seen one example, but the first encounter is a learning curve — a `chancery writ example` command that prints common patterns would help
