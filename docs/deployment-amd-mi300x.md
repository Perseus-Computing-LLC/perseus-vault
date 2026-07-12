# Deployment Reference: Perseus Vault Beside vLLM on MI300X

Operator guidance for co-hosting Perseus Vault's memory layer on the **same GPU
host** that serves the model — the topology behind the roadmap's "MI300X +
Perseus Vault reference stack." Everything here was learned (and measured,
**2026-07-09**) running Perseus Vault beside vLLM serving a 72B bf16 model on a
rented **MI300X** (ROCm) and a **2×H100** (CUDA) pod, under real inference
load — not synthetic idle-host benchmarks.

Full data + harness: [perseus-amd-act-ii](https://github.com/tcconnally/perseus-amd-act-ii)
(`docs/BENCHMARKS.md` §3a–3b, `src/amd_live_benchmark.py`). The
GPU-contention + agent-economics harness is being upstreamed into this repo
as [`benchmark/contention/`](../benchmark/contention/) (#530).

## TL;DR checklist

| # | Do this | Or this happens |
|---|---|---|
| 1 | Size `/dev/shm` (`--shm-size 128g`) | vLLM's multiprocess engine crashes at startup; on pod platforms the container restart-loops, re-downloading weights each cycle |
| 2 | Make the entrypoint restart-safe (`sleep infinity` + exec, or a supervisor) | A crashing server on a managed pod = an infinite **billing** loop |
| 3 | Pin torch/CUDA wheels and the full ROCm image tag | `_cuda_init` failures on cu130-vs-12.8-driver mismatch; `rocm/vllm:latest` shipped with **no vllm binary at all** |
| 4 | Budget ~85 MB RSS per 100K-memory agent store | Under-provisioned RAM on a host that has 283 GB to spare |
| 5 | Quote vLLM's printed "Maximum concurrency" as the agent ceiling | Idealized HBM/KV arithmetic overestimates (~20 vs **15.3** measured) |

## 1. `/dev/shm` sizing — the #1 silent killer

The default container `/dev/shm` is **64 MB**. vLLM's multiprocess engine
(V1 default) uses shared memory for inter-process transport and **crashes at
startup** against that default. On pod platforms (RunPod), the failure mode
is worse than a crash: the platform restarts the container, which
**re-downloads the model weights each cycle** — a restart loop that looks
like "stuck downloading" and bills the whole time.

**Fix:** give the container a large shm before serving.

```bash
docker run --shm-size 128g ...        # what we used on the MI300X host
# or remount inside a pod you can't relaunch:
mount -o remount,size=128g /dev/shm
```

**Escape hatch, measured cost:** single-process mode survives a small shm —

```bash
VLLM_ENABLE_V1_MULTIPROCESSING=0 vllm serve ...
```

— but it costs **60% higher TPOT (133 ms vs 83 ms measured)**. Use it only
when you cannot control the shm mount; fix the mount otherwise.

## 2. PID-1 semantics on managed pods

Managed pod platforms treat your start command as PID 1: **when it exits, the
platform restarts the container**. Combined with any startup crash (see §1, or
a bad pin from §3), a crashing server is an **infinite billing loop** — the
pod re-pulls, re-downloads, re-crashes, and charges for every lap.

**Fix:** never make the fallible server the container's start command. Keep
PID 1 alive independently and exec the server under it, so a crash leaves you
a running container to debug instead of a restart loop:

```yaml
# docker-compose.yml — restart-safe co-residency skeleton
services:
  llm:
    image: rocm/vllm:rocm7.13.0_gfx94X-dcgpu_..._vllm_0.19.1   # full tag — see §3
    shm_size: 128g                                             # see §1
    # PID 1 = sleep, not the server: a vLLM crash never kills the container
    command: ["/bin/bash", "-lc", "sleep infinity"]
    devices: ["/dev/kfd", "/dev/dri"]                          # ROCm
  vault:
    image: ghcr.io/perseus-computing-llc/perseus-vault:latest
    volumes: ["./stores:/data"]
```

```bash
# then start (and restart) the server explicitly, under the live container:
docker compose exec -d llm vllm serve Qwen/Qwen2.5-72B-Instruct ...
```

An equivalent restart-safe entrypoint (`sleep infinity` as PID 1, server
launched by `exec`/supervisor) works on platforms without compose. If you do
want auto-restart, use a real supervisor with backoff — never the platform's
naive container restart.

## 3. Pin torch, drivers, and image tags

Both GPU vendors' "latest" paths were broken at time of measurement:

- **CUDA:** the latest vLLM wheels pull **torch cu130**; hosts on **CUDA 12.8
  drivers fail at `_cuda_init`**. Pin the pair — we used **vLLM 0.19.1 with
  torch cu128**:

  ```bash
  pip install vllm==0.19.1 torch --index-url https://download.pytorch.org/whl/cu128
  ```

- **ROCm:** `rocm/vllm:latest` shipped with **no vllm binary at all**. Never
  deploy the `latest` tag — always pin the full descriptive tag, which encodes
  the ROCm version, GPU family, and vLLM version:

  ```text
  rocm/vllm:rocm7.13.0_gfx94X-dcgpu_..._vllm_0.19.1
  ```

Treat the model server's stack like Perseus Vault treats its own releases:
a pinned, named artifact or nothing.

## 4. Host sizing: the vault rides along for free

Measured on EPYC 9474F / 9334-class hosts (the CPUs MI300X and H100 pods
actually ship with), **2026-07-09**, under **100% GPU load** from real 72B
serving:

| Metric | Measured |
|---|---|
| Recall p50 @ 100K-entity store | **~19 ms** |
| Recall shift under 100% GPU load | **±0.6%** (unaffected) |
| RSS per 100K-memory agent store | **~85 MB** |

The takeaway for sizing: Perseus Vault is CPU/RAM-side and does not contend
with the GPU. A 283 GB-RAM MI300X host comfortably co-hosts **thousands** of
agent stores beside serving — memory is not the scarce resource on these
boxes; provision for the model and the vault rides along.

## 5. Honest capacity math: quote vLLM's own ceiling

vLLM prints its real concurrency budget at startup:

```text
Maximum concurrency for 8192 tokens per request: 15.30x
```

That line — weights actually loaded, KV cache actually allocated — is the
honest per-card agent ceiling, and what we recommend quoting in deployment
docs and capacity plans:

| Card(s) | GPUs | Measured ceiling (72B bf16, 8192-token requests) |
|---|---|---|
| MI300X (192 GB HBM) | **1** | **15.3** concurrent agents |
| 2×H100 (best boot achieved) | 2 | **5.0** concurrent agents |
| 2×A100 80GB (eager@0.97, same config as the H100 row) | 2 | **6.37** concurrent agents |
| 8×A100 40GB (standard boot) | 8 | **57.9** concurrent agents — **7.2/card**; the pooled 320 GB is heavily overprovisioned for a ~136 GB model, so KV headroom (and the resulting $0.275/agent-hr) is flattered. Never quote it without the GPU count: per card the MI300X leads 15.3 vs 7.2 and wins 1.9× on $/agent-hour |

The idealized `(HBM − weights) / KV-per-request` arithmetic **overestimates**
(~20 vs 15.3 measured on MI300X) because it ignores activation memory,
allocator overhead, and vLLM's reserved headroom. Don't publish the idealized
number; read the startup line.

## Reproducing

- Raw data + methodology: [perseus-amd-act-ii `docs/BENCHMARKS.md` §3a–3b](https://github.com/tcconnally/perseus-amd-act-ii/blob/main/docs/BENCHMARKS.md)
- Live-load harness: [`src/amd_live_benchmark.py`](https://github.com/tcconnally/perseus-amd-act-ii/blob/main/src/amd_live_benchmark.py)
- In-repo (upstreaming via #530): [`benchmark/contention/`](../benchmark/contention/) —
  GPU-contention + agent-economics runs; companion to [`benchmark/scale/`](../benchmark/scale/)
  and [`PERF.md`](../PERF.md)
