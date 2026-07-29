# Promotion-aware recall explanations

`perseus_vault_recall` returns a `why_served` object on every recalled item. The legacy `mimir_recall` alias remains callable during the v2 compatibility window. This metadata does not alter ranking, visibility filtering, temporal reconstruction, or reinforcement.

| Field | Meaning |
|---|---|
| `memory_class` | The entity category returned by recall. |
| `promotion_state` | The ladder state recorded by `promotion_transition.to_state`, or `unpromoted`. |
| `support_count` | Number of explicit source evidence IDs carried by the entity. |
| `source_evidence_ids` | IDs from promotion provenance, if present. |
| `promoted_scope` | Workspace scope of the served entity. |
| `reason` | Compact serving explanation; currently `matched the recall query`. |

These fields are added only after the normal visibility filter has removed inaccessible rows, so they cannot expose hidden evidence.
