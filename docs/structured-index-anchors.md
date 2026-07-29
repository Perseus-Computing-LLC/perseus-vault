# Structured-index anchors

`perseus_vault_structured_index_anchor` records a stable pointer to a fact in an upstream structured source such as an IDE index or domain fact map. The legacy `mimir_structured_index_anchor` alias remains callable during the v2 compatibility window.

## Reference vs. import

| Mode | Writes a Vault entity? | Use when |
|---|---:|---|
| `reference` (default) | No | The index remains available and the caller needs a stable, refetchable pointer. |
| `import` | Yes, as `structured_index_fact` | The fact needs durable local recall, correction, supersession, or cross-workspace sharing. |

```json
{
  "index_type": "ide_symbol",
  "index_uri": "ide://repo/src/lib.rs",
  "record_id": "symbol:parse",
  "revision": "git:abc123",
  "mode": "reference"
}
```

Import mode additionally requires `content`. It creates a low-confidence (`0.25`), draft, non-authoritative record with `origin.memory_kind: imported` and an `external_refs` entry of type `structured_index`.

## Verification and refetch

Every anchor carries `index_uri`, `record_id`, optional `revision`, and optional `observed_at_unix_ms`. Refetch by retrieving the same locator/record pair and compare revision or observation time before trusting it as current.

An anchor is evidence metadata, not permission to reach the upstream index. Workspace isolation continues to apply to imported records.

## Canonical anchor value

The stable `external_refs` value is:

```text
<index_type>:<index_uri>#<record_id>
```

Example: `ide_symbol:ide://repo/src/lib.rs#symbol:parse`.

This contract follows the structured-truth policy: prefer a reference while the source is available; import only when Vault needs to carry an explicitly provisional local copy.
