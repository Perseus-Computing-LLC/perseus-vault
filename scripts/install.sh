#!/usr/bin/env bash
set -euo pipefail

# Perseus Vault (formerly "Mneme"/"Mimir") one-line installer
# Usage: curl -sSf https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/install.sh | sh
#
# Prebuilt binaries (published as .tar.gz + .sha256):
#   - macOS Apple Silicon (aarch64)      — full build
#   - Linux x86_64 (glibc)               — full build
#   - Linux aarch64 (musl)               — 'lite' build (no bundled embedding model)
# Intel macOS (x86_64), Windows, and aarch64-linux-full: build from source
#   (cargo install --git https://github.com/Perseus-Computing-LLC/perseus-vault).

BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

REPO="Perseus-Computing-LLC/perseus-vault"
BIN_DIR="${MIMIR_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${MIMIR_VERSION:-latest}"

echo -e "${BOLD}Perseus Vault Installer${RESET}"
echo "Persistent memory for AI agents — MCP-native, local-first, zero dependencies."
echo ""

# ── Detect platform ──────────────────────────────────────────────────
OS="$(uname -s)"
ARCH="$(uname -m)"

no_prebuilt() {
    echo -e "${RED}No prebuilt binary for $1.${RESET}"
    echo ""
    echo "Build from source with cargo (needs the Rust toolchain):"
    echo "  cargo install --git https://github.com/${REPO}"
    exit 1
}

# Map platform → the published asset base name. These match the release assets
# produced by taiki-e/upload-rust-binary-action. NOTE: aarch64-linux ships only
# the reduced 'lite' musl build; x86_64-linux and macOS-arm64 ship full builds.
VARIANT="full"
case "${OS}/${ARCH}" in
    Darwin/arm64|Darwin/aarch64) ASSET_BASE="perseus-vault-aarch64-apple-darwin" ;;
    Linux/x86_64|Linux/amd64)    ASSET_BASE="perseus-vault-x86_64-unknown-linux-gnu" ;;
    Linux/aarch64|Linux/arm64)   ASSET_BASE="perseus-vault-lite-aarch64-unknown-linux-musl"; VARIANT="lite" ;;
    Darwin/x86_64)               no_prebuilt "Intel macOS (x86_64)" ;;
    *)                           no_prebuilt "${OS}/${ARCH}" ;;
esac

# Pre-rename asset names for older pinned versions (MIMIR_VERSION=<old tag>),
# newest first. Best-effort: tried only if the current name 404s.
SUFFIX="${ASSET_BASE#perseus-vault-}"
LEGACY_BASES="mneme-${SUFFIX} mimir-${SUFFIX}"

release_url() {  # release_url <filename> → full download URL
    if [ "$VERSION" = "latest" ]; then
        echo "https://github.com/${REPO}/releases/latest/download/$1"
    else
        echo "https://github.com/${REPO}/releases/download/${VERSION}/$1"
    fi
}

fetch() {  # fetch <url> <out> → 0 on success (HTTP 200), nonzero otherwise
    if command -v curl >/dev/null 2>&1; then
        curl -sSfL -o "$2" "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1"
    else
        echo -e "${RED}Need curl or wget to download.${RESET}"; exit 1
    fi
}

verify_sha() {  # verify_sha <shafile>  (cwd must contain the referenced archive)
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -c "$1" >/dev/null 2>&1
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 -c "$1" >/dev/null 2>&1
    else
        # Security: fail closed. Previously this returned 0 (verification
        # silently skipped) on any host without a hashing tool, installing an
        # unverified binary. Require an explicit opt-out to proceed unverified.
        if [ "${PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM:-0}" = "1" ]; then
            echo -e "${YELLOW}⚠  No sha256sum/shasum found; PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM=1 set — proceeding UNVERIFIED.${RESET}"
            return 0
        fi
        echo -e "${RED}✗ No sha256sum/shasum tool available to verify the download.${RESET}"
        echo -e "${RED}  Install coreutils, or re-run with PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM=1 to bypass (NOT recommended).${RESET}"
        return 1
    fi
}

echo -e "→ Platform: ${BOLD}${OS}/${ARCH}${RESET}  (asset: ${ASSET_BASE}.tar.gz, ${VARIANT} build)"
echo -e "→ Installing to: ${BOLD}${BIN_DIR}${RESET}"
echo ""

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Download + verify + extract ──────────────────────────────────────
try_install() {  # try_install <asset_base> → 0 if downloaded/verified/extracted
    local base="$1"
    local tgz="${base}.tar.gz" sha="${base}.sha256"
    if ! fetch "$(release_url "$tgz")" "$TMP_DIR/$tgz"; then
        return 1
    fi
    echo -e "→ Downloaded ${BOLD}${tgz}${RESET}"
    if fetch "$(release_url "$sha")" "$TMP_DIR/$sha"; then
        if ( cd "$TMP_DIR" && verify_sha "$sha" ); then
            echo "→ Checksum verified"
        else
            echo -e "${RED}✗ Checksum verification FAILED for ${tgz} — aborting.${RESET}"
            exit 1
        fi
    else
        # Security: fail closed. A missing published checksum previously meant
        # "install unverified"; an attacker who can tamper with the archive can
        # equally suppress the .sha256 (same origin), so this downgrade defeated
        # the check entirely. Abort unless the operator explicitly opts out.
        if [ "${PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM:-0}" = "1" ]; then
            echo -e "${YELLOW}⚠  No checksum published for ${tgz}; PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM=1 set — proceeding UNVERIFIED.${RESET}"
        else
            echo -e "${RED}✗ No checksum published for ${tgz} — refusing to install an unverified binary.${RESET}"
            echo -e "${RED}  Re-run with PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM=1 to bypass (NOT recommended).${RESET}"
            exit 1
        fi
    fi
    tar -xzf "$TMP_DIR/$tgz" -C "$TMP_DIR"
    return 0
}

echo "→ Downloading perseus-vault..."
INSTALLED=1
for base in "$ASSET_BASE" $LEGACY_BASES; do
    if try_install "$base"; then INSTALLED=0; break; fi
    echo -e "${YELLOW}→ '${base}.tar.gz' not available for ${VERSION}, trying next name...${RESET}"
done
[ "$INSTALLED" -eq 0 ] || no_prebuilt "${OS}/${ARCH} (version: ${VERSION})"

# The archive contains a single 'perseus-vault' binary at its root.
BIN_SRC="$(find "$TMP_DIR" -type f -name 'perseus-vault' 2>/dev/null | head -n1)"
if [ -z "$BIN_SRC" ]; then
    # Fallback: first regular file that isn't the archive/checksum.
    BIN_SRC="$(find "$TMP_DIR" -type f ! -name '*.tar.gz' ! -name '*.sha256' 2>/dev/null | head -n1)"
fi
if [ -z "$BIN_SRC" ]; then
    echo -e "${RED}Extraction succeeded but no binary was found in the archive.${RESET}"
    exit 1
fi

# ── Install ──────────────────────────────────────────────────────────
mkdir -p "$BIN_DIR"
chmod +x "$BIN_SRC"
mv "$BIN_SRC" "$BIN_DIR/perseus-vault"
# Perseus Vault rename: keep "mneme" and "mimir" symlinks so existing MCP host
# configs/scripts that invoke either older command name keep working unchanged.
ln -sf "$BIN_DIR/perseus-vault" "$BIN_DIR/mneme"
ln -sf "$BIN_DIR/perseus-vault" "$BIN_DIR/mimir"

# macOS: ad-hoc code-sign so the binary is not killed on launch (#312). On Apple
# Silicon an unsigned binary is SIGKILLed (Killed: 9) by the OS binary policy —
# even with no quarantine xattr — so `perseus-vault --version`/`doctor` would
# produce no output. `codesign --sign -` applies an ad-hoc signature; harmless
# on Intel.
if [ "$OS" = "Darwin" ] && command -v codesign >/dev/null 2>&1; then
    if codesign --force --sign - "$BIN_DIR/perseus-vault" 2>/dev/null; then
        echo "→ Ad-hoc code-signed for macOS"
    else
        echo -e "${YELLOW}⚠  Could not code-sign. If 'perseus-vault' is Killed: 9, run:${RESET}"
        echo "     codesign --sign - $BIN_DIR/perseus-vault"
    fi
fi

# Check if BIN_DIR is on PATH
if ! echo "$PATH" | tr ':' '\n' | grep -qxF "$BIN_DIR"; then
    case "${SHELL:-}" in
        */zsh) RC="$HOME/.zshrc" ;;
        */bash) RC="$HOME/.bashrc" ;;
        */fish) RC="$HOME/.config/fish/config.fish" ;;
        *) RC="$HOME/.profile" ;;
    esac
    echo ""
    echo -e "${YELLOW}⚠  $BIN_DIR is not on your PATH.${RESET}"
    echo "   Add this to your shell config:"
    echo ""
    echo -e "   ${BOLD}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
    echo ""
    echo "   Or run:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $RC"
fi

if [ "$VARIANT" = "lite" ]; then
    echo ""
    echo -e "${YELLOW}Note:${RESET} installed the 'lite' build (no bundled embedding model)."
    echo "  Local embeddings need a model — point --embedding-model at an ONNX model,"
    echo "  or configure an embedding endpoint. FTS5 keyword search works without one."
fi

# ── Verify ───────────────────────────────────────────────────────────
echo ""
echo "→ Verifying install..."
"$BIN_DIR/perseus-vault" --version 2>/dev/null || true
echo ""
echo -e "${GREEN}${BOLD}✓ Perseus Vault installed to $BIN_DIR/perseus-vault${RESET}"
echo ""
echo "Quick start:"
echo "  perseus-vault serve --db ~/.mimir/data/perseus-vault.db"
echo ""
echo "MCP config (Claude Desktop, Cursor, Hermes, etc.):"
echo '  {'
echo '    "mcpServers": {'
echo '      "perseus-vault": {'
echo '        "command": "'"$BIN_DIR"'/perseus-vault",'
echo '        "args": ["serve", "--db", "~/.mimir/data/perseus-vault.db"]'
echo '      }'
echo '    }'
echo '  }'
echo ""
echo "Docs: https://github.com/${REPO}"
