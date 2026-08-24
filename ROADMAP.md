# Perseus Vault Roadmap

## What Perseus Vault Is

A local-first persistent memory engine for AI agents. MCP-native. Single static binary.
Zero runtime dependencies. Structured entity model with journal events and state management.

## What Perseus Vault Is Not

- Not a knowledge graph or entity extraction engine
- Not a cloud service or SaaS
- Not a replacement for a vector database
- Not dependent on any specific AI assistant or framework

---

## Status — 2026-08-23

- **Latest release:** `v2.23.1` (published 2026-08-23).
- **`main`:** current post-release head includes the latest benchmark/retrieval remediation; the issue-backed forward backlog is listed below.
- **MCP tools:** **170 canonical tools**, spanning entities, search/RAG, journal, state, graph, lifecycle, multi-agent/federation, governance, operations, and benchmark/diagnostic surfaces.
- **In one line:** the original v0.1 through v2.0 platform plan has shipped; this document now tracks the open, issue-backed work without inventing delivery dates.

> **Doc hygiene note:** prior revisions of this file listed shipped capabilities
(peer federation, multi-agent scoping, gRPC, offline embeddings) as "future," and
carried fabricated quarterly milestones through 2031 — several describing tools
that already exist (`perseus_vault_synthesize`). That has been removed. Peer
federation is intentionally disabled; explicit export/import remains supported.
> Forward-looking work that is not committed now lives under **Exploratory** with
> no dates. The canonical roadmap is this file; `docs/ROADMAP.md` points here.

---

## Shipped

### v0.1 — MVP ✅ (2026-05)
- SQLite + FTS5 keyword search with LIKE fallback
- MCP JSON-RPC 2.0 stdio server; single static binary, bundled SQLite, zero runtime deps

### v0.2.0 — Structured entity model ✅ (2026-06-10)
- Three-table schema: **entities** (idempotent by `UNIQUE(category, key)`, FTS5-indexed),
  **journal** (append-only `evaluated/acted/forward` events), **state** (key-value + TTL)
- Entity tools (`remember`, `recall`, `forget`, `link`/`unlink`), journal (`journal`, `timeline`),
  state (`state_set/get/delete/list`), management (`stats`, `compact`, `migrate`, `context`, `workspace_list`)
- Became the sole persistent-memory backend for Perseus (Sibyl dependency removed)

### v1.0.0 — Intelligence & distribution ✅ (2026-06-15)
- **Confidence decay:** Ebbinghaus decay, `buffer → working → core` layering, trigram near-dup detection, `perseus_vault_decay`
- **Hybrid search:** FTS5 + dense embeddings + Reciprocal Rank Fusion; Porter-stemming query expansion; `perseus_vault_embed`
- **Synthesis:** chain traversal (`perseus_vault_traverse`), quality scoring (`perseus_vault_score`), conflict detection (`perseus_vault_conflicts`), RAG (`perseus_vault_ask`)
- **Vault & portability:** `.md` export/import (`perseus_vault_vault_export`/`import`) — human-readable, git-trackable, Obsidian-compatible
- **Connectors:** GitHub issues + file watcher via `perseus_vault_ingest`
- **Security & ops:** AES-256-GCM encryption at rest, web dashboard, Smithery/Glama/mcpservers.org listings

### v1.1 – v2.0 — Ecosystem, multi-agent, platform ✅ (2026-06)
- **Ecosystem:** framework adapters for **LangGraph, CrewAI, AutoGen** (`integrations/`), an
  **Obsidian plugin**, SSE/HTTP transport for non-stdio hosts, Docker image, and a one-line
  installer (`curl -sSf … | sh`, `v2.0.1`)
- **Multi-agent & transfer boundary:** workspace scoping (`workspace_hash`), agent identity
  (`agent_id`), per-entity `visibility`, and reviewed file-based export/import. Peer federation is
  intentionally disabled pending a governed transfer contract.
- **Local/offline embeddings:** ONNX path via `ort` — hybrid search without an external embedding service
- **Platform (`v2.0.0`):** gRPC transport alongside MCP, and a cryptographically-chained audit log
- **Additional tools since the docs last counted:** `autocohere`, `bench`, `correct`, `supersede`,
  `synthesize`, `share`, `purge`, `maintenance`, `recall_when`, `get_entity` — **40 tools total**

### v2.1.0 — Performance & Reliability ✅ (2026-06-26)
- **Trust-aware recall:** `perseus_vault_recall` ranks verified sources above unverified drafts
  (uses `verified`/`source`/`certainty`; on by default at a low weight). Consistent with
  `perseus_vault_conflicts`.
- **CLI:** top-level `--db` accepted when running the server directly (`perseus_vault --db <path>`),
  matching the documented MCP host config.
- **Performance & reliability:** HTTP/SSE connection pool (concurrent reads under WAL),
  cached ONNX session/tokenizer, `dense_search` top-k hydration, recall-ranking index and
  batched side-effects; `bundled-embeddings` made to link on Windows MSVC.

### v2.2.0 — Bundled/offline semantic release ✅
- **Offline embeddings bundled by default (#237/#238):** the quantized all-MiniLM-L6-v2 model
  is compiled into the binary and the embedding backend is on by default — dense/hybrid search
  works with zero config and zero network. Lean build via `--no-default-features`.
- **Time-aware / recency-boosted recall (#235):** optional `recency_half_life_secs` weight on
  the hybrid RRF fusion step, default off, fully local.
- **All-platform CI (#239):** the bundled default is built and tested (with real inference) on
  Linux, Windows MSVC, and macOS.

### v2.23.1 — Current release line ✅
- Release metadata and the source-derived canonical registry are aligned at **170 tools**;
  current governance, provenance, benchmark-custody, and retrieval-remediation hardening is
  documented in `CHANGELOG.md`.

---

## Now — Foundation ✅ (done as of `2.2.0`)

**Theme: "what we ship matches what we say."** Stabilize the base before adding capability.

- ✅ **Single source of version truth:** `Cargo.toml`, the README badge, git tags, and this doc agree.
- ✅ **Doc accuracy:** tool count corrected (40), README claims audited against code.
- ✅ **Cross-platform CI:** Linux, Windows MSVC, and macOS all first-class in the matrix (#239).
- ✅ **Release discipline:** `CHANGELOG.md` adopted, semver, clean releases (`2.1.0`, `2.2.0`).
- ✅ **Bundled-by-default offline embeddings (#237/#238):** model compiled into the binary —
  zero-network semantic search out of the box.

## Next — Remaining platform hardening

The genuinely-unshipped pieces of the "Perseus Vault as infrastructure" goal:

- **Clustering / HA:** leader election and read replicas for high-availability deployments
  (the one part of the v2.0 platform theme not yet built).
- **Local knowledge extraction (#234):** rule-based extractor + `perseus_vault_extract` tool shipped
  on `main` (local, deterministic, opt-in, no cloud key). A model-based extractor behind the
  same `Extractor` trait is the future increment.
- **Local multimodal ingestion (#236):** `perseus_vault_ingest_file` + an optional `multimodal`
  feature (DOCX/PDF text extraction) shipped on `main` — local-only, lean default unchanged.
- **Scale:** 100K+ entity stress tests with documented recall latency budgets.
- **Governed transfer (future):** peer federation remains uncommitted until authority,
  rollback/custody, conflict, and erasure propagation are specified and implemented.

## Active roadmap — open issue set (snapshot 2026-08-23)

**Theme:** prove memory quality, preserve provenance, and make durable knowledge serveable without
turning Markdown or a third-party benchmark into the source of truth. The order below is a
dependency-aware execution recommendation, not a promise of dates. The linked issues remain the
authoritative acceptance criteria.

### Recommended starting point

Start with two bounded tracks:

1. **Safety first:** [#1134](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1134)
   is the P1 security/control-plane gap. It is sufficiently isolated to implement without waiting
   for the retrieval work, and it prevents session splitting from bypassing task-level action
   composition and budget state.
2. **Evidence truth before experiments:** complete the no-spend publication/protocol fence of
   [#1133](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1133) and
   [#1138](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1138), then build the
   attribution gate in [#1132](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1132).

Do not start with a graph feature or a new paid benchmark run. The graph work has a wider storage,
provenance, and serving surface, while the benchmark issues explicitly require provider-free gates
and strict claim boundaries first. The documentation item can proceed as soon as the accepted
report/manifest is publishable; it does not authorize another run.

### Execution chunks

Each chunk below is a small workstream, not one giant PR. Use one issue-shaped branch/PR per
contract, and serialize only the parts that share files or a schema boundary.

#### Chunk 0 — claim and benchmark guardrails (start here; no provider calls)

- **[#1133 — refresh published LongMemEval claims](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1133):** publish the accepted frozen-default result only when its signed report and manifest are available; keep plain-prompt, official-CoT, historical mean, and the new single-run baseline as distinct rows.
- **[#1138 — cross-reader/cross-judge sensitivity matrix](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1138):** define the reader, judge, prompt-lane, depth, budget, denominator, and custody boundary before interpreting any model or cost delta.
- **[#1132 — category-specific answer-facing attribution](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1132):** build the deterministic per-question/category gate after the protocol schema is stable; keep it opt-in and separate from the frozen default.

**Exit condition:** public claims are hash-bound and correctly labeled, and provider-free reports
can distinguish missing evidence, selection/assembly failure, synthesis failure, temporal/version
failure, and provenance failure. No paid run is implied.

#### Chunk 1 — task-level AAR continuity (parallel-safe, but P1)

- **[#1134 — preserve task-level action lineage across sessions](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1134):** add the opt-in hash-bound lineage/continuation state, fail-closed transitions, bounded receipts, and the Ledger composition fixture.

**Exit condition:** continuation, reset/new authorization, stale/revoked state, scope/policy
mismatch, replay, and concurrent-head behavior are deterministic; raw prompts, memory bodies,
arguments, credentials, and sensitive outputs never enter the lineage record. This can run beside
Chunk 0 because it should not share the benchmark files.

#### Chunk 2 — provider/source and graph substrate

Implement these in order because declared topology needs stable source identity and revision
semantics:

- **[#1141 — provider-native source identity and event lifecycle](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1141):** source/event envelopes, update and reply lineage, visibility/scope, revision digests, and governed deletion/tombstones.
- **[#1142 — deterministic declared-edge ingestion and support attestation](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1142):** source-keyed manifests, stable entity/relation IDs, declared-versus-extracted-versus-inferred origin, support/attestation states, idempotent replacement, and fail-closed conflicts.

**Exit condition:** source and edge references resolve to exact revisions/digests and scope;
replay, collision, stale, deletion, and weak-support behavior is tested before any graph utility
claim is made.

#### Chunk 3 — answer-facing evidence and inspection

Keep the evidence representation ahead of its explanation surface:

- **[#1135 — governed derived and verbatim evidence lanes](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1135):** join derived facts to source spans without double-counting or promoting unverified verbatim evidence.
- **[#1140 — bounded context-selection decisions](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1140):** expose opt-in candidate dispositions, reason codes, budgets, and replay fingerprints without changing default response bytes or leaking raw content.

The recommended order is #1135 then #1140 so the inspector can describe lane, source-group,
evidence, scope, and conflict state consistently. If implementation proves the file boundaries
are disjoint, the work may be developed concurrently but should remain separate PRs.

#### Chunk 4 — controlled diagnostic interventions

- **[#1136 — receipt-conditioned bucket intervention](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1136):** follow #1132 and the existing evidence-ledger/replay contracts; seal the receipt before intervention, block re-entry through alternate lanes/fallbacks, and compare receipt, random, and matched-size controls.
- **[#1143 — matched graph-context ablation](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1143):** follow #1142, #1140, and #1138; compare graph-on/off under the same retrieval mode and context budget, and report declared-edge support, path attribution, evidence coverage, and cost separately.

**Exit condition:** all interventions are deterministic, provider-free for the first gate, and
cannot be presented as model-internal causality or as a third-party benchmark claim.

#### Chunk 5 — parked or separately authorized measurements

These remain visible on the roadmap but must not be pulled into the implementation wave:

- **[#1105 — reproducible edge resource envelopes](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1105):** capture the contract now; the issue explicitly parks implementation planning until the Amy/C3BM partner discussion on or after 2026-08-31.
- **[#1061 — RECON triathlon run](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1061):** the bounded recheck is not a positive efficacy verdict; a full run needs a frozen revision, resolved protocol discrepancies, cost/token guards, and fresh explicit authorization.
- **[#1021 — LongMemEval three-run refresh](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1021):** one accepted 81.4% frozen-default run is already recorded, but Runs 2/3 were not started. Keep the issue open for that residual only; do not blend it with plain-prompt or experimental results and do not infer spend authorization.

### Tool-call-sized operating plan

- **One preflight per chunk:** read the live `origin/main` SHA, target issue bodies/states,
  dependency states, and the exact files likely to change; save a compact progress row outside the
  repository rather than repeatedly re-fetching full issue pages.
- **One contract per PR:** run the focused gate and the repository-declared full gate once on the
  exact tree, then publish/verify that PR at its exact head. Do not combine unrelated benchmark,
  security, and storage changes to reduce round trips.
- **Serialize shared surfaces:** use #1138 → #1132 → #1136 for the shared benchmark harness and
  #1141 → #1142 for source/graph schema work. Rebase each dependent branch onto the verified live
  `main` before review; old CI or review evidence is stale after a base advance.
- **Parallelize only disjoint work:** #1133 can run as a docs-only slice beside #1134; later
  chunks may proceed concurrently only when changed-file sets and schema boundaries are disjoint.
- **Keep paid and cross-product work separate:** a green provider-free gate is evidence for
  implementation, not authorization to call a model or publish a new score. Every later canary/run
  gets its own custody, budget, and claim-boundary check.
- **Close out independently:** after merge, verify the exact default-branch SHA, artifact/content
  needles, PR metadata, issue state, and temporary-branch cleanup. Do not infer issue closure or
  publication from a successful merge response alone.

## Later — Gated & cross-product

- **Managed "Perseus Vault Cloud":** a hosted/multi-region option — only after the platform hardening above.
- **Billing for hosted tiers via Ledger:** explicitly **gated on Ledger reaching 1.0** (stable, frozen
  API + DB schema). No integration code before then, to avoid churn against a moving contract.

## Exploratory — directional, not committed (no dates)

Ideas we like and may pursue. Listed to capture intent, **not** to promise delivery or timing:

- Memory tiering (hot/warm/cold storage with automatic promotion/demotion)
- Proactive recall — pre-fetch relevant entities on task start instead of waiting to be asked
- Learned forgetting curves — decay parameters that self-tune per workspace/agent/type
- Causal memory graphs — entities linked by causation, traversable in both directions
- Multi-modal memory — image/audio/code entities with cross-modal recall
- Production CRDT sync across WAN with conflict resolution
- An open, versioned "Perseus Vault-compatible" memory standard + compliance suite
- Open memory benchmark pack and compliance suite for memory systems, with Vault semantics for provenance, time, correction, and supersession

---

## Design Principles

1. **Zero runtime dependencies.** The binary is self-contained.
2. **Offline-first.** All core operations work without internet.
3. **MCP-native.** Every feature ships as an MCP tool.
4. **Agent-first, not human-first.** Tools are designed for AI agents.
5. **Compose, don't integrate.** Perseus Vault does persistent memory; composes with Perseus, Obsidian, Git.
6. **Local-first, cloud-optional.** Run it anywhere; cloud features are additive.
