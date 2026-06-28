# Mimir on LongMemEval (session-level retrieval, offline & judge-free)

A **reproducible, fully offline** measurement of how well Mimir retrieves the
right memory on the public [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
benchmark. It reports **session-level recall@k** against LongMemEval's own
`answer_session_ids`, across Mimir's three search modes (fts5 keyword, dense
vector, hybrid RRF). No API key, no network, no LLM. Anyone can re-run it and get
the same number.

## What this measures (and what it does not)

LongMemEval has two stages:

1. **Retrieval** — given a question and a haystack of ~48 chat sessions (~46
   distractors + ~2 evidence sessions), surface the evidence. The official metric
   is **session-level recall** vs `answer_session_ids`. This is judge-free and
   deterministic. **This is what this harness measures.**
2. **QA accuracy** — feed the retrieved context to an LLM and judge the answer
   with another LLM. That stage needs an LLM + a judge model, so it is **not**
   offline or deterministic, and the score depends entirely on which models you
   pick. **This harness deliberately does not produce a QA number** (see "Honesty"
   below).

Mimir's pitch is local-first, so its credibility benchmark is the half that needs
no cloud: retrieval quality you can reproduce on your own machine.

## Run it

```bash
# 1. Build mimir (bundled embeddings are on by default)
cargo build --release

# 2. Get the real LongMemEval _s split (500 instances, ~48 sessions each, 277 MB)
curl -L https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json \
  -o longmemeval_s_cleaned.json

# 3. Run (full 500; use --max-instances N for a quick subset)
python benchmark/longmemeval/run.py --data longmemeval_s_cleaned.json
```

Output: a signed `report.json` plus a console table. The run is offline and the
metrics are deterministic run-to-run (fts5 and dense always were; hybrid RRF is
byte-stable per #247).

## Method

- One memory per session (`key` = session id, body = the session's turns flattened
  as `role: content`), namespaced by question id.
- Dense vectors populated with the **bundled** ONNX model (all-MiniLM-L6-v2, 384-d),
  in-process, offline.
- Each question is queried scoped to its own haystack (via the `category` filter),
  so retrieval competes only against that instance's ~48 sessions, exactly the
  LongMemEval-s setting.
- Process-per-instance with a fresh DB keeps each instance's store tiny and the
  isolation exact.
- `recall@k` = the gold evidence session appears in the top k; `MRR` = reciprocal
  rank of the first gold session. Reported overall and broken down by the six
  LongMemEval question types.

## Results

<!-- RESULTS-START (filled by the latest full run; see report.json for the signed copy) -->
Full LongMemEval `_s` split: **500 questions, 23,867 sessions, offline, 440s** on
Windows 11 with the release binary (bundled ONNX embeddings). Signed in `report.json`.

| mode | recall@1 | recall@3 | recall@5 | recall@10 | MRR |
|------|---------:|---------:|---------:|----------:|----:|
| fts5 (keyword) | 4.2% | 13.0% | 23.6% | 42.0% | 0.126 |
| dense (semantic) | 77.0% | 90.0% | 93.8% | 97.2% | 0.843 |
| **hybrid (RRF)** | **82.2%** | **93.4%** | **97.0%** | **98.6%** | **0.884** |

**The headline:** keyword search alone finds the right session only 4% of the time
at rank 1 (LongMemEval paraphrases its questions, so lexical matching fails). Mimir's
**bundled, offline** semantic + hybrid retrieval lifts that to **82% recall@1 and 97%
recall@5** with no API key, no cloud, no LLM. The whole reason the bundled embedding
model exists is on display here.

By question type (hybrid recall@1 / recall@5):

| question type | n | recall@1 | recall@5 |
|---|--:|--:|--:|
| single-session-assistant | 56 | 94.6% | 98.2% |
| multi-session | 133 | 89.5% | 98.5% |
| knowledge-update | 78 | 87.2% | 98.7% |
| temporal-reasoning | 133 | 82.0% | 97.7% |
| single-session-preference | 30 | 63.3% | 93.3% |
| single-session-user | 70 | 61.4% | 91.4% |

Reproduce: `python benchmark/longmemeval/run.py --data longmemeval_s_cleaned.json`
(signature `f82bee43...`; deterministic run-to-run).
<!-- RESULTS-END -->

## Stage 2: QA accuracy (answer generation + LongMemEval's official judge)

`qa.py` is the second stage. It feeds context to a named LLM, writes answers in
LongMemEval's hypothesis format, and is graded by **LongMemEval's own judge** (we do
not invent one). It runs every system through the **same** model at temperature 0.

Systems: `fullcontext` (all ~48 sessions), `mimir` (top-k hybrid-retrieved sessions),
`oracle` (gold sessions only, upper bound).

**Token efficiency (offline, no key needed, `--dry-run`).** On 50 `_s` instances,
k=5 retrieval:

| system | avg sessions/q | relative context | 
|---|--:|--:|
| fullcontext | 48.7 | 8.0x (baseline) |
| **mimir (k=5)** | **5.0** | **1.0x (8.0x less)** |
| oracle | 1.0 | ~34x less |

**Mimir feeds the LLM ~8x fewer tokens than dumping the whole history** — and from the
retrieval result above, hybrid recall@5 is 97%, so those 5 sessions contain the evidence
almost every time. (Token counts use tiktoken when present, else a ~4-chars/token
estimate; the *ratio* is tokenizer-independent. This is the honest, reproducible version
of the deprecated doc's "82x fewer tokens" claim.)

**Accuracy (needs a named LLM + judge).** Not run here (kept honest — no number without
named models). To produce it:

```bash
export OPENAI_API_KEY=...        # OPENAI_BASE_URL optional (OpenAI / OpenRouter / local)
python benchmark/longmemeval/qa.py --data longmemeval_s_cleaned.json \
  --systems fullcontext mimir --model <named-model> --k 5
# then grade each hypotheses file with LongMemEval's OFFICIAL judge:
#   cd <LongMemEval>/src/evaluation
#   python3 evaluate_qa.py <judge-model> hypotheses-<system>-<model>.jsonl ../../data/longmemeval_oracle.json
#   python3 print_qa_metrics.py <judge-model> hypotheses-<system>-<model>.jsonl.log ../../data/longmemeval_oracle.json
```

Run every system's hypotheses through the **same** judge model, and report the LLM and
judge by name beside the number.

## Honesty notes (read before quoting a number)

- This is a **retrieval** number, not end-to-end QA accuracy. Do not compare it to
  papers' QA-accuracy tables. Compare it only to other systems' **session-level
  recall** on LongMemEval-s.
- QA-accuracy comparisons across papers use different LLMs and judges and are not
  apples-to-apples. If we ever publish a QA number, it must name the exact LLM +
  judge and run every baseline through the identical models on the identical split.
- Mimir's headline mode is **hybrid** (it fuses keyword + vector). Report all three
  modes; do not cherry-pick.
- The `_s` split is the retrieval-stressing one (distractors present). The `oracle`
  split contains only evidence sessions, so retrieval recall there is trivially ~1.0
  and meaningless; do not benchmark retrieval on oracle.

## Supersedes

This replaces the earlier `benchmarks/LONG_MEM_EVAL.md`, whose numbers were not
reproducible (they cited a model that does not exist and mixed LLMs/judges/splits
in a single comparison table). Use this harness instead.
