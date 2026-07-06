# Design / RFC: Keyed-MAC audit chain + redaction-safe payload commitment

Status: **Draft for review** (2026-07-05 security review, follow-up to the v14
SHA-256 chain). This document is the artifact for external review *before* the
implementation is merged. A first-pass implementation accompanies it (opened, not
merged) so reviewers have running code + spec.

## 1. Why

The journal "audit chain" today (v14, shipped in 2.17.3) is a **SHA-256 hash chain**
over each entry's `(prev_hash, id, created_at_unix_ms, workspace_hash)`. That fixed
the pre-v14 64-bit `DefaultHasher`, but two properties remain unmet for a defensible
"audit trail" / CMMC posture:

1. **Unkeyed → forgeable by a recomputing attacker.** Anyone who can write the DB (or
   run the binary's own logic) can recompute a fully-valid chain over doctored rows.
   An unkeyed hash detects accidental corruption and naive edits, not motivated tampering.
2. **Payload not covered → content tampering invisible.** The chain deliberately excludes
   the event payload so `purge`/redaction can erase content (GDPR / right-to-erasure)
   while the chain stays verifiable. Correct trade-off, but it means the chain attests
   only to *existence / order / time / workspace*, not to *what the event said*.

This design closes both **without** breaking erasure, via two independent mechanisms:
a **keyed MAC** (closes #1) and a **redaction-safe payload commitment** (closes #2).

## 2. Threat model

- **In scope.** An attacker who can (a) read and/or write the SQLite file directly
  (stolen backup, container escape, another local process), or (b) call the engine's
  own APIs, and wants to **alter, reorder, delete, or forge** journal history
  undetected — including editing an event's body/type/identity.
- **Trust boundary (unchanged, per `THREAT-MODEL.md`).** The MCP caller is trusted;
  workspace scoping is routing, not isolation. This design does not change those.
- **Out of scope / inherent limits.**
  - An attacker holding **both** the DB *and* the audit MAC key can forge the chain —
    that is unavoidable for any keyed scheme; the mitigation is that the key is **never
    stored in the DB** and (in the recommended mode) is the operator's encryption key.
  - A **redacted** entry's original content is, by design, unrecoverable; the chain
    proves a specific content-commitment existed and has not been re-ordered/altered,
    but the erased bytes cannot be re-verified against it (that is the point of erasure).
  - **Unencrypted** deployments have no secret to key with → they fall back to the
    unkeyed SHA-256 chain (documented; tamper-evidence requires encryption enabled).

## 3. Design

### 3.1 Redaction-safe payload commitment (content integrity)

Add a per-entry column `payload_commitment TEXT`:

```
commitment = SHA256( LP(event_type) ‖ LP(evaluated_json) ‖ LP(acted_json)
                     ‖ LP(forward_json) ‖ LP(category) ‖ LP(key)
                     ‖ LP(entity_id) ‖ LP(agent_id) )
```

where `LP(x) = u64_le(len(x)) ‖ x` (length-prefix framing).

- Written at journal time from the payload.
- The chain link (§3.2) covers the **commitment**, not the raw payload.
- On **verify**, for each entry:
  - if the payload is still present → recompute the commitment and compare (detects
    content tampering);
  - if the payload was **redacted** (scrubbed by `purge`) → skip the recompute; the
    stored commitment is still covered by the chain, so structural integrity holds.
- On **redaction** (`purge`): scrub the payload columns as today, but **keep**
  `payload_commitment` (and `id`, `created_at`, `workspace_hash`, `audit_hash`). Verify
  still passes. This is the key property that lets content-integrity and erasure coexist.

### 3.2 Keyed MAC (tamper-evidence)

The chain link becomes:

```
link_input = LP(prev_hash) ‖ LP(id) ‖ u64_le(created_at_ms)
             ‖ LP(workspace_hash) ‖ LP(payload_commitment)

audit_hash = keyed ? HMAC_SHA256(audit_key, link_input)   // hex
                   : SHA256(link_input)                    // hex, unkeyed fallback
```

HMAC-SHA256 is implemented in-tree (RFC 2104) and unit-tested against **RFC 4231**
vectors — this is the one primitive we cannot get wrong, and the vector test makes CI
prove correctness even though the crate can't be compiled on the audit box.

### 3.3 Key derivation and the ordering problem

`Database::open()` runs schema migrations **before** `set_encryption(key)` is called,
so the MAC key is **not available at migration time**. Resolution splits the work:

- **Migration (v14→v15), at open, no key:** add the `payload_commitment` column,
  backfill commitments from existing payloads (pure SHA-256, no key), and recompute the
  chain **unkeyed** over the new `link_input` (includes the commitment). Deterministic,
  idempotent, no-op on a fresh DB.
- **`set_encryption(key)`, key now available:** derive
  `audit_key = SHA256("perseus-vault/audit-chain/v1\0" ‖ raw_key)` and, if the chain is
  not already keyed under *this* key, **rekey** it to HMAC (same shape as the existing
  `rekey_aad` / encryption-canary flow). This is where keying actually happens.

`EncryptionManager` gains a private `audit_key() -> [u8;32]` derived once at construction
(the raw key already flows through `from_key_file`); the raw key is never exposed further.

### 3.4 Scheme marker + audit-key canary

A meta row records the chain's current scheme so `verify` and startup know how to treat it:

- `audit_chain_scheme = "sha256-v1" | "hmac-sha256-v1"`.
- `audit_key_canary = HMAC_SHA256(audit_key, "perseus-vault/audit-canary/v1")` (only in
  keyed mode). On `set_encryption`, if the stored canary equals the canary under the
  current key, the chain is already correctly keyed (skip rekey); if it differs, the key
  changed → rekey; if absent and encryption is on → first-time key, rekey. Mirrors the
  encryption canary (2.17.0) so a wrong key cannot silently "bless" itself.

### 3.5 verify semantics

`verify-audit-chain` (and the library `verify_audit_chain`):

1. Read `audit_chain_scheme`.
2. If `hmac-sha256-v1` but no encryption key is loaded → **error**: "chain is keyed;
   provide --encryption-key to verify." (Fail closed, not a false pass.)
3. Recompute each link under the recorded scheme and compare to the stored `audit_hash`
   (detects reorder / move / delete / forge — keyed mode requires the secret).
4. For entries whose payload is present, recompute `payload_commitment` and compare
   (detects content tampering). Redacted entries skip step 4.

### 3.6 Interaction with redaction / purge

`purge` redaction keeps `payload_commitment` alongside the fields it already preserves
(`id`, `created_at`, `workspace_hash`, `audit_hash`) and scrubs the payload columns. The
existing purge-preserves-chain tests must continue to pass; a new test asserts a redacted
entry still verifies and that its commitment is unchanged.

## 4. Migration & rollout safety

- Schema `user_version` 14 → 15. Forward-only (like v11→v12, v13→v14). Idempotent.
- A DB written **keyed** then opened **without** the key: reads still work (the chain is
  data, not a gate); `verify-audit-chain` reports "provide the key" rather than failing
  open. Recall/remember are unaffected.
- Enabling encryption on a previously-unkeyed store rekeys the chain once (canary-gated).
- Rekey runs inside a transaction; a crash mid-rekey leaves the prior consistent chain
  (re-run on next `set_encryption`).
- Greg impact: both greg vaults run **unencrypted stdio**, so they stay on the unkeyed
  SHA-256 chain — no behavior change until/unless encryption is enabled there.

## 5. What this does and does not guarantee

- ✅ Detects reorder / deletion / forgery of journal entries **without the key** (keyed mode).
- ✅ Detects content tampering of **non-redacted** entries.
- ✅ Preserves erasure: redacted content stays erased; the chain still verifies.
- ✅ Fails closed when a keyed chain is verified without the key.
- ❌ Does not protect against an attacker holding **both** the DB and the MAC key.
- ❌ Unencrypted deployments get only the unkeyed chain (no secret to key with).
- ❌ Does not turn workspace scoping into an enforced boundary (unchanged, by design).

## 6. Test plan

- HMAC-SHA256 against **RFC 4231** test vectors (correctness of the primitive).
- Keyed chain: fresh chain verifies; reorder / workspace-move / content-edit each break it.
- Unkeyed→keyed transition on `set_encryption` (canary-gated, idempotent).
- Redaction: a redacted entry still verifies; its commitment is preserved; content-edit
  of a non-redacted entry breaks verify.
- Wrong key → `verify` fails closed; missing key on a keyed chain → clear error.
- v14→v15 migration backfills commitments and the chain verifies afterward.

## 7. Questions for reviewers

1. **Key choice.** Derive the audit key from the encryption key (proposed), or take a
   separate `--audit-key` so audit integrity is independent of at-rest encryption
   (e.g., encrypt off but audit-signing on)? The former is zero-config; the latter is
   more flexible for compliance separation-of-duties.
2. **External anchoring.** Is periodic publication of the head `audit_hash` to an
   append-only external sink (log service, notarization, transparency log) in scope for
   the "audit trail" claim, to defend against an attacker with the key rewriting history?
3. **Commitment salting.** `payload_commitment` is an unsalted SHA-256, so a redacted
   entry is still subject to a dictionary/confirmation attack on low-entropy content.
   Should the commitment be `HMAC(audit_key, payload)` in keyed mode to also hide content
   post-redaction? (Trades the ability to verify a commitment without the key.)
4. **Key rotation.** On encryption-key rotation, the chain is rekeyed — acceptable, or do
   we need to retain verifiability under the old key (dual-canary window)?
