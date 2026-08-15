# Evidence-Generation Runbook — CMMC L2 / NIST SP 800-171 Assessor Walk

**Companion to:** `cmmc-nist-800-171-evidence-matrix.md` (#1063) · **Status:** living document

This runbook tells an assessor (or a red-team reviewer) how to *produce* and *verify* each evidence class named in the matrix, with the exact surfaces and checks. Evidence is replayable by design: every step is a read-only query or a hash recomputation, never a claim.

## 0. Prerequisites

1. A deployment with the vault server + MCP surface (or CLI) reachable, and Ledger configured for the bridge (v1.2.3+).
2. A registered agent identity (`hermes-agent`, tier 3 fleet default) and a workspace hash, conventionally `sha256("<Org>/<repo>")` — manifests are per-workspace and refuse the empty/global scope.
3. `PERSEUS_VAULT_WORKSPACE` set consistently for every tool call (blank resolves global for *memory*, but manifests require explicit scope).
4. Never export raw memory bodies, prompts, tokens, or credentials into evidence packages — evidence carries hashes and metadata (see §11 redaction boundary).

## 1. Journal audit chain (E1 — AU 3.3.1/3.3.8)

1. Export the journal range: `perseus_vault_timeline` (or journal dump) for the assessment window.
2. Walk the chain: for each record, recompute `sha256(prev_hash | id | created_at_unix_ms | workspace_hash)` and compare to the stored `prev_hash` of the next record. A single mismatch = chain break (report, don't repair).
3. Verify ordering (monotonic `created_at_unix_ms`) and workspace scoping on every record.
4. **State the limitation** (G1): the chain is unkeyed — it proves order/continuity against accidental corruption, not authorship under a DB-writing attacker. Until the keyed-MAC design (`docs/audit-chain-keyed-mac-design.md`) ships, present it as exactly that.
5. Retention: confirm the retention config matches the SSP statement; confirm `purge` history (archived entities + tombstones) rather than silent deletion.

## 2. Authority manifests (E2 — AC 3.1.1/3.1.5)

1. `perseus_vault_authority_get {agent_id, workspace_hash}` → active manifest (capabilities, expiry, scope anchors).
2. Show a **denial**: attempt an action outside the manifest → fail-closed error naming the missing capability + permitted set. The refusal itself is evidence.
3. Show revocation: `authority_revoke` → subsequent `authority_get` returns null → subsequent intents fail closed.
4. Verify the manifest is versioned and per-workspace (attempt a blank-scope manifest → refused).

## 3. AAR receipts & leases (E3 — AC 3.1.7, AU 3.3.2)

1. Record an intent: `action_intent {agent_id, workspace_hash, capability, scope_anchor, external_ref, action_key, intent_hash}` where `intent_hash = sha256("capability|scope_anchor|action|date|external_ref")`.
2. Lease: `action_lease_acquire` (single active lease; second acquisition refused).
3. Execute, then `action_complete` with outcome (executed/failed/cancelled).
4. `action_receipt_get` → durable metadata + hashes. Verify hashes recompute.
5. **Negative case:** intent without manifest → refusal recorded; show it in the journal.

## 4. Ledger receipts (E4/E11 — AU 3.3.1/3.3.2/3.3.5/3.3.8)

1. `ledger_record` with hash-only vault↔ledger bindings: `evidence_hashes` (64-hex array), `policy_version`, `result_hash`, `context_render_schema`, `served_memory_provenance_hash`, `action_receipt_hash`.
2. Open the receipt verification block: reported evidence level (structural → attested → replay → inclusion), per-level reason codes, downgrades vs claimed level.
3. Verify the HMAC-SHA256 signature with the trusted key from `key_registry` (note: the verification block is excluded from signed bytes — pop before verify, re-attach after).
4. Inclusion: commit receipts (`action_status executed`) require an inclusion anchor; show a sign-then-abort receipt correctly attested-not-inclusion.

## 5. Corrections & tombstones (E5 — AU 3.3.2, SI 3.14.6)

1. Produce a correction: `correct` on an entity → correction entity + rejection tombstone on the original; original preserved with history.
2. `supersede` → `supersedes` link, loser valid-period closed, status deprecated. Query `history` to show both versions + who + when.
3. `valid_at`/`as_of` queries show what the system *believed at time T* vs what it *recorded at time T* — the bi-temporal proof.

## 6. Claim cards (E6 — AU 3.3.6, evidence quality)

1. `claim_card {entity_id}` → claim, provenance class, valid/recorded times, confidence/support, supersession/contradiction/stale state, evidence refs, reason codes.
2. Recompute the digest: sha256 over the canonical key-sorted JSON subset; compare with `agent_projection` digest. Field ordering must not change the digest — demonstrate by reordering a JSON copy and re-hashing.

## 7. Artifact evidence (E7 — AU 3.3.1, SC 3.13.16)

1. Register/read an artifact → full SHA-256 content identity + `byte_length` + `content_b64` (encrypted at rest).
2. `artifact_excerpt` for exact bounded excerpts; verify excerpt bytes against the original file's sha256.
3. State what a digest does NOT prove (byte identity ≠ logical truth/validity/authority) — that distinction is part of the evidence posture.

## 8. Evidence-log digests (E8 — AU 3.3.6, SI 3.14.6)

1. `artifact_log_digest` on a run log → deterministic view; confirm `error/warn/denied/refused/timeout/assertion/traceback` lines are preserved verbatim and repeated sections carry exact counts + first/last anchors.
2. Use this as the on-demand reduction/report artifact for an incident window.

## 9. Keystones + court of record (E9/E10 — AC 3.1.4, SI 3.14.6)

1. `keystone_get` → mandatory policy rules merged by scope; confirm they are fetched deterministically (not relevance-ranked) and survive compaction.
2. `consistency_audit` → contradiction pairs with deterministic recommendations (`decided_by` naming the ladder rung); confirm the audit is read-only (no mutation).
3. `audit_ruling accept` → compiled supersede guard; confirm the model never self-enforces — only an operator ruling compiles a guard.

## 10. Deterministic recall + state digest (E12 — AU 3.3.6, SI 3.14.6)

1. Run the same recall twice over a frozen DB → byte-identical results (stable total order, id tie-breaks, no access-state writes in dense/hybrid paths).
2. `PRAGMA integrity_check` + WAL checkpoint rules (`-wal`/`-shm` must be empty before snapshotting evidence).
3. State digest: confirm the digest covers the inputs that change context assembly; replay the same query + snapshot → identical digest.

## 11. Redaction boundary (all families)

Evidence packages contain: entity IDs, category/key, hashes, timestamps, workspace hashes, tool names, reason codes, counts. They NEVER contain: raw memory bodies, prompts, model outputs, tokens, credentials, or full event payloads. Sanitization is idempotent (re-sanitizing an already-clean package changes nothing) and rejects unknown/private fields fail-closed. This boundary is itself auditable — a package that crosses it is a finding.

## 12. Assessor checklist

- [ ] Chain walked with zero breaks over the window (limitation stated per §1.4)
- [ ] Manifest grant + denial + revocation demonstrated (§2)
- [ ] Receipt hashes recompute; negative intent recorded (§3)
- [ ] Ledger evidence level reported with HMAC verification (§4)
- [ ] Correction tombstone + supersession + bi-temporal queries (§5)
- [ ] Claim-card digest recomputes; reordering doesn't change it (§6)
- [ ] Artifact sha256 verified against original bytes (§7)
- [ ] Log-digest preserves risk lines verbatim (§8)
- [ ] Keystone deterministic fetch; audit_ruling requires operator (§9)
- [ ] Deterministic recall byte-stable over frozen DB (§10)
- [ ] No raw content in the evidence package (§11)
