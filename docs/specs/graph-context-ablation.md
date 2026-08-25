# Matched graph-context ablation v1

Status: provider-free implementation specification
Date: 2026-08-25
Resolves: #1143
Depends on: #869, #1065, #1111, #1112, #1135, #1138, #1140, #1142

## Purpose and claim boundary

The ablation answers one bounded question: under a fixed synthetic corpus and
fixed context-serving protocol, what evidence coverage and context cost change
when governed graph expansion is available? It does not establish model
causality, production efficacy, or product superiority. It does not reproduce
HydraDB's vendor scores or introduce HydraDB as a dependency.

The first gate is offline and provider-free. A future model canary would require
a separate manifest, authorization, custody package, and comparability label;
it cannot alter this offline verdict.

## Paired cells

`benchmark/graph_context_ablation/fixture.json` freezes the paired controls:

- query/case set and synthetic corpus;
- retrieval mode and top-k;
- context-token budget;
- deterministic required-evidence reader;
- prompt contract and deterministic fixture judge;
- seed and graph traversal depth.

The cells differ only in `graph_enabled`. No retrieval-mode comparison is
included. `matched_config_sha256` must be identical across the two cells, while
each complete cell config has its own digest.

## Corpus and negative controls

The synthetic company-brain fixture contains an ADR, meeting notes, a Slack
root/reply thread, a postmortem, and structured service manifests. Six cases
cover:

1. a true two-hop dependency answer;
2. a single-hop control;
3. a stale/current retry conflict;
4. an unsupported declared edge;
5. a cross-workspace target; and
6. a no-signal abstention case.

The graph-on arm follows only current, in-scope, supported edges with a nonempty
evidence anchor. Unsupported, cross-scope, and stale edges are retained as
explicit dropped decisions. Ordinary, temporal, and no-signal cases expose the
utility-gate skip instead of treating it as a graph failure.

## Evidence and path contract

Every selected path includes:

- edge and endpoint identifiers;
- relation and declared/derived origin;
- `support_state=supported`;
- source ID and exact revision;
- SHA-256 of the synthetic source content; and
- a nonempty evidence anchor.

Missing or weak support cannot become a selected path. The public report does
not carry source bodies or query text; queries are represented by SHA-256.

## Metrics

Metric classes remain separate:

- `retrieval_evidence`: source-evidence recall, all-required-evidence rate,
  path/relation precision, unsupported-edge rate, stale/conflict leakage;
- `answer_quality`: deterministic expected-verdict match and abstention counts;
- `context_cost`: selected/dropped counts and delivered tokens;
- `execution`: provider/network/error/operation counters plus latency status.

No wall-clock latency is fabricated. The provider-free v1 report records
latency as `not_measured_provider_free` with denominator zero. A future measured
lane must record a nonzero denominator and its host/protocol provenance as a
separate result.

## Custody and replay

The report binds fixture, dataset projection, protocol manifest, matched config,
prompt, offline reader, offline judge, source contents, and final report with
canonical JSON SHA-256 commitments. Input lists are identity-sorted before
execution, so reordering the fixture does not alter the report. The validator
recomputes cell aggregates from rows and rejects digest drift or unsupported
selected paths.

Verification:

```bash
python3 benchmark/graph_context_ablation/harness.py \
  --fixture benchmark/graph_context_ablation/fixture.json \
  --out benchmark/graph_context_ablation/report.json
python3 -m unittest discover \
  -s benchmark/graph_context_ablation -p 'test_*.py' -v
```
