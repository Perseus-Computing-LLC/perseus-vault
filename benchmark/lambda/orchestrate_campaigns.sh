#!/usr/bin/env bash
# orchestrate_campaigns.sh — run the approved GPU campaigns back-to-back, each on
# its own self-terminating H100, never overlapping. Short validation campaign
# first, long scale run second.
set -uo pipefail
cd "$(dirname "$0")"
LOG_DIR="results"; mkdir -p "$LOG_DIR"
echo "===================================================================="
echo " CAMPAIGN 1/2: competitors matrix + cold-start capture (short)"
echo "===================================================================="
NAME=pv-compete MAX_HOURS=1 \
REMOTE_CMD='source $PFS/venv/bin/activate 2>/dev/null; pip install -q mem0ai qdrant-client 2>/dev/null; \
  cd ~/lambda-kit && \
  python3 competitors_bench.py --bin "$BIN" --ollama-base http://localhost:11434 --out-dir /tmp && \
  bash ~/lambda-kit/coldstart_capture.sh' \
PULL_FILES='/tmp/competitors.json /tmp/competitors.html /home/ubuntu/coldstart.json /home/ubuntu/coldstart.log' \
  bash campaign_run.sh
echo "[orchestrator] campaign 1 exit=$?"

sleep 20  # let terminate settle before the overlap guard checks

echo "===================================================================="
echo " CAMPAIGN 2/2: 100k-entity scale recall (long)"
echo "===================================================================="
NAME=pv-scale100k MAX_HOURS=5 \
REMOTE_CMD='cd ~/lambda-kit && rm -f /tmp/scale100k.db* && python3 scale_bench.py \
  --bin "$BIN" --db /tmp/scale100k.db \
  --llm-endpoint http://localhost:11434/api/generate --llm-model qwen2.5:14b-instruct \
  --embedding-endpoint http://localhost:11434/api/embed --embedding-model nomic-embed-text \
  --clusters 1000 --per-cluster 100 --tier "1xH100-SXM (100k entities)" \
  --out /tmp/scale100k.json' \
PULL_FILES='/tmp/scale100k.json' \
  bash campaign_run.sh
echo "[orchestrator] campaign 2 exit=$?"

echo "===================================================================="
echo " ALL CAMPAIGNS COMPLETE. Results in $LOG_DIR/"
ls -la "$LOG_DIR"/ | grep -E "competitors|coldstart|scale100k" || true
echo "===================================================================="