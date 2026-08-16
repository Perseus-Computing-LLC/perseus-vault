# Seal-style tamper evidence for persisted state and exports

Status: **Implemented** (2026-08-16, #1060). Borrowed from the 1F916 protocol's
memory-seal pattern (SHA-256 commitments recorded at write time, registry stores
hashes only) — scoped to the Vault's own integrity surfaces.

## 1. What a seal is

A **seal** is a SHA-256 commitment over content, recorded at write/export time
and stored **outside the sealed content itself**: the `memory_seals` row carries
the hash + label + target identity — never the content. Two scopes exist:

- **`entity`** — a seal over a live entity's stored `body_json` (the exact
  bytes the store serves). Created by `perseus_vault_seal`.
- **`export`** — a seal over the `.seals.json` manifest written next to an
  exported vault. The manifest holds per-file hashes; the seal row holds the
  manifest's own digest, so post-export edits to any note are detectable, and
  tampering with the manifest itself is caught by the seal.

**Integrity ≠ truth, by design.** A seal proves *unchanged since sealed* —
never *true when written*. The Vault's admission/supersession governance
(who may write, what supersedes what) answers the truth question; seals answer
only the tamper question. This honest limit is part of the design, exactly as
in the 1F916 spec.

## 2. Surfaces

| Surface | Behavior |
|---|---|
| `perseus_vault_seal` | Records a seal over a live entity (hash + label only); journals a `seal_created` event covered by the audit chain. |
| Compare-on-recall | Every recall re-hashes sealed entities' served bytes; a mismatch journals a `tamper_evidence` event naming the entity (expected vs actual hash) — **never served silently**. Deduplicated: repeated recalls of the same tampered row journal once per (expected, actual) pair. |
| `perseus_vault_tamper_scan` | Verifies every entity seal against live content and every export seal against the on-disk manifest bytes (a deleted manifest is a finding). Journals each mismatch and returns the full report. |
| `vault_export` | Writes `.seals.json` (schema 1: per-file path/sha256/bytes) next to the notes and records an `export` seal over the manifest. |

## 3. Guarantees and limits

- ✅ A bit-flipped persisted row surfaces as a named tamper event on the next
  recall — and in every scan.
- ✅ A tampered or re-exported vault is detected by the manifest seal.
- ✅ Seals leak no content: rows and events carry hashes only.
- ✅ Tamper events and seal events are part of the keyed-MAC audit chain
  (`docs/audit-chain-keyed-mac-design.md`), so the surface itself is protected.
- ❌ A seal does not authenticate the writer, and does not protect against an
  attacker who can also re-seal (no signing key attached in v1 — recorded as a
  future extension in the issue).
- ❌ Entities never sealed have no comparison anchor (sealing is opt-in at
  entity level; exports are sealed automatically).

## 4. Test coverage

- Seal rows store hash + metadata only; `seal_created` events never contain
  content; audit chain verifies after sealing.
- Bit-flipped persisted row → recall journals `tamper_evidence` naming the
  entity with expected/actual hashes; `tamper_scan` reports the same finding.
- Unmodified sealed row → recall silent, scan clean.
- Repeated scans over one tampered row → one journal event (dedup).
- Export: manifest per-file hashes match on-disk bytes; a tampered export file
  is detected and named; audit chain still verifies.
