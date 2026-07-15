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
| `pure_1bit_hamming_only` | 32x | 1 | — pending — | | | | `MIMIR_DENSE_SIG_RERANK=0` (#630) |

### Headline: more quantization gives both higher recall and lower latency

Counterintuitive, and all of it measured and signed: moving *down* the ladder
(more aggressive index quantization) improves standalone dense recall *and*
cuts latency monotonically. The 1-bit prefilter (0.726 r@5 @ 194.5 ms) beats
full-precision exact cosine (0.684 r@5 @ 650.1 ms) at every k.

The mechanism is denoising. The 1-bit sign code discards magnitude, which
drops cross-cluster cosine confusers that full-precision cosine ranks highly —
so the coarser code is not just cheaper, it is a *cleaner* prefilter. It does
this at 32x less index memory: ~96 bytes/vec versus ~3072 at 768-dim
(int4 sits at ~384), i.e. ~96 MB resident for the 1-bit index at 1M.

`gate.py` locks this ordering in: 1-bit dense r@5 must stay within `EPS` (0.01)
below exact (it is above), and p50 must satisfy 1-bit < int4 < exact.

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

## The pending 1-bit row

`pure_1bit_hamming_only` is the shipped 1-bit tier with the phase-2 exact-cosine
rerank turned off — Hamming distance on the sign code only. It upper-bounds what
the rerank buys. The harness is ready via `DenseOpts.rerank` /
`MIMIR_DENSE_SIG_RERANK=0` (default ON; the default path is byte-identical, this
is opt-in), but the number is not yet measured — it needs a real embedded corpus
(next Lambda run or a local model), so it is recorded as
`harness_ready_measurement_pending` rather than fabricated.

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
1-bit p50 < int4 p50 < exact p50; (3) provenance — every measured tier cites a
source with a recorded SHA-256.

To measure the pending `pure_1bit_hamming_only` row, run the retrieval harness
against an embedded corpus with `MIMIR_DENSE_SIG_RERANK=0`.

## Source artifacts

All three are `sig619` tiers, `gpu_2x_h100_sxm5`, 1M reused corpus on local
NVMe, 995,562 rows persisted, `max_scan` 50,000. SHA-256 in `report.json`.

| Artifact | tier |
| --- | --- |
| `scale1m_exact_ceiling.json` | `sig619-EXACT-500q` |
| `scale1m_2c_on.json` | `sig619-2c-ON` |
| `scale1m_default_500.json` | `sig619-DEFAULT-500q` |
