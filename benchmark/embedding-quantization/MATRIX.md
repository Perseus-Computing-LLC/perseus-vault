# Recommendation Matrix — Embedding Quantization (#630)

**Acceptance item:** _"Recommendation matrix: use case -> recommended quantization."_

Every number below traces to [`report.json`](./report.json). The measured ladder was run
on a **1M-row corpus** (995,562 rows persisted, 10,000 clusters × 100, nomic-768 dim), on
**2× H100 SXM5**, over **500 queries**, uniform arm. The reported signal is **standalone
dense recall** — the honest quantization signal, because the hybrid keyword arm saturates
near 1.0 on the synthetic cluster markers and therefore carries no quantization information.

---

## TL;DR — most users need no tuning

The **shipped default** (1-bit signature prefilter + exact-cosine rerank) is **both the
recall winner and the latency winner** at 1M rows. It beats full-precision exact cosine on
every k *and* runs ~3.3× faster:

| | dense r@5 | dense p50 | index memory |
|---|---|---|---|
| full f32 exact cosine (ceiling) | 0.684 | 650.1 ms | ~3072 B/vec |
| **1-bit prefilter + rerank (SHIPPED DEFAULT)** | **0.726** | **194.5 ms** | ~96 B/vec (~96 MB @1M) |

The 1-bit sign code drops cross-cluster cosine confusers (a denoising effect), so more
aggressive index quantization here yields **higher** standalone-dense recall at **lower**
latency and **32× less** index memory. There is no accuracy-for-speed tradeoff to tune
against for the general case — leave it on.

---

## Index / signature quantization ladder (measured basis)

| Tier | column | compression | bits/dim | dense r@1 | dense r@5 | dense r@10 | dense p50 | source artifact |
|---|---|---|---|---|---|---|---|---|
| full_f32_exact_cosine | `embedding` | 1× | 32 | 0.486 | 0.684 | 0.754 | 650.1 ms | `scale1m_exact_ceiling.json` |
| int4_sig4_coarse | `emb_sig4` | 8× | 4 | 0.497 | 0.722 | 0.791 | 395.6 ms | `scale1m_2c_on.json` |
| **1bit_sig_prefilter_rerank** | `emb_sig` | 32× | 1 | 0.514 | 0.726 | 0.800 | 194.5 ms | `scale1m_default_500.json` **(SHIPPED DEFAULT)** |
| pure_1bit_hamming_only | `emb_sig` | 32× | 1 | 0.132 | 0.312 | 0.412 | 184.3 ms | `scale1m_pure1bit.json` (prefilter, **no rerank**) |

All four rows are content-hashed (SHA-256 in `report.json` → `provenance`); `max_scan =
50000`, `persisted = 995562` for each. Note the last row: the 1-bit prefilter
*without* the exact-cosine rerank collapses to 0.312 r@5 — the rerank, not the
1-bit ranking, is what earns the shipped tier's 0.726 (never disable it).

---

## Recommendation matrix: use case → recommended quantization

| Use case | Recommended tier | Measured basis (r@5 / p50) | Why (one line) |
|---|---|---|---|
| **Default / general agent memory** | **1-bit prefilter + rerank** (SHIPPED DEFAULT) | 0.726 @ 194.5 ms | Wins recall *and* latency at 1M — the denoising 1-bit prefilter beats the f32 ceiling; no tuning needed. |
| **Latency-critical / high-QPS** | 1-bit prefilter + rerank (SHIPPED DEFAULT) | 0.726 @ 194.5 ms | Lowest measured p50 (194.5 ms, p99 198 ms) with no recall sacrifice — already the fastest arm. |
| **Memory-constrained / edge** | 1-bit prefilter + rerank (SHIPPED DEFAULT) | 0.726 @ 194.5 ms | 32× compression, ~96 B/vec (~96 MB resident @1M) vs ~3072 B/vec for f32, at the best recall. |
| **Maximum-recall / audit** | 1-bit prefilter + rerank (SHIPPED DEFAULT) | 0.726 @ 194.5 ms | Highest measured dense recall at every k (r@1 0.514, r@5 0.726, r@10 0.800) — exceeds the f32 exact-cosine ceiling. |
| **Small corpora (< a few thousand rows)** | full f32 exact cosine (implicit) | ceiling reference 0.684 @ 650.1 ms | The signature prefilter does not engage below its threshold, so retrieval is already exact cosine over the full `embedding` column — nothing to tune; latency is a non-issue at this scale. |

Notes on the matrix:

- The 1-bit default is the recommendation for four of the five rows because the measured
  ladder shows it dominating on all three of the axes users normally trade against
  (recall, latency, memory) at 1M. This is the counterintuitive headline result: more
  aggressive index quantization is strictly better here, not a compromise.
- **int4_sig4_coarse** (0.722 @ 395.6 ms) is a real, measured, in-between rung — useful as
  a diagnostic or if a future workload shows the 1-bit rerank underperforming — but it is
  strictly dominated by the 1-bit default on both r@5 and p50 in this corpus, so it is not
  a first-line recommendation.
- The **small-corpora** row is the one genuinely distinct case: because the signature
  prefilter never activates, the effective tier is exact f32 cosine regardless of setting.
  The 650.1 ms ceiling latency is measured at 1M and is not representative of a few-thousand
  -row corpus, where exact cosine is trivially fast.

---

## Second axis: model-weight quantization (how embeddings are generated)

Orthogonal to the index/signature axis above. This governs how the vectors themselves are
produced, not how they are stored/prefiltered.

| Model-weight tier | Status | Note |
|---|---|---|
| **INT8** | **shipped** | Bundled **local default**: `all-MiniLM-L6-v2` qint8 (384-dim), used for the offline/default path + the repo's 100K/LongMemEval numbers. The 1M ladder above was measured on the nomic-768 endpoint model, not this default. |
| FP16 / BF16 | measurement pending | Re-embed with the full-precision ONNX export + rerun the harness (cheap at 100K local). |
| NVFP4 | deferred (#629) | Needs Blackwell-class hardware; the open question (larger embedder @ NVFP4 vs MiniLM-FP32 at equal memory) is tracked in #629. |

The measured ladder is a fair, apples-to-apples comparison — the index axis is
isolated cleanly on top of a *fixed base model* (nomic-768) across all three
tiers. Re-running the ladder on the bundled MiniLM-384 or on FP16-generated
vectors is the separate, pending model-weight axis.

---

## Measured: the rerank is what matters (do not disable it)

`DenseOpts.rerank` / env `MIMIR_DENSE_SIG_RERANK=0` (default **ON**, default path
byte-identical) skips the phase-2 cosine rerank. Measured on the 1M corpus, the pure
1-bit prefilter scores **0.312 r@5** vs the shipped **0.726** — turning the rerank off
saves ~10 ms and less-than-halves recall. The sign code is a strong candidate *filter*
but a weak *ranker*; the exact-cosine rerank over the 1-bit-selected pool does the heavy
lifting. Recommendation: never set `MIMIR_DENSE_SIG_RERANK=0` outside benchmarking.
