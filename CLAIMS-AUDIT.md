# Claims Audit — Perseus Vault (formerly Mimir/Mneme)

**Date:** 2026-07-16 (refreshed) · **Audited:** README.md vs code and committed benchmark artifacts on `main`

## Audit note 2026-07-16 (#702)

- Retired the "sub-millisecond recall" entry; measured recall latency now
  points at committed artifacts (see below).
- Removed the unbacked 100K-entity insert-rate figure from the README (no
  artifact anywhere in the repo backed it); the stress-test table now quotes
  `benchmark/scale/report.json`.
- Reworded "signed results/reports" to "content-hashed (sha256)" on
  README/PERF/benchmark surfaces: `signature_sha256` is a self-computed content
  hash for reproducibility, not a cryptographic signature. The journal audit
  chain (SHA-256 + keyed MAC) is cryptographic and its docs are unchanged.
- Clarified that `federate` is a local export / workspace-rename / re-import
  (file based, no network peers); the Windows-safe default path is tracked
  in #704.
- Tool-count note refreshed: the registry has grown since the v2.13-era
  recount; the published figure stays "55+" by convention.

## Findings

### LOW — no material gaps found

Claims verified against `src/`:

- **55+ MCP tools**: published as "55+" per naming convention. ✓ The registry
  has grown since the v2.13-era recount of 57 distinct base tool names in
  `src/mcp.rs` (each exposed under 3 aliases
  `perseus_vault_*`/`mimir_*`/`mneme_*`); the published figure remains "55+"
  by convention, and the recount command below remains the source of truth.

  Verify the count against source (this is the authoritative command — re-run
  it and update README/manifest.json/glama.json whenever a tool is added):

  ```bash
  grep -o '"name": "mimir_[a-z_]*"' src/mcp.rs | sort -u | wc -l
  ```

- **MCP-native** — full JSON-RPC stdio server (`initialize`, `tools/list`, `tools/call`). ✓
- **SQLite + FTS5** — schema builds FTS5 tables; recall uses FTS5 queries. ✓
- **AES-256-GCM encrypted** — encryption at rest for entity bodies. ✓
- **Fully local / zero-dependency** — no network runtime deps in `Cargo.toml`. ✓
- **Sub-millisecond recall**: RETIRED 2026-07-16. No committed artifact
  supports it, and the old justification (bundled offline embeddings) said
  nothing about latency. Measured: FTS5 recall p50 3.14 ms at 10K entities
  (`benchmark/scale/report.json`); dense recall p50 194.5 ms at 1M entities
  (`benchmark/lambda/results/scale1m_default_500.json`, uniform arm). The
  README makes no sub-millisecond claim.

## History

- 2026-06-12 (v0.5.0): 23 tools. 2026-06 interim: 30 tools (#130). 2026-06-28
  (v2.6.0): 46 (#271 mimir_semantic_search, #269 mimir_recall_layer, review
  follow-up mimir_history). v2.13.0: 49 (#327 mimir_consolidate, #332
  mimir_follow, #345 mimir_memories). Post-v2.13.0: 53 (#365
  mimir_communities, mimir_community_summary, mimir_global_recall; #364
  mimir_dream). 55 (#363 mimir_valid_at, mimir_bitemporal). 56 (#521
  mimir_check_failure_pattern). Now **57** (#520 mimir_capture).
  Earlier figures kept as historical record only.
