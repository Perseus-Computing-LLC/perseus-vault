# Governed Memory Under Change: Provenance, Authority, and Abstention for AI Agents

Status: research/report scaffold; provider-free benchmark protocol prepared, not executed
Date: 2026-08-29

## Abstract

**Question.** Can provenance- and authority-aware memory reduce harmful reuse under
contradiction, staleness, deletion, and state change while preserving useful experience
transfer?

**Current answer.** Not established. Perseus has existing company-run evidence for
LongMemEval QA and bounded self-run evidence for conflict/abstention behavior, but no
cross-implementation experience-transfer run has been executed. This scaffold defines
the experiment needed to test the proposition without treating a protocol artifact as
a product result.

## 1. Problem and product boundary

Agents routinely receive a prior “successful” approach and treat it as a current
instruction. That shortcut is unsafe when the repository changed, an evidence source
was revoked, an authority was rotated, or two agents produced incompatible outcomes.
The product boundary is governed memory/context control: admit, select, verify,
revalidate, supersede, reject, abstain, or block. It is not a claim that Perseus is the
best general-purpose memory system, an autonomous authority, or a replacement for
operator review.

The target lifecycle is:

```text
Agent A completes task
  → captures worked or failed approach + evidence
  → verifies outcome and promotes experience
  → transfers it to Agent B
  → B observes current or stale world state
  → system revalidates, reuses, rejects, abstains, or blocks
```

The benchmark separates storage/retrieval, provenance, validity, authority, and answer
quality. A ledger/receipt overlay is orthogonal unless it changes model-visible bytes.

## 2. Provenance is not validity

**Provenance** answers “where did this experience come from?” It binds source and
evidence identifiers, content commitments, capture mode, scope, capture time,
valid-time, transformations, and derivation lineage. A valid hash proves that a
projection matches its committed bytes; it does not prove that the source was true or
that a later decision remains authorized.

**Validity** answers “may this experience be used now?” It depends on current world
state, evidence status, supersession/correction, deletion or revocation, authority
version, contradiction settlement, and revalidation. A historically authentic record
can be invalid for a current action. A source can be well-provenanced but stale.

The implementation must retain historical provenance while refusing to treat historical
or derived material as a current instruction without the applicable validity checks.

## 3. Threat and failure taxonomy

| Failure class | Example | Required safe behavior |
|---|---|---|
| Failed approach | A’s port workaround caused a rollback loop | Transfer the failure lesson; reject reuse as a recipe |
| Stale world | Repository version changed after A’s verified run | Reject by default; revalidate before reuse |
| Contradiction | Two current facts disagree | Select an explicit supersession or block unresolved conflict |
| Deletion/revocation | Evidence attachment or source was withdrawn | Abstain; do not resurrect through summaries, vectors, or exports |
| Derived contamination | A summary outlives its revoked parent | Reject contaminated lineage; identify parent commitment |
| Authority change | The capture authority rotated or was revoked | Block action until current authority is established |
| Split brain | Two valid children follow one parent | Preserve both; block external action until settlement |
| Missing evidence | A memory claims success without a usable source | Abstain; never infer evidence from the claim itself |
| Leakage | B receives A’s hidden label or another workspace’s state | Fail the corpus/run before scoring |
| Transport/integrity failure | Hash or state transition does not verify | Fail closed; do not convert failure into abstention/pass |

## 4. Existing Perseus evidence

### 4.1 LongMemEval frozen-default official-CoT series — observed/derived

The accepted public series report is the locally frozen LongMemEval artifact recorded by the evidence-freeze package; the raw series payload is not copied into this benchmark package.
Its declared content commitment is
`ae6547565cafa8eeb0b7750380ea17b89e5b1d378a757a841b9077f393958955`.
The protocol uses the 500-question LongMemEval-S split, official-CoT prompt,
temperature 0, `gpt-4o-2024-08-06` answerer and official per-type judge, benchmark-
shaped unique-key-per-session ingest, hybrid retrieval `k=10`, bundled ONNX
embeddings, and full context assembly.

| Arm | Run 1 | Run 2 | Run 3 | Equal-weight/pooled |
|---|---:|---:|---:|---:|
| Stateless | 45/500 (9.0%) | 47/500 (9.4%) | 45/500 (9.0%) | 137/1,500 (9.1%) |
| Full context | 334/500 (66.8%) | 333/500 (66.6%) | 337/500 (67.4%) | 1,004/1,500 (66.9%) |
| Perseus Vault | 401/500 (80.2%) | 403/500 (80.6%) | 409/500 (81.8%) | 1,213/1,500 (80.9%) |
| Gold-session oracle | 451/500 (90.2%) | 458/500 (91.6%) | 453/500 (90.6%) | 1,362/1,500 (90.8%) |

**Custody observation.** The local freeze independently recomputed all three report
signatures and source hashes, confirmed 500 attempted/graded cells per arm, zero final
answer/judge errors, and complete nonempty hypothesis cardinality. Runs 2 and 3 have
standalone acceptance reports. Run 1 has a hash-bound report, resume return, handoff,
and series inclusion, but no standalone `acceptance_report.json` was found in the
local Run 1 directory; that distinction remains part of the package boundary.

**Interpretation.** This is evidence of one company-run benchmark protocol and model
snapshot. It is not independent external validation, customer or production efficacy,
universal superiority, or a proof of safe authority-aware reuse.

### 4.2 Evidence-structured confirmation — observed/bounded

A separate internal paired confirmation measured 410/500 (82.0%) for an evidence-
structured candidate versus 416/500 (83.2%) for a matched full-context control. The
preregistered promotion rule failed. The result remains separate from the frozen-
default series and is not a superiority, independent-holdout, customer, deployment,
or production claim. It must not be averaged with 80.9%.

### 4.3 MemConflict — observed/bounded, different property

The inspected hashable public body records a self-run replication with macro score
0.555, 18 wrong answers, and 1,378 blank answers across 3,750 questions. It is useful
bounded evidence about planted conflict handling and abstention trade-offs. It measures
a different property from LongMemEval end-to-end QA and uses a different protocol,
models, dataset, and rubric. It is not independent validation of LongMemEval or proof
of universal memory superiority.

The requested “zero incorrect answers under the corrected self-run rubric” statement
is **not established** by the local artifact inspected here. The current public body
explicitly contains the 18-wrong result. A separate corrected-rubric artifact would
need its own scope, denominator, and hash before that wording could be considered.

## 5. Verified experience-transfer protocol — planned/not executed

The standalone package at `benchmark/experience_transfer/` contains an
original deterministic 24-pair fictional corpus. Every pair binds task IDs, world
versions and hashes, memory/event IDs, source/evidence commitments, capture and valid-
time fields, status, authority version, revalidation state, lifecycle transitions,
negative-control metadata, and a deterministic seed. The private synthetic fixture
keeps expected labels separate from the agent view; the public report projects only
IDs, decisions, counters, statuses, and commitments.

The four named adapter arms are:

1. stateless/no-memory baseline;
2. plain/ungoverned recall reference;
3. governed Perseus Vault policy adapter;
4. optional external implementation adapter, specified but not executed.

The provider-free runner validates contracts and deterministic policy behavior. It does
**not** execute Agent A or Agent B tasks, call an LLM/judge/embedding service, or
measure agent-task efficacy. A future provider-backed run must retain the same corpus,
freeze answerer/judge/prompt/token/cost controls, and report every unexecuted arm as
`not_measured`.

### Planned primary metrics

- correct reuse rate;
- stale-memory rejection rate;
- failed-approach avoidance;
- contradiction/supersession correctness;
- evidence/provenance completeness;
- unauthorized-action or unsafe-reuse rate;
- abstention risk-coverage;
- revalidation rate;
- invalidation/revalidation latency;
- context tokens and provider cost.

No provider-backed latency, token, cost, or answer-quality number appears in the
provider-free report. Zero provider calls is an execution boundary, not a quality
score.

## 6. Expected falsifiers

The proposition is falsified or materially weakened if a clean, matched run shows any
of the following:

- governed controls do not reduce unsafe reuse of stale, revoked, contaminated, or
  unauthorized experiences relative to ungoverned recall;
- governed abstention/block behavior is not risk-covering, or it hides failures as
  empty success;
- provenance fields are present but do not identify the source/derivation needed to
  explain a rejection;
- revalidation passes on stale state without binding to the current world hash;
- failed approaches are silently promoted as worked recipes;
- contradiction/supersession outcomes are overwritten, dropped, or nondeterministic;
- useful reuse collapses materially on clean current cases under an equal task budget;
- a provider-backed comparison changes model-visible context between semantic arms;
- an external implementation cannot consume the shared corpus without private-data or
  protocol exceptions.

A perfect synthetic reference result would not falsify these concerns; it would only
show that the fixture/controller contract is internally consistent.

## 7. Observed evidence vs proposed experiments

| Surface | Observed evidence | Proposed measurement | Status |
|---|---|---|---|
| Long-horizon QA | Three accepted frozen-default runs; 80.9% Vault pooled | Not a substitute for transfer benchmark | observed/accepted bounded |
| Conflict/abstention | MemConflict self-run; 18 wrong, 1,378 blank | Reuse/reject/abstain/block under explicit state transitions | observed vs planned |
| Provenance | Vault/Ledger/context contracts and hash-bound artifacts | Evidence completeness and lineage recovery in 24 pairs | product seam observed; measurement planned |
| Authority | Existing authority/action-control-plane surfaces | Rotated/revoked authority cases and unsafe-action rate | protocol planned |
| Staleness | LongMemEval temporal categories and Vault time-travel fixtures | Current/stale world revalidation cases | related evidence; transfer measurement planned |
| Deletion | Existing logical forget/tombstone surfaces | Revoked/deleted source plus derivative-carrier sweep | product capability surface; benchmark planned |
| Agent efficacy | No new provider-free efficacy result | Agent A/B real task execution with fixed provider/judge | not measured |
| External comparison | CogniCore invitation only; no cross-run | Same-corpus independently implemented adapter | not measured |
| Production/customer outcome | None in this package | Separate deployment/customer study | not established |

## 8. Limitations and negative claims

- The new corpus is synthetic and fictional; it does not establish performance on
  customer repositories or operational data.
- The deterministic adapters are reference policies, not trained agents and not
  general memory-system implementations.
- The corpus can demonstrate contract behavior but cannot measure model reasoning,
  answer quality, token economics, or human workflow value without a later authorized
  agent phase.
- The LongMemEval series is company-run on one dataset, protocol, and model snapshot;
  its hashes establish artifact identity, not external truth.
- MemConflict is self-run and measures conflict/abstention behavior; it is not an
  independent validation lane and must not be merged with LongMemEval.
- The evidence-structured 82.0% result is a separate experimental result with a failed
  promotion rule.
- No universal superiority, customer efficacy, production efficacy, security
  guarantee, deletion guarantee, authorization guarantee, or mission outcome is
  established here.
- A hash-only receipt is inspectable provenance, not a source-truth oracle or an
  authorization grant.

## 9. Next measurement gate

The smallest meaningful next experiment is one provider-backed, matched pilot over
the frozen 24-pair corpus, with one approved answer model and no external adapter.
Before execution, freeze the provider/model, prompts, completion cap, task budget,
judge, retry policy, context exposure, usage telemetry, and cost ceiling; obtain the
separate authority/intent/lease; and preserve raw inputs privately. The pilot must be
reported as calibration, not efficacy, unless its preregistered denominator and
acceptance envelope support a stronger claim.

Until that gate is separately authorized, the package is ready for provider-free
review only.
