# Config self-report (#1010)

Status: normative. Surface: `perseus_vault_config_report` (MCP tool, read-only)
+ startup log block.

Borrowed from two independent practitioner systems (Hy-Memory scan,
insight/hy-memory-competitive-scan; MindCache scan,
insight/public-repo-comparison-mindcache-perseus). Both lost operator time to
the same failure: a stage silently resolved away from the requested
configuration (mode-vs-config drift; a hardcoded default provider leaving
non-default users without summaries/decision analysis, logged as "LLM
returned empty" and continuing ingest).

## Contract

Every pipeline stage that consumes a model or storage backend reports, in
machine-readable form, the configuration it ACTUALLY resolved — and, for the
first time, the REQUESTED half of the diff:

| stage             | requested (operator knob, sanitized) | resolved (runtime truth)           | drift condition |
|-------------------|--------------------------------------|------------------------------------|-----------------|
| embedding_backend | provider endpoint host / bundled / none | kind + available + degraded + semantic_recall | backend configured but unusable (never reclassified as empty success) |
| model_backend     | endpoint host + model, or none       | kind + available                   | endpoint configured but unavailable |
| quantization      | PERSEUS_VAULT_EMBEDDING_QUANT or unset→store record | live embedding_format record (point-in-time read, not the open cache) | flag unset but store record quantized (or vice versa) |
| db_path           | PERSEUS_VAULT_DB_PATH or default     | actual opened path                 | env path set but not the one in use |
| encryption        | plaintext-allowed or encrypted       | at_rest + storage_state            | plaintext or mixed-legacy store |
| network           | effective flags snapshot             | listener list                     | never (effective snapshot) |

- `drifted: true` stages are collected in `drifted_stages` — a queryable,
  loud condition.
- Startup prints one line per stage; drift lines are prefixed
  `CONFIG DRIFT` and go to stderr, not a log file someone has to remember to
  read.
- Sanitized by construction: hosts and kind labels only — no secrets, keys,
  or full URLs.
- Extends #870 (deployment profile = resolved posture only) with the
  requested half; the two reports remain complementary.

## Intentional divergences from the borrow

- Hy-Memory's drift went unnoticed for hours; MindCache's for an entire
  ingest run. Here drift is printed at startup and queryable via one tool —
  the condition is loud at both points.
- CogniCore-style silent type fallback is NOT used for invalid config:
  invalid values fail closed at open (existing behavior, unchanged).

## Tests

`src/config_report.rs` (5): default config drift-free (test-harness plaintext
stores are correctly reported as encryption drift — loud by design),
degraded embedding backend drifts (provider configured while LLM integration
is off), quantized store without flag drifts against the default, db-path
env mismatch drifts, machine-readable shape stability.
