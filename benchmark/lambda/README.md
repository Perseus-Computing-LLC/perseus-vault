# Perseus Vault — Dynamic Range Benchmark Suite

First-party, reproducible benchmarks measuring Perseus Vault across the full
hardware range it targets: from a single GPU up to an 8-GPU fleet. Run on Lambda
Cloud (A100 40GB, 2×H100 80GB, 8×H100 80GB) against local Ollama inference.

**Thesis:** the same API and correctness from air-gapped/offline to multi-GPU big
iron — and semantic recall that *holds at scale where keyword search collapses.*

## Headline results (measured)

### 1. Recall by mode — 10,000 distinct entities (2×H100)
| recall@k | keyword (fts5) | dense | hybrid |
|---|---|---|---|
| @1 | 0.002 | 0.563 | **0.900** |
| @5 | 0.008 | 0.795 | **1.000** |
| @10 | 0.011 | 0.862 | **1.000** |

Dense/hybrid p50 latency ~38ms, flat across corpus size. **Keyword search collapses
at scale** (0.2% @1 on 10k distinct entities); **hybrid holds 90% @1 / 100% @5**, with
reciprocal-rank fusion recovering dense's rank-1 dilution. This is the core argument
for vector + hybrid recall in agentic memory.

### 1b. Recall by mode — 100,000 distinct entities (1×H100)
| recall@k | keyword (fts5) | dense | hybrid |
|---|---|---|---|
| @1 | 0.003 | 0.680 | **0.785** |
| @5 | 0.015 | 0.859 | **1.000** |
| @10 | 0.029 | 0.899 | **1.000** |

At **100K** entities the gap *widens*: hybrid is perfect @5 while keyword lands ~1.5%
of the time — a **~66× gap**. See `results/scale_100k_distinct.json`.

### 1c. Competitive recall — same box, same corpus, all fully local (1×H100)
Every system stood up and run live against the same local Ollama
(`qwen2.5:14b-instruct` + `nomic-embed-text`); identical facts, queries, substring judge.

| System | Recall | p50 | Stack |
|---|---|---|---|
| **Perseus Vault** (hybrid) | **1.00** | 35.6 ms | single ~8MB binary, in-process |
| Letta (archival / pgvector) | 1.00 | 135.5 ms | server + Postgres/pgvector |
| Mem0 (vector) | 0.60 | 37.9 ms | Python + Qdrant |
| Zep (Graphiti temporal KG) | 0.20 | 49.7 ms | server + Neo4j; KG built by local model |

No fabricated numbers: Zep's deprecated CE server / Cloud-only memory API means we measure
its real OSS engine (Graphiti on Neo4j), whose 0.20 reflects lossy *local-model* graph
extraction, not Zep Cloud (frontier models). See `results/competitors.json` and
`competitors_bench.py`.

### 2. Multi-GPU throughput — 8×H100 fleet
Peak **651 embeddings/sec** at concurrency 64 — **22.8× the single-thread baseline**
and **~4.7× a single Ollama daemon's saturation ceiling (~137 eps)**. Achieved with
**8 Ollama daemons pinned one-per-GPU** (`CUDA_VISIBLE_DEVICES`) behind a round-robin
load balancer (`serve_fleet.sh` / `parallel_embed_fleet.py`). Near-linear per-GPU
scaling to ~concurrency 32-48, rolling off as request queuing dominates.

> **Re-validation 2026-07-12:** 100K recall-hold re-confirmed — hybrid recall@5 = recall@10 = **1.0** (matches §1 exactly; the fleet throughput win costs no accuracy). 8×H100 had zero in-region (us-south-2) capacity across the poll window, so the largest available in-region multi-GPU (8× Tesla V100) was used as a labeled fallback: peak **432 emb/s @ conc 64** — see `results/fleet_8gpu_v100_throughput.json` and `results/fleet100k_recall.json`. The 651 emb/s figure above is the H100 headline and is **retained unrefreshed** (not overwritten with V100 data). Caveat: today the vault's own embed path (`mimir_embed`) is sequential and cannot reach these fleet rates — see [#601](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/601).

### 3. Model quality vs latency — mimir_ask grounded QA
Both `qwen2.5:14b` and `qwen2.5:72b` scored **100% accuracy with citations** (pre-warmed).
14B at ~2.5× lower latency. Takeaway: when retrieval is strong, a smaller model suffices
for grounded recall — reinforcing the edge/offline story.

## Scripts

| Script | Purpose |
|---|---|
| `provision.sh` | Set up a fresh instance: repo, Ollama, models on persistent FS |
| `serve.sh` | Single-daemon inference endpoint (LLM + embeddings) |
| `serve_fleet.sh` | **N Ollama daemons pinned one-per-GPU + nginx LB** (multi-GPU scale-out) |
| `scale_bench.py` | Seed → embed → recall@k (fts5/dense/hybrid) at configurable corpus size |
| `parallel_embed_fleet.py` | Aggregate embedding throughput vs concurrency across the fleet |
| `quality_lift.py` | mimir_ask accuracy/latency across chat models (14B vs 72B) |
| `mem0_bench.py` | Competitive: same recall task against Mem0, same box + Ollama |
| `competitors_bench.py` | Competitive 4-way: same recall task vs Mem0, Zep (Graphiti/Neo4j) and Letta (pgvector), same box + Ollama → `results/competitors.json` |
| `rag_bench.py` | MCP JSON-RPC driver + single-endpoint RAG smoke bench |
| `build_report.py` | Render `results/*.json` → self-contained `results.html` |
| `check_8x.py` / `poll_8x.sh` | Detect high-end multi-GPU capacity on Lambda |
| `competitors_bench.py` | Same-box recall vs Mem0 / Zep / Letta (honest labeling — never fabricates a number for a stack that won't run locally) |
| `campaign_run.sh` | **Generic self-terminating campaign runner.** Launches a GPU box, provisions, gates, runs an arbitrary `REMOTE_CMD`, pulls `PULL_FILES`, and ALWAYS terminates (EXIT trap + deadline). |
| `run_scale100k_durable.sh` | Durable scale run: DB + result on the persistent FS, resumable via `--skip-seed`, terminates only on a DONE marker or hard deadline (transient SSH failures are retried, never fatal) |
| `orchestrate_campaigns.sh` | Chains campaigns back-to-back, non-overlapping |
| `gate.sh` | **Readiness gate** (run on the box): require a real `generate` (text) AND a real `embed` (dim>100) before benchmarking — stops connection-refused errors being recorded as fake 0.0 recall |
| `coldstart_capture.sh` | Time a bare box → first grounded RAG answer; refuses to record a fake time if the answer errors |
| `teardown_checklist.md` | Save results + terminate (avoid credit leak) |

### Orchestration env vars

The launcher scripts (`campaign_run.sh`, `run_scale100k_durable.sh`) read these from the
environment so no machine-specific paths or secrets are baked in:

| Var | Purpose | Default |
|---|---|---|
| `LAMBDA_KEY_FILE` | Path to a file containing your Lambda Cloud API key (required) | — |
| `LAMBDA_SSH_KEY` | Private SSH key registered with Lambda (key name `hermes`) | `$HOME/.ssh/lambda_ed25519` |
| `KITDIR` | Local kit dir (results + instance-id state land here) | the script's own directory |

Lambda API auth is HTTP basic with the key as the username: `curl -u "$KEY:" …`. The
persistent FS `perseus-vault-fs-south` (us-south-2) is expected to hold the prebuilt binary
and Ollama models; a fresh box still runs `provision.sh` first (Ollama is NOT preinstalled —
the ephemeral disk is wiped each boot; only the FS persists).

## Reproduce

```bash
# on a Lambda instance (see provision.sh for setup):
PFS=/path/to/persistent-fs ./provision.sh
./serve.sh                                    # single-GPU endpoint
python3 scale_bench.py --bin <perseus-vault> --db /tmp/b.db \
  --llm-endpoint http://localhost:11434/api/generate --llm-model nomic-embed-text \
  --embedding-endpoint http://localhost:11434/api/embed --embedding-model nomic-embed-text \
  --clusters 1250 --per-cluster 8 --tier "2xH100" --out results/scale_10k.json

# multi-GPU fleet (e.g. 8x):
NGPU=8 ./serve_fleet.sh
python3 parallel_embed_fleet.py results/fleet.json 8

python3 build_report.py results   # -> results/results.html
```

## Notes / findings surfaced during benchmarking

- **`--embedding-model-name`** was added (PR for #525) so remote embedding endpoints
  can use a dedicated embed model distinct from the chat model. Without it, a chat-only
  model returns HTTP 501 and dense/hybrid recall silently empties. These scripts pass
  the embedding model explicitly.
- **Content dedup:** `mimir_remember` collapses writes ≥70% trigram-similar (by design).
  Benchmark corpora must use genuinely distinct content per entity (`scale_bench.py`
  uses randomized filler) or the corpus silently collapses.
- **Build:** use `cargo build --release --no-default-features` on glibc<2.38 hosts
  (Ubuntu 22.04); the bundled-ONNX default fails to link there (see issue #526).
- Lambda bills weekly; **terminate idle instances** (see `teardown_checklist.md`).

All result JSON in `results/` is first-party measured. `results.html` is generated.
