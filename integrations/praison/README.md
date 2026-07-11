# Perseus Vault — PraisonAI adapter

A drop-in [`MemoryProtocol`](https://github.com/MervinPraison/PraisonAI) backend
that gives [PraisonAI](https://docs.praison.ai) agents persistent, **local-first,
offline** memory backed by [Perseus Vault](https://github.com/Perseus-Computing-LLC/perseus-vault).

Perseus Vault is a single static binary (SQLite + FTS5 + bundled ONNX
embeddings, optional AES-256-GCM) — no external memory service, no network, no
GPU. This adapter maps PraisonAI's short-term / long-term memory API onto the
vault's MCP tools via the shared, hardened `perseus_vault_client` stdio
transport (the same client the LangGraph / CrewAI / AutoGen adapters use).

## Install

```bash
pip install ./integrations/client   # shared stdio client (local path package)
pip install ./integrations/praison  # this adapter
```

Then put the `perseus-vault` binary on `PATH` (single static binary, no deps):

```bash
curl -sSL https://raw.githubusercontent.com/Perseus-Computing-LLC/perseus-vault/main/scripts/bootstrap.sh | bash
```

## Use it

### Via the PraisonAI registry

Importing the package self-registers the adapter under the name
`perseus_vault`:

```python
import perseus_vault_praison  # registers "perseus_vault"
from praisonaiagents.memory.adapters import get_memory_adapter

memory = get_memory_adapter("perseus_vault", db_path="./agent.db")
```

### Directly as a MemoryProtocol implementation

```python
from perseus_vault_praison import PerseusVaultAdapter

memory = PerseusVaultAdapter(db_path="./agent.db")
memory.store_long_term("User prefers metric units.", {"source": "chat"})
hits = memory.search_long_term("units preference")
print(hits[0]["text"])
```

## Configuration

| Argument | Env fallback | Default |
|---|---|---|
| `binary` | `PERSEUS_VAULT_BIN` | `perseus-vault` |
| `db_path` | `PERSEUS_VAULT_DB` | `./perseus-vault.db` |
| `encryption_key` | `PERSEUS_VAULT_ENCRYPTION_KEY` | _(none)_ |
| `timeout` | — | `30.0` |

## Protocol coverage

Implements the full `MemoryProtocol` plus the resettable and deletable
extensions:

| Method | Perseus Vault mapping |
|---|---|
| `store_short_term` / `store_long_term` | `perseus_vault_remember` into a dedicated category |
| `search_short_term` / `search_long_term` | hybrid `perseus_vault_recall` (keyword + vector) |
| `get_all_memories` | **paginated** enumeration across both categories (no 100-cap truncation) |
| `get_context` | hybrid recall assembled into a context string |
| `reset_short_term` / `reset_long_term` | `perseus_vault_prune purge_all` (category-scoped) |
| `delete_memory` / `delete_memories` | `perseus_vault_forget` by key |
| `get_entity` | `perseus_vault_get_entity` |
| `save_session` | `perseus_vault_remember` into a `session` category |

Short-term and long-term memories live in two dedicated categories
(`praison_short_term`, `praison_long_term`) so they are searched, enumerated,
and reset independently.

## Example

See [`examples/python/memory/praison_perseus_vault.py`](../../examples/python/memory/praison_perseus_vault.py)
for a runnable store/recall-across-runs example.

## Tests

```bash
pip install ./integrations/client pytest
cd integrations/praison && python -m pytest tests/ -v
```

The tests drive the adapter against a fake in-memory client, so they run with
no `perseus-vault` binary and no PraisonAI install (the Protocol-conformance
test self-skips when PraisonAI is absent).
