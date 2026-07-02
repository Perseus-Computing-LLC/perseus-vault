# Changelog

All notable changes to Perseus Vault (formerly Mimir/Mneme) are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- History retention mechanism (#398): entity_history can now be bounded via
  env knobs — `MIMIR_HISTORY_MAX_AGE_DAYS`, `MIMIR_HISTORY_MAX_VERSIONS_PER_KEY`
  (oldest-first per key), `MIMIR_HISTORY_MAX_BYTES` (globally oldest-first).
  **Every knob defaults OFF** — with none set, behavior is byte-identical to
  before (keep everything); enabling a bound is an explicit operator decision.
  Enforcement runs only in maintenance paths (`mimir_maintenance` `history`/
  `all`, `mimir_autocohere`, `mimir_prune scope='history'`), never on the
  write path.
- Tombstone roll-up compaction (#398, issue option 2 — default ON, disable
  via `MIMIR_HISTORY_TOMBSTONES=0` for hard delete): an evicted run of
  versions is replaced by ONE synthetic history row spanning
  [first_recorded_at, last_invalidated_at) carrying the rolled-up version
  count and a hash-chain digest of the evicted rows (successive passes merge:
  counts accumulate, digests chain). `mimir_as_of` at an instant inside a
  compacted window returns an explicit marker (`compacted: true`,
  `versions_compacted`, `digest`) instead of silently-wrong data; instants
  covered by surviving rows are answered exactly as before. The valid-time
  axis holds too: the tombstone carries the run's earliest effective
  `valid_from` (not first_recorded_at), so a retroactively-valid compacted
  version's window keeps answering `mimir_valid_at`/`mimir_bitemporal` —
  with the same explicit marker decoration — instead of flipping to None.
  Option 3 (export-then-delete to vault Markdown/JSONL) is deferred as a
  follow-up.
- `mimir_prune` gains `scope: 'history'` (#398) with per-call bound overrides
  (`max_age_days`, `max_versions_per_key`, `max_bytes`) and dry_run
  preview — reports the exact rows + bytes the real run would evict.
- `mimir_stats` reports history growth (#398): `total_history_rows`,
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
- `mimir_follow` accepts an optional `workspace_hash` (#396, the #338
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
  genuinely recoverable via `mimir_embed` batch mode
  (`WHERE embedding IS NULL`) or its next change. Deferral is within the
  existing contract — auto-embed already ran post-commit with non-fatal
  failures; a row simply doesn't surface in dense/hybrid search until
  embedded (now milliseconds later). Explicit `mimir_embed` stays
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
- `mimir_purge` now honors its own "actually remove" contract (#398): purging
  an archived entity also DELETEs every superseded version of it from
  `entity_history` (matched by id and by category/key/workspace, so versions
  written under earlier ids of the same key are erased too) and REDACTS
  journal rows referencing it — payload columns scrubbed in place,
  `event_type` stamped `redacted`. Journal rows are redacted rather than
  deleted because the audit chain hashes only (prev_hash, id, created_at);
  redaction removes every purged body from the log while
  `verify_audit_chain` stays valid end-to-end. Before this fix a GDPR-style
  forget-then-purge left all historical bodies readable via
  `mimir_history`/`mimir_as_of` and the journal append-only forever.
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
  (fresh store, dedup ON) 123.6s (pre, per #392) → 15.1s total (~8x). The opt-in `MIMIR_DEDUP_FTS_PREFILTER` path
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
- `mimir_cohere` no longer holds one writer lock for the whole grooming pass
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
- `mimir_autocohere`'s cohere step actually creates auto-links now (#412).
  `CohereParams`' derived `Default` gave `max_links = 0`, and autocohere
  builds its params with `..Default::default()` — so the link-candidate
  query ran with `LIMIT 0` and the graph-building half of scheduled
  maintenance had silently been a no-op since it shipped. A manual
  `Default` impl now carries the same `max_links = 20` budget the
  `mimir_cohere` argument path gets from its serde default;
  `promote_threshold`/`archive_threshold` keep their fall-through-to-
  constants sentinels, and explicit `mimir_cohere` args are unaffected.
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
- Recall-first context injection (#366): `mimir_context` / `prepare` default
  to `mode: on_demand` — a relevance-gated, budget-clamped block instead of
  the unconditional top-N dump. New `mimir_context` params: `query`, `mode`,
  `model`, `max_context_chars`; new `prepare` flags: `--max-context-chars`,
  `--model`, `--legacy-context`. Per-model budgets: 1500 chars default, 6000
  for "opus"-class hosts. Legacy dump is opt-in via `mode: "always_inject"`.
- Always-on set hard-capped (5 entities) under recall-first, with a
  documented overflow warning steering toward `recall_when` triggers (#366).
- GraphRAG over the link graph (#365): `mimir_communities` (deterministic
  community detection — label propagation with neighborhood-overlap weighting,
  or greedy one-level modularity "louvain"; pure Rust, no new dependencies),
  `mimir_community_summary` (extractive by default, optional LLM polish,
  materialized as a `community_summary` entity with `evidence_for` links,
  cached by member-set digest), and `mimir_global_recall` (breadth over
  community summaries, then depth into the best communities' members — cites
  entities across clusters instead of only the nearest one). Communities are
  persisted in a new `communities` table (schema v8); `mimir_stats` now
  reports `total_communities` and `graph_modularity`.
- `mimir_dream` — sleep-time LLM consolidation of episodic → semantic memory:
  clusters related cold memories per category, reflects over each cluster via
  the configured `--llm-endpoint`, and writes back provenance-linked semantic
  insights (`evidence_for` to every source, `derivation: "dream"`, idempotent
  by evidence-set hash, contradiction-aware, bounded budgets, dry-run;
  verified/importance-floored sources never archived). 53rd MCP tool (#364)
- **Bi-temporal memory — queryable valid-time axis (#363,
  SQL:2011 APPLICATION_TIME).** The `valid_from`/`valid_to` columns are no
  longer write-only: facts now carry a queryable application-time period
  ("when was this true in the world"), orthogonal to the existing
  transaction-time axis (`mimir_as_of`). New tools `mimir_valid_at`
  (what was actually true at instant T, per current knowledge) and
  `mimir_bitemporal` (the full 2-axis rectangle query: "as of transaction
  time T, what did we believe was true at valid time V"). Valid time is
  settable on `mimir_remember`/`mimir_correct` (`valid_from_unix_ms` /
  `valid_to_unix_ms`, defaulting to transaction time / unbounded);
  `mimir_supersede` closes the old fact's valid period. `mimir_recall` gains
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
  connection (`_with_conn` variants), including `mimir_embed`'s single-entity
  path; the same load now runs at ~4,200 req/s with zero errors. r2d2's
  checkout timeout is tunable via `MIMIR_POOL_TIMEOUT_MS`. A new
  `concurrency-gate` CI workflow pins the load test at 2× pool
  oversubscription plus the four concurrency hammer tests.
- decay_tick write amplification (#399): every tick rewrote every
  non-archived row even when nothing changed — 412MB of WAL per tick on a
  45MB database at 100k entities. Writes are now skipped when the recomputed
  score is within epsilon of the stored value (archive and layer-boundary
  crossings always write), so steady-state ticks write ~zero rows;
  `entities_updated` now reports rows actually written.
- `mimir_history` pagination (#403): the tool returned every full decrypted
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
  its edges, and concurrent `mimir_link` calls could lose edges to the
  unguarded read-modify-write. link/unlink now run under the writer lock and
  remember UNIONS caller links with stored links (dedup by target;
  stored relationship/weight win; `mimir_unlink` is the only removal path).
- invalidate_entity temporal-window corruption (#381): the fourth
  `entity_history` writer stamped `invalidated_at = now()` raw — an audited
  writer that legitimately set `recorded_at` ahead of the wall clock produced
  an INVERTED window, and a same-millisecond create+invalidate produced a
  zero-width window that `mimir_as_of` could never reconstruct. It now takes
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
  `mimir_bitemporal` at pre-supersede instants showed the expired fact as
  already deprecated). A same-status call (e.g. re-superseding an
  already-deprecated fact) still refreshes `archive_reason` in place,
  unversioned by design — a reason overwrite is operational metadata, not a
  knowledge change. A normal supersede now writes two snapshots: the audited
  close, then the audited flip.
- Supersede snapshot status fidelity (#375): `mimir_supersede` now closes the
  old fact's valid period BEFORE flipping its status to `deprecated`, so the
  #373 audit snapshot captures the true pre-supersede state — previously the
  snapshot baked `deprecated` in under the original transaction time, and
  `mimir_bitemporal` reconstruction at a pre-supersede instant showed the
  fact deprecated while it was still believed active. A failed close now
  also leaves the status untouched.
- Audited `set_valid_to` closes (#373): closing/tightening a fact's valid
  period (directly or via `mimir_supersede`) now snapshots the pre-close
  version to `entity_history` and advances the live row's transaction time —
  previously a close was invisible to `mimir_as_of`/`mimir_bitemporal`
  reconstruction, which reported the close even at transaction instants
  before it happened. Tighten-only acceptance semantics are unchanged, and a
  no-op call (an earlier stored close is kept) writes no snapshot.
- Bi-temporal audit gap (#371): an identical-body re-remember that moves the
  bounds of an already-CLOSED valid period (e.g. re-extending past a
  `mimir_supersede`/`set_valid_to` close) now snapshots the pre-change version
  to `entity_history` and advances the live row's transaction time, so
  `mimir_history`/`mimir_bitemporal` reconstruct both the closed period and
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
- `mimir_memories`: Anthropic `/memories` directory-convention adapter — file
  interface (`view`/`create`/`str_replace`/…) backed by vault entities (#345)
- Coldness-driven consolidation ("local dreaming") wired into autocohere (#350)

### Fixed
- Prompt-injection sanitization in `prepare` + unified decay/promote
  constants (#337)
- `workspace_hash` scoping for context/recall_when/prepare + write-path
  dedup (#338)
- Workspace-scoped entity identity — identity is now
  (category, key, workspace_hash), so `mimir_share`/`mimir_federate` copy
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
- `mimir_remember`/`mimir_recall` reject explicit JSON `null` on optional
  fields instead of misbehaving (#334, closes #330)

## [2.11.0] - 2026-07-01

### Added
- `perseus-vault connect` — one-command MCP client setup (PMB-inspired) (#333)

## [2.10.0] - 2026-07-01

### Added
- Follow-rate efficacy scoring: `mimir_follow` records whether an entity was
  actually followed or missed; `follow_rate`/`efficacy_status` feed decay so
  ignored rules decay out of recall (#332)
- `mimir_consolidate`: merge overlapping/duplicative entities into durable,
  evidence-tracked observations (#327)

## [2.9.0] - 2026-07-01

### Changed
- **Product rename: Perseus Vault → Perseus Vault.** "Perseus Vault" collided with an active
  commercial competitor (mneme.tools) plus several other unrelated AI-memory
  products and open-source projects already using that exact name — a repeat
  of the earlier Mimir naming collision. The crate and `[[bin]]` are now
  `perseus-vault`; the default database for fresh installs is
  `~/.mimir/data/perseus-vault.db` (an existing `perseus-vault.db` or `mimir.db` at
  that path is still used automatically, in that fallback order, so upgraders
  keep their data — see `default_db_path()` in `src/main.rs`). Every
  `mimir_*` MCP tool is now additionally registered under a `perseus_vault_*`
  name (on top of the existing `mneme_*` alias from the prior rename) — all
  three names dispatch to the same handler, so existing MCP host configs
  calling `mimir_remember`/`mimir_recall`/`mneme_remember`/etc. keep working
  unchanged. `perseus-vault doctor`/`--help` output now refers to the
  `perseus-vault` binary. The installer (`scripts/install.sh`) and Dockerfile
  install `perseus-vault` as the primary binary and add `mneme`/`mimir`
  symlinks for backward compatibility with existing scripts and MCP configs.
  Internal-only Rust identifiers (`MnemeGrpcServer`, the `mneme.v1` proto
  package, the MCP Registry `server.json`/Docker LABEL identity string) are
  intentionally left unchanged — those are wire-protocol/registry contracts
  external clients depend on by their literal names, not brand-facing text,
  and renaming them is a separate breaking-change decision to schedule on its
  own timeline.

### Breaking (soft — back-compat aliases provided)
- Fresh installs now default to `perseus-vault.db` instead of `perseus-vault.db`/
  `mimir.db`. Existing databases at the old paths are auto-detected and used
  as-is (no migration needed), but new installs on a machine with no prior
  database will create the new filename. Set `--db`/`MIMIR_DB_PATH`
  explicitly if you need a specific path.

## [2.8.0] - 2026-06-30

### Changed
- **Product rename: Mimir → Perseus Vault.** Avoids a trademark/SEO collision with
  Grafana Mimir and a same-niche competitor also named Mimir. The crate and
  `[[bin]]` are now `mneme`; the default database for fresh installs is
  `~/.mimir/data/perseus-vault.db` (an existing `mimir.db` at that path is still used
  automatically, so upgraders keep their data — see `default_db_path()` in
  `src/main.rs`). Every `mimir_*` MCP tool is now also registered under the
  equivalent `mneme_*` name — both dispatch to the same handler, so existing
  MCP host configs that call `mimir_remember`/`mimir_recall`/etc. keep working
  unchanged during the transition. `mimir doctor`/`--help` output now refers to
  the `mneme` binary. Internal-only Rust identifiers (`MimirGrpcServer`, the
  optional `grpc` feature's generated `Mimir`/`MimirServer` proto types) are
  renamed to their `Perseus Vault` equivalents with no back-compat surface, since
  nothing outside the binary depends on them.

### Fixed
- **`layer` filter on `mimir_recall` now actually filters (#269 follow-up).** The
  `layer` recall parameter was accepted but never applied — `RecallParams.layer`
  was a dead field. It now filters by biomimetic layer in all three modes:
  keyword (`fts5_search`) and BM25 (`fts5_bm25_search`) pre-filter in-query, and a
  mode-agnostic post-filter in `recall()` covers the dense arm of dense/hybrid
  (which scores vectors without `RecallParams` access). Aliases world/episodic/
  semantic are normalized to core/buffer/working at the tools layer.

### Added
- **`mimir_history` tool (code-review follow-up).** The bi-temporal `history_versions`
  reader (v2.4.0) was complete and tested but no tool exposed it — you could time-travel
  to one instant via `mimir_as_of` but couldn't list a fact's full version trail. Wired a
  `mimir_history` tool that returns all superseded versions of a (category, key), newest
  first. Tool count 45 → **46**; README badge/table/section, `server.json`, and
  `CLAIMS-AUDIT.md` reconciled (they had drifted to 44/43).

### Removed
- **Dead `EncryptionManager::decrypt`.** Fully superseded by `decrypt_body` (the
  legacy/auth-failure-classifying variant); the old method had zero callers and was the
  exact footgun the security fix replaced. Removed so it can't be reintroduced.

- **`mimir doctor` + verified client compatibility matrix (#272).** New `mimir doctor`
  subcommand validates the local install (binary path, db path) and prints the MCP
  stdio config plus a compatibility matrix for Claude Desktop, Claude Code/Hermes,
  Cursor, Windsurf, VS Code+Continue.dev, Zed, and Codex CLI. Added a "Works With
  Every MCP Client" table to the README and copy-paste config snippets in
  `docs/clients/`. Mimir is a standard MCP stdio server, so the same command works
  everywhere — this documents and self-checks it.
- **`include_confidence` on `mimir_recall` (#287).** Opt-in (default false): each result
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
  install line) and added the OCI ownership label to the Docker image, so Mimir
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
  test that pins the cross-arm consensus hit). The `mimir-recall-mini` headline
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
- **`mimir_reindex` no longer breaks keyword search on encrypted databases.**
  `reindex_fts` did a raw `INSERT … SELECT body_json`, which on an encrypted DB
  copied **ciphertext** into the FTS5 index — silently breaking all keyword and
  hybrid recall until re-ingest (the recovery tool corrupted the very index it was
  meant to rebuild). It now decrypts each body (AAD `category:key`) and indexes the
  plaintext, matching what `remember` writes. Unencrypted DBs keep the fast bulk
  copy. Regression test added.

### Security
- **Bounded file size for `mimir_ingest_file` (#236 hardening).** Document ingestion
  read the entire file into memory with no size limit, then copied the text into a
  JSON body and the FTS index — a single huge or maliciously-sized file could OOM
  the server (denial of service). Ingestion now rejects files larger than a
  configurable cap (`MIMIR_MAX_INGEST_BYTES`, default 50 MiB) **before** reading,
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
- **Opt-in conflict invalidation (#253).** `mimir_conflicts` gains `resolve=true`:
  the lower-certainty side of a clear conflict is invalidated — superseded into
  `entity_history` and removed from the live table, so it drops out of recall but
  stays reversible and time-travelable via `mimir_as_of`. Conservative by design:
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

Bi-temporal facts: Mimir now keeps a fact's prior versions when it changes and
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
- **`mimir_as_of` tool + `Database::as_of(category, key, as_of_unix_ms)`.**
  Bi-temporal time-travel: returns the version of a fact that was live at a past
  instant (or `found=false` if it had not been recorded yet). Brings the MCP
  tool count to **43**.

### Changed
- `recorded_at_unix_ms` is now set to `created_at_unix_ms` on insert; the
  `user_version` 1→2 migration backfills it for existing rows and adds the
  bi-temporal columns + the `idx_entities_invalidated` live-fact index.

### Documentation
- Reconciled the README tool count (badge / comparison table / section header)
  from a stale **40** to the actual **43**, adding the missing `mimir_extract`,
  `mimir_ingest_file` (both shipped in 2.3.0) and `mimir_as_of` rows.

## [2.3.0] - 2026-06-27

Local, offline knowledge tooling — structured extraction and multimodal document
ingestion — plus a reproducible recall-quality benchmark and a relevance-aware,
deterministic hybrid retrieval path.

### Added
- **Local multimodal document ingestion (#236).** New `mimir_ingest_file` tool
  extracts a document's text **locally** (no cloud, no network) and stores it as a
  recallable entity. Plaintext / markdown / structured-text work in any build;
  **DOCX and PDF** are supported when built with the new optional
  `--features multimodal` (pulls `zip` + `pdf-extract`), keeping the lean default
  binary dependency-free. Brings the MCP tool count to **42**.
- **Local knowledge extraction (#234).** New `mimir_extract` tool turns raw text
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
  paraphrase-heavy `mimir-recall-mini` set the offline dense model reaches **91.7%
  recall@1 / 100% recall@5**, making the local-first promise measurable.

### Changed
- **Relevance-aware, deterministic hybrid recall (#247).** The hybrid (Reciprocal
  Rank Fusion) keyword arm now drops stopwords and ranks by **BM25 relevance**
  instead of popularity, is dropped entirely when it finds no content match, and
  is fused at a reduced dense-primary weight — so a paraphrase query no longer
  dilutes a confident dense ranking. RRF breaks score ties by entity id and the
  hybrid recall path is fully read-only, making all three modes **byte-stable
  run-to-run**. Hybrid recall@1 on `mimir-recall-mini`: **20.8% → 87.5%** (MRR
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
- **Time-aware / recency-boosted hybrid recall (#235).** `mimir_recall` accepts an
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
