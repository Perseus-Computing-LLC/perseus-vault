# Changelog

All notable changes to Perseus Vault are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Governed rejection propagation through maintenance.** Consolidation,
  dream, and cross-workspace promotion now exclude rejected source values
  before clustering/prompt assembly, and public derived reads follow source
  lineage so a previously materialized observation cannot be refreshed or
  served after its source is rejected. Added an end-to-end rejection canary.
- **Federation and release metadata drift.** The disabled peer-federation
  handler is no longer advertised as a local transfer feature; public registry
  counts now reflect 169 canonical tools, and `server.json` pins the OCI
  identifier to the declared `2.23.0` version with CI validation.
- **Windows/macOS CI fixture flakes (SQLITE_BUSY "database is locked", #1031
  follow-up).** Test fixtures now default to a 30s `busy_timeout` on
  Windows/macOS (explicit `PERSEUS_VAULT_BUSY_TIMEOUT_MS` still wins;
  production default unchanged at 5000ms), raw `schema` fixture connections
  get the same headroom, and the dedup signature property test moved to the
  WAL fixture — under DELETE journaling its concurrent raw-conn reads +
  read-decide-write remembers starved the writer past busy_timeout (same
  class as #379/#400/#971).

### Added
- **Explicit rekey and strict shared-deployment contracts.** Added an
  end-to-end `init --rekey` migration/idempotence test and the opt-in
  `PERSEUS_VAULT_STRICT_SCOPE=1` MCP boundary, which requires transport
  identity, non-empty workspace scope, and an active exact workspace binding.
- **Keyed-MAC audit-chain verification hardening (#1067).** `verify-audit-chain`
  now accepts `--encryption-key` so operators can verify a keyed (HMAC-SHA256)
  chain, and verification fails closed with an explicit "chain is keyed" error
  when the key is absent (RFC §3.5 of `docs/audit-chain-keyed-mac-design.md`).
  New keyed-mode regression suite: unkeyed→keyed transition + canary-gated
  idempotent rekey, content-tamper and recompute-attacker detection, wrong-key
  fail-fast, keyed redaction, and v14→v15 payload-commitment backfill. The
  CMMC/NIST matrix rows 3.3.1 / 3.3.8 / 3.13.10 move PARTIAL → SERVED (gap G1
  closed).
- **Seal-style tamper evidence for persisted state and exports (#1060).**
- **Model-upgrade inheritance receipts (#1066).** Identity/vessel split
  (arXiv:2603.04740): `subject_identities` + `model_incarnations` tables
  (schema bump) keep a stable subject id separate from any
  model/provider/session id. `perseus_vault_model_inheritance`
  (registry +1) records policy-gated inheritance receipts (source-state
  hash, old/new model identities, compatibility report, approver-stamped
  approval), queryable in the provenance graph; `depart` is a governed
  transition preserving tombstones; `replay` samples representative
  memories as hash-only digests. Memory survives the model — and now the
  handoff is auditable. Docs: `docs/model-upgrade-inheritance.md`.
- **Typed provenance edges + evidence/execution split (#1064).**
- **Intent-aware typed-relational traversal (#1065).** MAGMA-pattern
  relation views over the existing graph: `temporal` (valid-time ordering),
  `causal` (depends_on/causes/updates/invalidates edges), `entity`
  (mention/identity neighborhood), `semantic` (ranked similarity) — routed
  by the deterministic query-shape classifier (identical query → identical
  route, no LLM). `perseus_vault_typed_traversal` returns the explainable
  selected path plus rejected distractors with reasons, with per-run token
  accounting; `perseus_vault_traversal_ablation` (registry 154→156) reports
  per-view cost/benefit so each view's token cost is auditable. Schema v53. The
  2606.04990 relation vocabulary lands on the link graph: `supports` /
  `contradicts` / `invalidates` / `updates` / `authorized_by` kinds with
  per-kind write-boundary validation (authorized_by targets must be
  authority manifests or action receipts; invalidates requires a live
  target), legacy relations classify deterministically at read time, and
  `perseus_vault_provenance_projection` separates the evidence graph from
  the execution trace (journal events + blocked-action receipts — intent +
  failure receipt extended into the graph). `perseus_vault_param_lineage`
  (registry 152→154) adds parameter-level lineage for high-risk tool
  arguments with dangling-source detection. Schema v52.
  New `memory_seals` table (schema v51) records SHA-256 commitments at
  seal/export time — hash + label only, never content. Compare-on-recall
  journals a `tamper_evidence` event naming any entity whose served bytes no
  longer match its seal (never served silently); `perseus_vault_seal` seals a
  live entity, `perseus_vault_tamper_scan` verifies every entity + export seal
  (registry 150→152). `perseus_vault_vault_export` now writes a `.seals.json`
  manifest (per-file hashes) sealed into the chain. Integrity ≠ truth by
  design: a seal proves unchanged-since-sealed, never true-when-written.
  Docs: `docs/seals-tamper-evidence.md`.
- **Admission quarantine disposition (#1026).**  disposition: candidates disposed as `quarantined` by trust admission are
  sealed OUTSIDE the authoritative head in a new `admission_quarantine`
  table (schema v44; body encrypted like entities, hash-only receipt, linked
  to the admission decision digest) — storage presence confers no authority,
  no read surface serves them, and only `perseus_vault_admission_quarantine`
  (list/show/retire/purge; the `admission_quarantine` lane of
  `perseus_vault_operator_review`) reaches them. Optional `proposal_id` on
  remember: a proposal disposed as quarantined stays RETIRED until purged —
  reuse is refused with a stable `RetiredIdentifier` error. Journaled
  admission_quarantined / admission_quarantine_retired /
  admission_quarantine_purged. Tests cover all four terminal dispositions
  (Commit / Reject / Quarantine / Defer) plus the retire→purge lifecycle.
- **Epoch-fenced writer handoff (#1027).** Per-workspace `writer_directory`
  (schema v45) with the Prepare → Fence → Retarget → Activate lifecycle,
  monotonically advanced epochs, and signed lifecycle receipts
  (`perseus_vault_writer_handoff`). Fence CLEARS the writer: the
  Fence→Activate gap leaves ZERO writers (fail-closed, never two), and a
  stale writer's in-flight write fails with a stable StaleRevision /
  WriterEpoch reason. Writes (`remember`) against an active directory must
  present the current `writer_epoch`; absent directory = unfenced legacy
  posture. Restore (#1028) honors the same fence.
- **Forward restoration (#1028).** `perseus_vault_restore_forward`: a
  checkpoint restore is a typed, forward-only transition — each candidate is
  applied as an audited VERSION ADVANCE of the current head (the
  pre-restore version moves to `entity_history` — the parent; a rollback is
  just another forward migration), never a rewrite or truncation. Protected
  authority paths P = {authority, policy, revocation, issuer, writer, epoch,
  dirSeq, lifecycle, createdFrom, provenance} always take CURRENT values
  (mask ∩ P = ∅ enforced fail-closed: `RestoreMaskProtected`), so a restore
  can never revive a stale credential, resurrect a superseded authority, or
  undo a recorded external effect (authorized actions untouched).
- **Supersession impact index (#1029).** Reverse `derived_from` closure +
  action justifications: `perseus_vault_impact_report` enumerates the
  decisions/actions that cited a changed fact, flags PENDING actions
  (`intent`/`approval_requested`) for AAR justification re-validation, and
  lists COMPLETED actions for review (never auto-reversed). Bounded by
  depth/age caps; `as_of_unix_ms` computes the report at a past transaction
  instant. `action_intent` gained `justification_entity_ids` (schema v46,
  fail-closed on unknown ids). Design:
  `docs/specs/supersession-impact-index.md`.
- **Hybrid recall graceful degradation (#1030).** A hybrid recall whose
  dense leg is unavailable (embedding tier off, no backend, no fingerprint
  tier) now DEGRADES to the sparse arm with an explicit marker
  (`RecallCompleteness.degraded`, outcome `status: partial` /
  `partial_arms`) instead of failing the whole recall — integrations on lean
  binaries get keyword hits. Dense mode keeps its hard error (#226).
- **Deterministic fingerprint tier — zero-API fallback semantic hashing
  (#1020).** Opt-in `PERSEUS_VAULT_EMBEDDING_FINGERPRINT=on|off` /
  `--embedding-fingerprint <on|off>` (default off): content-changing writes
  store a deterministic subword-HDC fingerprint of the plaintext body
  (Hillock-mirror: char 3–5-grams superposed into 10k bipolar sign bits,
  FNV-1a-seeded splitmix64, 1,250 bytes/entity, schema v43) and dense/hybrid
  recall falls back to popcount/Hamming ranking over those fingerprints when
  the embedding backend is unavailable — never primary while dense
  embeddings exist, and the #226 error contract is preserved while the tier
  is off. The fallback keeps the dense arm's governance suppression,
  bounded-scan ceiling, and completeness semantics, reports the arm as
  `fingerprint` in recall arm audits, and gains a `fingerprint_tier` stage
  in the config self-report. Spec: `docs/specs/fingerprint-tier.md`.
- **Active-decision anchor query expansion (#1009).** Opt-in
  `anchor_expansion` on recall: keystones (#683) + ACTIVE decision entities
  (un-superseded via the #363/#472 chain — a structural fact, not an LLM
  label) become extra lexical FTS5 queries after fusion; matching candidates
  get a capped boost (1.15/match, 1.5 cumulative) and are recorded in
  `FusedTrace.anchor_matched`. Workspace-scoped, read-only, default OFF
  (#247 byte-identical default recalls). Spec:
  `docs/specs/anchor-expansion.md`.
- **Sleep-cycle consolidation, proposal-only (#1002).** New
  `perseus_vault_sleep` MCP tool (registry 136→137): a bounded, LLM-free
  pass over a category that PROPOSES, never silently changes — merge
  proposals (trigram similarity ≥ threshold), negation-shaped conflict
  proposals (token overlap + negation word, classified BEFORE similarity so
  "X works" vs "X does not work" is never a merge), and optional delegated
  cold_first compression via consolidate (the only auto-committed phase;
  verified/scored exempt). Proposals persist under `sleep_proposal.*` state
  keys and surface as the new `sleep` lane of
  `perseus_vault_operator_review`. #952-bounded (maintenance gate, scan +
  proposal budgets), dry_run performs identical work with zero writes.
  Spec: `docs/specs/sleep-consolidation.md`.
- **Per-type retrieval context budgets (#1008).** Opt-in `budget_profile`
  on recall (diverse | fact_lookup | broad): floors guarantee decision and
  constraint classes a share of the fused result (pulled from below the
  caller limit); caps stop any one typed class from crowding the rest out;
  the #942 token budget still composes on top. Allocation is reported in
  `fused_trace.truncation.per_type` (never silent); unknown profiles are a
  hard error; default None keeps recalls byte-identical (#247). Spec:
  `docs/specs/per-type-context-budgets.md`.
- **Multi-hop retrieval arm: graph traversal + entity coverage (#1003).**
  Opt-in `multihop` on recall (default OFF, #247): hop-expands from the top
  fused anchors via the entity link graph (existing `graph_expand`, #869
  gates intact, 0.8^hop discount), then selects the final set by greedy
  ENTITY COVERAGE — the stopword-filtered significant query tokens, set-cover
  order within the caller limit + #942 token budget (CogniCore
  MultiHopMemoryBackend borrow). Trace in `fused_trace.multihop`
  (hop_expanded, expanded_ids, selection_order, covered/uncovered entities).
  Deterministic and bounded (3 anchors, 1 hop, 20 neighbors, 12 entities).
  Spec: `docs/specs/multihop-retrieval.md`. No registry change.
- **Config self-report with requested-vs-resolved diff (#1010).** New
  `perseus_vault_config_report` MCP tool (registry 134→135): per-stage
  provider/config self-report — embedding_backend, model_backend,
  quantization, db_path, encryption, network — each with `requested` (the
  operator knob as literally given, sanitized), `resolved` (runtime truth),
  `drifted` (loud flag when they differ), and a remediation `note`.
  Quantization reads the live store record (not the open cache), so a store
  declared by a previous process drifts loudly against this process's
  default. Startup prints one line per stage; drift lines are prefixed
  `CONFIG DRIFT`. Docs: `docs/specs/config-self-report.md`.
- **Typed memory classes with per-type policies (#1000).** New
  `perseus_vault_type_policies` MCP tool (registry 135→136): 8 CogniCore-
  borrowed MemoryTypes (semantic, episodic, procedural, preference,
  constraint, failure, reflection, knowledge) with per-type
  `decay_multiplier` (scales the #941 category half-life at decay tick) and
  `retrieval_weight` (multiplies the final fused recall score). Schema v41
  adds `entities.memory_type` (additive; legacy rows = '' = SEMANTIC
  policy, byte-compatible). `remember` validates `memory_type` fail-closed
  (unknown = hard write error, never a silent fallback); recall gains
  `type_filter` (all modes, validated at the public entry). Spec:
  `docs/specs/typed-memory-classes.md`.
- **Utility-driven lifecycle promotion (#1001).** Schema v42 adds
  `entities.utility_score` (saturating cap 100): +1 per reinforced recall
  hit, +5 per citation — default recalls stay pure reads (#247 preserved).
  A pure transition function auto-promotes `candidate → verified` at
  utility >= 10 with >= 1 citation, CAS-guarded against concurrent explicit
  promotion, and journals every transition as `auto_promotion` (the
  evidence surface — no silent mutation). Spec:
  `docs/specs/utility-driven-promotion.md`.
- **Scheduled recall evaluation with regression alerts (#930).** New
  `perseus-vault eval` CLI (record | history | alerts) and
  `perseus_vault_eval_history` MCP tool (registry 122→123): durable
  `eval_runs` history (schema v38) for nightly-curation + midday-eval
  cadences, deterministic floor/cap + trailing-mean regression detection
  with per-metric thresholds, and an `eval_regressions` lane in
  `perseus_vault_operator_review`. The nightly `maintain` after-action
  summary attaches to the eval record. Docs: `docs/scheduled-recall-eval.md`
  (ops + cron pattern), `docs/specs/scheduled-recall-eval.md` (design spec).
- **Live-web gap-fill, opt-in (#929).** New `perseus_vault_web_gap_fill`
  MCP tool (registry 122→123): the agent fetches (the Vault never makes
  network calls — no SSRF surface), then reports grounded content + source
  URLs for validated, audited storage as unverified-until-confirmed. Gated
  by `PERSEUS_VAULT_WEB_GAP_FILL_ENABLED=1`; per-workspace source allowlist
  (`PERSEUS_VAULT_WEB_ALLOWLIST`), http/https only, private/reserved literal
  IPs refused, fail-closed secret scan, relevance floor, per-workspace rate
  limit. Enabled empty recalls emit a `gap` signal (never an implicit
  fallback). Docs: `docs/web-gap-fill.md`.
- **Court-of-record governance (#940).** New `perseus_vault_consistency_audit`
  (read-only: contradiction pairs + deterministic winner recommendation via
  the importance → source-authority → recency → id ladder, already-ruled
  pairs, supersession lag, keystone-pending count) and
  `perseus_vault_audit_ruling` (idempotent accept/override/reverse; accept
  and override compile the winner into the existing supersede guard — link,
  valid-period close, status deprecation — and record the ruling +
  `supersede_receipt`; reverse reopens a pair for re-litigation). New
  `court_rulings` table (schema v38, bounded id/digest projection), every
  ruling journaled (`court_ruling_set` / `court_ruling_reversed`). Registry
  122→124. Docs: `docs/specs/court-of-record.md` (design),
  `docs/court-of-record.md` (ops).

### Fixed
- **Pre-rebrand encrypted vaults unlock after upgrade (#1018).** The rebrand
  changed the encryption-canary AAD category (`mimir_internal` →
  `perseus_vault_internal`) with no read fallback, so v2.23.0 rejected
  v2.21-era encrypted vaults even with the correct key ("failed to decrypt
  encryption canary"). `verify_or_init_canary` now falls back to the
  pre-rebrand AAD (the same key opens; a wrong key still fails loudly), and
  `rekey-aad` migrates the canary onto the current AAD as the deliberate,
  recoverable upgrade step (new `canary_migrated` tuple field). Restored #427
  key-path precedence: the default key resolves `~/.perseus-vault/secret.key`,
  else a legacy `~/.mimir/secret.key`, so upgraded installs never lose their
  key; `init` reuses an existing key instead of overwriting it, and `keygen`
  refuses to overwrite. End-to-end upgrade regression test drives the real
  binary: v2.21-era fixture (legacy canary AAD + legacy key path) →
  serve startup and recall succeed.

## [2.23.0] - 2026-08-10

### Fixed
- **Registry metadata consistency.** `server.json` description shortened under
  the 100-character official-registry limit (the previous description was
  rejected with 422 on publish, leaving the live entry stale at "55 tools");
  the count is now synced to 122 canonical tools across README, server.json,
  manifest.json, glama.json, and the GitHub repository description.

### Added
- **Hermes native MemoryProvider integration.** Originally contributed by
  [@sowerkoku](https://github.com/sowerkoku) in
  [#908](https://github.com/Perseus-Computing-LLC/perseus-vault/pull/908),
  overhauled in [#911](https://github.com/Perseus-Computing-LLC/perseus-vault/pull/911).
- **Deployment profiles (#870).** New `perseus_vault_deployment_profile` tool
  (registry 102→103): one machine-readable, runtime-derived answer to "what
  is this vault actually connected to?" — `offline` | `local_only` |
  `local_with_approved_network` | `external_actions_enabled`, with
  model/embedding backends (degraded reported explicitly, never silently
  reclassified as empty success), network listeners + sanitized egress hosts,
  connectors, cloud-provider use, external-mutation posture, encryption
  at-rest/storage-state/in-transit, and raw-retention policy. `serve`
  snapshots the effective flags after offline-mode zeroing so the profile
  reflects runtime state, not config intent; health and `doctor` expose it;
  benchmark run manifests embed it. Fixes a latent loopback-classification
  bug: `localhost:11434` and `[::1]:8767` now classify as loopback (port
  stripped before host comparison).
- **Keystone-suggestion instruction extraction (#889).** New
  `perseus_vault_keystone_suggestions` + `perseus_vault_keystone_suggestion_decide`
  tools (schema v32): `perseus_vault_correct` captures are scanned with
  word-boundary-anchored directive patterns (en/de/ru/it/es) and queue
  candidates (`keystone_suggestions` table) that require an explicit operator
  `approve` decision — gated by the #683/#684 trust tier — before promotion to
  the keystones table. Fixes the #507 whenever/never inversion: anchored
  matching never extracts "never" from "whenever". Pending candidates surface
  in `perseus_vault_operator_review` with source citations. Registry 100→102.
- **Retrieval telemetry & trust concentration (#872).** New
  `perseus_vault_retrieval_telemetry` tool (schema v31) reporting
  concentration (HHI, top shares), repeated serving, low-trust fanout,
  diversity displacement (with sole-evidence flag), per-arm recall audits,
  cross-arm contamination probing, and an artifact hash — fail-closed
  `empty`/`degraded` states. Non-serveable statuses
  (`deprecated/expired/quarantined/redacted`) are now excluded at the SQL
  boundary in **every** recall arm plus a Rust guard in both hydrators;
  the contamination probe fails `invariant` when raw arms could reach
  retired rows. Spec: `docs/specs/retrieval-telemetry.md`; harness:
  `benchmark/telemetry/run.py`.
- **Fused multi-strategy recall (#883, #867).** New `fused` recall mode
  fusing up to four strategies — `fts5`, `dense`, `graph` (one-hop
  neighbor expansion), `temporal` (proximity to `query_time`) — via
  weighted RRF (k=60; empty arms carry weight 0 and degenerate
  gracefully). Fail-closed keyword arm; dense arm degrades to a
  `Partial` outcome with `partial_arms` naming instead of failing the
  recall. New params: `strategies`, `strategy_weights` (validated:
  unknown/negative/non-finite rejected), `max_tokens` (chars/4 estimate,
  top entity always delivered), `depth_budget` (low/mid/high →
  1024/4096/16384), `rerank` (rank-calibrated 1/(1+rank) re-scoring,
  default off), `query_time_unix_ms` (temporal anchor). Every fused
  recall returns a full `fused_trace`: verbatim original query, per-arm
  status/rankings/timing, fusion weights, truncation accounting,
  rerank decision, final placement and per-entity arm sources. Spec:
  `docs/specs/fused-multi-strategy-recall.md`.
- **First-class Hermes profile ↔ Vault workspace binding (#879).** New
  `workspace_bindings` registry (schema v30) + tools
  `perseus_vault_workspace_bind` / `perseus_vault_workspace_unbind` /
  `perseus_vault_workspace_quarantine` / `perseus_vault_workspace_status`. One profile ↔ one
  workspace (a workspace may be shared by several profiles); `read_only`
  bindings, cross-workspace targets, and quarantined/unbound states are
  **denied at the tool boundary** (fail-closed for bound profiles; unbound
  profiles keep legacy behavior — binding is an opt-in governance surface).
  Every transition journaled (`workspace_bound` / `workspace_rebound` /
  `workspace_unbound` / `workspace_quarantined` / `workspace_reactivated`).
  Client heartbeats drive a stale/live diagnostic signal. Spec:
  `docs/specs/hermes-profile-workspace-binding.md`.
- **Governed distillation (#876).** New `learned_memory` capability + tool
  `perseus_vault_learned_artifact_register`: learned artifacts (trained weights /
  distilled cartridges) are registered fail-closed against a **completed**
  `learned_memory` action receipt (capability, actor/workspace, and
  outcome-hash all verified) and bound hash-only to their source entities
  (`learned_artifact_sources`: entity id + body digest + recorded_at, schema
  v29). Erasing or purging a source **revokes** the binding — serve paths
  (`artifact_manifest`/`excerpt`/`verify_value`) refuse revoked artifacts as
  not-found; superseding a source flags it **stale** (retraining trigger,
  still serveable). `artifact_revoked` / `artifact_stale` journal events carry
  hash-only evidence; `perseus_vault_purge` reports `artifact_bindings_revoked`
  (dry-run + real). Spec: `docs/specs/governed-distillation.md`.
- **AAR denial clarity (#768 follow-up).** `action_intent` denials name the
  offending value and the permitted set; `authority_set` validates
  agent_id/workspace_hash per-field with a self-explanatory per-workspace
  regime message.
- **Retention lifecycle: expiry, redaction, and physical erasure** (#868,
  #866). Three distinct operations, documented in
  `docs/specs/data-boundaries-retention-lifecycle.md`:
  - `perseus_vault_expire` / `expire` CLI — time-based lifecycle sweep: entities whose
    body `expires_at` (unix ms, numeric string, or ISO 8601 UTC) has passed
    transition to `status='expired'`. Content and history are retained;
    recall already excludes expired rows. The write path now persists
    `expires_at` into `entities.expires_at_unix_ms` on every remember/update.
  - `perseus_vault_redact` / `redact` CLI — content scrub with metadata retention:
    body → hash-only marker, history + FTS text deleted, `redacted` journal
    event (hash-only evidence). Re-ingest of the same value stays allowed.
  - `perseus_vault_erase` / `erase` CLI — physical erasure across ALL derived layers:
    primary row, FTS, history (+FTS), community membership (empty communities
    deleted, `member_digest` invalidated so summaries recompute), inbound
    links, journal payloads (chain tuple preserved), plus quarantine of
    derived beliefs/observations/syntheses that cited the erased source via
    evidence links. Installs a permanent rejection tombstone + decoupled
    governance mandate, so re-ingest fails closed even after a primary-DB
    rollback; appends a hash-only `erased` journal event. `dry_run` previews
    exact counts. All ops are workspace-explicit (fail-closed on ambiguity,
    #854) and attribute the actor.

### Fixed
- **Recall now excludes expired rows on every arm** (#868 follow-up): the FTS
  keyword/hybrid arms previously served rows past `expires_at_unix_ms` — only
  the dense arm filtered them. Both FTS plans (two-phase `bm25` and the
  FTS-driven single query) now apply the same `(expires_at IS NULL OR
  expires_at > now)` predicate as the dense arm.
- **Encryption diagnostics and key propagation.** `doctor` now reports a
  database as encrypted only when the authoritative `encryption_canary` row
  (`id=1`) exists; the schema table alone is not evidence of encryption.
  Server and write-capable CLI paths resolve the existing default key file
  (`~/.perseus-vault/secret.key`, with legacy `~/.perseus-vault/secret.key` fallback),
  warn before plaintext writes to an encrypted vault when no key is available,
  and generated client configurations carry the resolved key path.

### Added
- **BEAM — bi-temporal correctness + determinism at scale** (#685). A new
  harness (`benchmark/beam/`) that embeds the CI-verified bi-temporal gauntlet
  inside a filler corpus sized to the BEAM token tiers (128K / 500K / 1M / 10M)
  and asserts, at every tier: correctness holds (gauntlet still 13/13),
  results are deterministic (two independent runs → identical signature), and
  per-axis point-lookup latency stays under budget. It **reuses**
  `gauntlet.run_scenarios` / `build_report` verbatim (one source of temporal
  truth — the gauntlet's `main` was refactored to call the same extracted
  functions, output unchanged), so BEAM can only ever agree with the gauntlet's
  verdicts. Ships with a CI gate (`gate.py`), a methodology `README.md`, and a
  binary-free `--self-test` for the corpus/token logic. Fully offline,
  seeded/deterministic corpus. Published tier numbers come from a run on named
  hardware (the heavy 1M/10M tiers on the benchmark fleet) — captured in a
  signed `report.json`, never hand-written.
- **Multi-agent scoping — trust tiers + visibility enforcement** (#684). A new
  `agents` registry (schema v22: `agent_id`, `name`, `trust_tier` 0-3,
  `fleet_id`) plus the `perseus_vault_agent` tool to register/look up agents.
  `entities`/`journal` already carried `agent_id` (v1.2.0); this adds the trust
  tier that gates sensitive ops and **enforces the previously stored-but-unused
  `visibility` field** on recall:
  - `can_read()` model — `private` → author (or admin) only; `fleet` → same
    fleet or tier ≥ 2; `workspace`/`tenant`/`''` (the default) → everyone.
  - The MCP transport now **captures the client's identity** from the
    `initialize` handshake (`clientInfo.name`) and stamps it onto tool calls as
    `requesting_agent_id`, so `recall` transparently drops entities the caller
    may not see — no explicit arg required.
  - Keystone authoring (#683) now gates on the **authoritative registered tier**
    when the author is a known agent, replacing pure caller-assertion
    (`trust_enforced=true` then means registry-backed).
  - **Non-breaking:** an empty/unknown requester is unscoped (tier 3 = admin),
    and the default `workspace` visibility is visible to all, so single-agent
    deployments and every existing entity/benchmark are unaffected. Scoping only
    hides `private`/`fleet` entities from identified, lower-tier agents.
  - Follow-ups (noted, not yet wired): enforcement on the other read surfaces
    (`ask`/`global_recall`/`memories`), automatic author `agent_id` population
    from the session on writes, and per-agent recall tuning.
- **Keystones — mandatory policy rules** (#683). A new `keystones` table
  (schema v21) plus `perseus_vault_keystone_set` / `perseus_vault_keystone_get` MCP tools.
  Keystones are directives fetched deterministically at session start (unlike
  ordinary memories, which are retrieved when relevant), merged across scope
  (tenant < fleet < agent) with weight-based conflict resolution, and meant to
  be obeyed over any conflicting instruction. Re-setting the same
  (scope, scope_id, content) updates in place; every mutation is appended to
  the cryptographic audit chain (`event_type=keystone_set`). Authoring is
  trust-tier-gated (`trust_tier_required`, default 2) — caller-asserted via
  `author_trust_tier` until per-agent trust + session identity land (#684), at
  which point enforcement becomes automatic (the response's `trust_enforced`
  flag surfaces which mode applied). The session-start `@keystone` render
  directive that injects merged keystones ahead of all other context lives in
  the separate Perseus orchestrator; this ships the vault-side storage, query,
  and audit surface it renders from.
- **Temporal RAG: history-inclusive point-in-time recall** (#682). Temporal
  semantic recall (`as_of` / `valid_at`) could only reconstruct facts whose
  *current* body still matched the query, because candidates came from the live
  FTS/dense index — a fact whose query-matching version had since been
  superseded or retired (its text now living only in `entity_history`) could
  never surface (the documented v1 limitation). A new standalone
  `entity_history_fts` index (schema v20) makes superseded/retired body text
  searchable; on a temporal recall, `augment_temporal_with_history` discovers
  the keys the live index missed and reconstructs each through the **same**
  authoritative `bitemporal_at` / `as_of_version` engines, so temporal semantics
  are identical to the point-lookup tools. The index stores plaintext
  (decrypt-aware, mirroring `entities_fts`; never indexes ciphertext),
  maintained at the history-append site and cleared at both history-delete
  sites; pre-existing history is backfilled by `perseus_vault_reindex`. Purely
  additive: live/relevance-ranked hits keep their positions, history-only hits
  are appended only to fill the caller's limit, and a query that already found
  enough is a no-op. The hot `db::recall` core is untouched (no determinism or
  benchmark impact).
- **Outcome-weighted recall ranking** (#681). The honest follow-rate efficacy
  signal (`perseus_vault_follow`) now feeds recall ranking, not just decay: a memory
  that gets FOLLOWED is boosted (`1.0 + follow_rate * 0.3`, mirroring the decay
  composite so the two never drift) and a 'dead' lesson (ignored despite
  ≥ 5 attempts) is gently demoted (×0.5 — not the decay path's near-annihilating
  ×0.05, so ranking never fully buries an otherwise-relevant hit). Folded into
  the existing #487 usefulness rank pass (one primary-key lookup) and applied
  over the fused candidate pool before truncation. On by default, bounded, and
  a strict no-op on any memory without a follow/miss signal — so freshly
  ingested corpora and every benchmark rank exactly as before. Kill-switch:
  `PERSEUS_VAULT_OUTCOME_RANK=0`. Determinism (#247) preserved.
- **`perseus_vault_scan` — deterministic paginated enumeration** (#562). First-class
  "list all / export / sync / reset" path: pages a category (or the whole store)
  by immutable `id ASC` with a keyset continuation cursor (`next_cursor` /
  `has_more`), so callers walk every entity exactly once — unlike paging
  `recall(query="")` with `offset`, whose ranking keys (`retrieval_count`,
  `last_accessed`) mutate as recalls reinforce entities (pages can skip or
  repeat rows) and whose offset is clamped at 10,000. Read-only by contract
  (no retrieval-count/decay side-effects), optional `workspace_hash` scoping
  (strict #408 semantics) and `include_archived`. The Python client's
  `VaultClient.scan()` now uses the tool automatically, falling back to legacy
  offset paging on pre-#562 servers. Tool count: 57 → 58.

### Documentation
- **Claims hygiene pass** (#702): removed the unbacked 100K-entity insert-rate
  figure from the README (no committed artifact backed it); the stress-test section
  now quotes the committed `benchmark/scale/report.json` (479 docs/s sustained
  MCP stdio writes at 10K, 40 docs/s durable at 100K, hybrid recall p50
  19.03 ms / 79.73 ms). Refreshed CLAIMS-AUDIT.md: the sub-millisecond recall
  entry is retired in favor of artifact-backed latencies, and the tool recount
  note now says the registry has grown since the v2.13-era recount (the current
  published figure is 65 canonical tools). Reworded "signed results/reports" to
  "content-hashed (sha256)" across README/PERF/benchmark surfaces, since
  `signature_sha256` is a self-computed content hash, not a cryptographic
  signature (audit-chain, keyed-MAC, and release-signing docs untouched).
  Clarified that `federate` is a local export / workspace-rename / re-import
  with no network peers; the Windows-safe default path is tracked in #704.
- **Recall query contract made explicit** (#562): `query=""` is the match-all /
  enumeration path; `"*"` and other wildcards are literal FTS5 terms, **not**
  globs (`"*"` matches nothing). Documented in the `recall` tool schema, README
  tool table, and docs/retrieval-modes.md, and pinned by tests.

### Fixed
- **stdio server no longer dies on quiet-but-alive hosts** (#748). The
  orphan-leak guard (#57228) used a flat 600s idle timer as its abandonment
  signal. On Linux that was a redundant backstop (PR_SET_PDEATHSIG + ppid
  reparent detection already catch real orphans), but on macOS/Windows it was
  the ONLY orphan check — so a healthy Claude Desktop session that simply went
  >10 minutes between tool calls had its server executed, and Claude Desktop
  never respawns a dead stdio server: the memory backend silently vanished
  until a full app restart. The guard now detects *parent death* instead of
  *inactivity*:
  - `is_orphaned_by_ppid()` / `record_initial_ppid()` widened from Linux-only
    to all Unix (`getppid()` has identical reparent-to-PID-1 semantics on
    macOS — orphans are adopted by launchd).
  - New background **parent-death watcher thread** polls the reparent check
    every 5s and exits promptly when orphaned — the existing ppid poll only
    ran per-request, so an idle orphan never noticed. On Linux this backs up
    PR_SET_PDEATHSIG; on macOS it is the primary abandonment signal.
  - `PERSEUS_VAULT_IDLE_TIMEOUT_SECS` is now **opt-in (default: off)** instead of
    defaulting to 600s. It remains for the one topology parent-death detection
    cannot see — a host that leaks the child's stdin write-end while staying
    alive (the original #57228 Hermes-worker reconnect leak); such hosts
    should set it when spawning the server. Unparseable values are ignored
    with a warning instead of silently enabling the timer.
  - Windows is unchanged in mechanism (stdin EOF on host death) but shares
    the new default: no flat idle kill. OpenProcess-based parent liveness for
    Windows is tracked in #751 and landed right after: the watcher thread now
    also runs on Windows, probing the startup-recorded parent PID via
    `OpenProcess` + `GetProcessTimes` (creation-time compared, so PID reuse
    cannot mask a dead parent; a failed Toolhelp snapshot conservatively
    never fires). Adds a Windows-only `windows-sys` 0.52 dependency.

## [2.20.0] - 2026-07-10

### Added
- **`perseus_vault_check_failure_pattern` — failure-pattern / deja-vu guard** (#521). A lightweight
  pre-retry check: pass the command line or approach you are about to (re)try and get back
  matching prior failures — from BOTH the journal (`error` events and failure-marked
  `acted`/`forward` payloads) and the entity store (failure/pitfall/root-cause memories via
  the existing FTS5 recall) — each with the recorded cause and resolution, ranked by
  similarity × (recency + trust/decay). Returns a `deja_vu` flag and a one-line
  agent-actionable warning when the action was already tried and failed, and an unambiguous
  "no prior failures recorded matching this action" empty state otherwise. Read-only by
  contract (never bumps retrieval counts or decay), workspace-scoped (own + global failures
  only; other workspaces never leak), local and deterministic, no new storage model.
  Exposed under all three tool-name prefixes like every other tool. Tool count: 55 → 56.
- **`install-client` — one-command multi-client loop wiring** (#522). `perseus-vault
  install-client` (a visible alias of the extended `connect` verb) now wires the FULL
  recall/capture loop, not just the MCP registration: `--client` is optional (autodetects
  claude-code/codex/cursor by config-dir presence; `--all-detected` wires every detected
  client, `generic` prints a copy-paste stanza), every client is pointed at ONE shared
  memory database (the resolved `--db` path — one brain across projects and clients),
  `--hooks` registers the session lifecycle hooks per the docs/lifecycle-hooks.md contract
  (#523: Claude Code SessionStart `startup|resume` + SessionEnd; Codex Stop behind a
  once-per-day guard; Cursor hooks.json v1 with a JSON `additional_context` wrapper
  script), and `--rules` appends the portable usage-rules block to CLAUDE.md/AGENTS.md
  (append-guarded). Every file mutation is a merge that preserves unknown keys, backs the
  file up as `<name>.bak-perseus` first, and is skipped when already in place — re-running
  changes nothing. `--dry-run` prints every file it would touch with a line diff and
  writes nothing. The verb now also replaces pre-rename `perseus_vault`/`perseus_vault` server entries
  with the canonical `perseus-vault` one instead of duplicating them, and ends by printing
  the save-in-one-session/recall-in-another verify walkthrough.
- **Opt-in in-session memory capture** (#520): new CLI verb `perseus-vault capture` (stdin or
  `--file`; plain text, markdown, or JSONL — auto-detected) and MCP tool `perseus_vault_capture`
  (aliases `perseus_vault_capture`/`perseus_vault_capture`) that distill a session transcript or
  insight payload into durable entities the moment a problem is solved, instead of waiting for
  a scheduled harvest. The default distiller is fully local and deterministic (no LLM, no
  network): headed sections / paragraphs / JSONL records become candidate notes, classified by
  cheap keyword signals into root-cause / pitfall / decision / pattern / takeaway (failure
  markers aligned with the #521 deja-vu guard) and written through the normal remember path
  with `source="capture"`, layer buffer, moderate importance. `--llm` distills via the
  configured `--llm-endpoint` instead and falls back to the rule-based path on any LLM
  failure/timeout (#528 `PERSEUS_VAULT_LLM_TIMEOUT_SECS`). Flood control by design: trigram
  near-duplicate merging stays ON (re-captured solved problems merge instead of piling up),
  same-headline notes update in place, writes are hard-capped per invocation (20, lowerable via
  `max_entities`; dropped notes are reported), and `--dry-run` previews. Off by default: capture
  only happens when explicitly invoked (CLI / tool / user-written lifecycle hook — see
  docs/capture.md for on_insight and SessionEnd wiring; end-of-session order is capture, then
  `maintain`).

### Changed
- **`tools/list` advertises the canonical `perseus_vault_*` names only by default**
  — the tool manifest is no longer tripled. Since the Perseus Vault→Perseus Vault→Perseus Vault renames,
  every tool was advertised three times (`perseus_vault_*`, `perseus_vault_*`, `perseus_vault_*`), so a
  57-tool server exposed 171 entries. MCP clients that preload tool schemas (Rovo Dev,
  OpenWebUI/Minions, and others) reloaded that tripled ~260 KB payload on **every request**
  — reported in the field as context being "triple-input" each turn. The server now emits
  one canonical `perseus_vault_*` copy per tool (171 → 57). The legacy `perseus_vault_*`/`perseus_vault_*`
  names remain fully **callable** — `call_tool` still normalizes every prefix to the same
  handler — they are simply no longer advertised, so no existing client that calls them by
  name breaks. Opt back into advertising all three prefixes with
  `PERSEUS_VAULT_TOOL_ALIASES=all` (legacy env `PERSEUS_VAULT_TOOL_ALIASES` also honored;
  `PERSEUS_VAULT_` takes precedence).

### Performance
- **Hybrid recall at 100K: 295.5 → 80.9 ms p50 (3.7×)** (#511). Hybrid — the default
  recall mode — carried ~250 ms of cost beyond its dense + fts5 arms at 100K entities.
  Profile-first attribution (via the new opt-in `PERSEUS_VAULT_RECALL_TIMING=1` stage timing, a
  permanent zero-cost-when-off observability surface on the recall path) cleared every
  suspect in the issue — RRF over-fetch/hydration, query expansion, graph expand, and the
  post-RRF weighting passes measure ≤0.5 ms combined — and pinned 88% on the sparse arm:
  its single SQL joined every FTS match to `entities` and evaluated the full row (body
  overflow chains included) before `ORDER BY rank LIMIT k` could discard it (the
  #476/#507 disease, third instance). Fixed exactness-preserving: (1) two-phase BM25 —
  rank `rowid + bm25()` entirely inside the FTS index, hydrate + metadata-filter only a
  bounded pool, widen once then fall back to the exact single-query plan; (2) the dense
  and sparse arms now run concurrently (independent read-only queries on separate pooled
  connections), so hybrid pays max(arms) instead of their sum. Measured on the identical
  loaded 100K store: hybrid p50/p99 295.5/337.0 → 80.9/93.8 ms — within 2× of the sum of
  its arms; dense/fts5 unchanged; recall@5 regression 0 (deterministic recall benchmark
  byte-identical, LongMemEval retrieval subset recall@5/@10 unchanged). Scale-gate hybrid
  p99 budget tightened 1000 → 250 ms (100K) and 120 → 100 ms (10K) to lock the win.
  Attribution tables + methodology in PERF.md.
- **Write path: signature-driven near-duplicate scan** (#476, schema v17). The per-write
  dedup scan hydrated and re-hashed every same-category entity body (O(N·body_size) per
  insert) — the measured cause of the quadratic bulk-load curve. It now walks only the
  small `dedup_signatures` rows, band-pruned in SQL by the lossless trigram-count ratio
  bound, and touches an entity body only on an actual hit (verify-on-hit freshness with
  self-healing repair; provably no false positives). Measured: 10K load 141→554/s (3.9×),
  100K load 7→39/s (5.6×, ~4h→43min), 100K fts5 p99 182→22ms; recall/`as_of`/cold-start
  unchanged. See PERF.md. The v17 migration adds scope columns + the band index and
  eagerly backfills signatures for every active row (one-time, ~30-60s per 100K rows).
  The lossy opt-in `PERSEUS_VAULT_DEDUP_FTS_PREFILTER` (#228) is retired — it measured slower
  than the exact scan it pruned. Deliberate trade, pinned by tests: a row rewritten
  behind the engine's back (direct SQL, rolled-back pre-v10 binary) can be MISSED by
  dedup (one extra stored row) until its signature self-heals — it can never cause a
  false positive.

### Distribution
- **Listed on [mcpservers.org](https://mcpservers.org/servers/perseus-computing-llc/perseus-vault)**
  (Awesome MCP Servers) — the directory crawls the Official MCP Registry entry
  (registry.modelcontextprotocol.io), joining the Glama / PulseMCP / mcp.so listings noted
  under the 2.7.0 registry-publish entry below.

## [2.19.1] - 2026-07-07

### Fixed
- **`maintain`/`decay_tick`/`autocohere` failed with "no such column: usefulness_count" on
  upgraded stores** (#503). #487's usefulness columns were added to the migration block without
  bumping `SCHEMA_VERSION`, so any store already stamped v15 (anything upgraded through v2.18.x)
  skipped the ALTERs — fresh DBs were fine, and read paths kept working, so the failure stayed
  silent until the first maintenance pass. Hit live on the first v2.19.0 production deploy.
  `SCHEMA_VERSION` is now 16; the idempotent migration re-runs once and delivers the columns.

### Added
- **Cross-scope promotion** (#486, Belief-Memory-inspired; merged after 2.19.0 was cut). Opt-in
  `cross_scope_promote` on `cohere`: a fact independently observed in >= `cross_scope_k`
  (default 3) distinct workspaces is promoted to one global-scope entity carrying the most
  informative member body, `promoted_from` links to every per-scope evidence row, and
  provenance tags. Idempotent re-runs (the global scope's dedup absorbs them); undo = forget
  the promoted entity. Off by default — absence of the args is byte-for-byte prior behavior.

## [2.19.0] - 2026-07-07

### Added
- **One-shot `maintain` CLI verb** (#490). `perseus-vault maintain [--dry-run] [--vacuum] [--db …]`
  runs the full hands-off hygiene pass (decay tick → conflict re-scan → prune/compact →
  reindex → optional VACUUM) as a single shell entry point, exiting non-zero on failure so
  schedulers (cron / Task Scheduler / `perseus scheduler --job maintain`) can alert.
- **Maintenance dry-run estimates** (#491). `maintain --dry-run` (and the `maintenance` tool's
  dry-run) now report would-do estimates for vacuum (reclaimable bytes) and reindex, so a
  report-only first week can show what the pass *would* have done instead of zeros.
- **In-server maintenance loop** (#492). `serve --maintain-every <hours>` runs the same hygiene
  pass on a background thread inside the long-lived MCP server — hands-off hygiene with no
  external scheduler at all. Off unless the flag is passed.
- **Active-only stats fields** (#493). `gather_stats` counted archived (forgotten/decayed) rows
  in `total_entities` and every `by_*` group, inflating every number a user sees relative to
  what `list`/`count`/`recall` (all `archived = 0`) actually return. `Stats` now also carries
  `active_entities`, `archived_entities`, and `by_category_active`/`by_type_active`/`by_layer_active`;
  the unsuffixed fields are unchanged for compatibility. The dashboard headline prefers the
  active count and shows "+N archived" as a muted aside.
- **"About you" dashboard tab** (#494). Plain-language, trust-annotated, read-only view of what
  the vault holds — buckets mirror `perseus knows` (perseus#692): About you / Recently learned /
  Project facts & decisions / Low confidence; ✔/~ verified markers; old conversation/session
  flotsam collapses into a count line; curation is pointed at `perseus knows --correct/--forget`.
- **Scope as a ranking multiplier in `recall`** (#485, Belief-Memory-inspired).
  New optional `scope_weight` (0.0–1.0, requires `workspace_hash`): the workspace
  predicate widens from a hard filter to "current workspace OR global (`''`)", with
  broader-scope (global) hits weighted by the factor in the ranked paths — hybrid
  fused scores and dense similarities are multiplied (same first-phase expression as
  RRF/trust/decay/recency), and the score-less FTS5 keyword ordering gets a stable
  two-tier sort (current scope first). Current-workspace memories outrank
  equally-relevant global ones while a strong global memory still surfaces instead of
  being silently invisible. The widening only ever adds GLOBAL rows — other
  workspaces' memories stay excluded (#338/#339 scoping unchanged). Omitted =
  strict-filter behavior, byte-identical to before. Read-only in ranking (#247
  idempotence preserved). Invalid weights and weights without a workspace are
  rejected up front.
- **Auto-reinforcement via `derived_from` on `remember`** (#487, Belief-Memory-inspired).
  `remember` accepts an optional `derived_from` array (entity ids or `{category, key}`
  pairs, max 64) citing the memories the write was built on. Each cited source is
  automatically marked useful: `usefulness_count` bumped, `last_useful_unix_ms` stamped,
  `last_accessed_unix_ms` refreshed (a citation IS an access). Unlike `follow`/`score`,
  no explicit call is needed — the reinforcement rides the write the agent was already
  making, capturing the honest "this memory actually informed a later write" signal.
  Usefulness feeds the engine twice: `decay_tick` multiplies non-verified decay by a
  log-damped, capped `usefulness_weight` (1 citation ≈ ×1.07, 10 ≈ ×1.24, cap ×1.4) so
  cited-and-reused memories survive decay/compact longer, and hybrid recall applies the
  same weight post-RRF (Belief Memory's `(1.0 + usefulness())` rank term) so they outrank
  equally-relevant never-reused ones — read-only, preserving #247 idempotence. Row
  resolution mirrors `follow()`'s workspace semantics (#391/#396): strict equality when
  scoped, deterministic global-first pick when not. Self-citations are skipped; unknown
  citations are reported in the result (`derived_from: {reinforced, not_found}`), never
  fatal. Two new additive columns (`usefulness_count`, `last_useful_unix_ms`).

### Security — keyed-MAC audit chain + payload commitment (DRAFT, under review)
- **Keyed, content-committing audit chain** (v14→v15). The journal chain now (a)
  stores a per-entry SHA-256 **payload commitment** covered by the chain, so content
  tampering of a non-redacted entry is detected, while `purge` can still erase the
  payload (the commitment survives); and (b) is a **keyed HMAC-SHA256** when encryption
  is enabled — tamper-evident against an attacker who can recompute an unkeyed hash.
  Unencrypted deployments keep the unkeyed SHA-256 chain. Keying happens at
  `set_encryption` time (the key isn't available during open-time migration) and is
  **canary-gated** — an `audit_chain_state.key_canary` (an HMAC of a fixed label under
  the audit key) lets the rekey run once (first encrypted open after upgrade, or a key
  change) instead of on every open (§3.4).
  `verify-audit-chain` is scheme-aware and **fails closed** if a keyed chain is
  verified without the key. Ships a v15 migration (adds `payload_commitment`, backfills,
  rehashes). HMAC is unit-tested against RFC 4231. **Design + limits + reviewer questions:
  `docs/audit-chain-keyed-mac-design.md`.**

## [2.18.2] - 2026-07-07

### CI / Release
- **Hardened `release.yml` against the parallel upload race** (#483). Every recent release (v2.17.5 / v2.18.0 / v2.18.1) lost the full `x86_64-unknown-linux-gnu` asset because the matrix legs each *create-or-upload* to the release via `taiki-e/upload-rust-binary-action` and raced to create it — the loser failed with "release not found", requiring a manual cancel + `gh run rerun`. A new `create-release` job now creates the GitHub Release once up front and `lite`/`full` `needs` it, so every leg only *uploads* (no creation race). Added `timeout-minutes` to the full legs so a wedged runner can't hang the run. No binary/behavior change — the 2.18.2 binary is functionally identical to 2.18.1 (this release also validates the fixed pipeline).

## [2.18.1] - 2026-07-07

### Fixed
- **Temporal filter args now accept stringified integers** (#480). MCP/LLM tool-call clients frequently emit integer args as JSON strings (e.g. `"as_of_unix_ms": "1783400000000"`); the temporal `Option<i64>` fields on `recall` (`as_of_unix_ms`/`valid_at`/`valid_from_unix_ms`/`valid_to_unix_ms`) rejected them with "invalid type: string, expected i64" — making the v2.18.0 `as_of` filter uncallable from such clients (hit live on deploy). Added a number-or-string deserializer; also applied to `perseus_vault_ask`'s temporal args. Numbers still work unchanged; non-numeric strings still error.

### Added
- **Temporal RAG completion** (#472 / #481). `recall`'s `valid_at` now *reconstructs* the historical world-version (via `bitemporal_at`) instead of narrowing live rows; with `as_of` it forms the full bi-temporal cell. `perseus_vault_ask` gains `as_of_unix_ms` / `valid_at_unix_ms` (shared `Database::resolve_temporal_versions`) so RAG answers are reproducible at a past instant. `global_recall` stays current-view by design (its community summaries are recomputed artifacts, not versioned facts).

## [2.18.0] - 2026-07-07

### Added
- **Temporal RAG — point-in-time semantic recall** (#472). `recall` now accepts `as_of_unix_ms`, a transaction-time instant: semantic search reconstructs *what was believed at T* — each hit's body is the version that was live at that instant, and corrections recorded later do not leak into the T-view. Combine with `valid_at` for the full bi-temporal cell. Hits are stamped with `is_live_version` / `recorded_at_unix_ms` / `valid_from_unix_ms` / `valid_to_unix_ms`; absent `as_of`, output is unchanged. Additive post-ranking reconstruction (new `Database::as_of_version()` + `temporal_resolve()`) over the hardened bi-temporal engine (#470) — the ranking path is untouched. Transaction-time *ranked* recall is unclaimed territory among agent-memory systems; the compliance story is "reproduce the exact retrieval context the agent had at decision time." v1 note: candidate generation is over the live index, so a fact fully deleted since T won't surface; `valid_at` full reconstruction, `ask`/`global_recall` threading, and the temporal benchmark are follow-ups tracked on #472. Spec: `docs/specs/temporal-rag.md`. (#478)

## [2.17.5] - 2026-07-06

### Fixed
- **`recall`'s `category` filter (and `type`/`topic_path`/`workspace_hash`/`agent_id`/`min_decay`/`always_on`) was silently ignored in Dense and Hybrid modes** (#467). `fts5_search` applied these predicates in SQL, but `dense_search` ranked over the raw embedding space (`archived = 0` only) and `graph_expand` followed links regardless of metadata — so a category-scoped recall, including the common no-mode recall that auto-selects Hybrid when embeddings exist, returned cross-category hits. Added `retain_metadata_filters`, mirroring the `fts5_search` WHERE-clause semantics (including the #298/#525 default that hides `conversation` when no category is requested), applied to both semantic paths with over-fetch→filter→truncate so scoped queries still fill `limit`. (#468)

### Security
- Bump `crossbeam-epoch` 0.9.18 → 0.9.20 to clear **RUSTSEC-2026-0204** (invalid pointer dereference in the `fmt::Pointer` impl for `Atomic`/`Shared` null pointers; transitive dependency, lockfile-only).

## [2.17.4] - 2026-07-05

### Security — housekeeping (2026-07-05 review)
- **Removed the dead `--workspace-token` flag.** It was documented as
  "cross-workspace access" authentication but **no code ever read it** (the `serve`
  handler destructured it away) — a control that looked active and wasn't. Transport
  auth is `--mcp-token`; workspace scoping is a routing/relevance control, not an
  enforced boundary (see `docs/THREAT-MODEL.md`). Passing `--workspace-token` now
  errors instead of silently no-op'ing.

## [2.17.3] - 2026-07-05

### Security (2026-07-05 review)
- **`install.sh` checksum verification now fails closed** (MED). A missing published
  `.sha256`, or a host without `sha256sum`/`shasum`, previously warned and installed
  the binary **unverified**. Both now abort; set
  `PERSEUS_VAULT_INSECURE_SKIP_CHECKSUM=1` to explicitly opt out.
- **Docker image runs as a non-root `vault` user** (MED) instead of root; `/data` is
  created and owned by that user so the default `serve` command works on a fresh volume.
- **`cargo audit` now actually runs in CI** (MED) — a new `Security Audit` workflow
  scans `Cargo.lock` against RustSec on every push/PR and weekly, making the existing
  SECURITY.md claim true. (Vulnerabilities gate; two unmaintained-only advisories are
  documented ignores.)
- **`traverse` clamps caller-supplied `max_depth`/`max_nodes`** to sane ceilings
  (64 / 100,000) so a single request can't be asked to walk an unbounded slice of
  the link graph (LOW DoS hardening).
- **Dense recall clamps a negative `limit`** with `.max(0)` before the `usize` cast
  (a negative would wrap huge; downstream caps already neutralized it — hygiene).
- **GitHub connector validates `repo` as strict `owner/name`** before interpolating
  it into the api.github.com URL, preventing path/query injection from a malformed
  operator-config value (LOW).
- **Cryptographic audit chain.** The journal chain is now a real **SHA-256** hash
  chain (was a 64-bit non-cryptographic `DefaultHasher`/SipHash), length-prefixed,
  with a **v13→v14 rehash migration** (deterministic, idempotent, no-op on a fresh
  DB). New **`verify-audit-chain` CLI command** (`verify_audit_chain` was previously
  dead code, so nothing ever checked the chain). The chain still commits only to
  event existence/order/time/workspace (NOT payload — so `purge` erasure stays
  compatible) and remains **unkeyed**: a keyed MAC (off the encryption key) plus a
  redaction-safe payload commitment for full tamper-evidence is the tracked follow-up.
- See `docs/security-review-2026-07-05.md` for the full ranked review.

## [2.17.2] - 2026-07-05

### Fixed
- **Anthropic MCP Directory bundle was not installable.** The submitted `.mcpb`
  contained only `manifest.json` — the `binary` server it declared was never
  placed inside the bundle, so it could not be installed or run for review. The
  manifest also carried a stale `perseus_vault serve --db ~/.perseus-vault/...` command. Fixed:
  `entry_point` now points at `server/perseus-vault` inside the bundle, the
  command uses `${__dirname}/server/...` with `platform_overrides` for Windows
  (`.exe`) and Linux, and the stale `--db` arg is dropped (the binary
  self-resolves the cross-platform default DB path). (#456)

### Added
- **Real, self-contained `.mcpb` release artifact.** A new `mcpb.yml` workflow
  builds per-platform lite binaries — macOS **universal** (arm64+x86_64) via
  `lipo`, Windows MSVC (`crt-static`), Linux musl (static) — stages them under
  `server/`, and validates + packs with the official `@anthropic-ai/mcpb` CLI,
  attaching `perseus-vault.mcpb` to the release. (#456)
- **Windows and macOS-Intel prebuilt release binaries.** `release.yml` now builds
  `x86_64-pc-windows-msvc` and `x86_64-apple-darwin` in the full matrix, so every
  platform the directory listing declares has a prebuilt binary. (#456)

### Documentation
- Documented the **stdio idle-watchdog** (`PERSEUS_VAULT_IDLE_TIMEOUT_SECS`, default
  600s) in `docs/transport.md`, and explicitly warned against external
  process-count reapers of `perseus-vault` stdio subprocesses: those are the
  live transport for in-flight tool calls, so count-based reaping kills them
  mid-operation (surfaces as `Unknown tool` errors). The built-in watchdog
  already reclaims true orphans; external cleanup must key on age + orphaned
  parent, never raw count. (#450)

## [2.17.1] - 2026-07-04

### Fixed
- **`install.sh` was broken for every prebuilt install.** It downloaded a bare
  `perseus-vault-${TARGET}` asset name, but releases ship `.tar.gz` archives
  (+ `.sha256`), so the download 404'd on every platform and fell through to the
  build-from-source path. Now downloads the `.tar.gz`, extracts the binary, and
  verifies the published checksum (hard-fail on mismatch). Also corrected the
  platform→asset map: aarch64-linux ships only the `lite` musl build (the old
  code requested a nonexistent `-gnu` asset). (#451)
- **MCP Registry publish** (had failed on v2.16.0 and v2.17.0): the Docker
  image's `io.modelcontextprotocol.server.name` OCI annotation was still
  `io.github.Perseus-Computing-LLC/perseus-vault` while `server.json` had moved to
  `…/perseus-vault`, so the registry's ownership validation rejected the publish
  with a 400. The label now matches `server.json`, so v2.17.1 publishes under the
  `perseus-vault` namespace. (#452)

### Changed
- Dropped prebuilt **macOS Intel (x86_64-apple-darwin)** release binaries. The
  `macos-13` runner class is chronically backlogged and repeatedly stalled the
  release pipeline for ~1h. Apple Silicon (`aarch64-apple-darwin`) covers modern
  Macs; Intel-Mac users can `cargo install --git …` from source (or run the lite
  musl build under Rosetta). `install.sh` degrades gracefully with a source-build
  hint for that target. (#447)

## [2.17.0] - 2026-07-03

### Security / Hardening
- Multimodal ingest is now bounded against decompression bombs. A `.docx` is a
  DEFLATE zip, so a tiny on-disk file (within `PERSEUS_VAULT_MAX_INGEST_BYTES`) could
  decompress `word/document.xml` to many GB — the on-disk cap couldn't bound it,
  and the read was unbounded (OOM). The decompressed read is now capped at
  `PERSEUS_VAULT_MAX_DECOMPRESSED_BYTES` (default 256 MiB) and rejected past it. PDF
  extraction is bounded by the on-disk cap only (`pdf_extract` owns decompression
  with no limit API — documented; lower `PERSEUS_VAULT_MAX_INGEST_BYTES` for untrusted
  PDFs).
- Network transport & gRPC hardening (audit phases 1–3):
  - **Secure-bind guard**: binding an HTTP surface (MCP transport or web
    dashboard) to a non-loopback address with **no** auth token now refuses to
    start instead of coming up wide open. Override with
    `PERSEUS_VAULT_ALLOW_INSECURE_BIND=1` for a trusted network / auth-terminating proxy.
  - **Constant-time token comparison** for Bearer auth on both HTTP surfaces
    (was a byte-wise `==`, a timing side-channel on the secret).
  - **Request-body cap** (`PERSEUS_VAULT_MAX_HTTP_BODY_BYTES`, default 8 MiB) and a
    **global token-bucket rate limit** (`PERSEUS_VAULT_HTTP_RATE_PER_SEC` /
    `PERSEUS_VAULT_HTTP_RATE_BURST`, default 50 req/s + burst 100 → `429`).
  - **Tightened transport CORS** — explicit methods/headers instead of `Any`,
    with an optional `PERSEUS_VAULT_CORS_ALLOWED_ORIGINS` allowlist.
  - **gRPC security model**: `serve` now supports a Bearer-token auth interceptor
    (`PERSEUS_VAULT_GRPC_AUTH_TOKEN`), TLS and mutual-TLS (`PERSEUS_VAULT_GRPC_TLS_CERT/KEY`,
    `PERSEUS_VAULT_GRPC_TLS_CLIENT_CA`), a message-size cap (`PERSEUS_VAULT_GRPC_MAX_MSG_BYTES`),
    and the same secure-bind guard. See [docs/GRPC-SECURITY.md](docs/GRPC-SECURITY.md).
- Encryption canary (fail-fast wrong-key detection). `set_encryption` now
  verifies the configured key against a dedicated canary row at startup and
  **aborts loudly** ("the provided key is incorrect or the database is corrupt")
  instead of letting a wrong/rotated key silently `AuthFailed` on every later
  read. The canary is established on first encrypted setup (or when encryption is
  enabled on a legacy-plaintext DB); a canary-less store with pre-existing
  encrypted data is validated by scanning for authentic ciphertext, so a wrong
  key can never "bless" itself by writing a canary under it. Stored in its own
  `encryption_canary` table — invisible to recall/FTS/stats and caller-facing
  state tools.
- Build-time model fetch is now supply-chain pinned (`build.rs`): the bundled
  `all-MiniLM-L6-v2` ONNX model + tokenizer are fetched from an **immutable commit
  revision** (was the mutable `main` ref) and **SHA-256 verified** before being
  baked into the binary via `include_bytes!`. A compromised or updated upstream
  repo can no longer silently change the embedded model — a mismatch fails the
  build. Operator-supplied files (`PERSEUS_VAULT_BUNDLED_MODEL_DIR`, air-gapped builds)
  are verified against the same hashes.
- Windows key-file ACLs: `keygen` now restricts the new key file to the current
  user via `icacls` (Windows has no `0600`-at-creation equivalent), warning
  loudly if that fails; enabling encryption on Windows also emits a one-line
  runtime reminder that key-file ACLs are operator-owned.
- Bumped `anyhow` 1.0.102 → 1.0.103 to clear RUSTSEC-2026-0190 (unsoundness in
  `Error::downcast_mut()`).

### Performance
- Empty-query browse recall no longer degrades on large stores. The browse path
  orders by `retrieval_count DESC, last_accessed_unix_ms DESC, id ASC`, but
  `idx_entities_recall` covered only the first two keys — so a large tie-group on
  the leading keys (a cold or bulk-imported store with uniform `last_accessed`)
  forced SQLite to sort the whole group by `id` to satisfy `LIMIT k`
  (O(tie-group)). The index now includes the `id` tie-break, making browse a pure
  k-row range scan. Measured p50 at 1,000,000 rows: **29.7 ms → 0.046 ms**
  (~645×); FTS and point-lookup latencies were already flat and are unchanged.
  Ships a v13 schema migration that rebuilds the index on existing databases.

### Fixed
- De-flaked `concurrent_writer_not_starved_during_cohere`: the #400 lock-hold
  gate asserted *exactly zero* SQLITE_BUSY, which spuriously failed on loaded CI
  runners when OS scheduler jitter delayed a single (correctly chunked) cohere
  commit past the writer's ~250ms budget. Now asserts a low busy *rate* (<10%) —
  the #400 regression it guards produces ~100%, jitter ~0.5%, so detection is
  preserved with wide margin. No production-code change.

## [2.16.0] - 2026-07-03

### Security / Hardening
- Audit chain now cryptographically binds the workspace (#433 M2, #438): the
  journal hash folds in `workspace_hash` (stamped on every row since #417), so
  a journal entry can no longer be moved between workspaces without breaking
  `verify_audit_chain`. Ships a v11→v12 schema migration that re-hashes existing
  chains under the new formula (deterministic, idempotent, crash-safe inside the
  migration transaction) so pre-upgrade chains still verify, and purge redaction
  now preserves `workspace_hash` as a hashed field (still scrubbing payload +
  identifying columns).
- Encryption key file is created with `0600` at inode creation on Unix (#433 M1,
  #434), closing the brief world-readable window between write and chmod.
- `remember` bounds input sizes (#433, #434): category ≤ 256 B, key ≤ 1024 B,
  body ≤ 4 MiB — closes a DoS-via-huge-key vector on indexed/identity/FTS fields.
- File-watcher connector rejects symlinked entries (#433, #434): directory scans
  no longer follow a symlink out of the configured watch root.

### Added
- Prebuilt release binaries (#432, #435): tagged releases now publish
  `perseus-vault-lite` (static musl, linux x86_64/aarch64, no default features)
  and full `perseus-vault` (linux-gnu x86_64, macOS x86_64/arm64) with SHA-256
  checksums — no more mandatory from-source build to install/upgrade.
- `perseus-vault doctor` reports data freshness (#433 N4, #434): a "last write N
  days ago" line (WARN past 14 days) so a stale vault (stopped harvest) is
  visible instead of silently reported healthy.

### Changed
- Default on-disk paths moved to `~/.perseus-vault/` (#427, #437), precedence-only
  — fresh installs use `~/.perseus-vault/data/perseus-vault.db`; existing
  `~/.perseus-vault/` installs keep working via the fallback chain (no data moved).
  Adds `PERSEUS_VAULT_DB_PATH` (`PERSEUS_VAULT_DB_PATH` still honored); `secret.key`
  default prefers an existing location so encrypted installs never lose their key.
- MCP-registry server name aligned to `io.github.Perseus-Computing-LLC/perseus-vault` (#428, #436).

## [2.15.0] - 2026-07-03

### Fixed
- Default DB resolution now factors in emptiness (#424, follow-up to #421):
  when the database path is the implicit default (no `--db`, no
  `$PERSEUS_VAULT_DB_PATH`) and the highest-precedence candidate DB is *known-empty*
  (`SELECT COUNT(*) FROM entities == 0` — a keyless read, works under
  encryption), a lower-precedence but *non-empty* candidate is preferred
  instead. This fixes the reported case where a stale-empty
  `~/.perseus-vault/data/perseus-vault.db` shadowed a live `~/perseus-vault.db`. Candidates that can't
  be read (locked/corrupt/not-yet-a-vault) are treated as unknown — never
  demoted-on and never promoted-to — so behavior degrades gracefully to the
  path-based order plus the existing split-brain warning. Resolution + its
  warnings are now performed once in `main()` (`normalize_default_db`), so
  `serve` and every maintenance subcommand open the same resolved DB
  (previously only a handful of sites warned). `default_db_path()` (clap's
  eager default) stays path-only and side-effect-free.
- `scripts/bootstrap.sh` looked for a `target/release/perseus-vault` binary that the
  `perseus-vault`-named crate no longer produces (the build would report
  success then fail to find the binary). It now builds/installs `perseus-vault`
  with `perseus_vault`/`perseus_vault` compat symlinks, defaults to `perseus-vault.db`, and
  uses the `serve` subcommand — matching `scripts/install.sh` (#424).
- Onboarding/deploy surface completed the Perseus Vault → Perseus Vault rename: client
  setup docs (`docs/clients/README.md`, all 8 copy-paste snippets), MCP
  packaging (`smithery.yaml`, `manifest.json` version `2.13.0` → `2.14.0`),
  framework integration docs (langgraph/autogen + `docs/integration/*`),
  transport docs, the clawhub skill, and `awesome-perseus-vault.md` (tool count
  `36` → `55`) now use the `perseus-vault` command and the canonical
  `~/.perseus-vault/data/perseus-vault.db` default path. Fresh operators copy-pasting
  any client/integration snippet now get a working config that matches the
  installed binary. The `~/.perseus-vault/` support directory is intentionally
  unchanged pending the migration-shim work in #427.

### Added
- History retention mechanism (#398): entity_history can now be bounded via
  env knobs — `PERSEUS_VAULT_HISTORY_MAX_AGE_DAYS`, `PERSEUS_VAULT_HISTORY_MAX_VERSIONS_PER_KEY`
  (oldest-first per key), `PERSEUS_VAULT_HISTORY_MAX_BYTES` (globally oldest-first).
  **Every knob defaults OFF** — with none set, behavior is byte-identical to
  before (keep everything); enabling a bound is an explicit operator decision.
  Enforcement runs only in maintenance paths (`perseus_vault_maintenance` `history`/
  `all`, `perseus_vault_autocohere`, `perseus_vault_prune scope='history'`), never on the
  write path.
- Tombstone roll-up compaction (#398, issue option 2 — default ON, disable
  via `PERSEUS_VAULT_HISTORY_TOMBSTONES=0` for hard delete): an evicted run of
  versions is replaced by ONE synthetic history row spanning
  [first_recorded_at, last_invalidated_at) carrying the rolled-up version
  count and a hash-chain digest of the evicted rows (successive passes merge:
  counts accumulate, digests chain). `perseus_vault_as_of` at an instant inside a
  compacted window returns an explicit marker (`compacted: true`,
  `versions_compacted`, `digest`) instead of silently-wrong data; instants
  covered by surviving rows are answered exactly as before. The valid-time
  axis holds too: the tombstone carries the run's earliest effective
  `valid_from` (not first_recorded_at), so a retroactively-valid compacted
  version's window keeps answering `perseus_vault_valid_at`/`perseus_vault_bitemporal` —
  with the same explicit marker decoration — instead of flipping to None.
  Option 3 (export-then-delete to vault Markdown/JSONL) is deferred as a
  follow-up.
- `perseus_vault_prune` gains `scope: 'history'` (#398) with per-call bound overrides
  (`max_age_days`, `max_versions_per_key`, `max_bytes`) and dry_run
  preview — reports the exact rows + bytes the real run would evict.
- `perseus_vault_stats` reports history growth (#398): `total_history_rows`,
  `history_bytes` (stored body bytes), and `top_history_keys` (top-10 keys by
  version count with bytes) — the hot state-like keys to cap first.
- `perf-gate` CI workflow (#404, completing the issue — the concurrency half
  shipped as `concurrency-gate`): a release-build gate that seeds temp DBs
  via the fastest direct-SQL path and pins the 2026-07-02 capacity-deep-dive
  baselines with 3-5× CI-variance headroom — rare-term FTS recall, browse and
  get_entity p50 @100k, as_of p50 @50k history versions of one key,
  decay_tick wall @100k plus the #399 regression signature (second
  consecutive tick rewrites < 1% of rows, WAL growth < 2× DB size), cohere
  wall @100k plus the post-#400 longest single writer-lock hold < 1s
  (measured with the #400 BEGIN IMMEDIATE probe), and on-disk history bytes
  per superseded version at a ~1KB body. Medians of 5 for latency metrics;
  every metric prints a `PERF-GATE |` table row to the job log so a
  regression is diagnosable from the run. Budgets and corpus sizes are pinned
  as env vars in the workflow.
- `perseus_vault_follow` accepts an optional `workspace_hash` (#396, the #338
  pattern): when set, the efficacy stamp resolves its target row with strict
  workspace equality — the same semantics as a workspace-scoped recall — so a
  workspace-scoped agent's follow/miss signal lands on the row it actually
  saw, instead of the deterministic global-first pick giving another
  workspace's row (or the global `''` row) phantom counts. Omitted = the
  existing deterministic pick, unchanged.

### Changed
- Auto-embed on content-changing writes now runs on a background worker
  instead of inline (#393): the synchronous ONNX call added ~6.7ms to every
  default-build write (62×, ~145 writes/s single-writer ceiling). Writes now
  enqueue (id, plaintext) to a bounded queue (1024 jobs, drop-new on overflow
  with a rate-limited warning) and return immediately; the worker drains up
  to 32 jobs per wake, embeds, and stores each vector through a stale guard
  (an atomic conditional UPDATE against the entity's current FTS plaintext),
  so a queued embed can never overwrite a newer body's vector. A
  content-changing UPDATE also clears the row's stored embedding inside the
  write transaction, so embed lag — or a dropped job — means the row is
  ABSENT from dense search (keyword search still finds it), never served
  with the previous body's stale vector, and every unembedded row is
  genuinely recoverable via `perseus_vault_embed` batch mode
  (`WHERE embedding IS NULL`) or its next change. Deferral is within the
  existing contract — auto-embed already ran post-commit with non-fatal
  failures; a row simply doesn't surface in dense/hybrid search until
  embedded (now milliseconds later). Explicit `perseus_vault_embed` stays
  synchronous. The write path also no longer consults the #219 embedding
  session cache (new/changed bodies can never hit it — each write paid up
  to 256 full-body string compares for nothing), and the
  misconfigured-backend log (enabled, model missing, no endpoint — formerly
  one eprintln per write) is rate-limited to once per minute. `Drop` for
  `Database` disconnects the queue and waits up to 5s while the worker
  DRAINS the remaining jobs (CLI one-shot writes still get embedded, as
  they did synchronously pre-#393); a drain that outlives the grace
  continues on the detached thread, and any never-embedded row is NULL —
  batch-recoverable, never stale. Measured (debug profile, 1KB bodies,
  bundled ONNX, n=40, median-of-5-runs): write median 7,714µs → ~159µs
  (~48×).
- **Empty-string `workspace_hash` is now STRICT everywhere (#408).**
  `list_entities`/`count_entities` (the dashboard entity list and
  its `total`) and `get_entity_graph` treated `Some("")` as *unscoped* —
  no filter, every workspace's rows — while `recall`/`recall_when`/`follow`
  treat `""` with strict equality (only the global `''` rows). The same
  argument value meant two different scopes depending on the surface. All
  three now apply strict equality for any `Some`, including `Some("")`;
  `None`/omitting the param remains the unscoped view. On the web API this
  means `?workspace=` (present but empty) now returns only global-`''`
  rows instead of everything — omit the parameter entirely for the
  unscoped view. The bundled dashboard never sends the parameter, so its
  behavior is unchanged.

### Fixed
- Journal redaction is now workspace-scoped (#417, follow-up to #416): the
  `journal` table gained a `workspace_hash` column (SCHEMA_VERSION 10 → 11),
  stamped at write time in `Database::journal` from the referenced entity's
  workspace. `purge`'s `(category, key)` redaction match is scoped to the
  purged entity's workspace, so purging workspace A no longer redacts workspace
  B's live same-key journal rows. Rows with an empty `workspace_hash` (legacy
  pre-v11 rows, or genuine default-workspace rows) are still matched
  conservatively so erasure never *under*-redacts (no GDPR regression); the
  residual over-redaction is narrowed to default-workspace rows sharing an
  exact `(category, key)` with a purged *named*-workspace entity. `docs/
  retention.md` now also names the derivative artifacts `purge` does not
  auto-erase (dream/consolidate outputs, community summaries, vault_export
  files).
- Default DB-path resolution surfaces the split-brain instead of hiding it
  (#421): the legacy single-user location `~/perseus-vault.db` is now **added to the
  fallback chain** (adopted when it is the *only* existing DB, instead of
  creating a fresh empty `~/.perseus-vault/data/perseus-vault.db`). Full precedence
  (first existing wins): `~/.perseus-vault/data/perseus-vault.db` >
  `~/.perseus-vault/data/perseus-vault.db` > `~/.perseus-vault/data/perseus-vault.db` > `~/perseus-vault.db`; if none
  exist the canonical `perseus-vault.db` is created. Note this is
  precedence-only, not emptiness-aware: in the issue's reported scenario where
  a live `~/perseus-vault.db` **and** a stale-empty `~/.perseus-vault/data/perseus-vault.db` both
  exist, the higher-precedence dir DB still wins — `~/perseus-vault.db` is **surfaced
  via the warning, not auto-adopted**. When more than one candidate DB exists
  and no `--db`/`$PERSEUS_VAULT_DB_PATH` was given, a stderr warning names the chosen
  file and the ignored candidate(s) so the ambiguity is visible; passing
  `--db`/`$PERSEUS_VAULT_DB_PATH` explicitly is the deterministic remedy and suppresses
  the warning. Resolution was refactored into a pure, unit-tested
  `resolve_default_db(home, exists)` function. (Emptiness-aware precedence is a
  filed follow-up.)
- macOS Apple silicon build-from-source binaries no longer fail with an
  unexplained `Killed: 9` on first run (#422): the `bootstrap.sh` build-and-
  install path now ad-hoc code-signs the binary (`codesign --force --sign -`)
  guarded by a `uname` Darwin/arm64 check, and the README build-from-source
  note documents the required signing step after every rebuild.
- `perseus_vault_purge` now honors its own "actually remove" contract (#398): purging
  an archived entity also DELETEs every superseded version of it from
  `entity_history` (matched by id and by category/key/workspace, so versions
  written under earlier ids of the same key are erased too) and REDACTS
  journal rows referencing it — payload columns scrubbed in place,
  `event_type` stamped `redacted`. Journal rows are redacted rather than
  deleted because the audit chain hashes only (prev_hash, id, created_at);
  redaction removes every purged body from the log while
  `verify_audit_chain` stays valid end-to-end. Before this fix a GDPR-style
  forget-then-purge left all historical bodies readable via
  `perseus_vault_history`/`perseus_vault_as_of` and the journal append-only forever.
  `PurgeReport` gains `history_rows_deleted` / `journal_rows_redacted`
  (dry_run previews both with the same predicates).
- `remember()`'s near-duplicate scan no longer rebuilds every candidate's
  trigram set per insert (#392) — the O(M·N) cost that made a single write
  stall ~1.6s at 50k same-category entities (0.6 inserts/s). Each row's
  packed trigram set is now stored once at write time (schema v10:
  `dedup_signatures` + `dedup_signature_blobs`, derived from the stored —
  i.e. possibly encrypted — `body_json` column value) and the scan computes
  its verdict from the stored signature behind two provably lossless prunes
  (exact set-size ceiling, 256-bucket histogram intersection ceiling).
  Dedup semantics are EXACT: the new path returns the identical
  match-or-not and matched id as the exhaustive trigram-Jaccard scan
  (randomized property-tested against the old implementation, including
  threshold-boundary, tiny-body, unicode and encrypted stores). Existing
  rows need no migration pass: unsigned rows take the old rebuild path and
  are backfilled lazily in bounded batches (512/scan). A signature is
  trusted only while BOTH the stored body's byte length and a stable
  64-bit content hash still match, so same-length rewrites by
  signature-unaware writers — e.g. a rolled-back pre-v10 binary running
  against a v10 store — read as stale and self-heal instead of poisoning
  verdicts (rollback-safe; dropping the two side tables is also always a
  safe reset). The lazy write-back re-verifies the row's current body
  under the write lock before landing, so a backfill can never overwrite
  the fresher signature a concurrent update just committed. Measured
  (release, 1KB uniform-length bodies — the length prefilter's worst
  case; medians over 15 probes, same run for both paths): single-insert
  dedup scan @50k 1363.3ms → 89.4ms (15.3x); bulk import of 5,000
  (fresh store, dedup ON) 123.6s (pre, per #392) → 15.1s total (~8x). The opt-in `PERSEUS_VAULT_DEDUP_FTS_PREFILTER` path
  is unchanged and composes with the stored signatures.
- `follow()`'s row resolution no longer collapses real DB errors into
  "not found" (#396, the #394 principle): only `QueryReturnedNoRows` maps to
  the clean `found: false` report; a locked file or corruption error now
  propagates.
- Selective FTS recall cost now tracks the number of HITS, not corpus size
  (#401). Queries whose FTS match set fits under a cap (512 rows)
  materialize the matched rowids first and run the ranking ORDER BY over
  just those rows via INTEGER PRIMARY KEY lookups (`NOT INDEXED` pins the
  plan), instead of scanning `idx_entities_recall` while probing an
  up-to-100k-rowid FTS IN-list. Larger match sets keep the legacy
  rank-index-driven plan, which is efficient exactly when matches are
  dense. Result semantics are byte-identical (same filters, ranking order,
  LIMIT/OFFSET — equivalence-tested against the legacy plan); a query
  whose FTS terms match nothing now short-circuits without touching the
  entities table. Measured @100k (release, p50/50 iters): rare-term
  (20 hits) recall 5.1ms → 0.08ms (~64x); dense-match queries pay a small
  fixed probe cost (common term ~33k hits: +~0.5ms, the intrinsic FTS5
  prefix-doclist materialization — they were and remain O(corpus)).
- Web dashboard and gRPC no longer wrap the pooled `Database` in a global
  `std::Mutex` (#402): both surfaces now share the SAME `Arc<Database>` as the
  MCP transport (one process, one connection pool — the dashboard previously
  opened a second 16-conn pool on the same file) and run every DB call on the
  blocking thread pool via `tokio::task::spawn_blocking`, mirroring
  transport.rs (#210/#217). Dashboard requests and gRPC RPCs now execute in
  parallel instead of single-lane, and no longer stall async runtime workers.
- `GET /api/graph` is paginated (#402): `limit` (default 500, max 5000) and
  `offset` query params; response adds `total_nodes` / `returned_nodes` /
  `truncated` so clients can tell a page from the whole graph (previously it
  full-scanned and returned every node+edge unpaginated — tens of MB of JSON
  at 100k entities, per dashboard render). The dashboard's graph tab shows a
  truncation note when capped. Edges dangling outside the returned node set
  are dropped (previously the unscoped path emitted edges to archived/deleted
  targets that the renderer couldn't resolve).
- `perseus_vault_cohere` no longer holds one writer lock for the whole grooming pass
  (#400). The single BEGIN IMMEDIATE previously spanned promotion, a
  full-table decay UPDATE, link building, and archive — a lock window linear
  in store size (~4.4s @100k entities) that crossed the default 5000ms
  `busy_timeout` just past ~130k entities, so concurrent `remember`s failed
  SQLITE_BUSY during every maintenance run. cohere now runs three bounded
  windows: promotion, a decay pass chunked at 1000 rows per drop-safe
  transaction (with a 2ms inter-chunk yield so waiting writers can actually
  acquire the lock), and link+archive. Longest single hold measured @200k
  entities (release, ~450B rows): 0.68s → 0.09s; the write work under any
  one lock is now bounded by the chunk size, and the remaining linear
  component (the promote/archive full-table read scans) is ~7.5x shallower
  than before. Preserves the #399/#405 no-op write skip (floored
  rows are not rewritten), cohere's documented ×0.95 standalone decay
  semantics, and per-transaction drop-safety (#388); the run stays correct
  under interleaved writers, and the new non-atomicity boundaries are
  documented at the split site.
- `perseus_vault_autocohere`'s cohere step actually creates auto-links now (#412).
  `CohereParams`' derived `Default` gave `max_links = 0`, and autocohere
  builds its params with `..Default::default()` — so the link-candidate
  query ran with `LIMIT 0` and the graph-building half of scheduled
  maintenance had silently been a no-op since it shipped. A manual
  `Default` impl now carries the same `max_links = 20` budget the
  `perseus_vault_cohere` argument path gets from its serde default;
  `promote_threshold`/`archive_threshold` keep their fall-through-to-
  constants sentinels, and explicit `perseus_vault_cohere` args are unaffected.
- `GET /api/entities`, `GET /api/search`, and `GET /api/journal` clamp
  `limit` to [1, 5000] (#413), exactly like #402's `/api/graph` clamp: an
  explicit `?limit=1000000` previously passed straight into SQL and dumped
  the whole table (14.7MB/1.5s at 20k rows) — and since #402 moved the
  dashboard onto the shared connection pool, a handful of such requests
  could pin every pooled connection and brown out MCP traffic. Defaults are
  unchanged (50 each); non-numeric/overflowing `limit`/`offset` values are
  rejected with 400; the responses now echo the effective `limit` (and
  `offset` for `/api/entities`).

## [2.14.0] - 2026-07-02

### Added
- Recall-first context injection (#366): `perseus_vault_context` / `prepare` default
  to `mode: on_demand` — a relevance-gated, budget-clamped block instead of
  the unconditional top-N dump. New `perseus_vault_context` params: `query`, `mode`,
  `model`, `max_context_chars`; new `prepare` flags: `--max-context-chars`,
  `--model`, `--legacy-context`. Per-model budgets: 1500 chars default, 6000
  for "opus"-class hosts. Legacy dump is opt-in via `mode: "always_inject"`.
- Always-on set hard-capped (5 entities) under recall-first, with a
  documented overflow warning steering toward `recall_when` triggers (#366).
- GraphRAG over the link graph (#365): `perseus_vault_communities` (deterministic
  community detection — label propagation with neighborhood-overlap weighting,
  or greedy one-level modularity "louvain"; pure Rust, no new dependencies),
  `perseus_vault_community_summary` (extractive by default, optional LLM polish,
  materialized as a `community_summary` entity with `evidence_for` links,
  cached by member-set digest), and `perseus_vault_global_recall` (breadth over
  community summaries, then depth into the best communities' members — cites
  entities across clusters instead of only the nearest one). Communities are
  persisted in a new `communities` table (schema v8); `perseus_vault_stats` now
  reports `total_communities` and `graph_modularity`.
- `perseus_vault_dream` — sleep-time LLM consolidation of episodic → semantic memory:
  clusters related cold memories per category, reflects over each cluster via
  the configured `--llm-endpoint`, and writes back provenance-linked semantic
  insights (`evidence_for` to every source, `derivation: "dream"`, idempotent
  by evidence-set hash, contradiction-aware, bounded budgets, dry-run;
  verified/importance-floored sources never archived). 53rd MCP tool (#364)
- **Bi-temporal memory — queryable valid-time axis (#363,
  SQL:2011 APPLICATION_TIME).** The `valid_from`/`valid_to` columns are no
  longer write-only: facts now carry a queryable application-time period
  ("when was this true in the world"), orthogonal to the existing
  transaction-time axis (`perseus_vault_as_of`). New tools `perseus_vault_valid_at`
  (what was actually true at instant T, per current knowledge) and
  `perseus_vault_bitemporal` (the full 2-axis rectangle query: "as of transaction
  time T, what did we believe was true at valid time V"). Valid time is
  settable on `perseus_vault_remember`/`perseus_vault_correct` (`valid_from_unix_ms` /
  `valid_to_unix_ms`, defaulting to transaction time / unbounded);
  `perseus_vault_supersede` closes the old fact's valid period. `perseus_vault_recall` gains
  `valid_at` and SQL:2011 `overlaps`/`contains` period filters. Schema v9
  backfills `valid_from = recorded_at` on existing rows (idempotent). Tool
  count 53 → 55.

### Fixed
- Pool-exhaustion collapse under concurrency (#397): recall, insert, and
  auto-embed each drew a SECOND pooled connection while holding one, so at
  ≥ pool-size concurrent requests every slot was held by a frame blocking on
  the nested draw — 32 clients vs pool 16 measured 174 req/s with 30-second
  stalls and failed writes; 64 clients wedged. `apply_recall_side_effects`,
  `find_near_duplicate`, and `store_embedding` now reuse the caller's held
  connection (`_with_conn` variants), including `perseus_vault_embed`'s single-entity
  path; the same load now runs at ~4,200 req/s with zero errors. r2d2's
  checkout timeout is tunable via `PERSEUS_VAULT_POOL_TIMEOUT_MS`. A new
  `concurrency-gate` CI workflow pins the load test at 2× pool
  oversubscription plus the four concurrency hammer tests.
- decay_tick write amplification (#399): every tick rewrote every
  non-archived row even when nothing changed — 412MB of WAL per tick on a
  45MB database at 100k entities. Writes are now skipped when the recomputed
  score is within epsilon of the stored value (archive and layer-boundary
  crossings always write), so steady-state ticks write ~zero rows;
  `entities_updated` now reports rows actually written.
- `perseus_vault_history` pagination (#403): the tool returned every full decrypted
  version body with no limit — a hot key with 10k versions produced a
  ~10-15MB tool response. Now takes `limit` (default 20, newest-first,
  0 = count-only) and `offset`, and reports `total`/`returned`.
- follow() cross-workspace efficacy clobber (#391) and lost updates (#385):
  the key-addressed UPDATE stamped one workspace's counts and
  `efficacy_status` onto every (category,key) row — other workspaces and
  archived rows included — and the unlocked read-modify-write lost
  increments under concurrent calls. follow() now resolves ONE live row
  (the deterministic get_entity pick) under the audited writer lock and pins
  its UPDATE to that id.
- link/unlink pool starvation (#387): both resolved the source entity via
  `get_entity()`, drawing a second pooled connection while one was held —
  ≥16 concurrent linkers hit 30s r2d2 timeouts with opaque `Error(None)`.
  Ids now resolve on the caller's own connection.
- cohere error-path transaction leak (#388, corrected premise): the raw
  `BEGIN IMMEDIATE`/`COMMIT` pair had no rollback guard — any error between
  them returned the pooled connection with the transaction still open,
  permanently poisoning that slot ("cannot start a transaction within a
  transaction" on every subsequent checkout). cohere now uses the drop-safe
  transaction; errors roll back. (The filed links-clobber scenario could not
  occur — the pair-scan read already ran inside the writer transaction.)
- remember() erased link graphs (#382): the MCP remember tool constructs
  entities with empty links and remember's full-row UPDATE wrote them
  wholesale — ANY re-remember of a linked entity deterministically erased
  its edges, and concurrent `perseus_vault_link` calls could lose edges to the
  unguarded read-modify-write. link/unlink now run under the writer lock and
  remember UNIONS caller links with stored links (dedup by target;
  stored relationship/weight win; `perseus_vault_unlink` is the only removal path).
- invalidate_entity temporal-window corruption (#381): the fourth
  `entity_history` writer stamped `invalidated_at = now()` raw — an audited
  writer that legitimately set `recorded_at` ahead of the wall clock produced
  an INVERTED window, and a same-millisecond create+invalidate produced a
  zero-width window that `perseus_vault_as_of` could never reconstruct. It now takes
  the writer lock and bumps `invalidated_at` strictly past `recorded_at`.
- rekey-aad stale overwrite (#386): a `remember()` landing between rekey's
  read and its re-encrypted write was silently reverted to stale content
  under a valid ciphertext; the per-row write is now guarded on the
  ciphertext being unchanged.
- Audited-writer TOCTOU (#379): the three audited temporal writers (the #371
  re-assert path in remember, the #373 `set_valid_to` close, the #377 status
  flip) read their preconditions on the bare pooled connection before opening
  a transaction, so two concurrent writers on the same id could both pass
  their checks against the same stale read and interleave — double snapshots,
  zero/inverted history windows, and a live `recorded_at` moving backwards
  (reliably reproducible under a 3-thread hammer). All three now take an
  IMMEDIATE writer lock BEFORE the precondition read via a shared
  `audited_write_tx` helper; concurrent writers serialize on the connection's
  `busy_timeout` instead of corrupting, and rejected writes roll back cleanly.
- Audited status flips (#377): `update_entity_status` now snapshots the
  pre-change version to `entity_history` and advances the live row's
  transaction time whenever the status actually changes — closing the
  expired-fact supersede corner, where the valid period was already closed,
  `set_valid_to`'s close was a no-op that wrote no snapshot, and the
  `deprecated` flip was baked in under the original transaction time (so
  `perseus_vault_bitemporal` at pre-supersede instants showed the expired fact as
  already deprecated). A same-status call (e.g. re-superseding an
  already-deprecated fact) still refreshes `archive_reason` in place,
  unversioned by design — a reason overwrite is operational metadata, not a
  knowledge change. A normal supersede now writes two snapshots: the audited
  close, then the audited flip.
- Supersede snapshot status fidelity (#375): `perseus_vault_supersede` now closes the
  old fact's valid period BEFORE flipping its status to `deprecated`, so the
  #373 audit snapshot captures the true pre-supersede state — previously the
  snapshot baked `deprecated` in under the original transaction time, and
  `perseus_vault_bitemporal` reconstruction at a pre-supersede instant showed the
  fact deprecated while it was still believed active. A failed close now
  also leaves the status untouched.
- Audited `set_valid_to` closes (#373): closing/tightening a fact's valid
  period (directly or via `perseus_vault_supersede`) now snapshots the pre-close
  version to `entity_history` and advances the live row's transaction time —
  previously a close was invisible to `perseus_vault_as_of`/`perseus_vault_bitemporal`
  reconstruction, which reported the close even at transaction instants
  before it happened. Tighten-only acceptance semantics are unchanged, and a
  no-op call (an earlier stored close is kept) writes no snapshot.
- Bi-temporal audit gap (#371): an identical-body re-remember that moves the
  bounds of an already-CLOSED valid period (e.g. re-extending past a
  `perseus_vault_supersede`/`set_valid_to` close) now snapshots the pre-change version
  to `entity_history` and advances the live row's transaction time, so
  `perseus_vault_history`/`perseus_vault_bitemporal` reconstruct both the closed period and
  the re-extension. Acceptance semantics are unchanged (deliberate re-asserts
  may still extend); re-asserts that leave the period untouched write no
  spurious snapshot.
- Context injection relevance gating (#356): `context`/`prepare` no longer
  dump topically unrelated entities — injection is gated by `recall_when`
  trigger matching + stopword-filtered keyword search against the current
  query (retrieval_count is no longer a relevance proxy), workspace-scoped
  including the always-on set. Injected blocks are framed as informational
  memory, not authoritative instructions.
- MCP Registry publish: `server.json` version/OCI identifier now synced from
  `Cargo.toml` at publish time, and the publish waits for the GHCR image tag
  to exist, so a stale hand-maintained version can never be published again
  (#351).

## [2.13.0] - 2026-07-01

### Added
- `## Perseus Vault Context` header for injected context blocks +
  `docs/retention.md` (#341)
- Opt-in `reinforce` flag for dense/hybrid recall (#343)
- Persistent `importance` column — explicit scores survive decay recompute (#344)
- `perseus_vault_memories`: Anthropic `/memories` directory-convention adapter — file
  interface (`view`/`create`/`str_replace`/…) backed by vault entities (#345)
- Coldness-driven consolidation ("local dreaming") wired into autocohere (#350)

### Fixed
- Prompt-injection sanitization in `prepare` + unified decay/promote
  constants (#337)
- `workspace_hash` scoping for context/recall_when/prepare + write-path
  dedup (#338)
- Workspace-scoped entity identity — identity is now
  (category, key, workspace_hash), so `perseus_vault_share`/`perseus_vault_federate` copy
  instead of clobbering the source row (#342, closes #339)
- Dashboard (web) endpoints workspace-scoped + hardened, with test
  coverage (#346)
- Build break on main — `list_entities` arity after #346 (#349)

### Performance
- Batched `graph_expand` hydration + cached consolidate trigram sets (#340)
- Sign-signature Hamming prefilter for dense search at scale — new `emb_sig`
  column, backfilled by the v6 schema migration (#347)

## [2.12.0] - 2026-07-01

### Added
- `perseus-vault prepare` — pre-turn auto-injection of relevant memories
  (PMB-inspired) (#336)

## [2.11.1] - 2026-07-01

### Fixed
- `perseus_vault_remember`/`perseus_vault_recall` reject explicit JSON `null` on optional
  fields instead of misbehaving (#334, closes #330)

## [2.11.0] - 2026-07-01

### Added
- `perseus-vault connect` — one-command MCP client setup (PMB-inspired) (#333)

## [2.10.0] - 2026-07-01

### Added
- Follow-rate efficacy scoring: `perseus_vault_follow` records whether an entity was
  actually followed or missed; `follow_rate`/`efficacy_status` feed decay so
  ignored rules decay out of recall (#332)
- `perseus_vault_consolidate`: merge overlapping/duplicative entities into durable,
  evidence-tracked observations (#327)

## [2.9.0] - 2026-07-01

### Changed
- **Product rename: Perseus Vault → Perseus Vault.** "Perseus Vault" collided with an active
  commercial competitor (perseus_vault.tools) plus several other unrelated AI-memory
  products and open-source projects already using that exact name — a repeat
  of the earlier Perseus Vault naming collision. The crate and `[[bin]]` are now
  `perseus-vault`; the default database for fresh installs is
  `~/.perseus-vault/data/perseus-vault.db` (an existing `perseus-vault.db` or `perseus_vault.db` at
  that path is still used automatically, in that fallback order, so upgraders
  keep their data — see `default_db_path()` in `src/main.rs`). Every
  `perseus_vault_*` MCP tool is now additionally registered under a `perseus_vault_*`
  name (on top of the existing `perseus_vault_*` alias from the prior rename) — all
  three names dispatch to the same handler, so existing MCP host configs
  calling `perseus_vault_remember`/`perseus_vault_recall`/`perseus_vault_remember`/etc. keep working
  unchanged. `perseus-vault doctor`/`--help` output now refers to the
  `perseus-vault` binary. The installer (`scripts/install.sh`) and Dockerfile
  install `perseus-vault` as the primary binary and add `perseus_vault`/`perseus_vault`
  symlinks for backward compatibility with existing scripts and MCP configs.
  Internal-only Rust identifiers (`PerseusVaultGrpcServer`, the `perseus_vault.v1` proto
  package, the MCP Registry `server.json`/Docker LABEL identity string) are
  intentionally left unchanged — those are wire-protocol/registry contracts
  external clients depend on by their literal names, not brand-facing text,
  and renaming them is a separate breaking-change decision to schedule on its
  own timeline.

### Breaking (soft — back-compat aliases provided)
- Fresh installs now default to `perseus-vault.db` instead of `perseus-vault.db`/
  `perseus_vault.db`. Existing databases at the old paths are auto-detected and used
  as-is (no migration needed), but new installs on a machine with no prior
  database will create the new filename. Set `--db`/`PERSEUS_VAULT_DB_PATH`
  explicitly if you need a specific path.

## [2.8.0] - 2026-06-30

### Changed
- **Product rename: Perseus Vault → Perseus Vault.** Avoids a trademark/SEO collision with
  Grafana Perseus Vault and a same-niche competitor also named Perseus Vault. The crate and
  `[[bin]]` are now `perseus_vault`; the default database for fresh installs is
  `~/.perseus-vault/data/perseus-vault.db` (an existing `perseus_vault.db` at that path is still used
  automatically, so upgraders keep their data — see `default_db_path()` in
  `src/main.rs`). Every `perseus_vault_*` MCP tool is now also registered under the
  equivalent `perseus_vault_*` name — both dispatch to the same handler, so existing
  MCP host configs that call `perseus_vault_remember`/`perseus_vault_recall`/etc. keep working
  unchanged during the transition. `perseus_vault doctor`/`--help` output now refers to
  the `perseus_vault` binary. Internal-only Rust identifiers (`PerseusVaultGrpcServer`, the
  optional `grpc` feature's generated `PerseusVault`/`PerseusVaultServer` proto types) are
  renamed to their `Perseus Vault` equivalents with no back-compat surface, since
  nothing outside the binary depends on them.

### Fixed
- **`layer` filter on `perseus_vault_recall` now actually filters (#269 follow-up).** The
  `layer` recall parameter was accepted but never applied — `RecallParams.layer`
  was a dead field. It now filters by biomimetic layer in all three modes:
  keyword (`fts5_search`) and BM25 (`fts5_bm25_search`) pre-filter in-query, and a
  mode-agnostic post-filter in `recall()` covers the dense arm of dense/hybrid
  (which scores vectors without `RecallParams` access). Aliases world/episodic/
  semantic are normalized to core/buffer/working at the tools layer.

### Added
- **`perseus_vault_history` tool (code-review follow-up).** The bi-temporal `history_versions`
  reader (v2.4.0) was complete and tested but no tool exposed it — you could time-travel
  to one instant via `perseus_vault_as_of` but couldn't list a fact's full version trail. Wired a
  `perseus_vault_history` tool that returns all superseded versions of a (category, key), newest
  first. Tool count 45 → **46**; README badge/table/section, `server.json`, and
  `CLAIMS-AUDIT.md` reconciled (they had drifted to 44/43).

### Removed
- **Dead `EncryptionManager::decrypt`.** Fully superseded by `decrypt_body` (the
  legacy/auth-failure-classifying variant); the old method had zero callers and was the
  exact footgun the security fix replaced. Removed so it can't be reintroduced.

- **`perseus_vault doctor` + verified client compatibility matrix (#272).** New `perseus_vault doctor`
  subcommand validates the local install (binary path, db path) and prints the MCP
  stdio config plus a compatibility matrix for Claude Desktop, Claude Code/Hermes,
  Cursor, Windsurf, VS Code+Continue.dev, Zed, and Codex CLI. Added a "Works With
  Every MCP Client" table to the README and copy-paste config snippets in
  `docs/clients/`. Perseus Vault is a standard MCP stdio server, so the same command works
  everywhere — this documents and self-checks it.
- **`include_confidence` on `perseus_vault_recall` (#287).** Opt-in (default false): each result
  gains a normalized `confidence` (0.0–1.0) rolled up from rank, trust (verified/certainty),
  and decay — a single number for callers/UIs instead of eyeballing raw signals. Purely
  presentation-layer; ranking math and existing snapshots are unchanged.

### Security
- **Decryption failures no longer silently return ciphertext.** On an encrypted DB,
  the read path (`entity_from_row`), FTS reindex, and the history content-change
  check used `decrypt(...).unwrap_or(raw)`, so any authentication failure — wrong
  key, or AAD-mismatched / tampered ciphertext (exactly what AES-256-GCM + AAD exist
  to detect) — was swallowed and the raw ciphertext was returned/indexed as if it
  were the plaintext body. That nullified the integrity guarantee: an attacker who
  could write to the DB file could tamper with a body and have it surface
  undetected. New `EncryptionManager::decrypt_body` classifies the input as
  decrypted plaintext, a legacy plaintext row (a real JSON body is never valid
  base64, so mixed DBs still work), or an authentication failure — and read paths
  now refuse to return the bytes on failure (a clear error sentinel + stderr warning
  for recall; an empty FTS entry so ciphertext is never indexed). Regression tests
  cover roundtrip, legacy-plaintext passthrough, and tamper / wrong-AAD / wrong-key
  rejection.

## [2.7.0] - 2026-06-28

### Distribution
- **Published to the Official MCP Registry (#270).** Fixed `server.json` (valid
  `oci` package on GHCR, current version and 43 tool count, dropped a stale
  install line) and added the OCI ownership label to the Docker image, so Perseus Vault
  is discoverable at registry.modelcontextprotocol.io and the directories that
  crawl it (Glama, PulseMCP, mcp.so).

## [2.6.0] - 2026-06-28

Round-3 hardening & efficiency: a data-loss fix on encrypted databases, an
ingest DoS guard, a lean-build injection fix, and recall-quality + perf
improvements.

### Changed
- **Hybrid recall over-fetches each arm before RRF fusion.** The dense and BM25
  keyword arms were each pre-truncated to `limit` *before* being fused, so a hit
  ranked just past `limit` in one arm but strong in the other — or one that lands
  just past `limit` in *both* yet would have the best *fused* score — could never
  enter fusion. Each arm is now fetched at a larger candidate pool (≈`5×limit`,
  capped) and RRF truncates to `limit` afterward. Strictly a recall-quality
  improvement; still fully read-only and byte-deterministic (verified by the
  existing idempotency/#125 tests + a new `hybrid_over_fetches_arms_before_fusion`
  test that pins the cross-arm consensus hit). The `perseus_vault-recall-mini` headline
  metrics are unchanged (24 docs saturate at `limit=10`), but the benchmark
  signature updates as the fused tail re-orders.
- **Conflict scan window is now an explicit, wider constant.** `detect_conflicts`
  / `resolve_conflicts` hard-coded a `LIMIT 200` candidate window (the O(window²)
  pairwise scan only ever looked at the 200 most-recently-accessed entities per
  call). Replaced the magic number with a documented `CONFLICT_SCAN_WINDOW` (500),
  widening coverage; still paged by `offset`.

### Performance
- **Scalar `dense_search` fallback precomputes the query norm once.** The
  non-`bundled-embeddings` (lean-build) cosine path recomputed the query vector's
  norm for every candidate; it is constant across a search, so it is now computed
  once and only the dot product + candidate norm are per-row. No effect on the
  default (vectorized ndarray) build.

### Fixed
- **`perseus_vault_reindex` no longer breaks keyword search on encrypted databases.**
  `reindex_fts` did a raw `INSERT … SELECT body_json`, which on an encrypted DB
  copied **ciphertext** into the FTS5 index — silently breaking all keyword and
  hybrid recall until re-ingest (the recovery tool corrupted the very index it was
  meant to rebuild). It now decrypts each body (AAD `category:key`) and indexes the
  plaintext, matching what `remember` writes. Unencrypted DBs keep the fast bulk
  copy. Regression test added.

### Security
- **Bounded file size for `perseus_vault_ingest_file` (#236 hardening).** Document ingestion
  read the entire file into memory with no size limit, then copied the text into a
  JSON body and the FTS index — a single huge or maliciously-sized file could OOM
  the server (denial of service). Ingestion now rejects files larger than a
  configurable cap (`PERSEUS_VAULT_MAX_INGEST_BYTES`, default 50 MiB) **before** reading,
  for plaintext, DOCX and PDF alike. Regression test added.
- **Python embedding fallback no longer interpolates text into its script.** The
  lean-build ONNX fallback (`generate_with_python`) escaped only `\` and `'` when
  embedding the (agent/user-controlled) text into a `python3 -c` source string, so
  a newline or other control character could break out of the string literal — a
  code-injection / DoS hazard. The tokenizer path, model path and text are now
  passed as **`argv`** (never parsed as code). Affects only `--no-default-features`
  builds (the default uses the in-process ONNX runtime).

## [2.5.0] - 2026-06-27

Bi-temporal facts, completed: conflicting facts can now be actively resolved
(not just detected), with the loser superseded into history rather than deleted.

### Added
- **Opt-in conflict invalidation (#253).** `perseus_vault_conflicts` gains `resolve=true`:
  the lower-certainty side of a clear conflict is invalidated — superseded into
  `entity_history` and removed from the live table, so it drops out of recall but
  stays reversible and time-travelable via `perseus_vault_as_of`. Conservative by design:
  `dry_run` defaults to **true** (an accidental `resolve` previews, never mutates),
  and pairs whose certainties are within `certainty_margin` (default 0.2) are
  skipped as ambiguous. Detection (`resolve=false`) is unchanged and remains the
  default. New `Database::invalidate_entity` / `Database::resolve_conflicts`.

### Tested
- **History-resurrection invariant guard (#257).** Locks in that superseded
  versions and conflict-invalidated losers (both in `entity_history`) are never
  resurfaced by `decay_tick` or `recall` — the architecture already guarantees
  this; the guard fails loudly if a future change breaks it.

## [2.4.0] - 2026-06-27

Bi-temporal facts: Perseus Vault now keeps a fact's prior versions when it changes and
can answer "what did we believe at time T?" — pure SQLite, local, no cloud.

### Added
- **Bi-temporal fact history (#249, #250, #251).** When `remember()` overwrites
  an existing `(category, key)` with new content, the prior version is now
  snapshotted into a new `entity_history` table instead of being lost. Each
  entity gains two time axes — **valid time** (`valid_from`/`valid_to`) and
  **transaction time** (`recorded_at`/`invalidated_at`) — plus
  `supersedes`/`superseded_by` links. The live `entities` table stays
  one-row-per-key (its `UNIQUE(category, key)`, recall, and dedup paths are
  untouched), so default recall remains live-only by construction. An identical
  re-assertion creates no version (idempotent, compared on plaintext).
- **`perseus_vault_as_of` tool + `Database::as_of(category, key, as_of_unix_ms)`.**
  Bi-temporal time-travel: returns the version of a fact that was live at a past
  instant (or `found=false` if it had not been recorded yet). Brings the MCP
  tool count to **43**.

### Changed
- `recorded_at_unix_ms` is now set to `created_at_unix_ms` on insert; the
  `user_version` 1→2 migration backfills it for existing rows and adds the
  bi-temporal columns + the `idx_entities_invalidated` live-fact index.

### Documentation
- Reconciled the README tool count (badge / comparison table / section header)
  from a stale **40** to the actual **43**, adding the missing `perseus_vault_extract`,
  `perseus_vault_ingest_file` (both shipped in 2.3.0) and `perseus_vault_as_of` rows.

## [2.3.0] - 2026-06-27

Local, offline knowledge tooling — structured extraction and multimodal document
ingestion — plus a reproducible recall-quality benchmark and a relevance-aware,
deterministic hybrid retrieval path.

### Added
- **Local multimodal document ingestion (#236).** New `perseus_vault_ingest_file` tool
  extracts a document's text **locally** (no cloud, no network) and stores it as a
  recallable entity. Plaintext / markdown / structured-text work in any build;
  **DOCX and PDF** are supported when built with the new optional
  `--features multimodal` (pulls `zip` + `pdf-extract`), keeping the lean default
  binary dependency-free. Brings the MCP tool count to **42**.
- **Local knowledge extraction (#234).** New `perseus_vault_extract` tool turns raw text
  (or a stored entity) into structured items — facts, preferences, temporal
  events, episodes — via a fully **local, deterministic, rule-based** extractor:
  no cloud LLM, no embedding/API call, no network (unlike GoodMem/Synap, which
  require a Gemini key). **Read-only and strictly opt-in** — the remember/recall
  paths and the zero-dependency story are unchanged. An `Extractor` trait is the
  plugin point for future strategies (`strategy: "none"` is a no-op). Brings the
  MCP tool count to **41**.
- **Reproducible offline recall-quality benchmark (#247).** New `benchmark/recall/`
  measures recall@k / MRR across `fts5` / `dense` / `hybrid` modes by driving the
  real binary over MCP stdio with the **bundled** ONNX model — no network, no API
  key, no LLM — and emits a signed, re-runnable `report.json`. On the
  paraphrase-heavy `perseus_vault-recall-mini` set the offline dense model reaches **91.7%
  recall@1 / 100% recall@5**, making the local-first promise measurable.

### Changed
- **Relevance-aware, deterministic hybrid recall (#247).** The hybrid (Reciprocal
  Rank Fusion) keyword arm now drops stopwords and ranks by **BM25 relevance**
  instead of popularity, is dropped entirely when it finds no content match, and
  is fused at a reduced dense-primary weight — so a paraphrase query no longer
  dilutes a confident dense ranking. RRF breaks score ties by entity id and the
  hybrid recall path is fully read-only, making all three modes **byte-stable
  run-to-run**. Hybrid recall@1 on `perseus_vault-recall-mini`: **20.8% → 87.5%** (MRR
  0.44 → 0.92).

### Documentation
- **Threat model + encryption spec (#246).** Added `docs/THREAT-MODEL.md` and
  `docs/ENCRYPTION.md` and corrected SECURITY.md overclaims. AES-256-GCM encrypts
  only `entities.body_json`; the FTS5 index and metadata are **plaintext** (pair
  with OS disk encryption).

## [2.2.1] - 2026-06-27

### Fixed
- **Docker/Alpine image builds again (#242).** The bundled-embeddings default
  (#237/#238) broke the musl Docker build — `ort` (ONNX Runtime) prebuilt
  binaries are glibc-only and the download chain needs `openssl-sys`, absent on
  Alpine. The Firecracker/sandbox image now builds **lean** (`--no-default-features`),
  restoring a working static-musl binary and the GHCR publish. (Native binaries
  remain bundled-by-default; a semantic-search Docker image would need a glibc base.)

## [2.2.0] - 2026-06-27

Local-first semantic memory, now true out of the box and on every platform, plus
the first time-aware retrieval control. The headline since `2.1.0`: dense/hybrid
search works with zero config and zero network by default.

### Added
- **Time-aware / recency-boosted hybrid recall (#235).** `perseus_vault_recall` accepts an
  optional `recency_half_life_secs` for `mode: "hybrid"`. When set, each fused
  (RRF) result's score is multiplied by `0.5^(age / half_life)` based on the
  memory's creation time, so recent context outranks older but lexically/semantically
  similar hits. **Default off** — omitting it preserves the existing relevance-only
  ranking exactly. Fully local, no new dependency; memories with no creation
  timestamp are never penalized.
- **Offline dense/hybrid search out of the box (#237).** A quantized
  all-MiniLM-L6-v2 model (int8, ~23 MB, 384-dim) is now fetched once by `build.rs`
  and **compiled into the binary**, and the embedding backend is **enabled by
  default**. Semantic recall works with zero config and zero network — no Ollama,
  no API key, no first-run model download — making the local-first / fully-offline
  promise literally true. Build a lean binary without the embedding stack via
  `cargo build --no-default-features`.

### Fixed
- **Native ONNX embedding now passes `token_type_ids`.** The `ort` inference path
  sent only `input_ids` + `attention_mask`; the (quantized) BERT graph requires
  the `token_type_ids` input (all-zeros for a single sequence), so native
  embedding failed at runtime. Now passed explicitly.

### CI
- The default build (now bundled-embeddings) is built **and tested** on **Linux,
  Windows MSVC, and macOS** (#239) — including an end-to-end test that runs real
  inference through the compiled-in model — confirming the single-binary
  semantic-search claim on every platform a developer runs. Added a `lite-build`
  job guarding `--no-default-features`.

[2.2.1]: https://github.com/Perseus-Computing-LLC/perseus-vault/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/Perseus-Computing-LLC/perseus-vault/compare/v2.1.0...v2.2.0
