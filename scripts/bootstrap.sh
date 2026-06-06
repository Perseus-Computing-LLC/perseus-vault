#!/usr/bin/env bash
# =============================================================================
#  Engram-rs One-Shot Bootstrap
#  Persistent Memory Engine for Perseus — MCP JSON-RPC stdio server
#
#  Usage:
#    curl -sSL https://raw.githubusercontent.com/tcconnally/engram-rs/main/scripts/bootstrap.sh | bash
#
#  What this does:
#    1. Installs system dependencies (Rust toolchain via rustup, build tools)
#    2. Clones and builds engram-rs from source (release binary)
#    3. Installs the binary to ~/.local/bin/engram
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
ENGRAM_REPO="https://github.com/tcconnally/engram-rs.git"
ENGRAM_DIR="${ENGRAM_DIR:-$HOME/.engram-rs}"
ENGRAM_BIN_DIR="${ENGRAM_BIN_DIR:-$HOME/.local/bin}"
ENGRAM_DATA_DIR="${ENGRAM_DATA_DIR:-$HOME/.perseus/engram}"
ENGRAM_DB_PATH="${ENGRAM_DB_PATH:-$ENGRAM_DATA_DIR/engram.db}"
WORKSPACE="${WORKSPACE:-$(pwd)}"

echo ""
echo "============================================"
echo "  Engram-rs One-Shot Bootstrap"
echo "  Persistent Memory Engine for Perseus"
echo "  github.com/tcconnally/engram-rs"
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
    fail "C compiler is required to build engram-rs (rusqlite with bundled SQLite). Install build-essential or equivalent."
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

# ── Step 2: Clone / update engram-rs repo ───────────────────────────────────
header "Step 2: Clone & build engram-rs"

if [ -d "$ENGRAM_DIR/.git" ]; then
    info "Updating existing checkout at $ENGRAM_DIR..."
    git -C "$ENGRAM_DIR" fetch origin 2>/dev/null || true
    LOCAL_HASH=$(git -C "$ENGRAM_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
    REMOTE_HASH=$(git -C "$ENGRAM_DIR" rev-parse origin/main 2>/dev/null || echo "unknown")
    if [ "$LOCAL_HASH" != "$REMOTE_HASH" ] || [ "$FORCE" = "1" ]; then
        info "Pulling latest changes..."
        git -C "$ENGRAM_DIR" checkout main 2>/dev/null || git -C "$ENGRAM_DIR" checkout master 2>/dev/null || true
        git -C "$ENGRAM_DIR" pull origin main 2>/dev/null || git -C "$ENGRAM_DIR" pull origin master 2>/dev/null || true
    else
        ok "Repo is up to date"
    fi
else
    info "Cloning engram-rs from GitHub..."
    rm -rf "$ENGRAM_DIR"
    git clone --depth 1 "$ENGRAM_REPO" "$ENGRAM_DIR"
fi

# Build release binary
info "Building engram-rs (release)..."
cd "$ENGRAM_DIR"
cargo build --release 2>&1 | tail -5
BINARY="$ENGRAM_DIR/target/release/engram"

if [ ! -f "$BINARY" ]; then
    fail "Build failed. Check the output above for errors."
fi
ok "Binary built: $BINARY ($(du -h "$BINARY" | cut -f1))"

# ── Step 3: Install binary ──────────────────────────────────────────────────
header "Step 3: Install binary"

mkdir -p "$ENGRAM_BIN_DIR"
cp "$BINARY" "$ENGRAM_BIN_DIR/engram"
chmod +x "$ENGRAM_BIN_DIR/engram"

# Ensure ~/.local/bin is on PATH
case ":$PATH:" in
    *":$ENGRAM_BIN_DIR:"*) ;;
    *) export PATH="$ENGRAM_BIN_DIR:$PATH" ;;
esac

if command -v engram &>/dev/null; then
    ENGRAM_VER=$(engram --version 2>&1 || echo "unknown")
    ok "engram installed to $ENGRAM_BIN_DIR/engram"
    ok "Version: $ENGRAM_VER"
else
    fail "engram not found on PATH after install. Check $ENGRAM_BIN_DIR"
fi

# ── Step 4: Create data directory ───────────────────────────────────────────
header "Step 4: Data directory"

if [ -d "$ENGRAM_DATA_DIR" ]; then
    ok "Data directory exists: $ENGRAM_DATA_DIR"
else
    info "Creating data directory: $ENGRAM_DATA_DIR"
    mkdir -p "$ENGRAM_DATA_DIR"
    ok "Data directory created"
fi

# Warm up the database (creates tables + FTS5 index)
if [ ! -f "$ENGRAM_DB_PATH" ]; then
    info "Warming up database at $ENGRAM_DB_PATH..."
    # Brief serve+kill to trigger DB creation
    timeout 2 engram serve --db "$ENGRAM_DB_PATH" --mcp 2>/dev/null || true
    if [ -f "$ENGRAM_DB_PATH" ]; then
        ok "Database created: $ENGRAM_DB_PATH"
    else
        warn "Database warm-up didn't create the file (will be created on first serve)"
    fi
else
    ok "Database exists: $ENGRAM_DB_PATH"
fi

# ── Step 5: .env entries ────────────────────────────────────────────────────
header "Step 5: Environment"

ENV_FILE="$WORKSPACE/.env"
ENGRAM_ENV_BLOCK="# ── Engram-rs ──────────────────────────────────────────────────────────
# Database path (default shown)
ENGRAM_DB_PATH=$ENGRAM_DB_PATH
"

if [ -f "$ENV_FILE" ]; then
    if grep -q "ENGRAM_DB_PATH" "$ENV_FILE" 2>/dev/null; then
        ok "ENGRAM_DB_PATH already in .env"
    else
        info "Appending ENGRAM_DB_PATH to existing .env..."
        echo "$ENGRAM_ENV_BLOCK" >> "$ENV_FILE"
        ok "Appended to $ENV_FILE"
    fi
else
    BOOTSTRAP_DATE=$(date -u +"%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || date -u)
    cat > "$ENV_FILE" << ENVEOF
# =============================================================================
#  Engram-rs Environment
#  Generated by engram-rs bootstrap — ${BOOTSTRAP_DATE}
# =============================================================================

# Database path
ENGRAM_DB_PATH=$ENGRAM_DB_PATH

# ── Optional: LLM Provider Keys (for future engram versions with LLM extraction) ──
# DEEPSEEK_API_KEY=***
# OPENAI_API_KEY=***
# ANTHROPIC_API_KEY=***
ENVEOF
    ok ".env created at $ENV_FILE"
fi

# ── Step 6: Verify MCP server ───────────────────────────────────────────────
header "Step 6: Verify MCP server"

info "Testing MCP handshake (tools/list)..."
MCP_TEST=$(python3 -c "
import subprocess, json
proc = subprocess.Popen(
    ['$ENGRAM_BIN_DIR/engram', 'serve', '--db', '$ENGRAM_DB_PATH', '--mcp'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True
)
# initialize
init_req = json.dumps({'jsonrpc':'2.0','id':1,'method':'initialize','params':{
    'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'bootstrap','version':'1.0'}
}})
out, err = proc.communicate(input=init_req + '\n' + json.dumps({'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}) + '\n', timeout=5)
# Parse the second line (tools/list response)
lines = out.strip().split('\n')
for line in lines:
    try:
        resp = json.loads(line)
        if resp.get('id') == 2 and 'result' in resp:
            tools = [t['name'] for t in resp['result'].get('tools',[])]
            print('TOOLS:' + ','.join(tools))
            break
    except: pass
proc.terminate()
" 2>/dev/null || echo "MCP_TEST_FAILED")

if echo "$MCP_TEST" | grep -q "TOOLS:"; then
    TOOLS=$(echo "$MCP_TEST" | sed 's/TOOLS://')
    ok "MCP server responded with tools: $TOOLS"
else
    warn "MCP handshake test had issues (expected if Python not available). Manual check:"
    warn "  Run: engram serve --db $ENGRAM_DB_PATH --mcp"
fi

# ── Step 7: Success summary ─────────────────────────────────────────────────
header "Success Summary"

echo ""
printf "  ${BOLD}%-30s${NC} %s\n" "Engram version:" "$(engram --version 2>&1 || echo 'unknown')"
printf "  ${BOLD}%-30s${NC} %s\n" "Binary:" "$ENGRAM_BIN_DIR/engram"
printf "  ${BOLD}%-30s${NC} %s\n" "Database:" "$([ -f "$ENGRAM_DB_PATH" ] && echo "✓ $ENGRAM_DB_PATH" || echo 'created on first serve')"
printf "  ${BOLD}%-30s${NC} %s\n" "Data dir:" "$ENGRAM_DATA_DIR"
printf "  ${BOLD}%-30s${NC} %s\n" "MCP tools:" "engram_recall, engram_store, engram_health"
printf "  ${BOLD}%-30s${NC} %s\n" "Cargo:" "$(cargo --version 2>&1)"
printf "  ${BOLD}%-30s${NC} %s\n" "OS:" "$(uname -s) $(uname -m)"
printf "  ${BOLD}%-30s${NC} %s\n" ".env:" "$([ -f "$ENV_FILE" ] && echo '✓ exists' || echo '✗ missing')"

echo ""
echo "============================================"
echo "  ${GREEN}Engram-rs bootstrap complete!${NC}"
echo ""
echo "  Quick commands:"
echo "    engram serve --db $ENGRAM_DB_PATH --mcp   # Start MCP server"
echo "    engram --version                          # Show version"
echo ""
echo "  To use with Perseus, add to .perseus/config.yaml:"
echo "    engram:"
echo "      enabled: true"
echo "      transport: \"stdio\""
echo "      command: [\"engram\", \"serve\", \"--db\", \"$ENGRAM_DB_PATH\", \"--mcp\"]"
echo ""
echo "  Docs: https://github.com/tcconnally/engram-rs"
echo "============================================"
