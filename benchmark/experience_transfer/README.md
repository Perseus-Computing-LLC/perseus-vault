# Verified experience transfer benchmark

Status: provider-free reference package; ready for review

This benchmark tests one narrow proposition:

> A useful prior experience is not automatically a trustworthy current instruction.

Agent A completes a fictional task, captures a worked or failed approach with evidence,
verifies the outcome, promotes it, and transfers it to Agent B. B encounters current,
stale, contradictory, deleted/revoked, derived, split-brain, or authority-changed state.
The system may reuse, reject, abstain, or block.

## Contents

- `generate_corpus.py`: deterministic original 24-pair synthetic corpus generator.
- `build_shared_views.py`: label-blind shared projection for independent implementations.
- `common.py`: canonical commitments and fail-closed validators.
- `adapters.py`: stateless, ungoverned recall, governed reference, and explicit
  `not_measured` external adapter specification.
- `runner.py`: provider-free deterministic reference runner and public report.
- `verify.py`: independent readiness gate.
- `corpus/corpus.json`: self-contained synthetic evaluator fixture, including hidden
  expected labels for local contract verification only.
- `corpus/shared_agent_views.json`: shared implementation input; opaque IDs and no
  expected decision/control labels.
- `corpus/label_commitments.json`: label commitments without label values.
- `schemas/`: JSON Schema shape contracts.
- `reports/provider_free/`: hash-bound manifest, public-safe report, acceptance, and
  readiness artifacts.
- `reference_workflow/`: hash-only context → provenance → authority → decision receipt.
- `tests/`: deterministic, tamper, leakage, adapter, report, and workflow checks.

For a blind external implementation run, provide only `corpus/shared_agent_views.json`
and keep the expected-label map/commitments out of the adapter process. The complete
labeled fixture is retained here so the provider-free governed reference can be tested
without a provider; it is not a model-facing input.

## Reproduce from the repository root

```bash
python3 -m benchmark.experience_transfer.generate_corpus \
  --out benchmark/experience_transfer/corpus/corpus.json
python3 -m benchmark.experience_transfer.build_artifacts \
  --corpus benchmark/experience_transfer/corpus/corpus.json \
  --generator benchmark/experience_transfer/generate_corpus.py \
  --out benchmark/experience_transfer/corpus/manifest.json
python3 -m benchmark.experience_transfer.build_shared_views \
  --corpus benchmark/experience_transfer/corpus/corpus.json \
  --out benchmark/experience_transfer/corpus/shared_agent_views.json \
  --labels-out benchmark/experience_transfer/corpus/label_commitments.json
python3 -m benchmark.experience_transfer.runner \
  --corpus benchmark/experience_transfer/corpus/corpus.json \
  --outdir benchmark/experience_transfer/reports/provider_free
python3 -m benchmark.experience_transfer.reference_workflow.implementation \
  --corpus benchmark/experience_transfer/corpus/corpus.json \
  --out benchmark/experience_transfer/reference_workflow/receipts.json
python3 -m unittest discover \
  -s benchmark/experience_transfer/tests -p 'test*.py' -v
python3 -m benchmark.experience_transfer.verify
```

All commands are deterministic and provider-free: zero LLM, judge, embedding,
external-implementation, network, or production-state calls. The final readiness
artifact must say `ready_for_provider_free_review`.

## Interpretation boundary

The governed reference’s synthetic decision-label agreement is a contract-policy result
over fictional records. It is not agent-task efficacy, customer/production efficacy,
independent validation, cross-provider generalization, or universal superiority. Real
Agent A/B task execution, provider-backed token/cost/latency data, and the external
implementation adapter remain `not_measured`.

The package is complementary to `docs/specs/experience-projections.md`: that Vault
surface provides a non-authoritative derived projection over canonical sources, while
this benchmark tests whether a transferred experience is still trustworthy under
change. A projection’s provenance is not itself current authority.

## Decision and safety classes

- `reuse`: current verified experience passed evidence, scope, authority, and any
  required revalidation.
- `reject`: failed, stale, superseded-without-replacement, or contaminated experience
  cannot be reused.
- `abstain`: evidence is missing, deleted, revoked, insufficient, or lineage is unknown.
- `block`: authority is changed, contradiction is unresolved, or split-brain requires
  settlement.

Malformed hashes, unauthorized accepted transitions, duplicate IDs, public forbidden
fields, and nondeterministic output fail closed. The public report contains IDs,
commitments, decisions, counters, and statuses only; it contains no prompts, context
bodies, provider responses, credentials, secrets, or customer data.
