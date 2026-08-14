# Authority trace suite (`benchmark/security/`)

Deterministic gold traces for authority and trust semantics. The suite exists
because memory systems are easy to demo and hard to compare: a benchmark that
only asks "can the system retrieve X?" misses the harder question — *should it
still trust X, and can it prove why?*

The design follows the trace shape contributed by Steve_Was_Here26 in
r/OpenSourceAI (2026-08): deterministic gold event traces with expected
accept/reject/failed-to-confirm outcomes, published so other implementations can
run identical cases.

## Outcome taxonomy

Never a scalar confidence score. Each trace declares one expected decision:

| Decision           | Meaning                                                                    |
|--------------------|----------------------------------------------------------------------------|
| `accept`           | the attempt is authorized and executed                                    |
| `reject`           | the attempt is refused; the controlling reason is named                   |
| `failed_to_confirm`| no authoritative postcondition readback exists; result is unconfirmed     |
| `blocked`          | provisional/deferred; external action gated until an authorized settlement|

## Trace fields

Every event in a trace carries: event ID, observed/effective time (unix ms),
actor/key ID, authority scope, operation, target, payload digest (sha256 of a
fictional canary), provenance, `supersedes`, expiry, and evidence. Each trace
ends with: expected decision + reason, expected receipt, expected authoritative
postcondition readback, and negative assertions.

Negative assertions are first-class checks, not afterthoughts:

- `no_external_action` — the receipt proves no external side effect occurred;
- `no_message_sent` — no outbound message was emitted;
- `no_deleted_memory_resurrection` — a deleted memory never reappears;
- `no_derived_resurrection` — no permission is reconstructed from any derived
  carrier (summary, vector index, cache, export);
- `no_identity_merge` — distinct identities are never merged because their
  display names match;
- `no_replay_authorization` — a replayed/out-of-order event never re-authorizes;
- `no_silent_winner` — in split-brain, neither child silently wins;
- `no_accept_without_readback` — a success toast without authoritative
  postcondition readback is `failed_to_confirm`, not `accept`;
- `no_post_rotation_authority` — a pre-rotation signature stays historically
  authentic but never conveys post-rotation action authority;
- `no_compensation_effect` — a rejected compensation attempt creates no
  effect record;
- `no_inherited_authority` — compensation authority is evaluated at
  compensation time, never inherited from the original action;
- `no_third_party_compensation` — without a receipted remediation handoff, an
  unrelated current grant cannot remediate a revoked authority's effect;
- `no_self_claimed_undo` — "I'm just undoing my earlier action" without a
  verified finding+head linkage is rejected fail-closed.

## Corpus

`traces/authority_traces.json` is generated deterministically by
`traces/generate_authority_traces.py` (fixed inputs, real sha256 payload
digests). Regenerate and re-verify after any edit:

```bash
python3 benchmark/security/traces/generate_authority_traces.py
python3 -c "import json;d=json.load(open('benchmark/security/traces/authority_traces.json'));print(d['trace_count'], 'traces')"
```

Trace inventory:

1. `authority-canonical-revoke` — grant X, revoke X, new session attempts X →
   reject, revocation controlling, grant superseded, receipt shows no external
   action.
2. `authority-revoke-different-actor` — revocation from a different actor →
   reject (authority mismatch, fail closed).
3. `authority-replay-grant-after-revoke` — out-of-order delivery / replay of the
   grant after the revocation → reject (replay does not re-authorize).
4. `authority-expired-grant` — expiry on the grant → reject (expiry controlling).
5. `authority-revoked-memory-deleted` — deletion of the revoked memory →
   reject, no resurrection.
6. `authority-success-toast-no-readback` — claimed success toast without
   authoritative postcondition readback → `failed_to_confirm`, not accept.
7. `authority-split-brain-continuation` — two sessions from one accepted parent,
   each producing a valid authorized child → `blocked`: both preserved, instance
   marked split-brain/provisional, external actions gated until an authorized
   settlement cites both parents.
8. `authority-derived-revocation` — raw grant revoked/deleted while a summary,
   vector index, cache, or export still encodes it → reject; derivative lineage
   identified; no resurrection across any carrier.
9. `authority-key-rotation` — old signature remains historically authentic but
   no longer conveys current action authority after the rotation's effective
   point → reject current, retain historical authenticity.
10. `authority-identity-collision` — display-name collision → reject; no
    identity merge; fail closed under ambiguity.
11. `residue-undeclared-snapshot-orphan` — an embedding snapshot row outliving
    its source → the independent sweep observes it as undeclared residual and
    any purge refuses to complete (hard gate).
12. `residue-compliant-purge-partition` — a compliant purge removes projections
    with their source (or declares them), keeps journal rows redacted as
    declared residual, and the sweep observes zero undeclared residue.
13. `residue-deletion-dominates-correction` — superseded versions retained for
    reconstructability are purged with their source; deletion dominates
    correction.
14. `remediation-revocation-barrier` — a revoked principal attempts to
    compensate its own past effect → reject on CURRENT action authority
    (compensation authority is never inherited); the original receipt still
    verifies historically.
15. `remediation-handoff-transfer` — a receipted "revoke A, hand open cases to
    B" remediation handoff transfers remediation authority; B's compensation
    citing E + finding + head + the handoff receipt → accept, with the
    handoff receipt recorded in B's evidence set.
16. `remediation-no-authority-fail-closed` — no valid authority exists
    (revoked principal + third party without a handoff) → compensation is not
    executable, the finding waits in review, no effect record is created.
17. `remediation-self-claimed-undo` — the AAR linkage negatives: missing
    finding linkage, forged finding reference, and mismatched superseding
    head are each rejected with a stable reason code (detection produces a
    finding, never a decision).

## Execution note

The corpus is the spec and the shared-publishable artifact. The deletion-
residue and remediation trace families are wired against the live binary in
the Rust suite (`deletion_residue_trace_corpus_runner` and
`remediation_trace_corpus_runner` in `src/db.rs`): each event replays through
the public surfaces (remember / purge / authority_set / action_intent /
action_complete / authority_revoke / finding_record) and the model outcome is
asserted against the corpus contract. The remaining families (canonical
revocation, split-brain, key rotation, identity collision) follow the
`benchmark/package` adapter contract; until their runner exists, each trace's
`expected_decision` is the documented contract and the suite reports
`not_measured` rather than a fabricated pass.
