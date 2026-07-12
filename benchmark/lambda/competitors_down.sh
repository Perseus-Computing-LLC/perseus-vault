#!/usr/bin/env bash
# competitors_down.sh — tear down the competitor stacks started by
# competitors_up.sh (Letta + Neo4j containers). Leaves Ollama and the vault
# untouched. Safe to run when nothing is up.
set -euo pipefail
for c in letta neo4j; do
  if docker rm -f "$c" >/dev/null 2>&1; then
    echo "[competitors_down] removed $c"
  else
    echo "[competitors_down] $c not running"
  fi
done
