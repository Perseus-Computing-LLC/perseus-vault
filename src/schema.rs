use rusqlite::{params, Connection};
use serde_json::json;

use crate::db::now_ms;
use crate::models::{MigrationReport, Stats};

/// SQL to create the v0.2.0 schema from scratch.
pub const DDL_V0_2_0: &str = "
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'general',
    key TEXT NOT NULL,
    body_json TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'active',
    type TEXT DEFAULT 'insight',
    tags TEXT DEFAULT '[]',
    decay_score REAL DEFAULT 1.0,
    retrieval_count INTEGER DEFAULT 0,
    layer TEXT DEFAULT 'working',
    topic_path TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    archive_reason TEXT DEFAULT '',
    links TEXT DEFAULT '[]',
    verified INTEGER DEFAULT 0,
    source TEXT DEFAULT 'agent',
    created_at_unix_ms INTEGER NOT NULL,
    last_accessed_unix_ms INTEGER NOT NULL,
    embedding BLOB,
    -- Sign-bit signature of `embedding` (v2.13.0, dim/8 bytes): bit i set iff
    -- embedding[i] > 0. dense_search Hamming-prefilters on this instead of
    -- reading every full embedding blob once the vault is large enough.
    -- Written by store_embedding; backfilled by the v6 migration.
    emb_sig BLOB,
    always_on INTEGER DEFAULT 0,
    certainty REAL DEFAULT 0.5,
    -- Persistent importance floor (v2.13.0). Set by mimir_score; decay_tick and
    -- cohere floor decay_score at this value, so an explicit score survives the
    -- recency-based recompute instead of being erased by the next tick
    -- (fidelity > recency). 0.0 = unset, no effect.
    importance REAL DEFAULT 0.0,
    workspace_hash TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    visibility TEXT DEFAULT 'workspace',
    -- Bi-temporal facts (v2.4.0). Two time axes plus a supersession link, so a
    -- fact can be retired without deleting history. All NULL/'' here means
    -- \"valid since creation, currently true, never superseded\" — the behavior
    -- before bi-temporal support, so existing rows need no interpretation change.
    valid_from_unix_ms INTEGER,      -- when the fact became true in the world (NULL = since creation)
    valid_to_unix_ms INTEGER,        -- when it stopped being true (NULL = still true)
    recorded_at_unix_ms INTEGER,     -- transaction time: when Mneme first knew it (backfilled = created_at)
    invalidated_at_unix_ms INTEGER,  -- transaction time: when Mneme retired it (NULL = live)
    supersedes TEXT DEFAULT '',      -- id of the entity this one replaced
    superseded_by TEXT DEFAULT '',   -- id of the entity that replaced this one

    -- Efficacy tracking (v2.10.0 — PMB-inspired follow-rate scoring). Tracks
    -- whether a lesson/convention/insight actually gets FOLLOWED by the agent,
    -- not just recalled. follow_rate feeds into decay_tick as a composite
    -- weight so rules that get ignored decay out of recall, and rules that
    -- earn their place resist decay even without recency.
    follow_count INTEGER DEFAULT 0,      -- times confirmed/detected as followed
    miss_count INTEGER DEFAULT 0,        -- times confirmed/detected as NOT followed
    follow_rate REAL DEFAULT 0.0,        -- follow_count / (follow_count + miss_count), 0.0 if no attempts
    efficacy_status TEXT DEFAULT 'unverified',  -- 'unverified' | 'useful' | 'dead'

    -- Usefulness tracking (#487 — Belief-Memory-inspired derived_from
    -- reinforcement). Unlike retrieval_count (how often a memory was merely
    -- recalled), usefulness_count only increments when a later remember()
    -- explicitly cites this entity via derived_from — i.e. the memory
    -- demonstrably informed a subsequent write. Feeds decay_tick (cited
    -- memories decay slower) and the hybrid-recall rank boost.
    usefulness_count INTEGER DEFAULT 0,  -- times cited as a derived_from source
    last_useful_unix_ms INTEGER DEFAULT 0 -- when it was last cited (0 = never)
);

-- Identity index: (category, key, workspace_hash) — #339. Created in
-- initialize_schema's gated block, NOT here: on a legacy DB this ungated DDL
-- runs before the ALTER that adds workspace_hash, so an index referencing the
-- column here would fail the whole batch.

-- Recall ranking index: lets the browse path (WHERE archived=0 [+ residual
-- filters] ORDER BY retrieval_count DESC, last_accessed_unix_ms DESC, id ASC
-- LIMIT k) seek the archived=0 partition and read rows already in rank order,
-- avoiding a full table scan + temp-b-tree sort. EXPLAIN-verified: ~224x on
-- global browse, ~66x on workspace-scoped browse at 30k rows. (#209)
-- The trailing `id ASC` covers recall's #254 determinism tie-break: without it,
-- a large tie-group on (retrieval_count, last_accessed) — e.g. a cold or
-- bulk-imported store with uniform last_accessed — forced SQLite to sort the
-- whole group by id to satisfy LIMIT k (O(tie-group); ~30ms browse @1M). With it
-- the index satisfies the FULL ordering, so browse stays a k-row range scan.
CREATE INDEX IF NOT EXISTS idx_entities_recall ON entities(archived, retrieval_count DESC, last_accessed_unix_ms DESC, id ASC);

CREATE VIRTUAL TABLE IF NOT EXISTS entities_fts USING fts5(body_json, content_rowid='rowid');

CREATE TABLE IF NOT EXISTS journal (
    id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL DEFAULT 'decision',
    evaluated_json TEXT DEFAULT '{}',
    acted_json TEXT DEFAULT '{}',
    forward_json TEXT DEFAULT '{}',
    category TEXT DEFAULT '',
    key TEXT DEFAULT '',
    entity_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    audit_hash TEXT DEFAULT '',
    -- #417: workspace of the referenced entity, stamped at write time so purge
    -- can scope journal redaction per-workspace. '' = system event or legacy row.
    workspace_hash TEXT NOT NULL DEFAULT '',
    -- v15 (2026-07-05): SHA-256 commitment over the payload, covered by the audit
    -- chain so content tampering is detectable while the payload can still be
    -- redacted (the commitment survives). See docs/audit-chain-keyed-mac-design.md.
    payload_commitment TEXT DEFAULT '',
    created_at_unix_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_journal_created ON journal(created_at_unix_ms);
CREATE INDEX IF NOT EXISTS idx_journal_entity ON journal(entity_id);
-- NOTE: the (category, key, workspace_hash) index is created in apply_migrations,
-- not here -- on a legacy DB this batch runs BEFORE the workspace_hash column is
-- added, so referencing it here would fail with a no-such-column error (#417).

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL DEFAULT '{}',
    expires_at_unix_ms INTEGER,
    created_at_unix_ms INTEGER NOT NULL
);

-- Encryption canary (fail-fast wrong-key detection). A single row (id=1) holding
-- a known marker encrypted under the configured key. On startup set_encryption
-- decrypts it and aborts loudly if it fails, so a wrong/rotated key is caught
-- before any read silently returns AuthFailed. Deliberately NOT in `entities` or
-- `state`: it must stay invisible to recall/FTS/stats and to caller-facing state
-- tools. Ungated IF NOT EXISTS so it back-fills onto older databases too.
CREATE TABLE IF NOT EXISTS encryption_canary (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ciphertext TEXT NOT NULL,
    created_at_unix_ms INTEGER NOT NULL
);

-- v15 (2026-07-05): records the audit chain keying so set_encryption can skip
-- the O(journal) rekey when the chain is already keyed under the current key.
-- key_canary = HMAC-SHA256(audit_key, fixed label); a match means already-keyed
-- under this key. See docs/audit-chain-keyed-mac-design.md section 3.4.
CREATE TABLE IF NOT EXISTS audit_chain_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    scheme TEXT NOT NULL,
    key_canary TEXT NOT NULL,
    updated_at_unix_ms INTEGER NOT NULL
);

-- Superseded fact versions (v2.4.0 — bi-temporal facts). When a remember()
-- overwrites an existing (category,key,workspace_hash) with new content, the prior
-- row is snapshotted here with invalidated_at set, so live reads stay one-row-per-key
-- (entities + its UNIQUE(category,key,workspace_hash) are untouched) while history is kept for
-- as-of / time-travel queries. A version was live during
-- [recorded_at_unix_ms, invalidated_at_unix_ms). superseded_by points at the
-- live entity id that replaced it. body_json carries the same encryption as
-- entities (ciphertext if a key is configured).
CREATE TABLE IF NOT EXISTS entity_history (
    history_id TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    key TEXT NOT NULL,
    body_json TEXT NOT NULL DEFAULT '{}',
    status TEXT DEFAULT 'active',
    type TEXT DEFAULT 'insight',
    tags TEXT DEFAULT '[]',
    decay_score REAL DEFAULT 1.0,
    retrieval_count INTEGER DEFAULT 0,
    layer TEXT DEFAULT 'working',
    topic_path TEXT DEFAULT '',
    archived INTEGER DEFAULT 0,
    archive_reason TEXT DEFAULT '',
    links TEXT DEFAULT '[]',
    verified INTEGER DEFAULT 0,
    source TEXT DEFAULT 'agent',
    always_on INTEGER DEFAULT 0,
    certainty REAL DEFAULT 0.5,
    workspace_hash TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    visibility TEXT DEFAULT 'workspace',
    valid_from_unix_ms INTEGER,
    valid_to_unix_ms INTEGER,
    recorded_at_unix_ms INTEGER,
    invalidated_at_unix_ms INTEGER,
    supersedes TEXT DEFAULT '',
    superseded_by TEXT DEFAULT '',
    created_at_unix_ms INTEGER NOT NULL,
    last_accessed_unix_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entity_history_id ON entity_history(id);
CREATE INDEX IF NOT EXISTS idx_entity_history_catkey ON entity_history(category, key, invalidated_at_unix_ms);
";

/// Current schema migration level, stamped into `PRAGMA user_version` once all
/// the column-add migrations below have been applied. Bump this whenever you add
/// a new ALTER-probe migration in `initialize_schema`, or existing databases
/// (already at the previous level) will skip it.
const SCHEMA_VERSION: i64 = 15;

/// Initialize the v0.2.0 schema on a fresh database.
pub fn initialize_schema(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    // DDL is all IF NOT EXISTS, so it stays ungated: it both creates a fresh DB
    // and back-fills newer objects (e.g. idx_entities_recall) on older ones.
    // It also stays OUTSIDE the migration transaction below — IF NOT EXISTS is
    // already concurrency-safe, and it keeps the FTS5 virtual-table creation
    // out of the transaction entirely.
    conn.execute_batch(DDL_V0_2_0)?;

    // The column-add migrations each probe for the column first. `open` runs
    // several times per process, so on a fully-migrated DB this is pure wasted
    // work. Gate it on PRAGMA user_version: run once when behind, then stamp
    // current and skip on every subsequent open. (#208)
    let user_version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if user_version >= SCHEMA_VERSION {
        return Ok(());
    }

    // #353: PRAGMA busy_timeout serializes individual statements, not this
    // whole check-then-migrate sequence — two *processes* opening the same
    // pre-upgrade DB could both read a stale user_version, both enter the
    // migration path, and the loser would die on "duplicate column name".
    // BEGIN IMMEDIATE takes SQLite's write lock up front, acting as a
    // cross-process mutex: the loser blocks here (subject to busy_timeout)
    // until the winner commits, then sees the stamped version inside
    // apply_migrations and no-ops. Bonus: the migration steps used to
    // auto-commit individually; one transaction makes the whole migration
    // atomic (SQLite DDL is transactional), so a crash mid-migration rolls
    // back cleanly instead of leaving a half-migrated DB.
    conn.execute_batch("BEGIN IMMEDIATE;")?;
    match apply_migrations(conn) {
        Ok(()) => {
            conn.execute_batch("COMMIT;")?;
            Ok(())
        }
        Err(e) => {
            let _ = conn.execute_batch("ROLLBACK;");
            Err(e)
        }
    }
}

/// Add `column` to `table` unless it already exists. Defense-in-depth for the
/// #353 race: even if the existence probe raced another writer (e.g. a process
/// that migrated between our probe and our ALTER), "duplicate column name"
/// means the column is present — exactly the state we wanted — so it is
/// treated as success, not an error.
fn ensure_column(
    conn: &Connection,
    table: &str,
    column: &str,
    decl: &str,
) -> Result<(), Box<dyn std::error::Error>> {
    if conn
        .prepare(&format!("SELECT {column} FROM {table} LIMIT 1"))
        .is_ok()
    {
        return Ok(());
    }
    match conn.execute_batch(&format!("ALTER TABLE {table} ADD COLUMN {column} {decl};")) {
        Ok(()) => Ok(()),
        Err(e) if e.to_string().contains("duplicate column name") => Ok(()),
        Err(e) => Err(e.into()),
    }
}

/// The gated column-add migrations. Runs inside the BEGIN IMMEDIATE
/// transaction taken by `initialize_schema` (#353) — must not BEGIN/COMMIT
/// itself.
fn apply_migrations(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    // Re-check the version now that we hold the write lock: if another
    // process completed the migration while we waited on BEGIN IMMEDIATE,
    // there is nothing left to do. (#353)
    let user_version: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0))?;
    if user_version >= SCHEMA_VERSION {
        return Ok(());
    }

    // Add embedding column if it doesn't exist (migration from v0.2.0)
    ensure_column(conn, "entities", "embedding", "BLOB")?;

    // v1.x: always_on, certainty
    ensure_column(conn, "entities", "always_on", "INTEGER DEFAULT 0")?;
    ensure_column(conn, "entities", "certainty", "REAL DEFAULT 0.5")?;

    // v1.2.0: multi-workspace scoping, agent attribution, access controls
    ensure_column(conn, "entities", "workspace_hash", "TEXT DEFAULT ''")?;
    ensure_column(conn, "entities", "agent_id", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "agent_id", "TEXT DEFAULT ''")?;
    ensure_column(conn, "entities", "visibility", "TEXT DEFAULT 'workspace'")?;

    // v2.0: cryptographic audit log
    ensure_column(conn, "journal", "audit_hash", "TEXT DEFAULT ''")?;

    // Add bi-temporal columns (v2.4.0 — bi-temporal facts). Valid time
    // (valid_from/valid_to), transaction time (recorded_at/invalidated_at), and
    // supersession links. All additive; existing rows keep their meaning.
    ensure_column(conn, "entities", "valid_from_unix_ms", "INTEGER")?;
    ensure_column(conn, "entities", "valid_to_unix_ms", "INTEGER")?;
    ensure_column(conn, "entities", "recorded_at_unix_ms", "INTEGER")?;
    ensure_column(conn, "entities", "invalidated_at_unix_ms", "INTEGER")?;
    ensure_column(conn, "entities", "supersedes", "TEXT DEFAULT ''")?;
    ensure_column(conn, "entities", "superseded_by", "TEXT DEFAULT ''")?;

    // Add efficacy-tracking columns (v2.10.0 — PMB-inspired follow-rate scoring).
    ensure_column(conn, "entities", "follow_count", "INTEGER DEFAULT 0")?;
    ensure_column(conn, "entities", "miss_count", "INTEGER DEFAULT 0")?;
    ensure_column(conn, "entities", "follow_rate", "REAL DEFAULT 0.0")?;
    ensure_column(conn, "entities", "efficacy_status", "TEXT DEFAULT 'unverified'")?;

    // Add usefulness-tracking columns (#487 — derived_from reinforcement).
    ensure_column(conn, "entities", "usefulness_count", "INTEGER DEFAULT 0")?;
    ensure_column(conn, "entities", "last_useful_unix_ms", "INTEGER DEFAULT 0")?;

    // Backfill transaction time for pre-existing rows: a fact's recorded_at is
    // when Mneme first stored it, i.e. its created_at. (No-op on a fresh DB.)
    conn.execute_batch(
        "UPDATE entities SET recorded_at_unix_ms = created_at_unix_ms \
         WHERE recorded_at_unix_ms IS NULL;",
    )?;

    // Live-fact filter index. Created here (not in the ungated DDL) because it
    // references invalidated_at_unix_ms, which on a migrating DB only exists
    // after the ALTER above. NULL = live; recall will exclude non-NULL rows.
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_entities_invalidated \
         ON entities(invalidated_at_unix_ms);",
    )?;

    // v5: persistent importance floor (see the column comment in the DDL).
    ensure_column(conn, "entities", "importance", "REAL DEFAULT 0.0")?;

    // v6: sign-bit embedding signatures for the dense-search prefilter, plus a
    // backfill for embeddings stored before the column existed. Bounded work:
    // one pass over embedded rows, ~50 bytes written per row.
    ensure_column(conn, "entities", "emb_sig", "BLOB")?;
    {
        let mut stmt = conn.prepare(
            "SELECT id, embedding FROM entities \
             WHERE embedding IS NOT NULL AND emb_sig IS NULL",
        )?;
        let rows = stmt.query_map([], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?))
        })?;
        let pending: Vec<(String, Vec<u8>)> = rows.filter_map(|r| r.ok()).collect();
        drop(stmt);
        for (id, blob) in pending {
            let emb: Vec<f32> = blob
                .chunks_exact(4)
                .map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]]))
                .collect();
            let sig = crate::db::embedding_signature(&emb);
            conn.execute(
                "UPDATE entities SET emb_sig = ?1 WHERE id = ?2",
                params![sig, id],
            )?;
        }
    }

    // v4 (#339): identity becomes (category, key, workspace_hash). A plain
    // (category, key) uniqueness made cross-workspace key collisions
    // unstorable, which is what forced mimir_share's "copy into workspace" to
    // clobber the source row. Created here (after the workspace_hash ALTER,
    // like idx_entities_invalidated) rather than in the ungated DDL. Safe on
    // a populated DB — the old constraint was strictly tighter, so no
    // existing rows can collide. Create-then-drop, so uniqueness is never
    // unenforced.
    conn.execute_batch(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_category_key_ws \
         ON entities(category, key, workspace_hash); \
         DROP INDEX IF EXISTS idx_entities_category_key;",
    )?;

    // ── v8 (#365): GraphRAG communities ─────────────────────────────────
    // Self-contained block (easy to renumber at merge). Persists the output
    // of community detection over the entity link graph: one row per detected
    // community, scoped to a workspace. `id` is derived from a digest of the
    // sorted member ids, so a membership change produces a NEW community id —
    // that is the cache-invalidation mechanism for community summaries (the
    // state-digest cache-key pattern from #256). All DDL is IF NOT EXISTS,
    // so this block is idempotent and safe to re-run.
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS communities (
            id TEXT PRIMARY KEY,                        -- 'com-' + digest of sorted member ids
            workspace_hash TEXT NOT NULL DEFAULT '',
            member_ids TEXT NOT NULL DEFAULT '[]',      -- JSON array of entity ids
            member_digest TEXT NOT NULL DEFAULT '',     -- digest of the member set (cache key)
            summary TEXT NOT NULL DEFAULT '',           -- extractive (or LLM-polished) summary
            summary_entity_id TEXT NOT NULL DEFAULT '', -- entities.id of the stored summary entity
            algorithm TEXT NOT NULL DEFAULT 'label_prop',
            modularity REAL NOT NULL DEFAULT 0.0,       -- partition modularity of the detection run
            member_count INTEGER NOT NULL DEFAULT 0,
            generated_at_unix_ms INTEGER NOT NULL DEFAULT 0
         );
         CREATE INDEX IF NOT EXISTS idx_communities_ws ON communities(workspace_hash);",
    )?;

    // ── v9 (#363): bi-temporal valid-time backfill ──────────────────────
    // Self-contained block (renumber-safe). The valid-time axis becomes
    // queryable (mimir_valid_at / mimir_bitemporal / recall valid filters),
    // so make the historical convention "NULL valid_from = valid since the
    // fact was recorded" explicit: backfill valid_from to the row's
    // transaction time. valid_to stays NULL (= still true / unbounded).
    // Idempotent and re-runnable — only NULL rows are touched, and query
    // paths still COALESCE for rows written by older binaries afterwards.
    conn.execute_batch(
        "UPDATE entities SET valid_from_unix_ms = COALESCE(recorded_at_unix_ms, created_at_unix_ms) \
         WHERE valid_from_unix_ms IS NULL; \
         UPDATE entity_history SET valid_from_unix_ms = COALESCE(recorded_at_unix_ms, created_at_unix_ms) \
         WHERE valid_from_unix_ms IS NULL;",
    )?;
    // ── end v9 ──────────────────────────────────────────────────────────

    // ── v10 (#392): stored near-duplicate signatures ─────────────────────
    // Self-contained block (renumber-safe). One row per entity holding the
    // packed character-trigram set of the STORED body_json column value (see
    // src/dedup.rs), so find_near_duplicate can compute its exact Jaccard
    // verdict without rebuilding the trigram set per candidate per insert —
    // the O(M·N) cost behind the 1.6s-per-write stall at 50k rows.
    //
    // Backfill is LAZY: rows written before this migration simply have no
    // signature; the dedup scan takes the old rebuild-from-body path for
    // them (identical verdicts, old cost) and writes the signature back in
    // bounded batches, so a large store converges without a potentially
    // multi-minute eager migration. body_len records the stored body's byte
    // length as a freshness guard. ON DELETE CASCADE keeps the side table in
    // step with every entity-delete path (the pool opens connections with
    // foreign_keys=ON); a surviving orphan is inert — the scan joins FROM
    // entities, so it can never resurrect a deleted row.
    // Two tables, split by access pattern. The scan LEFT JOINs
    // dedup_signatures (small fixed-size rows: freshness guard, set size,
    // 256-byte prune histogram) for EVERY candidate, so its per-row page
    // footprint must stay tiny — a writer committing between scans
    // invalidates other pooled connections' page caches, making the scan's
    // hot page count the real cost on write-heavy workloads. The multi-KB
    // exact trigram set lives in dedup_signature_blobs and is fetched by a
    // separate point query ONLY for the rare candidate that survives both
    // lossless prunes. WITHOUT ROWID: the entity_id probes hit one clustered
    // b-tree instead of autoindex-then-rowid double lookups.
    //
    // Freshness guard = (body_len, body_hash): a signature is trusted only
    // while BOTH match the stored body. Length alone cannot catch a
    // same-length rewrite by a signature-unaware writer — which is exactly
    // what a rolled-back pre-v10 binary produces (it rewrites body_json
    // without touching these tables; AES-GCM re-encryption even preserves
    // ciphertext length). With the hash, such rows read as stale, fall back
    // to the exact rebuild path (identical verdicts), and self-heal on the
    // next dedup touch — so v10 stores stay ROLLBACK-SAFE: running an older
    // binary never poisons dedup verdicts, and dropping both side tables is
    // always a safe reset (they hold only derived, rebuildable data).
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS dedup_signatures (
            entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
            body_len INTEGER NOT NULL,
            body_hash INTEGER NOT NULL,
            tg_count INTEGER NOT NULL,
            histo BLOB
         ) WITHOUT ROWID;
         CREATE TABLE IF NOT EXISTS dedup_signature_blobs (
            entity_id TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
            sig BLOB NOT NULL
         ) WITHOUT ROWID;",
    )?;
    // ── end v10 ─────────────────────────────────────────────────────────

    // ── v11: per-workspace journal scoping (#417) ───────────────────────
    // journal had no workspace column, so purge's (category, key) redaction
    // over-redacted cross-workspace same-key rows. Add the column + its match
    // index. Legacy rows keep '' (unknown) and stay conservatively matched by
    // purge; new rows are stamped at write time in Database::journal.
    // category/key have been in the journal table since v0.2.0 and are already
    // assumed by purge's JRN_MATCH and the journal readers; ensure_column them
    // here so the composite index below is robust even on ancient pre-v0.2.0
    // journals (and minimal test fixtures) that predate those columns.
    ensure_column(conn, "journal", "category", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "key", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "workspace_hash", "TEXT NOT NULL DEFAULT ''")?;
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_journal_catkeyws ON journal(category, key, workspace_hash);",
    )?;
    // ── end v11 ─────────────────────────────────────────────────────────

    // ── v12 (#433 M2): bind workspace into the audit-chain hash ──────────
    // Pre-v12 chains hashed only (prev_hash, id, created_at_unix_ms), so a
    // journal entry could be moved between workspaces without breaking the
    // chain. The hash now also folds in workspace_hash (stamped since v11).
    // (The chain rehash that was here is now performed once in the v15 block —
    // the v15 formula supersedes v12's, and the migrate function runs every block
    // top-to-bottom for any DB below SCHEMA_VERSION, so a single final rehash is
    // sufficient. Doing it here as well would fail on legacy journals that don't
    // yet have the payload columns the v15 rehash reads.)
    // ── end v12 ──────────────────────────────────────────────────────────

    // ── v13: cover the browse ORDER BY tie-break in idx_entities_recall ──
    // The empty-query browse path orders by
    //   retrieval_count DESC, last_accessed_unix_ms DESC, id ASC
    // but the pre-v13 index covered only the first two keys, so a large
    // tie-group on (retrieval_count, last_accessed) — a cold or bulk-imported
    // store where last_accessed is uniform — forced SQLite to sort the whole
    // group by id to satisfy LIMIT k (O(tie-group); measured ~30ms browse p50
    // at 1M rows). Adding id ASC as a trailing key lets the index satisfy the
    // full ordering, so browse is a pure k-row range scan again. DROP+recreate
    // because the old index lacks the column; safe on a populated DB (the index
    // is derived, and recall/browse fall back to a scan for the brief window).
    conn.execute_batch(
        "DROP INDEX IF EXISTS idx_entities_recall; \
         CREATE INDEX IF NOT EXISTS idx_entities_recall \
           ON entities(archived, retrieval_count DESC, last_accessed_unix_ms DESC, id ASC);",
    )?;
    // ── end v13 ──────────────────────────────────────────────────────────

    // ── v14 (2026-07-05 security review): cryptographic audit-chain hash ──
    // Pre-v14 the journal chain used a 64-bit, non-cryptographic `DefaultHasher`
    // (SipHash) — brute-forceable for targeted collisions. It is now a real
    // SHA-256 over the same erasure-safe identifying tuple (prev, id,
    // created_at, workspace_hash). Recompute existing chains under the new
    // formula so they verify from here forward. Deterministic + idempotent;
    // a no-op on a fresh DB (empty journal). (Rehash deferred to the v15 block —
    // see the v12 note; the v15 formula supersedes this one.)
    // ── end v14 ──────────────────────────────────────────────────────────

    // ── v15 (2026-07-05 security review): payload commitment + keyed chain ──
    // Add the per-entry payload_commitment column and recompute the chain over
    // (prev, id, created_at, workspace, commitment). At migration time no key is
    // available, so the rehash is UNKEYED; `set_encryption` later rekeys it to
    // HMAC once the encryption key is loaded. Backfills commitments for existing
    // rows. Deterministic + idempotent; no-op on a fresh DB. See
    // docs/audit-chain-keyed-mac-design.md.
    // Ensure every column the rehash reads exists — a very old (pre-bitemporal)
    // journal may predate some of them; ensure_column is idempotent.
    ensure_column(conn, "journal", "event_type", "TEXT DEFAULT 'decision'")?;
    ensure_column(conn, "journal", "evaluated_json", "TEXT DEFAULT '{}'")?;
    ensure_column(conn, "journal", "acted_json", "TEXT DEFAULT '{}'")?;
    ensure_column(conn, "journal", "forward_json", "TEXT DEFAULT '{}'")?;
    ensure_column(conn, "journal", "category", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "key", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "entity_id", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "agent_id", "TEXT DEFAULT ''")?;
    ensure_column(conn, "journal", "payload_commitment", "TEXT DEFAULT ''")?;
    crate::db::rehash_audit_chain(conn)?;
    // ── end v15 ──────────────────────────────────────────────────────────

    // Stamp the migration level so subsequent opens skip the probe block above.
    conn.pragma_update(None, "user_version", SCHEMA_VERSION)?;

    Ok(())
}

/// Check if a database has the v0.2.0 entities table.
#[allow(dead_code)]
pub fn is_v0_2_0(conn: &Connection) -> Result<bool, Box<dyn std::error::Error>> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='entities'",
        [],
        |r| r.get(0),
    )?;
    Ok(count > 0)
}

/// Check if a database has the old v0.1.x memories table.
pub fn has_v0_1_memories(conn: &Connection) -> Result<bool, Box<dyn std::error::Error>> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='memories'",
        [],
        |r| r.get(0),
    )?;
    Ok(count > 0)
}

/// Get total entity count.
#[allow(dead_code)]
pub fn entity_count(conn: &Connection) -> Result<i64, Box<dyn std::error::Error>> {
    let count: i64 = conn.query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))?;
    Ok(count)
}

/// Truncate `s` to at most `max_bytes` bytes without splitting a UTF-8
/// character. `&s[..n]` panics when `n` is not a char boundary, so walk the
/// cut point back to the nearest boundary instead (stable-Rust equivalent of
/// the nightly `floor_char_boundary`). (#352)
fn truncate_at_char_boundary(s: &str, max_bytes: usize) -> &str {
    if s.len() <= max_bytes {
        return s;
    }
    let mut end = max_bytes;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Migrate from v0.1.x schema to v0.2.0.
///
/// Opens the old DB, reads all memories, writes them as entities into the new schema,
/// and returns a MigrationReport.
pub fn migrate_from_v0_1(
    old_path: &str,
    conn: &Connection,
) -> Result<MigrationReport, Box<dyn std::error::Error>> {
    let old_conn = Connection::open(old_path)?;

    if !has_v0_1_memories(&old_conn)? {
        return Ok(MigrationReport {
            total_old_memories: 0,
            entities_created: 0,
            entities_updated: 0,
            errors: vec!["No v0.1.x memories table found in source DB".to_string()],
            completed_at_unix_ms: now_ms(),
        });
    }

    // Ensure target has v0.2.0 schema
    initialize_schema(conn)?;

    let mut stmt = old_conn.prepare(
        "SELECT id, content, type, summary, relevance, decay_score, retrieval_count,
                layer, topic_path, created_at_unix_ms, last_accessed_unix_ms,
                workspace_hash, tags, links, source, verified
         FROM memories",
    )?;

    let old_memories = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,         // id
            row.get::<_, String>(1)?,         // content
            row.get::<_, String>(2)?,         // type
            row.get::<_, Option<String>>(3)?, // summary
            row.get::<_, f64>(4)?,            // relevance
            row.get::<_, f64>(5)?,            // decay_score
            row.get::<_, i64>(6)?,            // retrieval_count
            row.get::<_, String>(7)?,         // layer
            row.get::<_, String>(8)?,         // topic_path
            row.get::<_, i64>(9)?,            // created_at_unix_ms
            row.get::<_, i64>(10)?,           // last_accessed_unix_ms
            row.get::<_, String>(11)?,        // workspace_hash
            row.get::<_, String>(12)?,        // tags
            row.get::<_, String>(13)?,        // links
            row.get::<_, String>(14)?,        // source
            row.get::<_, i32>(15)?,           // verified
        ))
    })?;

    let mut total = 0i64;
    let mut created = 0i64;
    let updated = 0i64;
    let mut errors: Vec<String> = Vec::new();

    for row in old_memories {
        total += 1;
        let (
            id,
            content,
            mem_type,
            summary,
            _relevance,
            decay_score,
            retrieval_count,
            layer,
            topic_path,
            created_at,
            last_accessed,
            workspace_hash,
            tags_str,
            links_str,
            source,
            verified,
        ) = match row {
            Ok(r) => r,
            Err(e) => {
                errors.push(format!("Row {} read error: {}", total, e));
                continue;
            }
        };

        // Build body_json: wrap content + summary
        let body = serde_json::to_string(&json!({
            "content": content,
            "summary": summary.unwrap_or_default(),
        }))
        .unwrap_or_else(|_| "{}".to_string());

        // Parse tags as JSON array, inject workspace_hash if present
        let mut tags_value: serde_json::Value =
            serde_json::from_str(&tags_str).unwrap_or(json!([]));
        if !workspace_hash.is_empty() {
            if let Some(arr) = tags_value.as_array_mut() {
                arr.push(json!(format!("workspace:{}", workspace_hash)));
            }
        }
        let tags_json = serde_json::to_string(&tags_value).unwrap_or_else(|_| "[]".to_string());

        // Category and key: derive from type + truncated id. Truncation must
        // respect char boundaries: legacy v0.1 ids were written by external
        // systems and may contain multi-byte UTF-8 — a raw byte slice panics
        // if byte 20 falls inside a char, aborting the whole migration (#352).
        let category = "general".to_string();
        let key = format!("migrated-{}", truncate_at_char_boundary(&id, 20));

        let verified_int = if verified != 0 { 1 } else { 0 };

        let result = conn.execute(
            "INSERT OR REPLACE INTO entities
             (id, category, key, body_json, status, type, tags,
              decay_score, retrieval_count, layer, topic_path,
              archived, archive_reason, links, verified, source,
              created_at_unix_ms, last_accessed_unix_ms)
             VALUES (?1, ?2, ?3, ?4, 'active', ?5, ?6,
                     ?7, ?8, ?9, ?10,
                     0, '', ?11, ?12, ?13,
                     ?14, ?15)",
            params![
                id,
                category,
                key,
                body,
                mem_type,
                tags_json,
                decay_score,
                retrieval_count,
                layer,
                topic_path,
                links_str,
                verified_int,
                source,
                created_at,
                last_accessed,
            ],
        );

        match result {
            Ok(_) => created += 1,
            Err(e) => errors.push(format!("Migrate error for id={}: {}", id, e)),
        }
    }

    // Rebuild FTS5 index
    let _ = conn.execute(
        "INSERT INTO entities_fts (rowid, body_json)
         SELECT rowid, body_json FROM entities",
        [],
    );

    Ok(MigrationReport {
        total_old_memories: total,
        entities_created: created,
        entities_updated: updated,
        errors,
        completed_at_unix_ms: now_ms(),
    })
}

/// Gather database statistics across all tables.
pub fn gather_stats(conn: &Connection, db_path: &str) -> Result<Stats, Box<dyn std::error::Error>> {
    let total_entities: i64 = conn.query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))?;

    let by_category = query_grouped_counts(conn, "entities", "category")?;
    let by_type = query_grouped_counts(conn, "entities", "type")?;
    let by_layer = query_grouped_counts(conn, "entities", "layer")?;

    let total_journal: i64 = conn.query_row("SELECT COUNT(*) FROM journal", [], |r| r.get(0))?;

    let total_state: i64 = conn.query_row("SELECT COUNT(*) FROM state", [], |r| r.get(0))?;

    let db_size = std::fs::metadata(db_path).map(|m| m.len()).unwrap_or(0);

    let oldest: Option<i64> = conn
        .query_row("SELECT MIN(created_at_unix_ms) FROM entities", [], |r| {
            r.get(0)
        })
        .ok();
    let newest: Option<i64> = conn
        .query_row("SELECT MAX(created_at_unix_ms) FROM entities", [], |r| {
            r.get(0)
        })
        .ok();

    // Graph health (#365): community membership + modularity, so operators
    // can see whether the link graph has global structure. Defaults keep the
    // stats call working even if the communities table is somehow absent.
    let total_communities: i64 = conn
        .query_row("SELECT COUNT(*) FROM communities", [], |r| r.get(0))
        .unwrap_or(0);
    let graph_modularity: Option<f64> = conn
        .query_row(
            "SELECT modularity FROM communities \
             ORDER BY generated_at_unix_ms DESC, id ASC LIMIT 1",
            [],
            |r| r.get(0),
        )
        .ok();

    // History growth visibility (#398): entity_history is append-only unless
    // retention/purge trims it, so surface size + the hot keys directly in
    // stats — rows, stored body bytes, and the top-10 keys by version count.
    let (total_history_rows, history_bytes): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(body_json)), 0) FROM entity_history",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap_or((0, 0));
    let top_history_keys: serde_json::Value = {
        let mut items = Vec::new();
        if let Ok(mut stmt) = conn.prepare(
            "SELECT category, key, COUNT(*) AS versions, COALESCE(SUM(LENGTH(body_json)), 0) \
             FROM entity_history GROUP BY category, key \
             ORDER BY versions DESC, category ASC, key ASC LIMIT 10",
        ) {
            if let Ok(rows) = stmt.query_map([], |r| {
                Ok(json!({
                    "category": r.get::<_, String>(0)?,
                    "key": r.get::<_, String>(1)?,
                    "versions": r.get::<_, i64>(2)?,
                    "bytes": r.get::<_, i64>(3)?,
                }))
            }) {
                items.extend(rows.flatten());
            }
        }
        serde_json::Value::Array(items)
    };

    Ok(Stats {
        total_entities,
        by_category,
        by_type,
        by_layer,
        total_journal_events: total_journal,
        total_state_entries: total_state,
        db_file_size_bytes: db_size,
        oldest_unix_ms: oldest,
        newest_unix_ms: newest,
        total_communities,
        graph_modularity,
        total_history_rows,
        history_bytes,
        top_history_keys,
    })
}

fn query_grouped_counts(
    conn: &Connection,
    table: &str,
    column: &str,
) -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let sql = format!(
        "SELECT {}, COUNT(*) FROM {} GROUP BY {} ORDER BY COUNT(*) DESC",
        column, table, column
    );
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map([], |r| {
        Ok((
            r.get::<_, String>(0)
                .unwrap_or_else(|_| "(null)".to_string()),
            r.get::<_, i64>(1).unwrap_or(0),
        ))
    })?;

    let mut map = serde_json::Map::new();
    for (key, count) in rows.flatten() {
        map.insert(key, json!(count));
    }
    Ok(serde_json::Value::Object(map))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn temp_db() -> (Connection, String) {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("mimir-test-schema-{}.db", uuid::Uuid::new_v4()));
        let path_str = path.to_str().unwrap().to_string();
        let conn = Connection::open(&path_str).expect("open test db");
        (conn, path_str)
    }

    #[test]
    fn initializes_schema_on_new_db() {
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        assert!(is_v0_2_0(&conn).unwrap());
    }

    #[test]
    fn stamps_user_version_and_gates_migration_probes() {
        // Fresh init stamps the current schema version.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION, "fresh init must stamp the schema version");

        // Re-running on an already-current DB is a no-op that preserves data and
        // leaves the version untouched (the probe block is skipped).
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('v-test', 'insight', 'k', '{}', 0, 0)",
            [],
        )
        .unwrap();
        initialize_schema(&conn).expect("re-init should be a no-op");
        let v2: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v2, SCHEMA_VERSION);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities WHERE id='v-test'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1, "re-init must not drop data");
    }

    #[test]
    fn migrates_pre_versioned_db_missing_a_column() {
        // Simulate a legacy DB at user_version=0 that predates the visibility
        // column: the gate must still run the probes and add the column, then
        // stamp the version so later opens skip.
        let (conn, _path) = temp_db();
        // Base v0.2.0 columns the DDL's indexes reference, but WITHOUT the
        // later ALTER-added columns (embedding/always_on/certainty/
        // workspace_hash/agent_id/visibility, journal agent_id/audit_hash).
        conn.execute_batch(
            "CREATE TABLE entities (
                id TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT 'general', key TEXT NOT NULL,
                body_json TEXT NOT NULL DEFAULT '{}', archived INTEGER DEFAULT 0,
                retrieval_count INTEGER DEFAULT 0,
                created_at_unix_ms INTEGER NOT NULL, last_accessed_unix_ms INTEGER NOT NULL
             );
             CREATE TABLE journal (
                id TEXT PRIMARY KEY, entity_id TEXT DEFAULT '',
                created_at_unix_ms INTEGER NOT NULL
             );",
        )
        .unwrap();
        assert!(
            conn.prepare("SELECT visibility FROM entities LIMIT 1").is_err(),
            "precondition: legacy table lacks visibility"
        );

        initialize_schema(&conn).expect("migrate legacy db");

        assert!(
            conn.prepare("SELECT visibility FROM entities LIMIT 1").is_ok(),
            "visibility column must be added during gated migration"
        );
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
    }

    #[test]
    fn apply_migrations_post_lock_version_recheck_is_noop() {
        // #353: the loser of the migration race blocks on BEGIN IMMEDIATE,
        // then must see the winner's stamped user_version and skip cleanly.
        // Exercise that re-check path directly: on an already-current DB,
        // apply_migrations must return Ok without touching anything.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('recheck-test', 'insight', 'k', '{}', 0, 0)",
            [],
        )
        .unwrap();

        apply_migrations(&conn).expect("post-lock re-check must be a clean no-op");

        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn ensure_column_treats_duplicate_column_as_benign() {
        // #353 defense-in-depth: simulate losing the probe/ALTER race — the
        // column appears after our probe would have run. ensure_column's
        // ALTER error path must swallow "duplicate column name".
        let (conn, _path) = temp_db();
        conn.execute_batch("CREATE TABLE t (id TEXT PRIMARY KEY, extra TEXT);")
            .unwrap();
        // Probe path: column exists, no ALTER attempted.
        ensure_column(&conn, "t", "extra", "TEXT").expect("existing column is a no-op");
        // Error path: bypass the probe by asserting the raw duplicate error is
        // matched by the same predicate ensure_column uses.
        let err = conn
            .execute_batch("ALTER TABLE t ADD COLUMN extra TEXT;")
            .unwrap_err();
        assert!(
            err.to_string().contains("duplicate column name"),
            "SQLite duplicate-column error text changed: {err}"
        );
    }

    #[test]
    fn concurrent_opens_of_pre_upgrade_db_both_succeed() {
        // #353 end-to-end: two "processes" (independent connections — same
        // lock semantics as separate OS processes) open the same pre-upgrade
        // DB and run initialize_schema concurrently. Before the fix the loser
        // could fail with "duplicate column name"; now BEGIN IMMEDIATE
        // serializes them and the loser's post-lock re-check no-ops.
        let dir = std::env::temp_dir();
        let path = dir.join(format!("mimir-test-race-{}.db", uuid::Uuid::new_v4()));
        let path_str = path.to_str().unwrap().to_string();

        // Pre-upgrade fixture: legacy tables without the ALTER-added columns,
        // user_version=0 (same shape as migrates_pre_versioned_db_missing_a_column).
        {
            let conn = Connection::open(&path_str).unwrap();
            conn.execute_batch(
                "CREATE TABLE entities (
                    id TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT 'general', key TEXT NOT NULL,
                    body_json TEXT NOT NULL DEFAULT '{}', archived INTEGER DEFAULT 0,
                    retrieval_count INTEGER DEFAULT 0,
                    created_at_unix_ms INTEGER NOT NULL, last_accessed_unix_ms INTEGER NOT NULL
                 );
                 CREATE TABLE journal (
                    id TEXT PRIMARY KEY, entity_id TEXT DEFAULT '',
                    created_at_unix_ms INTEGER NOT NULL
                 );",
            )
            .unwrap();
        }

        let barrier = std::sync::Arc::new(std::sync::Barrier::new(2));
        let handles: Vec<_> = (0..2)
            .map(|_| {
                let path = path_str.clone();
                let barrier = barrier.clone();
                std::thread::spawn(move || {
                    let conn = Connection::open(&path).map_err(|e| e.to_string())?;
                    // Same busy_timeout Database::open applies: the loser must
                    // WAIT on BEGIN IMMEDIATE, not fail fast with SQLITE_BUSY.
                    conn.execute_batch("PRAGMA busy_timeout=5000;")
                        .map_err(|e| e.to_string())?;
                    barrier.wait();
                    initialize_schema(&conn).map_err(|e| e.to_string())?;
                    Ok::<(), String>(())
                })
            })
            .collect();
        for h in handles {
            h.join()
                .expect("thread panicked")
                .expect("concurrent initialize_schema must succeed in both openers");
        }

        // The DB came out fully migrated exactly once.
        let conn = Connection::open(&path_str).unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
        assert!(conn.prepare("SELECT emb_sig FROM entities LIMIT 1").is_ok());
        drop(conn);
        let _ = std::fs::remove_file(&path_str);
    }

    #[test]
    fn migrates_unique_index_to_workspace_scoped_identity() {
        // v4 (#339): a v3-era DB with the two-column unique index and existing
        // rows must come out with the three-column index, the old index
        // dropped, and cross-workspace key collisions storable.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("fresh init");
        // Rewind to the v3 state: old index back, new index gone, version 3.
        conn.execute_batch(
            "DROP INDEX IF EXISTS idx_entities_category_key_ws;
             CREATE UNIQUE INDEX idx_entities_category_key ON entities(category, key);
             PRAGMA user_version = 3;",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, workspace_hash, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('mig-a', 'note', 'k', '{}', 'ws-alpha', 0, 0)",
            [],
        )
        .unwrap();

        initialize_schema(&conn).expect("v3 -> v4 migration");

        let old_idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_entities_category_key'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(old_idx, 0, "old two-column unique index must be dropped");
        let new_idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_entities_category_key_ws'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(new_idx, 1, "workspace-scoped unique index must exist");

        // Same (category, key) in a different workspace now inserts…
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, workspace_hash, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('mig-b', 'note', 'k', '{}', 'ws-beta', 0, 0)",
            [],
        )
        .expect("cross-workspace key collision must be storable after v4");
        // …while a true duplicate in the SAME workspace is still rejected.
        assert!(
            conn.execute(
                "INSERT INTO entities (id, category, key, body_json, workspace_hash, created_at_unix_ms, last_accessed_unix_ms)
                 VALUES ('mig-c', 'note', 'k', '{}', 'ws-alpha', 0, 0)",
                [],
            )
            .is_err(),
            "same-workspace duplicate must still violate uniqueness"
        );
    }

    #[test]
    fn migration_backfills_embedding_signatures() {
        // v6: embeddings stored before emb_sig existed must get a signature
        // during the gated migration, matching what store_embedding writes.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("fresh init");
        // Rewind: drop the column's data by simulating a pre-v6 row.
        let emb: Vec<f32> = vec![1.0, -2.0, 0.5, -0.1];
        let blob: Vec<u8> = emb.iter().flat_map(|f| f.to_le_bytes()).collect();
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, embedding, emb_sig,
                                   created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('sig-1', 'note', 'k', '{}', ?1, NULL, 0, 0)",
            params![blob],
        )
        .unwrap();
        conn.pragma_update(None, "user_version", 5).unwrap();

        initialize_schema(&conn).expect("v5 -> v6 migration");

        let sig: Vec<u8> = conn
            .query_row("SELECT emb_sig FROM entities WHERE id = 'sig-1'", [], |r| r.get(0))
            .expect("emb_sig must be backfilled");
        assert_eq!(sig, crate::db::embedding_signature(&emb));
    }

    #[test]
    fn adds_bitemporal_columns_and_backfills_recorded_at() {
        // A legacy DB (no bi-temporal columns) with one row predating the migration.
        let (conn, _path) = temp_db();
        conn.execute_batch(
            "CREATE TABLE entities (
                id TEXT PRIMARY KEY, category TEXT NOT NULL DEFAULT 'general', key TEXT NOT NULL,
                body_json TEXT NOT NULL DEFAULT '{}', archived INTEGER DEFAULT 0,
                retrieval_count INTEGER DEFAULT 0,
                created_at_unix_ms INTEGER NOT NULL, last_accessed_unix_ms INTEGER NOT NULL
             );
             CREATE TABLE journal (
                id TEXT PRIMARY KEY, entity_id TEXT DEFAULT '',
                created_at_unix_ms INTEGER NOT NULL
             );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('e1', 'general', 'k', '{}', 111, 222)",
            [],
        )
        .unwrap();
        assert!(
            conn.prepare("SELECT recorded_at_unix_ms FROM entities LIMIT 1").is_err(),
            "precondition: legacy table lacks the bi-temporal columns"
        );

        initialize_schema(&conn).expect("migrate legacy db to bi-temporal schema");

        // All six bi-temporal columns must now exist.
        for col in [
            "valid_from_unix_ms",
            "valid_to_unix_ms",
            "recorded_at_unix_ms",
            "invalidated_at_unix_ms",
            "supersedes",
            "superseded_by",
        ] {
            assert!(
                conn.prepare(&format!("SELECT {col} FROM entities LIMIT 1")).is_ok(),
                "column {col} must be added during migration"
            );
        }

        // recorded_at backfilled to created_at; the row is live (not invalidated)
        // and unbounded in valid time — i.e. unchanged in meaning.
        let recorded: i64 = conn
            .query_row("SELECT recorded_at_unix_ms FROM entities WHERE id='e1'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(recorded, 111, "recorded_at must backfill to created_at");
        let invalidated: Option<i64> = conn
            .query_row("SELECT invalidated_at_unix_ms FROM entities WHERE id='e1'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(invalidated, None, "existing rows must be live (not invalidated)");
        // v7 (#363): the historical "NULL = valid since recorded" convention is
        // made explicit — valid_from backfills to the transaction time.
        let valid_from: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entities WHERE id='e1'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(valid_from, Some(111), "v7 must backfill valid_from to recorded_at");
        let valid_to: Option<i64> = conn
            .query_row("SELECT valid_to_unix_ms FROM entities WHERE id='e1'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(valid_to, None, "valid_to must stay NULL (= still true)");

        let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(v, SCHEMA_VERSION);
    }

    #[test]
    fn v7_valid_from_backfill_is_idempotent_and_preserves_explicit_values() {
        // A v6-era DB: bi-temporal columns exist, valid_from is NULL on one row
        // and explicitly set on another. The v7 backfill must fill only the
        // NULL row, leave the explicit one alone, and be safely re-runnable.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("fresh init");
        conn.execute_batch(
            "INSERT INTO entities (id, category, key, body_json, recorded_at_unix_ms,
                                   valid_from_unix_ms, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('v7-null', 'note', 'a', '{}', 500, NULL, 400, 400),
                    ('v7-set',  'note', 'b', '{}', 500, 42,   400, 400);
             INSERT INTO entity_history (history_id, id, category, key, body_json,
                                         recorded_at_unix_ms, valid_from_unix_ms,
                                         created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('h1', 'v7-null', 'note', 'a', '{}', 300, NULL, 300, 300);
             PRAGMA user_version = 6;",
        )
        .unwrap();

        initialize_schema(&conn).expect("v6 -> v7 migration");

        let vf_null: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entities WHERE id='v7-null'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(vf_null, Some(500), "NULL valid_from must backfill to recorded_at");
        let vf_set: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entities WHERE id='v7-set'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(vf_set, Some(42), "explicit valid_from must be preserved");
        let vf_hist: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entity_history WHERE history_id='h1'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(vf_hist, Some(300), "history rows must backfill too");

        // Re-run (rewind the stamp): idempotent, values unchanged.
        conn.pragma_update(None, "user_version", 6).unwrap();
        initialize_schema(&conn).expect("v7 re-run");
        let vf_again: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entities WHERE id='v7-null'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(vf_again, Some(500));
        let vf_set_again: Option<i64> = conn
            .query_row("SELECT valid_from_unix_ms FROM entities WHERE id='v7-set'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(vf_set_again, Some(42));
    }

    #[test]
    fn fresh_db_has_bitemporal_columns_and_live_index() {
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        assert!(conn.prepare("SELECT invalidated_at_unix_ms FROM entities LIMIT 1").is_ok());
        let idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_entities_invalidated'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(idx, 1, "idx_entities_invalidated should be created on a fresh DB");
    }

    #[test]
    fn creates_recall_ranking_index() {
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        // Index must exist...
        let exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_entities_recall'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(exists, 1, "idx_entities_recall should be created");
        // ...and the recall browse query must use it with NO temp-b-tree sort.
        // Uses the FULL 3-key ordering the recall path actually emits (db.rs:
        // "ORDER BY retrieval_count DESC, last_accessed_unix_ms DESC, id ASC"),
        // including the #254 determinism tie-break. This is the v13 guard: the
        // pre-v13 index covered only the first two keys, so this exact query
        // needed a TEMP B-TREE to order the tie-break — the O(tie-group) sort
        // that made browse ~30ms at 1M rows.
        let plan: Vec<String> = conn
            .prepare(
                "EXPLAIN QUERY PLAN SELECT id FROM entities WHERE archived = 0 \
                 ORDER BY retrieval_count DESC, last_accessed_unix_ms DESC, id ASC LIMIT 20",
            )
            .unwrap()
            .query_map([], |r| r.get::<_, String>(3))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        let joined = plan.join(" | ");
        assert!(
            joined.contains("idx_entities_recall"),
            "recall query should use idx_entities_recall, got: {joined}"
        );
        assert!(
            !joined.to_uppercase().contains("TEMP B-TREE"),
            "recall browse (incl. id tie-break) should not need a temp-b-tree sort, got: {joined}"
        );
    }

    #[test]
    fn fresh_db_has_communities_table() {
        // v8 (#365): GraphRAG communities table + workspace index.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("init schema");
        assert!(
            conn.prepare("SELECT id, workspace_hash, member_ids, member_digest, summary, \
                          summary_entity_id, algorithm, modularity, member_count, \
                          generated_at_unix_ms FROM communities LIMIT 1")
                .is_ok(),
            "communities table with all v8 columns must exist on a fresh DB"
        );
        let idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_communities_ws'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(idx, 1, "idx_communities_ws must be created");
    }

    #[test]
    fn migrates_legacy_db_to_v8_with_communities_table() {
        // A v6-era DB (fully migrated through emb_sig, user_version=6) must
        // gain the communities table when re-opened, and land on the current
        // SCHEMA_VERSION, without disturbing existing data.
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("fresh init");
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('v8-keep', 'note', 'k', '{}', 0, 0)",
            [],
        )
        .unwrap();
        // Rewind to the v6 state: communities table gone, version 6.
        conn.execute_batch(
            "DROP INDEX IF EXISTS idx_communities_ws;
             DROP TABLE IF EXISTS communities;
             PRAGMA user_version = 6;",
        )
        .unwrap();
        assert!(
            conn.prepare("SELECT id FROM communities LIMIT 1").is_err(),
            "precondition: v6 DB lacks the communities table"
        );

        initialize_schema(&conn).expect("v6 -> v8 migration");

        assert!(conn.prepare("SELECT id FROM communities LIMIT 1").is_ok());
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
        let kept: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities WHERE id='v8-keep'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(kept, 1, "migration must not drop data");
    }

    #[test]
    fn migrates_legacy_db_to_v10_with_dedup_signatures() {
        // v10 (#392): a v9-era DB must gain the dedup_signatures side table on
        // reopen, land on the current SCHEMA_VERSION, and keep its data. No
        // eager backfill: pre-existing entities simply have no signature row
        // (the dedup scan rebuilds and writes back lazily).
        let (conn, _path) = temp_db();
        initialize_schema(&conn).expect("fresh init");
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES ('v10-keep', 'note', 'k', '{}', 0, 0)",
            [],
        )
        .unwrap();
        // Rewind to the v9 state: side tables gone, version 9.
        conn.execute_batch(
            "DROP TABLE IF EXISTS dedup_signatures;
             DROP TABLE IF EXISTS dedup_signature_blobs;
             PRAGMA user_version = 9;",
        )
        .unwrap();
        assert!(
            conn.prepare("SELECT entity_id FROM dedup_signatures LIMIT 1").is_err(),
            "precondition: v9 DB lacks the dedup_signatures table"
        );

        initialize_schema(&conn).expect("v9 -> v10 migration");

        assert!(
            conn.prepare(
                "SELECT entity_id, body_len, body_hash, tg_count, histo \n                 FROM dedup_signatures LIMIT 1"
            )
            .is_ok(),
            "dedup_signatures with all v10 columns must exist"
        );
        assert!(
            conn.prepare("SELECT entity_id, sig FROM dedup_signature_blobs LIMIT 1").is_ok(),
            "dedup_signature_blobs must exist"
        );
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, SCHEMA_VERSION);
        let kept: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities WHERE id='v10-keep'", [], |r| r.get(0))
            .unwrap();
        assert_eq!(kept, 1, "migration must not drop data");
        // Lazy backfill: the migration itself writes NO signatures.
        let sigs: i64 = conn
            .query_row("SELECT COUNT(*) FROM dedup_signatures", [], |r| r.get(0))
            .unwrap();
        assert_eq!(sigs, 0, "v10 must not eagerly backfill signatures");
    }

    #[test]
    fn migrates_legacy_db_to_v11_with_journal_workspace_hash() {
        // v11 (#417): a v10-era journal table has no workspace_hash column. The
        // gated migration must ALTER it in, create the (category,key,workspace)
        // index, stamp SCHEMA_VERSION, and preserve existing rows (defaulting to
        // an empty workspace_hash).
        let (conn, _path) = temp_db();
        // A pre-v11 journal table: all v10 columns, but no workspace_hash.
        conn.execute_batch(
            "CREATE TABLE journal (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL DEFAULT 'decision',
                evaluated_json TEXT DEFAULT '{}',
                acted_json TEXT DEFAULT '{}',
                forward_json TEXT DEFAULT '{}',
                category TEXT DEFAULT '',
                key TEXT DEFAULT '',
                entity_id TEXT DEFAULT '',
                agent_id TEXT DEFAULT '',
                audit_hash TEXT DEFAULT '',
                created_at_unix_ms INTEGER NOT NULL
             );
             INSERT INTO journal (id, category, key, created_at_unix_ms)
                 VALUES ('jrn-old', 'facts', 'k', 1);
             PRAGMA user_version = 10;",
        )
        .unwrap();
        assert!(
            conn.prepare("SELECT workspace_hash FROM journal LIMIT 1").is_err(),
            "precondition: v10 journal lacks workspace_hash"
        );

        initialize_schema(&conn).expect("v10 -> v11 migration");

        // Column added, index created, version stamped.
        assert!(
            conn.prepare("SELECT workspace_hash FROM journal LIMIT 1").is_ok(),
            "workspace_hash column must be added"
        );
        let has_idx: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_journal_catkeyws'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(has_idx, 1, "purge-match index must be created");
        let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
        assert_eq!(v, SCHEMA_VERSION);
        // Legacy row preserved, defaulting to empty workspace_hash.
        let (kept, ws): (i64, String) = conn
            .query_row(
                "SELECT COUNT(*), COALESCE(MAX(workspace_hash), '') FROM journal WHERE id='jrn-old'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(kept, 1, "migration must not drop journal rows");
        assert_eq!(ws, "", "legacy journal rows default to empty workspace_hash");
    }

    #[test]
    fn detects_v0_1_memories_table() {
        let (conn, _path) = temp_db();
        conn.execute_batch("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT);")
            .expect("create v0.1 memories");
        assert!(has_v0_1_memories(&conn).unwrap());
        assert!(!is_v0_2_0(&conn).unwrap());
    }

    #[test]
    fn migration_from_v0_1_preserves_data() {
        let (old_conn, old_path) = temp_db();
        old_conn
            .execute_batch(
                "CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    type TEXT DEFAULT 'insight', summary TEXT DEFAULT '',
                    relevance REAL DEFAULT 0.0, decay_score REAL DEFAULT 1.0,
                    retrieval_count INTEGER DEFAULT 0, layer TEXT DEFAULT 'working',
                    topic_path TEXT DEFAULT '', created_at_unix_ms INTEGER NOT NULL,
                    last_accessed_unix_ms INTEGER NOT NULL, workspace_hash TEXT DEFAULT '',
                    tags TEXT DEFAULT '{}', links TEXT DEFAULT '[]', source TEXT DEFAULT 'mimir',
                    verified INTEGER DEFAULT 0
                );",
            )
            .expect("create v0.1 schema");

        let now = now_ms();
        old_conn
            .execute(
                "INSERT INTO memories (id, content, type, created_at_unix_ms, last_accessed_unix_ms)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params!["mem-test1", "Test content", "insight", now, now],
            )
            .expect("insert test memory");
        drop(old_conn);

        let (new_conn, _new_path) = temp_db();
        let report = migrate_from_v0_1(&old_path, &new_conn).expect("migrate");

        assert_eq!(report.total_old_memories, 1);
        assert_eq!(report.entities_created, 1);
        assert!(report.errors.is_empty());

        // Verify entity exists
        let count: i64 = new_conn
            .query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // Verify body_json contains original content
        let body: String = new_conn
            .query_row(
                "SELECT body_json FROM entities WHERE id = 'mem-test1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(body.contains("Test content"));

        // Cleanup old db
        let _ = std::fs::remove_file(&old_path);
    }

    #[test]
    fn truncate_at_char_boundary_never_splits_chars() {
        // Shorter than the limit: unchanged.
        assert_eq!(truncate_at_char_boundary("abc", 20), "abc");
        // Exactly at the limit: unchanged.
        assert_eq!(truncate_at_char_boundary("a".repeat(20).as_str(), 20), "a".repeat(20));
        // ASCII over the limit: plain byte cut.
        assert_eq!(truncate_at_char_boundary("abcdef", 3), "abc");
        // Multi-byte char straddling the cut point: back up to the boundary.
        // "é" is 2 bytes; cutting "aé" at byte 2 lands mid-char.
        assert_eq!(truncate_at_char_boundary("aéz", 2), "a");
        // 4-byte char straddling the cut.
        assert_eq!(truncate_at_char_boundary("ab😀z", 4), "ab");
        // Degenerate limit 0.
        assert_eq!(truncate_at_char_boundary("é", 0), "");
    }

    #[test]
    fn migration_from_v0_1_handles_multibyte_id_without_panic() {
        // #352: a legacy id whose multi-byte UTF-8 char straddles byte offset
        // 20 used to panic in the byte-index slice building `key`, aborting
        // the whole one-time migration. The char at bytes 19..21 ("é") is the
        // exact repro from the issue.
        let (old_conn, old_path) = temp_db();
        old_conn
            .execute_batch(
                "CREATE TABLE memories (
                    id TEXT PRIMARY KEY, content TEXT NOT NULL,
                    type TEXT DEFAULT 'insight', summary TEXT DEFAULT '',
                    relevance REAL DEFAULT 0.0, decay_score REAL DEFAULT 1.0,
                    retrieval_count INTEGER DEFAULT 0, layer TEXT DEFAULT 'working',
                    topic_path TEXT DEFAULT '', created_at_unix_ms INTEGER NOT NULL,
                    last_accessed_unix_ms INTEGER NOT NULL, workspace_hash TEXT DEFAULT '',
                    tags TEXT DEFAULT '{}', links TEXT DEFAULT '[]', source TEXT DEFAULT 'mimir',
                    verified INTEGER DEFAULT 0
                );",
            )
            .expect("create v0.1 schema");

        // 19 ASCII bytes, then a 2-byte char occupying bytes 19..21.
        let evil_id = format!("{}é-tail", "x".repeat(19));
        assert!(!evil_id.is_char_boundary(20), "precondition: byte 20 is mid-char");
        let now = now_ms();
        old_conn
            .execute(
                "INSERT INTO memories (id, content, type, created_at_unix_ms, last_accessed_unix_ms)
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                params![evil_id, "Unicode id content", "insight", now, now],
            )
            .expect("insert multibyte-id memory");
        drop(old_conn);

        let (new_conn, _new_path) = temp_db();
        let report = migrate_from_v0_1(&old_path, &new_conn).expect("migrate must not panic");

        assert_eq!(report.total_old_memories, 1);
        assert_eq!(report.entities_created, 1);
        assert!(report.errors.is_empty(), "errors: {:?}", report.errors);

        let key: String = new_conn
            .query_row(
                "SELECT key FROM entities WHERE id = ?1",
                params![evil_id],
                |r| r.get(0),
            )
            .unwrap();
        // Boundary walked back from 20 to 19: the é is dropped, not split.
        assert_eq!(key, format!("migrated-{}", "x".repeat(19)));

        let _ = std::fs::remove_file(&old_path);
    }

    #[test]
    fn gather_stats_returns_expected_shape() {
        let (conn, path) = temp_db();
        initialize_schema(&conn).expect("init schema");

        let now = now_ms();
        conn.execute(
            "INSERT INTO entities (id, category, key, body_json, created_at_unix_ms, last_accessed_unix_ms)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params!["mem-1", "decision", "test-decision", "{}", now, now],
        )
        .unwrap();

        let stats = gather_stats(&conn, &path).unwrap();
        assert_eq!(stats.total_entities, 1);
        assert!(stats.db_file_size_bytes > 0);
        let _ = std::fs::remove_file(&path);
    }
}
