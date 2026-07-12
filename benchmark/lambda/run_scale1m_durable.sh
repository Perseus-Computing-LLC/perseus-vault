#!/usr/bin/env bash
# run_scale1m_durable.sh — 1,000,000-entity recall scale point, fleet-embedded.
#
# Polls for a multi-GPU node in us-south-2 (the FS is region-locked there),
# preferring 8xH100; launches it, provisions, brings up the per-GPU Ollama fleet
# + nginx LB (serve_fleet.sh), seeds 1M via mimir_remember (real dedup), embeds
# the corpus CLIENT-SIDE across all GPUs (scale_bench_1m.py --embed-fleet), then
# measures uniform + warm-set recall. DB + result live on the PERSISTENT FS so a
# timeout/termination is resumable. Terminates the instance ONLY on a definitive
# DONE marker or the hard run deadline — never on a transient SSH failure. Does
# NOT touch any instance it did not launch (parallel-safety).
set -uo pipefail
KEY="$(cat /root/.minions/workspace/uploads/6d92c114-16e4-4aad-ba4a-4c7406bea01a/5402f6bf-f7c9-4189-b617-7ee6c9fe163d-hermes.txt)"
API="https://cloud.lambdalabs.com/api/v1"
REGION="us-south-2"; FS="perseus-vault-fs-south"; SSHKEY="hermes"
# Capacity preference order (all must be launched in us-south-2):
PREF=(gpu_8x_h100_sxm5 gpu_4x_h100_sxm5 gpu_8x_a100_80gb_sxm4 gpu_8x_a100 gpu_2x_h100_sxm5 gpu_2x_a100)
PFS="/lambda/nfs/perseus-vault-fs-south"
KITDIR="/opt/data/webui/minions/.minions-data/workspace/lambda-kit"
RES="$KITDIR/results"; IDFILE="$KITDIR/.scale1m_id"; STATE="$KITDIR/.scale1m_state"
SSHOPT="-i /root/.ssh/lambda_ed25519 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=3"
RPATH='export PATH=$PATH:/usr/local/bin:/usr/bin'
BIN="$PFS/repo/perseus-vault/target/release/perseus-vault"
DB="$PFS/bench/scale1m.db"
JSON="$PFS/bench/scale1m.json"
LOG="$PFS/bench/scale1m.log"
DONE="$PFS/bench/scale1m.DONE"
POLL_INTERVAL="${POLL_INTERVAL:-90}"
MAX_POLL_MIN="${MAX_POLL_MIN:-720}"   # arm for up to 12h waiting on capacity

log(){ echo "[$(date +%H:%M:%S)] $*"; echo "[$(date +%F\ %T)] $*" >> "$STATE"; }
api(){ curl -s -u "$KEY:" "$@"; }
count_running(){ api "$API/instances" | python3 -c "import sys,json;print(len(json.load(sys.stdin)['data']))" 2>/dev/null; }

ID=""
terminate(){
  local id="${ID:-$(cat "$IDFILE" 2>/dev/null)}"
  [ -z "$id" ] && { log "no instance to terminate (poll-only exit)"; return; }
  log "TERMINATING $id"
  api -X POST "$API/instance-operations/terminate" -H "Content-Type: application/json" -d "{\"instance_ids\":[\"$id\"]}" >/dev/null
  sleep 6; log "instances running after terminate: $(count_running)"; rm -f "$IDFILE"
}
trap terminate EXIT INT TERM
mkdir -p "$RES"

# ── pick a type with capacity in us-south-2 (poll up to MAX_POLL_MIN) ──────────
ITYPE=""; NGPU=0
poll_deadline=$(( $(date +%s) + MAX_POLL_MIN*60 ))
log "polling for multi-GPU capacity in $REGION (pref: ${PREF[*]})"
while [ "$(date +%s)" -lt "$poll_deadline" ]; do
  AVAIL="$(api "$API/instance-types")"
  for t in "${PREF[@]}"; do
    HIT=$(echo "$AVAIL" | python3 -c "
import sys,json
d=json.load(sys.stdin)['data']; t='$t'
v=d.get(t)
if v and any(r['name']=='$REGION' for r in v.get('regions_with_capacity_available',[])):
    print(v['instance_type'].get('specs',{}).get('gpus',0))
" 2>/dev/null)
    if [ -n "$HIT" ] && [ "${HIT:-0}" -gt 1 ]; then ITYPE="$t"; NGPU="$HIT"; break; fi
  done
  [ -n "$ITYPE" ] && { log "CAPACITY_FOUND $ITYPE gpus=$NGPU in $REGION"; break; }
  log "no multi-GPU capacity in $REGION yet; sleeping ${POLL_INTERVAL}s"
  sleep "$POLL_INTERVAL"
done
[ -z "$ITYPE" ] && { log "POLL_TIMEOUT: no multi-GPU capacity in $REGION within ${MAX_POLL_MIN}min"; ID=""; trap - EXIT; exit 2; }

# Scale the RUN deadline to GPU count: ~26min embed @8 GPUs, plus seed+recall+setup.
# 8 GPUs -> 4h; fewer GPUs embed slower, so give more head-room (capped 6h).
RUN_HOURS=4; [ "$NGPU" -lt 8 ] && RUN_HOURS=5; [ "$NGPU" -le 2 ] && RUN_HOURS=6
DEADLINE=$(( $(date +%s) + RUN_HOURS*3600 ))
log "run deadline: ${RUN_HOURS}h from launch"

# ── overlap guard: only proceed if 0 instances currently running ──────────────
for i in $(seq 1 24); do R=$(count_running); [ "${R:-1}" = 0 ] && break; log "waiting $R instance(s) to clear ($i)"; sleep 5; done
[ "${R:-1}" != 0 ] && { log "ABORT: $R instance(s) still running (not mine to touch)"; ID=""; trap - EXIT; exit 3; }

# ── launch ────────────────────────────────────────────────────────────────────
log "launching $ITYPE ($NGPU GPU) in $REGION ..."
ID=$(api -X POST "$API/instance-operations/launch" -H "Content-Type: application/json" \
  -d "{\"region_name\":\"$REGION\",\"instance_type_name\":\"$ITYPE\",\"ssh_key_names\":[\"$SSHKEY\"],\"file_system_names\":[\"$FS\"],\"name\":\"pv-scale1m\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('instance_ids',[''])[0])" 2>/dev/null)
[ -z "$ID" ] && { log "LAUNCH FAILED: $(api "$API/instance-types" >/dev/null; echo see-log)"; exit 1; }
echo "$ID" > "$IDFILE"; log "id=$ID"

# ── wait active + IP ────────────────────────────────────────────────────────────
IP=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  J=$(api "$API/instances/$ID")
  ST=$(echo "$J"|python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('status',''))" 2>/dev/null)
  IP=$(echo "$J"|python3 -c "import sys,json;print(json.load(sys.stdin).get('data',{}).get('ip','') or '')" 2>/dev/null)
  log "status=$ST ip=$IP"; [ "$ST" = active ] && [ -n "$IP" ] && break; sleep 20
done
[ -z "$IP" ] && { log "no IP before deadline"; exit 1; }
ssh-keygen -f /root/.ssh/known_hosts -R "$IP" >/dev/null 2>&1 || true
for i in $(seq 1 40); do ssh $SSHOPT ubuntu@"$IP" "echo ready" 2>/dev/null && break; sleep 15; done

# ── provision + fleet ────────────────────────────────────────────────────────
log "sync kit + provision ..."
ssh $SSHOPT ubuntu@"$IP" "mkdir -p ~/lambda-kit $PFS/bench" 2>/dev/null
scp $SSHOPT -q "$KITDIR"/*.py "$KITDIR"/*.sh ubuntu@"$IP":~/lambda-kit/ 2>/dev/null
ssh $SSHOPT ubuntu@"$IP" "$RPATH; PFS=$PFS bash ~/lambda-kit/provision.sh" 2>&1 | tail -8
log "bringing up $NGPU-GPU Ollama fleet (per-GPU daemons; nginx LB optional) ..."
# Launch serve_fleet DETACHED: it backgrounds one nohup'd ollama daemon per GPU,
# and if we ran it foreground over ssh the session would hang waiting on those
# daemons. Its nginx step can also block indefinitely on an apt/dpkg lock — and
# we do NOT need the LB (both fleet_embed and the binary's query embed hit the
# per-GPU ports directly). So detach, then poll the ports for real readiness.
ssh $SSHOPT ubuntu@"$IP" "$RPATH; mkdir -p $PFS/logs; PFS=$PFS NGPU=$NGPU nohup bash ~/lambda-kit/serve_fleet.sh > $PFS/logs/serve_fleet.out 2>&1 </dev/null & echo launched"
for t in $(seq 1 40); do
  READY=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; ok=1; for p in \$(seq 11434 $((11434+NGPU-1))); do curl -sf -m5 http://127.0.0.1:\$p/api/tags >/dev/null 2>&1 || ok=0; done; echo \$ok" 2>/dev/null)
  [ "$READY" = 1 ] && { log "fleet daemons up ($NGPU)"; break; }
  log "waiting for $NGPU fleet daemons ($t)"; sleep 10
done

# ── fleet readiness gate: every daemon must embed dim>100, :11434 must generate ─
log "fleet readiness gate ..."
GATE=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; python3 - <<'PY'
import json,urllib.request
ok=True
for p in range($NGPU):
    port=11434+p
    try:
        r=json.loads(urllib.request.urlopen(urllib.request.Request(
            f'http://127.0.0.1:{port}/api/embed',
            data=json.dumps({'model':'nomic-embed-text','input':'x'}).encode(),
            headers={'Content-Type':'application/json'}),timeout=60).read())
        d=len(r['embeddings'][0])
        print(f'port {port} dim={d}')
        ok = ok and d>100
    except Exception as e:
        print(f'port {port} ERR {e}'); ok=False
try:
    g=json.loads(urllib.request.urlopen(urllib.request.Request(
        'http://127.0.0.1:11434/api/generate',
        data=json.dumps({'model':'qwen2.5:14b-instruct','prompt':'OK','stream':False}).encode(),
        headers={'Content-Type':'application/json'}),timeout=120).read())
    print('generate ok:', (g.get('response') or '')[:8])
except Exception as e:
    print('generate ERR', e)  # non-fatal: recall path does not call the LLM
print('GATE_OK' if ok else 'GATE_FAIL')
PY" 2>/dev/null)
echo "$GATE"
echo "$GATE" | grep -q GATE_OK || { log "ABORT: fleet gate failed"; exit 4; }
log "gate OK ($NGPU daemons embedding)"

# ── seed decision: reuse persisted corpus if already ~1M ────────────────────────
SEEDED=$(ssh $SSHOPT ubuntu@"$IP" "$RPATH; python3 -c \"import sqlite3,os;p='$DB';print(sqlite3.connect(p).execute('select count(*) from entities').fetchone()[0] if os.path.exists(p) else 0)\" 2>/dev/null" 2>/dev/null)
SKIP=""; [ "${SEEDED:-0}" -ge 990000 ] && SKIP="--skip-seed" && log "reusing $SEEDED seeded entities (skip-seed)"
[ -z "$SKIP" ] && log "seeding fresh (existing=$SEEDED)" && ssh $SSHOPT ubuntu@"$IP" "rm -f $DB*" 2>/dev/null

# ── run the 1M benchmark (nohup; DONE only on real completion) ──────────────────
log "starting 1M benchmark (fleet embed across $NGPU GPUs; DB+result on FS) ..."
ssh $SSHOPT ubuntu@"$IP" "$RPATH; rm -f $DONE; cd ~/lambda-kit; source $PFS/venv/bin/activate 2>/dev/null; nohup bash -c '
  python3 -u scale_bench_1m.py --bin $BIN --db $DB $SKIP \
    --llm-endpoint http://localhost:11434/api/generate --llm-model qwen2.5:14b-instruct \
    --embedding-endpoint http://localhost:11434/api/embed --embedding-model nomic-embed-text \
    --clusters 10000 --per-cluster 100 \
    --embed-fleet $NGPU --fleet-concurrency 64 \
    --sample-queries 2000 --sample-seed 1337 --warm-set --max-scan 50000 \
    --tier \"${NGPU}xGPU-fleet ($ITYPE, 1M entities)\" --out $JSON \
    > $LOG 2>&1 && touch $DONE
' >/dev/null 2>&1 & echo started"

# ── robust wait: terminate ONLY on DONE or deadline; SSH failures are retried ──
log "waiting for DONE marker ..."
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  M=$(ssh $SSHOPT ubuntu@"$IP" "test -f $DONE && echo DONE || echo NO" 2>/dev/null)
  if [ "$M" = DONE ]; then
    log "DONE; pulling results."
    scp $SSHOPT -q ubuntu@"$IP":"$JSON" "$RES/scale_1m_distinct.json" 2>/dev/null && log "SAVED scale_1m_distinct.json"
    scp $SSHOPT -q ubuntu@"$IP":"$LOG" "$RES/scale_1m_run.log" 2>/dev/null
    break
  fi
  TAIL=$(ssh $SSHOPT ubuntu@"$IP" "tail -1 $LOG 2>/dev/null" 2>/dev/null)
  log "progress: ${TAIL:-<ssh unreachable, retrying>}"
  sleep 60
done
log "wait loop ended; trap terminates instance. (DB persists on FS for reuse.)"
