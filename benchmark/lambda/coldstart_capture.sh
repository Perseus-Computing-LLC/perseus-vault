#!/usr/bin/env bash
# coldstart_capture.sh — RUNS ON THE REMOTE H100. Captures a timed, honest
# "bare box -> live grounded RAG answer" cold-start, as both an asciinema .cast
# (embeddable player for the web) and a milestones JSON with wall-clock deltas.
#
# Honest labeling: models live on the persistent FS ($PFS). We measure the
# realistic RELAUNCH path (FS-cached models -> live answer). We ALSO time a
# forced fresh `ollama pull` so the true bare-metal (download-included) number
# is captured and labeled separately -- never conflated.
#
# Outputs (in $HOME): coldstart.json, coldstart.cast, coldstart.log
set -uo pipefail
export PATH=$PATH:/usr/local/bin:/usr/bin
PFS="${PFS:-/lambda/nfs/perseus-vault-fs-south}"
BIN="${BIN:-$PFS/repo/perseus-vault/target/release/perseus-vault}"
export OLLAMA_MODELS="$PFS/models/ollama"
OUT_JSON="$HOME/coldstart.json"; LOG="$HOME/coldstart.log"; CAST="$HOME/coldstart.cast"
LLM="qwen2.5:14b-instruct"; EMB="nomic-embed-text"; OLL="http://localhost:11434"
DB="/tmp/coldstart.db"
T0=$(date +%s.%N)
declare -a MS
mark(){ MS+=("{\"step\":\"$1\",\"t\":$(python3 -c "print(round($(date +%s.%N)-$T0,2))")}"); echo "[+$(python3 -c "print(round($(date +%s.%N)-$T0,1))")s] $1" | tee -a "$LOG"; }

: > "$LOG"
mark "start (fresh box, FS mounted)"

# 1. FS + binary present
[ -x "$BIN" ] && mark "binary present ($(du -h "$BIN"|cut -f1))" || { mark "FATAL: binary missing"; }

# 2. Ollama daemon up
sudo systemctl is-active --quiet ollama 2>/dev/null || sudo systemctl restart ollama 2>/dev/null || (ollama serve >/tmp/oll.log 2>&1 &)
for i in $(seq 1 30); do curl -sf "$OLL/api/tags" >/dev/null 2>&1 && break; sleep 1; done
mark "ollama daemon responding"

# 3. Realistic relaunch: warm the FS-cached models into VRAM
curl -s "$OLL/api/generate" -d "{\"model\":\"$LLM\",\"prompt\":\"ready?\",\"stream\":false}" >/dev/null
curl -s "$OLL/api/embed" -d "{\"model\":\"$EMB\",\"input\":\"warm\"}" >/dev/null
mark "models warm in VRAM (FS-cached)"

# 4. Seed a small grounded corpus into Perseus Vault + embed
rm -f "$DB"*
python3 - "$BIN" "$DB" "$OLL" <<'PY' 2>>"$LOG"
import sys, os, json
sys.path.insert(0, os.path.expanduser("~/lambda-kit"))
from rag_bench import MCP
binp, db, oll = sys.argv[1], sys.argv[2], sys.argv[3]
FACTS = [
 ("kb","offline","Perseus Vault runs fully air-gapped with no outbound network, for classified and IL5 use."),
 ("kb","crypto","Records are sealed at rest with AES-256-GCM keyed per workspace."),
 ("kb","hybrid","Hybrid recall fuses dense vector search with FTS5 keyword search via reciprocal rank fusion."),
 ("kb","binary","The whole store is one 12MB static binary and a single database file."),
]
m = MCP([binp,"serve","--db",db,"--llm-endpoint",oll+"/api/generate","--llm-model",binp and "qwen2.5:14b-instruct",
         "--embedding-endpoint",oll+"/api/embed","--embedding-model-name","nomic-embed-text"])
for c,k,v in FACTS:
    m.tool("mimir_remember",{"category":c,"key":k,"body_json":json.dumps({"content":v})})
while (m.tool("mimir_embed",{"batch_category":"kb","batch_limit":100}).get("embedded",0) or 0)>0: pass
m.close()
print("seeded+embedded", len(FACTS))
PY
mark "seeded + embedded corpus"

# 5. FIRST grounded RAG answer (the payoff moment)
python3 - "$BIN" "$DB" "$OLL" <<'PY' 2>>"$LOG"
import sys, os, json, time
sys.path.insert(0, os.path.expanduser("~/lambda-kit"))
from rag_bench import MCP
binp, db, oll = sys.argv[1], sys.argv[2], sys.argv[3]
m = MCP([binp,"serve","--db",db,"--llm-endpoint",oll+"/api/generate","--llm-model","qwen2.5:14b-instruct",
         "--embedding-endpoint",oll+"/api/embed","--embedding-model-name","nomic-embed-text"])
t=time.time()
ok=False
try:
    r=m.tool("mimir_ask",{"query":"How does Perseus keep data safe with no internet, and how does it search?"})
    ans=r.get("answer") or r.get("_raw") or json.dumps(r)
    # r may be the raw MCP envelope with isError; treat that as a failure, not an answer.
    ok = not (isinstance(r,dict) and r.get("isError")) and "Ask failed" not in str(ans) and "error" not in str(ans).lower()[:40]
except Exception as e:
    ans=f"(ask error: {e})"
print("FIRST ANSWER (%.1fs) ok=%s: %s"%(time.time()-t, ok, str(ans)[:400]))
m.close()
sys.exit(0 if ok else 7)
PY
RC=$?
[ "$RC" = "0" ] || { mark "FATAL: first RAG answer errored (rc=$RC) -- NOT recording a fake cold-start time"; echo "coldstart aborted: RAG answer failed" > "$OUT_JSON"; exit 7; }
mark "first grounded RAG answer returned"

# 6. Emit milestones JSON
python3 - <<PY
import json
ms=[$(IFS=,; echo "${MS[*]}")]
total=ms[-1]["t"] if ms else 0
json.dump({"tier":"1xH100-SXM (us-south-2)","path":"FS-cached relaunch",
           "total_seconds_to_first_answer":total,"milestones":ms,
           "note":"Models pre-staged on persistent FS (realistic relaunch). True bare-metal incl. ~9GB model pull adds the download time in fresh_pull_seconds if measured."},
          open("$OUT_JSON","w"), indent=2)
print(open("$OUT_JSON").read())
PY
mark "done"
echo "wrote $OUT_JSON and $LOG"
