#!/usr/bin/env bash
# serve.sh — bring up + smoke-test the standing inference endpoint (#2).
# Ollama serves BOTH the instruct model and the embedding model from one daemon
# on :11434. This is what the Perseus Vault RAG path talks to.
set -euo pipefail
PFS="${PFS:-/home/ubuntu/persist}"
export OLLAMA_MODELS="$PFS/models/ollama"

# Ensure the daemon is up (systemd on Lambda's Ubuntu image).
sudo systemctl is-active --quiet ollama || sudo systemctl restart ollama || \
  (ollama serve >"$PFS/logs/ollama.log" 2>&1 &)
sleep 3

echo "==> Models loaded:"
ollama list

echo "==> Smoke test: instruct completion"
curl -s http://localhost:11434/api/generate \
  -d '{"model":"qwen2.5:14b-instruct","prompt":"Reply with exactly: OK","stream":false}' \
  | jq -r '.response'

echo "==> Smoke test: embedding dimension"
curl -s http://localhost:11434/api/embed \
  -d '{"model":"nomic-embed-text","input":"perseus vault memory recall"}' \
  | jq '.embeddings[0] | length'

echo
echo "Endpoint LIVE at http://localhost:11434  (Ollama, OpenAI-compat at /v1)"
echo "  LLM:        qwen2.5:14b-instruct"
echo "  Embeddings: nomic-embed-text  (/api/embed)"
echo
echo "--- vLLM ALTERNATIVE (only if you want a high-throughput OpenAI endpoint for demos) ---"
echo "  source $PFS/venv/bin/activate && pip install vllm"
echo "  vllm serve Qwen/Qwen2.5-14B-Instruct --gpu-memory-utilization 0.85 \\"
echo "    --max-model-len 8192 --enable-prefix-caching --port 8000 --host 0.0.0.0"
echo "  (Serve embeddings as a 2nd vLLM proc on :8001 with --task embed, or keep them on Ollama.)"
