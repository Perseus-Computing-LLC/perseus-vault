# Receipt-conditioned evidence intervention (#1136)

This package implements a **provider-free, deterministic trace-faithfulness and
evidence-necessity diagnostic**. It does not claim that an evidence receipt
reveals a model's hidden causal reasoning.

## Arms

Every synthetic case uses the same retrieval mode, top-k, context-token budget,
scan budget, reader, judge, scope, as-of anchor, and seed:

- `baseline` — seals the canonical baseline selection and evidence receipt;
- `receipt-blocked` — blocks every source group named by that sealed receipt;
- `random-control` — blocks the same number of eligible source groups using the
  frozen deterministic seed; and
- `matched-size-control` — blocks the same number of source groups and exact
  token total as the receipt arm.

The baseline for every case is selected and sealed before any intervention set
is computed. Gold evidence is available only to the offline evaluator after
that boundary.

## No-reentry contract

Blocking applies to the receipt's source groups, not one candidate ID. Every
candidate from a blocked group is removed before selection, including synonym,
dense, source-expansion, cache, and fallback aliases. Scope, agent, lifecycle,
and as-of checks run fail-closed; stale, superseded, tombstoned, missing, and
ambiguous receipt references cannot produce a report.

## Report

`report.json` separates:

1. retrieval sufficiency;
2. context/assembly counts and budgets;
3. deterministic answer outcomes; and
4. provider/answerer/judge execution counters.

Per-row accounting distinguishes blocked receipt evidence, blocked evaluator
gold, selected-but-unreceipted evidence, and unavailable evidence. Public
output contains synthetic lane-local identifiers, counters, reason codes, and
SHA-256 commitments—not prompts, memory bodies, provider payloads, or secrets.

The report binds the question set, retrieval configuration, fixture, baseline
receipt set, intervention set, output projection, and harness bytes. The
existing #1132 attribution contract and #1109 counterfactual gate remain the
controlling publication boundaries.

## Run

```bash
python3 benchmark/receipt_intervention/harness.py \
  --fixture benchmark/receipt_intervention/fixture.json \
  --out /tmp/receipt-intervention-report.json

python3 -m unittest discover \
  -s benchmark/receipt_intervention -p 'test_*.py' -v
```

A paid canary is not part of this package. It requires a separate authorization,
manifest, paired custody record, and claim boundary.
