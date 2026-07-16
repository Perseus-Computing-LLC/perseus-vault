# Head-to-head vs Zep: LongMemEval end-to-end QA (#475)

This page holds the one number that answers "are we better than Zep?" — and the
exact conditions it was produced under. **No number goes in this table without
its conditions.** The deprecated [`benchmarks/LONG_MEM_EVAL.md`](../../benchmarks/LONG_MEM_EVAL.md)
is the precedent: the old end-to-end claims were retracted because they cited an
unnamed/nonexistent model and mixed splits and judges. We do not do that again.

## The scoreboard

LongMemEval ships **two** official answer prompts (`run_generation.py`: plain
and `cot=true` step-by-step). Both rows below are 100% official methodology;
they differ **only** in which official prompt the answerer used. Zep's
publication does not state which variant they used, so the comparison is
flagged per-row, not blended.

| system | LongMemEval QA accuracy | answer prompt | answerer | judge | split | source |
|---|---:|---|---|---|---|---|
| **Perseus Vault (official CoT)** | **79.0% mean** (80.0 / 78.6 / 78.4 across 3 full runs) | `official-cot` (`qa.py --cot`) | `gpt-4o-2024-08-06` (pinned) | `gpt-4o-2024-08-06`, LongMemEval **official** per-type judge | `longmemeval_s` (500) | [`qa_report_cot.json`](qa_report_cot.json), [`qa_report_cot_seed2.json`](qa_report_cot_seed2.json), [`qa_report_cot_seed3.json`](qa_report_cot_seed3.json) (all content-hashed, sha256), this repo |
| **Perseus Vault (plain)** | **73.8% mean** (72.8 / 73.6 / 75.0 across 3 full runs) | `plain` | `gpt-4o-2024-08-06` (pinned) | same official judge | `longmemeval_s` (500) | [`qa_report.json`](qa_report.json), [`qa_report_seed2.json`](qa_report_seed2.json), [`qa_report_seed3.json`](qa_report_seed3.json) (all content-hashed, sha256), this repo |
| Zep | 63.8% (published) | not stated | "GPT-4o" (snapshot not stated) | not stated | LongMemEval `_s` (as published) | Zep's published claim, cited in #475 |
| Mem0 | 49.0% (published) | not stated | "GPT-4o" (snapshot not stated) | not stated | LongMemEval `_s` (as published) | published claim, cited in #475 |

**With LongMemEval's own official CoT answer prompt, Perseus Vault scores 79.0%
mean over three independent full runs (range 78.4–80.0, stdev 0.9) — the
*worst* CoT run beats Zep's published 63.8% by 14.6 points; even the worst
plain-prompt run beats it by 9.0.** The CoT gain over plain (+5.2 mean) is
exactly where the #580 retrieval diagnostic predicted: 69% of consistent
plain-prompt failures were reasoning-over-correctly-retrieved-evidence, and the
step-by-step prompt recovers a large share of them (preference 28.9% → 57.8%
mean; temporal 69.2% → 76.2% mean). Abstention stayed healthy across the CoT
runs (86.7 / 83.3 / 80.0) — no robustness trade.

Independently confirmed, one run per prompt variant, re-graded by LongMemEval's
own `src/evaluation/evaluate_qa.py`: plain run 2 produced **368/500 = 73.60%**
(bit-for-bit match, [`official-eval-results-gpt-4o.jsonl`](official-eval-results-gpt-4o.jsonl));
the CoT primary run's 500 answers ([`hypotheses-cot-mimir-gpt-4o-2024-08-06.jsonl`](hypotheses-cot-mimir-gpt-4o-2024-08-06.jsonl))
re-grade to the **identical aggregate 400/500 = 80.00%**, with 4 individual
judge verdicts differing (2 flips in each direction — normal LLM-judge
nondeterminism at temperature 0, net zero; see
[`official-eval-results-cot-gpt-4o.jsonl`](official-eval-results-cot-gpt-4o.jsonl)).

> **Still confirm before external publication:** the primary source of Zep's
> 63.8% and the exact split + GPT-4o snapshot Zep used. Our answerer is pinned to
> `gpt-4o-2024-08-06` (the closest snapshot to an unspecified "GPT-4o"); if Zep
> used a different snapshot the comparison carries a "close but not identical
> answerer" caveat.

## By question type

### Official CoT prompt (primary run, content-hashed `qa_report_cot.json`)

| question type | n | correct | accuracy |
|---|---:|---:|---:|
| single-session-assistant | 56 | 56 | 100.0% |
| single-session-user | 70 | 68 | 97.1% |
| knowledge-update | 78 | 63 | 80.8% |
| **temporal-reasoning** | 133 | 103 | **77.4%** |
| multi-session | 133 | 92 | 69.2% |
| single-session-preference | 30 | 18 | 60.0% |
| — abstention subset (`_abs`) | 30 | 26 | 86.7% |
| **overall** | **500** | **400** | **80.0%** |

The two categories the CoT prompt moves most are exactly the two the plain-prompt
failure analysis (#579/#580) attributed to reasoning rather than retrieval:
preference **30.0% → 60.0%** and temporal **69.2% → 77.4%** on the primary runs.
`multi-session` improves less (63.2% → 69.2%) because a large share of its
remaining misses are *retrieval* (aggregation questions needing 2–4 sessions) —
tracked as engine work out of #580's case studies, not papered over.

### Plain prompt (run 2, content-hashed `qa_report.json`, non-CoT reference)

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
- **Answer prompt:** both official variants, recorded as `answer_prompt` in
  every report and folded into the run signature (a CoT number can never be
  silently blended with a plain-prompt one): `plain` for the 73.8% rows,
  `official-cot` (`run_generation.py` cot=true, ported verbatim; `qa.py --cot`,
  max_tokens 1200, final `Answer:` line parsed for judging) for the 79.0% rows.
- **Provenance:** plain run 2 signature `929623670d8bcc67…d064345`; CoT runs
  `20327b31b5940f58…` / `7c8ce1b406c0cc4b…` / `eb848e786677a8d1…` — each over
  the per-question verdict set; hardware, elapsed, and config all in the
  respective `qa_report*.json`.

## Caveats (read before quoting the number)

- **Say which prompt variant you are quoting.** 79.0% is the `official-cot`
  mean; 73.8% is the `plain` mean. Both are official LongMemEval prompts, but
  they are different numbers under different (official) conditions — every
  quote must carry its `answer_prompt` label. Zep's publication does not state
  their variant; flag that when comparing, never blend. Anything beyond the two
  official prompts is out of bounds for published numbers.
- **Run-to-run variance.** LLM answering/grading at temperature 0 is still not
  perfectly deterministic. CoT: three independent full runs scored 80.0 / 78.6 /
  78.4 (mean 79.0, spread 1.6 points, stdev 0.9). Plain: 73.6 / 75.0 / 72.8
  (mean 73.8, spread 2.2, stdev 1.1). All six content-hashed reports are committed
  here. Quote the mean with the range, not a single run's number.
- **The CoT primary run was resumed.** `qa_report_cot.json` was produced across
  multiple process invocations via the crash-safe `--resume` journal (one
  interruption was an API-quota outage); the config signature guarantees all
  500 verdicts were produced under the same pinned config, and two
  uninterrupted seeds confirm the number.
- **Zep's conditions are quoted, not verified.** We have not reproduced Zep's own
  run; 63.8% is their published claim. State that when comparing.
- **Preference is still the weakest category (60% CoT / 30% plain).** The CoT
  prompt doubled it, but if a competitor stresses preference specifically, that
  remains our soft spot. Own it.

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
python benchmark/longmemeval/qa.py --yes            # plain official prompt
python benchmark/longmemeval/qa.py --yes --cot      # official CoT prompt (the 79.0% row)

# 5. Independent re-grade with LongMemEval's official evaluator
#    (plain run 2 reproduces 368/500 = 73.60%; the CoT hypotheses file
#    re-grades the 80.0% primary run)
python evaluate_qa.py gpt-4o \
  benchmark/longmemeval/hypotheses-mimir-gpt-4o-2024-08-06.jsonl \
  benchmark/longmemeval/longmemeval_s_cleaned.json
python evaluate_qa.py gpt-4o \
  benchmark/longmemeval/hypotheses-cot-mimir-gpt-4o-2024-08-06.jsonl \
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
