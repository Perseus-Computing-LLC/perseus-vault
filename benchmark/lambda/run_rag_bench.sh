#!/usr/bin/env bash
# run_rag_bench.sh — (#1b) THE real-inference benchmark. Exercises the A100 via the
# vault binary's LLM/embedding path against the live Ollama endpoint.
#
# Unlike Gauntlet v2 (CPU: render + FTS5 + shell directives), this measures the
# GPU-dependent numbers that are actually citable as "first-party, measured on A100":
#   1. Embedding throughput + coverage (mimir_embed -> Ollama /api/embed, nomic-embed-text)
#   2. Dense (vector) recall  vs  FTS5 recall  on the SAME seeded query set (the delta)
#   3. mimir_ask RAG latency (recall -> assemble -> qwen2.5:14b generate)
#
# Prereq: serve.sh endpoint live; perseus-vault release binary built.
set -euo pipefail
PFS="${PFS:-/lambda/nfs/perseus-vault-fs}"
BIN="$PFS/repo/perseus-vault/target/release/perseus-vault"
DB="$PFS/bench/rag_bench.db"
OUT="$PFS/logs/rag_bench_$(date +%Y%m%d-%H%M%S).json"
LLM_ENDPOINT="http://localhost:11434/api/generate"
EMBED_ENDPOINT="http://localhost:11434/api/embed"
LLM_MODEL="qwen2.5:14b-instruct"
EMBED_MODEL="nomic-embed-text"

mkdir -p "$PFS/bench"
[ -x "$BIN" ] || { echo "ERROR: binary not built at $BIN"; exit 1; }
curl -sf http://localhost:11434/api/tags >/dev/null || { echo "ERROR: endpoint down; run ./serve.sh"; exit 1; }

echo "==> vault binary: $("$BIN" --version 2>/dev/null || echo unknown)"
echo "==> Seeding vault with benchmark corpus + embedding all entities on A100"
# The harness python does the seeding + timing against the binary's MCP/CLI surface.
python3 "$(dirname "$0")/rag_bench.py" \
  --bin "$BIN" \
  --db "$DB" \
  --llm-endpoint "$LLM_ENDPOINT" --llm-model "$LLM_MODEL" \
  --embedding-endpoint "$EMBED_ENDPOINT" --embedding-model "$EMBED_MODEL" \
  --out "$OUT"

echo
echo "==> results written to $OUT"
python3 -c "import json;d=json.load(open('$OUT'));print(json.dumps(d.get('summary',d),indent=2))"
