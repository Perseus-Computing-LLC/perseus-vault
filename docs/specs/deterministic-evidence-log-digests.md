# Deterministic evidence-preserving log digests

Status: implementation contract  
Date: 2026-07-31  
Resolves: #812  
Depends on: `artifact-manifests-and-exact-evidence.md`

`perseus_vault_artifact_log_digest` is a deterministic navigation view over a
visible immutable artifact. It is not an LLM summary and does not replace the
source bytes.

## Contract

1. The tool resolves the source through the artifact read path first. Workspace
   and visibility filtering happen before template aggregation, counts,
   provenance, or anchors are constructed.
2. The source must be UTF-8 text. The original artifact remains the evidence
   authority and is available through `perseus_vault_artifact_excerpt`.
3. Repeated non-protected lines are deterministically normalized only for
   high-cardinality numeric and long-identifier tokens. Each collapsed section
   reports its exact count from original records and first/last source anchors.
4. The following lines are preserved verbatim by default: lines containing
   `error`, `warn`/`warning`, `exception`, `fatal`, `panic`, `denied`,
   `refused`, `timeout`, `assertion`, or `traceback` (case-insensitive).
5. The output states the number of omitted repeat occurrences and carries a
   retrieval instruction for exact source bytes.
6. Same source bytes and config version produce byte-identical digest content.

## Derived-artifact semantics

Every generated digest is registered as a derived artifact:

```json
{
  "kind": "derived",
  "derived_from_sha256": "<full source SHA-256>",
  "derivation_kind": "evidence_log_digest",
  "derivation_version": "evidence-log-digest-v1"
}
```

The derived digest inherits only a binding already visible to the caller. It
never changes, deletes, substitutes for, or grants access to the original
artifact.

## Output shape

```json
{
  "digest": {
    "format": "perseus_vault_evidence_log_digest",
    "source_sha256": "<full SHA-256>",
    "config_version": "evidence-log-digest-v1",
    "input_line_count": 42,
    "omitted_line_count": 31,
    "protected_line_count": 2,
    "sections": [{
      "kind": "collapsed_template",
      "template": "INFO task=<value> complete",
      "count": 33,
      "first": {"sha256":"...","byte_start":0,"byte_end":23},
      "last": {"sha256":"...","byte_start":800,"byte_end":824},
      "omitted_occurrences": 31
    }],
    "protected_lines": [["ERROR deploy timeout", {"sha256":"..."}]],
    "retrieval": "Use perseus_vault_artifact_excerpt with returned anchors for exact original bytes."
  },
  "derived_artifact_sha256": "<digest SHA-256>",
  "representation": {"kind":"derived", "derived_from_sha256":"..."}
}
```

## Safety and limits

- no regex processing
- no LLM calls
- no inferred counts
- protected severity lines are not collapsed or removed
- source anchors are exact ranges into original bytes
- the #811 artifact registration and retrieval limits remain in force

## Fixture coverage

The implementation tests CI, deploy, service, and high-repeat record shapes.
They prove deterministic output, exact template accounting, source-anchor
presence, and that protected severity lines are preserved verbatim.
