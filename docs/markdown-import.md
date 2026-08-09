# Controlled Markdown import

`perseus_vault_markdown_import` imports a single Markdown file as **non-authoritative evidence**, not native Vault truth. The legacy `perseus_vault_markdown_import` alias remains callable during the v2 compatibility window.

```json
{"path":"/path/to/wiki-note.md","workspace_hash":"workspace","source_system":"wiki"}
```

Imported records are written as:

- category: `imported_markdown`
- status: `draft`
- type: `reference`
- low certainty and importance (`0.2`)
- provenance: `origin.memory_kind = imported`, source system, capture method `import`
- `non_authoritative: true`

The key is deterministic from the source path and content. Reimporting unchanged content into the same workspace is idempotent and returns `deduped: true`; altered content produces a distinct imported record rather than silently overwriting evidence.

Imported references may surface only according to normal ranking and serving policy. They are deliberately draft/low-confidence so verified native observations, corrections, and conventions receive higher trust signals. Promote or supersede imported material explicitly only after validation; import itself never asserts world truth.