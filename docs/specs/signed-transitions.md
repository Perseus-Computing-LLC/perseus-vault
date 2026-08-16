# Signed Transitions — Cryptographically Authorized Mutation (#1080)

Status: implemented · Source: MutMem (arXiv:2608.02843)

## Problem

Mutable retrieval weights create an attribution problem: reviewers cannot
distinguish authorized adaptation from database tampering. Before this
change, supersession and tombstones governed entity history, but
retrieval-relevant mutations (scores, layers, poison labels) were not
epoch-bound signed transitions.

## Protocol

Every nontrivial retrieval-relevant state change commits as a signed
transition binding:

1. a terminal provenance node (the mutated entity id),
2. a signer epoch (key-generation era from `signer_epochs`),
3. quantized old → new values (4-decimal rounding, sorted keys),
4. a no-fork predecessor (the chain hash of the previous record),
5. two domain-separated SHA-256 commitments — one per value side,
6. an Ed25519 signature over the canonical payload,
7. a chain hash binding payload + commitments + signature.

Domain separators: `perseus-vault/transition/v1|old-value`,
`perseus-vault/transition/v1|new-value`, `perseus-vault/transition/v1|chain`.

The protocol provides evidence of integrity, authorization, traceability,
and historical continuity — it does not establish content truth (MutMem's
explicit scope).

## Coverage (v1)

- `perseus_vault_score` — explicit score/importance mutation.
- `perseus_vault_promote` / `perseus_vault_demote` — ladder identity changes
  (new entity supersedes the source; transition terminal = new entity).
- `perseus_vault_poison_label` — signed, revisable poison labels.

Backward compatibility: with no signer epoch registered, mutations proceed
in the unsigned regime and record no transition (the pre-#1080 behavior).
Once an epoch is registered via `perseus_vault_signer_epoch_set` (Ops), the
signed regime is active: transitions commit with every covered mutation and
a recording failure surfaces as a loud `signed:false` warning in the
response. Poison labels are fail-closed from the start: they require a
registered epoch.

## Poison labels as trust evidence

Poison-likely content is retained (never silently deleted). At ranking time
an entity's effective score is multiplied by (1 − penalty):

| level           | penalty |
|-----------------|---------|
| `poison_likely` | 0.9     |
| `suspect`       | 0.5     |
| `clean`         | 0.0     |

v1 applies the penalty in the FTS content-witness re-sort and the
provenance-trust sort. Labels are revisable: a later signed transition can
set `clean`.

## Verification

- Writer-side: `Database::record_signed_transition` signs with the
  registered epoch key; chain integrity is established by replay.
- `perseus_vault_transition_audit` (Ops): replays every record — epoch key
  lookup, both commitments, signature, predecessor linkage, chain-hash
  reproduction — and names the first divergence.
- Portable verifier: `perseus-vault verify-transition --json <record>
  --epoch-key-b64 <key>` — the same pure function with no database access.
- Storage-level no-fork: the UNIQUE index on `predecessor_hash` makes forks
  unrepresentable (at most one successor per predecessor; one genesis).

## Key management

`signer_epochs` stores {epoch, public key, fingerprint, seed}. The seed is
stored at rest alongside the database — the same trust-domain posture as the
AES at-rest key file — so the writer can sign in-process. Epochs are
additive: old epochs' records stay verifiable after rotation. The seed is
never echoed in responses or logs.

## Success criteria vs implementation

- Suite classes (authorization, topology, tamper, signer-epoch,
  post-mutation-recall): covered by `src/signed_transition.rs` tests —
  wrong-key, unsigned, tampered-value, tampered-terminal, fork/genesis
  rejection, epoch registration, portable-reproduces-writer.
- Poison exclusion: N=100 PoisonedRAG-style adaptation test
  (`signed_poison_labels_exclude_injected_content_from_top_k`) — 100/100
  injected rows excluded from top-5 with labels active (and dominating
  top-5 without labels).
- Transition latency: one Ed25519 sign + one INSERT per mutation (O(1),
  µs–ms class); no timing assertion in CI (environment-dependent).
- Portable verifier reproduces writer-side verification: asserted by test.

## Schema

v55: `signer_epochs`, `signed_transitions`, `poison_labels`.
