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

Output: a content-hashed (sha256) `report.json` plus a console table. The run is offline and the
metrics are **deterministic run-to-run** across every mode: `fts5` and the RRF
fusion step always were (#247), and the embedding-backed `dense`/`hybrid`/`auto`
modes are now too — the bundled ONNX backend is pinned to single-threaded,
deterministic inference (#310), so the same input yields a byte-identical
embedding (and therefore a byte-identical signature) on every run.

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

<!-- RESULTS-START (filled by the latest full run; see report.json for the content-hashed copy) -->
Full LongMemEval `_s` split: **500 questions, 23,867 sessions, offline** on Windows 11
with the release binary (bundled ONNX embeddings). Fingerprinted (sha256) in `report.json`.

This is the **default user experience after #271**: a bare `mimir_remember` then
`mimir_recall` with no manual `mimir_embed` and no `mode` argument
(`--skip-explicit-embed --modes auto`). `auto` exercises #271's auto-select; the run
skips the explicit embed to prove #271's auto-embed-on-write is what populates the
vectors.

| path | recall@1 | recall@3 | recall@5 | recall@10 | MRR |
|------|---------:|---------:|---------:|----------:|----:|
| keyword only (fts5) | 4.2% | 13.0% | 23.6% | 42.0% | 0.126 |
| **default (auto, post-#271 + #309)** | **84.6%** | **95.2%** | **97.4%** | **99.2%** | **0.903** |
| hybrid (explicit) | 84.6% | 95.2% | 97.4% | 99.2% | 0.903 |

**The headline:** before #271 a bare remember+recall fell back to keyword search, which
finds the right session only **4%** of the time at rank 1 (LongMemEval paraphrases its
questions). #271 makes auto-embed-on-write + hybrid the default, so the same bare calls
now hit **~85% recall@1 / 97% recall@5** with no API key, no cloud, no LLM, and no manual
step. `auto` == `hybrid` to the digit, confirming the default equals the ceiling.
(Standalone dense, measured separately, is 77.0% / 93.8% — so fusing the keyword arm
adds ~8 points of recall@1 over dense alone.) **#309** raised the keyword arm to equal
weight in the RRF fusion (it had been under-weighted at 0.5), lifting the default from
82.2% / 0.884 MRR to the numbers above. These numbers are now reproducible run-to-run
across all modes (deterministic embeddings, #310).

By question type (default/auto recall@1 / recall@5):

| question type | n | recall@1 | recall@5 |
|---|--:|--:|--:|
| single-session-assistant | 56 | 98.2% | 98.2% |
| multi-session | 133 | 90.2% | 98.5% |
| knowledge-update | 78 | 89.7% | 98.7% |
| temporal-reasoning | 133 | 83.5% | 97.0% |
| single-session-user | 70 | 71.4% | 98.6% |
| single-session-preference | 30 | 56.7% | 86.7% |

Equal-weight fusion (#309) improved 5 of the 6 types vs the old 0.5 weight; the small
`single-session-preference` set (n=30) traded down (63.3→56.7 recall@1) as the net
across all 500 rose. Reproduce the default experience:
`python benchmark/longmemeval/run.py --data longmemeval_s_cleaned.json --skip-explicit-embed --modes auto fts5`
(signature `9babb85...`, byte-identical run-to-run now that embeddings are deterministic,
#310). Drop the flags to also measure the explicit dense/hybrid modes.
<!-- RESULTS-END -->

## Stage 2: QA accuracy (pinned answerer + pinned judge — the vs-Zep harness, #475)

`qa.py` is the second stage and the head-to-head-vs-Zep harness. Per question it
ingests the haystack into the real binary, hybrid-retrieves top-k sessions,
answers with a **pinned, named** LLM (default `gpt-4o-2024-08-06`, temperature 0)
and grades with a **pinned, named** judge (default `gpt-4o-2024-08-06`,
temperature 0, strict yes/no prompt committed in `qa.py`). It writes a
content-hashed (sha256) `qa_report.json` (models, split, per-category accuracy, commit, binary, verdict
sha256) plus hypotheses files in LongMemEval's official format so their
`evaluate_qa.py` can cross-check our judge. See [COMPARISON.md](COMPARISON.md)
for the comparison rules and the scoreboard vs Zep's published 63.8%.

Systems: `fullcontext` (all ~48 sessions), `mimir` (top-k hybrid-retrieved sessions,
the default), `oracle` (gold sessions only, upper bound).

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

**Accuracy (needs a named LLM + judge; opt-in, NOT in any CI gate).** To produce it:

```bash
export OPENAI_API_KEY=...        # or put the key in ~/.openai_key; OPENAI_BASE_URL optional

# Free plumbing check (no key, no network): stubbed answerer+judge, real ingest+retrieval
python benchmark/longmemeval/qa.py --mock-llm --limit 5

# Cheap paid smoke run
python benchmark/longmemeval/qa.py --limit 10

# Full 500-question head-to-head number (prints a cost estimate, requires --yes)
python benchmark/longmemeval/qa.py --yes
```

Defaults: answerer `gpt-4o-2024-08-06`, judge `gpt-4o-2024-08-06`, split `_s`,
hybrid k=10 — override with `--model` / `--judge` / `--split` / `--k` / `--limit`
(all recorded in the report). Optionally cross-check with LongMemEval's official
judge by feeding the emitted `hypotheses-<system>-<model>.jsonl` to their
`src/evaluation/evaluate_qa.py`. Run every system through the **same** models,
and report the LLM and judge by name beside the number (see COMPARISON.md).

## Chain-of-thought answer prompt (#579)

LongMemEval ships **two** official answer prompts: a direct one (the default
here) and a chain-of-thought variant (`run_generation.py cot=true`). The
retrieval diagnostic below attributed the majority of consistent QA failures to
*reasoning over correctly-retrieved evidence*, not retrieval — the class CoT
addresses. Run the official CoT prompt with `--cot`:

```bash
python qa.py --data longmemeval_s_cleaned.json --cot --k 10 --yes \
    --journal cot_full.jsonl --out cot_report.json
```

`--cot` uses LongMemEval's own CoT prompt (still 100% official methodology),
raises the answer `max_tokens` to 1200, and parses the final `Answer:` line for
judging. Both the journal `_config` and the report carry
`answer_prompt: "official-cot"` (vs `"plain"`), and the run signature includes
it — a CoT number can never be silently blended with a plain-prompt one. Use
`--only-types single-session-preference multi-session temporal-reasoning` to run
a weak-category slice cheaply. **A single run's number is never quoted alone**
(see the honesty notes) — confirm with ≥2 seeds and, ideally, an independent
re-grade via the authors' `evaluate_qa.py` on the emitted `hypotheses-*.jsonl`.

## Retrieval coverage diagnostic (offline, judge-free — #580)

`retrieval_diag.py` isolates the *retrieval* half of QA failure with **no LLM,
no judge, no API cost**. It replays the identical benchmark ingest + hybrid
recall for every question at a deep top-K and records where each gold evidence
session ranked, then reports gold-evidence **coverage@k**:

```bash
python retrieval_diag.py --data longmemeval_s_cleaned.json \
    --bin ../../target/release/perseus-vault --k 50 --out diag_report.json
```

It also works as a **coverage regression guard** — fail CI when recall drops:

```bash
python retrieval_diag.py --data longmemeval_s_cleaned.json \
    --bin ../../target/release/perseus-vault --min-coverage-at 20:0.95
```

`--min-coverage-at K:FRAC` exits non-zero if coverage@K falls below the floor.
The report buckets misses into **k-recoverable** (gold ranked 11–K, a deeper k
would catch them) and **hard** (a gold session absent from the top-K entirely —
the interesting engine cases for multi-query / aggregation-aware retrieval).

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
