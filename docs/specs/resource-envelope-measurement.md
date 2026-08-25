# Offline edge resource-envelope measurement v1

Status: implemented
Date: 2026-08-25
Resolves: #1105
Composes with: #870, #1008, #1103, #1104

## Purpose

Provide a reproducible measurement surface for Vault recall and context
assembly across local and constrained-edge declarations without implying a
low-SWAP result or selecting partner hardware prematurely.

## Frozen identities

A run manifest binds the benchmark and Vault revisions, seed, driver digest,
profile digest, hardware class, deployment profile, backend/runtime posture,
corpus fixture digest and counts, typed retrieval/context budget, phase,
expected outcome set, repetitions, and tolerance. IDs are bounded and reports
contain no query text, memory body, provider payload, authorization material, or
credential fields.

The profile digest covers the complete profile before measurements begin. A
result from a different machine, backend, corpus, budget, or driver is a new
run, not another repetition of the same cell.

## Collection model

The collector executes one isolated driver process per case/repetition with a
hard timeout and process-group kill. It measures:

- end-to-end wall and child CPU time;
- peak RSS and read/write bytes through `/proc` when available;
- recall candidate and selected counts;
- traversal depth, context-assembly time, and output bytes/tokens;
- measurement overhead and net wall time.

The real driver starts the checkout-built Vault binary in `--offline` mode,
uses an isolated SQLite database, writes deterministic admitted fixtures, and
calls the canonical MCP recall and context tools. The driver discards response
content after calculating bounded counters. A warm case performs one unmeasured
operation pair before measurement. A cold case measures the first operation
pair after deterministic seeding.

Process startup and seeding belong to the outer process envelope; operation
context time remains a distinct metric. This distinction prevents a fast query
from hiding expensive setup and prevents setup from being mislabeled as query
latency.

## Observation states

Case outcomes are explicit:

- `available`
- `empty`
- `partial`
- `unavailable`
- `timeout`
- `degraded`

A case freezes an allowed outcome set because backend coverage and bounded
retrieval may honestly return more than one non-success state. The synthetic
contract fixture pins one state per case so every state is regression tested.
Real manifests may allow a bounded set, but the report retains the observed
state and never upgrades it to available.

Every metric is either `available` with a finite non-negative value or
`partial`/`unavailable` with a reason. Missing metrics are not zero. The runtime
and JSON Schema reject unknown fields, malformed values, duplicate identities,
unbound dimensions, incomplete matrices, and nonzero network calls.

## Repeatability

For each case with the minimum number of available net-wall observations, the
harness reports median net wall time and relative spread:

```text
(max - min) / median
```

The manifest freezes the tolerance. A group outside tolerance stays visible as
measurement evidence; it is not rewritten or silently discarded. Aggregates
remain separated by profile, corpus, budget, phase, and outcome state.

## Optional power and energy

V1 has no power sensor adapter. `power_watts` and `energy_joules` are explicit
unavailable observations. CPU utilization, RSS, elapsed time, and bytes moved
must never be converted into power/energy values or a low-SWAP assertion.

A future sensor adapter must name its API, hardware scope, sampling period,
measurement overhead, calibration, and digest. RAPL, NVML, board telemetry, and
partner instruments are distinct evidence sources and must not be blended.

## Custody

The report binds canonical SHA-256 commitments for:

- normalized manifest;
- normalized observations;
- public sample projection;
- harness source;
- driver identity;
- full report excluding its digest field.

The validator independently reconstructs dimensions, aggregates,
repeatability, execution claims, and commitments. A caller cannot re-sign a
mutated sample while retaining stale derived sections.

## Claim boundary

This implementation establishes an offline, reproducible measurement contract
and proves one bounded real-driver smoke path against a checkout-built Vault
binary. The committed report is a synthetic contract fixture. It establishes
neither product efficacy, low-SWAP operation, partner-platform performance,
power efficiency, nor a neuromorphic architecture result.

Partner-specific thresholds and hardware profiles are additive manifests after
a target platform, workload, sensor boundary, and transition consumer are
identified.
