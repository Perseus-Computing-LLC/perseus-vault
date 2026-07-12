#!/usr/bin/env bash
# run_scale100k_durable.sh — launch 1x H100, provision, seed+embed+recall the 100k
# scale benchmark with the #591 embed-model fix, writing DB + result to the
# PERSISTENT FS so nothing is lost on termination. Robust: terminates ONLY on a
# definitive DONE marker or the hard deadline -- never on a transient SSH failure.
set -uo pipefail
KEY="$(cat "${LAMBDA_KEY_FILE:?set LAMBDA_KEY_FILE to the path of your Lambda API key file}")"
API="https://cloud.lambdalabs.com/api/v1"
REGION="us-south-2"; ITYPE="gpu_1x_h100_sxm5"; FS="perseus-vault-fs-south"; SSHKEY="hermes"
PFS="/lambda/nfs/perseus-vault-fs-south"
KITDIR="${KITDIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
RES="$KITDIR/results"; IDFILE="$KITDIR/.scale100k_id"
SSHOPT="-i ${LAMBDA_SSH_KEY:-$HOME/.ssh/lambda_ed25519} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
RPATH='export PATH=$PATH:/usr/local/bin:/usr/bin'
BIN="$PFS/repo/perseus-vault/target/release/perseus-vault"
DB="$PFS/bench/scale100k.db"          # PERSISTENT: survives termination, reusable
JSON="$PFS/bench/scale100k.json"
DONE="$PFS/bench/scale100k.DONE"      # unambiguous completion marker
DEADLINE=$(( $(date +%s) + 3*3600 ))
log(){ echo "[$(date +%H:%M:%S)] $*"; }
api(){ curl -s -u "$KEY:" "$@"; }
count_running(){ api "$API/instances" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data']))" 2>/dev/null; }

ID=""
terminate(){
  local id="${ID:-$(cat "$IDFILE" 2>/dev/null)}"
  [ -z "$id" ] && return
  log "TERMINATING $id"
  api -X POST "$API/instance-operations/terminate" -H "Content-Type: application/json" -d "{\"instance_ids\":[\"$id\"]}" >/dev/null
  sleep 6; log "instances running after terminate: $(count_running)"; rm -f "$IDFILE"
}
trap terminate EXIT INT TERM
mkdir -p "$RES"

# overlap guard: poll until 0
for i in $(seq 1 24); do R=$(count_running); [ "${R:-1}" = 0 ] && break; log "waiting $R to clear ($i)"; sleep 5; done
[ "${R:-1}" != 0 ] && { log "ABORT: $R still running"; ID=""; trap - EXIT; exit 3; }

log "launching $ITYPE ..."
ID=$(api -X POST "$API/instance-operations/launch" -H "Content-Type: application/json" \
  -d "{\"region_name\":\"$REGION\",\"instance_type_name\":\"$ITYPE\",\"ssh_key_names\":[\"$SSHKEY\"],\"file_system_names\":[\"$FS\"],\"name\":\"pv-scale100k\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('instance_ids',[''])[0])" 2>/dev/null)
[ -z "$ID" ] && { log "LAUNCH FAILED"; exit 1; }
echo "$ID" > "$IDFILE"; log "id=$ID"

IP=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  J=$(api "$API/instances/$ID")
  ST=$(echo "$J"|python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('status',''))" 2>/dev/null)
  IP=$(echo "$J"|python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('ip','') or '')" 2>/dev/null)
  log "status=$ST ip=$IP"; [ "$ST" = active ] && [ -n "$IP" ] && break; sleep 20
done
[ -z "$IP" ] && { log "no IP"; exit 1; }
for i in $(seq 1 40); do ssh $SSHOPT ubuntu@"$IP" "echo ready" 2>/dev/null && break; sleep 15; done

log "sync kit + provision ..."
ssh $SSHOPT ubuntu@"$IP" "mkdir -p ~/lambda-kit $PFS/bench" 2>/dev/null
scp $SSHOPT -q "$KITDIR"/*.py "$KITDIR"/*.sh ubuntu@"$IP":~/lambda-kit/ 2>/dev/null
ssh $SSHOPT ubuntu@"$IP" "$RPATH; PFS=$PFS bash ~/lambda-kit/provision.sh" 2>&1 | tail -6

# readiness gate: real generate + embed (runs gate.sh on the box; no nested quoting)
log "readiness gate ..."
GATE=$(ssh $SSHOPT ubuntu@"$IP" "bash ~/lambda-kit/gate.sh" 2>/dev/null)
echo "$GATE" | grep -q GATE_OK || { log "ABORT: ollama gate failed ($GATE)"; exit 4; }
log "gate OK"

# decide skip-seed: is the persistent DB already fully seeded (~99500+)?
SEEDED=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; python3 -c \"import sqlite3,os;p='$DB';print(sqlite3.connect(p).execute('select count(*) from entities').fetchone()[0] if os.path.exists(p) else 0)\" 2>/dev/null" 2>/dev/null)
SKIP=""; [ "${SEEDED:-0}" -ge 99000 ] && SKIP="--skip-seed" && log "reusing $SEEDED seeded entities (skip-seed)"
[ -z "$SKIP" ] && log "seeding fresh (existing=$SEEDED)" && ssh $SSHOPT ubuntu@"$IP" "rm -f $DB*" 2>/dev/null

# launch bench nohup, DONE marker written by wrapper on real completion only
log "starting benchmark (DB+result on persistent FS) ..."
ssh $SSHOPT ubuntu@"$IP" "$RPATH; rm -f $DONE; cd ~/lambda-kit; nohup bash -c '
  python3 -u scale_bench.py --bin $BIN --db $DB $SKIP \
    --llm-endpoint http://localhost:11434/api/generate --llm-model qwen2.5:14b-instruct \
    --embedding-endpoint http://localhost:11434/api/embed --embedding-model nomic-embed-text \
    --clusters 1000 --per-cluster 100 --tier \"1xH100-SXM (100k entities)\" --out $JSON \
    > $PFS/bench/scale100k.log 2>&1 && touch $DONE
' >/dev/null 2>&1 & echo started"

# robust wait: terminate ONLY on DONE marker or deadline. SSH failure => retry.
log "waiting for DONE marker (ssh failures are retried, not fatal) ..."
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  M=$(ssh $SSHOPT ubuntu@"$IP" "test -f $DONE && echo DONE || echo NO" 2>/dev/null)
  if [ "$M" = DONE ]; then
    log "DONE marker present; pulling results."
    scp $SSHOPT -q ubuntu@"$IP":"$JSON" "$RES/scale100k.json" 2>/dev/null && log "SAVED scale100k.json"
    scp $SSHOPT -q ubuntu@"$IP":"$PFS/bench/scale100k.log" "$RES/scale100k_run.log" 2>/dev/null
    break
  fi
  EMB=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; python3 -c \"import sqlite3;print(sqlite3.connect('$DB').execute('select count(*) from entities where embedding is not null').fetchone()[0])\" 2>/dev/null" 2>/dev/null)
  log "progress: embedded=${EMB:-<ssh unreachable, retrying>}"
  sleep 60
done
log "wait loop ended; trap terminates. (DB persists on FS for reuse.)"
