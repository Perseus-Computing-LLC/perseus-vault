# Scheduled Recall Evaluation with Regression Alerts

Status: design specification | draft
Date: 2026-08-11
Resolves: #930 · Consumed by: perseus-vault#930
Related: `docs/scheduled-recall-eval.md` (#930), `benchmark/quality/run.py` (#862), `docs/operator-review.md`

## 1. Overview

The Vault ships deterministic memory-quality evaluation (`benchmark/quality`,
52-case manifest, `release_ready` scorecard) but nothing measures recall quality
over time or alerts when it regresses. This spec adds a durable eval-history
contract: scheduled eval runs (nightly curation + midday eval) record bounded
metric snapshots, regression thresholds are evaluated deterministically against
the trailing window, and breaches surface through the operator review queue
pattern — the same read-only lane mechanism as contradictions/stale/supersession.
Eval history is queryable over MCP for dashboards.

Design posture: the Vault **records and alerts**; it does not run the harness.
The Python harness drives a checkout-built binary over MCP stdio (repo layout,
no network), so the scheduler (cron/systemd/launchd) orchestrates: run
`benchmark/quality/run.py`, then `perseus-vault eval record --report <file>`.
The binary cross-checks against its own DB history. No LLM is required —
the deterministic gate stays CPU-only (the #916 LLM-judged arm is out of scope).

## 2. Data schema — `eval_runs` (schema v38)

```sql
CREATE TABLE IF NOT EXISTS eval_runs (
    id TEXT PRIMARY KEY,              -- evr-<unix_ms>-<rand>
    run_id TEXT NOT NULL DEFAULT '',  -- external correlation (perseus runtime-eval run_id)
    eval_kind TEXT NOT NULL,          -- nightly | midday | manual
    suite TEXT NOT NULL DEFAULT 'memory-quality-v1',
    status TEXT NOT NULL,             -- passed | failed | blocked
    run_at_unix_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    manifest_digest TEXT NOT NULL DEFAULT '',  -- control_profile_sha256
    binary_digest TEXT NOT NULL DEFAULT '',    -- binary_sha256
    harness_version TEXT NOT NULL DEFAULT '',
    checks_passed INTEGER NOT NULL DEFAULT 0,
    checks_total INTEGER NOT NULL DEFAULT 0,
    accuracy REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',   -- metric_rates (bounded, 12 entries)
    maintain_summary_json TEXT NOT NULL DEFAULT '',  -- nightly after-action summary
    breaches_json TEXT NOT NULL DEFAULT '[]',  -- [{metric, current, trailing_mean, delta, threshold_type, direction}]
    regressed INTEGER NOT NULL DEFAULT 0,      -- any breach
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_kind ON eval_runs(eval_kind, run_at_unix_ms);
CREATE INDEX IF NOT EXISTS idx_eval_runs_regressed ON eval_runs(regressed, run_at_unix_ms);
```

Stored fields are booleans/counters/digests only. Prompts, memory bodies, tool
arguments, and raw MCP payloads are rejected at ingestion (report-level
validation, same allowlist posture as the harness).

## 3. Regression computation (`src/eval_regression.rs`, pure)

- Input: current `metric_rates` map, trailing window of prior runs (same
  `suite` + `eval_kind`, oldest→newest, default window 7, minimum 1 for any
  comparison), per-metric thresholds.
- Direction: higher-is-better default; `LOWER_IS_BETTER = {scope_invalid_recall_rate, stale_recall_rate}`.
- Defaults: floor `0.90` / cap `0.10` (lower-is-better); `regression_delta = 0.05`.
  Higher-is-better regression: `current <= trailing_mean - 0.05`.
  Lower-is-better regression: `current >= trailing_mean + 0.05`.
- Overrides: `--thresholds '{"<metric>":{"floor":0.9,"regression_delta":0.05}}'`;
  `floor: 0` or `regression_delta: 0` disables that check.
- A metric with no baseline (first run) is floor-checked only. Metrics with
  `status != "available"` or missing rates are skipped. NaN rates are skipped.
- Breach record: `{metric, current, trailing_mean, delta, threshold_type: "floor"|"regression", direction}`.
- `regressed = breaches.is_empty() == false`.

## 4. Operational rules

1. `eval record` validates the report: parseable JSON, `metric_rates` object,
   integer `checks_total`/`checks_passed`, bounded `accuracy`; rejects
   oversized payloads (report > 4 MiB).
2. `eval record --kind` is restricted to `nightly|midday|manual` (closed enum,
   unknown → error, matching `valid_op` precedent).
3. Status: `failed` when `checks_passed < checks_total` or the scorecard
   verdict is `blocked` (when `--scorecard` provided); else `passed`.
   `regressed` is orthogonal (a passing run can regress).
4. `eval record --maintain-report` attaches the nightly after-action summary
   (JSON, size-bounded) for observability of the curation pass.
5. `eval history` / `perseus_vault_eval_history` are read-only; they never
   mutate, matching the operator-review surface contract.
6. The operator review queue gains an `eval_regressions` lane: recent
   (default 10) runs with `regressed=1`, including breaches and a pointer to
   `perseus_vault_eval_history`. Read-only surfacing; no auto-resolution.
7. Scheduling is external and documented: nightly = `maintain` then `eval
   record --kind nightly --maintain-report ...`; midday = `eval record --kind
   midday`. `scripts/scheduled-eval.sh` is the reference cron wrapper.
8. No secrets: `--created-by` is an agent label; credentials never appear in
   run records. The report itself already excludes raw payloads.

## 5. API surface

CLI (mirrors `op-runs` action-style subcommand):

```text
perseus-vault eval record  --db PATH --kind nightly|midday|manual --report FILE
                           [--run-id ID] [--scorecard FILE] [--maintain-report FILE]
                           [--thresholds JSON] [--created-by AGENT] [--dry-run]
perseus-vault eval history --db PATH [--kind K] [--limit N] [--regressed-only]
perseus-vault eval alerts  --db PATH [--since-hours N]
```

MCP tool `perseus_vault_eval_history` (read-only):

```json
{"kind": "midday", "limit": 20, "regressed_only": false}
```

Response: `{runs: [...], trend: {<metric>: {trailing_mean, min, max, latest, breaches_n}}}`.
`perseus_vault_operator_review` additionally returns `eval_regressions`.

## 6. Implementation slice

1. `SCHEMA_VERSION` 37→38; `eval_runs` DDL + indexes in the v38 migration block.
2. `src/eval_regression.rs`: pure threshold/regression module + unit tests
   (floor, delta, direction, window, overrides, no-baseline, NaN, skip).
3. `db.rs`: `eval_run_record`, `eval_run_history`, `eval_run_alerts` + tests
   (roundtrip, kind filter, regressed flag, maintain summary, lane query).
4. `main.rs`: `Commands::Eval` (record/history/alerts) + parse tests.
5. `tools.rs`: `handle_eval_history`; extend `handle_operator_review` with the
   `eval_regressions` lane; `mcp.rs` registry entry + dispatch; registry sync
   (README/CLAIMS-AUDIT/glama.json/manifest.json/server.json, 122→123).
6. Docs: `docs/specs/scheduled-recall-eval.md` (this spec),
   `docs/scheduled-recall-eval.md` (ops: cron lines, thresholds),
   `docs/operator-review.md` lane note, CHANGELOG.
7. Gates: full suite green; quality gate `release_ready` on the branch;
   registry metadata check + integration conformance green.

## 7. Acceptance criteria

- [ ] Nightly + midday eval cadence documented and scripted; runs land in `eval_runs`.
- [ ] A metric dropping ≥ 0.05 below its trailing mean (or crossing its floor)
      marks the run `regressed` with a machine-readable breach record.
- [ ] `perseus_vault_operator_review` surfaces regressed runs without mutating.
- [ ] Eval history queryable via MCP tool and CLI with trend summary.
- [ ] Quality gate + full suite stay green; registry counts synced.
