#!/usr/bin/env bash
# Assemble the Perseus Vault MCP bundle (.mcpb) staging directory.
#
# Background: an .mcpb is a zip whose root holds manifest.json and the actual
# server executable(s) — Claude Desktop unpacks it and launches the binary
# directly, so the binary MUST be inside the bundle. Our first submission shipped
# the manifest alone (no binary), which cannot be installed or reviewed.
#
# This script stages a self-contained bundle from per-platform binaries produced
# by the CI matrix and dropped under ./artifacts/ (see .github/workflows/mcpb.yml):
#
#   artifacts/bin-darwin/perseus-vault        macOS universal (arm64 + x86_64, lipo'd)
#   artifacts/bin-win32/perseus-vault.exe     Windows x86_64
#   artifacts/bin-linux/perseus-vault-linux   Linux x86_64 (musl, static)
#
# The lite (`--no-default-features`) flavor is bundled on purpose: it is fully
# static with no runtime deps, so it "just runs" inside Claude Desktop's sandbox
# and keeps the bundle small (~20 MB vs ~90 MB for the bundled-embeddings build).
# Zero-config semantic search (bundled ONNX embeddings) stays available via the
# one-line installer, Docker image, and MCP registry entry.
#
# Layout produced (matches manifest.json server.entry_point / platform_overrides):
#
#   build-mcpb/
#   ├── manifest.json
#   ├── icon.png
#   ├── README.md
#   ├── LICENSE
#   └── server/
#       ├── perseus-vault         (darwin, universal — the base command)
#       ├── perseus-vault.exe     (win32 override)
#       └── perseus-vault-linux   (linux override)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ART="${ARTIFACTS_DIR:-$ROOT/artifacts}"
BUILD="$ROOT/build-mcpb"

echo "==> Staging bundle from artifacts in: $ART"
rm -rf "$BUILD"
mkdir -p "$BUILD/server"

# --- manifest (bundle root) ---
cp "$ROOT/manifest.json" "$BUILD/manifest.json"

# --- icon (optional but shown in the directory) ---
if [ -f "$ROOT/assets/mimir-400.png" ]; then
  cp "$ROOT/assets/mimir-400.png" "$BUILD/icon.png"
fi

# --- docs (optional) ---
[ -f "$ROOT/README.md" ] && cp "$ROOT/README.md" "$BUILD/README.md"
[ -f "$ROOT/LICENSE" ]   && cp "$ROOT/LICENSE"   "$BUILD/LICENSE"

# --- server binaries ---
copy_bin() {
  local src="$1" dest="$2"
  if [ ! -f "$src" ]; then
    echo "ERROR: missing binary: $src" >&2
    exit 1
  fi
  cp "$src" "$dest"
}
copy_bin "$ART/bin-darwin/perseus-vault"      "$BUILD/server/perseus-vault"
copy_bin "$ART/bin-win32/perseus-vault.exe"   "$BUILD/server/perseus-vault.exe"
copy_bin "$ART/bin-linux/perseus-vault-linux" "$BUILD/server/perseus-vault-linux"
chmod +x "$BUILD/server/perseus-vault" "$BUILD/server/perseus-vault-linux"

echo "==> Staged bundle contents:"
find "$BUILD" -type f -exec ls -lh {} \; | awk '{print $5"\t"$NF}'

echo "==> Bundle staged at: $BUILD"
