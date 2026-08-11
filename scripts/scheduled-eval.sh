#!/usr/bin/env bash
# Scheduled recall evaluation wrapper (issue #930).
#
# Cadence: nightly = maintain (curation after-action summary) + quality eval;
#          midday  = quality eval only.
# Requires the repo layout (the harness drives a checkout-built binary over
# MCP stdio) and PERSEUS_VAULT_BIN pointing at that binary.
#
# Usage:
#   PERSEUS_VAULT_BIN=/path/to/perseus-vault DB=/path/to/vault.db \
#     scheduled-eval.sh nightly|midday
set -euo pipefail

KIND="${1:-}"
if [[ "$KIND" != "nightly" && "$KIND" != "midday" ]]; then
  echo "usage: $0 nightly|midday" >&2
  exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB="${DB:-$PERSEUS_VAULT_DB_PATH}"
: "${DB:?set DB (or PERSEUS_VAULT_DB_PATH) to the vault database}"
: "${PERSEUS_VAULT_BIN:?set PERSEUS_VAULT_BIN to the checkout-built binary}"

OUT_DIR="${TMPDIR:-/tmp}/perseus-scheduled-eval"
mkdir -p "$OUT_DIR"

# 1. Nightly curation pass: maintain + after-action summary.
MAINTAIN_ARGS=()
if [[ "$KIND" == "nightly" ]]; then
  "$PERSEUS_VAULT_BIN" maintain --db "$DB" > "$OUT_DIR/maintain.json" 2>/dev/null \
    || { echo "maintain failed" >&2; exit 3; }
  MAINTAIN_ARGS=(--maintain-report "$OUT_DIR/maintain.json")
fi

# 2. Deterministic quality eval (no LLM, no network).
python3 "$REPO/benchmark/quality/run.py" --out "$OUT_DIR/quality.json" \
  || { echo "quality run failed" >&2; exit 4; }
python3 "$REPO/benchmark/quality/scorecard.py" "$OUT_DIR/quality.json" \
  --out "$OUT_DIR/scorecard.json" \
  || { echo "scorecard failed" >&2; exit 5; }

# 3. Record the eval run; regression breaches are stored with it.
"$PERSEUS_VAULT_BIN" eval record --db "$DB" --kind "$KIND" \
  --report "$OUT_DIR/quality.json" --scorecard "$OUT_DIR/scorecard.json" \
  "${MAINTAIN_ARGS[@]}" --created-by scheduled-eval.sh
