# Integrating Perseus Vault with Cursor

Cursor is the AI-first code editor built on VS Code. It supports MCP servers
natively, allowing Perseus Vault to provide persistent memory across coding sessions
and projects.

## Quick Start

### 1. Install Perseus Vault

```bash
# One-shot bootstrap (recommended)
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash

# Or build from source via cargo
cargo install --git https://github.com/Perseus-Computing-LLC/perseus-vault
```

Verify:
```bash
perseus-vault --version
# Expected: perseus-vault 2.14.0
```

### 2. Create a data directory

```bash
mkdir -p ~/.mimir/data
```

### 3. Configure Cursor

**Option A: Via Settings UI (recommended)**

1. Open Cursor
2. Go to **Settings** (`Cmd+,` on macOS, `Ctrl+,` on Linux/Windows)
3. Navigate to **Features** → **MCP**
4. Click **"+ Add New MCP Server"**
5. Fill in:
   - **Type:** `command`
   - **Name:** `Perseus Vault`
   - **Command:**
     ```
     perseus-vault --db /home/YOUR_USER/.mimir/data/perseus-vault.db
     ```
     (Use absolute paths — `~` may not expand correctly)
6. Click **Save**

**Option B: Via config file**

Create or edit `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["--db", "/home/YOUR_USER/.mimir/data/perseus-vault.db"]
    }
  }
}
```

### 4. Verify

1. Open Cursor Settings → Features → MCP
2. Look for the Mimir entry — it should show a green **"Connected"** indicator
3. Open a Chat or Composer session and ask:

> Use Perseus Vault to check if you have any stored context for this project.

## Usage Patterns

### In Chat mode

> Remember that I prefer React Server Components over client-side fetching
> for this project.

Cursor will call `mimir_remember` via MCP.

> What did I say about data fetching patterns?

Cursor will call `mimir_recall`.

### In Composer / Agent mode

> @Perseus Vault Search for any stored decisions about the authentication module.
> Then implement the login page based on those decisions.

Cursor's agent can chain: recall → code generation, all in one prompt.

### Cross-session continuity

Cursor remembers context within a session. Perseus Vault adds cross-session memory:

> Before I start coding today, recall what we were working on last time.

Perseus Vault returns the context block from `mimir_context`, which includes recent
entities, decisions, and architecture notes.

### Project-specific memory

Create a `.mimir/` directory in your project and configure Cursor to use it:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": ["--db", "/home/YOU/projects/my-app/.mimir/perseus-vault.db"]
    }
  }
}
```

This keeps project memories isolated. Add `.mimir/perseus-vault.db` to `.gitignore`.

## Troubleshooting

### Perseus Vault shows "Disconnected" or fails to connect

1. **Absolute paths:** Check that `--db` uses a full path, not `~`.
2. **Binary location:** Run `which perseus-vault`. If not found, use the full path
   in the command: `/usr/local/bin/perseus-vault --db ...`
3. **Restart Cursor:** After config changes, use `Cmd+Shift+P` →
   "Developer: Reload Window" or quit and reopen Cursor.

### MCP status indicator stays gray/yellow

1. Run Perseus Vault manually to check for startup errors:
   ```bash
   perseus-vault --db ~/.mimir/data/perseus-vault.db
   ```
   It should hang waiting for stdin. If it exits, there's a startup error.

2. Check Cursor's developer console for MCP-related errors:
   `Cmd+Shift+P` → "Developer: Toggle Developer Tools" → Console tab

### Database locked

If you see "database is locked" errors:

1. Check for orphaned Perseus Vault processes:
   ```bash
   ps aux | grep '[p]erseus-vault'
   ```
2. Kill orphans: `kill <PID>`
3. Restart Cursor

### Perseus Vault tools not appearing in agent

Cursor's agent discovers tools on session start. After connecting Perseus Vault:

1. Start a new Chat or Composer session
2. Ask: "List all available tools"
3. Verify `mimir_remember`, `mimir_recall`, etc. appear

If they don't appear, reload the window (`Cmd+Shift+P` → "Developer: Reload Window").

## Advanced

### Encryption at rest

```bash
perseus-vault keygen --key-file ~/.mimir/secret.key
```

Then configure Cursor to use the encrypted database:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": [
        "--db", "/home/YOU/.mimir/data/perseus-vault.db",
        "--encryption-key", "/home/YOU/.mimir/secret.key"
      ]
    }
  }
}
```

### Web dashboard for browsing

Perseus Vault includes a web dashboard. Run it alongside Cursor:

```bash
perseus-vault --db ~/.mimir/data/perseus-vault.db --web --port 8767
```

Open `http://localhost:8767` to browse entities, search, view journal events,
and explore the entity link graph.

### Hybrid search (semantic + keyword)

If you have Ollama running, Perseus Vault can generate embeddings for hybrid search:

```bash
# Ensure Ollama is running with an embedding-capable model
ollama pull nomic-embed-text
```

Then in your Perseus Vault config, configure the LLM endpoint and model:

```json
{
  "mcpServers": {
    "perseus-vault": {
      "command": "perseus-vault",
      "args": [
        "--db", "/home/YOU/.mimir/data/perseus-vault.db",
        "--llm-endpoint", "http://localhost:11434/api/generate",
        "--llm-model", "nomic-embed-text"
      ]
    }
  }
}
```

> **Note:** `--llm-model` sets the model for BOTH embeddings and `mimir_ask`
> (RAG). If you use `mimir_ask`, choose a model that supports both chat and
> embeddings, or run a separate Perseus Vault instance for each.

With embeddings enabled, `mimir_recall` with `mode: "hybrid"` combines
keyword matching with semantic similarity for better recall.
