# CMMC L2 + NIST SP 800-171 Evidence Matrix — Perseus Vault & Ledger

**Status:** living document · **Resolves:** #1063 · **Prepared:** 2026-08-15 · Perseus Computing LLC

> This document maps the **product's** evidence-generating surfaces to CMMC Level 2 / NIST SP 800-171 audit-evidence requirements. It is NOT a company-level CMMC/SPRS assessment (that posture lives in the company SSP and SPRS/CMMC portal records) and NOT a NIST AI RMF mapping (see `docs/NIST-AI-RMF-ALIGNMENT.md`). It proves the product generates audit-ready evidence; the operator's enclave supplies platform-level controls.

## 1. Operative standard (read this first)

- **CMMC Level 2 operates against NIST SP 800-171 Revision 2.** 32 CFR 170 §170.20(a) ("Standards acceptance") states the scope of the Level 2 certification assessment is aligned to "NIST SP 800-171 R2 DoD assessments". L2 = 110 security requirements / 320 assessment objectives.
- **NIST withdrew SP 800-171r2 on 2024-05-14** and superseded it with **Rev 3** (May 2024). The withdrawal is for NIST publication purposes; DoD's CMMC program still anchors on R2. A Rev 2→Rev 3 crosswalk (incl. the ~30 CMMC L2 assessment objectives aligned to Rev 3 organization-defined parameters) is tracked as gap **G3**.
- Rev 2 family numbering used below: **3.1 AC** (22) · **3.3 AU** (9) · **3.13 SC — System and Communications Protection** (16) · **3.14 SI — System and Information Integrity** (7). Note: SI-7/SI-4 style control IDs are NIST SP 800-53 tailoring inputs; the operative requirements are the 3.x.y rows. (SI-7 integrity is "NFO — not a requirement" in Rev 2 tailoring; integrity evidence is argued via AU-9/3.3.8, SC-28/3.13.16, and SI-4/3.14.6-7.)

## 2. Methodology & honesty rules

- Status per row: **SERVED** (artifact on main, replayable) · **PARTIAL** (exists, documented limitation) · **DEPLOY** (operator/enclave responsibility; product binds via named surface) · **GAP** (missing, tracked in §7).
- No inflation: a row is SERVED only when a deterministic, replayable artifact exists — never a process promise. Gaps are stated as gaps; a low-but-honest posture beats a high-and-unverifiable one.
- "Non-repudiation argument" = what a skeptical assessor can verify: hash continuity, signature/key coverage, actor binding, and replay steps (runbook §).
- Evidence definitions: see `docs/authority-and-aar-control-plane.md`, `docs/evidence-chain-guidance.md`, `docs/specs/claim-cards.md`, `docs/specs/artifact-manifests-and-exact-evidence.md`, `docs/specs/deterministic-evidence-log-digests.md`, `docs/court-of-record.md`, `docs/audit-chain-keyed-mac-design.md`.

## 3. Evidence inventory (product artifacts)

| # | Artifact | Surface | What it proves | Replay |
|---|---|---|---|---|
| E1 | Journal audit chain (v14) | journal / timeline tools | append-only order, id, time, workspace continuity (`prev_hash` chained over `id, created_at_unix_ms, workspace_hash`) | RB §2 |
| E2 | Authority manifests | `authority_get` / `authority_set` | per-workspace, agent-bound grant of named capabilities; revocation | RB §3 |
| E3 | AAR receipts | `action_intent` → `action_lease_acquire` → `action_complete` → `action_receipt_get` | intent, lease holder, completion, outcome + durable hashes; fail-closed denial recorded | RB §3 |
| E4 | Ledger receipts (v1.2.3+) | `ledger_record`, receipt verification block | HMAC-SHA256 signature, evidence levels (structural/attested/replay/inclusion), trusted-key attestation, inclusion anchors | RB §4 |
| E5 | Corrections + rejection tombstones | `correct` → correction entity + tombstone; `supersede` | rejection provenance, valid-period closure, full history (never in-place edit) | RB §5 |
| E6 | Claim cards (#852) | `claim_card` | deterministic evidence-backed projection; sha256-bound `agent_projection`; supersession/contradiction/stale state | RB §6 |
| E7 | Artifact store (#811) | artifacts + manifests + `artifact_excerpt` | full SHA-256 content identity, preserved original bytes, exact excerpt verification | RB §7 |
| E8 | Deterministic evidence-log digests (#812) | `artifact_log_digest` | deterministic navigation view; `error/denied/refused/…` lines preserved verbatim; exact counts | RB §8 |
| E9 | Keystones (#683) | `keystone_get` | mandatory policy rules fetched deterministically, merged by scope, obeyed over conflicting instruction | RB §9 |
| E10 | Court of record (#940) | `consistency_audit` + `audit_ruling` | contradiction findings w/ deterministic recommendation; operator rulings compile into supersede guards (model recommends — never enforces) | RB §9 |
| E11 | Vault↔Ledger bridge | ledger `evidence_hashes`, `policy_version`, `result_hash`, `context_render_schema`, `served_memory_provenance_hash`, `action_receipt_hash` | hash-only binding of served memory + rendered context + receipts into the ledger audit trail | RB §4 |
| E12 | Deterministic recall + state digest | recall contract + state digest | reproducible retrieval over frozen DB; byte-stable context assembly | RB §10 |
| E13 | Encryption at rest | DB key path (AES-256-GCM) | CUI-at-rest confidentiality (`docs/ENCRYPTION.md`) | RB §9 |

## 4. Matrix — 3.1 Access Control (AC, 22 requirements)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.1.1 limit access to authorized users/processes/devices | E2 manifests (agent+workspace bound); MCP server authn; identity gates on read surfaces (branch `feat/vault-996-leak-harness`, not yet main) | access requires a registered agent identity + non-empty workspace-scoped manifest; unauthorized read blocked at surface | **PARTIAL** → SERVED on #996 merge |
| 3.1.2 limit access to functions | curated MCP tool allowlist (#911); per-tool authority gates | only allowlisted tools exposed; governed writes require manifest capability | **SERVED** |
| 3.1.3 control flow of CUI | workspace scoping (#904); federation/share boundary | cross-workspace flow requires explicit `share`/`federate` action w/ receipt | **SERVED** |
| 3.1.4 separation of duties | operator-review queue; court-of-record (E10) | model recommends, operator rules; contributor ≠ reviewer ≠ authority | **SERVED** (process, audited) |
| 3.1.5 least privilege | E2 capability-scoped manifests; E3 single-active leases | privilege = named capability grant, not ambient role; lease serializes mutators | **SERVED** |
| 3.1.6 non-privileged accounts | bounded agent toolsets; read-only surfaces | agents hold only granted tools; account hygiene = enclave | **PARTIAL**/DEPLOY |
| 3.1.7 prevent + log privileged function execution | E3 fail-closed intents + journal | denied intent recorded; execution without manifest impossible (fail-closed) | **SERVED** |
| 3.1.8 limit unsuccessful logon attempts | transport authn | enclave SSO/OS layer | **DEPLOY** |
| 3.1.9 privacy/security notices | docs (retention, encryption, export control) | disclosure artifacts | **DEPLOY** |
| 3.1.10–3.1.11 session lock / auto-terminate | OS/session layer | enclave | **DEPLOY** |
| 3.1.12–3.1.17 remote access & wireless | TLS/gRPC transport (`docs/GRPC-SECURITY.md`) | transport confidentiality; product binds via authenticated RPC | **DEPLOY** (product: authenticated MCP surface) |
| 3.1.18–3.1.19 mobile devices / encrypt CUI on them | E13 at-rest encryption | AES-256-GCM on DB + artifacts | **PARTIAL**/DEPLOY |
| 3.1.20–3.1.21 external systems / portable storage | enclave boundary | enclave | **DEPLOY** |
| 3.1.22 CUI on public systems | redaction discipline (publication custody: secrets never cross the publish boundary) | sanitized projections only; secret-scanner gates | **PARTIAL** (policy, not yet machine-enforced on all exports) |

## 5. Matrix — 3.3 Audit & Accountability (AU, 9 requirements)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.3.1 create + retain audit logs | E1 journal chain + E4 ledger events + E7 artifact manifests | every mutation journaled; chain links records; retention documented | **PARTIAL** (chain unkeyed → G1) |
| 3.3.2 uniquely trace actions to users | journal `agent_id`/actor per event; E3 receipts bind agent+workspace | per-actor attribution via registered identity + receipt | **SERVED** |
| 3.3.3 review/update logged events | operator-review queue; `consistency_audit`; timeline/journal reads | review loop with deterministic recommendations | **SERVED** |
| 3.3.4 alert on audit logging failure | fail-closed AAR errors; ledger prebind failures surfaced | logging failure = operation failure (no silent pass) | **PARTIAL** (alerting channel = deployment) |
| 3.3.5 correlate reviews for investigation | E11 bridge; bi-temporal `as_of`/`valid_at`; graph `traverse` | hash-only bindings join served-memory evidence to ledger events | **SERVED** |
| 3.3.6 reduction + report generation | E8 log digests; E6 claim cards; hygiene reports | deterministic summaries w/ verbatim-preserved risk lines | **SERVED** |
| 3.3.7 time sync w/ authoritative source | `created_at_unix_ms` in chain | authoritative clock = enclave NTP (documented in runbook) | **PARTIAL**/DEPLOY |
| 3.3.8 protect audit info from access/modification/deletion | append-only journal + chain continuity + governed purge w/ history | modification breaks chain; deletion = governed, tombstoned, chain-visible | **PARTIAL** (unkeyed chain forgeable by motivated attacker → G1) |
| 3.3.9 limit audit-logging management to privileged subset | E2 gated destructive ops; operator-only purge | privileged management requires manifest capability | **SERVED** |

## 6. Matrix — 3.13 System & Communications Protection (SC, 16 requirements)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.13.1 boundary comms monitoring | gRPC/TLS surface | transport monitoring = enclave | **DEPLOY** |
| 3.13.2 effective architecture principles | Rust memory-safety, deterministic recall, typed ontology, fail-closed design | design artifacts (specs/) | **PARTIAL** (narrative) |
| 3.13.3 separate user vs system management | read surfaces vs governed writes vs admin tools split | distinct MCP tool classes w/ distinct authority | **SERVED** |
| 3.13.4 prevent info transfer via shared resources | workspace scoping + per-workspace retrieval filtering | cross-workspace leakage blocked by scope, not policy prose | **SERVED** |
| 3.13.5 subnetworks | enclave | enclave | **DEPLOY** |
| 3.13.6 deny by default | fail-closed AAR; retrieval abstention (#953); deny-by-default tool policy | default action without grant = deny + record | **SERVED** |
| 3.13.7–3.13.9 remote sessions, transmission crypto, session termination | TLS transport | enclave/transport | **DEPLOY** |
| 3.13.10 key establishment & management | DB encryption key path; ledger trusted-key registry (`key_registry`, `sign_key_id`) | key lifecycle visible in receipts | **PARTIAL** (vault chain keying = G1) |
| 3.13.11 FIPS-validated cryptography for CUI | — | no FIPS-validated module today (AES-256-GCM + HMAC-SHA256 implementations not certified) | **GAP** (G2) |
| 3.13.12–3.13.14 collaborative devices, mobile code, VoIP | n/a for this product surface | n/a | **DEPLOY/N/A** |
| 3.13.15 authenticity of comms sessions | TLS + gRPC authn + MAC'd ledger receipts | session + artifact authenticity | **PARTIAL** |
| 3.13.16 protect CUI at rest | E13 AES-256-GCM DB + artifact encryption | key-path-encrypted storage | **SERVED** (implementation; certification = G2) |

## 7. Matrix — 3.14 System & Information Integrity (SI, 7 requirements)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.14.1 identify/report/correct flaws | CI regression gates, SBOMs, security-review cadence, issue tracker | fix → test → changelog loop with reproducible failures | **PARTIAL** (process) |
| 3.14.2 malicious code protection | SBOM + dependency audit + CI scans | supply-chain evidence | **DEPLOY**/PARTIAL |
| 3.14.3 monitor security alerts & advisories | SECURITY.md + release advisories | disclosure artifacts | **PARTIAL** |
| 3.14.4 update malicious-code mechanisms | enclave AV/patching | enclave | **DEPLOY** |
| 3.14.5 periodic + real-time scans | CI security gates; pre-commit scans | scan evidence | **PARTIAL** |
| 3.14.6 monitor systems for attacks/indicators | quality telemetry (contradictions/anomalies), `integrity_check`, E8 verbatim `denied/refused` preservation, #996 leak harness | memory-surface monitoring with deterministic, replayable signals | **SERVED** |
| 3.14.7 identify unauthorized use | E2/E3 denials journaled; #996 identity gates; contradiction telemetry | unauthorized attempts leave receipts, not silence | **PARTIAL** → SERVED on #996 merge |

## 8. Matrix — remaining families (phase 2, complete)

### 3.2 Awareness & Training (AT, 3)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.2.1 security awareness | operator guides (AAR usage contract, court-of-record guide, this matrix) | written awareness artifacts, dated + versioned | **PARTIAL** (training delivery = operator) |
| 3.2.2 role-based training | docs/authority-and-aar-control-plane.md usage contract | role duties documented per surface | **DEPLOY** |
| 3.2.3 insider-threat reporting | AAR denials + contradiction/anomaly telemetry | reporting path surfaced by receipts, not silence | **DEPLOY**/PARTIAL |

### 3.4 Configuration Management (CM, 9)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.4.1 baseline configs + inventories | SBOMs (NTIA elements), registry pins, deployment profiles (#870) | published SBOM per release; pinned manifests | **SERVED** |
| 3.4.2 security configuration settings | deployment-profiles spec + signed policy/authority profiles (#847) | signed, distributable config artifacts | **SERVED** |
| 3.4.3 track/review/approve/log changes | git history + PR review + CI + signed releases | every change = reviewed commit w/ CI evidence | **SERVED** |
| 3.4.4 analyze security impact pre-implementation | PR review gates + pre-commit security scans + THREAT-MODEL.md updates | impact analysis artifacts per change | **PARTIAL** |
| 3.4.5 access restrictions for change | branch protection + merge gated by AAR | merge = receipted authorized action | **SERVED** |
| 3.4.6 least functionality | curated MCP tool allowlist (#911) + deployment profiles | only allowlisted surfaces exposed | **SERVED** |
| 3.4.7 restrict nonessential programs/ports | minimal container surface + enclave firewall | enclave | **DEPLOY** |
| 3.4.8 deny-by-exception unauthorized software | enclave software allowlisting | enclave | **DEPLOY** |
| 3.4.9 control/monitor user-installed software | enclave | enclave | **DEPLOY** |

### 3.5 Identification & Authentication (IA, 11)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.5.1 identify users/processes/devices | agent registry (`perseus_vault_agent`: tier, fleet default) + process identity incl. macOS fallback (#950) | registered identity bound to every write | **SERVED** |
| 3.5.2 authenticate identities | MCP server authn + HMAC-SHA256 trusted-key attestations (ledger `key_registry`) | signed attestations verify actor identity | **SERVED** |
| 3.5.3 multifactor authentication | enclave SSO | enclave | **DEPLOY** |
| 3.5.4 replay-resistant authentication | receipt timestamp/nonce fields + signed profiles | replay = duplicate receipt detection | **PARTIAL** (transport-level = enclave) |
| 3.5.5–3.5.9 identifier & password lifecycle | enclave identity provider | enclave | **DEPLOY** |
| 3.5.10 cryptographically-protected passwords | product stores no passwords (token/key-based authn) | no plaintext credential surface by design | **SERVED (by design)** |
| 3.5.11 obscure auth feedback | generic authn failures (no oracle) | failures leak nothing | **SERVED** |

### 3.6 Incident Response (IR, 3)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.6.1 incident-handling capability | court-of-record reversal, `authority_revoke`, quarantine/forget, fail-closed AAR | response mechanics exist + audited; formal IRP doc = operator | **PARTIAL** |
| 3.6.2 track/document/report incidents | journal + issues + receipts | every incident traceable w/ receipt | **SERVED** |
| 3.6.3 test IR capability | authority-trace suite v2 (split-brain, derived-revocation, key-rotation cases) | deterministic security corpus w/ real digests | **SERVED** |

### 3.7 Maintenance (MA, 6)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.7.1 perform maintenance | release process + upgrade guides | versioned release artifacts | **DEPLOY** |
| 3.7.2 maintenance tool controls | pinned CI toolchains + signed artifacts | tool identity pinned per release | **PARTIAL** |
| 3.7.3–3.7.6 equipment sanitization, media checks, nonlocal MFA, supervision | enclave | enclave | **DEPLOY** |

### 3.8 Media Protection (MP, 9)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.8.1 protect media | AES-256-GCM at rest + enclave physical | encrypted storage | **PARTIAL**/DEPLOY |
| 3.8.2 limit access to CUI on media | workspace scoping + per-workspace retrieval filtering | scope, not policy prose | **SERVED** (digital analog) |
| 3.8.3 sanitize/destroy media | retention lifecycle physical erasure (#866/#868) + deletion-residue accounting w/ independent sweep (#990) | erasure with independent verification | **SERVED** (digital analog) |
| 3.8.4 mark media | CUI marking policy | operator | **DEPLOY** |
| 3.8.5 transport accountability | export manifests + hash-bound exports | hash-bound transfer artifacts | **PARTIAL** |
| 3.8.6 crypto for media transport | encrypted exports/backups | key-path encryption | **PARTIAL** |
| 3.8.7–3.8.8 removable/portable media | enclave | enclave | **DEPLOY** |
| 3.8.9 backup CUI protection | backup encryption guidance (runbook) | operator | **DEPLOY** |

### 3.9 Personnel Security (PS, 2)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.9.1 personnel screening | operator HR process | operator | **DEPLOY** |
| 3.9.2 protect during/after termination/transfer | `authority_revoke` + manifest expiry on offboarding | access dies with the grant, receipted | **PARTIAL** (product side) |

### 3.10 Physical Protection (PE, 6) — all **DEPLOY** (enclave facility; product has no physical surface)

### 3.11 Risk Assessment (RA, 3)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.11.1 periodically assess risk | THREAT-MODEL.md + security-review-2026-07-05.md + NIST-AI-RMF-ALIGNMENT.md | dated, versioned risk artifacts | **SERVED** |
| 3.11.2 vulnerability scanning | CI security gates + SBOM + dependency audit cadence | scan evidence per release | **PARTIAL** |
| 3.11.3 remediate per risk assessments | issue tracker + gap register G1–G6 (POA&M analog) | risk → issue → fix loop, all receipted | **SERVED** |

### 3.12 Security Assessment (CA, 4)

| Req | Artifact | Non-repudiation argument | Status |
|---|---|---|---|
| 3.12.1 periodically assess controls | self-assessment + this matrix + benchmark/quality/security gates | assessment artifacts, replayable | **SERVED** |
| 3.12.2 plans of action (POA&M) | gap register + open issues w/ owners/labels | deficiencies tracked to closure | **SERVED** |
| 3.12.3 continuous monitoring | quality telemetry + hygiene + `integrity_check` + consistency audit | ongoing, deterministic monitoring signals | **SERVED** |
| 3.12.4 system security plan | company SSP (SPRS portal records; compliance/sprs-cmmc-verified-2026-08-08) | company-level artifact | **DEPLOY** (company) |

## 9. Gap register

| # | Controls | Gap | Action |
|---|---|---|---|
| G1 | AU-3.3.8, SC-3.13.10 | v14 journal chain is **unkeyed** — a motivated attacker who can write the DB can recompute a valid chain; payload excluded (redaction-safe) so chain attests existence/order/time, not content | implement keyed-MAC audit chain per `docs/audit-chain-keyed-mac-design.md` (RFC drafted; no surviving first-pass branch — fresh implementation; staged in #1067) |
| G2 | SC-3.13.11 | no FIPS-validated crypto module | guidance + decision delivered: `docs/compliance/fips-validated-crypto-path.md` (Paths A/B deploy-now, Path C roadmap) — row now PARTIAL w/ dated plan; #1068 |
| G3 | all families | Rev 2→Rev 3 crosswalk + CMMC ODP alignment | delivered: `docs/compliance/nist-800-171-rev2-rev3-crosswalk.md`; #1069 |
| G4 | AU-3.3.7 | authoritative time = operator NTP | runbook step (no code change) |
| G5 | AC-3.1.1, SI-3.14.7 | read-surface identity gates live on branch `feat/vault-996-leak-harness` | merge #996 — in flight |
| G6 | AU-3.3.8 adjacency | persisted-state/export tamper evidence | cross-ref #1060 (seal-style tamper evidence) |

## 10. Sources

- NIST SP 800-171 Rev 2 (Feb 2020, upd. Jan 2021) — withdrawn 2024-05-14, superseded by Rev 3; still operative for CMMC L2 via 32 CFR 170 §170.20
- 32 CFR Part 170, Cybersecurity Maturity Model Certification Program — https://www.ecfr.gov/current/title-32/subtitle-A/chapter-I/subchapter-G/part-170
- CMMC L2: 110 security requirements / 320 assessment objectives
- Internal: the docs referenced in §2/§3 (all paths relative to this repo)
