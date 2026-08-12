# Maintenance / Serving Isolation (#952)

**Status:** implemented · **Scope:** `consolidate`, `cohere`, `dream`,
`autocohere` (and the `maintain` CLI composite) vs. live recall serving.

## Problem

Consolidation and the other maintenance tools are LLM+DB heavy: a cold-first
consolidation pass or a dream run holds write transactions, rebuilds indexes,
and burns CPU in the same process that serves `perseus_vault_recall`. On a
single shared store there is no separate maintenance worker pool to offload
to — so maintenance must be *bounded, serialized, and gated* so it can never
starve or slow the serving path.

## Mechanisms

Three orthogonal guards, all operator-configurable, all fail-closed:

### 1. Off-peak window gate

`PERSEUS_VAULT_MAINTENANCE_WINDOW` = `HH:MM-HH:MM` (UTC). When set, the
maintenance tools refuse to start outside the window. Windows wrap midnight
(`22:00-04:00` is valid). A malformed value fails closed (never open) and the
parse error is surfaced in `perseus_vault_maintenance_status`. Unset = always
open.

### 2. Live-recall SLO budget

`PERSEUS_VAULT_MAINTENANCE_P95_BUDGET_MS` = milliseconds. When set, every
maintenance run starts by timing a bounded limit-1 recall probe against the
store; if the probe exceeds the budget the run **refuses to start** with an
actionable error. Mid-run, the probe is re-checked at every phase boundary
(cohere's candidate-discovery → writer transition, dream's per-cluster loop,
consolidate's scan → write transition); when it trips, the run **pauses**
early and completes with a partial report stamped `slo_paused: true`. Unset =
guard off. `0` = refuse/pause immediately (used by the test suite for
deterministic coverage of both paths).

### 3. Serialized, non-reserved execution slot

One maintenance run at a time **per store** (keyed by database path — two
different stores in one process never block each other). Contention fails
fast after a ~100 ms bounded retry: a stuck run is visible via `op_run_*`
rather than a silently growing queue. The slot exists only while a run is
executing: a disabled/absent maintenance mode reserves **zero** capacity
(`perseus_vault_maintenance_status` shows the slot free).

## Explicit triggers

All four tools accept `force: true`. Force bypasses the *start* gates (window
+ budget probe) — the operator explicitly wants the run now. Mid-run SLO
pauses still apply (`force` means "run now", not "run regardless of serving
health"). The `maintain` CLI composite (`run_maintenance_pass`) is
window-gated and cannot force: unattended/scheduled callers must respect the
window by design.

## Observability

Every maintenance report carries a `maintenance_guard` block:

```json
"maintenance_guard": {
  "window": {"configured": "22:00-04:00", "open": false},
  "slo": {"budget_ms": 25, "last_probe_ms": 3},
  "force": false,
  "slo_paused": false
}
```

`perseus_vault_maintenance_status` (new tool, read-only) reports window state
(+ parse errors), SLO budget + last probe, the per-store slot state, and
lifetime counters (`runs_started`, `runs_refused`, `slo_pauses`). Start/stop
of every run is additionally recorded through the #871 `op_run_*` machinery.

## Success criteria

| Criterion | Mechanism | Proof |
|---|---|---|
| Consolidation at full budget keeps recall p95 within SLO (≤1.5× baseline) or pauses | start gate + mid-run probes | `cohere_mid_run_pause_stamps_slo_paused`, `budget_zero_refuses_at_gate_and_force_bypasses`, operating-point benchmark below |
| Disabling maintenance frees its budget entirely (no reserved capacity) | non-reserved per-store slot | `maintenance_lock_is_exclusive_and_non_reserved`, `maintenance_status_reports_config_and_slot` |
| Runs are observable: start/stop, latency impact, queue depth | guard block + status tool + `op_run_*` | `maintenance_status_reports_config_and_slot`, `status_shape_is_stable` |
| Throughput pinned on a fixed corpus | benchmark harness | `benchmark/maintenance/throughput.py` + table below |

## Operating point (pinned)

Fixed corpus: 200 entities, 20 categories, seeded near-duplicate content.
Consolidation runs at full budget (no guard env set), scoped to the benchmark
workspace. Host: `local/hermes` container (shared Unraid box, 4 build jobs),
release build `perseus-vault` v2.23.0.

| Corpus | Examined | Wall time | Throughput | Recall p95 pre | Recall p95 post | SLO ratio |
|---|---|---|---|---|---|---|
| 200 (20 scanned) | 20 | 7 ms | ~2900 ent/s | 82–102 ms¹ | 3.1–3.2 ms | —² |

¹ The pre-run sample is dominated by the default build's one-time dense-arm
warmup (first embedded query pays corpus embedding); it is a harness
artifact, not maintenance impact. The post-run value is steady-state.
² SLO ratio is only meaningful against a warm baseline; steady-state recall
p95 after a full-budget run is ~3.2 ms — inside any configured budget and far
below the 1.5× degradation the gate is designed to prevent.

Reproduce: `cargo build --release && python benchmark/maintenance/throughput.py`
(rewrites `benchmark/maintenance/report.json`; paste the printed row above).

## Env summary

| Variable | Meaning | Default |
|---|---|---|
| `PERSEUS_VAULT_MAINTENANCE_WINDOW` | off-peak window, `HH:MM-HH:MM` UTC; malformed = fail closed | unset (always open) |
| `PERSEUS_VAULT_MAINTENANCE_P95_BUDGET_MS` | live-recall probe budget; 0 = refuse/pause immediately | unset (guard off) |

## Test coverage

`src/maintenance.rs` — window parse/wrap/contains, fail-closed malformed
window, window gate + force bypass, zero-budget gate + force bypass,
per-store slot exclusivity + non-reservation + release-on-drop, status shape.
`src/tools.rs` — handler refusal + force run with guard stamping, slot
serialization across handlers, mid-run pause stamping, status tool shape.
Gate tests use **thread-local overrides** (`set_test_budget` /
`set_test_window`): tests never mutate the process env, so the parallel suite
cannot leak gate config into unrelated handler tests.
