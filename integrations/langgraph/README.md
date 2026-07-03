# Perseus Vault LangGraph Integration

Drop-in persistent long-term memory for LangGraph agents via Perseus Vault.

## Install

Install from source (not yet published to PyPI):

```bash
pip install langgraph
pip install -e integrations/langgraph/
```

## Quick Start

```python
from mimir_langgraph import MimirStore

# Create a Perseus Vault-backed store
store = MimirStore(
    binary="perseus-vault",  # or /usr/local/bin/perseus-vault
    db_path="~/.mimir/data/perseus-vault.db",
)

# Use as a drop-in BaseStore replacement
store.put(("users", "123"), "preferences", {"theme": "dark", "language": "en"})

item = store.get(("users", "123"), "preferences")
print(item.value)  # {"theme": "dark", "language": "en"}

# Search across namespaces
results = store.search(("users",), query="preferences theme")
for r in results:
    print(r.key, r.value, r.score)
```

## Integration with LangGraph Agents

```python
from langgraph.graph import StateGraph
from langgraph.store.base import BaseStore
from mimir_langgraph import MimirStore

# Use MimirStore as your long-term memory
store = MimirStore()

# Build your graph with store
graph = (
    StateGraph(AgentState)
    .add_node("agent", agent_node)
    .compile(store=store)
)
```

The store persists across sessions. Agents can retrieve context
from previous interactions using `store.search()`.

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `binary` | `"perseus-vault"` | Path to the perseus-vault binary |
| `db_path` | `"~/.mimir/data/perseus-vault.db"` | Path to the SQLite database |
| `timeout` | `30.0` | Tool call timeout in seconds |
| `encryption_key` | `None` | Path to AES-256-GCM key file |
| `ollama_url` | `None` | Ollama endpoint for hybrid search |
| `embedding_model` | `None` | Embedding model name (requires ollama_url) |

## How It Works

LangGraph's BaseStore interface maps cleanly onto Perseus Vault's entity model:

| LangGraph | Perseus Vault |
|---|---|
| `namespace: tuple[str, ...]` | `category: str` (joined with `/`) |
| `key: str` | `key: str` |
| `value: dict` | `body_json: str` (JSON) |
| `search()` | `mimir_recall` (FTS5) |
| `put()` | `mimir_remember` |
| `delete()` | `mimir_forget` |

## Requirements

- Perseus Vault v1.0.0+ installed (`curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash`)
- LangGraph >= 0.2.0
- Python 3.10+
