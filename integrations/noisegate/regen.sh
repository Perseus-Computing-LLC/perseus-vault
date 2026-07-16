#!/bin/sh
# Regenerate the golden `prepare --json` fixture from the perseus-vault binary.
#
# Seeds a THROWAWAY database with two synthetic entities (no real memory data),
# then captures the deterministic read-only pre-turn output. The result is
# byte-identical across repeated runs against the unchanged DB.
#
# Usage:  BIN=/path/to/perseus-vault ./regen.sh > prepare_output.golden.json
# (defaults to `perseus-vault` on PATH)
set -eu

BIN="${BIN:-perseus-vault}"
DB="$(mktemp -u "${TMPDIR:-/tmp}/pv-prepare-fixture.XXXXXX.db")"
trap 'rm -f "$DB"' EXIT

"$BIN" write --db "$DB" --category convention --key commit_discipline \
  --entity-type convention --always-on --importance 0.9 \
  --body '{"content":"Always run the full test suite before committing; never push directly to main."}' >/dev/null

"$BIN" write --db "$DB" --category insight --key auth_tokens \
  --entity-type insight --tags auth,security \
  --body '{"content":"The auth module issues JWT access tokens (15-min TTL) with refresh-token rotation."}' >/dev/null

"$BIN" prepare --json --task "refactor the auth module token handling" --db "$DB"
