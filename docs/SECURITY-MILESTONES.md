# Security Milestones

Predefined triggers for **when to increase security effort** — harden further,
commission an audit, or gate a claim — rather than deciding ad hoc. This
satisfies OSTIF best-practices **step 7** ("milestones in mind" for escalating
security). Each milestone pairs a **trigger** (an observable condition) with the
**action it gates**, so the decision is made in advance and honored when the
trigger fires.

*Last reviewed: 2026-07-10. Owner: Security Lead (see [`vuln-response.md`](./vuln-response.md)).*

---

## Escalation milestones

| # | Trigger | Action it gates | Status |
|---|---|---|---|
| M1 | **Before any public "CMMC Level 2 / audit trail" or equivalent compliance claim** | The **external cryptographic review** of the at-rest encryption + keyed-MAC audit chain must be complete and its findings remediated. Scope is drafted in [`audit-chain-crypto-review-SOW.md`](./audit-chain-crypto-review-SOW.md). | ⛔ **Hard gate — not yet met.** Do not make the claim until done. |
| M2 | **Before tagging 1.0 / GA** | (a) A full independent security audit; (b) signed releases with SLSA provenance; (c) close the plaintext gap — extend encryption from entity bodies to the FTS index + metadata. | 🟡 In progress — (b) SLSA build provenance wired into the release workflow (verify with `gh attestation verify … --repo …`); (a) and (c) open. |
| M3 | **First named production / enterprise / government adopter, OR sustained adoption** (e.g. notable downstream project embedding Vault, or meaningful crates.io/registry download volume) | Commission a full independent audit **and re-approach OSTIF** — at this point Vault meets their "widely-used open-source infrastructure" bar (the exact status change they asked us to signal). | ⬜ Open — see [OSTIF re-approach](#the-ostif-re-approach-trigger) |
| M4 | **Before enabling any network transport (HTTP/SSE/gRPC) by default** (today it is off; stdio-only) | Transport security review + an authentication design. See [`transport.md`](./transport.md) and [`GRPC-SECURITY.md`](./GRPC-SECURITY.md). | ⬜ Open (default-off holds it back) |
| M5 | **Any Critical or High severity vulnerability report** | Execute the [`vuln-response.md`](./vuln-response.md) timeline (48h ack, severity-banded fix targets, coordinated disclosure + CVE). | ♻️ Standing |
| M6 | **Continuous** | `cargo-audit` + CodeQL stay green; weekly advisory sweep; review every newly-added crate for maintenance/licensing/CVE posture before merge. | ♻️ Standing |

Legend: ⛔ hard gate · ⬜ open · ♻️ standing/recurring.

## The OSTIF re-approach trigger

Milestone **M3** is deliberately also our re-engagement criterion with OSTIF.
In July 2026 OSTIF declined a gratis audit because Vault was pre-traction, but
explicitly left the door open: *"if anything changes with your project's status,
don't hesitate to let us know."* M3 defines that status change concretely — a
named adopter or real download volume — so we know exactly when to reopen the
conversation rather than guessing. Until then we work OSTIF's best-practices
guide (this doc and [`SECURITY-INDEX.md`](./SECURITY-INDEX.md) are part of that
preparation).

## Maintenance

Review this list whenever a milestone is met (move it to a "completed" note and
update the `Last reviewed` date), when a new claim/feature introduces a new risk
threshold, or at each major release. Milestones are commitments — if one cannot
be met on schedule, record why and the interim mitigation.
