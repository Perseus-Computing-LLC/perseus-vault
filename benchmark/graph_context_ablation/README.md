# Matched graph-context ablation (#1143)

This package is a **provider-free, deterministic, Vault-owned diagnostic** for
graph utility. It compares two cells over the same synthetic company-brain
fixture:

- `graph-off`: fused fixture retrieval without graph expansion;
- `graph-on`: the same retrieval mode, top-k, context budget, reader, prompt,
  judge, seed, and corpus, with graph expansion available.

The only intended difference is `graph_enabled`. It is not a HydraDB runtime
integration, does not reproduce vendor-reported LongMemEval/BEAM scores, and
must not be presented as model-internal causality or third-party efficacy.

## Fixture

`fixture.json` contains synthetic:

- ADR, meeting notes, Slack root/replies, postmortem, and service manifest
  sources;
- a true multi-hop answer and a single-hop control;
- stale/current conflict, unsupported declared edge, cross-workspace target,
  no-signal utility-gate skip, and abstention cases;
- supported declared/derived edges tied to exact source revisions and evidence
  anchors.

Unsupported, cross-scope, and stale edges remain visible as dropped edge
decisions. They are never counted as supported paths.

## Report contract

`report.json` is generated from the fixture and validated by
`perseus-vault-graph-context-ablation-report/v1` in `report.schema.json`.
The report separates:

1. retrieval/evidence coverage and path integrity;
2. deterministic reader/judge answer outcomes;
3. context selected/dropped counts and delivered tokens;
4. provider-free execution counters and an explicit unmeasured-latency
   denominator.

It contains source IDs, revisions, anchors, counters, reason codes, and SHA-256
commitments—not raw provider payloads, credentials, or external benchmark
scores. The fixture, dataset projection, protocol manifest, matched config,
prompt, offline reader, offline judge, and complete report are digest-bound.

## Run

```bash
python3 benchmark/graph_context_ablation/harness.py \
  --fixture benchmark/graph_context_ablation/fixture.json \
  --out /tmp/graph-context-ablation-report.json

python3 -m unittest discover \
  -s benchmark/graph_context_ablation -p 'test_*.py' -v
```

The committed `report.json` must exactly equal a fresh build from the committed
fixture. Any source, fixture, config, metric, path, or report edit requires
regeneration and a fresh exact-tree verification.
