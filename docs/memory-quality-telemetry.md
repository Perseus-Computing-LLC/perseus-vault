# Memory-quality telemetry

`mimir_quality_telemetry` is a machine-readable operational report for memory-health monitoring:

```json
{"category":"general"}
```

The response includes:

| Signal | Interpretation |
|---|---|
| `contradiction_count`, `contradiction_rate` | Conflicting candidates discovered in the selected category, normalized by active memory count. Rising values warrant operator review. |
| `supersession_lag_count` | Active records marked `deprecated`; these need successor/revalidation review. |
| `class_distribution` | Active memory-class mix. Unexpected growth can identify ingestion drift. |
| `layer_distribution` / `promotion_flow` | Current buffer/working/semantic/core population proxy. Large accumulation in a low layer can indicate poor retrieval or promotion. |
| `served_tier_mix` | States where served-tier evidence is exposed: recall profile/explanation output. |

This report is read-only. It supports dashboards and regression monitoring but does not resolve conflicts or archive records. Use `mimir_operator_review` to inspect candidates and explicit maintenance tools to make changes.
