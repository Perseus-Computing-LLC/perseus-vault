# Head-to-head vs Zep: LongMemEval end-to-end QA (#475)

This page holds the one number that answers "are we better than Zep?" — and the
exact conditions it was produced under. **No number goes in this table without
its conditions.** The deprecated [`benchmarks/LONG_MEM_EVAL.md`](../../benchmarks/LONG_MEM_EVAL.md)
is the precedent: the old end-to-end claims were retracted because they cited an
unnamed/nonexistent model and mixed splits and judges. We do not do that again.

## The scoreboard

| system | LongMemEval QA accuracy | answerer | judge | split | source |
|---|---:|---|---|---|---|
| Zep | 63.8% (published) | "GPT-4o" (snapshot not stated) | not stated | LongMemEval (split as published) | Zep's published claim, cited in #475 |
| Mem0 | 49.0% (published) | "GPT-4o" (snapshot not stated) | not stated | LongMemEval (split as published) | published claim, cited in #475 |
| **Perseus Vault** | **TBD — not yet run** | `gpt-4o-2024-08-06` (pinned) | `gpt-4o-2024-08-06` (pinned) | `longmemeval_s` (500 instances) | `qa_report.json` (signed), this repo |

> Before publishing our number next to Zep's, pin down the primary source of the
> 63.8% (paper/blog table) and record which split and answerer snapshot Zep used.
> The numbers above are quoted from issue #475 as stated.

## Exact-conditions statement (fill in when the run lands)

When we publish our number, it is accompanied by ALL of:

- **Split:** `longmemeval_s` — 500 instances, ~48 sessions per haystack. Same
  family Zep reports on; confirm their exact split (500 `_s` vs any stratified
  subset) before calling it same-split.
- **Answerer:** `gpt-4o-2024-08-06`, temperature 0. This is the closest pinnable
  snapshot to Zep's "GPT-4o" claim; if Zep used a different snapshot, the
  comparison carries a "close but not identical answerer" caveat.
- **Judge:** `gpt-4o-2024-08-06`, temperature 0, with the strict yes/no judge
  prompt committed verbatim in [`qa.py`](qa.py) (`JUDGE_PROMPT` /
  `JUDGE_PROMPT_ABSTAIN`). **Judge caveat:** our judge prompt is not LongMemEval's
  official `evaluate_qa.py` prompt set and is almost certainly not Zep's judge
  either. Judges differ across papers; that alone can move a score several
  points. The harness also emits hypotheses files in LongMemEval's official
  format so anyone can re-grade our answers with the official judge.
- **Retrieval:** perseus-vault hybrid recall, top-k 10 (configurable, recorded
  in the report), bundled ONNX embeddings, real binary over MCP stdio.
- **Provenance:** commit SHA, binary version, hardware, and a sha256 signature
  over the per-question verdict set — all inside `qa_report.json`.

## The comparison rules (from #475, non-negotiable)

- Report our accuracy **on the same split Zep reports** (the 500-instance `_s`,
  or the 102-instance stratified subset — pick one and state which).
- If we can run GPT-4o as the answerer (matching Zep), do so and put the numbers
  side by side.
- If we can only run a different model, report BOTH ours and Zep's with a bold
  **"different answerer model — not directly comparable"** caveat. No silent
  apples-to-oranges.
- Temporal reasoning is where the bi-temporal engine should shine — break it out
  (the report's `by_question_type` does). If we beat or match Zep on the temporal
  subset, that becomes the headline claim, with the reproduce command linked.

## Reproduce

```bash
# 0. Get the binary (any of): cargo build --release, or a release binary
# 1. Get the dataset (public, 277 MB)
curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o benchmark/longmemeval/longmemeval_s_cleaned.json

# 2. Free plumbing check (no key): stubbed answerer+judge, real ingest+retrieval
python benchmark/longmemeval/qa.py --mock-llm --limit 5

# 3. Cheap paid smoke (needs OPENAI_API_KEY or ~/.openai_key)
python benchmark/longmemeval/qa.py --limit 10

# 4. The full number (500 questions; prints a cost estimate, requires --yes)
python benchmark/longmemeval/qa.py --yes
```

Defaults are the pinned models above; `--model`, `--judge`, `--split`, `--k`,
`--limit` override (every override is recorded in `qa_report.json`). This run is
**opt-in and NOT part of any CI gate** — it costs real money (estimate printed
upfront; roughly $28 for the full 500-question mimir-only run at k=10 and
2026-07 GPT-4o pricing, ~$15 at k=5, ~$165 if you add the fullcontext and
oracle baselines — dominated by answerer context tokens).

## Related numbers (do not conflate)

- **Session-level retrieval recall** ([`README.md`](README.md), `report.json`):
  recall@1 0.846 / recall@10 0.992, fully offline, judge-free. That is a
  *retrieval* metric — never present it as QA accuracy.
- **Token efficiency** (`qa.py --dry-run`): mimir feeds ~8x fewer tokens than
  full-context stuffing at k=5. Offline and reproducible.
