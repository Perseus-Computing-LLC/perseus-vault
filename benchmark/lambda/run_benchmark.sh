#!/usr/bin/env bash
# run_benchmark.sh — (#1) Perseus Vault Gauntlet v2, wired to the LOCAL Ollama endpoint
# so the memory/RAG numbers are FIRST-PARTY MEASURED on real A100 inference, not published-spec.
#
# Prereq: ./provision.sh and ./serve.sh have run and the endpoint smoke test passed.
set -euo pipefail
PFS="${PFS:-/home/ubuntu/persist}"
REPO="$HOME/perseus-repo"
# shellcheck disable=SC1091
source "$PFS/venv/bin/activate"

# --- Wire the Perseus Vault binary's RAG path to the local A100-backed Ollama ---
# Confirmed against perseus-vault/src/main.rs CLI flags:
#   --llm-endpoint      Ollama /api/generate  (enables mimir_ask/dream/synthesize)
#   --llm-model         model name
#   --embedding-endpoint  Ollama /api/embed   (mimir_embed; else bundled ONNX is used)
export PERSEUS_LLM_ENDPOINT="http://localhost:11434/api/generate"
export PERSEUS_LLM_MODEL="qwen2.5:14b-instruct"
export PERSEUS_EMBED_ENDPOINT="http://localhost:11434/api/embed"
export PERSEUS_EMBED_MODEL="nomic-embed-text"

echo "==> Endpoint reachable?"
curl -sf http://localhost:11434/api/tags >/dev/null && echo "   ollama OK" || {
  echo "   ERROR: endpoint down. Run ./serve.sh first."; exit 1; }

cd "$REPO"

# Rebuild the perseus.py artifact if src changed (gauntlet must not run a stale build).
if git diff --quiet --stat src/ 2>/dev/null; then :; else
  echo "==> src/ changed — rebuilding perseus.py"
  python3 scripts/build.py && python3 perseus.py --version
fi

# --- Run order: smoke first (catches wiring bugs in ~30min), THEN full ---
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG_SMOKE="$PFS/logs/gauntlet_smoke_$STAMP.log"
LOG_FULL="$PFS/logs/gauntlet_full_$STAMP.log"

echo "==> SMOKE run (phases 0-5, ~30 min) -> $LOG_SMOKE"
GAUNTLET_SMOKE=1 python3 -u benchmark/gauntlet/v2/gauntlet_v2_orchestrator.py \
    --duration smoke --developers-per-node 50 > "$LOG_SMOKE" 2>&1
echo "   smoke score:"; tail -3 "$LOG_SMOKE"

echo
echo "==> FULL run (all 10 phases, ~6 hrs) -> $LOG_FULL"
echo "    Backgrounding. Monitor with: tail -f $LOG_FULL"
nohup python3 -u benchmark/gauntlet/v2/gauntlet_v2_orchestrator.py \
    --duration full --nodes local --developers-per-node 500 \
    > "$LOG_FULL" 2>&1 &
echo "    PID $!"
echo
echo "Results land in: $REPO/benchmark/gauntlet/v2/"
echo "  gauntlet_v2_report.md   gauntlet_v2_results.json   gauntlet_v2_score.txt"
echo "COPY THOSE TO $PFS BEFORE TEARDOWN (see teardown_checklist.md)."
