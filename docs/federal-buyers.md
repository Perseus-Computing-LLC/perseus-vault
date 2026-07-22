# Perseus & Perseus Vault — AI Infrastructure for Government

## The Stack

| Component | What it does | License | Language |
|---|---|---|---|
| **Perseus** | Live context engine — grounds AI agents with verifiable state before every turn | MIT | Python 3.10+ |
| **Perseus Vault** | Persistent memory — encrypted, auditable, FTS5-searchable knowledge store for AI agents | MIT | Rust |

Both are open source, production-deployed, and maintained by Perseus Computing LLC (US-owned).

---

## Why Government Buyers Choose Open Source

- **No vendor lock-in.** MIT license means your agency owns the deployment forever. Switch integrators without switching tools.
- **Supply chain transparency.** Full SBOMs published. Every dependency auditable.
- **Air-gap ready.** Zero cloud dependencies. Deploy in SCIFs and classified environments.
- **Audit native.** Every AI decision traceable to source. Chain-of-custody journal with cryptographic verification.

---

## Compliance at a Glance

| Requirement | Status |
|---|---|
| SBOM (NTIA Minimum Elements) | ✅ Published for both repos |
| License (MIT) | ✅ No copyleft, no GPL/AGPL |
| Encryption at rest | ✅ AES-256-GCM (Perseus Vault) |
| NIST AI RMF alignment | 🟡 In progress |
| FedRAMP path | 🟡 Gap analysis phase |
| Section 508 / accessibility | 🟡 Audit planned |
| Supply chain (SLSA) | 🟡 Attestation in development |

---

## Measured Performance

LOCOMO long-term conversational-memory benchmark — 1,540 questions (categories 1–4), top-200 retrieval, gpt-5 answerer + judge — executed on mem0's own benchmark harness via our public fork, 2026-07-22:

| Memory engine | Overall | Single | Temporal | Multi | Open-domain |
|---|---|---|---|---|---|
| **Perseus Vault 2.20.2** | **87.9%** | 89.1 | 92.2 | 85.1 | 70.8 |
| Mem0 Platform Starter | 82.2% | 85.0 | 82.9 | 78.0 | 67.7 |
| Zep Cloud Flex | 33.8% | 36.9 | 6.9 | 50.0 | 49.0 |

- **Air-gap relevance:** Perseus Vault is the only engine in this comparison that runs fully local (single binary, no cloud service) with instant time-to-searchable; both competitors are hosted cloud offerings (Mem0 Platform ~11–45s, Zep Cloud ~222s per session) that cannot operate in disconnected environments.
- **Adversarial robustness:** On LOCOMO category 5 (adversarial, 446 questions) — the benchmark's robustness category for misleading evidence — Perseus Vault leads at 63.5% vs Mem0 55.6% and Zep 49.8%.

Source and full per-question results: [Perseus-Computing-LLC/memory-benchmarks](https://github.com/Perseus-Computing-LLC/memory-benchmarks) (fork of `mem0ai/memory-benchmarks`). Disclosure: our Mem0 measurement is 9.4 points below Mem0's own published file, attributed to judge/platform drift.

---

## Security

- **Perseus Vault:** AES-256-GCM encryption for all stored entities. Encryption keys never leave the deployment boundary.
- **Perseus:** Context injection is read-only — Perseus never writes to your systems. It renders, injects, and exits.
- **Both:** No telemetry. No phoning home. No usage tracking. Network calls are strictly opt-in (MCP servers you configure).

---

## Deployment Models

### Air-Gapped / Classified
Single-container deployment. All dependencies bundled. No internet required. Suitable for DoD IL5+, IC Directive 503 environments.

### On-Premises
Deploy on agency infrastructure. Full data sovereignty. Integrate with existing identity providers.

### Cloud (Coming)
AWS GovCloud, Azure Government, GCP Assured Workloads. (Roadmap item — contact us for timeline.)

---

## SBIR / RFP Alignment

Perseus and Perseus Vault address multiple government AI priorities:

| Priority Area | How We Address It |
|---|---|
| AI Interpretability | Perseus traces every context injection to source (file, line, timestamp) |
| AI Control | Live context grounding prevents hallucination — agent decisions are anchored to verifiable state |
| Adversarial Robustness | Perseus Vault's cryptographic journal detects tampering. Perseus's context chain is immutable |
| Audit & Compliance | PROV-O provenance exports. Immutable journal with SHA-256 chain-of-custody |
| Knowledge Management | Perseus Vault's FTS5 search + entity graph for cross-session institutional knowledge |

---

## Active Federal Engagements

- **DARPA AI Forge RFI** (DARPA-SN-26-80) — Response submitted June 2026. University partnerships in progress.
- **DoD SBIR/STTR monitoring** — Active pipeline for AI/autonomy topics
- **NSF SBIR** — Targeting "Knowledge and Data Management Technologies" sub-topic

---

## Procurement Information

| Field | Value |
|---|---|
| Entity | Perseus Computing LLC |
| UEI | [Pending SAM.gov registration] |
| CAGE Code | [Pending] |
| NAICS Codes | 541715 (Primary), 541511, 541512 |
| SBIR Registry | [Pending] |
| Website | https://perseus.observer |
| Contact | perseus@perseus.observer |
| GitHub | https://github.com/Perseus-Computing-LLC |

---

## Get Started

**Evaluate in 5 minutes:**

```bash
# Perseus — context engine
pip install perseus-ctx
perseus --help

# Perseus Vault — persistent memory (MCP server)
# Download binary from https://github.com/Perseus-Computing-LLC/perseus-vault/releases
./mimir --help
```

**For procurement inquiries, security assessments, or ATO support:**
Email perseus@perseus.observer.

---

*Perseus Computing LLC is a US-owned small business. All software is MIT-licensed open source. No proprietary dependencies. No vendor lock-in. No telemetry.*
