#!/usr/bin/env bash
# serve_fleet.sh — Tier-4 TRUE multi-GPU scale-out: 8 Ollama daemons, one pinned
# per H100, behind an nginx round-robin load balancer on :8080.
#
# This is the architecture that unlocks near-linear GPU scaling: a single Ollama
# daemon binds one GPU per model, so N daemons across N GPUs + a balancer is how
# you actually use all 8 cards for embedding throughput.
set -euo pipefail
PFS="${PFS:-/lambda/nfs/perseus-vault-fs-south}"
export OLLAMA_MODELS="$PFS/models/ollama"
NGPU="${NGPU:-8}"
BASE_PORT=11434
LOG="$PFS/logs/fleet"
mkdir -p "$LOG"

echo "==> stopping any systemd ollama (we run our own pinned daemons)"
sudo systemctl stop ollama 2>/dev/null || true
pkill -f 'ollama serve' 2>/dev/null || true
sleep 2

echo "==> launching $NGPU pinned Ollama daemons (one per GPU)"
UPSTREAMS=""
for i in $(seq 0 $((NGPU-1))); do
  port=$((BASE_PORT + i))
  CUDA_VISIBLE_DEVICES=$i OLLAMA_HOST=127.0.0.1:$port OLLAMA_MODELS="$OLLAMA_MODELS" \
    OLLAMA_NUM_PARALLEL=4 OLLAMA_KEEP_ALIVE=60m \
    nohup ollama serve > "$LOG/ollama_gpu$i.log" 2>&1 &
  UPSTREAMS="$UPSTREAMS    server 127.0.0.1:$port;\n"
  echo "   GPU $i -> 127.0.0.1:$port"
done

echo "==> waiting for daemons + warming nomic-embed-text on each GPU"
for i in $(seq 0 $((NGPU-1))); do
  port=$((BASE_PORT + i))
  for t in $(seq 1 30); do curl -sf http://127.0.0.1:$port/api/tags >/dev/null 2>&1 && break; sleep 2; done
  # warm the embed model into THIS gpu's daemon
  curl -s http://127.0.0.1:$port/api/embed -d '{"model":"nomic-embed-text","input":"warm"}' >/dev/null &
done
wait
echo "   all daemons warmed"

echo "==> installing + configuring nginx round-robin load balancer on :8080"
command -v nginx >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq nginx >/dev/null; }
sudo tee /etc/nginx/conf.d/ollama_lb.conf >/dev/null <<EOF
upstream ollama_fleet {
$(echo -e "$UPSTREAMS")
}
server {
    listen 8080;
    location / {
        proxy_pass http://ollama_fleet;
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
    }
}
EOF
# drop the default server that also listens on 80 (avoid conflicts), reload
sudo rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true
sudo nginx -t && sudo systemctl restart nginx
sleep 2

echo "==> verify balancer"
curl -s http://127.0.0.1:8080/api/embed -d '{"model":"nomic-embed-text","input":"lb test"}' \
  | python3 -c "import sys,json; print('LB embed dim:', len(json.load(sys.stdin)['embeddings'][0]))"
echo
echo "FLEET LIVE: $NGPU daemons behind nginx at http://localhost:8080"
echo "  per-GPU direct: 127.0.0.1:11434 .. 127.0.0.1:$((BASE_PORT+NGPU-1))"
