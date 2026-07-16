# NVFP4-quantized embedding models — feasibility (#629)

Research memo. Companion to the model-weight axis in [`README.md`](./README.md)
and the measured index-quantization ladder ([`report.json`](./report.json), #630).
No GPU was spent for this; it decides *whether* GPU spend is warranted and on
what experiment.

## The question, corrected

The issue's original premise — *NVFP4-quantize the embedding model to cut its
VRAM ~2× with minimal quality loss* — **does not apply to what Vault ships.**

- Vault's default embedder is **all-MiniLM-L6-v2 qint8**: 22M params, 384-dim,
  ~23 MB int8 (~80 MB fp32), run on the **host CPU via bundled ONNX**. It uses
  **zero GPU VRAM** in the default path. There is no VRAM to save, and a 22 MB
  model is not the runtime bottleneck NVFP4 targets.
- **NVFP4 is a Blackwell-only GPU format** (E2M1 4-bit float, 16-value
  micro-blocks, FP8 block scale + FP32 tensor scale), accelerated by tensor
  cores that **do not exist on Hopper (H100) or Ampere (A100)** — i.e. not on
  the Lambda fleet we benchmark on. It is a *serving-side* optimization for
  large decoder models, delivered through SGLang/vLLM + NVIDIA ModelOpt
  (`--quantization modelopt_fp4` / `nvfp4_online`).

So NVFP4 is architecturally orthogonal to Vault's local-first, single-binary,
CPU-ONNX default. It only enters the picture for an **endpoint** deployment
(`--embedding-endpoint`) serving a large model on a Blackwell GPU — the opposite
of the bundled path.

Two more premise checks:
- The cited `r=0.924` correlation (humans& "The 4-bitter Lesson") is for **RL
  multi-turn tool-use rollouts judged by an LLM**, not embedding retrieval — it
  does not transfer to recall@k. The transferable evidence is NVFP4's **~1%
  accuracy degradation on LLM PTQ** (e.g. DeepSeek-R1 FP8→NVFP4), which is
  suggestive but still unmeasured for retrieval.
- NVFP4 serving support today is documented for **decoder LLMs**, not BERT-family
  encoders. MiniLM/BGE are encoders; the strong large embedders
  (Qwen3-Embedding, NV-Embed) are **decoder-LLM-based**, so they are the natural
  NVFP4 candidates — which conveniently is the same family the reframe points at.

## The reframe (the version worth funding)

Per the #630 grounding: the NVFP4-worthy question is **not** compression of the
current model, it is a *capability* question —

> Does a **larger embedder at 4-bit** beat **MiniLM at full precision** for
> Vault's retrieval quality, at a comparable resource budget?

Quality headroom is real. On MTEB:

| Model | params | dim | MTEB (avg) | path |
| --- | --- | --- | --- | --- |
| all-MiniLM-L6-v2 (shipped) | 22M | 384 | ~56.0 | bundled CPU ONNX |
| Qwen3-Embedding-0.6B | 0.6B | up to 1024 (MRL) | ~64.3 | decoder LLM → NVFP4-able |
| BGE-M3 | 0.6B | 1024 | ~64.6 | encoder |
| Qwen3-Embedding-8B | 8B | 4096 | ~75 | decoder LLM → NVFP4-able |

A larger model offers **+8 MTEB or more** over MiniLM. The catch is resource:
Qwen3-Embedding-0.6B is ~1.2 GB fp16, ~0.3 GB at NVFP4 — even quantized it is
~4× MiniLM-int8's footprint **and needs a GPU**. So "equal memory" is not
literally achievable against a 22 MB CPU model; the honest axis is
**quality-per-resource**, and NVFP4 is the lever that makes a *bigger, better*
model affordable to *serve* — it is not a way to shrink the local default.

## Recommendation (go / no-go)

1. **No** to NVFP4 for the bundled/local default. Irrelevant to a 22M CPU model
   with no VRAM, and it contradicts the local-first/single-binary contract.
   Consider that sub-question closed.
2. **Skip** implementation-sketch item 4 (auto-detect NVFP4 hardware) — premature
   until a larger model has proven a quality lift worth deploying.
3. **Quality before quantization.** The gating experiment is the reframe, and it
   is **not blocked on Blackwell**: measure whether a larger embedder materially
   lifts Vault's retrieval recall *first*, served at fp16/int8 on existing
   hardware. NVFP4 only becomes worth GPU spend *after* a bigger model earns its
   keep — at which point it is a Blackwell serving-economics question.

## Concrete first experiment (no Blackwell required)

- **Candidate:** Qwen3-Embedding-0.6B (fp16 or int8), decoder-based (so an NVFP4
  serving path exists later), MRL dims for a fair index-size comparison.
- **Baselines:** the shipped MiniLM-L6-v2 qint8 (384-dim) and nomic-embed-text
  768-dim (already the 1M endpoint model, so the #619/#630 corpus is reusable).
- **Harness:** `benchmark/longmemeval/run.py` retrieval-only + `benchmark/scale`
  via `--embedding-endpoint` pointed at the candidate; recall@1/5/10 + MRR at
  100K, with per-query latency and index bytes/vector recorded.
- **Success gate:** a material recall lift over nomic-768/MiniLM that justifies
  the larger runtime. Only on a pass does NVFP4 (Blackwell, `nvfp4_online` via
  SGLang) get measured — expected recall delta ≤ ~1% per PTQ evidence, with the
  throughput/$ win as the payoff.

This keeps GPU spend gated behind a cheap, decisive quality measurement, exactly
as the #630 plan-of-record intended.

## Measured result (2026-07-15): the naive larger-embedder swap loses

Ran the gating experiment at **1M** (not just 100K) — re-embedded the reused
corpus with **Qwen3-Embedding-0.6B (fp16)** and measured recall against the
nomic-768 baseline on the same entities / clusters / queries / shipped-default
recall config (2× H100, 500 queries, uniform, standalone dense = the honest
signal). Artifact: `benchmark/lambda/results/scale1m_qwen3_0.6b.json`
(`entities_embedded=995562`, `dim=1024` — verified, not a stale-embedding no-op).

| Model (1M, dense, default config) | r@1 | r@5 | r@10 | p50 | dim |
| --- | --- | --- | --- | --- | --- |
| nomic-embed-text (baseline) | 0.514 | **0.726** | 0.800 | 194 ms | 768 |
| Qwen3-Embedding-0.6B fp16 (as-integrated) | 0.190 | **0.368** | 0.440 | 334 ms | 1024 |

The larger, higher-MTEB model **lost decisively** as integrated — roughly half
the dense recall and ~1.7× slower (bigger vectors → more scan + rerank cost).

**Read this as an integration result, not qwen3's ceiling.** Qwen3-Embedding is
instruction-tuned: queries need a task-instruction prefix and the model uses
last-token pooling. The vault embeds through a generic Ollama `/api/embed` call
that applies **neither** — so this is "Qwen3-0.6B dropped into the vault as-is,"
which is exactly the naive swap the issue proposed, and it does not pay off.
Reaching qwen3's MTEB-implied quality would require prefix + pooling handling the
vault does not have.

**Conclusion (closes the evaluation):** a naive larger-embedder swap is a
regression here, so there is nothing for NVFP4 to make "affordable" — NVFP4 only
matters once a *properly integrated* larger model first shows a quality win,
which this does not. Combined with the local-first / Blackwell-only constraints
above, NVFP4 embedding is **not worth pursuing for Vault now**. If revisited, the
prerequisite is instruction-prefix + last-token embedding support (a model-serving
feature), measured to beat nomic-768 *before* any 4-bit/Blackwell spend.

## Sources

- NVFP4 format + Blackwell-native tensor cores: [Introducing NVFP4 (NVIDIA)](https://developer.nvidia.com/blog/introducing-nvfp4-for-efficient-and-accurate-low-precision-inference/), [NVFP4 training precision (NVIDIA)](https://developer.nvidia.com/blog/nvfp4-trains-with-precision-of-16-bit-and-speed-and-efficiency-of-4-bit/)
- NVFP4 serving via SGLang/ModelOpt: [SGLang quantization docs](https://docs.sglang.io/docs/advanced_features/quantization), [LMSYS ModelOpt integration](https://www.lmsys.org/blog/2025-12-02-modelopt-quantization/)
- Embedding-model landscape / MTEB: [Qwen3-Embedding (Qwen)](https://qwenlm.github.io/blog/qwen3-embedding/), [all-MiniLM-L6-v2 card (HF)](https://huggingface.co/onnx-models/all-MiniLM-L6-v2-onnx)
