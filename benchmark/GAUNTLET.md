# The Gauntlet — Perseus / Perseus Vault continuous-improvement spec

**Mission:** continuous improvement of **performance, stability, security, and
scalability**, proven by a repeatable gauntlet run every release. The scalability
axis is a **deployment ladder** from a solo dev's laptop to a **20,000-employee
air-gapped enclave** — the load-bearing product claim is that *the same single
binary and the same API hold at every rung.*

**Ground rules (non-negotiable):**
- **Fresh baselines, not folklore.** Prior numbers (the Jul-2026 MI300X/H100
  runs, old scale reports) are a *directional guide*, never source of truth.
  Re-measure on current hardware; track deltas over time.
- **No claim without a rerunnable script.** Every reported number carries named
  hardware, binary/commit, and a `data_source` tag: `measured` (timed live),
  `published-spec` (datasheet/price list, cited), or `projection` (derived, with
  assumptions stated). No projection is ever presented as measured.
- **Regressions are locked out.** `benchmark/scale/gate.py` budgets + the signed
  `report.json` fail CI if a number moves the wrong way.
- **Never falsify a bench.** A worse number is recorded, flagged, and reported.

---

## The four axes

| Axis | What we measure | Harness | Gate |
|---|---|---|---|
| **Performance** | recall p50/p99 (fts5·dense·hybrid), write & embed throughput, `as_of` point lookup, temporal recall, cold start; perseus render time / directive cost | vault `benchmark/scale/`, `benchmark/contention/burn_bench.py`; perseus `benchmark/extreme_enterprise_benchmark.py` | `scale/gate.py` budgets; no regression vs signed `report.json` |
| **Stability** | soak (RSS drift, latency drift over hours), fault injection (kill writer mid-flight, corrupt→recover, idle-watchdog self-heal, degradation banner), concurrency correctness (no lost/torn writes under the writer-family) | perseus `harness/load/{load_pass,resilience_pass}.py`; vault `concurrent_writer`/`concurrent_opens` tests | 0 lost writes; RSS flat over N h; self-heal verified |
| **Security** | air-gap **0 network egress** under load; audit-chain (SHA-256 + keyed-MAC) integrity under concurrent writes + redaction; workspace-scoping isolation; `@query` double-gate | packet-capture harness; audit-chain verify; cross-workspace leak probe | 0 egress; chain verifies; cross-workspace leak = 0. External crypto review is the gate before any CMMC "audit trail" claim (see security SOW) |
| **Scalability** | the R0→R4 ladder below — same binary, same API, documented break point | all of the above at increasing corpus / concurrency; `benchmark/lambda/` fleet for GPU rungs | same artifact clears every rung it claims; break point is stated, not hidden |

---

## The deployment ladder (scalability axis)

| Rung | Environment | Concurrent agents | Store size | Load | Hardware | Runnable on |
|---|---|---|---|---|---|---|
| **R0** | Hyper-local **solo dev** | 1–2 | 1k–5k entities | interactive, <1 recall/s | single binary, CPU embeddings, 0 GPU | **local (free)** |
| **R1** | **Small team** | 5–20 | 10k–50k | bursty writes + recall | 1 instance | **local / greg (free)** |
| **R2** | **Department** | 50–250 | ~100k | writer-family engaged, 100s req/s | greg or single GPU (dense) | **greg / 1× GPU** |
| **R3** | **Enterprise, single site** | 1k–5k | 500k–1M | sustained 1000s req/s, model-in-loop | multi-GPU fleet | **Lambda ($7.5k)** |
| **R4** | **20k-employee air-gapped enclave** | thousands | multi-M (federated/sharded) | peak load, **0 network egress**, audit-chain per write | representative on-prem / multi-node | **Lambda proxy + local air-gap verify + $25 RunPod MI300X cross-vendor point** |

Not every axis needs every rung's hardware: **air-gap and audit-chain security
verification run locally regardless of scale** — the network egress count and
chain integrity don't change because the box got bigger.

### Per-rung acceptance (initial; tighten as baselines improve)
- **R0:** recall p50 < 5 ms, cold start < 100 ms, RSS < 100 MB, single-file install.
- **R1:** hybrid p99 < 30 ms @10k, 0 write loss under team concurrency.
- **R2:** hybrid p50 < 100 ms @100k *(currently ~80 ms — see below)*, `as_of` flat
  < 1 ms @100k, hybrid recall@5 ≥ 0.95, write throughput holds to last decile.
- **R3:** near-linear embed throughput to concurrency ~32–48; keyword recall
  collapses while hybrid holds recall@5 ≈ 1.0; measured agents/card economics.
- **R4:** packet-capture shows **0 egress**; audit-chain verifies under concurrent
  writes; cross-workspace leak = 0; federation correctness; **sub-linear**
  latency degradation vs R3.

---

## Hardware budget (2026-07)

- **Local** — Ryzen 7 9800X3D (16 logical) + RTX 5070 Ti. Free. R0–R2 + single-GPU dense/hybrid.
- **greg** — homelab; isolated throwaway instances for mid-tier concurrency/soak.
- **Lambda** — ~$7,500 credits. R3/R4 GPU fleet. **Bursty: launch → run → terminate**
  (billed on wall-clock even idle).
- **RunPod** — ~$25 ≈ 8 h MI300X @ ~$3/hr. The cross-vendor economics point (AMD).

---

## The loop, in evidence

Continuous improvement is not aspirational here — `PERF.md` already shows the
chain, each entry ending in the *next* target:

- **#476** write path — signature-driven dedup scan: **3.9–5.6×** (141→554/s @10k).
- **#507** dense recall — covering index: **14.5×** (360→25 ms p50 @100k).
- **#511** hybrid recall — two-phase BM25 + concurrent arms: **3.7×**
  (295→81 ms p50 @100k), recall quality byte-identical. **← shipped into the AMD
  Act II submission (2026-07-10): the submitted v2.19 benchmarks showed the
  pre-#511 308 ms; v2.20 measures ~80 ms.**
- **Next target (open):** the sparse (BM25) arm is now the hybrid floor —
  ~60 ms for ~100k matched rows, an engine-level O(matches) cost for broad terms.
  Shrinking it is a recall trade (cap/pre-prune the match set), so it gets its own
  measured A/B, not a blind optimization.

Each release: run the gauntlet, record deltas in `PERF.md`, let the residual
"next target" set the next improvement.
