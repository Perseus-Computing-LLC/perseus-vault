# AMR 0.1 export and conformance lane

This directory contains a provider-free interoperability profile for the existing
Vault claim-card/evidence projection. It does not add a storage backend, change
Vault authority, or make a factuality, authenticity, security, or customer-efficacy
claim.

## Pinned reference

The lane mirrors the AMR 0.1 draft and its conformance vector set at:

- repository: `https://github.com/phasespace-labs/auditable-memory-records`
- revision: `2b44803b4bba15bc47f5590e24a47fd09e8ef66f`
- vector files and upstream Git blob IDs:
  - `normalize.yaml`: `95feec95a698ea358dab9703073a43c44fc09632`
  - `level1-marked.yaml`: `8f6bd76dacc5a9e5c248e4c4f69f5d23bda70a77`
  - `level2-linked.yaml`: `4e8d0f9d6a6c64f3863a1da1b42bf0c69d14dae3`
  - `level3-cited.yaml`: `5ae86986c64d19d3f7f13b6cf165208c257e11f5`

`fixtures/conformance_vectors.json` is a no-network JSON mirror of the pinned
vector cases. It enforces the complete upstream case-ID sets (10 normalization,
15 marked, 12 linked, and 16 cited cases) plus one local empty-citation negative
case. It stores case IDs and expected behavior, not raw model prompts, provider
output, customer data, or production records.

## Profile

`profile.py` exposes:

- `export_claim_card(card)`: maps one sanitized Vault claim-card/evidence projection
to an `auditable_memory: "0.1"` record;
- `validate_record(record)`: rejects malformed refs, closed-vocabulary violations,
  unsupported hash algorithms, malformed claims/spans, unsafe extensions, and
  inconsistent loss reports without repairing input;
- `validate_cited_record(record)`: applies the Level 3 citation gate and rejects
  records without both source and claim evidence;
- `verify_record(record, sources)`: returns `ok`, `anchor_tampered`,
`source_drifted`, or `source_missing`, with a `partial` flag when a quote hash is
absent;
- `import_record(record)`: validates an imported record and returns explicit
`authoritative: false` and `promotion_required: true` metadata;
- `InMemoryAMRStore`: a fixture-only relation store. `contradicts` remains a
queryable, non-resolving link and does not hide either record.

Vault-only fields are retained under `extensions.vault`, including:

- claim-card version, category, key, entity type, provenance, and original
Vault epistemic state;
- valid time and transaction time;
- scope and authority metadata;
- supersession, quarantine, revocation, archive, and tombstone state;
- original typed links and evidence entity IDs;
- verified/support-count claim-card fields and otherwise-unmapped nested time/state
  fields, which are retained under the Vault extension rather than silently dropped.

The export also emits `loss_report`. Unknown non-sensitive card fields are named
there without copying their values. A caller-declared `lossy_required_fields`
list fails closed instead of producing a misleading complete record. Raw bodies, secrets, credentials, prompts, benchmark metadata, inferred links, and
provider/model/judge fields are rejected recursively at the card and extension
boundaries. Unknown nested evidence/span/link fields fail closed instead of being
silently discarded.

AMR's four epistemic values are closed: `fact`, `inference`, `open_question`, and
`unverified`. An absent value remains absent and is never promoted to `fact`.
Vault values such as `candidate` are mapped explicitly to `unverified` while the
original value remains in the Vault extension. Unsupported values fail closed.

Exported quote hashes are algorithm-prefixed SHA-256 values. The verifier also
accepts the AMR draft's compatibility form for bare 32-character MD5 and
64-character SHA-256 digests. Unknown algorithms and other digest lengths fail
closed. Quote normalization folds the AMR punctuation table, collapses whitespace,
and strips the ends before hashing or source matching.

## Run the offline lane

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  python3 -m benchmark.amr.run \
  --outdir /tmp/perseus-vault-amr
```

The command writes a manifest, sanitized conformance report, signature file, and
artifact inventory. It makes no provider, model, judge, network, or production
database calls. Repeating it with the same checkout produces byte-identical
artifacts.

The lane covers the AMR `marked`, `linked`, and `cited` levels for the synthetic
records in this repository. The runner independently exercises the discoverability,
inferred-link rejection, empty-citation rejection, legacy-hash interpretation, and
source-verifiability behaviors; it does not turn fixture `expect` flags into passes.
Level 3 is limited to records for which the verifier has the cited source text. AMR
hashes provide integrity checking, not signatures or authority. Imported and
inferred records never become authoritative merely by carrying an AMR declaration.
