#!/usr/bin/env bash
# sign-macos.sh — Developer ID sign + notarize a macOS binary in CI (#732).
#
# Unsigned (ad-hoc) release binaries are rejected by Claude Desktop's extension
# host on macOS even though they execute fine from a terminal — Gatekeeper's
# trust check fails (`spctl`: "no usable signature"). This script gives CI
# builds a real Developer ID signature plus a notarization ticket.
#
# Required environment (GitHub secrets; setup guide: docs/macos-signing.md):
#   APPLE_CERTIFICATE            base64-encoded .p12 export of the
#                                "Developer ID Application" certificate + key
#   APPLE_CERTIFICATE_PASSWORD   password for the .p12
#   APPLE_SIGNING_IDENTITY       e.g. "Developer ID Application: Perseus Computing LLC (ABCDE12345)"
#   APPLE_TEAM_ID                10-character Apple team ID
#   APPLE_NOTARY_KEY             base64-encoded App Store Connect API key (.p8)
#   APPLE_NOTARY_KEY_ID          App Store Connect API key ID
#   APPLE_NOTARY_ISSUER          App Store Connect API issuer UUID
#
# Usage:
#   scripts/sign-macos.sh <path-to-binary>            # sign + notarize
#   NOTARIZE=0 scripts/sign-macos.sh <path-to-binary> # sign only
#
# The script fails hard when credentials are missing or any step errors — the
# caller (workflow) is responsible for gating on APPLE_SIGNING_ENABLED so this
# only runs when signing is actually configured.
set -euo pipefail

BIN="${1:?usage: sign-macos.sh <path-to-binary>}"
[ -f "$BIN" ] || { echo "error: binary not found: $BIN" >&2; exit 1; }

: "${APPLE_CERTIFICATE:?missing secret}"
: "${APPLE_CERTIFICATE_PASSWORD:?missing secret}"
: "${APPLE_SIGNING_IDENTITY:?missing secret}"
: "${APPLE_TEAM_ID:?missing secret}"

NOTARIZE="${NOTARIZE:-1}"

# ── Ephemeral keychain ───────────────────────────────────────────────────────
# Never touch the login keychain on a shared runner: create a throwaway
# keychain, import the cert, sign, then delete it.
KEYCHAIN="perseus-sign-$$.keychain-db"
KEYCHAIN_PASSWORD="$(openssl rand -hex 16)"
CERT_P12="$(mktemp /tmp/perseus-cert-XXXXXX.p12)"

cleanup() {
  security delete-keychain "$KEYCHAIN" >/dev/null 2>&1 || true
  rm -f "$CERT_P12"
}
trap cleanup EXIT

echo "==> Importing Developer ID certificate into ephemeral keychain"
echo "$APPLE_CERTIFICATE" | base64 --decode > "$CERT_P12"
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 21600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$CERT_P12" -k "$KEYCHAIN" \
  -P "$APPLE_CERTIFICATE_PASSWORD" -T /usr/bin/codesign
# Allow codesign to use the private key without a UI prompt.
security set-key-partition-list -S apple-tool:,apple: \
  -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

# ── Sign ─────────────────────────────────────────────────────────────────────
# --options runtime: hardened runtime, required for notarization.
# --timestamp:       secure timestamp so the signature outlives the cert.
echo "==> Signing $BIN as: $APPLE_SIGNING_IDENTITY"
codesign --force --options runtime --timestamp \
  --sign "$APPLE_SIGNING_IDENTITY" "$BIN"
codesign --verify --verbose=2 "$BIN"
spctl -a -t exec -vv "$BIN" || true   # informational pre-notarization

# ── Notarize ─────────────────────────────────────────────────────────────────
# Bare Mach-O binaries cannot be stapled; the ticket lives in Apple's notary
# service and Gatekeeper fetches it online. We still notarize so the binary is
# trusted on first launch (this is the check Claude Desktop's extension host
# effectively depends on).
if [ "$NOTARIZE" = "1" ]; then
  : "${APPLE_NOTARY_KEY:?missing secret (or set NOTARIZE=0)}"
  : "${APPLE_NOTARY_KEY_ID:?missing secret (or set NOTARIZE=0)}"
  : "${APPLE_NOTARY_ISSUER:?missing secret (or set NOTARIZE=0)}"

  echo "==> Submitting to Apple notary service"
  KEY_DIR="$HOME/private_keys"
  mkdir -p "$KEY_DIR"
  echo "$APPLE_NOTARY_KEY" | base64 --decode > "$KEY_DIR/AuthKey_${APPLE_NOTARY_KEY_ID}.p8"

  ZIP="$(mktemp /tmp/perseus-notary-XXXXXX.zip)"
  ditto -c -k --keepParent "$BIN" "$ZIP"
  xcrun notarytool submit "$ZIP" \
    --key "$KEY_DIR/AuthKey_${APPLE_NOTARY_KEY_ID}.p8" \
    --key-id "$APPLE_NOTARY_KEY_ID" \
    --issuer "$APPLE_NOTARY_ISSUER" \
    --wait
  rm -f "$ZIP"
  echo "==> Notarization accepted"
fi

if [ "$NOTARIZE" = "1" ]; then
  echo "==> Done: $BIN is Developer ID signed and notarized"
else
  echo "==> Done: $BIN is Developer ID signed (notarization skipped)"
fi
