# Hostile Memory Gauntlet

The Hostile Memory Gauntlet is a provider-neutral, deterministic benchmark for
memory-system admission and retrieval boundaries. It tests whether a memory
provider preserves scope, provenance, validity, deletion, replay, and hostile
write controls without using an external LLM, answerer, judge, embeddings API,
or paid inference service.

It is deliberately separate from the answer-facing LongMemEval arm in #1164:
the Gauntlet grades memory evidence and lifecycle decisions, not generated
natural-language answers.

## What it measures

The case protocol covers:

- current versus superseded evidence;
- out-of-order valid-time writes;
- safe abstention and foreign-scope refusal;
- scope isolation;
- prompt-injection and low-trust admission;
- same-time conflict handling;
- exact and near-duplicate content floods;
- replay idempotency;
- deletion/tombstone behavior;
- provenance preservation;
- bounded result/context behavior; and
- deterministic evidence commitments.

Every answer probe names required and forbidden record IDs. Every record carries
a SHA-256 digest of its exact text, source reference, scope, actor, trust class,
valid-time interval, and recorded time. The loader rejects probes that reference
unknown records, request evidence from the wrong scope, exceed their result
limit, or declare an impossible zero-word answer budget.

## No model or network dependency

The benchmark itself is standard-library Python and deterministic provider code.
A valid run must declare `network_calls: 0` and `offline: true`. The Gauntlet
never calls an answer model, judge, embedding service, or paid inference API.

## Provider contract

A participant supplies an adapter implementing:

```text
reset() -> None
ingest(record: MemoryRecord) -> AdmissionReceipt
forget(scope, record_id) -> MutationReceipt
retrieve(query, scope, as_of, limit) -> RetrievalResult
```

The adapter is part of the provider boundary. It must translate its backend's
native API into these semantics rather than silently dropping them:

1. **Record identity:** each record version is addressable by `(scope,
   memory_key, record_id)`; replaying the same record ID is not a new write.
2. **Scoped deduplication:** identical content may be a duplicate within one
   scope, but must not be rejected merely because it exists in another scope.
3. **Supersession:** `record.supersedes` must close the older version's
   half-open valid-time interval. A late older write must not resurrect stale
   evidence at a later `as_of`.
4. **Admission:** hostile, low-trust, conflicting, or duplicate writes must
   return `quarantined`/`rejected` with a reason code and must not become
   serveable evidence.
5. **Retrieval:** results must be scope-filtered, validity-filtered, bounded by
   `limit` and `max_context_words`, and retain non-empty provenance fields.
6. **Failure behavior:** missing capabilities, malformed backend responses, and
   cleanup failures are explicit errors/blocked outcomes, never synthetic
   passes.

A backend may use a real database, but it must provide bounded cleanup and
non-sensitive execution metadata. The public report may contain only bounded
IDs/categories/statuses, reason codes, counts, metrics, provider metadata, and
SHA-256 commitments.

## Public control fixture

The repository includes a small synthetic control suite. It is not the private
holdout and cannot establish a general product claim by itself.

Regenerate the committed public fixture deterministically:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  python3 benchmark/hostile_memory_gauntlet/fixtures/build_public_fixtures.py
```

Run the known-good and known-bad controls:

```bash
python3 benchmark/hostile_memory_gauntlet/run.py run \
  --provider benchmark.hostile_memory_gauntlet.gauntlet.providers:ReferenceProvider \
  --manifest benchmark/hostile_memory_gauntlet/fixtures/public_manifest.json \
  --out /tmp/gauntlet-reference-run.json \
  --acceptance-out /tmp/gauntlet-reference-acceptance.json \
  --run-id public-control-v2

python3 benchmark/hostile_memory_gauntlet/run.py run \
  --provider benchmark.hostile_memory_gauntlet.gauntlet.providers:NaiveProvider \
  --manifest benchmark/hostile_memory_gauntlet/fixtures/public_manifest.json \
  --out /tmp/gauntlet-naive-run.json \
  --acceptance-out /tmp/gauntlet-naive-acceptance.json \
  --run-id public-naive-v2
```

The reference control is expected to pass all **14 cases / 15 probes** and be
`release_ready: true`. The naive control is expected to complete as valid
negative evidence but fail the release gate.

## Tests

From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s benchmark/hostile_memory_gauntlet/tests \
  -t . -v
```

The focused package suite contains **44 tests** covering protocol validation,
public projection, evaluator/lifecycle behavior, reference/negative controls,
and the real Vault adapter boundary.

## Private holdout runs

A private case bundle must conform to `cases/v1`, and its manifest must bind the
exact case-file bytes:

```json
{
  "schema": "perseus-hostile-memory-gauntlet/manifest/v1",
  "suite_id": "custodian-suite-v1",
  "case_file": "private_cases.json",
  "case_file_sha256": "<sha256 of the exact case-file bytes>",
  "case_ids": ["..."],
  "required_categories": ["..."],
  "config": {"max_cases": 30}
}
```

The CLI resolves a relative `case_file` beside the manifest unless `--cases` is
supplied. It refuses a hash mismatch and refuses to overwrite existing output
artifacts unless `--force` is explicitly supplied for a freshly created empty
output directory.

Private cases, raw prompts/queries, provider responses, credentials, and raw
stdout/stderr stay outside Git and outside public reports. For release claims,
the holdout must be independently provisioned or reviewed by the benchmark
custodian; a locally authored diagnostic bundle is not an independent blind
split.

Run and independently recheck a provider:

```bash
python3 benchmark/hostile_memory_gauntlet/run.py run \
  --provider <module>:<ProviderClass> \
  --manifest /private/path/private_manifest.json \
  --cases /private/path/private_cases.json \
  --out /private/run/run-return.json \
  --acceptance-out /private/run/acceptance.json \
  --run-id private-v1

python3 benchmark/hostile_memory_gauntlet/run.py accept \
  --manifest /private/path/private_manifest.json \
  --cases /private/path/private_cases.json \
  --run-return /private/run/run-return.json \
  --out /private/run/acceptance-recheck.json
```

A complete failed run can be accepted as evidence while remaining
`release_ready: false`. `acceptance_status: accepted` means the artifact is
structurally valid and hash-bound; it does not mean the provider passed.

## Real Vault adapter

`gauntlet/perseus_mcp.py` is the reference integration for a checkout-built
`perseus-vault` binary over MCP stdio. It:

- uses a fresh temporary SQLite database per case;
- negotiates `initialize` and `tools/list`;
- configures an isolated authority manifest;
- signs admission-source journal envelopes;
- translates record versions to record-level Vault keys;
- enforces duplicate/replay and hostile-admission dispositions at the adapter
  boundary;
- uses `perseus_vault_valid_at` for provenance-bearing historical reads; and
- owns process-group and DB/WAL/SHM cleanup.

Set `PERSEUS_GAUNTLET_BINARY` to an absolute checkout-built binary and never
point this adapter at a production database. The adapter reports
`real_producer`, `offline`, `network_calls`, and the binary SHA-256 in
`provider_metadata`.

## Publication boundary

Before publishing a result, verify the exact checkout/binary identity, package
test count, manifest/case binding, complete case/probe denominators, finite
metrics, independent acceptance recheck, stable evidence across repeat runs,
recursive forbidden-field scanning, and zero child-process/database residue.
Do not publish a score from an incomplete, malformed, locally unblinded, or
structurally accepted-but-failed run as a general performance claim.
