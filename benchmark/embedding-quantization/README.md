# Embedding-quantization ladder (#630)

Documents what changing embedding precision costs in retrieval quality. The
benchmark separates two orthogonal precision axes, measures the one that ships
today, and records the pending rows as status stubs rather than fabricated
numbers. Every figure below is extracted verbatim from a signed 1M artifact and
lives in the committed `report.json`; nothing here is estimated.

## Two axes

1. **Index / signature quantization** — how a stored embedding is *compressed*
   for the dense prefilter. The engine keeps three tiers of the same vector:
   full f32 (`embedding`, exact cosine, 32 bits/dim, 1x), int4 (`emb_sig4`,
   4-bit ADC signature, 8x), and 1-bit (`emb_sig`, sign signature scored by
   Hamming, 32x). **Measured** at 1M — this is the ladder below.
2. **Model-weight quantization** — how the embedding is *generated* (the model
   weights used at embed time): INT8 (shipped) → FP16/BF16 → NVFP4. See
   [Model-weight axis](#model-weight-axis).

## Index-quantization ladder (measured, 1M)

Corpus: 1M synthetic (995,562 rows persisted, 10,000 clusters × 100,
nomic-768 dim), 2× H100 SXM5, 500 queries, `uniform` query set,
`max_scan` 50,000.

**The honest signal is standalone dense recall.** The hybrid keyword arm
saturates to ~1.0 recall@5 at every tier because it nails the synthetic cluster
markers, so hybrid recall carries no quantization information. Dense recall
isolates what the compressed vector actually preserves.

| Tier | compression | bits/dim | dense r@1 | r@5 | r@10 | dense p50 | source artifact |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `full_f32_exact_cosine` | 1x | 32 | 0.486 | 0.684 | 0.754 | 650.1 ms | `scale1m_exact_ceiling.json` |
| `int4_sig4_coarse` | 8x | 4 | 0.497 | 0.722 | 0.791 | 395.6 ms | `scale1m_2c_on.json` |
| `1bit_sig_prefilter_rerank` | 32x | 1 | 0.514 | 0.726 | 0.800 | 194.5 ms | `scale1m_default_500.json` (**shipped default**) |
| `pure_1bit_hamming_only` | 32x | 1 | 0.132 | 0.312 | 0.412 | 184.3 ms | `scale1m_pure1bit.json` |

### Headline: more quantization gives both higher recall and lower latency

Counterintuitive, and all of it measured and signed: moving *down* the ladder
(more aggressive index quantization) improves standalone dense recall *and*
cuts latency monotonically. The 1-bit prefilter (0.726 r@5 @ 194.5 ms) beats
full-precision exact cosine (0.684 r@5 @ 650.1 ms) at every k.

The mechanism is denoising, and the rerank-ablation below pins where it happens.
The 1-bit sign code discards magnitude, so as a *filter* it drops cross-cluster
confusers that full-precision cosine over-ranks — giving a cleaner candidate
**set** than an unbounded exact scan. But the sign code is a poor *ranker* on its
own (see [rerank ablation](#the-rerank-does-the-denoising)); the exact-cosine
rerank over the 1-bit-selected pool restores the order. The net — better set +
exact rerank — is what beats the f32 ceiling, at 32x less index memory:
~96 bytes/vec versus ~3072 at 768-dim (int4 sits at ~384), i.e. ~96 MB resident
for the 1-bit index at 1M.

`gate.py` locks this in: 1-bit dense r@5 ≥ exact − `EPS` (0.01); p50 ordering
1-bit < int4 < exact; and the reranked default r@5 ≥ pure-prefilter r@5 + 0.2.

### Why standalone dense is the honest signal

Retrieval in production runs the hybrid path (dense prefilter + keyword arm,
fused). But on this corpus the keyword arm resolves the synthetic cluster
markers on its own, so hybrid recall reads ~1.0 (r@5 = 1.0, r@1 in the
0.93–0.96 range) across every tier and is blind to the quantization change
under test. Standalone dense recall — the dense arm alone, no keyword help — is
the only column that moves with the compression tier, so it is the column the
ladder and the gate read.

## Model-weight axis

| Model precision | status | note |
| --- | --- | --- |
| INT8 | **shipped** | bundled **local default**: all-MiniLM-L6-v2 qint8 (384-dim), used for the offline/default path and the repo's 100K + LongMemEval numbers — **not** the model behind the 1M ladder above (that used the nomic-768 endpoint model) |
| FP16 / BF16 | measurement pending | re-embed with the full-precision ONNX export and rerun the harness (cheap at 100K local) |
| NVFP4 | deferred → #629 | needs Blackwell-class hardware; the open question (a larger embedder @ NVFP4 vs MiniLM-FP32 at equal memory) is tracked in #629 |

The two axes are orthogonal. The index tiers (f32/int4/1-bit) are derived from
whatever vector is stored, so the ladder above isolates the index axis on a
*fixed* base model — but that base model is **nomic-768** (the endpoint model
that embedded the 1M corpus), not the bundled INT8 default. Whether the same
denoising holds for the bundled MiniLM-384 is plausible but unmeasured; a
FP16/BF16 or MiniLM re-run of the ladder is the (pending) model-weight axis.

## The rerank does the denoising {#the-rerank-does-the-denoising}

`pure_1bit_hamming_only` is the shipped 1-bit tier with the phase-2 exact-cosine
rerank turned off (`DenseOpts.rerank` / `MIMIR_DENSE_SIG_RERANK=0`; default ON,
default path byte-identical). Measured on the same 1M corpus/instance:

| | dense r@1 | r@5 | r@10 | p50 |
| --- | --- | --- | --- | --- |
| 1-bit prefilter **+ rerank** (shipped) | 0.514 | **0.726** | 0.800 | 194.5 ms |
| 1-bit prefilter, **no rerank** | 0.132 | **0.312** | 0.412 | 184.3 ms |

Turning the rerank off saves ~10 ms and collapses recall (0.726 → 0.312 r@5).
So the sign code is a strong candidate *filter* but a weak *ranker* — the
exact-cosine rerank over the 1-bit-selected pool is what does the heavy lifting.
The rerank is nearly free relative to what it buys: keep it on (it is, by
default). This is why the shipped tier, not the raw 1-bit order, is the one that
beats the f32 ceiling.

For a **local, small-corpus** complement to this 1M number, `measure_1bit_small.py`
(ONNX-direct, bundled MiniLM-384) measures the same pure-1-bit-vs-cosine question
without Lambda — useful as a CI-able sanity check on the effect at small scale.

## Reproduce

```bash
python benchmark/embedding-quantization/aggregate.py   # regenerate report.json
python benchmark/embedding-quantization/gate.py        # assert the invariants
```

`aggregate.py` reads the three signed 1M source artifacts from
`benchmark/lambda/results/`, extracts the `uniform` dense and hybrid rows
verbatim, and writes `report.json`. Integrity is per-source: each artifact's
raw-bytes SHA-256 is recorded in `report.json`'s `provenance` block (rather than
a cross-language self-signature). Re-running on the same artifacts reproduces
identical rows and hashes on any machine; a row is trustworthy exactly as far as
its cited source file still hashes to the recorded value.

`gate.py` reads the committed `report.json` and exits non-zero on any violation
of: (1) denoising — 1-bit dense r@5 ≥ exact − 0.01; (2) latency ordering —
1-bit p50 < int4 p50 < exact p50; (3) rerank essential — reranked default r@5 ≥
pure-prefilter r@5 + 0.2; (4) provenance — every measured tier cites a source
with a recorded SHA-256.

The `pure_1bit_hamming_only` row is reproduced by rerunning the retrieval harness
on the embedded corpus with `MIMIR_DENSE_SIG_RERANK=0` (`scale1m_pure1bit.json`).

## Source artifacts

All three are `sig619` tiers, `gpu_2x_h100_sxm5`, 1M reused corpus on local
NVMe, 995,562 rows persisted, `max_scan` 50,000. SHA-256 in `report.json`.

| Artifact | tier |
| --- | --- |
| `scale1m_exact_ceiling.json` | `sig619-EXACT-500q` |
| `scale1m_2c_on.json` | `sig619-2c-ON` |
| `scale1m_default_500.json` | `sig619-DEFAULT-500q` |
