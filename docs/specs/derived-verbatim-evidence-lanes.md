# Governed derived and verbatim evidence lanes (#1135)

Status: implemented
Schema: evidence receipt v1
Surface: opt-in `evidence_lanes` field on `perseus_vault_recall`

This contract keeps answer-facing derived facts separate from retained source
text. It is a read-only projection over existing entities, links, entity
history, transcript `source_chunk` metadata, residual spans, and provider
source metadata/event history. It adds no storage table and no MCP tool.

Related contracts:

- [Source-chunk expansion](source-chunk-expansion.md)
- [Provenance classes for derived facts](provenance-classes-derived-facts.md)
- [Evidence-backed claim cards](claim-cards.md)
- [Source anchors, corrections, and retention](source-anchors-corrections-retention.md)
- [Temporal RAG](temporal-rag.md)
- [Runtime stage traces](runtime-stage-trace-v1.md)

## 1. Request and default compatibility

`evidence_lanes` is optional and accepts an array containing one or both of:

- `derived` — an evidence-linked fact or claim whose support can be walked;
- `verbatim` — a retained source or residual character span.

The omitted field is different from an explicit empty array. Omission runs the
pre-existing recall path and does not invoke lane classification, source
recovery, evidence budgeting, or receipt generation. Its response bytes,
ordering, ranking, and side effects remain unchanged. An explicit empty list,
unknown lane, non-string entry, or non-array value fails closed at the tool
boundary.

Lane lists are canonicalized to `["derived", "verbatim"]` order and duplicate
names collapse. The caller's existing `max_tokens` is the total evidence
budget when positive; when it is unset/zero, the evidence projection uses the
bounded default of 256 tokens. Negative values and values above the evidence
maximum are rejected when evidence is requested. There is one total budget for
a union request, not an independent budget per lane.

## 2. Classification

Classification uses existing metadata only; it never infers provenance from
body wording:

| Lane | Predicate |
|---|---|
| `derived` | Entity has a support link (`supports`, `derived_from`, `evidence_for`, or `promoted_to`) or a valid `source_chunk` pointer. An `inferred` entity without a support link is not derived. |
| `verbatim` | A valid `source_chunk` pointer resolves to a retained source span, or a retained transcript/source entity is explicitly selected as a source. Residual spans are included only for an explicit verbatim request. |

A malformed or absent reference is not assigned a lane. It becomes a structured
exclusion (`malformed_reference`, `missing_provenance`, or `source_missing`).
Evidence linkage is not authority: selecting a source cannot promote an
inferred claim, change an entity's epistemic state, or overwrite a derived
fact.

## 3. Source-group identity

A source group is the stable identity of one source revision and character
span:

```text
source_id + revision + start_char + end_char + content_sha256
```

The group id is `sg-` followed by the lowercase SHA-256 of that canonical
representation. Character offsets are Unicode scalar-value offsets, never byte
offsets. The content hash may be absent only when the retained source has no
expected digest; such an item is explicitly `unchecked` and `untrusted`.

Multiple derived entities or links that resolve to the same group count once in
the answer-facing evidence set and once in receipt selected accounting. Source
group and selected-entry ordering is deterministic and independent of link or
candidate discovery order.

## 4. Governance before assembly

Every candidate is governed before it can be selected or source-expanded:

1. requesting-agent visibility (`private`, `fleet`, and workspace rules);
2. workspace scope, including global-source rules already used by recall;
3. requested transaction-time `as_of` and world-time `valid_at` anchors;
4. entity lifecycle, archival, invalidation, supersession, and corrections;
5. provider-source state and deletion/tombstone status;
6. source span bounds using character offsets;
7. expected source/provider revision and content hash verification.

The source is resolved at the requested temporal anchor before verification. A
source absent at that anchor is `source_missing`; current state must not silently
stand in for historical state. Hash mismatch returns no source text and is
reported as `hash_mismatch`. A source with no expected digest can be returned
only as `verification: "unchecked"`, `trust: "untrusted"`.

Governance exclusions are machine-readable and counted without returning raw
bodies or inaccessible spans. The stable reason vocabulary includes:

`missing_provenance`, `malformed_reference`, `source_missing`, `scope_mismatch`,
`requester_mismatch`, `archived`, `superseded`, `stale`, `tombstoned`,
`hash_mismatch`, `unverified`, `insufficient_budget`, and `unsupported_lane`.

## 5. Answer-facing response block

When `evidence_lanes` is present, recall adds an `evidence` object. Existing
recall fields remain unchanged. A representative shape is:

```json
{
  "evidence": {
    "lanes": ["derived", "verbatim"],
    "items": [
      {
        "lane": "derived",
        "entity_id": "mem-derived-1",
        "source_groups": ["sg-…"],
        "verification": "evidence_linked",
        "trust": "trusted",
        "tokens": 7
      },
      {
        "lane": "verbatim",
        "entity_id": "mem-transcript-1",
        "source": {
          "id": "mem-transcript-1",
          "category": "transcript",
          "key": "transcript-…",
          "revision": "entity-created"
        },
        "span": {"start_char": 12, "end_char": 84},
        "source_groups": ["sg-…"],
        "verification": "verified",
        "trust": "untrusted",
        "tokens": 18,
        "text": "retained source text"
      }
    ],
    "budget": {
      "max_tokens": 256,
      "selected_tokens": 25,
      "omitted_tokens": 19,
      "per_lane": []
    },
    "receipt": {
      "schema_version": 1,
      "selected": [],
      "excluded": [],
      "digest": "<64 lowercase hex characters>"
    }
  }
}
```

`text` is answer-facing only. It is never copied into the receipt or its digest
input. A returned verbatim item remains untrusted even when its expected digest
verifies: integrity proves correspondence to retained bytes, not truth or
authority.

## 6. Budget accounting

Token estimate is deterministic: `max(1, ceil(character_count / 4))`. Items are
considered in stable lane/source/entity order after governance. An item is
selected only if it fits the one total budget; otherwise it is omitted and its
estimated cost is charged to `omitted_tokens` with `insufficient_budget`.
Per-lane selected/omitted item and token totals are reported. Source recovery is
bounded by the same budget and may not bypass a prior lane's charge.

## 7. Hash-only receipt

The receipt binds the exact answer-facing evidence set after governance and
budget truncation. Its canonical digest input contains only:

- receipt schema version;
- SHA-256 of the query, never the raw query;
- canonical lane list and evidence token limit;
- workspace/requester scope identifiers and temporal anchors;
- selected entity ids, source-group ids, revisions, span hashes, lane,
  verification/trust states, and token estimates;
- normalized per-lane budget accounting; and
- sorted exclusion reason/count records.

The receipt contains no raw source text, entity bodies, prompts, arbitrary tool
payloads, credentials, or generation timestamp. Object key order and candidate
or link discovery order do not affect the digest. Changing any selected id,
source group, revision, span hash, status, lane, budget, scope, query digest, or
temporal anchor changes it. `verify` recomputes the digest and fails closed on
tampering.

## 8. Corrections, temporal state, and provider tombstones

Corrections and supersession are read through existing entity history. A
superseded or stale source is excluded rather than replaced with its current
successor. A tombstoned provider source is excluded even when a bound entity
still exists. Provider-source state is replayed from append-only events at the
transaction-time anchor; at an `as_of` anchor before the source was recorded,
no current source is disclosed. A source revision/hash mismatch never returns the candidate
text. These rules preserve the distinction between "what was believed then" and
"what is retained now".

## 9. Non-goals

This issue does not add provider calls, source promotion, authority changes,
calibrated confidence claims, new MCP tools, graph-context ablation, candidate
disposition explanations, receipt-conditioned intervention, paid benchmarks,
or automatic correction of source/entity metadata. Those belong to separate
issues or existing governed write surfaces.
