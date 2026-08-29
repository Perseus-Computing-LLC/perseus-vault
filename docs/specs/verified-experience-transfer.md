# Verified Experience Transfer Benchmark

Status: design specification / provider-free reference implementation
Date: 2026-08-29
Resolves: evidence-to-product phase · Consumed by: `benchmark/experience_transfer/runner.py`, adapter implementations
Related: `benchmark/experience_transfer/schemas/adapter-contract.schema.json`, `benchmark/experience_transfer/corpus/corpus.json`, `docs/research/governed-memory-under-change.md`

## Overview

The benchmark tests one proposition: a useful prior experience is not automatically a trustworthy current instruction. Agent A completes a fictional repository task, records a worked or failed approach with evidence, verifies the outcome, promotes the experience, and transfers it to Agent B. B faces current, stale, contradictory, deleted, revoked, derived, or authority-changed state.

The provider-free phase validates corpus integrity, state transitions, commitment checking, adapter contracts, leakage resistance, and deterministic policy behavior. It is a protocol/readiness artifact, not an agent-task efficacy result.

## Model

| Term | Contract meaning |
|---|---|
| Provenance | Where an experience came from: source ID, evidence ID, capture mode, scope, capture/valid time, lineage, and commitments. |
| Validity | Whether the experience is usable now: current/stale/superseded/revoked, current world hash, evidence status, and authority version. |
| Promotion | A verified A outcome becomes transferable; promotion does not make it permanently current. |
| Revalidation | A new check against the current world state; pass may reopen reuse, fail blocks reuse. |
| Abstain | Withhold an answer because evidence is missing, revoked, deleted, insufficient, or lineage is unknown. |
| Block | Withhold an action because authority, scope, contradiction, or split-brain settlement is unresolved. |

## Corpus and split

- `benchmark/experience_transfer/corpus/corpus.json` contains 24 independent A→B pairs, deterministic seed `20260829`, and ten balanced threat categories.
- Each case binds case/task IDs, prior/current world versions and hashes, event/memory IDs, source/evidence commitments, capture and valid-time fields, lifecycle transitions, authority, revalidation, hidden expected label, and leakage partition.
- The agent view excludes expected labels, gold answers, provider responses, and evaluator fields. The expected decision is retained only in the private synthetic fixture for scoring the reference contract.
- Negative controls cover failed approaches, stale state, revoked/deleted evidence, derived revoked lineage, authority changes, unresolved contradictions, split-brain outcomes, missing evidence, and failed revalidation.

## Arms and instrumentation

1. `stateless`: no-memory baseline; returns abstention and never sees experience records.
2. `ungoverned_recall`: plain recall reference; selects a worked experience without validating provenance, authority, freshness, or evidence.
3. `perseus_vault_governed`: provider-neutral governed policy over the same agent view; validates commitments, scope, authority, evidence, lineage, supersession, and revalidation.
4. `external_implementation`: contract-only adapter; no external implementation is run in this phase.

Ledger/provenance capture is an orthogonal instrumentation overlay. It is not a semantic arm and is not model-visible in this phase. If a future overlay changes context bytes, it becomes a separate named arm.

## Metrics frozen before execution

- `correct_reuse_rate`: reuse on expected safe-reuse cases.
- `stale_memory_rejection_rate`: non-reuse on stale or failed-revalidation cases.
- `failed_approach_avoidance`: non-reuse of transferred failed approaches.
- `contradiction_supersession_correctness`: expected decision on contradiction/supersession cases.
- `evidence_provenance_completeness`: outputs that validated evidence commitments.
- `unauthorized_action_or_unsafe_reuse_rate`: unsafe reuse flags; lower is better.
- `abstention_risk_coverage`: abstain/reject/block on harmful-risk cases.
- `revalidation_rate`: required revalidation checks actually performed.
- `invalidation_revalidation_latency`: planned provider-backed runtime metric; not measured here.
- `context_tokens` and `provider_cost`: planned provider-backed metrics; not measured here.

All metrics retain numerator, denominator, polarity, status, and scope. A zero denominator is `not_measured`, never a passing zero.

## Fail-closed rules

Reject duplicate IDs, malformed transitions, unauthorized accepted transitions, invalid commitments, stale/revoked evidence reused as current, unknown derived lineage, scope violations, missing evidence, public forbidden fields, non-finite numbers, nondeterministic regeneration, and any adapter output outside the contract. Preserve `not_measured` for unexecuted provider/external arms.

## Implementation slice and acceptance criteria

- Generator produces byte-identical corpus output for the fixed seed and validates every commitment.
- Runner executes the three local adapters over all 24 views, writes a public-safe report, and records zero provider/judge/network calls.
- Public report contains IDs, decisions, counters, statuses, and hashes only; it excludes prompts, context bodies, memory bodies, provider responses, secrets, and customer data.
- Acceptance envelope binds corpus, manifest, report bytes, and report signature; it names agent efficacy as `not_measured`.
- A future provider-backed run must use the same corpus and adapter contract, add real Agent A/B execution, retain provider-reported usage, and keep any external adapter result separately attributable.
