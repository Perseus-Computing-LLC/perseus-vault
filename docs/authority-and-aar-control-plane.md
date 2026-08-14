# Authority manifests and the Authorized-Action Receipt (AAR) control plane

This document is the operator/agent usage contract for the vault's
authorization substrate (`#768`, `#836`, `#865`): how authority manifests
gate agent actions, and how every publication action is recorded as a
durable, hash-committed receipt. Read this before wiring an agent or a
workflow to `authority_set` / `action_intent` / `action_lease_acquire` /
`action_complete`.

## 1. Manifests are per-workspace and must be explicit

A manifest binds `(agent_id, workspace_hash)` and **cannot** be created for
the empty/global scope:

```
authority manifest requires a non-empty workspace_hash (authority is
per-workspace; empty/global scope is not valid for manifests)
```

Memory entities may live in the global scope (`workspace_hash = ""`), but
authority is intentionally stricter: a manifest must name the exact
workspace it governs, so a policy can never accidentally apply to
everything. Consequences for operators:

- Choose a deterministic workspace identifier and use it consistently
  (e.g. `sha256("Org/repo")` — stable across machines).
- Configure the vault client with that identifier. Hermes plugin:
  `PERSEUS_VAULT_WORKSPACE` env var or
  `memory.perseus-vault.workspace_hash` in `config.yaml` (read at session
  start; the plugin surfaces its resolved scope in diagnostics).
- An agent session that resolves to a blank workspace cannot participate
  in the AAR plane until it is configured with the same hash.

## 2. Manifest fields and match semantics

| Field | Semantics |
|---|---|
| `allowed_capabilities` | Exact-string capability names (`git push`, `github pull request creation`, `github pull request merge`, `github issue closure`, …). An intent's `capability` must equal one verbatim. |
| `scope_anchors` | **Exact-match** trusted identifiers (e.g. `Org/repo`). An intent's `scope_anchor` must equal one of these **verbatim** — not contain, not prefix. Detailed per-run context (branch, head SHA) belongs in `resource_constraints_json`, never in the anchor. |
| `permitted_external_ref_prefixes` | Prefix-matched: an intent's `external_ref` must equal a prefix or start with `prefix + "/"`. |
| `approval_required_capabilities` | Capabilities that require `action_approve` by an `approver_principals` member before a lease can be taken. |
| `approver_principals` / `allowed_inbound_principals` | Who may approve / who may be addressed. |
| `max_parallel_actions` | One active lease per `(workspace_hash, action_key)`. |
| `mode` | `shadow` (record, do not enforce elsewhere) or `enforce`. |
| `expires_at_unix_ms` | Optional expiry; expired manifests return no active authority. |
| `capability_constraints_json` | Canonical policy over constraint fields; a denial is recorded as an explicit `denied` receipt (`#836` null-effect-on-deny) and grants nothing. |

`authority_set` rejects a manifest whose agent is not registered, with a
field-specific message naming exactly which input is invalid.

## 3. The AAR lifecycle (intent → lease → execute → complete)

1. **`action_intent`** — record the fail-closed intent. Requires
   `agent_id`, `workspace_hash`, an exact-match `scope_anchor`, a
   prefix-permitted `external_ref`, an allowed `capability`, a unique
   `action_key`, and a 64-hex `intent_hash` committing the planned action.
   Returns an `act-…` id with status `intent` (or `approval_requested`).
   Rejections are self-explanatory: each denial names the offending value
   **and** lists the permitted set from the active manifest.
2. **`action_approve`** (only for approval-required capabilities) — an
   `approver_principals` member grants/denies; the action transitions to
   `approval_granted` / `approval_denied`.
3. **`action_lease_acquire`** — take the single active lease for
   `(workspace_hash, action_key)`; denied actions can never be leased.
4. **Execute** the external action (git push, PR, …) — the lease is the
   concurrency guard; `external_ref` + `resource_constraints_json` pin the
   scope.
5. **`action_complete`** — `executed` / `failed` / `cancelled` (+ for
   denied approvals only `denied`), with a 64-hex `outcome_hash`
   committing the result evidence. The actor must equal the action owner.
6. **`action_receipt_get`** — durable receipt with manifest version, all
   hashes, and the full lifecycle status.

Leases are released with `action_lease_release` or expire via TTL;
completed/executed actions remain queryable receipt history.

## 4. Compensation admission (#1033): findings, not self-claimed undo

Compensation — an action that undoes or reworks an earlier effect — is the
strongest authority-laundering vector in the model: "I'm just undoing my
earlier action" must never smuggle fresh mutations past authorization. Two
rules make it enforceable at the gate:

1. **Detection produces a finding, never a decision.** A detection pass
   (supersession impact index, `perseus_vault_impact_report`) surfaces
   findings; it cannot self-trigger execution. The disposition
   (accept-drift / revalidate-pending / open-compensation-case / escalate)
   is chosen by the authority plane. Findings are durable, receipted
   records created via `perseus_vault_finding_record` — the surface
   compensation intents cite to prove they are grounded in a real,
   detected impact.
2. **Authority is evaluated at compensation time, never inherited.** A
   revoked principal cannot act — even to undo its own past effect. The
   compensation runs under whoever holds valid authority now; if none, the
   finding waits in review. The deliberate exception is a receipted
   remediation handoff: a "revoke A, hand open cases to B" action, filed
   under the revoker's still-active authority as its own authorized action
   with its own receipt, transfers remediation authority to B.

An `action_intent` whose `compensates_for` names an original effect receipt
additionally requires (fail-closed, stable reason codes):

| Check | Violation reason code |
|---|---|
| `finding_ref` + `superseding_head` both present | `compensation_requires_finding_linkage` |
| `compensates_for` references an existing effect receipt | `original_effect_not_found` |
| `finding_ref` is an authenticated, un-archived finding | `finding_unauthenticated` |
| the finding's `covers` includes the compensated effect | `finding_does_not_cover_effect` |
| presented `superseding_head` equals the finding's `cited_head` | `superseding_head_mismatch` |
| (original authority revoked) `handoff_receipt_ref` present | `requires_receipted_handoff` |
| handoff is `action_executed` with capability `remediation_handoff` | `handoff_not_receipted` |
| handoff names the requesting agent as `beneficiary_agent_id` | `handoff_beneficiary_mismatch` |
| handoff's `original_effects` includes the compensated effect | `handoff_does_not_cover_effect` |

The verified linkage is stored on the action row (`compensates_for`,
`finding_ref`, `superseding_head`, `handoff_receipt_ref`) so the evidence
set is auditable. The remediation trace family in
`benchmark/security/traces/authority_traces.json` runs these cases
(revocation barrier, handoff transfer, no-authority fail-closed,
self-claimed undo) against the live surfaces.

## 5. Client call pattern (correct usage)

```
scope_anchor    = "Org/repo"                          # exact manifest anchor
external_ref    = "https://github.com/Org/repo"       # permitted prefix (or prefix + "/…")
capability      = "git push"                          # exact allowed capability
resource_constraints_json = '{"branch":"feat/x","head":"<sha>"}'   # per-run detail
```

Common mistakes (each now yields a message stating the fix):

- Passing a descriptive `scope_anchor` such as
  `"Org/repo branch feat/x head abc123"` — anchors match exactly; use the
  bare anchor and move detail into `resource_constraints_json`.
- Passing an `external_ref` that is not a permitted prefix or a
  `prefix + "/"` continuation.
- Recording intents from an unscoped session (`workspace_hash = ""`) while
  the manifest lives under a named workspace.

## 6. Client configuration summary

- Vault server: `authority_set` / `authority_get` / `authority_revoke`
  (plus signed-profile variant `authority_set_signed`).
- Hermes memory plugin: set `PERSEUS_VAULT_WORKSPACE` or
  `memory.perseus-vault.workspace_hash` to the manifest's workspace hash
  so sessions bind to the AAR scope.
- Operators set manifests; agents record intents. An agent must never
  author its own manifest — that is the boundary the per-workspace regime
  exists to protect.
