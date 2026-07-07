# Statement of Work — Cryptographic Review: Perseus Vault audit-chain (keyed-MAC + payload commitment)

**Requestor:** Perseus Computing LLC — perseus@perseus.observer
**Project:** Perseus Vault (open-source, MIT) — MCP-native local-first memory engine for AI agents
**Repo:** https://github.com/Perseus-Computing-LLC/perseus-vault (Rust)
**Date:** 2026-07-05  **Prepared for:** OSTIF intake / crypto-audit firm quote

## 1. Objective
Independently review the design and implementation of Perseus Vault's **journal audit
chain** — a keyed-MAC, content-committing tamper-evidence mechanism — and validate that
it delivers the integrity properties we claim, before we position the "audit trail" for
government / CMMC contexts. We need a written report with severity-ranked findings and
concrete recommendations, including answers to four specific open design questions.

## 2. Background
The audit chain is a per-journal-entry hash chain intended to make the append-only
decision log tamper-evident while remaining compatible with GDPR-style erasure (`purge`).
Two mechanisms are under review (both recently landed on `main`, unreleased):
- **Payload commitment** — a per-entry SHA-256 over the event payload, covered by the
  chain, so content tampering of a live entry is detectable while the payload can still
  be redacted (the commitment survives).
- **Keyed MAC** — the chain link is HMAC-SHA256 keyed off a subkey derived from the
  at-rest encryption key when encryption is enabled (tamper-evident vs. a recomputing
  attacker); unkeyed SHA-256 otherwise.

Full design rationale, threat model, and stated limits are in the repo:
`docs/audit-chain-keyed-mac-design.md` (RFC) and `docs/THREAT-MODEL.md`.

## 3. Scope
**In scope** (design + implementation):
1. **HMAC-SHA256 construction & implementation** — the hand-rolled HMAC over the `sha2`
   crate (RFC 2104 correctness; it is unit-tested against RFC 4231, but review the code):
   `src/db.rs` `hmac_sha256`.
2. **Key derivation & domain separation** — `audit_key = SHA256("perseus-vault/audit-chain/v1\0" || enc_key)`
   and the audit-key canary `HMAC(audit_key, fixed-label)`: `src/encryption.rs`
   `derive_audit_key`, `src/db.rs` `audit_key_canary`.
3. **Chain link & commitment** — framing (length-prefixed fields), what the link covers,
   the payload-commitment scheme and its redaction interaction: `src/db.rs`
   `audit_chain_mac`, `audit_payload_commitment`, `rehash_audit_chain_keyed`,
   `verify_audit_chain`.
4. **Migration & ordering** — the encryption-key-is-set-after-DB-open ordering, the
   v14→v15 migration, and the canary-gated rekey in `set_encryption`
   (`src/db.rs::set_encryption`, `src/schema.rs` v15 block).
5. **`verify` semantics** — scheme-awareness and fail-closed behavior when a keyed chain
   is verified without the key.
6. **Validation of the residual-risk claims** in RFC §5 (does the design deliver what it
   claims, and are the stated limits complete?).

**Out of scope:** the AES-256-GCM at-rest encryption itself (previously reviewed sound);
the broader MCP transport/auth surface; CMMC compliance assessment (that is a C3PAO
engagement, separate from this crypto review).

## 4. Specific questions to answer (RFC §7)
1. **Key choice** — derive the audit key from the encryption key (current), or a separate
   `--audit-key` for separation-of-duties (audit signing independent of at-rest encryption)?
2. **External anchoring** — is periodic publication of the chain head to an append-only
   external sink (transparency log / notarization) needed to defend against an attacker
   who holds the key, for a defensible "audit trail" claim?
3. **Commitment salting** — should the payload commitment be `HMAC(audit_key, payload)`
   in keyed mode to also hide low-entropy content after redaction (vs. unsalted SHA-256)?
4. **Key rotation** — on encryption-key rotation the chain is rekeyed; is a dual-canary
   window to retain verifiability under the old key required?

## 5. Materials provided
- RFC: `docs/audit-chain-keyed-mac-design.md`
- Threat model: `docs/THREAT-MODEL.md`
- Implementation: PR #463 (keyed-MAC + commitment, merged) and PR #464 (canary-gated
   rekey, open); primary files `src/db.rs`, `src/encryption.rs`, `src/schema.rs`.
- Public repo; a specific reviewed commit hash will be pinned at kickoff.

## 6. Deliverables
- A written report: methodology, severity-ranked findings (design + implementation),
  and explicit recommendations on the four questions in §4.
- Confirmation (or refutation) of each residual-risk claim in RFC §5.
- A short re-review pass after we address report findings (fix verification).
- Permission to publish the report (open-source project; we intend to link it publicly).

## 7. Threat model (summary; full doc in repo)
Local-first, MIT open source. The MCP caller is trusted; workspace scoping is a
routing control, not an enforced boundary. The audit-chain adversary is one who can read
and/or write the SQLite file (stolen backup, another local process) or drive the engine's
APIs, and wants to alter/reorder/delete/forge journal history undetected. Known inherent
limits (to confirm): an attacker holding both the DB and the MAC key can forge; unencrypted
deployments have no key and fall back to an unkeyed chain; redacted content is unrecoverable
by design.

## 8. Logistics
- Engagement size: one subsystem; we estimate a few days to ~two weeks of reviewer effort.
- Format: remote; GitHub for Q&A; report as PDF/markdown.
- Timeline: seeking to complete before we cut the release that ships the keyed chain and
  before any CMMC positioning.
- Budget: seeking OSTIF facilitation/subsidy given the MIT/open-source status; open to a
  direct fixed-price quote otherwise.
