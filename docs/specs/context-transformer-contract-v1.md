# Context-transformer contract v1 (#1106)

`perseus-vault-context-transformer/v1` is a fail-closed boundary between
Vault-governed context assembly and a provider adapter or context transformer.
It is a serving and audit contract, not a compression backend. A transformer
may propose a provider-shaped request, but the adapter must not admit the
proposal merely because the provider wire shape remains valid.

The Rust implementation is in [`src/context_transform.rs`](../../src/context_transform.rs).
The deterministic synthetic corpus is
[`tests/fixtures/context_transformer_v1.json`](../../tests/fixtures/context_transformer_v1.json),
with boundary tests in
[`tests/context_transformer_contract.rs`](../../tests/context_transformer_contract.rs).

## Contract record

A `ContextTransformRequest` carries transient input messages and the
configuration commitment for one serving attempt:

- `provider` and `request_format` identify the provider/request shape;
- `transformer.name` and `transformer.version` identify the adapter;
- `stages` records enabled stages, stage versions, trust (`trusted` or
  `untrusted`), and an optional SHA-256 configuration commitment;
- `lossiness_policy` is one of `passthrough`, `reversible`, or
  `lossy_opt_in`;
- `input_messages` preserves stable message identity, assembly order, an
  explicit content class, and the transient provider-shaped message;
- `input_tokens` is optional, but the receipt exposes `known`, `partial`, or
  `missing` rather than treating absent counts as zero;
- `original_retained`, `original_ref`, and `replay_envelope_ref` state whether
  the original can be recovered. A retained original without a bounded
  reference is invalid.

The durable `ContextTransformReceipt` contains no provider message bodies. It
commits to:

- exact ordered `input_digest` and accepted `output_digest`;
- `candidate_output_digest` when a different proposal was rejected or fell
  back to the original;
- the complete stage configuration and lossiness policy;
- token counts and explicit `outcome`;
- changed content classes and bounded changed-span metadata (identity, class,
  character counts, and a reason, never the changed text);
- original retention and bounded original locator;
- a hash-only replay plan containing membership/order, per-message content
  digests, output disposition, a replay-envelope reference, and a replay
  fingerprint;
- independent provider-shape results, including whether tool-call/result
  pairing was checked;
- a sealed receipt digest.

The receipt is canonicalized before hashing. A mutated digest, stage setting,
replay membership, count state, or outcome/lossiness combination fails
validation.

## Admission policy

| Situation | Contract decision |
| --- | --- |
| No change, unknown request format, unknown content, disabled stage, or explicit passthrough policy | `passthrough`; the original is the accepted output |
| Trusted stage changes recoverable content with original and replay references | `transformed`, `actual_lossiness: reversible` |
| Trusted stage changes content under explicit `lossy_opt_in` without recovery | `degraded`, `actual_lossiness: lossy`; the receipt must not claim recovery |
| Untrusted stage changes `system`, policy/Keystone, authority, or user-constraint content | `rejected`; the original is sent instead |
| Protected content changes without recovery, regardless of lossy opt-in | `rejected` |
| Reversible policy lacks both an original locator and replay-envelope locator | `rejected` |
| Proposed OpenAI message shape or tool pairing is invalid | `rejected`; shape failure is reported separately from content recall |
| Transformer/backend cannot be used | `unavailable`; never represented as a successful empty context |

Tool-result compression is therefore reversible by default. It becomes
lossy only when the caller explicitly selects `lossy_opt_in`, and the result
is marked `degraded`. Pair-safety is checked independently: a valid
`tool_use`/`tool_result` pairing does not prove that a critical record or
source-code tail survived.

Unknown classes and unsupported provider formats default to the original
context. The contract does not guess whether an unfamiliar object is safe to
rewrite.

## Replay and original recovery

`ReplayPlan` contains only stable source IDs, input/output order, content
commitments, disposition (`retained`, `transformed`, `reordered`, or
`omitted`), and bounded references. `replay_membership` verifies the separately
retrieved original against those commitments and returns source IDs in the
admitted order plus omitted IDs. It does not fetch or return bodies. The caller
resolves `original_ref` through its governed retrieval/replay envelope (for
example, the retrieval replay work tracked by #1104); authorization remains at
that retrieval boundary.

This lets an adapter reproduce membership and order without publishing
prompts, durable memory bodies, credentials, or tool arguments. The contract
is compatible with a separate scoped-memory capability declaration (#1103) and
with per-type context budgets (#1008): those upstream stages select/govern the
input set, while this boundary records and admits the provider-serving
projection.

## Synthetic fixture coverage

The fixture includes:

- old user requirements with load-bearing markers;
- system, Keystone/policy, authority, and user-constraint classes;
- assistant prose;
- an OpenAI-shaped tool-use/result pair with a critical JSON tail;
- source code and fenced content with critical tails;
- an OpenAI multimodal content array;
- an explicitly unknown class;
- cases for rejected protected loss, non-reversible tool-result loss,
  explicit lossy opt-in, reversible replay, malformed pairing, passthrough,
  multimodal shape validation, and unavailable backend state.

Run the focused contract lane with:

```bash
cargo test --no-default-features --test context_transformer_contract
cargo test --no-default-features context_transform_tool_returns_hash_only_outcome_and_is_advertised
```

The MCP validator is `perseus_vault_context_transform_validate` and is
ops-scoped. Its response is hash-only; transient request/proposal messages are
never echoed.
