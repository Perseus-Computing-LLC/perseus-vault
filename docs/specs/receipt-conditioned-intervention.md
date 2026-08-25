# Receipt-conditioned trace-faithfulness intervention v1

Status: provider-free implementation specification
Date: 2026-08-25
Resolves: #1136
Depends on: #1104, #1108, #1109, #1112, #1132, #1135, #1140

## Purpose and claim boundary

The intervention tests whether the evidence region named by a sealed baseline
receipt is necessary to reproduce a deterministic retrieval/context path under
matched controls. It is labeled `trace-faithfulness-evidence-necessity`.

It does not establish model-internal causal attribution, reproduce MemBukkit's
reported score, or use benchmark gold to alter production retrieval. The first
gate is offline and provider-free. Any paid paired lane is a separate action
requiring its own authorization, manifest, custody, and comparability label.

## Frozen paired protocol

For each case, the harness freezes the question, candidate corpus, retrieval
mode, top-k, context-token budget, scan budget, reader, judge, scope, agent,
as-of anchor, and seed. It then:

1. runs the baseline selection;
2. seals a canonical evidence receipt over the baseline selection and named
   evidence references;
3. derives all intervention sets from that already-sealed state; and
4. exposes evaluator gold only after intervention selection is complete.

The report contains four arms: baseline, receipt-blocked, deterministic random
control, and same-cardinality/same-token control.

## Source-group blocking

The intervention unit is the stable source group. Blocking only one candidate
ID is insufficient because the same evidence can re-enter through a synonym,
source expansion, dense/lexical alternate lane, cache, or fallback. Before
selection, every candidate whose source group is blocked receives an explicit
blocked disposition.

A receipt reference must resolve uniquely to an eligible baseline-selected
candidate in the same workspace/agent scope and at the frozen as-of time.
Missing, ambiguous, stale, superseded, tombstoned, or out-of-scope references
fail closed.

## Controls and accounting

Every intervention records:

- blocked source-group set and digest;
- blocked cardinality and token total;
- scan and context-token budgets;
- baseline receipt digest; and
- intervention digest.

The random control uses the frozen seed and blocks the same cardinality. The
matched-size control blocks the same cardinality and exact token total. The
retrieval scan can refill vacated positions only from nonblocked eligible source
groups under the unchanged budgets.

Per-row evaluator accounting distinguishes:

- blocked receipt evidence;
- blocked gold evidence;
- selected-but-unreceipted evidence; and
- evaluator-required evidence unavailable to retrieval.

These are separate from context assembly and answer outcome fields.

## Custody and replay

Canonical SHA-256 commitments bind the normalized fixture, hash-only question
set, retrieval configuration, complete baseline receipt set, intervention set,
output projection, harness bytes, and final report. Input list order is
normalized before execution. The report validator recomputes cell aggregates,
receipt/intervention digests, token accounting, and blocked-set/output
disjointness.

The controlling publication boundaries remain #1132's failure-attribution
contract and #1109's paired evidence-ledger counterfactual gate. This synthetic
fixture is an enabling diagnostic, not an efficacy score.

Verification:

```bash
python3 benchmark/receipt_intervention/harness.py \
  --fixture benchmark/receipt_intervention/fixture.json \
  --out benchmark/receipt_intervention/report.json
python3 -m unittest discover \
  -s benchmark/receipt_intervention -p 'test_*.py' -v
```
