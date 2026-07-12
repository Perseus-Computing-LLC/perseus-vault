#!/usr/bin/env bash
# gate.sh — runs ON the remote box. Verifies Ollama genuinely serves a real
# generate AND a real embed before any benchmark. Prints GATE_OK / GATE_FAIL.
export PATH=$PATH:/usr/local/bin:/usr/bin
for i in $(seq 1 30); do
  G=$(curl -s -m60 http://localhost:11434/api/generate \
        -d '{"model":"qwen2.5:14b-instruct","prompt":"OK","stream":false}' \
      | python3 -c 'import sys,json;print((json.load(sys.stdin).get("response") or "").strip()[:8])' 2>/dev/null)
  E=$(curl -s -m60 http://localhost:11434/api/embed \
        -d '{"model":"nomic-embed-text","input":"x"}' \
      | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get("embeddings",[[]])[0]))' 2>/dev/null)
  if [ -n "$G" ] && [ "${E:-0}" -gt 100 ]; then echo "GATE_OK gen=[$G] dim=$E"; exit 0; fi
  echo "gate retry $i: gen=[$G] dim=[$E]" >&2
  sudo systemctl restart ollama 2>/dev/null; sleep 8
done
echo GATE_FAIL; exit 1
