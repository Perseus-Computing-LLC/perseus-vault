# Evidence Chains and Continuous Attestation for Durable Memory

> **Vault remembers — and it records *what it remembers, from where, and with what
> authority*.** This document adds evidence-chain and continuous-attestation guidance on
> top of the existing deterministic recall and provenance contracts.

Related: [Deterministic Recall and Provenance](deterministic-recall-and-provenance.md) ·
[Retention](retention.md) · [Memory Quality Telemetry](memory-quality-telemetry.md)

## 1. Why evidence chains

A memory entry is a **claim**, not a fact. To be actionable under evidence-bounded control
(the Perseus research thesis, NSF 26-510), every served memory should be traceable to the
evidence that supports it: when it was written, from which source, under which authority,
and whether it has since been superseded or contradicted. Research across the 2026
literature converges on this model (Protocol-Driven Development arXiv:2605.12981;
Temporal Validity in Retrieval Memory arXiv:2606.26511; Portable Agent Memory
arXiv:2605.11032).

## 2. Write-time provenance tags

The strongest defense against memory poisoning is provenance binding **at write time**,
before the entry is committed (SMSR, arXiv:2606.12703: HMAC-SHA256 tags at the memory-write
boundary with key separation from storage writers; a provenance-free filter cannot certify
defense against runtime memory poisoning).

Guidance for memory-write paths:

- Every write carries a provenance tag binding: source digest, writer identity/authority,
  valid-time interval, and recorded-at timestamp.
- The tagging key is separated from the storage writer (signing oracle or HSM where
  deployed), so a compromised storage path cannot forge fresh provenance.
- Keys can be rotated and revoked independently of data.
- Write-time quarantine: entries whose provenance cannot be established are quarantined,
  not silently committed (MEMSAD, arXiv:2605.03482).

## 3. Evidence-chain semantics for entries

Treat admission (write/recall) and continuous attestation (later verification,
correction, supersession) as distinct evidence stages:

| Stage | What is recorded | Vault surface |
|---|---|---|
| **Admission** | Signed write record: source digest, authority, valid-time, recorded-at | `remember`/capture provenance fields |
| **Served evidence** | What was recalled, from which entity, with which selection rationale | recall receipts / retrieval receipts |
| **Correction** | A superseding write that invalidates or refines an earlier entry | supersession links (bi-temporal history) |
| **Contradiction** | Conflicting evidence detected across entries | conflict detection records |
| **Attestation** | Periodic re-verification that an entry remains current and in-scope | hygiene/decay + quality telemetry |

The durable system-of-record position (2026-08-02 synthesis) holds: external auditable
memory is the system of record; valid-time and transaction-time facts are combined with
explicit supersession/contradiction/provenance links; retrieval receipts explain selection
rationale. Native model memory is never substituted for auditable storage.

## 4. Retrieval receipts reference the evidence chain

A retrieval receipt for a served memory should reference the entry's evidence chain, not
just its content hash. When Perseus serves `@memory` context, the receipt answers:

- Which entity was served, by which authority, at which valid-time?
- What was the selection rationale (why this entry, not its superseded predecessor)?
- Is the entry current, superseded, or in conflict at serve time?

This is the Vault side of the cross-product context-to-receipt contract exercised by the
flywheel harness.

## 5. Hash-chained transition records

Where deployment allows, record memory lifecycle transitions as a compact hash chain
(Immutable Memory Systems, arXiv:2506.13246 — Merkle automaton; corrections as
supersession/refinement edges). The event tuple binds predecessor state, input, output,
policy/version, actor, time, and delegation. This makes the memory lifecycle
independently auditable rather than relying on in-process state.

## 6. What this document does *not* claim

- Provenance tags make poisoning *detectable and attributable*; they are not a
  content-level safety guarantee. Detection requires verification at recall time plus
  anomaly checks (MEMSAD-style cosine anomaly at ingestion; randomized ablation).
- "Replayable" is not "reproducible in the world": recall receipts prove what was served
  under captured inputs, not that the external world still matches.
- This is research-evidence guidance, not an engineering guarantee.

## References

- Protocol-Driven Development (Dynamic Evidence Ledger; remediation) — arXiv:2605.12981
- SMSR: Certified Defense Against Runtime Memory Poisoning — arXiv:2606.12703
- MEMSAD: Gradient-Coupled Anomaly Detection for Memory Poisoning — arXiv:2605.03482
- The Misattribution Gap (poisoning vs model failure) — arXiv:2605.22842
- Portable Agent Memory (Merkle-DAG provenance, signed roots) — arXiv:2605.11032
- Eywa: Provenance-Grounded Long-Term Memory — arXiv:2605.30771
- Immutable Memory Systems (Merkle automaton) — arXiv:2506.13246
- Temporal Validity in Retrieval Memory — arXiv:2606.26511
