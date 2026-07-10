# Security Documentation Index

A single entry point to everything security-relevant in Perseus Vault: the
documents, the processes, and who holds which privileges. This satisfies OSTIF
best-practices **step 6** (an updated knowledgebase tracking security efforts and
access privileges) and is the map an auditor or contributor should start from.

*Last reviewed: 2026-07-10.*

---

## 1. Document map

| Document | What it covers |
|---|---|
| [`../SECURITY.md`](../SECURITY.md) | Reporting policy, supported versions, security model, encryption summary, attack surface, compliance posture |
| [`THREAT-MODEL.md`](./THREAT-MODEL.md) | What Vault defends against, what it does **not**, and residual operator-owned risk |
| [`ENCRYPTION.md`](./ENCRYPTION.md) | AES-256-GCM at-rest specification: algorithm, nonce/AAD, scope, key handling, limits |
| [`vuln-response.md`](./vuln-response.md) | Internal vulnerability-response runbook: handler roles, CVSS severity rubric, embargo, CVE/disclosure flow |
| [`security-review-2026-07-05.md`](./security-review-2026-07-05.md) | Pre-launch internal security review and findings |
| [`audit-chain-crypto-review-SOW.md`](./audit-chain-crypto-review-SOW.md) | Scope of work for the pending **external cryptographic review** of the audit chain + encryption |
| [`audit-chain-keyed-mac-design.md`](./audit-chain-keyed-mac-design.md) | Tamper-evident audit-chain design (SHA-256 + keyed-MAC / payload commitment) |
| [`SBOM.md`](./SBOM.md) | CycloneDX software bill of materials for dependency transparency |
| [`transport.md`](./transport.md) / [`GRPC-SECURITY.md`](./GRPC-SECURITY.md) | Transport posture (stdio default; optional HTTP/SSE/gRPC security) |
| [`EXPORT-CONTROL.md`](./EXPORT-CONTROL.md) | Export-control classification of the cryptography |
| [`NIST-AI-RMF-ALIGNMENT.md`](./NIST-AI-RMF-ALIGNMENT.md) | Alignment with the NIST AI Risk Management Framework |
| [`deterministic-recall-and-provenance.md`](./deterministic-recall-and-provenance.md) | Provenance and deterministic-recall guarantees |
| [`retention.md`](./retention.md) | Data retention and lifecycle behavior |
| [`SECURITY-MILESTONES.md`](./SECURITY-MILESTONES.md) | Predefined triggers for escalating security effort (OSTIF step 7) |

## 2. Automated security controls (CI)

| Control | Where | Posture |
|---|---|---|
| Dependency CVE scanning | `.github/workflows/audit.yml` (`cargo-audit`) | Gating on push/PR + weekly, against the RustSec advisory DB |
| Static analysis (SAST) | `.github/workflows/codeql.yml` (CodeQL, Rust `build-mode: none`) | Non-gating, weekly + push/PR; findings in the Security tab |
| Private vulnerability reporting | GitHub Security → Private Vulnerability Reporting | **Enabled** — reports arrive as private advisories |

## 3. Access & privileges register

Governance transparency, not secrets. This tracks **who holds which privilege** so
access can be reviewed and revoked. No keys or tokens appear here.

| Privilege | Holder(s) | Notes |
|---|---|---|
| Repository admin | Thomas Connally (`tcconnally`) | Sole repo-admin as of 2026-07-10. Mark Thrailkill contributes via org write access — `[CONFIRM whether Mark should hold admin]` |
| Merge to protected `main` | via PR + required `test` check | ✅ Verified: `main` is protected and requires the `test` status check. No direct pushes. |
| Release / publish (crates.io, GHCR, MCP registry) | `[CONFIRM token holder(s)]` | Publishing credentials held out-of-band, not in repo |
| Release signing / provenance | *none yet* | Signed releases + SLSA provenance are a tracked milestone (§ SECURITY-MILESTONES) |
| Security disclosure — primary handler | Thomas Connally (perseus@perseus.observer) | See [`vuln-response.md`](./vuln-response.md) |
| Security disclosure — backup handler | Mark Thrailkill (mark@perseus.observer) | Covers when primary is unavailable |

> **Review cadence:** revisit this register whenever a team member joins/leaves,
> a new publishing target is added, or a signing key is created. Update the
> `Last reviewed` date above on each pass.

## 4. How the pieces fit

- **Someone found a vulnerability** → [`../SECURITY.md`](../SECURITY.md) (how to report) → [`vuln-response.md`](./vuln-response.md) (how we handle it).
- **An auditor wants scope** → [`THREAT-MODEL.md`](./THREAT-MODEL.md) + [`ENCRYPTION.md`](./ENCRYPTION.md) + [`audit-chain-crypto-review-SOW.md`](./audit-chain-crypto-review-SOW.md).
- **"Should we harden further / get an audit yet?"** → [`SECURITY-MILESTONES.md`](./SECURITY-MILESTONES.md).
