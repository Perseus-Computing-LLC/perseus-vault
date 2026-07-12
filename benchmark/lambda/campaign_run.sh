#!/usr/bin/env bash
# campaign_run.sh — GENERIC self-terminating Lambda GPU campaign runner.
#
# Launches ONE H100 SXM in us-south-2 (mounts perseus-vault-fs-south which holds
# the prebuilt lean binary + Ollama models), syncs the whole kit, brings up the
# Ollama endpoint, runs $REMOTE_CMD on the box, pulls $PULL_FILES back to results/,
# then ALWAYS terminates (EXIT trap + hard deadline + on-disk id for manual reap).
#
# Never overlaps: refuses to launch while any instance is already running.
#
# Usage:
#   REMOTE_CMD='cd ~/lambda-kit && python3 scale_bench.py ...' \
#   PULL_FILES='/tmp/scale100k.json' \
#   NAME=pv-scale100k bash campaign_run.sh
#
# Env knobs: MAX_HOURS (default 3), NAME (default pv-campaign).
set -uo pipefail

KEYFILE="${LAMBDA_KEY_FILE:?set LAMBDA_KEY_FILE to the path of your Lambda API key file}"
KEY="$(cat "$KEYFILE")"
API="https://cloud.lambdalabs.com/api/v1"
REGION="us-south-2"; ITYPE="gpu_1x_h100_sxm5"
FS_SOUTH="perseus-vault-fs-south"; SSHKEY="hermes"
PFS="/lambda/nfs/perseus-vault-fs-south"
KITDIR="${KITDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RESULTS="$KITDIR/results"
IDFILE="$KITDIR/.campaign_instance_id"
NAME="${NAME:-pv-campaign}"
MAX_HOURS="${MAX_HOURS:-3}"
REMOTE_CMD="${REMOTE_CMD:?set REMOTE_CMD}"
PULL_FILES="${PULL_FILES:-}"
SSHOPT="-i ${LAMBDA_SSH_KEY:-$HOME/.ssh/lambda_ed25519} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o ServerAliveInterval=30"
DEADLINE=$(( $(date +%s) + MAX_HOURS*3600 ))
# non-interactive ssh needs ollama on PATH:
RPATH='export PATH=$PATH:/usr/local/bin:/usr/bin'

log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -u "$KEY:" "$@"; }

INSTANCE_ID=""
terminate(){
  local id="${INSTANCE_ID:-$(cat "$IDFILE" 2>/dev/null)}"
  [ -z "$id" ] && { log "no instance to terminate"; return; }
  log "TERMINATING $id"
  # Lambda terminate is ASYNC and a single call can silently not take (observed
  # 2026-07-12: instance still 'active' minutes after one call — credit leak).
  # Verify OUR OWN id left GET /instances; retry until it does. Never gate on a
  # bare running count: parallel seats inflate it.
  local i
  for i in $(seq 1 6); do
    api -X POST "$API/instance-operations/terminate" -H "Content-Type: application/json" \
        -d "{\"instance_ids\":[\"$id\"]}" >/dev/null
    sleep 10
    if ! api "$API/instances" | grep -q "$id"; then
      log "terminate confirmed: $id absent from GET /instances"; rm -f "$IDFILE"; return
    fi
    log "instance $id still present after terminate attempt $i/6; retrying"
  done
  log "WARNING: $id STILL PRESENT after 6 terminate attempts — MANUAL REAP REQUIRED (id kept in $IDFILE)"
}
trap terminate EXIT INT TERM
mkdir -p "$RESULTS"

# --- guard: never overlap. Lambda terminate is ASYNC, so poll until the count
#     actually reaches 0 (up to ~2min) instead of aborting on a transient 1. ---
count_running(){ api "$API/instances" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data']))" 2>/dev/null; }
for i in $(seq 1 24); do
  RUNNING=$(count_running)
  [ "${RUNNING:-1}" = "0" ] && break
  log "waiting for $RUNNING instance(s) to clear before launch (attempt $i)..."
  sleep 5
done
if [ "${RUNNING:-1}" != "0" ]; then
  log "ABORT: $RUNNING instance(s) still running after wait; refusing to launch a second."
  INSTANCE_ID=""; trap - EXIT; exit 3
fi

log "Launching $ITYPE ($NAME) in $REGION ..."
LAUNCH=$(api -X POST "$API/instance-operations/launch" -H "Content-Type: application/json" \
  -d "{\"region_name\":\"$REGION\",\"instance_type_name\":\"$ITYPE\",\"ssh_key_names\":[\"$SSHKEY\"],\"file_system_names\":[\"$FS_SOUTH\"],\"name\":\"$NAME\"}")
echo "$LAUNCH" | head -c 400; echo
INSTANCE_ID=$(echo "$LAUNCH" | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('instance_ids',[''])[0])" 2>/dev/null)
[ -z "$INSTANCE_ID" ] && { log "LAUNCH FAILED"; exit 1; }
echo "$INSTANCE_ID" > "$IDFILE"; log "id=$INSTANCE_ID"

IP=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  J=$(api "$API/instances/$INSTANCE_ID")
  ST=$(echo "$J" | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('status',''))" 2>/dev/null)
  IP=$(echo "$J" | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('ip','') or '')" 2>/dev/null)
  log "status=$ST ip=$IP"
  [ "$ST" = "active" ] && [ -n "$IP" ] && break
  sleep 20
done
[ -z "$IP" ] && { log "no IP before deadline"; exit 1; }

log "waiting for ssh ..."
for i in $(seq 1 40); do ssh $SSHOPT ubuntu@"$IP" "echo ready" 2>/dev/null && break; sleep 15; done

log "syncing whole kit -> ~/lambda-kit ..."
ssh $SSHOPT ubuntu@"$IP" "mkdir -p ~/lambda-kit" 2>/dev/null
scp $SSHOPT -q "$KITDIR"/*.py "$KITDIR"/*.sh ubuntu@"$IP":~/lambda-kit/ 2>/dev/null

log "provisioning box (installs Ollama on the ephemeral disk; models persist on FS) ..."
# ROOT-CAUSE FIX (2026-07-11): a fresh Lambda box has NO Ollama installed -- the
# ephemeral disk is wiped every boot; only the MODELS persist on the FS. serve.sh
# assumes Ollama is already installed and just starts it, so on a fresh box it
# silently fails and every /api/generate|/embed returns connection-refused, which
# then shows up as fake 0.0 recall. provision.sh installs Ollama + points its model
# store at the FS + pulls any missing models. It is idempotent and fast on relaunch
# (models already cached). The FS is virtiofs, not NFS -- ignore NFS perm framing.
ssh $SSHOPT ubuntu@"$IP" "$RPATH; PFS=$PFS bash ~/lambda-kit/provision.sh" 2>&1 | tail -8

# HARD READINESS GATE: do NOT run the benchmark until a REAL generate AND a REAL
# embed both succeed. This is what stops connection-refused errors being recorded
# as fake '0.0 recall' / '0.08s answer'. Abort loudly (trap still terminates box).
log "readiness gate: verifying real generate + embed ..."
GATE=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; \
  for i \$(seq 1 30); do \
    G=\$(curl -s -m 60 http://localhost:11434/api/generate -d '{\"model\":\"qwen2.5:14b-instruct\",\"prompt\":\"say OK\",\"stream\":false}' | python3 -c 'import sys,json;print((json.load(sys.stdin).get(\"response\") or \"\").strip()[:20])' 2>/dev/null); \
    E=\$(curl -s -m 60 http://localhost:11434/api/embed -d '{\"model\":\"nomic-embed-text\",\"input\":\"warm\"}' | python3 -c 'import sys,json;print(len(json.load(sys.stdin).get(\"embeddings\",[[]])[0]))' 2>/dev/null); \
    if [ -n \"\$G\" ] && [ \"\${E:-0}\" -gt 100 ]; then echo \"GATE_OK gen=[\$G] embed_dim=\$E\"; exit 0; fi; \
    echo \"gate retry \$i: gen=[\$G] embed_dim=[\$E]\"; \
    sudo systemctl restart ollama.service 2>/dev/null; sleep 8; \
  done; echo GATE_FAIL; exit 1" 2>&1)
echo "$GATE" | tail -6
if ! echo "$GATE" | grep -q GATE_OK; then
  log "ABORT: Ollama never served a real generate+embed. NOT running benchmark (no fake numbers)."
  exit 4
fi
log "gate passed; Ollama is genuinely serving."

log "RUNNING campaign command ..."
ssh $SSHOPT ubuntu@"$IP" "$RPATH; export PFS=$PFS BIN=$PFS/repo/perseus-vault/target/release/perseus-vault; $REMOTE_CMD" 2>&1 | tee "$RESULTS/${NAME}_run.log" | tail -50

for f in $PULL_FILES; do
  base=$(basename "$f")
  scp $SSHOPT -q ubuntu@"$IP":"$f" "$RESULTS/$base" 2>/dev/null \
    && log "SAVED $RESULTS/$base" || log "WARN: could not pull $f"
done

log "campaign done; trap will terminate."
