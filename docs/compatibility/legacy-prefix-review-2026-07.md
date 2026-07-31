# Legacy Tool-Prefix Compatibility Review — 2026-07-25

## Decision: NO-GO

Do **not** open or implement a v3 issue that removes the callable `mimir_*` or
`mneme_*` aliases. None of the time-based evidence gates is mature, no released
build has completed an observation window, and maintained surfaces still contain
legacy call names.

This report satisfies the review requirement; it does not authorize removal.
The v2 compatibility commitment remains unchanged.

## Review scope

- Canonical prefix: `perseus_vault_*`
- Legacy callable aliases: `mimir_*`, `mneme_*`
- Review date: 2026-07-25 UTC
- Current stable release: v2.21.0, published 2026-07-22
- Prefix counter implementation: #764 / #772
- Required evidence window: at least 90 days after the first stable v2 release
  containing the readout, spanning at least two stable v2 releases

## Gate results

| Gate | Result | Evidence |
|---|---|---|
| At least two stable v2 releases contain the readout | **FAIL** | v2.21.0 predates #764/#772. Qualifying releases: 0 of 2. |
| At least 90 days of observation | **FAIL** | The clock starts when the first stable release containing `perseus_vault_alias_usage` is published. No such release exists yet. |
| Zero observed legacy calls across maintained deployments | **NOT MEASURED** | No released build exposes the readout, so the sample contains 0 deployments and 0 observation time. This is not evidence of zero legacy use. |
| All maintained clients use canonical calls without `PERSEUS_VAULT_TOOL_ALIASES=all` | **FAIL** | Canonical clients exist, but maintained repository surfaces still contain legacy names; see the matrix below. |
| Clean-install and upgrade walkthroughs pass | **NOT RUN** | They must use a stable release containing the readout and cover both a clean store and a pre-readout upgraded store. |
| No unresolved compatibility reports for 90 days | **FAIL** | The required 90-day interval has not begun. Absence of reports before observability ships does not satisfy the gate. |

## Aggregate call counts

No call totals are published in this review.

| Sample | Deployments | Observation duration | Canonical | Mimir | Mneme | Other |
|---|---:|---:|---:|---:|---:|---:|
| Stable releases containing readout | 0 | 0 days | Not measured | Not measured | Not measured | Not measured |

Writing zeroes in the count columns would incorrectly turn “not observed” into
“observed zero.” Future reports must preserve that distinction.

## Maintained client and surface matrix

| Surface | Evidence reviewed | Canonical status | Gate status |
|---|---|---|---|
| Perseus Vault default `tools/list` | `tool_aliases_default_to_canonical_only` and the #764 stdio smoke | Advertises only `perseus_vault_*` by default; aliases remain callable | Pass for server behavior |
| Hermes Perseus Vault memory provider | Deployed provider call sites inspected 2026-07-25 | Uses canonical `perseus_vault_*` calls | Pass for source audit; runtime snapshot still required |
| Perseus CLI | Current main dynamically prefers `perseus_vault_*`, then falls back to legacy names | Canonical-first | Pass for source audit; runtime snapshot still required |
| Claude Desktop MCPB | Uses the bundled server’s MCP registry rather than hard-coded call names | Canonical by default through `tools/list` | Runtime snapshot and clean-install walkthrough required |
| MCP Registry package | Publishes the current server package | Canonical by default through `tools/list` | Runtime snapshot required |
| Obsidian integration | `plugins/obsidian-mimir/main.ts` contains hard-coded `mimir_vault_export` and `mimir_remember` calls | Legacy calls remain | **Fail** |
| Claude Code integration documentation | `docs/integration/claude-code.md` instructs `mimir_*` calls | Legacy examples remain | **Fail** |
| Cursor integration documentation | `docs/integration/cursor.md` instructs and verifies `mimir_*` calls | Legacy examples remain | **Fail** |
| General MCP integration documentation | `docs/integration/general-mcp.md` lists the legacy tool surface | Legacy examples remain | **Fail** |

Documentation references are not proof that a client emitted a call, but they
are migration defects: maintained instructions must not steer new deployments
toward aliases proposed for removal.

## Sampling and privacy limitations

`perseus_vault_alias_usage` is deliberately not centralized telemetry:

- counters are process-local and reset on restart;
- snapshots contain totals and a process-start timestamp only;
- no tool arguments, memory content, keys, IDs, credentials, client identity, or
  request metadata are retained or emitted;
- a snapshot cannot identify which client or tool verb produced a count;
- deployments that do not volunteer snapshots are outside the sample;
- sampled maintained deployments cannot prove behavior of unknown external users;
- restarts split one deployment’s evidence into multiple observation intervals.

Every future report must disclose sampled deployments, exact server versions,
counter start/end timestamps, restarts, missing intervals, and opt-in external
reports. It must never describe the sample as global adoption telemetry.

## Required next review procedure

A future reviewer may issue a **GO** decision only after all steps pass:

1. Identify the first stable v2 release containing
   `perseus_vault_alias_usage`; record its publication timestamp as the start of
   the earliest possible observation window.
2. Confirm at least one later stable v2 release also contains the unchanged
   readout contract.
3. For Cloud, Greg, release fixtures, and each other maintained deployment,
   record version, process-start timestamp, snapshot time, restart gaps, and the
   four aggregate totals. External reports remain opt-in.
4. Require `mimir_calls == 0` and `mneme_calls == 0` for every sampled interval.
   Any observed legacy call resets the decision to NO-GO until its source is
   migrated and a fresh evidence window completes.
5. Audit maintained client source and documentation for canonical calls and
   confirm `PERSEUS_VAULT_TOOL_ALIASES=all` is not required.
6. Run and record clean-install and upgrade walkthroughs for every maintained
   client/package path, including Claude Desktop MCPB and MCP Registry installs.
7. Review compatibility issues over a continuous 90-day interval and require no
   unresolved reports at decision time.
8. Publish a new dated report with raw aggregate totals, the client/version
   matrix, limitations, and an explicit GO or NO-GO decision.

If the first qualifying stable release were published on 2026-07-25, the
90-day date would be 2026-10-23. Because no qualifying release exists on this
report date, that is only an illustrative lower bound, not the scheduled review
date.

## Removal-work prohibition

Do not open the v3 alias-removal implementation issue while the latest dated
report says NO-GO. A future GO report must precede the RFC and implementation
issue; it must not be inferred from elapsed calendar time or lack of bug reports.
