# Bounded selection-decision projection v1

This document defines the opt-in `#1140` projection attached to
`fused_trace.selection_decisions` and, when requested, to the context response's
`selection_decisions`. It is an observability and serving contract; it does not
change retrieval, ranking, authority, provenance, correction, delete, temporal,
or abstention semantics.

## Request boundary

The projection is requested with:

```json
{
  "mode": "fused",
  "query": "…",
  "include_selection_decisions": true
}
```

`include_selection_decisions` defaults to `false`. In `fused` mode it is
rejected for non-searchable requests by the existing recall dispatcher. In
`context` mode it adds the same projection to the context response after the
existing context selection and visibility gates. The default fused/context
requests take their existing paths and do not serialize `selection_decisions`.

The context projection is a report of the effective context candidate set. Its
source arms are `context_always_on` and `context_topical`; fused/rerank scores
are omitted because context serving does not compute them. A candidate whose
complete rendered line does not fit the existing character clamp is reported as
`dropped_budget`, and a context with no deliverable candidate records an
explicit abstention reason. This projection does not replace the existing
character-based context clamp or change which content is served.

No new MCP tool or persistence table is introduced.

## Response contract

The projection is versioned by:

```text
perseus-vault-selection-decisions/v1
```

The bounded object contains:

- `policy_digest`: SHA-256 over the canonical selection policy;
- `arms`: engaged source arms and their explicit `ok`, `empty`, `degraded`,
  `skipped`, or `unavailable` state;
- `candidate_count`: all bounded candidate records in the projection; the
  projection caps this list at 4,096, retaining final delivered candidates
  first and then the highest-priority eligible candidates deterministically;
- `eligible_count`: candidates admitted to the fused selection pool;
- `retained_count`: candidates admitted to the pre-token-budget delivery order;
- `delivered_count` and `delivered_order`: the final response order after the
  tool-layer serving gates have run;
- `token_budget`, `estimated_tokens_used`, and the declared estimator name on
  each candidate when an estimate is available;
- `candidates`: hash/identifier-only decision records; and
- `replay_fingerprint_sha256`: a digest over the complete projection.

Each candidate record can include source-arm ranks, fused/rerank/validity
components when those values exist, a token estimate, `eligible`, `selected`,
`final_rank`, and one of these closed disposition values:

```text
selected
dropped_budget
dropped_type_cap
dropped_caller_limit
dropped_coverage
filtered_lifecycle
filtered_scope
filtered_policy
abstained
unavailable
not_in_candidate_pool
```

Missing scores are omitted; they are not replaced with zero. Arm failures are
reported in `arms` and do not create fabricated candidates. If the final
served order itself exceeds the bound, the opt-in projection fails closed
rather than returning an unverifiable partial order.

## Canonicalization and replay

Candidate records and arm states are sorted before sealing. Strategy names and
policy maps are canonicalized. Candidate and delivered identifiers are unique,
and all ranks are one-based. The replay fingerprint covers candidate
membership, policy digest, arm state, ranks, available score components,
dispositions, counts, token accounting, and final order.

The fingerprint excludes raw queries, bodies, prompts, credentials, provider
payloads, and wall-clock measurement. An implicit temporal `now` is not copied
into the policy digest; if temporal ranking changes the candidate scores or
order, those observed decisions are covered. Callers that require a fixed
point-in-time temporal run should provide `query_time_unix_ms` explicitly.

Tampering with a candidate, rank, disposition, policy digest, budget, count, or
order causes validation/fingerprint verification to fail.

## Serving-stage reconciliation

The fused database path records source-arm, fusion, type-allocation,
multihop-coverage, caller-limit, and token-budget decisions. The tool layer
then reconciles the opt-in projection after the existing governed serving gates:

1. requesting-agent visibility;
2. retrieval profile and workspace scope;
3. external-reference filters;
4. startup post-ranking and caller limit;
5. temporal reconstruction, valid-time filtering, and history fallback; and
6. confirmed-query fallback/prepend.

The projection therefore reports the actual delivered order, not merely the
pre-filter fused order. Candidates removed by those gates are marked with the
first applicable lifecycle, scope, policy, or caller-limit disposition. A
history or confirmed-query item that is delivered but was not in the initial
fused pool receives an explicit fallback arm and decision record.

These gates remain authoritative. The projection cannot resurrect a
superseded, expired, quarantined, redacted, invisible, or out-of-scope entity.
It is a report of serving decisions, not an alternate retrieval path.

## Privacy and custody

`candidate_id` is an existing bounded opaque entity identifier. The projection
contains no `body_json`, body text, raw query, prompt, provider payload, secret,
or tool argument payload. Policy inputs that could contain sensitive values are
represented only inside the one-way `policy_digest`.

Resolving an identifier, if allowed, remains the responsibility of the
existing governed entity-inspection boundary. This projection is not an
authorization grant and must not be treated as evidence of model-internal
causal reasoning.

## Provider-free verification

The implementation is verified with local fixtures and no provider or paid
call. The focused acceptance command is:

```bash
cargo test --locked --no-default-features selection -- --nocapture
```

The fixture coverage includes hash-only serialization, deterministic ordering,
implicit temporal-clock stability, budget/type/caller dispositions, lifecycle
and policy reconciliation, unavailable arms, abstention, and tamper detection.
