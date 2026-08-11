# Scheduled recall evaluation (ops)

`perseus-vault eval` records bounded quality-run snapshots and alerts when
recall quality regresses. It is the operator-facing half of issue #930; the
design spec is `docs/specs/scheduled-recall-eval.md`.

## Cadence

Scheduling is external (cron/systemd/launchd). The nightly pass runs the
curation job (`maintain`) and records its after-action summary; the midday
pass is eval-only:

```cron
# nightly 02:00 — curation + eval
0 2 * * * /opt/data/work/perseus-vault-main/scripts/scheduled-eval.sh nightly >> /var/log/pv-scheduled-eval.log 2>&1
# midday 12:00 — eval only
0 12 * * * /opt/data/work/perseus-vault-main/scripts/scheduled-eval.sh midday >> /var/log/pv-scheduled-eval.log 2>&1
```

`scripts/scheduled-eval.sh` runs `maintain` (nightly only), the quality
harness (`benchmark/quality/run.py` + `scorecard.py`), then
`perseus-vault eval record`. It needs the repo layout (the harness drives a
checkout-built binary over MCP stdio) and `PERSEUS_VAULT_BIN` set to that
binary. A server without the repo layout uses `--maintain-every HOURS` for
in-server hygiene and records eval runs from wherever the harness runs.

## Recording a run

```bash
perseus-vault eval record --db /path/to/vault.db \
  --kind nightly --report /tmp/quality.json \
  --scorecard /tmp/scorecard.json \
  --maintain-report /tmp/maintain.json \
  --run-id perseus-runtime-eval-<id> \
  --created-by cron
```

- `--kind` is a closed enum: `nightly | midday | manual`.
- Report files are capped at 4 MiB; only metric rates, check counts,
  digests, and accuracy are retained. Prompts, memory bodies, tool
  arguments, and credentials never reach `eval_runs`.
- `--dry-run` computes breaches without storing.
- Status: `passed` when checks pass (or the scorecard verdict is
  `release_ready`), `blocked` when the scorecard blocks, else `failed`.
  `regressed` is orthogonal — a passing run can regress.

## Thresholds

Defaults: floor/cap `0.90`/`0.10` (higher-is-better metrics; lower-is-better
for `scope_invalid_recall_rate` and `stale_recall_rate`), regression delta
`0.05` against the trailing mean of the prior 7 runs (same suite + kind).
Override per metric:

```bash
--thresholds '{"validity_rate": {"floor": 0.95, "regression_delta": 0.03}}'
```

`"floor": 0` or `"regression_delta": 0` disables that check. A metric with
no baseline is floor-checked only.

## Alerts

- `perseus-vault eval alerts --db PATH [--since-hours N]` — regressed runs
  (JSON; default window 24h). Wire a watchdog to this for paging.
- `perseus_vault_operator_review` surfaces the same regressed runs in its
  `eval_regressions` lane (read-only).
- `perseus-vault eval history --db PATH [--kind K] [--limit N] [--regressed-only]`
  and `perseus_vault_eval_history` expose the full history with a per-metric
  trend (latest/min/max/mean, breach count) for dashboards.

## No LLM required

The deterministic quality harness is CPU-only; the #916 LLM-judged arm is
out of scope for this contract.
