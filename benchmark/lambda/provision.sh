#!/usr/bin/env bash
# provision.sh — one-shot setup on a fresh Lambda A100 80GB instance.
# Run once after SSH. Idempotent: safe to re-run.
#
#   Instance:  1x A100 80GB SXM (on-demand) + Persistent Filesystem
#   Goal:      (#1) real-inference Perseus Vault Gauntlet v2  +  (#2) standing inference endpoint
#
# ASSUMPTIONS (edit these two lines if your launch differs):
PFS="${PFS:-/home/ubuntu/persist}"                 # mount point of your Persistent Filesystem
REPO_URL="${REPO_URL:-https://github.com/Perseus-Computing-LLC/perseus-vault.git}"
set -euo pipefail

echo "==> [1/6] Persistent filesystem"
mkdir -p "$PFS"/{models,repo,cache,logs}
# Lambda auto-mounts the PFS you attached at launch; verify it is a real mount:
if ! mountpoint -q "$PFS" 2>/dev/null; then
  echo "WARN: $PFS is not a mountpoint. Data here will NOT survive teardown."
  echo "      Attach a Persistent Filesystem at launch and set PFS=<mountpoint>."
fi
export HF_HOME="$PFS/cache/hf"
export OLLAMA_MODELS="$PFS/models/ollama"
mkdir -p "$HF_HOME" "$OLLAMA_MODELS"

echo "==> [2/6] System deps"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-pip python3-venv git jq curl >/dev/null

echo "==> [3/6] Perseus Vault repo (canonical)"
if [ -d "$PFS/repo/perseus-vault/.git" ]; then
  git -C "$PFS/repo/perseus-vault" pull --ff-only || true
else
  git clone --depth 1 "$REPO_URL" "$PFS/repo/perseus-vault"
fi
ln -sfn "$PFS/repo/perseus-vault" "$HOME/perseus-repo"

echo "==> [4/6] Python env for the benchmark harness"
python3 -m venv "$PFS/venv" 2>/dev/null || true
# shellcheck disable=SC1091
source "$PFS/venv/bin/activate"
pip install -q --upgrade pip
pip install -q requests

echo "==> [5/6] Ollama (native fit for the vault RAG path: /api/embed + --llm-endpoint)"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
# Point Ollama's model store at the persistent FS so weights survive teardown.
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_MODELS=%s"\n' "$OLLAMA_MODELS" \
  | sudo tee /etc/systemd/system/ollama.service.d/override.conf >/dev/null
# The systemd 'ollama' user (uid ~998) must be able to write the NFS model dir,
# which is owned by the login user. Group-add avoids NFS root_squash chown pain.
# (Discovered 2026-07-09: without this, ollama crash-loops on
#  'mkdir .../blobs: permission denied'.)
sudo usermod -aG "$(id -gn)" ollama || true
sudo systemctl daemon-reload
sudo systemctl reset-failed ollama || true
sudo systemctl restart ollama || (ollama serve >"$PFS/logs/ollama.log" 2>&1 &)

# Wait for the daemon to actually answer before pulling (avoids the pull racing
# service startup and failing with 'could not connect to ollama server').
echo "    waiting for ollama endpoint..."
for i in $(seq 1 30); do
  curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && { echo "    ollama up"; break; }
  sleep 2
done

echo "==> [6/6] Pull models to persistent FS"
# Instruct LLM for mimir_ask/dream/synthesize + benchmark agent calls.
ollama pull qwen2.5:14b-instruct
# Embedding model for mimir_embed (Ollama /api/embed).
ollama pull nomic-embed-text
echo
echo "GPU check:" && nvidia-smi --query-gpu=name,memory.total,memory.used --format=csv,noheader
echo
echo "DONE. Next: ./serve.sh (verify endpoint) then ./run_benchmark.sh"
