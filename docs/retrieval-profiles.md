# Retrieval profiles

`mimir_recall` supports `retrieval_profile` as an explicit serving posture applied after Vault visibility enforcement and before external-reference/time filters.

| Profile | Eligible memory | Scope behavior |
|---|---|---|
| `shared` (default) | Any non-`preference`/`personal` class | Strictly the requested workspace; omitted workspace preserves legacy unscoped search. |
| `personal` | `preference`, `personal` | Searches global/personal policy records, then returns only personal classes. |
| `agent` | `convention`, `correction`, `keystone` | Searches global agent-operating records, then returns only governed operating classes. |

The profile can only narrow the result set after `can_read` visibility checks. It does not bypass agent tier, fleet/private visibility, or expose another workspace through the shared profile.

Responses include `retrieval_profile` so consumers can inspect the posture that served the result. The profiles compose with existing trust-weight ranking and workspace scoping; they are an additional serving filter rather than a replacement authorization model.

Unknown profile names are rejected clearly. Regression coverage: `recall_profiles_partition_personal_agent_and_shared_memory`.
