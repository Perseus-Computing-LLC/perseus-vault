# Memory-quality scorecard and release gate policy

This policy implements [issue #779](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/779). It turns the machine-readable report from `benchmark/quality/run.py` into a release decision that maintainers can inspect in one artifact.

## Policy

The memory-quality gate is **blocking** for pull requests that touch Vault behavior or the benchmark itself.

A scorecard is `release_ready` only when all of the following hold:

- Report accuracy is exactly **1.0**.
- Every required category is present:
  - long-horizon recall
  - contradiction/supersession
  - shared-memory visibility
  - adversarial contamination
- Every check in every category passes.

Anything else produces `verdict: blocked` and a non-zero process exit.

## CI artifacts

The `Memory quality gate` workflow uploads one artifact named `memory-quality-scorecard` containing:

- `benchmark/quality/report.json` — scenario evidence and raw assertions
- `benchmark/quality/scorecard.json` — concise release verdict and threshold policy

Run locally:

```bash
cargo build --locked --no-default-features
python benchmark/quality/run.py --out /tmp/memory-quality-report.json
python benchmark/quality/scorecard.py /tmp/memory-quality-report.json --out /tmp/memory-quality-scorecard.json
```

## Override path

A maintainer may override a blocked quality gate only when the release cannot safely wait. The override must:

- Identify every failing check and expected user impact.
- Link a remediation issue with an owner.
- Be recorded in the release notes.
- Receive explicit maintainer approval in the pull request or release record.

The scorecard reports this policy but never silently converts a failed run to passing. The CI check remains red; an override is a documented repository-governance decision.

## Interpreting results

- `release_ready`: all required memory-quality behavior held; no override required.
- `blocked`: at least one required category is absent or at least one check failed.
- Compare raw evidence in `report.json` before attributing a failure to product behavior; infrastructure failures should be repaired and rerun, not overridden as quality regressions.
