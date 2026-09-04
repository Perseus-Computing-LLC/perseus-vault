# LongMemEval-V2 provider-free Vault readiness lane

This directory implements the provider-neutral `Memory` boundary used by the
pinned LongMemEval-V2 harness. It is a readiness adapter, not an answer
benchmark and not a production memory store.

Pinned external interface:

- repository: `https://github.com/xiaowu0162/LongMemEval-V2`
- revision: `2cc8c540bdb87fe6761629b585e727e1c4704520`
- backend interface: `memory_modules/memory.py`
- harness: `evaluation/harness.py`

`LongMemEvalV2VaultMemory` has exactly the V2 boundary:

```python
insert(trajectory: dict[str, object]) -> None
query(query: str, query_image: str | None = None) -> list[dict[str, str]]
```

The adapter uses a fresh in-process Vault-shaped store for synthetic replay.
It does not import a provider SDK, open a Vault database, read customer data,
or make network/model/judge calls. A future external harness integration must
supply only full trajectories to `insert` and only question text plus the
optional question image to `query`.

## Guarded behavior

- Trajectory/session identity, input event order, V2 `state_index`, timestamps,
  source references/URLs, scope, lifecycle, conflict links, and supersession
  metadata are retained in the bounded internal projection.
- Only active, in-scope, non-superseded evidence can be served.
- Conflicts remain visible; the adapter never resolves or blends them.
- Text and image output is bounded. Image paths are restricted to the configured
  fixture/data root and are verified as existing files before emission.
- Empty/no-visible evidence is an explicit abstained diagnostic. An unavailable
  backend is a distinct unavailable diagnostic.
- Unknown top-level trajectory fields are ignored. Benchmark question IDs,
  question types, answer-session IDs, gold answers, evaluator metadata, and
  hidden labels never enter adapter logic or the public replay report.

## Provider-free replay

Run the deterministic preparation/replay with an output directory outside the
repository if desired:

```bash
PYTHONPATH=. python3 -m benchmark.longmemeval_v2.replay \
  --outdir /tmp/perseus-vault-lme-v2-replay
```

The output contains:

- `manifest.json`: pinned V2 revision, synthetic dataset revision, Vault source
  and schema identities, adapter/config digest, prompt/token-budget metadata,
  concurrency/retry policy, and zero-call offline controls;
- `replay_report.json`: provider-free retrieval/context readiness, all five V2
  ability rows, web/enterprise rows, separate answer/cost/instrumentation
  surfaces, and the claim boundary;
- `replay_signature.txt`: deterministic report signature;
- `artifact_inventory.json`: SHA-256 inventory for the generated report,
  manifest, signature, fixture, config, and Vault schema.

Run the replay twice and compare all four files byte-for-byte. No result is an
answer-accuracy, leaderboard, customer-efficacy, production-validation, or
cross-model claim. A canary and any answer evaluation remain separately
authorized work.
