#!/usr/bin/env bash
# poll_8x.sh — watch for high-end multi-GPU capacity via check_8x.py. Detect-only
# (does NOT launch — an 8x B200 is $53/hr). Exits 0 with CAPACITY_FOUND line when hit.
cd "$(dirname "$0")"
INTERVAL="${INTERVAL:-90}"
MAX_MIN="${MAX_MIN:-720}"
deadline=$(( $(date +%s) + MAX_MIN*60 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  HIT="$(python3 check_8x.py 2>/dev/null)"
  if [ -n "$HIT" ]; then
    echo "$HIT"
    exit 0
  fi
  echo "[$(date +%H:%M:%S)] no 8x/4x capacity yet; sleeping ${INTERVAL}s"
  sleep "$INTERVAL"
done
echo "POLL_TIMEOUT after ${MAX_MIN}min"; exit 1
