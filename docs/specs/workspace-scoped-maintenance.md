# Workspace-scoped maintenance (consolidate / dream)

Status: implemented (#854)
Scope: `mimir_consolidate`, `mimir_dream`, and the consolidation step of
`mimir_autocohere`

## Problem

`mimir_consolidate` and `mimir_dream` previously scanned `entities` by
category with **no workspace predicate**. A maintenance or dream run could
cluster same-category facts from multiple workspaces and emit a derived
record (`observation` / `insight`) with `workspace_hash = ''` whose
provenance links pointed back across workspace boundaries. That made derived
memories silently global even in a multi-workspace vault.

## Contract

Every consolidation/dreaming run must name exactly one scope:

| `workspace_hash` | `global` | Effective scope |
|---|---|---|
| `Some(ws)` (non-empty) | `false` | Scoped run: all source scans, clusters, evidence links, and archive operations restricted to `workspace_hash = ws` (strict equality — legacy `''`/global entities are **not** included). Derived records inherit `ws`. |
| `Some("")` | `false` | Scoped to the legacy/global workspace (`workspace_hash = ''`). |
| `None` | `true` | Global run: whole-vault, deliberate, labeled, and audited. |
| `None` | `false` | **Error** (fail-closed): "workspace scope required". |
| `Some(ws)` + `true` | — | **Error**: ambiguous ("mutually exclusive"). |

## Global mode

Global mode is an explicit, reviewable operation:

- **Authorization**: identity-carrying callers (`requesting_agent_id`, stamped
  by the MCP transport from `clientInfo.name` and overwriting any forged
  value) FAIL CLOSED: they must hold capability `memory.maintenance.global`
  in the **system scope** — an authority manifest with
  `workspace_hash = "*"` (the system/global-scope sentinel; manifests
  require a non-empty workspace). Missing manifest or missing capability =
  denied. Anonymous callers (no host identity) fail open — there is no
  identity to hold accountable, matching the authority regime's pre-manifest
  behavior.
- **Labeling**: reports expose `workspace_hash: null` + `global: true`; MCP
  output schemas carry both fields.
- **Audit**: real (non-dry-run) global consolidate runs journal a
  `maintenance_global_run` event with the requesting agent; dream journal
  events (event_type `dream`) carry the effective scope
  (`scoped:<ws>` or `global`) and the requesting agent.

## Derived-record inheritance

Scoped runs write derived records (`observation`, `insight`) with the run's
workspace and the requesting agent as author — a scoped run can no longer
produce a record that silently becomes global. Global runs write records with
`workspace_hash = ''` (system scope), exactly as before, but now labeled and
audited.

## `mimir_autocohere`

`mimir_autocohere`'s consolidation step accepts the same `workspace_hash` /
`global` fields. When neither is given it keeps its historical whole-vault
behavior (global pass, now capability-gated for identity-carrying callers).
The other grooming steps (cohere/decay/compact) remain whole-vault by design
and are out of scope for this contract.

## Tests

- `consolidate_requires_workspace_scope_or_global` — fail-closed and
  ambiguity rejection.
- `consolidate_never_merges_across_workspaces` — two-workspace fixture: no
  cross-workspace cluster or evidence, observation inherits the scope, the
  other workspace's entities untouched.
- `consolidate_global_mode_crosses_workspaces_and_audits` — global mode
  clusters across workspaces, derived record is system-scoped, audit event
  present with the requester.
- `consolidate_global_mode_denied_without_capability` — manifest without
  `memory.maintenance.global` denies the global run; scoped runs still work.
- `consolidate_global_mode_denied_without_manifest` — identity-carrying
  caller with no manifest at all is denied global mode (fail-closed).
- `transport_host_identity_overrides_forged_requesting_agent_id` — an MCP
  caller-supplied (forged) `requesting_agent_id` is overwritten by the
  captured `clientInfo.name`; the correction attributes the host.
- `dream_fallback_consolidate_is_scoped` — the no-LLM fallback carries the
  workspace scope into the mechanical pass.
- `dream_scoped_restricts_scan_and_inherits_workspace` — scoped dream
  examines only the scoped workspace, insight evidence stays in-scope,
  insight and journal event carry the scope.
- `correction_host_identity_overrides_model_agent_id` /
  `correction_empty_attribution_records_legacy_empties` (#855) — attribution
  propagation through correction entities, journal events, tombstones, and
  results.
