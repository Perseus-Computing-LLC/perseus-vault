//! #871: durable long-running operation states — shared run/run-item contract.
//!
//! Persists a bounded status, scope, input digest, progress counters,
//! per-item receipts, error class, and sanitized error detail for
//! long-running Vault operations (maintenance, embed flush, consolidation,
//! export/import, reindex). Restart recovery marks in-flight work
//! `interrupted` (mark-only — resume only via an explicit bounded, scoped,
//! idempotent retry). See docs/specs/durable-op-states.md.
//!
//! State machine (fail-closed; terminal states accept no further
//! transitions):
//! ```text
//! queued → running → completed | failed | cancelled | interrupted
//! queued → cancelled | interrupted | failed_to_start
//! ```
//! Orthogonal flags: `partial`, `timeout`, `stale`.

use rusqlite::{params, Connection, OptionalExtension};
use serde::Serialize;

pub(crate) fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}

/// Maximum stored length of a sanitized error detail.
const ERROR_DETAIL_CAP: usize = 500;
/// Default retry bound when a caller does not specify one.
// (retry budget is caller-supplied per run; the MCP default is 2)

/// #871: run lifecycle states.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum OpRunState {
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
    Interrupted,
    FailedToStart,
}

impl OpRunState {
    pub fn as_str(&self) -> &'static str {
        match self {
            OpRunState::Queued => "queued",
            OpRunState::Running => "running",
            OpRunState::Completed => "completed",
            OpRunState::Failed => "failed",
            OpRunState::Cancelled => "cancelled",
            OpRunState::Interrupted => "interrupted",
            OpRunState::FailedToStart => "failed_to_start",
        }
    }

    pub fn parse(s: &str) -> Option<OpRunState> {
        match s {
            "queued" => Some(OpRunState::Queued),
            "running" => Some(OpRunState::Running),
            "completed" => Some(OpRunState::Completed),
            "failed" => Some(OpRunState::Failed),
            "cancelled" => Some(OpRunState::Cancelled),
            "interrupted" => Some(OpRunState::Interrupted),
            "failed_to_start" => Some(OpRunState::FailedToStart),
            _ => None,
        }
    }

    pub fn is_terminal(&self) -> bool {
        matches!(
            self,
            OpRunState::Completed
                | OpRunState::Failed
                | OpRunState::Cancelled
                | OpRunState::Interrupted
                | OpRunState::FailedToStart
        )
    }

    /// `queued|running` — the run is in flight (not yet terminal).
    pub fn is_inflight(&self) -> bool {
        matches!(self, OpRunState::Queued | OpRunState::Running)
    }
}

/// One durable run record (`op_runs` row).
#[derive(Debug, Clone, Serialize)]
pub struct OpRun {
    pub id: String,
    pub op_type: String,
    pub state: OpRunState,
    pub partial: bool,
    pub timeout: bool,
    pub stale: bool,
    pub scope: String,
    pub input_digest: String,
    pub items_total: i64,
    pub items_done: i64,
    pub items_failed: i64,
    pub items_unattempted: i64,
    pub error_class: String,
    pub error_detail: String,
    pub receipt: String,
    pub retry_count: i64,
    pub max_retries: i64,
    pub parent_run_id: String,
    pub created_by: String,
    pub created_at_unix_ms: i64,
    pub started_at_unix_ms: Option<i64>,
    pub updated_at_unix_ms: i64,
    pub finished_at_unix_ms: Option<i64>,
}

/// One per-item receipt (`op_run_items` row).
#[derive(Debug, Clone, Serialize)]
pub struct OpRunItem {
    pub id: String,
    pub run_id: String,
    pub item_ref: String,
    pub item_digest: String,
    pub state: OpRunState,
    pub receipt_ref: String,
    pub error_class: String,
    pub error_detail: String,
    pub retry_count: i64,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    pub finished_at_unix_ms: Option<i64>,
}

fn new_id(prefix: &str) -> String {
    format!("{prefix}-{}", uuid::Uuid::new_v4().simple())
}

/// #871: sanitize an error detail before it is persisted or served.
/// Secrets must never reach the store or operator surfaces: `sk-…` tokens,
/// `KEY=value` / `token=…` assignments, `Bearer …` credentials, and 32+
/// character hex/base64 blobs are masked; length is bounded.
pub fn sanitize_error_detail(detail: &str) -> String {
    let mut out = String::with_capacity(detail.len().min(ERROR_DETAIL_CAP) + 32);
    let mut chars = detail.chars().peekable();
    while let Some(c) = chars.next() {
        // 32+ char hex/base64-looking run -> [REDACTED]
        if c.is_ascii_hexdigit() {
            let mut run = String::from(c);
            while let Some(&n) = chars.peek() {
                if n.is_ascii_hexdigit() || n == '-' {
                    run.push(n);
                    chars.next();
                } else {
                    break;
                }
            }
            if run.len() >= 32 {
                out.push_str("[REDACTED]");
            } else {
                out.push_str(&run);
            }
            continue;
        }
        // KEY=value / token=... / bearer ... assignments
        if c == '=' || c == ':' {
            let tail: String = chars.clone().take(3).collect();
            if tail
                .chars()
                .all(|t| !t.is_whitespace() && t != '=' && t != ':')
            {
                out.push(c);
                // consume the value up to a delimiter; mask non-trivial values
                let mut val = String::new();
                while let Some(&v) = chars.peek() {
                    if v == ' ' || v == '\n' || v == '\r' || v == '\t' || v == ',' || v == '"' {
                        break;
                    }
                    val.push(v);
                    chars.next();
                }
                if val.len() >= 4 {
                    out.push_str("[REDACTED]");
                } else {
                    out.push_str(&val);
                }
                continue;
            }
            out.push(c);
            continue;
        }
        out.push(c);
    }
    // Whole-string credential forms (Bearer ..., sk-..., Authorization header).
    let mut lower = out.to_lowercase();
    let mut masked = out;
    for marker in ["bearer ", "sk-", "authorization ", "x-api-key "] {
        // Mask from the marker itself: replacing only the value would leave
        // the marker in the output and re-match forever (infinite loop).
        while let Some(idx) = lower.find(marker) {
            let rest: String = masked.chars().skip(idx).collect();
            let end = rest
                .find(|ch: char| ch.is_whitespace() || ch == ',' || ch == '"')
                .unwrap_or(rest.len());
            let value: String = rest
                .chars()
                .skip(marker.len())
                .take(end.saturating_sub(marker.len()))
                .collect();
            if value.len() >= 4 {
                masked = masked.chars().take(idx).collect::<String>()
                    + "[REDACTED]"
                    + &rest.chars().skip(end).collect::<String>();
                // Re-derive lower on the masked string for the next marker.
                let mut m = String::new();
                let mut it = masked.chars();
                while let Some(ch) = it.next() {
                    m.push(ch.to_ascii_lowercase());
                }
                lower.replace_range(.., &m);
            } else {
                break;
            }
        }
    }
    let mut s = masked;
    if s.chars().count() > ERROR_DETAIL_CAP {
        let budget = ERROR_DETAIL_CAP.saturating_sub("[truncated]".len() + 1);
        s = s.chars().take(budget).collect::<String>() + "…[truncated]";
    }
    s
}

fn row_to_run(row: &rusqlite::Row<'_>) -> rusqlite::Result<OpRun> {
    let state: String = row.get(2)?;
    Ok(OpRun {
        id: row.get(0)?,
        op_type: row.get(1)?,
        state: OpRunState::parse(&state).unwrap_or(OpRunState::Interrupted),
        partial: row.get::<_, i64>(3)? != 0,
        timeout: row.get::<_, i64>(4)? != 0,
        stale: row.get::<_, i64>(5)? != 0,
        scope: row.get(6)?,
        input_digest: row.get(7)?,
        items_total: row.get(8)?,
        items_done: row.get(9)?,
        items_failed: row.get(10)?,
        items_unattempted: row.get(11)?,
        error_class: row.get(12)?,
        error_detail: row.get(13)?,
        receipt: row.get(14)?,
        retry_count: row.get(15)?,
        max_retries: row.get(16)?,
        parent_run_id: row.get(17)?,
        created_by: row.get(18)?,
        created_at_unix_ms: row.get(19)?,
        started_at_unix_ms: row.get(20)?,
        updated_at_unix_ms: row.get(21)?,
        finished_at_unix_ms: row.get(22)?,
    })
}

const RUN_COLS: &str = "id, op_type, state, partial, timeout, stale, scope, \
     input_digest, items_total, items_done, items_failed, items_unattempted, \
     error_class, error_detail, receipt, retry_count, max_retries, parent_run_id, \
     created_by, created_at_unix_ms, started_at_unix_ms, updated_at_unix_ms, \
     finished_at_unix_ms";

fn row_to_item(row: &rusqlite::Row<'_>) -> rusqlite::Result<OpRunItem> {
    let state: String = row.get(3)?;
    Ok(OpRunItem {
        id: row.get(0)?,
        run_id: row.get(1)?,
        item_ref: row.get(2)?,
        item_digest: row.get(4)?,
        state: OpRunState::parse(&state).unwrap_or(OpRunState::Interrupted),
        receipt_ref: row.get(5)?,
        error_class: row.get(6)?,
        error_detail: row.get(7)?,
        retry_count: row.get(8)?,
        created_at_unix_ms: row.get(9)?,
        updated_at_unix_ms: row.get(10)?,
        finished_at_unix_ms: row.get(11)?,
    })
}

const ITEM_COLS: &str = "id, run_id, item_ref, state, item_digest, receipt_ref, \
     error_class, error_detail, retry_count, created_at_unix_ms, updated_at_unix_ms, \
     finished_at_unix_ms";

fn get_run(conn: &Connection, id: &str) -> Result<Option<OpRun>, String> {
    let sql = format!("SELECT {RUN_COLS} FROM op_runs WHERE id = ?1");
    conn.query_row(&sql, params![id], row_to_run)
        .optional()
        .map_err(|e| format!("op_run get: {e}"))
}

fn assert_state(
    conn: &Connection,
    id: &str,
    allowed: &[OpRunState],
    action: &str,
) -> Result<OpRun, String> {
    let run = get_run(conn, id)?.ok_or_else(|| format!("unknown op run: {id}"))?;
    if !allowed.contains(&run.state) {
        return Err(format!(
            "invalid op_run transition: {action} from {} (run {id})",
            run.state.as_str()
        ));
    }
    Ok(run)
}

/// Create a run in `queued` state. `op_type` is one of the known operation
/// kinds (see the spec) or a custom string; `max_retries` is bounded to
/// `[0, 10]` fail-closed.
pub fn begin(
    conn: &Connection,
    op_type: &str,
    scope: &str,
    input_digest: &str,
    max_retries: i64,
    created_by: &str,
) -> Result<OpRun, String> {
    if op_type.trim().is_empty() || op_type.chars().count() > 64 {
        return Err("op_run begin: invalid op_type".to_string());
    }
    let max_retries = max_retries.clamp(0, 10);
    let now = now_ms();
    let run = OpRun {
        id: new_id("opr"),
        op_type: op_type.trim().to_string(),
        state: OpRunState::Queued,
        partial: false,
        timeout: false,
        stale: false,
        scope: scope.to_string(),
        input_digest: input_digest.to_string(),
        items_total: 0,
        items_done: 0,
        items_failed: 0,
        items_unattempted: 0,
        error_class: String::new(),
        error_detail: String::new(),
        receipt: String::new(),
        retry_count: 0,
        max_retries,
        parent_run_id: String::new(),
        created_by: created_by.to_string(),
        created_at_unix_ms: now,
        started_at_unix_ms: None,
        updated_at_unix_ms: now,
        finished_at_unix_ms: None,
    };
    conn.execute(
        "INSERT INTO op_runs (id, op_type, state, partial, timeout, stale, scope, \
         input_digest, items_total, items_done, items_failed, items_unattempted, \
         error_class, error_detail, receipt, retry_count, max_retries, parent_run_id, \
         created_by, created_at_unix_ms, started_at_unix_ms, updated_at_unix_ms, \
         finished_at_unix_ms) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,?15,?16,?17,?18,?19,?20,?21,?22,?23)",
        params![
            run.id, run.op_type, run.state.as_str(), 0i64, 0i64, 0i64, run.scope,
            run.input_digest, 0i64, 0i64, 0i64, 0i64, "", "", "", run.retry_count,
            run.max_retries, run.parent_run_id, run.created_by, run.created_at_unix_ms,
            Option::<i64>::None, run.updated_at_unix_ms, Option::<i64>::None
        ],
    )
    .map_err(|e| format!("op_run begin: {e}"))?;
    Ok(run)
}

/// `queued → running`. Records `started_at`.
pub fn start(conn: &Connection, id: &str) -> Result<OpRun, String> {
    let run = assert_state(conn, id, &[OpRunState::Queued], "start")?;
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET state = 'running', started_at_unix_ms = ?1, \
         updated_at_unix_ms = ?1 WHERE id = ?2",
        params![now, id],
    )
    .map_err(|e| format!("op_run start: {e}"))?;
    Ok(OpRun {
        state: OpRunState::Running,
        started_at_unix_ms: Some(now),
        updated_at_unix_ms: now,
        ..run
    })
}

/// Bounded progress update. Counters are clamped to non-negative values;
/// `partial` is derived (done > 0 && !all done). `total` updates the
/// expected item count when provided (>= 0).
pub fn progress(
    conn: &Connection,
    id: &str,
    done: i64,
    failed: i64,
    total: i64,
) -> Result<OpRun, String> {
    let run = assert_state(conn, id, &[OpRunState::Running], "progress")?;
    let done = done.max(0);
    let failed = failed.max(0);
    let total = if total >= 0 { total } else { run.items_total };
    let unattempted = (total - done - failed).max(0);
    let partial = done > 0 && (done + failed) < total;
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET items_done = ?1, items_failed = ?2, items_total = ?3, \
         items_unattempted = ?4, partial = ?5, updated_at_unix_ms = ?6 WHERE id = ?7",
        params![done, failed, total, unattempted, partial as i64, now, id],
    )
    .map_err(|e| format!("op_run progress: {e}"))?;
    Ok(OpRun {
        items_done: done,
        items_failed: failed,
        items_total: total,
        items_unattempted: unattempted,
        partial,
        updated_at_unix_ms: now,
        ..run
    })
}

/// `running → completed`. `receipt` links the terminal outcome (journal
/// event id / artifact ref). A run that completed with zero items is an
/// explicit no-op success — never conflated with failure.
pub fn complete(conn: &Connection, id: &str, receipt: &str) -> Result<OpRun, String> {
    let run = assert_state(conn, id, &[OpRunState::Running], "complete")?;
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET state = 'completed', receipt = ?1, finished_at_unix_ms = ?2, \
         updated_at_unix_ms = ?2, items_unattempted = 0 WHERE id = ?3",
        params![receipt, now, id],
    )
    .map_err(|e| format!("op_run complete: {e}"))?;
    Ok(OpRun {
        state: OpRunState::Completed,
        receipt: receipt.to_string(),
        items_unattempted: 0,
        finished_at_unix_ms: Some(now),
        updated_at_unix_ms: now,
        ..run
    })
}

/// `running → failed` (or `queued → failed_to_start` when `to_start`).
/// `detail` is sanitized before it is stored.
pub fn fail(
    conn: &Connection,
    id: &str,
    error_class: &str,
    detail: &str,
    to_start: bool,
) -> Result<OpRun, String> {
    let from = if to_start {
        &[OpRunState::Queued][..]
    } else {
        &[OpRunState::Running][..]
    };
    let run = assert_state(
        conn,
        id,
        from,
        if to_start { "failed_to_start" } else { "fail" },
    )?;
    let detail = sanitize_error_detail(detail);
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET state = ?1, error_class = ?2, error_detail = ?3, \
         finished_at_unix_ms = ?4, updated_at_unix_ms = ?4 WHERE id = ?5",
        params![
            if to_start {
                "failed_to_start"
            } else {
                "failed"
            },
            error_class,
            detail,
            now,
            id
        ],
    )
    .map_err(|e| format!("op_run fail: {e}"))?;
    Ok(OpRun {
        state: if to_start {
            OpRunState::FailedToStart
        } else {
            OpRunState::Failed
        },
        error_class: error_class.to_string(),
        error_detail: detail,
        finished_at_unix_ms: Some(now),
        updated_at_unix_ms: now,
        ..run
    })
}

/// `queued|running → cancelled`.
pub fn cancel(conn: &Connection, id: &str) -> Result<OpRun, String> {
    let run = assert_state(
        conn,
        id,
        &[OpRunState::Queued, OpRunState::Running],
        "cancel",
    )?;
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET state = 'cancelled', finished_at_unix_ms = ?1, \
         updated_at_unix_ms = ?1 WHERE id = ?2",
        params![now, id],
    )
    .map_err(|e| format!("op_run cancel: {e}"))?;
    Ok(OpRun {
        state: OpRunState::Cancelled,
        finished_at_unix_ms: Some(now),
        updated_at_unix_ms: now,
        ..run
    })
}

/// `running → failed` with `timeout` set (bounded deadline exceeded).
pub fn timeout(conn: &Connection, id: &str) -> Result<OpRun, String> {
    let _run = assert_state(conn, id, &[OpRunState::Running], "timeout")?;
    let now = now_ms();
    conn.execute(
        "UPDATE op_runs SET state = 'failed', timeout = 1, error_class = 'timeout', \
         error_detail = 'bounded deadline exceeded', finished_at_unix_ms = ?1, \
         updated_at_unix_ms = ?1 WHERE id = ?2",
        params![now, id],
    )
    .map_err(|e| format!("op_run timeout: {e}"))?;
    // Fresh counters (item transitions updated them before this call).
    get_run(conn, id)?.ok_or_else(|| format!("unknown op run: {id}"))
}

/// Add a per-item receipt in `queued` state. `UNIQUE(run_id, item_ref)`.
pub fn item_add(
    conn: &Connection,
    run_id: &str,
    item_ref: &str,
    item_digest: &str,
) -> Result<OpRunItem, String> {
    let run = assert_state(conn, run_id, &[OpRunState::Running], "item_add")?;
    let now = now_ms();
    let item = OpRunItem {
        id: new_id("opi"),
        run_id: run_id.to_string(),
        item_ref: item_ref.to_string(),
        item_digest: item_digest.to_string(),
        state: OpRunState::Queued,
        receipt_ref: String::new(),
        error_class: String::new(),
        error_detail: String::new(),
        retry_count: 0,
        created_at_unix_ms: now,
        updated_at_unix_ms: now,
        finished_at_unix_ms: None,
    };
    conn.execute(
        "INSERT INTO op_run_items (id, run_id, item_ref, state, item_digest, receipt_ref, \
         error_class, error_detail, retry_count, created_at_unix_ms, updated_at_unix_ms, \
         finished_at_unix_ms) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
        params![
            item.id,
            item.run_id,
            item.item_ref,
            item.state.as_str(),
            item.item_digest,
            "",
            "",
            "",
            0i64,
            item.created_at_unix_ms,
            item.updated_at_unix_ms,
            Option::<i64>::None
        ],
    )
    .map_err(|e| format!("op_run item_add: {e}"))?;
    let total = run.items_total + 1;
    let unattempted = total - run.items_done - run.items_failed;
    conn.execute(
        "UPDATE op_runs SET items_total = ?1, items_unattempted = ?2, \
         updated_at_unix_ms = ?3 WHERE id = ?4",
        params![total, unattempted.max(0), now, run_id],
    )
    .map_err(|e| format!("op_run item_add counters: {e}"))?;
    Ok(item)
}

fn get_item(conn: &Connection, run_id: &str, item_ref: &str) -> Result<Option<OpRunItem>, String> {
    let sql = format!("SELECT {ITEM_COLS} FROM op_run_items WHERE run_id = ?1 AND item_ref = ?2");
    conn.query_row(&sql, params![run_id, item_ref], row_to_item)
        .optional()
        .map_err(|e| format!("op_run item get: {e}"))
}

fn assert_item_state(
    conn: &Connection,
    run_id: &str,
    item_ref: &str,
    allowed: &[OpRunState],
    action: &str,
) -> Result<OpRunItem, String> {
    let item = get_item(conn, run_id, item_ref)?
        .ok_or_else(|| format!("unknown op_run item {item_ref} in run {run_id}"))?;
    if !allowed.contains(&item.state) {
        return Err(format!(
            "invalid op_run item transition: {action} from {} (item {item_ref})",
            item.state.as_str()
        ));
    }
    Ok(item)
}

fn touch_item(
    conn: &Connection,
    item: OpRunItem,
    state: OpRunState,
    receipt_ref: &str,
    error_class: &str,
    detail: &str,
) -> Result<OpRunItem, String> {
    let now = now_ms();
    let finished = if state.is_terminal() { Some(now) } else { None };
    conn.execute(
        "UPDATE op_run_items SET state = ?1, receipt_ref = ?2, error_class = ?3, \
         error_detail = ?4, updated_at_unix_ms = ?5, finished_at_unix_ms = ?6 \
         WHERE id = ?7",
        params![
            state.as_str(),
            receipt_ref,
            error_class,
            detail,
            now,
            finished,
            item.id
        ],
    )
    .map_err(|e| format!("op_run item touch: {e}"))?;
    // Keep run-level counters in sync after every item transition. SQLite
    // evaluates all SET-clause RHS against the OLD row, so compute the
    // deltas in Rust rather than in the UPDATE expression.
    let (total, done, failed): (i64, i64, i64) = conn
        .query_row(
            "SELECT items_total,
               (SELECT COUNT(*) FROM op_run_items WHERE run_id = ?1 AND state = 'completed'),
               (SELECT COUNT(*) FROM op_run_items WHERE run_id = ?1 AND state = 'failed')
             FROM op_runs WHERE id = ?1",
            params![item.run_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .map_err(|e| format!("op_run item touch counters read: {e}"))?;
    conn.execute(
        "UPDATE op_runs SET items_done = ?2, items_failed = ?3, \
         items_unattempted = ?4, updated_at_unix_ms = ?5 WHERE id = ?1",
        params![
            item.run_id,
            done,
            failed,
            (total - done - failed).max(0),
            now
        ],
    )
    .map_err(|e| format!("op_run item touch counters: {e}"))?;
    Ok(OpRunItem {
        state,
        receipt_ref: receipt_ref.to_string(),
        error_class: error_class.to_string(),
        error_detail: detail.to_string(),
        updated_at_unix_ms: now,
        finished_at_unix_ms: finished,
        ..item
    })
}

/// Per-item `queued → running`.
pub fn item_start(conn: &Connection, run_id: &str, item_ref: &str) -> Result<OpRunItem, String> {
    let item = assert_item_state(conn, run_id, item_ref, &[OpRunState::Queued], "item_start")?;
    touch_item(conn, item, OpRunState::Running, "", "", "")
}

/// Per-item `running → completed` with its receipt linkage.
pub fn item_complete(
    conn: &Connection,
    run_id: &str,
    item_ref: &str,
    receipt_ref: &str,
) -> Result<OpRunItem, String> {
    let item = assert_item_state(
        conn,
        run_id,
        item_ref,
        &[OpRunState::Running],
        "item_complete",
    )?;
    touch_item(conn, item, OpRunState::Completed, receipt_ref, "", "")
}

/// Per-item `running → failed` with sanitized detail.
pub fn item_fail(
    conn: &Connection,
    run_id: &str,
    item_ref: &str,
    error_class: &str,
    detail: &str,
) -> Result<OpRunItem, String> {
    let item = assert_item_state(conn, run_id, item_ref, &[OpRunState::Running], "item_fail")?;
    touch_item(
        conn,
        item,
        OpRunState::Failed,
        "",
        error_class,
        &sanitize_error_detail(detail),
    )
}

/// Per-item `queued|running → cancelled`.
pub fn item_cancel(conn: &Connection, run_id: &str, item_ref: &str) -> Result<OpRunItem, String> {
    let item = assert_item_state(
        conn,
        run_id,
        item_ref,
        &[OpRunState::Queued, OpRunState::Running],
        "item_cancel",
    )?;
    touch_item(conn, item, OpRunState::Cancelled, "", "", "")
}

/// Bounded, scoped, idempotent retry. Forks a NEW child run:
/// - refused fail-closed when `retry_count >= max_retries` (`retry_exhausted`);
/// - refused when nothing is recoverable (`nothing_to_retry`);
/// - `completed` items are carried into the child as completed WITH their
///   `receipt_ref` — never re-executed, so retry cannot duplicate writes or
///   receipts;
/// - `failed | cancelled | interrupted | queued | running` items are
///   re-queued in the child (running items count as interrupted here).
pub fn retry(conn: &Connection, id: &str) -> Result<OpRun, String> {
    let run = get_run(conn, id)?.ok_or_else(|| format!("unknown op run: {id}"))?;
    if run.state.is_inflight() {
        return Err(format!(
            "op_run retry refused: run {} is still {} (not terminal)",
            id,
            run.state.as_str()
        ));
    }
    if run.retry_count >= run.max_retries {
        return Err(format!(
            "op_run retry: retry_exhausted (max {}) for run {id}",
            run.max_retries
        ));
    }
    let items = list_items(conn, id)?;
    let recoverable = items
        .iter()
        .filter(|i| i.state != OpRunState::Completed)
        .count();
    if recoverable == 0 {
        return Err(format!("op_run retry: nothing_to_retry for run {id}"));
    }
    let tx = conn
        .unchecked_transaction()
        .map_err(|e| format!("op_run retry tx: {e}"))?;
    let child = begin(
        &tx,
        &run.op_type,
        &run.scope,
        &run.input_digest,
        run.max_retries,
        &run.created_by,
    )?;
    tx.execute(
        "UPDATE op_runs SET retry_count = ?1, parent_run_id = ?2, updated_at_unix_ms = ?3 \
         WHERE id = ?4",
        params![run.retry_count + 1, run.id, now_ms(), child.id],
    )
    .map_err(|e| format!("op_run retry parent link: {e}"))?;
    for item in &items {
        if item.state == OpRunState::Completed {
            // Carry completed receipts — idempotent, never re-executed.
            let now = now_ms();
            tx.execute(
                "INSERT INTO op_run_items (id, run_id, item_ref, state, item_digest, \
                 receipt_ref, error_class, error_detail, retry_count, created_at_unix_ms, \
                 updated_at_unix_ms, finished_at_unix_ms) \
                 VALUES (?1,?2,?3,'completed',?4,?5,'','',?6,?7,?7,?7)",
                params![
                    new_id("opi"),
                    child.id,
                    item.item_ref,
                    item.item_digest,
                    item.receipt_ref,
                    item.retry_count,
                    now
                ],
            )
            .map_err(|e| format!("op_run retry carry: {e}"))?;
        } else {
            // Re-queue everything else (failed/cancelled/interrupted/queued/running).
            let now = now_ms();
            tx.execute(
                "INSERT INTO op_run_items (id, run_id, item_ref, state, item_digest, \
                 receipt_ref, error_class, error_detail, retry_count, created_at_unix_ms, \
                 updated_at_unix_ms, finished_at_unix_ms) \
                 VALUES (?1,?2,?3,'queued',?4,'','','',?5,?6,?6,NULL)",
                params![
                    new_id("opi"),
                    child.id,
                    item.item_ref,
                    item.item_digest,
                    item.retry_count + 1,
                    now
                ],
            )
            .map_err(|e| format!("op_run retry requeue: {e}"))?;
        }
    }
    let done = items
        .iter()
        .filter(|i| i.state == OpRunState::Completed)
        .count() as i64;
    let failed = items
        .iter()
        .filter(|i| i.state != OpRunState::Completed)
        .count() as i64;
    let total = items.len() as i64;
    tx.execute(
        "UPDATE op_runs SET items_done = ?1, items_failed = ?2, items_total = ?3, \
         items_unattempted = ?4 WHERE id = ?5",
        params![done, failed, total, failed, child.id],
    )
    .map_err(|e| format!("op_run retry counters: {e}"))?;
    tx.commit()
        .map_err(|e| format!("op_run retry commit: {e}"))?;
    // Re-fetch the child: `begin` returned a stale struct (retry_count /
    // parent_run_id are set by the UPDATE after it).
    get_run(conn, &child.id)?.ok_or_else(|| format!("op_run retry: child {} vanished", child.id))
}

/// Restart recovery (runs on `Database::open`): every run still
/// `queued|running` — and its in-flight items — becomes `interrupted`.
/// Mark-only; resume happens only through an explicit `retry`.
/// Returns the interrupted run count.
pub fn recover(conn: &Connection) -> Result<i64, String> {
    let now = now_ms();
    let runs: Vec<String> = conn
        .prepare(
            "SELECT id FROM op_runs WHERE state IN ('queued','running') \
             ORDER BY created_at_unix_ms",
        )
        .map_err(|e| format!("op_run recover prepare: {e}"))?
        .query_map([], |r| r.get::<_, String>(0))
        .map_err(|e| format!("op_run recover query: {e}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("op_run recover rows: {e}"))?;
    if runs.is_empty() {
        return Ok(0);
    }
    conn.execute(
        "UPDATE op_runs SET state = 'interrupted', updated_at_unix_ms = ?1 \
         WHERE state IN ('queued','running')",
        params![now],
    )
    .map_err(|e| format!("op_run recover runs: {e}"))?;
    conn.execute(
        "UPDATE op_run_items SET state = 'interrupted', updated_at_unix_ms = ?1 \
         WHERE state IN ('queued','running') AND run_id IN (SELECT id FROM op_runs \
         WHERE state = 'interrupted')",
        params![now],
    )
    .map_err(|e| format!("op_run recover items: {e}"))?;
    Ok(runs.len() as i64)
}

/// Retention prune: delete TERMINAL runs whose `updated_at` is older than
/// `retention_days`, plus their items. In-flight runs are never pruned.
/// Returns the number of runs deleted.
pub fn prune(conn: &Connection, retention_days: i64) -> Result<i64, String> {
    let retention_days = retention_days.max(1);
    let cutoff = now_ms() - retention_days * 86_400_000;
    let ids: Vec<String> = conn
        .prepare(
            "SELECT id FROM op_runs WHERE state IN ('completed','failed','cancelled', \
             'interrupted','failed_to_start') AND updated_at_unix_ms < ?1",
        )
        .map_err(|e| format!("op_run prune prepare: {e}"))?
        .query_map(params![cutoff], |r| r.get::<_, String>(0))
        .map_err(|e| format!("op_run prune query: {e}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("op_run prune rows: {e}"))?;
    if ids.is_empty() {
        return Ok(0);
    }
    let tx = conn
        .unchecked_transaction()
        .map_err(|e| format!("op_run prune tx: {e}"))?;
    for id in &ids {
        tx.execute("DELETE FROM op_run_items WHERE run_id = ?1", params![id])
            .map_err(|e| format!("op_run prune items: {e}"))?;
    }
    let placeholders = vec!["?"; ids.len()].join(",");
    let sql = format!("DELETE FROM op_runs WHERE id IN ({placeholders})");
    tx.execute(&sql, rusqlite::params_from_iter(ids.iter()))
        .map_err(|e| format!("op_run prune runs: {e}"))?;
    tx.commit()
        .map_err(|e| format!("op_run prune commit: {e}"))?;
    Ok(ids.len() as i64)
}

/// List runs, optionally filtered by terminal state and/or op_type, bounded
/// by `limit` (1..=100), newest first.
pub fn list(
    conn: &Connection,
    state_filter: Option<OpRunState>,
    op_type_filter: Option<&str>,
    limit: i64,
) -> Result<Vec<OpRun>, String> {
    let limit = limit.clamp(1, 100);
    let mut sql = format!("SELECT {RUN_COLS} FROM op_runs");
    let mut conds: Vec<String> = Vec::new();
    let mut params: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    if let Some(s) = state_filter {
        conds.push("state = ?".to_string());
        params.push(Box::new(s.as_str().to_string()));
    }
    if let Some(t) = op_type_filter {
        conds.push("op_type = ?".to_string());
        params.push(Box::new(t.to_string()));
    }
    if !conds.is_empty() {
        sql.push_str(" WHERE ");
        sql.push_str(&conds.join(" AND "));
    }
    sql.push_str(" ORDER BY created_at_unix_ms DESC LIMIT ?");
    params.push(Box::new(limit));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| format!("op_run list prepare: {e}"))?;
    let rows = stmt
        .query_map(
            rusqlite::params_from_iter(params.iter().map(|p| p.as_ref())),
            row_to_run,
        )
        .map_err(|e| format!("op_run list query: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("op_run list rows: {e}"))
}

/// Items of a run, oldest first.
pub fn list_items(conn: &Connection, run_id: &str) -> Result<Vec<OpRunItem>, String> {
    let sql = format!(
        "SELECT {ITEM_COLS} FROM op_run_items WHERE run_id = ?1 ORDER BY created_at_unix_ms"
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| format!("op_run items prepare: {e}"))?;
    let rows = stmt
        .query_map(params![run_id], row_to_item)
        .map_err(|e| format!("op_run items query: {e}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("op_run items rows: {e}"))
}

/// Fetch one run with its items (`None` when unknown).
pub fn get_with_items(
    conn: &Connection,
    id: &str,
) -> Result<Option<(OpRun, Vec<OpRunItem>)>, String> {
    let Some(run) = get_run(conn, id)? else {
        return Ok(None);
    };
    let items = list_items(conn, id)?;
    Ok(Some((run, items)))
}

// ── Transaction helpers (used by Database wrappers / tests) ─────────────

// (begin/retry run on plain connections; retry manages its own transaction.)
