# Hermes Profile ↔ Vault Workspace Binding (#879)

Status: **implemented (v1)** · Scope: client/integration boundary + operator
experience; vault-side scope rules remain authoritative.

## Contract

A **Hermes profile** is the identity a Hermes instance presents to the vault:
the MCP handshake's `clientInfo.name` (transport-captured, sanitized, and
authoritatively stamped as `requesting_agent_id` on every tool call — a
model-supplied id is overwritten, never trusted, #684/#855).

A **Vault workspace** is a scope namespace (`workspace_hash`) inside the vault
store. Vault-side scope rules (workspace-scoped reads/writes, fail-closed
scope requirements on consolidate/dream, #854) are authoritative regardless of
any binding.

### Binding rules

| rule | value |
|---|---|
| cardinality | one profile ↔ one workspace (PK on `profile_name`); a workspace ↔ many profiles (intentional shared memory) |
| access modes | `read_write` (default) and `read_only` — enforced at the tool boundary |
| lifecycle states | `active` → `quarantined` → (reactivate → `active`) or `unbound` |
| enforcement surface | scoped mutation tools (remember/reject/forget/link/unlink/supersede/state_set/embed/artifact_register/learned_artifact_register/expire/redact/erase/correct/follow) and scoped read tools (recall/recall_batch/recall_layer/scan/context/ask/artifact reads) |
| unbound profiles | keep the legacy unscoped behavior — binding is an **opt-in governance surface** |
| auditing | every transition journaled: `workspace_bound`, `workspace_rebound`, `workspace_unbound`, `workspace_quarantined`, `workspace_reactivated` (profile + workspace identifiers, actor, hash-only payloads) |

### Enforcement semantics (fail-closed for bound profiles)

- bound profile + target workspace ≠ bound workspace → **denied** (read or
  write) with a message naming both workspaces;
- bound profile + `read_only` + mutation → **denied**;
- bound profile + `quarantined`/`unbound` state → **denied** (any access,
  reason surfaced when present);
- unbound profile → legacy behavior (no new restrictions).

## Vault-side surface

| tool | purpose |
|---|---|
| `perseus_vault_workspace_bind(profile_name, workspace_hash, access_mode, metadata?)` | bind or re-bind (re-bind switches workspace, resets state to active; journaled) |
| `perseus_vault_workspace_unbind(profile_name, reason?)` | lifecycle → `unbound` (row retained for audit; journaled) |
| `perseus_vault_workspace_quarantine(profile_name, action=quarantine\|reactivate, reason?)` | operator lifecycle control (journaled) |
| `perseus_vault_workspace_status()` | diagnostics: all bindings with state, access mode, heartbeat (`last_seen_unix_ms`), and a computed `stale` signal (active binding whose heartbeat is older than 7 days) |

Client heartbeats: `workspace_heartbeat(profile_name, workspace_hash)` bumps
`last_seen_unix_ms` on the ACTIVE binding (schema v30). Status distinguishes
live / stale / quarantined / unbound without conflating them with empty
results.

## Recommended setup

### Separate companies / projects (isolation default)

- One Hermes profile per company/project; each profile bound to its own
  workspace: `perseus_vault_workspace_bind(profile_name=<profile>, workspace_hash=<ws>)`.
- Profiles never share a Hermes home directory; each profile runs against the
  same vault server but its own workspace.

### Intentional shared memory

- Two profiles bound to the **same** workspace:
  `perseus_vault_workspace_bind(profile-a, ws-shared)` and
  `perseus_vault_workspace_bind(profile-b, ws-shared, access_mode=read_only)` —
  writer + reader roles are explicit and enforced.
- Do **not** point two independent Hermes writers at the same Hermes home
  directory or profile directory — that is the **unsafe shared-home
  anti-pattern** (two writers, one local memory store, no coordination
  boundary). Use the vault binding mechanism instead: each profile keeps its
  own home; the vault workspace is the explicit sharing boundary.

### Diagnostics

`perseus_vault_workspace_status()` is the operator view: it shows every profile, its
bound workspace, access mode, lifecycle state, heartbeat, and staleness — and
states plainly that vault-side scope rules are authoritative. Provider-level
states (unavailable / timeout / stale / partial / empty) come from the client
provider diagnostics on top of this binding state; the vault's own embedding
backend health remains available via `perseus_vault_health`/stats.

## Schema

v30: `workspace_bindings(profile_name PK, workspace_hash, access_mode,
binding_state, quarantine_reason, bound_at_unix_ms, rebound_at_unix_ms,
unbound_at_unix_ms, last_seen_unix_ms, metadata_json)` +
`idx_workspace_bindings_ws`. Additive, backfill-free, idempotent migration.

## Non-goals (unchanged)

- No replacement of Hermes built-in memory/skills.
- No implicit cross-workspace/global analysis: any global mode stays explicit,
  authorized, and auditable.
- No provider-specific behavior in the vault.
