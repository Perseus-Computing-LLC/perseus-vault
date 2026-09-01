# LongMemEval — current retrieval claim

> **The former QA-accuracy page is deprecated.** The supported measurement now lives
> at [`benchmark/longmemeval/`](../benchmark/longmemeval/README.md): a reproducible,
> fully offline, judge-free **session-level recall** harness that drives the real
> `perseus-vault` binary over the public LongMemEval `_s` split.

## Current source-checked result

The committed **content-hashed** artifact
[`report-currentmain-2026-08-16.json`](../benchmark/longmemeval/report-currentmain-2026-08-16.json)
contains 500 questions and 23,867 ingested sessions. Against LongMemEval's
`answer_session_ids`, the hybrid RRF arm reports:

- recall@1: **83.2%**
- recall@3: **96.6%**
- recall@5: **98.8%**
- recall@10: **99.8%**
- MRR: **0.8949**

This is a retrieval metric, not end-to-end QA accuracy. The run is offline and
judge-free: it does not call an answerer or judge, and it uses the real binary
with the bundled local embedding path. The full mode table, methodology, report
hash checks, and reproduction command are maintained in the benchmark README.

## Why the former QA results are not public claims

The earlier page mixed an answerer, a judge, and a split whose provenance was not
sufficiently reproducible. It also referred to an unidentifiable model and a
harness path that was not committed in this repository. A percentage that cannot
be regenerated from named, committed inputs is not carried into the current
public product claim.

End-to-end QA remains an opt-in engineering lane in `benchmark/longmemeval/qa.py`.
If it is run, the report must name the exact answerer, judge, prompts, split,
denominator, and artifact digests; it must not be presented as the offline
retrieval result above or compared across unlike protocols.
