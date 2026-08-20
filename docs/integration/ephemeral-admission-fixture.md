# Ephemeral admitted integration fixture

`perseus_vault_client.EphemeralAdmissionFixture` is the supported path for an
integration test that needs a real, serveable memory through Vault 2.23's
admission boundary.

It is deliberately narrower than `VaultClient`:

- creates and owns a fresh `TemporaryDirectory` and `vault.db`;
- accepts no caller-provided database path;
- generates a new source-event HMAC key for each fixture process;
- starts the real `perseus-vault serve` MCP stdio server;
- registers one synthetic fixture agent and an enforce-mode authority manifest
  with only `memory.admission.source`, `memory.commit`, and `memory.read`;
- creates the admission source event and admitted write through the public MCP
  tools; and
- terminates the child and removes the temporary database on exit.

The generated HMAC key is an in-process test credential. It is never printed,
stored in SQLite, or reused between fixture runs. The fixture is not a way to
open or authorize a normal Vault database.

## Minimal example

Build a lean local binary and install the dependency-free client:

```bash
cargo build --locked --no-default-features
python -m pip install ./integrations/client
```

Then drive the public write and recall path:

```python
from perseus_vault_client import EphemeralAdmissionFixture

with EphemeralAdmissionFixture(binary="target/debug/perseus-vault") as vault:
    write = vault.remember(
        "integration-fixture",
        "deterministic",
        {"content": "ephemeral fixture record"},
    )
    assert write["serveable"] is True

    hits = vault.recall(
        "ephemeral fixture",
        category="integration-fixture",
    )
    assert any(hit["id"] == "deterministic" for hit in hits)
```

A normal client remains governed and does not become an implicit fixture:

```python
with VaultClient(binary="target/debug/perseus-vault", db_path="normal.db") as vault:
    result = vault.remember("facts", "ordinary", {"content": "review me"})
    assert result["proposed"] is True
    assert result["serveable"] is not True
```

The fixture is appropriate for offline CI and local deterministic replay. It
leaves no external state and does not require a personal credential or a
long-lived admission secret.
