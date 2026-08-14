# Claims Audit — Perseus Vault

**Date:** 2026-08-01 (refreshed) · **Audited:** README.md vs code and committed benchmark artifacts on `main`

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
- Tool-count note refreshed: the current registry contains 95 unique canonical
  tool names. Compatibility aliases are callable but excluded from the count.

## Findings

### LOW — no material gaps found (encryption caveat tracked below)

Claims verified against `src/`:

- **141 canonical MCP tools**: ✓ The current registry contains 142 distinct
  tool names in `src/mcp.rs`, each exposed under the canonical
  `perseus_vault_*` prefix. The legacy `perseus_vault_*` and `perseus_vault_*` aliases remain
  callable but are not counted separately.

  Verify the count against source and current-facing metadata (this parser is
  formatting-insensitive and runs in CI):

  ```bash
  python3 scripts/registry_metadata_check.py
  ```

- **MCP-native** — full JSON-RPC stdio server (`initialize`, `tools/list`, `tools/call`). ✓
- **SQLite + FTS5** — schema builds FTS5 tables; recall uses FTS5 queries. ✓
- **AES-256-GCM encrypted** — encryption at rest for entity bodies. ✓
- **Encryption is enabled by default for fresh default installs** — the first
  default startup creates the standard owner-only key and encrypted canary.
  Existing plaintext databases remain readable for explicit migration with
  `init --rekey`; `doctor` reports the actual on-disk state. See
  `docs/ENCRYPTION.md`.
- **Fully local / zero-dependency** — no network runtime deps in `Cargo.toml`. ✓
- **Sub-millisecond recall**: RETIRED 2026-07-16. No committed artifact
  supports it, and the old justification (bundled offline embeddings) said
  nothing about latency. Measured: FTS5 recall p50 3.14 ms at 10K entities
  (`benchmark/scale/report.json`); dense recall p50 194.5 ms at 1M entities
  (`benchmark/lambda/results/scale1m_default_500.json`, uniform arm). The
  README makes no sub-millisecond claim.

## History

- 2026-06-12 (v0.5.0): 23 tools. 2026-06 interim: 30 tools (#130). 2026-06-28
  (v2.6.0): 46 (#271 perseus_vault_semantic_search, #269 perseus_vault_recall_layer, review
  follow-up perseus_vault_history). v2.13.0: 49 (#327 perseus_vault_consolidate, #332
  perseus_vault_follow, #345 perseus_vault_memories). Post-v2.13.0: 53 (#365
  perseus_vault_communities, perseus_vault_community_summary, perseus_vault_global_recall; #364
  perseus_vault_dream). 55 (#363 perseus_vault_valid_at, perseus_vault_bitemporal). 56 (#521
  perseus_vault_check_failure_pattern). Now **95 canonical MCP tools** (registry-derived; #520 perseus_vault_capture and subsequent tools).

  Earlier figures kept as historical record only.
