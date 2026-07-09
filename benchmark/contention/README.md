# GPU-contention + agent-economics benchmark

Measures the co-residency claim behind Perseus Vault's deployment story:
**memory lives on the host CPU and uses 0 bytes of GPU HBM, so recall steals
no inference cycles from a co-located model** — and, because every byte of
HBM stays available for KV cache, one accelerator serves more concurrent
agents per dollar. Upstreamed from the AMD ACT II hackathon harness where
these numbers were first measured on rented hardware (#530); the measured
rows live in [PERF.md](../../PERF.md).

Both harnesses drive the **real shipped binary over MCP stdio** (one
persistent process, same shape as [`../scale/`](../scale/)), seeded with a
distinct-content corpus (`skip_dedup`, #531).

## Scripts

| Script | Load source | What it answers |
|---|---|---|
| [`burn_bench.py`](./burn_bench.py) | Synthetic compute-bound FP16 matmul (torch, subprocess-isolated); **pure-Python CPU-spin fallback** when torch/GPU is absent | Does recall latency move when the accelerator (or the CPU) is pinned at 100%? Self-contained — no model, no endpoint. |
| [`live_bench.py`](./live_bench.py) | Real LLM serving traffic against any **OpenAI-compatible endpoint** (vLLM, llama.cpp, TGI, ...) | Same question under a *real* inference load, **plus** serving throughput, $/1M output tokens, concurrent-agent ceiling, and $/agent-hour. |
| [`vault_client.py`](./vault_client.py) | — | Shared MCP stdio client, corpus seeding, recall probe. |

Both print `[measured]` for everything timed live and `[derived]` for anything
computed from published specs, and write a JSON report (default: OS temp; pass
`--out` to capture).

## Prerequisites

- A built binary: `cargo build --release` (auto-located at
  `target/release/perseus-vault`; override with `--bin` or `MIMIR_BIN`).
- Python 3.10+. **Stdlib only** — no packages required.
- `burn_bench.py`: **torch is optional.** With a CUDA/ROCm torch build and a
  GPU, the burn is an FP16 matmul at 100% utilization; without torch (or
  without a GPU) each burner degrades to a pure-Python spin that saturates one
  CPU core, so the harness runs end-to-end anywhere (label says which).
- `live_bench.py`: an OpenAI-compatible `/v1/chat/completions` endpoint.

## Run it

Synthetic burn (self-contained; on a GPU box use torch-cuda/torch-rocm):

```bash
python benchmark/contention/burn_bench.py --store-size 100000 --iters 1000
# CPU-only smoke of the harness itself:
python benchmark/contention/burn_bench.py --store-size 500 --iters 200 \
    --burners 4 --ramp 3
```

Live serving load + economics (the flags shown reproduce the PERF.md MI300X
rows — any OpenAI-compatible endpoint and any GPU work the same way):

```bash
vllm serve Qwen/Qwen2.5-72B-Instruct --max-model-len 8192 \
    --gpu-memory-utilization 0.92 2>&1 | tee /tmp/vllm.log &
python benchmark/contention/live_bench.py \
    --base-url http://127.0.0.1:8000 --model Qwen/Qwen2.5-72B-Instruct \
    --gpu-price 2.19 --vllm-log /tmp/vllm.log \
    --concurrency 32 --duration 60 --store-size 100000
```

- `--gpu-price` is the $/hr you actually pay; `0` (default) skips the
  economics rows rather than inventing a price.
- `--vllm-log` makes the concurrent-agent ceiling **measured**: vLLM prints
  its KV-cache-bound `Maximum concurrency for 8192 tokens per request: N x`
  at startup and the harness reads it. Without it, pass `--gpu-hbm-gb` +
  `--model-weights-gb` for a published-spec `[derived]` fallback.
- Dry-run the harness on a laptop first against llama.cpp
  (`llama-server -m model.gguf --port 8081`) with `--store-size 1000`.

## Measured results (2026-07-09, full tables in [PERF.md](../../PERF.md))

On a rented **MI300X** (RunPod, vLLM 0.19.1 + ROCm 7.13, AMD EPYC 9474F host)
serving Qwen2.5-72B bf16 under sustained load, host-CPU recall p50 at a
100K-entity store moved **18.7 → 18.8 ms (±0.6% median, 6 idle-vs-serving
runs)** — and **+0.6%** under a synthetic 100%-utilization matmul
(97.4 TFLOPS FP16). Economics: **15.3** concurrent 8K-token agents/card →
**$0.143/agent-hr** at $2.19/hr; 658 output tok/s → **$0.92/1M output
tokens** (untuned bf16). Cross-vendor baseline (Lambda 2×H100 SXM, same
model + vLLM): best boot **5.0 agents → $1.68/agent-hr** → **11.7×**
measured-vs-measured in the MI300X's favor (1×H100 cannot load the model).

## Honesty notes

- The recall probe runs in the same Python process as `live_bench.py`'s load
  generator threads; those threads are almost always blocked on network I/O,
  but a heavily CPU-bound client could add client-side noise. `burn_bench.py`
  avoids this entirely by isolating the load in subprocesses.
- Recall latencies here include MCP stdio round-trip over the real binary —
  the same path a production agent pays — so they are comparable to
  `benchmark/scale/` numbers, not to in-process library calls.
- The idle-vs-loaded delta is the point, not the absolute p50 (which varies
  by host CPU and store size). Run both passes on the same box, same store.
- $/agent-hour divides the card's rental price by the KV-cache-bound
  concurrent-sequence ceiling at `--ctx-tokens`; it is a **capacity** number
  (agents the card can hold), not a throughput guarantee per agent.
