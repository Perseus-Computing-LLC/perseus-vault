#!/usr/bin/env bash
# =============================================================================
#  Perseus Vault One-Shot Bootstrap (build from source)
#  Persistent memory engine for AI agents — MCP JSON-RPC stdio server
#
#  Usage:
#    curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
#
#  What this does:
#    1. Installs system dependencies (Rust toolchain via rustup, build tools)
#    2. Clones and builds Perseus Vault from source (release binary)
#    3. Installs the binary to ~/.local/bin/perseus-vault (+ perseus_vault/perseus-vault compat symlinks)
#    4. Creates the data directory and generates .env defaults
#    5. Verifies the installation and prints a success summary
#
#  Prefer scripts/install.sh if you just want a prebuilt binary; this script is
#  for building from source. Idempotent — safe to re-run. Existing binary is
#  only rebuilt if FORCE=1 or the repo checkout is stale.
# =============================================================================
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

ok()   { printf "${GREEN}✓${NC} %s\n" "$*"; }
warn() { printf "${YELLOW}⚠${NC} %s\n" "$*"; }
fail() { printf "${RED}✗${NC} %s\n" "$*" >&2; exit 1; }
info() { printf "${CYAN}→${NC} %s\n" "$*"; }
header() { printf "\n${BOLD}══ %s ══${NC}\n" "$*"; }

FORCE="${FORCE:-0}"
# Repo redirects from the historical perseus_vault/perseus-vault names, but use the canonical
# one so the clone URL matches what users see everywhere else.
VAULT_REPO="https://github.com/Perseus-Computing-LLC/perseus-vault.git"
# Script-local dirs. NOTE: PERSEUS_VAULT_DB_PATH is the *real* env var the binary reads
# (see default_db_path() in src/main.rs), so it keeps its name for compatibility;
# the default filename is the canonical perseus-vault.db.
PERSEUS_VAULT_DIR="${PERSEUS_VAULT_DIR:-$HOME/.perseus-vault}"
PERSEUS_VAULT_BIN_DIR="${PERSEUS_VAULT_BIN_DIR:-$HOME/.local/bin}"
PERSEUS_VAULT_DATA_DIR="${PERSEUS_VAULT_DATA_DIR:-$HOME/.perseus-vault/data}"
PERSEUS_VAULT_DB_PATH="${PERSEUS_VAULT_DB_PATH:-$PERSEUS_VAULT_DATA_DIR/perseus-vault.db}"
WORKSPACE="${WORKSPACE:-$(pwd)}"

echo ""
echo "============================================"
echo "  Perseus Vault One-Shot Bootstrap"
echo "  Persistent memory engine for AI agents"
echo "  github.com/Perseus-Computing-LLC/perseus-vault"
echo "============================================"

# ── Step 1: System dependencies ─────────────────────────────────────────────
header "Step 1: System dependencies"

detect_pkg_manager() {
    if command -v apt-get &>/dev/null; then echo "apt"
    elif command -v yum &>/dev/null; then echo "yum"
    elif command -v dnf &>/dev/null; then echo "dnf"
    elif command -v pacman &>/dev/null; then echo "pacman"
    elif command -v brew &>/dev/null; then echo "brew"
    elif command -v apk &>/dev/null; then echo "apk"
    else echo "unknown"; fi
}

PKG_MGR=$(detect_pkg_manager)

# Install build tools (C compiler, linker — needed by rusqlite with bundled feature)
install_build_tools() {
    case "$PKG_MGR" in
        apt)
            apt-get update -qq && apt-get install -y -qq build-essential pkg-config curl git
            ;;
        yum|dnf)
            $PKG_MGR install -y gcc gcc-c++ make pkg-config curl git
            ;;
        pacman)
            pacman -Sy --noconfirm base-devel pkg-config curl git
            ;;
        apk)
            apk add --no-cache build-base pkgconfig curl git
            ;;
        brew)
            # Xcode CLI tools should already be present on macOS
            if ! xcode-select -p &>/dev/null; then
                info "Installing Xcode Command Line Tools..."
                xcode-select --install 2>/dev/null || true
            fi
            ;;
        *)
            info "Checking for C compiler..."
            ;;
    esac
}

# Check for C compiler
if ! command -v cc &>/dev/null; then
    warn "C compiler not found. Installing build tools..."
    install_build_tools
fi
if command -v cc &>/dev/null; then
    ok "C compiler: $(cc --version 2>&1 | head -1)"
else
    fail "C compiler is required to build Perseus Vault (rusqlite with bundled SQLite). Install build-essential or equivalent."
fi

# Check/install Rust
install_rust() {
    info "Installing Rust via rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
    # shellcheck disable=SC1091
    source "$HOME/.cargo/env"
}

if command -v cargo &>/dev/null; then
    RUST_VER=$(cargo --version 2>&1)
    ok "Cargo: $RUST_VER"
else
    if [ -f "$HOME/.cargo/bin/cargo" ]; then
        info "Found cargo in ~/.cargo/bin — adding to PATH"
        export PATH="$HOME/.cargo/bin:$PATH"
        ok "Cargo: $(cargo --version 2>&1)"
    else
        warn "Rust toolchain not found."
        install_rust
        if ! command -v cargo &>/dev/null; then
            fail "Rust installation failed. Install manually: https://rustup.rs"
        fi
        ok "Cargo: $(cargo --version 2>&1)"
    fi
fi

# ── Step 2: Clone / update repo ─────────────────────────────────────────────
header "Step 2: Clone & build Perseus Vault"

if [ -d "$PERSEUS_VAULT_DIR/.git" ]; then
    info "Updating existing checkout at $PERSEUS_VAULT_DIR..."
    git -C "$PERSEUS_VAULT_DIR" fetch origin 2>/dev/null || true
    LOCAL_HASH=$(git -C "$PERSEUS_VAULT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
    REMOTE_HASH=$(git -C "$PERSEUS_VAULT_DIR" rev-parse origin/main 2>/dev/null || echo "unknown")
    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ] || [ "$FORCE" = "1" ]; then
        info "Pulling latest changes..."
        git -C "$PERSEUS_VAULT_DIR" checkout main 2>/dev/null || git -C "$PERSEUS_VAULT_DIR" checkout master 2>/dev/null || true
        git -C "$PERSEUS_VAULT_DIR" pull origin main 2>/dev/null || git -C "$PERSEUS_VAULT_DIR" pull origin master 2>/dev/null || true
    else
        ok "Repo is up to date"
    fi
else
    info "Cloning Perseus Vault from GitHub..."
    rm -rf "$PERSEUS_VAULT_DIR"
    git clone --depth 1 "$VAULT_REPO" "$PERSEUS_VAULT_DIR"
fi

# Build release binary. The crate/bin is named perseus-vault, so cargo emits
# target/release/perseus-vault (this path was stale — it used to look for a
# `perseus_vault` binary that no longer exists, #424).
info "Building Perseus Vault (release)..."
cd "$PERSEUS_VAULT_DIR"
cargo build --release 2>&1 | tail -5
BINARY="$PERSEUS_VAULT_DIR/target/release/perseus-vault"

if [ ! -f "$BINARY" ]; then
    fail "Build failed (expected $BINARY). Check the output above for errors."
fi
ok "Binary built: $BINARY ($(du -h "$BINARY" | cut -f1))"

# ── Step 3: Install binary ──────────────────────────────────────────────────
header "Step 3: Install binary"

mkdir -p "$PERSEUS_VAULT_BIN_DIR"
cp "$BINARY" "$PERSEUS_VAULT_BIN_DIR/perseus-vault"
chmod +x "$PERSEUS_VAULT_BIN_DIR/perseus-vault"

# macOS Apple silicon: a freshly built (unsigned) binary is SIGKILLed on first
# run — `perseus-vault --version` prints "Killed: 9" with no other output
# (#422). Apply an ad-hoc code signature so it launches. Guarded by Darwin +
# arm64 so it is a no-op on Intel macOS and other platforms.
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && command -v codesign >/dev/null 2>&1; then
    info "Ad-hoc code-signing binary (macOS Apple silicon, #422)..."
    if codesign --force --sign - "$PERSEUS_VAULT_BIN_DIR/perseus-vault" 2>/dev/null; then
        ok "Ad-hoc code-signed"
    else
        warn "Could not code-sign. If 'perseus-vault' is Killed: 9, run:"
        warn "  codesign --force --sign - $PERSEUS_VAULT_BIN_DIR/perseus-vault"
    fi
fi

# Ensure ~/.local/bin is on PATH
case ":$PATH:" in
    *":$PERSEUS_VAULT_BIN_DIR:"*) ;;
    *) export PATH="$PERSEUS_VAULT_BIN_DIR:$PATH" ;;
esac

if command -v perseus-vault &>/dev/null; then
    VAULT_VER=$(perseus-vault --version 2>&1 || echo "unknown")
    ok "perseus-vault installed to $PERSEUS_VAULT_BIN_DIR/perseus-vault"
    ok "Version: $VAULT_VER"
else
    fail "perseus-vault not found on PATH after install. Check $PERSEUS_VAULT_BIN_DIR"
fi

# ── Step 4: Create data directory ───────────────────────────────────────────
header "Step 4: Data directory"

if [ -d "$PERSEUS_VAULT_DATA_DIR" ]; then
    ok "Data directory exists: $PERSEUS_VAULT_DATA_DIR"
else
    info "Creating data directory: $PERSEUS_VAULT_DATA_DIR"
    mkdir -p "$PERSEUS_VAULT_DATA_DIR"
    ok "Data directory created"
fi

# Warm up the database (creates tables + FTS5 index)
if [ ! -f "$PERSEUS_VAULT_DB_PATH" ]; then
    info "Warming up database at $PERSEUS_VAULT_DB_PATH..."
    # Brief serve+kill to trigger DB creation
    timeout 2 perseus-vault serve --db "$PERSEUS_VAULT_DB_PATH" 2>/dev/null || true
    if [ -f "$PERSEUS_VAULT_DB_PATH" ]; then
        ok "Database created: $PERSEUS_VAULT_DB_PATH"
    else
        warn "Database warm-up didn't create the file (will be created on first serve)"
    fi
else
    ok "Database exists: $PERSEUS_VAULT_DB_PATH"
fi

# ── Step 5: .env entries ────────────────────────────────────────────────────
header "Step 5: Environment"

ENV_FILE="$WORKSPACE/.env"
PERSEUS_VAULT_ENV_BLOCK="# ── Perseus Vault ──────────────────────────────────────────────────────
# Database path (default shown)
PERSEUS_VAULT_DB_PATH=$PERSEUS_VAULT_DB_PATH
"

if [ -f "$ENV_FILE" ]; then
    if grep -q "PERSEUS_VAULT_DB_PATH" "$ENV_FILE" 2>/dev/null; then
        ok "PERSEUS_VAULT_DB_PATH already in .env"
    else
        info "Appending PERSEUS_VAULT_DB_PATH to existing .env..."
        echo "$PERSEUS_VAULT_ENV_BLOCK" >> "$ENV_FILE"
        ok "Appended to $ENV_FILE"
    fi
else
    BOOTSTRAP_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u)
    cat > "$ENV_FILE" << ENVEOF
# =============================================================================
#  Perseus Vault Environment
#  Generated by Perseus Vault bootstrap — ${BOOTSTRAP_DATE}
# =============================================================================

# Database path
PERSEUS_VAULT_DB_PATH=$PERSEUS_VAULT_DB_PATH

# ── Optional: LLM Provider Keys (for future versions with LLM extraction) ──
# DEEPSEEK_API_KEY=***
# OPENAI_API_KEY=***
# ANTHROPIC_API_KEY=***
ENVEOF
    ok ".env created at $ENV_FILE"
fi

# ── Step 6: Verify binary ───────────────────────────────────────────────────
header "Step 6: Verify binary"

# Quick smoke test: start server directly, check it initializes
SMOKE_OUT=$(timeout 2 perseus-vault serve --db "$PERSEUS_VAULT_DB_PATH" 2>&1 </dev/null || true)
if echo "$SMOKE_OUT" | grep -q "MCP server ready"; then
    ok "MCP server initializes correctly"
    ok "Tools: perseus_vault_recall, perseus_vault_remember, perseus_vault_health"
else
    warn "MCP smoke test had issues (non-critical). Manual check:"
    warn "  Run: perseus-vault serve --db $PERSEUS_VAULT_DB_PATH"
fi

# ── Step 7: Success summary ─────────────────────────────────────────────────
header "Success Summary"

echo ""
printf "  ${BOLD}%-30s${NC} %s\n" "Perseus Vault version:" "$(perseus-vault --version 2>&1 || echo 'unknown')"
printf "  ${BOLD}%-30s${NC} %s\n" "Binary:" "$PERSEUS_VAULT_BIN_DIR/perseus-vault"
printf "  ${BOLD}%-30s${NC} %s\n" "Database:" "$([ -f "$PERSEUS_VAULT_DB_PATH" ] && echo "✓ $PERSEUS_VAULT_DB_PATH" || echo 'created on first serve')"
printf "  ${BOLD}%-30s${NC} %s\n" "Data dir:" "$PERSEUS_VAULT_DATA_DIR"
printf "  ${BOLD}%-30s${NC} %s\n" "MCP tools:" "perseus_vault_recall, perseus_vault_remember, perseus_vault_health"
printf "  ${BOLD}%-30s${NC} %s\n" "Cargo:" "$(cargo --version 2>&1)"
printf "  ${BOLD}%-30s${NC} %s\n" "OS:" "$(uname -s) $(uname -m)"
printf "  ${BOLD}%-30s${NC} %s\n" ".env:" "$([ -f "$ENV_FILE" ] && echo '✓ exists' || echo '✗ missing')"

echo ""
echo "============================================"
echo "  ${GREEN}Perseus Vault bootstrap complete!${NC}"
echo ""
echo "  Quick commands:"
echo "    perseus-vault serve --db $PERSEUS_VAULT_DB_PATH   # Start MCP server"
echo "    perseus-vault --version                   # Show version"
echo ""
echo "  Docs: https://github.com/Perseus-Computing-LLC/perseus-vault"
echo "============================================"
