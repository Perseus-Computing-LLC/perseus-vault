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

## Status — 2026-08-25

- **Latest release:** `v2.23.1` (published 2026-08-23).
- **`main`:** current post-release head includes provider-native source identity and deterministic declared-edge ingestion with support attestation; the issue-backed forward backlog is listed below.
- **MCP registry:** the exact tool count is intentionally **not maintained as roadmap prose**. Derive it from `src/mcp.rs` with `python3 scripts/registry_metadata_check.py`; CI validates the canonical registry and synchronized public metadata surfaces.
- **In one line:** the original v0.1 through v2.0 platform plan has shipped; this document tracks issue-backed work and integrity rules without inventing delivery dates or freezing a changing registry count.

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
- Release metadata and the source-derived canonical registry are CI-checked; the exact
  count is intentionally generated at verification time rather than copied into this roadmap.
  Current governance, provenance, benchmark-custody, and retrieval-remediation hardening is
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

## Active roadmap — open issue set (snapshot 2026-08-25)

**Theme:** prove memory quality, preserve provenance, and make durable knowledge serveable without
turning Markdown or a third-party benchmark into the source of truth. The order below is a
dependency-aware execution recommendation, not a promise of dates. Linked issues remain the
authoritative acceptance criteria.

**Live queue at refresh:** six open issues and no open pull requests. The shipped prerequisites
[#1132](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1132),
[#1133](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1133),
[#1134](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1134),
[#1135](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1135),
[#1138](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1138),
[#1141](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1141), and
[#1142](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1142) are not re-queued.

### Registry count policy

Do **not** use the exact MCP tool count as a roadmap KPI or hand-maintained claim. Do keep the
exact count as an automated registry-integrity invariant:

- derive it from the canonical registry in `src/mcp.rs`;
- run `python3 scripts/registry_metadata_check.py` at every metadata or publication boundary;
- require the synchronized README, claims, manifest, and server metadata surfaces to pass;
- omit the changing number from durable roadmap prose and external claims unless a publication
  step generates and timestamps it directly from the check.

A count mismatch blocks metadata publication; it does not change the capability roadmap.

### Recommended starting point

Use one implementation track and one measurement track:

1. **Selection observability first:** [#1140 — bounded context-selection decisions](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1140).
   Explain candidate dispositions, reason codes, budgets, and replay fingerprints over the stable
   evidence representation.
2. **Graph validation second:** [#1143 — matched graph-context ablation](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1143).
   Compare graph-on/off under matched retrieval and context conditions; do not interpret an
   ablation before the selection/evidence receipt is inspectable.
3. **Separate intervention track:** [#1136 — receipt-conditioned bucket intervention](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1136)
   follows the existing evidence-ledger/replay contracts and should not be mixed into #1140.

Keep [#1105](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1105) parked until the
Amy/C3BM discussion, and keep [#1021](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1021)
and [#1061](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1061) outside this
provider-free implementation wave unless their separate custody and spend gates are reopened.

### Execution chunks

Each chunk below is a small workstream, not one giant PR. Use one issue-shaped branch/PR per
contract, and serialize only the parts that share files or a schema boundary.

#### Chunk 0 — roadmap and registry hygiene (complete at this refresh)

- Remove stale hand-maintained tool-count prose from this roadmap.
- Reconcile the live issue set and mark #1132, #1133, #1134, #1135, #1138, #1141, and #1142 as shipped
  prerequisites rather than future work.
- Keep `scripts/registry_metadata_check.py` as the publication/CI source of truth; no new tool is
  added by this documentation refresh.

**Exit condition:** the roadmap describes the current queue without stale issue states or a frozen
registry number.

#### Chunk 1 — governed derived and verbatim evidence (complete; merged in `origin/main`)

- **[#1135](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1135):** the typed
  `derived` and `verbatim` lanes, stable source-group identity, evidence/source-span resolution,
  temporal validity, workspace/agent visibility, stale/superseded/tombstone exclusion, and a
  bounded union path for answer assembly are shipped.
- Keep unverified verbatim material explicitly untrusted; it may be returned as evidence only under
  the governed opt-in path and may never silently become an authoritative derived fact.
- The hash-only receipt covers the exact selected rows/spans, lane configuration, source groups,
  exclusions, token accounting, scope, and temporal anchor. Raw bodies and prompts are excluded.
- The existing default recall request/response bytes remain unchanged; no SQLite schema migration
  was introduced.

**Exit condition:** derived-only, verbatim-only, and union fixtures return the same governed
answer-facing evidence set on replay; duplicates are source-group deduplicated; insufficient
budgets are explicit; scope, temporal, lifecycle, correction, and malformed-reference cases fail
closed; the default path is byte-compatible.

#### Chunk 2 — bounded context-selection decisions (active; this branch)

- **[#1140](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1140):** expose opt-in
  candidate dispositions, reason codes, lane/source-arm decisions, budgets, and replay
  fingerprints over #1135's shipped evidence contract. The projection is available on fused recall
  and context serving while the default response remains unchanged.

**Exit condition:** an inspector can distinguish candidate generation, governance exclusion, lane
selection, budget truncation, and answer assembly without receiving raw prompts or hidden content.

#### Chunk 3 — matched graph-context diagnosis

- **[#1143](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1143):** compare graph-on
  and graph-off under the same retrieval mode, reader, context budget, and judge; report declared
  edge support, path attribution, evidence coverage, latency, and cost as separate dimensions.

**Exit condition:** the diagnostic is provider-free at its first gate, replayable, and cannot be
presented as model-internal causality or as a third-party benchmark claim.

#### Chunk 4 — controlled intervention

- **[#1136](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1136):** seal the
  evidence/replay receipt before intervention, block re-entry through alternate lanes or fallbacks,
  and compare receipt, random, and matched-size controls.

**Exit condition:** intervention outputs are tied to exact receipts and remain separate from the
frozen default and from efficacy claims.

#### Chunk 5 — parked or separately authorized measurements

These remain visible but must not be pulled into the implementation wave:

- **[#1105](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1105):** capture the
  resource-envelope contract now; implementation planning remains parked until the partner
  discussion on or after 2026-08-31.
- **[#1061](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1061):** any RECON
  recheck needs a frozen revision, resolved protocol discrepancies, cost/token guards, and fresh
  explicit authorization; it is not a positive efficacy verdict.
- **[#1021](https://github.com/Perseus-Computing-LLC/perseus-vault/issues/1021):** retain the
  accepted frozen-default result as its own row and keep the residual runs/custody work separate
  from plain-prompt or experimental results.

### Tool-call-sized operating plan

- **One preflight per chunk:** read the live `origin/main` SHA, target issue bodies/states,
  dependency states, and the exact files likely to change; save a compact progress row outside the
  repository rather than repeatedly re-fetching full issue pages.
- **One contract per PR:** run the focused gate and the repository-declared full gate once on the
  exact tree, then publish/verify that PR at its exact head. Do not combine unrelated benchmark,
  security, and storage changes to reduce round trips.
- **Serialize shared surfaces:** treat the shipped #1135 contract as the prerequisite for #1140 and
  #1143; keep #1136 separate even though it consumes the same evidence/replay vocabulary. Rebase
  every dependent branch onto the verified live `main` before review; old CI or review evidence is
  stale after a base advance.
- **Parallelize only disjoint work:** documentation/spec work may proceed beside pure fixture work,
  but merge order remains contract-first when modules or schemas converge.
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
