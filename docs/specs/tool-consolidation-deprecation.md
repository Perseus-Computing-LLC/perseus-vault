# Tool Consolidation & Deprecation Aliases (#1053)

**Status:** adopted (2026-08-14) · **Scope:** any future merge of canonical MCP
tools (retrieval family in particular)

## Principle

The tool registry is a **public contract**. Any merge must preserve callable
legacy names (deprecation aliases) for **at least one major release cycle** —
an agent's stored prompts, integrations, and habits reference tool names; a
silent rename breaks them.

## Family analysis (value / risk)

| Family | Merge idea | Verdict |
| --- | --- | --- |
| Retrieval (`recall` / `recall_batch` / `recall_layer` / `recall_when` / `semantic_search` / `global_recall`) | canonical `recall(mode=...)` | **HIGH risk — DEFER to a versioned major.** The query contract (see #562) is the most-integrated surface in the codebase. Scopes already hide the specialized arms from the `agent` view. |
| State CRUD (`state_set`/`get`/`delete`/`list`) | one `state` tool with verbs | MED risk — separate verbs are the MCP idiom; merging hurts agent ergonomics more than it helps. **NOT WORTH IT.** |
| Workspace (`bind`/`unbind`/`quarantine`/`status`/`list`) | — | 5 distinct operator ops. **NOT WORTH IT.** |
| Export (`vault_export` / `derived_export`) | one export tool | OPTIONAL later; distinct output contracts today. |
| Grooming depth (`decay`/`compact`/`prune`/`purge`/`maintenance`) | — | distinct destructive semantics; admin/ops tiering already scopes them. **NOT WORTH IT.** |

## Adopted plan

1. **No API-breaking merges this cycle.**
2. When client data shows real selection errors on the retrieval family
   (measured via `perseus_vault_alias_usage` telemetry, not speculation):
   introduce a canonical tool with a `mode` parameter; **legacy tools stay
   callable**, re-tiered `ops`, descriptions prefixed
   `Deprecated — use perseus_vault_recall(mode=...)`.
3. Revisit once `alias_usage` telemetry accumulates across deployments.

## Relationship to the lifecycle policy

Deletion (see [tool lifecycle policy](tool-lifecycle-policy.md)) is a separate,
stricter path: a tool must be a trim candidate (zero-referenced >= 3 releases
**and** superseded) AND survive a deprecation-alias cycle before it can be
removed. Consolidation and deletion share one rule: **the registry never breaks
a caller without a named replacement and a release-cycle runway.**
