# Internal Vulnerability Response Policy

This is the **internal runbook** for handling a security report. The
outward-facing promise to reporters — where to send reports and what response
to expect — lives in [`SECURITY.md`](../SECURITY.md). This document is the
process the maintainers follow once a report arrives, so response is consistent
and time-bound rather than improvised.

*Scope: Perseus Vault, the local MCP memory engine (this repo). The context
engine has its own copy of this policy in `perseus/docs/vuln-response.md`; a
report that spans both is handled jointly under whichever repo the root cause
lives in.*

---

## 1. Roles

| Role | Who | Responsibility |
|---|---|---|
| **Security Lead** | Thomas Connally (perseus@perseus.observer) | Owns the report end-to-end: acknowledgment, triage, fix coordination, disclosure. |
| **Backup handler** | Mark Thrailkill (mark@perseus.observer) | Covers acknowledgment and triage when the Security Lead is unavailable (OOO, >24h). |

At least one handler monitors the intake channels every business day. If the
primary is unreachable within the acknowledgment window, the backup takes over.

## 2. Intake channels

1. **Email** — `perseus@perseus.observer` (published in `SECURITY.md`).
2. **GitHub Private Vulnerability Reporting** — enabled on the repo; reports
   arrive as private security advisories.

Reports through any other channel (public issue, social, etc.) are moved to a
private channel immediately and the public trace is minimized.

## 3. Response timeline

These match the commitments in `SECURITY.md` — do not let them drift apart.

| Stage | Target | Action |
|---|---|---|
| **Acknowledge** | ≤ 48 hours | Confirm receipt to the reporter; open a private tracking advisory. |
| **Triage** | ≤ 5 business days | Reproduce, assign severity (§4), decide fix path and target date. |
| **Fix** | severity-dependent (§4) | Develop and test the fix under embargo. |
| **Disclose** | coordinated | Release fix, publish advisory, assign CVE, credit reporter. |

## 4. Severity & fix targets

Severity uses **CVSS v3.1** as the baseline, adjusted for Perseus Vault's actual
threat model (see [`docs/THREAT-MODEL.md`](./THREAT-MODEL.md) — the primary trust
boundary is the local machine and filesystem; the engine is single-operator and
local-first by default, so many "network" vectors score lower in practice). The
band sets the fix urgency:

| Severity | CVSS | Fix target | Examples for Perseus Vault |
|---|---|---|---|
| **Critical** | 9.0–10.0 | ≤ 7 days | Encryption bypass exposing `body_json` at rest; RCE via a crafted MCP request; audit-chain forgery. |
| **High** | 7.0–8.9 | ≤ 30 days | SQL/FTS injection via entity input; path traversal in `ingest_file`; nonce reuse in AES-GCM. |
| **Medium** | 4.0–6.9 | ≤ 90 days | DoS via a malformed request; metadata leak beyond the documented plaintext caveat. |
| **Low** | 0.1–3.9 | next release | Info leak in verbose logs; hardening gaps with no direct exploit. |

If a fix cannot meet its target (e.g. an upstream crate has no patch), the
Security Lead documents the interim mitigation and the reason in the advisory.

## 5. Fix, embargo, and disclosure

- **Embargo.** Work happens in a private fork/branch or GitHub Security Advisory
  workspace. No public commit, issue, or PR references the vulnerability before
  the coordinated release.
- **CVE.** Request a CVE via the GitHub Security Advisory "Request CVE" flow
  (GitHub is a CNA). A CVE is requested for every Medium+ issue.
- **Release.** Ship the fix in a patched release across all supported versions
  (see `SECURITY.md` "Supported Versions"). Because the engine ships as a
  prebuilt binary, rebuild and re-publish release assets, not just the tag.
  Publish the advisory at release time.
- **Credit.** Credit the reporter in the advisory unless they ask otherwise.
  We treat good-faith research under this policy as authorized — no legal action.
- **Disclosure window.** Default coordinated disclosure is at fix release, or
  90 days from report, whichever comes first; extended only by mutual agreement
  with the reporter.

## 6. Continuous prevention

Response is the last line; these keep reports rare:

- **Dependency CVEs** — `cargo-audit` runs on every push/PR and weekly
  (`.github/workflows/audit.yml`) against the RustSec advisory database.
- **SBOM** — see [`docs/SBOM.md`](./SBOM.md) for CycloneDX generation.
- **Threat model, encryption spec & security review** — see
  [`docs/THREAT-MODEL.md`](./THREAT-MODEL.md),
  [`docs/ENCRYPTION.md`](./ENCRYPTION.md), and
  [`docs/security-review-2026-07-05.md`](./security-review-2026-07-05.md).
- **Audit-chain crypto review** — external review is scoped in
  [`docs/audit-chain-crypto-review-SOW.md`](./audit-chain-crypto-review-SOW.md)
  and gates any CMMC "audit trail" claim.

## 7. Post-incident

After any Medium+ incident, the Security Lead writes a short retrospective
(root cause, timeline, what prevention would have caught it) and files any
resulting hardening work as tracked issues. Update this policy if the process
itself failed.

---

*Last reviewed: 2026-07-10.*
