# Governed Derived and Verbatim Evidence Lanes — Implementation Plan

> **For Hermes:** Implement this plan task-by-task on a fresh branch from the verified live `main`. Keep each task reviewable and run the focused test before moving to the next task.

**Goal:** Implement issue #1135 as an opt-in, provider-free retrieval/answer-assembly contract that keeps derived facts and retained verbatim evidence distinct, joins them through governed source references, deduplicates by source group, and seals the exact answer-facing evidence set in a hash-only receipt.

**Planning snapshot:** The merged `main` tree was checked on 2026-08-25. The source-derived registry check reported 173 canonical tools and 519 compatibility entries at planning time; that number is evidence for this snapshot only. Do not copy it into the roadmap or future claims—run `python3 scripts/registry_metadata_check.py` instead.

**Issue boundary:** This plan covers #1135 only. #1140 owns candidate dispositions and selection explanations; #1143 owns matched graph-context ablation; #1136 owns receipt-conditioned intervention. Do not combine those contracts into this branch.

**Architecture:** Add a pure evidence-lane classifier/assembler over the existing entity, history, origin, source-span, provider-source, claim-card, and replay-digest primitives. The normal recall path must remain byte-compatible when the new option is omitted. When the option is present, governance runs before final lane selection and source expansion; the answer-facing result gets an additive evidence block, while the durable receipt contains only identifiers, hashes, statuses, exclusions, budgets, and scope/temporal inputs—not raw bodies, prompts, or credentials.

**Tech Stack:** Rust 2021, `serde`/`serde_json`, `rusqlite`, existing SHA-256/canonicalization helpers, SQLite body/history/source-reference channels, deterministic JSON fixtures, Cargo feature gates.

---

## Existing implementation surfaces

Use these as extension points rather than creating a second evidence store:

- `src/models.rs` — `Entity`, `RecallParams`, origin/evidence metadata, links, validity and history-facing result types.
- `src/db.rs` — candidate generation, fused recall, temporal/as-of resolution, lifecycle filtering, workspace visibility, and entity hydration.
- `src/tools.rs` — recall argument deserialization, recall/batch handlers, context/projection assembly, and JSON result shaping.
- `src/mcp.rs` — inline MCP request schemas, read-scope registration, dispatch, and registry metadata tests.
- `src/anchor_expansion.rs` — retained transcript/source-chunk span resolution and fail-closed hash verification.
- `src/extraction_loss.rs` — residual-span representation and its explicit non-default-serving behavior.
- `src/provider_source.rs` — provider-native source identity, revision, visibility, and deletion/tombstone semantics.
- `src/claim_card.rs` — evidence-linked provenance class, source/evidence references, canonical digest, and withholding rules.
- `src/stage_trace.rs` and `src/context_transform.rs` — existing hash-only, replay-oriented receipt patterns.
- `docs/specs/source-chunk-expansion.md`, `docs/specs/provenance-classes-derived-facts.md`, and `docs/specs/claim-cards.md` — contracts that #1135 must extend without contradicting.
- `scripts/registry_metadata_check.py` — authoritative registry/metadata consistency gate; #1135 should not add a tool, so the count must remain unchanged.

## Contract decisions to lock before coding

1. **Lane names are closed:** `derived` means an evidence-linked fact/claim whose support can be walked; `verbatim` means retained source text or a residual/source span. Unknown or malformed provenance is not silently assigned to either lane.
2. **Opt-in only:** add an optional recall request field (proposed name: `evidence_lanes`) accepting `derived`, `verbatim`, or both. Omitted means the pre-#1135 path, response shape, ordering, side effects, and bytes remain unchanged. An explicitly empty or unknown lane list is an error.
3. **Union is bounded:** a two-lane request is a union with one total answer-facing token budget. Report per-lane selected/omitted token totals and the reason for every budget exclusion; never let one lane exceed the total budget by applying two independent budgets.
4. **Source groups are deterministic:** normalize a source entity/revision plus span (or provider-source revision/span) into a stable source-group key. Multiple derived claims pointing to the same source group count once in the answer-facing evidence set.
5. **Evidence is not authority:** a returned verbatim item carries an explicit untrusted/verification state unless its expected source digest verifies. It cannot promote an inferred claim or overwrite a derived fact merely because it was selected.
6. **Governance precedes assembly:** apply workspace/agent visibility, as-of/valid-time, lifecycle, supersession/correction, deletion/tombstone, and source-integrity checks before an item is selected. Missing, ambiguous, stale, or malformed references are excluded with machine-readable reasons, not silently dropped.
7. **Receipts are hash-only:** the receipt binds query/config, selected entity IDs, source-group IDs, source revision/span hashes, lane/status, exclusions, token accounting, scope, and temporal anchor. It never stores raw text, prompts, credentials, or full entity bodies.
8. **No storage migration:** use existing `body_json`, `entity_history`, transcript/source entities, and provider-source metadata. If a new metadata key is needed, it is additive and defaults safely for legacy rows.

A representative opt-in response block should be structurally equivalent to:

```json
{
  "evidence": {
    "lanes": ["derived", "verbatim"],
    "items": [
      {
        "lane": "derived",
        "entity_id": "mem-derived-1",
        "source_groups": ["sg-…"],
        "verification": "evidence_linked"
      },
      {
        "lane": "verbatim",
        "source": {"category": "transcript", "key": "…"},
        "span": {"start_char": 12, "end_char": 84},
        "verification": "verified",
        "trust": "untrusted"
      }
    ],
    "budget": {"max_tokens": 256, "selected_tokens": 42, "omitted_tokens": 19},
    "receipt": {
      "schema_version": 1,
      "selected": 2,
      "excluded": [{"reason": "superseded", "count": 1}],
      "digest": "<64 lowercase hex characters>"
    }
  }
}
```

The exact Rust names and field ordering may change during Task 1, but the semantics above are part of the acceptance contract.

---

## Task 1 — Write the contract/spec and fixture schema first

**Files:**

- Add `docs/specs/derived-verbatim-evidence-lanes.md`.
- Add a minimal deterministic fixture under `tests/fixtures/` or the repository’s existing golden-fixture location after checking the local convention.

**Steps:**

1. Document lane predicates, source-group normalization, trust/verification states, governance exclusion reasons, union budget accounting, receipt digest inputs, and default compatibility.
2. Cross-link the source-chunk, provenance-class, claim-card, validity, and replay specifications.
3. Define the fixture records for: one derived fact, two claims sharing one source span, one retained verbatim span, an unverified span, a stale/superseded value, a tombstoned source, a malformed reference, a cross-workspace source, and a correction/history pair.
4. State non-goals: no provider call, no new MCP tool, no automatic source promotion, no calibration claim, no graph ablation, and no paid benchmark.

**Verification:** review the spec against issue #1135 and the existing three related specs; run `git diff --check`.

## Task 2 — Add pure typed lane and receipt models with red tests first

**Files:**

- Add `src/evidence_lanes.rs`.
- Add `mod evidence_lanes;` to `src/main.rs`.
- Add unit tests in `src/evidence_lanes.rs` or a focused `tests/evidence_lanes_contract.rs` following the repository’s integration-test import pattern.

**Steps:**

1. Define serializable, closed enums/structs for lane selection, verification/trust state, source-group identity, exclusion reason, budget accounting, selected evidence reference, and the hash-only receipt.
2. Implement strict input parsing: unknown lanes, empty explicit selections, negative/out-of-range budgets, malformed spans, and invalid digest shapes fail closed.
3. Implement canonical receipt input serialization with sorted IDs/source groups and no raw text fields. Reuse an existing canonical SHA-256 pattern where possible; do not add a crypto dependency.
4. Write tests before implementation for lane-list determinism, order-independent source groups, key-order-independent receipt input, receipt tamper detection, and raw-content exclusion from serialized receipts.

**Verification:** run the focused lane tests and `cargo fmt --check` (or `cargo fmt -- --check` if required by the repository version).

## Task 3 — Implement deterministic lane classification and source-group normalization

**Files:**

- `src/evidence_lanes.rs`.
- `src/models.rs` only if a shared, additive metadata type is required.
- Existing source/reference modules only for small visibility/helper refactors; do not duplicate source-span parsing.

**Steps:**

1. Classify derived candidates from existing origin/evidence/link metadata only when supporting references are present; inferred content without evidence remains ungrounded and is not treated as `derived`.
2. Classify retained transcript/source/provider spans as `verbatim` only when the reference contains a valid source identity and span; residual spans retain their existing explicit non-default-serving semantics until the opt-in lane asks for them.
3. Normalize source groups from stable source identity + revision/content digest + character span. Use character offsets, never byte offsets, for UTF-8 safety.
4. Return `unknown`/`unclassified` plus a reason for malformed or absent metadata instead of guessing.
5. Add tests covering API-created legacy entities, rule-based extracted facts, inferred facts with and without `derived_from`, transcript spans, provider revisions, duplicate links in different order, and multibyte text.

**Verification:** focused unit tests; confirm legacy rows classify safely without migration and default recall does not call the new classifier.

## Task 4 — Add governed source resolution using existing history and visibility paths

**Files:**

- `src/evidence_lanes.rs`.
- `src/db.rs` for a narrow read-only helper that resolves an evidence reference under the caller’s workspace/agent and as-of anchor.
- `src/anchor_expansion.rs`, `src/provider_source.rs`, or `src/claim_card.rs` only when extracting an existing private helper is necessary.

**Steps:**

1. Resolve derived support links and verbatim source chunks through the existing entity/history/source APIs rather than reading raw SQLite rows from the new module.
2. Apply the same workspace/agent authorization and visibility rules as recall/claim-card serving.
3. Apply the requested temporal anchor before verifying the source digest. A source absent at the anchor is `source_missing`, not current-state evidence.
4. Exclude archived, superseded, invalidated, deleted, tombstoned, stale, or scope-mismatched sources with explicit reason codes. Do not leak their raw bodies or spans.
5. Verify expected transcript/provider revision hashes; if no expected hash exists, return `unchecked` and keep the evidence explicitly untrusted.
6. Add tests for as-of before/after source edits, corrections, supersession, tombstones, workspace mismatch, requester mismatch, malformed spans, hash mismatch, and missing source.

**Verification:** focused governance tests plus the existing source-expansion/claim-card test filters. Confirm all new failure cases are structured exclusions, not panics or silent current-state fallbacks.

## Task 5 — Integrate the opt-in request and preserve default recall bytes

**Files:**

- `src/models.rs` (`RecallParams` and any result/request types).
- `src/tools.rs` (recall args, handler, batch path, and response projection).
- `src/mcp.rs` (the `perseus_vault_recall` input schema/dispatch metadata).
- Any compile-failing struct literals found by a repository-wide search.

**Steps:**

1. Add the optional lane-selection field with a safe omitted default. Keep the normal path short-circuited before lane classification/assembly.
2. Validate the field once at the tool boundary and pass a typed selection into the database/assembler path; do not reparse strings in each layer.
3. Ensure `perseus_vault_recall_batch`, task projections, context blocks, and other callers either pass `None` or explicitly opt in. Do not silently opt existing surfaces into raw verbatim serving.
4. Make the opt-in response additive and deterministic. Existing fields, ordering, scores, side-effect behavior, and default JSON bytes remain unchanged when the field is omitted.
5. Add schema tests for omitted, derived-only, verbatim-only, union, unknown, empty, and over-budget requests.

**Verification:** compare serialized default recall responses before/after using an existing fixture; run the focused MCP/schema tests and the registry metadata check. The registry count must remain unchanged because this issue adds no tool.

## Task 6 — Implement bounded lane-aware selection and answer-facing assembly

**Files:**

- `src/evidence_lanes.rs`.
- `src/db.rs` for candidate selection/overfetch or direct source-reference recovery before final limit/truncation.
- `src/tools.rs` for final evidence projection.

**Steps:**

1. Generate candidates using the existing selected recall mode and filters, then classify/govern them before the final answer-facing limit. Do not apply a lane filter after a too-small SQL limit and claim completeness.
2. For a derived hit whose governed source span is requested, recover the exact retained span even when a derived-only candidate would otherwise miss it; bound any source recovery by the request budget and report omissions.
3. For verbatim-only selection, serve only governed retained spans/source records, not arbitrary entity bodies mislabeled as raw evidence.
4. For union selection, merge candidates by source-group identity, retain stable tie-breaking, preserve lane labels, and charge one total token budget.
5. Keep evidence trust separate from ranking: unverified verbatim evidence can be selected but is visibly untrusted and cannot raise the derived claim’s authority.
6. Return explicit `insufficient_budget`, `source_missing`, `stale`, `superseded`, `tombstoned`, `scope_mismatch`, `unverified`, and `malformed_reference` accounting where applicable.
7. Keep side effects disabled for the evidence path; recall reinforcement remains governed by the pre-existing opt-in behavior.

**Verification:** fixture tests for derived-only, verbatim-only, union, source recovery, source-group deduplication, stable ordering, total-budget truncation, and default-path byte identity.

## Task 7 — Seal and expose the exact evidence receipt

**Files:**

- `src/evidence_lanes.rs`.
- `src/tools.rs` response projection.
- `src/stage_trace.rs` or `src/claim_card.rs` only if a shared canonical-digest helper can be safely reused without changing its existing contract.
- `docs/specs/derived-verbatim-evidence-lanes.md` for the final JSON contract.

**Steps:**

1. Populate receipt inputs from the exact selected rows/spans after governance and budget truncation—not from the pre-filter candidate pool.
2. Include request digest, lane configuration, query-time/as-of anchor, scope, selected IDs, source-group IDs, revision/span hashes, lane/verification status, per-lane counts, token totals, and sorted exclusion reasons.
3. Exclude raw source text, full entity bodies, prompts, credentials, arbitrary tool payloads, and wall-clock generation time from the digest input.
4. Test that changing any selected ID/span/revision/lane/status/budget/scope/anchor changes the digest, while JSON key order and candidate discovery order do not.
5. Test that a receipt cannot claim a row/span that was not present in the answer-facing evidence set.

**Verification:** deterministic replay tests and a negative raw-content scan over serialized receipt JSON.

## Task 8 — Complete contract fixtures and regression coverage

**Files:**

- `tests/fixtures/derived-verbatim-evidence-lanes-v1.json` (or the established fixture path).
- `tests/evidence_lanes_contract.rs` or the focused source module tests.
- Existing recall/source/claim-card tests only for compatibility assertions.

**Required cases:**

- derived-only returns linked derived facts and their governed support;
- verbatim-only returns retained spans with explicit trust/verification state;
- union returns both lanes under one budget;
- duplicate derived claims sharing a source group appear once in evidence accounting;
- derived-only source recovery finds the retained span when direct raw retrieval would miss it;
- stale, superseded, corrected, archived, tombstoned, missing, malformed, and hash-mismatched sources fail closed;
- workspace/agent visibility is preserved for both facts and sources;
- multibyte character spans are safe;
- replay is deterministic across input/link ordering and JSON key ordering;
- receipts contain no raw bodies/prompts/secrets;
- omitted `evidence_lanes` preserves the pre-feature response bytes and side effects;
- no new MCP tool or registry metadata count change occurs.

## Task 9 — Run repository gates and prepare the implementation handoff

Run from a fresh tree based on the verified live `main`, with the repository’s required Rust environment (`RUSTUP_HOME=/opt/data/home/.rustup`, `CARGO_HOME=/opt/data/home/.cargo`, `CARGO_BUILD_JOBS=4`; unset stale `OPENSSL_DIR` if present):

```text
cargo fmt --check
python3 scripts/registry_metadata_check.py
git diff --check
cargo check --locked --no-default-features --bin perseus-vault
cargo test --locked --no-default-features evidence_lanes -- --nocapture
cargo test --locked --no-default-features -- --test-threads=1
cargo test --locked -- --test-threads=1
```

If the repository’s CI matrix has an explicit grpc/multimodal/default lane, run the relevant declared checks as well; do not replace them with an ad hoc feature combination. Capture exit codes and the exact tested HEAD. Before review, verify:

- the worktree is clean except for intentional plan/spec/source changes;
- registry metadata remains consistent and no tool was added accidentally;
- default recall fixture bytes are unchanged when lanes are omitted;
- no raw evidence/prompt/credential content appears in receipts or logs;
- the issue acceptance checklist maps to named tests and spec sections;
- all claims are provider-free and no benchmark/spend gate was implicitly opened.

## Commit/PR boundary

Use a dedicated feature branch such as `feat/vault-1135-evidence-lanes` from the verified live `main`. Prefer one PR containing the contract, implementation, fixtures, and focused tests only if the changed-file set stays coherent; otherwise keep the spec/fixture contract as the first small commit and the implementation as the second. Do not add #1140, #1143, #1136, paid-provider calls, or a registry-count rewrite. Rebase before review and rerun the gates after any base advance.

**Definition of done:** #1135’s three lane modes are deterministic and governed; exact answer-facing rows/spans are receipt-bound; raw/unverified evidence never becomes authoritative by selection alone; default recall is byte-compatible; provider-free tests and repository gates are green; and the PR is reviewed against the live exact head.
