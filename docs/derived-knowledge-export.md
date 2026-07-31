# Derived knowledge surface export

`perseus_vault_derived_export` creates a human-readable Markdown projection of durable knowledge while keeping SQLite as the only source of truth. The legacy `mimir_derived_export` alias remains callable during the v2 compatibility window.

```json
{"output_path":"/tmp/derived-knowledge.md","workspace_hash":"optional-workspace"}
```

The exporter includes `belief`, `convention`, `correction`, `insight`, and `keystone` records. It sorts records by category, key, and immutable entity ID, so the same source state produces byte-stable output. Each entry includes a provenance comment with its category, key, entity ID, and workspace scope.

The projection is derived: it is safe to regenerate or discard. Do not edit it as if it were an import source; use normal Vault mutation tools to change durable knowledge, then re-export.

The result reports the output path, count exported, and `deterministic: true`.