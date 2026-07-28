# Operator review queue

`perseus_vault_operator_review` provides a read-only maintenance surface for operator triage. The legacy `mimir_operator_review` alias remains callable during the v2 compatibility window.

```json
{"category":"general","limit":50,"stale_threshold":0.35}
```

It combines three durable review lanes without modifying or hiding findings:

- **contradictions**: the existing conflict report, including all detected candidates;
- **stale candidates**: low-actionability records from the hygiene surface, with reasons;
- **supersession lag**: deprecated records that require confirmation of a successor.

The report includes a review timestamp and is safe to run repeatedly. It intentionally does not auto-resolve conflicts: operators can inspect evidence and use the existing conflict-resolution or supersession APIs with an explicit dry run where appropriate.

This is a review surface, not an authorization bypass; underlying entity visibility and mutation controls remain unchanged.
