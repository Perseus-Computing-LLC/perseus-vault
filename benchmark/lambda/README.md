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

### 2. Multi-GPU throughput — 8×H100 fleet
Peak **651 embeddings/sec** at concurrency 64 — **22.8× the single-thread baseline**
and **~4.7× a single Ollama daemon's saturation ceiling (~137 eps)**. Achieved with
**8 Ollama daemons pinned one-per-GPU** (`CUDA_VISIBLE_DEVICES`) behind a round-robin
load balancer (`serve_fleet.sh` / `parallel_embed_fleet.py`). Near-linear per-GPU
scaling to ~concurrency 32-48, rolling off as request queuing dominates.

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
| `rag_bench.py` | MCP JSON-RPC driver + single-endpoint RAG smoke bench |
| `build_report.py` | Render `results/*.json` → self-contained `results.html` |
| `check_8x.py` / `poll_8x.sh` | Detect high-end multi-GPU capacity on Lambda |
| `teardown_checklist.md` | Save results + terminate (avoid credit leak) |

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
