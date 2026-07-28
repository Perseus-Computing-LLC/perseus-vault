# Perseus Vault memory-quality benchmark

This directory is the first implementation slice for [issue #778](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/778).

It drives the real Perseus Vault binary over MCP stdio through deterministic quality scenarios covering:

- long-horizon recall
- contradiction and supersession handling
- shared-memory visibility
- adversarial or contaminated memories

## Run

```bash
cargo build --no-default-features
python benchmark/quality/run.py
python benchmark/quality/run.py --out /tmp/perseus-vault-memory-quality.json
python -m unittest discover -s benchmark/quality -p 'test_*.py' -v
```

The harness is offline, uses an isolated temporary database, and emits a SHA-256
signature over the dataset and scenario verdicts. Exit code is non-zero when any
quality check fails, so it can gate.

## Scenario coverage

| Category | Scenario | Checks |
|---|---|---|
| Long horizon | `long-horizon-basic` | Target survives intervening memories; current answer is returned |
| Contradiction / supersession | `contradiction-supersession-basic` | Current write wins; superseded evidence is retained |
| Shared memory | `shared-memory-scope-basic` | Author can read private memory; another identified agent cannot |
| Adversarial | `adversarial-contamination-basic` | Verified truth remains current; contamination does not displace it |

## Report contract

Each report contains:

- `benchmark`
- `dataset`
- `cases[]` with `id`, `category`, `checks.passed` / `checks.total`, `assertions`, and `evidence`
- `checks_passed`, `checks_total`, `accuracy`, and `passed`
- `missing_categories`
- `offline`
- `binary`
- `signature_sha256`
