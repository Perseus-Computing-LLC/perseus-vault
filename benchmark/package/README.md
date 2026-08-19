# Perseus Vault benchmark package

This package is the shared contract layer for the Vault benchmark portfolio. It
is deliberately **not** a composite score. Each specialized suite publishes an
orthogonal scorecard for contract quality, retrieval, correction, deletion,
freshness, economics, security, or downstream utility.

## Common artifact contract

`control_profile.schema.json`, `report.schema.json`, and
`common/artifacts.py` define:

- canonical JSON hashing;
- control-profile digesting;
- semantic result signatures over verdicts and metric outcomes;
- run fingerprints bound to binary, dataset, profile, and harness commit;
- deterministic JSON output.

Runtime evidence is excluded from semantic signatures. The artifact helpers
also enforce the publishable boundary: top-level, case, metric, capability, and
claim fields are allow-listed; evidence is hash-only; raw-input flags must be
false; identifiers and reasons are bounded and private-looking labels are
rejected; and non-finite values are rejected.

The JSON Schema is a shape contract. `validate_report()` is the mandatory
semantic/publication gate and must run before writing or publishing an artifact.
Specialized suites may additionally use the stricter sanitizer in
`benchmark/quality` before constructing a report.

## Retrieval replay envelope

`retrieval_replay.schema.json` and `common/replay.py` define the shared
`perseus-vault-retrieval-replay/v1` contract used by BEAM, LongMemEval
retrieval diagnostics, and the generic recall lane. The envelope is a
provider-free, hash-only projection of one retrieval cell:

- candidate identity, source/provenance reference, and content are SHA-256
  commitments; raw query text, memory bodies, prompts, and provider payloads
  are never emitted;
- `wire_rank` preserves the producer's original position while `final_rank`
  records the delivered position after an explicit sequence-policy overlay;
- optional scores remain optional and retain their declared semantics—missing
  scores are not synthesized as `0.0`;
- membership records requested top-k, candidate/delivered counts, completeness,
  and truncation; `complete`, `partial`, `empty`, `unavailable`, and `degraded`
  are distinct states;
- a hash-only synthetic snapshot sidecar allows a second process to replay
  membership/order and verify the envelope/projection fingerprints without
  gold labels or production bodies.

Producers write `retrieval_replay.jsonl` and an aligned
`retrieval_snapshot.jsonl`. The BEAM fixture tests consume both sidecars as a
real replay check; LongMemEval `retrieval_diag.py` and `recall/run.py` emit the
same versioned envelope. The replay builder rejects unknown fields, malformed
ranks/digests, raw payload keys, inconsistent state, incomplete top-k marked as
complete, and tampered snapshots or projections.

## Zero-model corpus certification (#1118)

`common/corpus.py` provides the provider-free packaging boundary for agent-visible
benchmark inputs. `materialize_git_tree()` archives a pinned Git object into a
fresh Git-less tree and excludes `.git`, build output, and dependency/cache
roots. `redact_tree()` is a separate deterministic operation: it removes known
auto-loaded project-context paths and emits the removed relative paths with
pre-redaction SHA-256 commitments. Certification never calls redaction or
mutates its candidate.

`certify_surfaces()` requires all five surfaces—materialized source, fixture,
evidence, graph identity, and challenge text—and emits only surface digests,
bounded counts, finding-class counts, and a receipt digest. It detects
`auto-loaded-context`, `suspicious-metadata`, `benchmark-awareness`,
`patch-or-diff`, and `solution-leak`. The word `fixture` is checked in
benchmark-owned identity fields, but not merely because it appears in a graph
node body. Missing artifacts fail closed; the explicit
`PERSEUS_VAULT_ALLOW_UNCHECKED_CORPUS=1` opt-out produces `status=unchecked`,
never a pass.

Schemas:

- `corpus_certification.schema.json`
- `corpus_redaction.schema.json`

All tests are offline and model/provider-free. Public receipts never contain
prompts, memory bodies, host paths, credentials, or scanner text.

## Synthetic GovCon handling profiles (#1127)

`common/handling_profile.py` composes with the #1118 corpus boundary and
provides a versioned, provider-free synthetic handling-profile fixture. The
labels are test-policy classes only; they are not legal CUI, export-control, or
contract-information determinations.

The fixture covers `PUBLIC_SAFE`, `INTERNAL_PROGRAM`, `FCI_LIKE`, `CUI_LIKE`,
`EXPORT_CONTROLLED_SIGNAL`, `CREDENTIAL`, and `REVIEW_REQUIRED`, with signals
placed independently in content, title, safe summary, Core tags, project/task/
topic metadata, source references, contract identifiers, and program
identifiers. It exercises the complete deterministic projection boundary:

1. classify the combined candidate projection;
2. apply local synthetic-marker redaction only when the case policy permits it;
3. reclassify the redacted projection;
4. route it to `SAVE/AGENT_VISIBLE`, `PROTECTED`, `BLOCK`, `PENDING_REVIEW`, or
   `REVIEW_REQUIRED`.

The receipt publishes case IDs, expected/classified/actual profile labels,
expected/actual outcomes, bounded reason codes, policy/taxonomy versions, and
SHA-256 commitments for candidate, redaction, projection, and protected storage.
It reports false-negative, false-positive, mismatch, and missingness counts by
profile and proves the synthetic workspace/visibility isolation invariant.
Raw case text, credentials, CUI-like markers, and customer data never enter the
receipt. The default lane declares zero model calls, zero provider calls, zero
network calls, and `raw_inputs_captured=false`.

Files:

- `handling_profile_fixture.json` — synthetic corpus only;
- `handling_profile_corpus.schema.json` — corpus contract;
- `handling_profile_report.schema.json` — hash-only receipt contract;
- `common/handling_profile.py` — validator/classifier/redaction/report logic;
- `test_handling_profile.py` — deterministic and adversarial contract tests.

This is product evidence for a bounded projection experiment. It does not
establish CMMC Level 2, NIST SP 800-171 compliance, FIPS validation, ATO,
IL5/IL6 authorization, ITAR/EAR compliance, or any customer/prime data-handling
determination.

## Status vocabulary

`available`, `partial`, `unavailable`, `not_measured`, and `failed` are distinct.
A missing capability or unmeasured metric is never silently converted to a
passing zero or one. A specialized suite may be blocked while another suite
remains runnable.

## Suite inventory

- `quality/`: deterministic contract and safety gate;
- `recall/` and `longmemeval/`: retrieval and optional pinned QA;
- `correction/`: A/B/C/C'/D/E contradiction durability matrix;
- `deletion/`: re-ingestion, background-job, derived-store, and propagation protocol;
- `freshness/`: write-to-readable lag and failure/concurrency stress;
- `economics/`: storage, token, and optional cost overlays;
- `security/`: deterministic authority gold traces (accept/reject/failed-to-confirm/blocked) with negative assertions;
- `scale/` and `beam/`: latency, throughput, and corpus-size overlays;
- `context_selection/`: provenance-preserving context selection without a model judge;
- `agent_tasks/`: planned deterministic downstream task utility.

## Verification

From the repository root:

```bash
python3 -m unittest discover -s benchmark/package -p 'test_*.py' -v
python3 -m unittest discover -s benchmark/quality -p 'test_*.py' -v
python3 -m unittest discover -s benchmark/correction -p 'test_*.py' -v
python3 -m unittest discover -s benchmark/freshness -p 'test_*.py' -v
python3 -m unittest discover -s benchmark/economics -p 'test_*.py' -v
```

Paid LLM runs and fleet-scale runs are opt-in. Their reports must include the
full control profile, exact answerer/judge configuration, complete denominators,
and a negative-claim section before any result is used externally.

## Compatibility

Product legacy MCP tool names are not renamed as part of this benchmark work.
The package drives the real `perseus-vault` binary over its existing interfaces.
The existing `benchmark/quality` scorecard remains the blocking release gate
until a later, explicitly reviewed migration.

## Verified current slice

- `quality/`: v1 deterministic contract gate, 30 cases / 41 checks, executed
  against the release binary and passing; scorecard `release_ready`.
- `correction/`: five-shape A/B/C/C'/D/E durability matrix, 35 checks, executed
  against the release binary and passing.
- `deletion/`: logical-forget and permanent-purge vertical slice, 18 checks,
  executed against the release binary and passing. Its README names the
  unimplemented external-copy probes explicitly.
- `freshness/`: healthy write-to-readable lag, explicit outcome states,
  deadline, and restart checks, 19 checks, executed against the release binary
  and passing. Provider-failure injection remains a separate unmeasured lane.
- `economics/`: storage/token/cost helper layer, 3 focused tests passing; not
  yet integrated into canonical scale/BEAM reports.

The current implementation is ready for review as an offline benchmark package.
The remaining intentionally unmeasured lanes are recorded in
`../claim_register.json`; they are not silently treated as passes.
