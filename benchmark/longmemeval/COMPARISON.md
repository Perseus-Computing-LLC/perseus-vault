# Head-to-head vs Zep: LongMemEval end-to-end QA (#475)

This page holds the one number that answers "are we better than Zep?" — and the
exact conditions it was produced under. **No number goes in this table without
its conditions.** The deprecated [`benchmarks/LONG_MEM_EVAL.md`](../../benchmarks/LONG_MEM_EVAL.md)
is the precedent: the old end-to-end claims were retracted because they cited an
unnamed/nonexistent model and mixed splits and judges. We do not do that again.

## The scoreboard

| system | LongMemEval QA accuracy | answerer | judge | split | source |
|---|---:|---|---|---|---|
| **Perseus Vault** | **73.8% mean** (72.8 / 73.6 / 75.0 across 3 full runs) | `gpt-4o-2024-08-06` (pinned) | `gpt-4o-2024-08-06`, LongMemEval **official** per-type judge | `longmemeval_s` (500) | [`qa_report.json`](qa_report.json), [`qa_report_seed2.json`](qa_report_seed2.json), [`qa_report_seed3.json`](qa_report_seed3.json) (all signed), this repo |
| Zep | 63.8% (published) | "GPT-4o" (snapshot not stated) | not stated | LongMemEval `_s` (as published) | Zep's published claim, cited in #475 |
| Mem0 | 49.0% (published) | "GPT-4o" (snapshot not stated) | not stated | LongMemEval `_s` (as published) | published claim, cited in #475 |

**Perseus Vault scores 73.8% mean over three independent full runs (range
72.8–75.0, stdev 1.1) — the *worst* run beats Zep's published 63.8% by 9.0
points** — under the official LongMemEval metric. Independently confirmed: run
2's 500 answers, re-graded by LongMemEval's own `src/evaluation/evaluate_qa.py`,
produce **368/500 = 73.60%**, matching this harness bit-for-bit (see
[`official-eval-results-gpt-4o.jsonl`](official-eval-results-gpt-4o.jsonl)).
Per-type results are highly stable across runs: single-session-assistant scored
100.0% and single-session-user 95.7% in all three; the weakest category
(preference) stayed within 26.7–30.0%.

> **Still confirm before external publication:** the primary source of Zep's
> 63.8% and the exact split + GPT-4o snapshot Zep used. Our answerer is pinned to
> `gpt-4o-2024-08-06` (the closest snapshot to an unspecified "GPT-4o"); if Zep
> used a different snapshot the comparison carries a "close but not identical
> answerer" caveat.

## By question type

The signed run-2 report's `by_question_type`. Temporal reasoning is broken out because
the bi-temporal engine is supposed to shine there.

| question type | n | correct | accuracy |
|---|---:|---:|---:|
| single-session-assistant | 56 | 56 | 100.0% |
| single-session-user | 70 | 67 | 95.7% |
| knowledge-update | 78 | 60 | 76.9% |
| **temporal-reasoning** | 133 | 92 | **69.2%** |
| multi-session | 133 | 84 | 63.2% |
| single-session-preference | 30 | 9 | 30.0% |
| — abstention subset (`_abs`) | 30 | 25 | 83.3% |
| **overall** | **500** | **368** | **73.6%** |

**Honest read of the weak spots.** `single-session-preference` (30.0%) is our
weakest category and genuine headroom, not a harness artifact: the model has to
surface a preference the user stated in an *earlier* session and apply it, and
top-k=10 hybrid retrieval does not always surface it. `multi-session` (63.2%)
carries the same signature on aggregation questions — the model retrieves *some*
of the sessions a "how many / how much in total" question needs but not all
(e.g. finds 2 of 3 charity events and under-counts). This retrieval-recall gap
on multi-hop questions is tracked as follow-on engine work, not papered over.

## Methodology correction (run 1 → run 2)

The first full run of this harness scored **55.2%**. Investigation found that was
depressed by two ways this harness had deviated from LongMemEval's *official*
methodology — the very methodology Zep's number is measured under — not by any
weakness of the memory engine:

1. **Answer prompt.** An earlier revision added *"if the answer is not present in
   the history, say you don't know."* LongMemEval's official answer prompt
   ([`run_generation.py`](https://github.com/xiaowu0162/LongMemEval)) has no such
   clause. Ours force-abstained the model on open-ended questions:
   `single-session-preference` collapsed to **1/30** — 29 of 30 failures were the
   literal string "I don't know."
2. **Judge.** An earlier revision used one homegrown "does the response contain
   the gold answer" judge for every type. The official metric
   ([`evaluate_qa.py::get_anscheck_prompt`](https://github.com/xiaowu0162/LongMemEval))
   is **per-question-type**: it forgives off-by-one day counts on temporal
   questions, and grades preference answers against a *rubric* ("correct as long
   as it recalls and utilizes the user's personal information") rather than
   requiring the answer to literally contain a rubric paragraph.

Run 2 replaced both with the official prompts, ported verbatim into
[`qa.py`](qa.py) (`ANSWER_PROMPT`, `get_anscheck_prompt`). The gain is entirely
recovery of points the harness was wrongly discarding — confirmed by re-grading
through the authors' own evaluator. Abstention correctly *fell* from a
suspicious 100% to 83.3%, because the official prompt no longer forces the model
to abstain; that loss is far outweighed by the honest gains elsewhere. **No
tuning toward gold answers was done, and none is acceptable here.**

## Exact-conditions statement

- **Split:** `longmemeval_s` — 500 instances, ~48 sessions per haystack. Same
  family Zep reports on; confirm their exact split before calling it same-split.
- **Answerer:** `gpt-4o-2024-08-06`, temperature 0.
- **Judge:** `gpt-4o-2024-08-06`, temperature 0, LongMemEval's **official**
  per-type `get_anscheck_prompt`, ported verbatim in [`qa.py`](qa.py). The
  earlier judge caveat is now resolved: grading matches the authors'
  `evaluate_qa.py` bit-for-bit (368/500 both ways). Hypotheses are emitted in
  LongMemEval's official format ([`hypotheses-mimir-gpt-4o-2024-08-06.jsonl`](hypotheses-mimir-gpt-4o-2024-08-06.jsonl))
  so anyone can re-grade independently.
- **Retrieval:** perseus-vault hybrid recall, top-k 10, bundled ONNX embeddings,
  real binary (`perseus-vault 2.19.1`) over MCP stdio. Full k=10 context on every
  question (avg 9.9 sessions / ~25k tokens per prompt; no truncation).
- **Provenance:** signature `929623670d8bcc67…d064345` over the per-question
  verdict set; hardware, elapsed (55.8 min at 400k TPM), and config all in
  [`qa_report.json`](qa_report.json).

## Caveats (read before quoting the number)

- **Run-to-run variance.** LLM answering/grading at temperature 0 is still not
  perfectly deterministic. Three independent full runs scored 73.6 / 75.0 / 72.8
  (mean 73.8, spread 2.2 points, stdev 1.1) — all three signed reports are
  committed here. Quote the mean with the range, not a single run's number.
- **Zep's conditions are quoted, not verified.** We have not reproduced Zep's own
  run; 63.8% is their published claim. State that when comparing.
- **Preference is weak (30%).** If a competitor stresses preference specifically,
  that is our soft spot today. Own it.

## Reproduce

```bash
# 1. Dataset (public, ~277 MB)
curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o benchmark/longmemeval/longmemeval_s_cleaned.json

# 2. Free plumbing check (no key): stubbed answerer+judge, real ingest+retrieval
python benchmark/longmemeval/qa.py --mock-llm --limit 5

# 3. Cheap paid smoke (needs OPENAI_API_KEY or ~/.openai_key)
python benchmark/longmemeval/qa.py --limit 10

# 4. The full number (500 questions; prints a cost estimate, requires --yes)
python benchmark/longmemeval/qa.py --yes

# 5. Independent re-grade with LongMemEval's official evaluator
#    (produces the same 368/500 = 73.60%)
python evaluate_qa.py gpt-4o \
  benchmark/longmemeval/hypotheses-mimir-gpt-4o-2024-08-06.jsonl \
  benchmark/longmemeval/longmemeval_s_cleaned.json
```

Defaults are the pinned models above; `--model`, `--judge`, `--split`, `--k`,
`--limit` override (every override is recorded in `qa_report.json`). This run is
**opt-in and NOT part of any CI gate** — it costs real money (estimate printed
upfront; roughly $28 for the full 500-question mimir-only run at k=10 and
2026-07 GPT-4o pricing).

## Related numbers (do not conflate)

- **Session-level retrieval recall** ([`README.md`](README.md), `report.json`):
  recall@1 0.846 / recall@10 0.992, fully offline, judge-free. That is a
  *retrieval* metric — never present it as QA accuracy.
- **Token efficiency** (`qa.py --dry-run`): mimir feeds ~8x fewer tokens than
  full-context stuffing at k=5. Offline and reproducible.
