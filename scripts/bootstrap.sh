#!/usr/bin/env bash
# =============================================================================
#  Mimir One-Shot Bootstrap
#  Persistent memory engine for AI agents — MCP JSON-RPC stdio server
#
#  Usage:
#    curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/mimir/main/scripts/bootstrap.sh | bash
#
#  What this does:
#    1. Installs system dependencies (Rust toolchain via rustup, build tools)
#    2. Clones and builds Mimir from source (release binary)
#    3. Installs the binary to ~/.local/bin/mimir
#    4. Creates the data directory and generates .env defaults
#    5. Verifies the installation and prints a success summary
#
#  Idempotent — safe to re-run. Existing binary is only rebuilt if
#  FORCE=1 or the repo checkout is stale.
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
MIMIR_REPO="https://github.com/Perseus-Computing-LLC/mimir.git"
MIMIR_DIR="${MIMIR_DIR:-$HOME/.mimir}"
MIMIR_BIN_DIR="${MIMIR_BIN_DIR:-$HOME/.local/bin}"
MIMIR_DATA_DIR="${MIMIR_DATA_DIR:-$HOME/.mimir/data}"
MIMIR_DB_PATH="${MIMIR_DB_PATH:-$MIMIR_DATA_DIR/mimir.db}"
WORKSPACE="${WORKSPACE:-$(pwd)}"

echo ""
echo "============================================"
echo "  Mimir One-Shot Bootstrap"
echo "  Persistent memory engine for AI agents"
echo "  github.com/Perseus-Computing-LLC/mimir"
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
    fail "C compiler is required to build Mimir (rusqlite with bundled SQLite). Install build-essential or equivalent."
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

# ── Step 2: Clone / update Mimir repo ───────────────────────────────────────
header "Step 2: Clone & build Mimir"

if [ -d "$MIMIR_DIR/.git" ]; then
    info "Updating existing checkout at $MIMIR_DIR..."
    git -C "$MIMIR_DIR" fetch origin 2>/dev/null || true
    LOCAL_HASH=$(git -C "$MIMIR_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
    REMOTE_HASH=$(git -C "$MIMIR_DIR" rev-parse origin/main 2>/dev/null || echo "unknown")
    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ] || [ "$FORCE" = "1" ]; then
        info "Pulling latest changes..."
        git -C "$MIMIR_DIR" checkout main 2>/dev/null || git -C "$MIMIR_DIR" checkout master 2>/dev/null || true
        git -C "$MIMIR_DIR" pull origin main 2>/dev/null || git -C "$MIMIR_DIR" pull origin master 2>/dev/null || true
    else
        ok "Repo is up to date"
    fi
else
    info "Cloning Mimir from GitHub..."
    rm -rf "$MIMIR_DIR"
    git clone --depth 1 "$MIMIR_REPO" "$MIMIR_DIR"
fi

# Build release binary
info "Building Mimir (release)..."
cd "$MIMIR_DIR"
cargo build --release 2>&1 | tail -5
BINARY="$MIMIR_DIR/target/release/mimir"

if [ ! -f "$BINARY" ]; then
    fail "Build failed. Check the output above for errors."
fi
ok "Binary built: $BINARY ($(du -h "$BINARY" | cut -f1))"

# ── Step 3: Install binary ──────────────────────────────────────────────────
header "Step 3: Install binary"

mkdir -p "$MIMIR_BIN_DIR"
cp "$BINARY" "$MIMIR_BIN_DIR/mimir"
chmod +x "$MIMIR_BIN_DIR/mimir"

# macOS Apple silicon: a freshly built (unsigned) binary is SIGKILLed on first
# run — `mimir --version` prints "Killed: 9" with no other output (#422). Apply
# an ad-hoc code signature so it launches. Guarded by Darwin + arm64 so it is a
# no-op on Intel macOS and other platforms.
if [ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] && command -v codesign >/dev/null 2>&1; then
    info "Ad-hoc code-signing binary (macOS Apple silicon, #422)..."
    if codesign --force --sign - "$MIMIR_BIN_DIR/mimir" 2>/dev/null; then
        ok "Ad-hoc code-signed"
    else
        warn "Could not code-sign. If 'mimir' is Killed: 9, run:"
        warn "  codesign --force --sign - $MIMIR_BIN_DIR/mimir"
    fi
fi

# Ensure ~/.local/bin is on PATH
case ":$PATH:" in
    *":$MIMIR_BIN_DIR:"*) ;;
    *) export PATH="$MIMIR_BIN_DIR:$PATH" ;;
esac

if command -v mimir &>/dev/null; then
    MIMIR_VER=$(mimir --version 2>&1 || echo "unknown")
    ok "mimir installed to $MIMIR_BIN_DIR/mimir"
    ok "Version: $MIMIR_VER"
else
    fail "mimir not found on PATH after install. Check $MIMIR_BIN_DIR"
fi

# ── Step 4: Create data directory ───────────────────────────────────────────
header "Step 4: Data directory"

if [ -d "$MIMIR_DATA_DIR" ]; then
    ok "Data directory exists: $MIMIR_DATA_DIR"
else
    info "Creating data directory: $MIMIR_DATA_DIR"
    mkdir -p "$MIMIR_DATA_DIR"
    ok "Data directory created"
fi

# Warm up the database (creates tables + FTS5 index)
if [ ! -f "$MIMIR_DB_PATH" ]; then
    info "Warming up database at $MIMIR_DB_PATH..."
    # Brief serve+kill to trigger DB creation
    timeout 2 mimir --db "$MIMIR_DB_PATH" 2>/dev/null || true
    if [ -f "$MIMIR_DB_PATH" ]; then
        ok "Database created: $MIMIR_DB_PATH"
    else
        warn "Database warm-up didn't create the file (will be created on first serve)"
    fi
else
    ok "Database exists: $MIMIR_DB_PATH"
fi

# ── Step 5: .env entries ────────────────────────────────────────────────────
header "Step 5: Environment"

ENV_FILE="$WORKSPACE/.env"
MIMIR_ENV_BLOCK="# ── Mimir ──────────────────────────────────────────────────────────────
# Database path (default shown)
MIMIR_DB_PATH=$MIMIR_DB_PATH
"

if [ -f "$ENV_FILE" ]; then
    if grep -q "MIMIR_DB_PATH" "$ENV_FILE" 2>/dev/null; then
        ok "MIMIR_DB_PATH already in .env"
    else
        info "Appending MIMIR_DB_PATH to existing .env..."
        echo "$MIMIR_ENV_BLOCK" >> "$ENV_FILE"
        ok "Appended to $ENV_FILE"
    fi
else
    BOOTSTRAP_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u)
    cat > "$ENV_FILE" << ENVEOF
# =============================================================================
#  Mimir Environment
#  Generated by Mimir bootstrap — ${BOOTSTRAP_DATE}
# =============================================================================

# Database path
MIMIR_DB_PATH=$MIMIR_DB_PATH

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
SMOKE_OUT=$(timeout 2 mimir --db "$MIMIR_DB_PATH" 2>&1 </dev/null || true)
if echo "$SMOKE_OUT" | grep -q "MCP server ready"; then
    ok "MCP server initializes correctly"
    ok "Tools: mimir_recall, mimir_store, mimir_health"
else
    warn "MCP smoke test had issues (non-critical). Manual check:"
    warn "  Run: mimir --db $MIMIR_DB_PATH"
fi

# ── Step 7: Success summary ─────────────────────────────────────────────────
header "Success Summary"

echo ""
printf "  ${BOLD}%-30s${NC} %s\n" "Mimir version:" "$(mimir --version 2>&1 || echo 'unknown')"
printf "  ${BOLD}%-30s${NC} %s\n" "Binary:" "$MIMIR_BIN_DIR/mimir"
printf "  ${BOLD}%-30s${NC} %s\n" "Database:" "$([ -f "$MIMIR_DB_PATH" ] && echo "✓ $MIMIR_DB_PATH" || echo 'created on first serve')"
printf "  ${BOLD}%-30s${NC} %s\n" "Data dir:" "$MIMIR_DATA_DIR"
printf "  ${BOLD}%-30s${NC} %s\n" "MCP tools:" "mimir_recall, mimir_store, mimir_health"
printf "  ${BOLD}%-30s${NC} %s\n" "Cargo:" "$(cargo --version 2>&1)"
printf "  ${BOLD}%-30s${NC} %s\n" "OS:" "$(uname -s) $(uname -m)"
printf "  ${BOLD}%-30s${NC} %s\n" ".env:" "$([ -f "$ENV_FILE" ] && echo '✓ exists' || echo '✗ missing')"

echo ""
echo "============================================"
echo "  ${GREEN}Mimir bootstrap complete!${NC}"
echo ""
echo "  Quick commands:"
echo "    mimir --db $MIMIR_DB_PATH   # Start MCP server"
echo "    mimir --version             # Show version"
echo ""
echo "  Standalone MCP server:"
echo "    mimir --db $MIMIR_DB_PATH"
echo ""
echo "  Docs: https://github.com/Perseus-Computing-LLC/mimir"
echo "============================================"
