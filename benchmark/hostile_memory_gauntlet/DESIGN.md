# Hostile Memory Gauntlet design

## Scope

The Gauntlet is a provider-neutral protocol for testing durable-memory
admission and retrieval under hostile conditions. It is adjacent to, not a
replacement for, the answer-facing LongMemEval work in #1164.

The evaluator grades:

- admission disposition and serveability;
- current versus superseded evidence identity;
- safe abstention versus wrong evidence;
- workspace scope and provenance preservation;
- deletion/tombstone behavior;
- bounded results/context;
- duplicate replay materialization.

It does not generate answers or use an LLM judge.

## Threat families

The case schema supports forged quotes, stale corrections, out-of-order writes,
same-time authoritative conflicts, low-trust recency attacks, scope leaks,
foreign-only refusal, prompt injection, duplicate/near-duplicate floods,
terminology collisions, deletion, replay, provenance loss, irrelevant
distractors, Unicode/typo queries, and bounded-context pressure.

Each record is source-bound with a record digest, source reference, scope,
actor, trust class, validity interval, and recorded time. Cases and manifests
are independently hashed. Public projections reject raw text, queries, bodies,
provider payloads, credentials, and unknown/private fields.

## Result model

A probe observes `answer`, `abstain`, `blocked`, or `error`. Missing required
evidence is a miss. Returning forbidden, stale, foreign-scope, or unsupported
evidence is wrong. The report keeps `correct_evidence_rate`,
`safe_abstention_rate`, and `wrong_evidence_rate` separate, with stale and scope
leakage reported independently.

## Provider contract

```text
reset() -> None
ingest(record) -> AdmissionReceipt
forget(scope, record_id) -> MutationReceipt
retrieve(query, scope, as_of, limit) -> RetrievalResult
```

A live provider may use a real database, but it must expose bounded cleanup and
non-sensitive execution metadata. An unavailable or malformed live capability
is an explicit blocked/error result, never a synthetic pass.

## Lifecycle

```text
private manifest + private cases -> run-return -> acceptance report
```

The runner records the manifest/case commitments and provider metadata. The
acceptance verifier recomputes case identity, metric aggregates, denominators,
public-boundary safety, and the report signature. `release_ready` requires a
complete passing run; accepted failed evidence remains useful for diagnosis but
cannot be promoted as a product gate.

## Public/private separation

This repository intentionally does not include the FP-AMB-derived runnable case
bodies or live reports. The full case bundle belongs in a private evaluation
workspace. A private holdout is required for any claim that a provider did not
fit the questions; public fixtures alone cannot establish that.
