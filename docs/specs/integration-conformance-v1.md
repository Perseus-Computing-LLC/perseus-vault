# Perseus Vault integration conformance v1

This is the shared behavioral contract for organization-owned Vault adapters.
The contract is deliberately transport- and framework-neutral. It is not a
claim that an adapter has passed: each adapter must publish a sanitized report
against the fixture and declare its Vault version.

## Version

```text
perseus-vault-integration-conformance/v1
```

The normative synthetic cases are in
[`tests/fixtures/integration_conformance_v1.json`](../../tests/fixtures/integration_conformance_v1.json).
Validate a fixture or report with:

```bash
python scripts/validate_integration_conformance.py \
  --fixture tests/fixtures/integration_conformance_v1.json \
  --report path/to/sanitized-report.json
```

## Required behavior

Adapters must distinguish:

- an empty, valid recall from an unavailable or failed backend;
- workspace/tenant isolation;
- idempotent logical updates from duplicate writes;
- forget/revocation from permanent erasure when the Vault contract retains history;
- complete, degraded, and abstain/review outcomes;
- provenance/scope metadata from raw memory bodies.

## Report contract

Every adapter report includes:

- `contract_version`;
- adapter name and version;
- Vault binary/schema version;
- one result per case;
- `status`: `pass`, `fail`, `degraded`, or `skip`;
- a lowercase SHA-256 `evidence_digest` for sanitized evidence.

Reports must not contain prompts, memory bodies, raw provider payloads, API keys,
or other secret material. A `skip` is not a pass and must include an explanation
in the adapter’s own CI output.

## Adoption status

The core fixture and validator are published here first. Current adapter
repositories should consume this contract in CI and add their own migration PRs;
until then, the parent integration issue remains open.
