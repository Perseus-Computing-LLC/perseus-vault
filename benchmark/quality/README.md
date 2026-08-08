# Perseus Vault memory-quality benchmark

This directory extends the issue #778/#779 benchmark with the bounded v0 slice
for [issue #862](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/862).
It remains the same manifest-driven MCP stdio harness; there is no parallel
benchmark runner.

The current v1 manifest contains **30 deterministic cases and 41 checks**. The
manifest identity is `perseus-vault-memory-quality-v1`; generated reports bind
the manifest digest, binary digest, control profile, and harness commit. Synthetic fixtures use stable category/key/token commitments, while the report
retains only booleans, counters, scope/key labels, and SHA-256 digests. Prompts,
memory bodies, tool arguments, credentials, timestamps, random identifiers, and
raw MCP payloads are not written to public artifacts. Evidence uses an explicit
allow-list; unknown fields are dropped rather than best-effort hashed.

## Run

```bash
cargo build --locked --no-default-features
python benchmark/quality/run.py --out /tmp/perseus-vault-memory-quality.json
python benchmark/quality/scorecard.py /tmp/perseus-vault-memory-quality.json \
  --out /tmp/perseus-vault-memory-quality-scorecard.json
python -m unittest discover -s benchmark/quality -p 'test_*.py' -v
```

Use `--bin /path/to/perseus-vault` or `PERSEUS_VAULT_BIN` to select a
checkout-built binary. The runner creates and removes a fresh temporary SQLite
store. It makes no network calls and does not use an LLM or embedding provider.
The live-binary test is explicitly skipped when no binary is available; the
CLI reports a blocked `binary_unavailable` result instead of fabricating a
pass. The loader also accepts the prior v1 four-case manifest, normalizing its
legacy category-shaped scenarios without weakening the v2 20–30-case checks.

## Coverage

| Area | Cases | Machine-readable metric |
|---|---:|---|
| Existing long-horizon / contradiction / shared-memory / adversarial | 4 | `validity_rate`, `scope_invalid_recall_rate`, `mutation_supersession_rate` |
| Temporal validity (`valid_at`) | 2 | `validity_rate` |
| Scope-invalid recall and retrieval profiles | 2 | `scope_invalid_recall_rate` (lower is better) |
| Recall provenance, origin, external refs, hash-only evidence | 3 | `provenance_completeness` |
| Frozen and temporal replay, optional stage fingerprint | 3 | `replay_fidelity` |
| Mutation, live-only recall, retained history | 3 | `stale_recall_rate`, `mutation_supersession_rate` |
| Compaction and bounded context projections | 3 | `compaction_projection_rate` |
| Authority, action receipt, grounding, lease lifecycle | 4 | `action_grounding_rate` |

The report contains both a grouped `metrics` object and a flat
`metric_rates` object with these stable names:

- `validity_rate`
- `stale_recall_rate`
- `scope_invalid_recall_rate`
- `provenance_completeness`
- `replay_fidelity`
- `mutation_supersession_rate`
- `compaction_projection_rate`
- `action_grounding_rate`

A stale/scope-invalid rate counts the undesirable event, so zero is the good
value. `stale_recall_rate` is computed from the live-recall assertion rather
than copied from a success metric. Completeness/fidelity/grounding rates count
satisfied observations, so one is the good value.

## Optional capabilities

The runner probes `tools/list` and records capability state in
`report.capabilities`. Missing optional tools produce case status
`unavailable`, a reason, and a metric status of `unavailable` or `partial`;
they are never silently counted as passing checks. In particular:

- `stage_trace_validate` is optional for the stage-fingerprint replay case.
- `compact` is optional for the compaction case.
- `context` and the action control-plane tools are probed explicitly.

The blocking scorecard names `unavailable_categories`, `unavailable_cases`,
`unavailable_capabilities`, and `unavailable_metrics`. A local build without an
optional capability therefore yields a useful qualified report but not a
false `release_ready` verdict. The scorecard also requires finite exact
accuracy of 1.0, consistent check counts, and a bounded response from every
MCP request.

## Report contract

The report includes:

- `benchmark`, `dataset`, `harness_version`, and `required_categories`
- v1 additions: recall outcome, admission, prompt safety, and identity ambiguity cases; v1 metric rates are required by the scorecard
- exactly 30 manifest-driven `cases[]` in v1, each with `id`, `category`, `metric`,
  `status`, `checks`, sanitized `assertions`, and sanitized `evidence`
- grouped `metrics` and flat `metric_rates`
- `capabilities`, `offline: true`, `network_calls: 0`,
  `public_evidence: "hash-only"`, and `raw_inputs_captured: false`
- checkout binary name and SHA-256, plus a deterministic `signature_sha256`
  over verdicts and metric outcomes (not random IDs, timestamps, or evidence)
- repeatable sanitized evidence: identical fresh runs produce identical report
  bytes for the deterministic v0 fixtures

The scorecard is a separate release decision. It remains blocking: all required
categories and checks must pass, aggregate accuracy must be 1.0, and no case or
metric may be unavailable.
