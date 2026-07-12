#!/usr/bin/env bash
# competitors_up.sh — one-command bring-up of the competitor stacks measured by
# competitors_bench.py (#599): Letta (server + pgvector) and Zep's OSS engine
# Graphiti (which needs Neo4j). Everything points at the SAME local Ollama the
# rest of the bench uses, so the 4-way comparison is a single reproducible
# command alongside provision.sh / gate.sh. Tear down with competitors_down.sh.
#
# Encoded gotchas (learned the hard way in #599):
#   - Letta's Ollama EMBEDDING endpoint needs OLLAMA_BASE_URL=.../v1 or the
#     embed call 404s (the LLM endpoint tolerates either; the embedding-handle
#     resolution drops /v1).
#   - Neo4j needs the apoc plugin for Graphiti.
#   - Both containers use --network host so they reach host Ollama on :11434.
set -euo pipefail

OLLAMA_BASE=${OLLAMA_BASE:-http://localhost:11434}
NEO4J_PASSWORD=${NEO4J_PASSWORD:-password123}
LETTA_IMAGE=${LETTA_IMAGE:-letta/letta:latest}
NEO4J_IMAGE=${NEO4J_IMAGE:-neo4j:5.26}
WAIT_SECS=${WAIT_SECS:-180}

log(){ echo "[competitors_up] $*"; }
die(){ echo "[competitors_up] FATAL: $*" >&2; exit 1; }

# 0) preflight: Ollama up with both models the bench needs
curl -fsS "$OLLAMA_BASE/api/tags" >/dev/null \
  || die "Ollama not reachable at $OLLAMA_BASE (start it / set OLLAMA_BASE)"
for m in qwen2.5:14b-instruct nomic-embed-text; do
  curl -fsS "$OLLAMA_BASE/api/tags" | grep -q "$m" \
    || log "WARNING: model $m not in ollama tags — 'ollama pull $m' before benching"
done

# 1) Letta (agent server backed by embedded Postgres/pgvector)
log "starting letta ($LETTA_IMAGE)"
docker rm -f letta >/dev/null 2>&1 || true
docker run -d --name letta --network host \
  -e OLLAMA_BASE_URL="$OLLAMA_BASE/v1" \
  "$LETTA_IMAGE" >/dev/null

# 2) Neo4j with apoc (required by Graphiti / the Zep row)
log "starting neo4j ($NEO4J_IMAGE, apoc enabled)"
docker rm -f neo4j >/dev/null 2>&1 || true
docker run -d --name neo4j --network host \
  -e NEO4J_AUTH="neo4j/$NEO4J_PASSWORD" \
  -e NEO4J_PLUGINS='["apoc"]' \
  "$NEO4J_IMAGE" >/dev/null

# 3) python clients into the active venv/interpreter
log "installing python clients (letta-client, graphiti-core, packaging)"
python3 -m pip install -q letta-client graphiti-core packaging

# 4) wait for health — loud failure with container logs, never a silent hang
wait_for(){ # name url
  local name=$1 url=$2 t=0
  until curl -fsS "$url" >/dev/null 2>&1; do
    t=$((t+3)); [ "$t" -ge "$WAIT_SECS" ] && {
      docker logs --tail 30 "$name" >&2 || true
      die "$name not healthy after ${WAIT_SECS}s ($url)"
    }
    sleep 3
  done
  log "$name healthy ($url)"
}
wait_for letta  http://localhost:8283/v1/health/
wait_for neo4j  http://localhost:7474

log "ready. run: python3 competitors_bench.py --bin <vault-binary> --ollama-base $OLLAMA_BASE"
log "env for the bench: NEO4J_PASSWORD=$NEO4J_PASSWORD (bolt://localhost:7687), LETTA_BASE_URL=http://localhost:8283"
