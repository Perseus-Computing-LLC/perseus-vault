# Memory-quality scorecard and release gate policy

This policy implements [issue #779](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/779)
and the bounded v0 evaluation slice in [issue #862](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/862).
The machine-readable report is produced by the existing
`benchmark/quality/run.py`; the scorecard remains a separate blocking decision.

## Policy

The memory-quality gate is **blocking** for pull requests that touch Vault
behavior or the benchmark itself.

For the v0 manifest, `release_ready` requires all of the following:

- 20–30 manifest cases (the committed v0 has 24 cases and 29 checks).
- Aggregate accuracy must be finite and exactly **1.0**; `checks_passed` must
  equal a positive `checks_total`.
- Every required category is present:
  - long-horizon recall
  - contradiction/supersession
  - shared-memory visibility
  - adversarial contamination
  - temporal validity
  - scope validity / scope-invalid recall
  - provenance
  - replay fidelity
  - mutation/supersession
  - compaction/projection
  - action grounding
- Every executed check passes.
- No optional case is silently omitted: `unavailable` capability/case/metric
  state blocks the scorecard and is named in the scorecard artifact.
- The report is offline and hash-only for public evidence. The evidence
  sanitizer is allow-list based: credentials, timestamps, random identifiers,
  and unknown fields are dropped rather than retained or hashed.

Anything else produces `verdict: blocked` and a non-zero process exit.

## Metrics

The v0 report records grouped metrics plus flat `metric_rates` for:

- `validity_rate`
- `stale_recall_rate`
- `scope_invalid_recall_rate`
- `provenance_completeness`
- `replay_fidelity`
- `mutation_supersession_rate`
- `compaction_projection_rate`
- `action_grounding_rate`

`stale_recall_rate` and `scope_invalid_recall_rate` count undesirable recalls,
so zero is the passing value. Completeness, fidelity, and grounding rates count
satisfied observations, so one is the passing value. A missing optional
capability has `status: unavailable` or `partial` and a null/qualified rate; it
is not converted to zero or one.

`stale_recall_rate` is derived from the live superseded-version assertion, not
from the success-oriented mutation metric. The MCP client applies a per-request
wall-clock timeout so a hung checkout binary produces a bounded failure.

## CI artifacts

The `Memory quality gate` workflow uploads one artifact named
`memory-quality-scorecard` containing:

- `benchmark/quality/report.json` — sanitized scenario evidence, assertions,
  capabilities, and metrics
- `benchmark/quality/scorecard.json` — concise release verdict and threshold
  policy

Run locally:

```bash
cargo build --locked --no-default-features
python benchmark/quality/run.py --out /tmp/memory-quality-report.json
python benchmark/quality/scorecard.py /tmp/memory-quality-report.json \
  --out /tmp/memory-quality-scorecard.json
```

Pass `--bin /path/to/perseus-vault` or set `PERSEUS_VAULT_BIN` when the binary
is outside the normal checkout target directories. The harness uses a fresh
temporary SQLite database and performs no remote actions.

## Optional capability semantics

`tools/list` is used as a capability probe. The v0 runner currently records
explicit state for `compact`, `context`, `stage_trace_validate`, and the
action-control-plane tool set. An absent optional tool creates an `unavailable`
case with a reason; an execution error is a failed case, not a successful skip.
The scorecard exposes `unavailable_categories`, `unavailable_cases`,
`unavailable_capabilities`, and `unavailable_metrics` so environment limits are
visible to reviewers.

## Override path

A maintainer may override a blocked quality gate only when the release cannot
safely wait. The override must:

- identify every failing or unavailable check and expected user impact;
- link a remediation issue with an owner;
- be recorded in the release notes; and
- receive explicit maintainer approval in the pull request or release record.

The scorecard reports this policy but never silently converts a failed or
unavailable run to passing. The CI check remains red; an override is a
documented repository-governance decision.

## Interpreting results

- `release_ready`: all required memory-quality behavior held and all declared
  capabilities were exercised.
- `blocked`: at least one required category is absent, a check failed, or a
  capability/case/metric is unavailable.
- Compare raw **sanitized** evidence in `report.json` before attributing a
  failure to product behavior; infrastructure failures should be repaired and
  rerun, not overridden as quality regressions.
