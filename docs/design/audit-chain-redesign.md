# Design: Audit-chain redesign (keyed, payload-binding, verifiable)

Status: **DECIDED (2026-07-06) — implementation GATED on external cryptographic review**
Tracking: 2026-07-05 security audit deferred item.

## Decisions (2026-07-06)

- **Key management: Option A + floor.** Derive `K_audit` via HKDF from the
  existing AES-256 master key (`info="perseus-vault/audit/v1"`); when no
  encryption key is configured, fall back to the unkeyed SHA-256 chain (Option C)
  and have `verify_audit_chain` / health **report the active mode** so a "keyed
  audit" guarantee is never claimed for a keyless DB.
- **Sequencing: design-only now.** This PR lands as design. **No audit-chain code
  changes until external cryptographic review** of this design completes — then
  implement the v13→v14 migration. No CMMC / "audit trail" claim is made in the
  interim.

## Problem

The current journal audit chain (`src/db.rs::audit_hash`) has three defects that
make it unfit to back any CMMC / NIST "audit trail" claim:

1. **Non-cryptographic.** It uses `std::collections::hash_map::DefaultHasher`
   (SipHash-1-3, fixed seed) truncated to a **64-bit** hex string. Not
   collision-resistant; brute-forceable. The code comment already says so
   ("SHA-256 substitute … upgrade to a proper crypto crate") — and `sha2 = "0.10"`
   is *already* a dependency.
2. **Unkeyed.** The digest is a pure function of public inputs. An attacker with
   write access to the SQLite file can tamper with a row and **recompute every
   subsequent `audit_hash`**; `verify_audit_chain` then passes. The chain proves
   only internal consistency, not authenticity.
3. **Payload-blind.** The digest covers only `(prev_hash, id, created_at_ms,
   workspace_hash)`. It does **not** cover the entry's content (`category`, `key`,
   `entity_id`, `agent_id`, body). The *contents* of an audited event can be
   altered without breaking the chain.

## Goals

- Cryptographic integrity (collision/preimage resistance): real SHA-256.
- Authenticity / forgery-resistance against a DB-file attacker: a key held
  outside the DB.
- Content binding: tampering with what an entry records must break verification.
- Preserve the existing **redaction/purge** guarantee: a redacted body must not
  break chain verification (today's payload-blindness is the current, blunt way
  of achieving this).
- Backward-verifiable migration, matching the existing v11→v12 `rehash_audit_chain`
  precedent.

## Proposed formula

```
payload_commitment = HMAC-SHA256(K_audit, canonical(category, key, entity_id, agent_id, visibility, body_ref))
link_hash          = HMAC-SHA256(K_audit, prev_hash || id || created_at_ms || workspace_hash || payload_commitment)
```

- `canonical(...)` is a length-prefixed, field-tagged encoding (no delimiter
  ambiguity), mirroring the note already in `audit_hash`.
- `body_ref` binds content without retaining it after redaction — see below.
- Genesis: `prev_hash = "genesis"`.
- Store `payload_commitment` in a new journal column so it **survives redaction**:
  redaction scrubs the body but keeps `(id, created_at, workspace_hash,
  payload_commitment)`. The chain still verifies, and we retain tamper-evident
  proof of *what was there* without retaining the data itself. This is the
  standard commitment/tombstone reconciliation of audit-integrity vs.
  right-to-erasure.

## The crux: where does `K_audit` come from?

`K_audit` MUST NOT live in the DB it protects. Options:

| Option | Forgery-resistant vs DB attacker? | Works without encryption enabled? | Notes |
| :-- | :-- | :-- | :-- |
| **A. HKDF from the AES-256 master key** (`encryption.rs` key file), `info="perseus-vault/audit/v1"` | **Yes** | **No** — requires a key file | Reuses key the operator already protects; zero new key management. Recommended. |
| B. Dedicated audit key file (separate from encryption) | Yes | Yes | New key-management surface to document/rotate/back up. |
| C. Unkeyed SHA-256 only (no HMAC) | No (only tamper-*evident* to a holder of a trusted head) | Yes | Fixes defect #1 and #3, not #2. Useful as a floor when no key exists. |
| D. External notarization (periodically publish chain head to an append-only store) | Yes (even vs. insider) | Yes | Strongest, but operational; complementary to A/B. |

Recommended: **A as the primary**, automatically **falling back to C** when no
encryption key is configured (and surfacing which mode is active via
`verify_audit_chain` / health, so a "keyed audit" claim is never made for a
keyless DB). Optionally layer **D** later for insider-threat / regulator posture.

## Migration (v13 → v14)

- Add `payload_commitment` column (default `''`).
- `rehash_audit_chain` upgrades: for each row in canonical order, recompute
  `payload_commitment` from the *current* row content (non-redacted rows) and the
  new `link_hash`. **Rows already redacted before migration** have no recoverable
  body → their commitment is recorded as the HMAC of the surviving metadata only,
  and flagged (`payload_commitment_scope = 'metadata-only'`) so verification and
  auditors can distinguish "content-bound" from "pre-migration metadata-bound"
  links. Deterministic and idempotent, like the existing rehash.
- Under Option A, migration requires the key file present (documented; refuse to
  claim keyed mode otherwise).

## Rollout

1. This design → owner decisions below → **external cryptographic review** (per
   the 2026-07-05 deferral) before any "audit trail" marketing/compliance claim.
2. Implement behind the migration; keep `verify_audit_chain` the single source of
   truth and report the active mode (keyed-A vs floor-C).
3. Update `CLAIMS-AUDIT.md` / `SECURITY.md` to state exactly what the chain does
   and does not guarantee in each mode.

## Explicitly out of scope for the first cut

- Key rotation / re-keying of an existing chain (would need a re-HMAC pass;
  design separately).
- Multi-writer / distributed notarization (Option D) beyond a single-node head.
