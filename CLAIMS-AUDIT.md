# Claims Audit — Perseus Vault (formerly Mimir/Mneme)

**Date:** 2026-07-01 (refreshed) · **Audited:** README.md vs code on `main` (v2.13.0)

## Findings

### LOW — no material gaps found

Claims verified against `src/`:

- **55+ MCP tools** — published as "55+" per naming convention (source count: 57 distinct base tool names in `src/mcp.rs`; each exposed under 3 aliases `perseus_vault_*`/`mimir_*`/`mneme_*`). ✓
  (Recount 2026-07-09: 56 after `mimir_check_failure_pattern`, #521; 57
  after `mimir_capture`, #520.)

  Verify the count against source (this is the authoritative command — re-run
  it and update README/manifest.json/glama.json whenever a tool is added):

  ```bash
  grep -o '"name": "mimir_[a-z_]*"' src/mcp.rs | sort -u | wc -l
  ```

- **MCP-native** — full JSON-RPC stdio server (`initialize`, `tools/list`, `tools/call`). ✓
- **SQLite + FTS5** — schema builds FTS5 tables; recall uses FTS5 queries. ✓
- **AES-256-GCM encrypted** — encryption at rest for entity bodies. ✓
- **Fully local / zero-dependency** — no network runtime deps in `Cargo.toml`. ✓
- **Sub-millisecond recall** — bundled offline embeddings, no external model download. ✓

## History

- 2026-06-12 (v0.5.0): 23 tools. 2026-06 interim: 30 tools (#130). 2026-06-28
  (v2.6.0): 46 (#271 mimir_semantic_search, #269 mimir_recall_layer, review
  follow-up mimir_history). v2.13.0: 49 (#327 mimir_consolidate, #332
  mimir_follow, #345 mimir_memories). Post-v2.13.0: 53 (#365
  mimir_communities, mimir_community_summary, mimir_global_recall; #364
  mimir_dream). 55 (#363 mimir_valid_at, mimir_bitemporal). 56 (#521
  mimir_check_failure_pattern). Now **57** (#520 mimir_capture).
  Earlier figures kept as historical record only.
