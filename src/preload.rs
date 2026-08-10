//! #875: learned anticipation — preload usage telemetry + operator-gated
//! trigger tuning.
//!
//! Records every preloaded item (recall_when result, context-block
//! injection) as a `preload_events` row, resolves "used" via the
//! touch-after-serve signal (the entity's `last_accessed_unix_ms` advanced
//! past the value captured at serving time), rolls per-session precision /
//! recall / miss-rate into `preload_sessions`, and proposes trigger
//! adjustments (`retire` for persistently low-precision triggers,
//! `add_trigger` for entities used-but-never-preloaded) into an operator
//! review queue. Mutations apply ONLY through `review_approve`, via the
//! audited `remember` path (journal + entity_history provenance) — never
//! silently (#863/#865). Metrics here are deliberately SEPARATE from #872
//! serving-concentration telemetry (separate tables, separate counters).

use std::collections::{BTreeMap, HashMap, HashSet};

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::db::{is_stopword, Database};
use crate::models::{Entity, JournalEvent};
use crate::retrieval_telemetry::hash_query;

/// Sentinel trigger refs for context-block injections that are not
/// recall_when matches.
pub const TRIGGER_ALWAYS_ON: &str = "__always_on__";
pub const TRIGGER_KEYWORD: &str = "__keyword__";
pub const TRIGGER_CONTEXT_BLOCK: &str = "__context_block__";

/// Minimum served count before a trigger is eligible for a retire proposal.
pub const MIN_SERVED_DEFAULT: i64 = 3;
/// Precision below which a served trigger is proposed for retirement.
pub const RETIRE_PRECISION: f64 = 0.25;
/// Distinct sessions in which a never-preloaded entity was used before an
/// add_trigger proposal is raised.
pub const ADD_TRIGGER_MIN_SESSIONS: i64 = 2;
/// Cap on missed entities tracked per session.
pub const MISSED_CAP: i64 = 200;
/// Default usage window: an event is resolved once it is older than this.
pub const RESOLVE_WINDOW_MINUTES_DEFAULT: i64 = 30;
/// Cap on the raw context text stored per event (bounded storage).
pub const CONTEXT_CAP_CHARS: usize = 500;
/// Cap on meaning-bearing words retained per session for proposals.
pub const SESSION_WORDS_CAP: usize = 50;

fn new_id(prefix: &str) -> String {
    format!(
        "{prefix}-{}",
        &uuid::Uuid::new_v4().to_string().replace('-', "")[..16]
    )
}

/// Meaning-bearing words of a context (stopword-filtered, >= 3 chars) —
/// the same filter `recall_when` uses, so attribution stays consistent.
pub fn words_of(context: &str) -> Vec<String> {
    context
        .split_whitespace()
        .filter(|w| w.len() >= 3 && !is_stopword(&w.to_lowercase()))
        .map(|w| w.to_lowercase())
        .collect()
}

/// First recall_when trigger (lowercased) containing any of `lc_words`.
pub fn matched_trigger(body_json: &str, lc_words: &[String]) -> Option<String> {
    let parsed: Value = serde_json::from_str(body_json).ok()?;
    let triggers = parsed.get("recall_when")?.as_array()?;
    for t in triggers {
        let s = t.as_str()?;
        let s_lc = s.to_lowercase();
        if lc_words.iter().any(|w| s_lc.contains(w.as_str())) {
            return Some(s_lc);
        }
    }
    None
}

/// Record one served preload event. `la_before` is the entity's
/// `last_accessed_unix_ms` captured AFTER serving (serving never bumps it:
/// recall_when uses a direct SELECT and context-block recall arms pass
/// `skip_side_effects`), so any later read touch counts as usage.
pub fn record_event(
    conn: &Connection,
    entity_id: &str,
    trigger_ref: &str,
    context: &str,
    workspace_hash: &str,
    session_id: &str,
    la_before: i64,
    ts: i64,
) -> Result<(), String> {
    let context_capped: String = context.chars().take(CONTEXT_CAP_CHARS).collect();
    conn.execute(
        "INSERT INTO preload_events
         (id, ts, context_hash, context, entity_id, trigger_ref, workspace_hash,
          session_id, la_before, used, resolved_ts)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, NULL, NULL)",
        params![
            new_id("pl"),
            ts,
            hash_query(context),
            context_capped,
            entity_id,
            trigger_ref,
            workspace_hash,
            session_id,
            la_before
        ],
    )
    .map_err(|e| format!("preload record: {e}"))?;
    Ok(())
}

#[derive(Clone)]
struct EventRow {
    id: String,
    context_hash: String,
    context: String,
    entity_id: String,
    session_id: String,
    la_before: i64,
    ts: i64,
}

pub struct ResolutionSummary {
    pub events_resolved: i64,
    pub sessions_written: i64,
}

/// Resolve preload events older than the usage window and roll them into
/// per-session rows. Deterministic: `now_ms` is explicit so tests and the
/// harness can drive time. Idempotent: events resolve once; re-running on
/// the same window recomputes the same session rows (INSERT OR REPLACE).
pub fn resolve(
    db: &Database,
    window_minutes: i64,
    now_ms: i64,
) -> Result<ResolutionSummary, String> {
    // Test/ops escape hatch: an explicit millisecond window overrides the
    // minute floor so harnesses can resolve fresh events without waiting.
    // Never set in production deployments.
    let window_ms = match std::env::var("PERSEUS_VAULT_PRELOAD_WINDOW_MS") {
        Ok(v) => v
            .parse::<i64>()
            .map_err(|e| format!("invalid PERSEUS_VAULT_PRELOAD_WINDOW_MS: {e}"))?,
        Err(_) => window_minutes.max(1) * 60_000,
    };
    let cutoff = now_ms - window_ms;
    let conn = db.conn().map_err(|e| e.to_string())?;

    let mut stmt = conn
        .prepare(
            "SELECT id, context_hash, context, entity_id, session_id, la_before, ts
             FROM preload_events WHERE used IS NULL AND ts <= ?1",
        )
        .map_err(|e| e.to_string())?;
    let rows: Vec<EventRow> = stmt
        .query_map(params![cutoff], |r| {
            Ok(EventRow {
                id: r.get(0)?,
                context_hash: r.get(1)?,
                context: r.get(2)?,
                entity_id: r.get(3)?,
                session_id: r.get(4)?,
                la_before: r.get(5)?,
                ts: r.get(6)?,
            })
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    drop(stmt);

    // Group by session: explicit session_id (set by callers through the MCP
    // tools), else a pseudo-session per context_hash (`ctx-<hash8>`). Both
    // are stable keys for the preload_sessions PK.
    let mut by_session: BTreeMap<String, Vec<EventRow>> = BTreeMap::new();
    for ev in rows {
        let key = if ev.session_id.is_empty() {
            format!("ctx-{}", &ev.context_hash[..ev.context_hash.len().min(8)])
        } else {
            ev.session_id.clone()
        };
        by_session.entry(key).or_default().push(ev);
    }
    let mut events_resolved = 0i64;
    let mut sessions_written = 0i64;
    for (skey, evs) in by_session.iter_mut() {
        // Resolve each event: used = entity touched after serving and before
        // this resolution sweep. The sweep is the session boundary — touches
        // after resolution can never credit (events resolve exactly once).
        let mut used_ids: HashSet<String> = HashSet::new();
        let mut preloaded: Vec<String> = Vec::new();
        let mut anchor = now_ms;
        for ev in evs.iter() {
            anchor = anchor.min(ev.ts);
            if !preloaded.contains(&ev.entity_id) {
                preloaded.push(ev.entity_id.clone());
            }
            let la_now: Option<i64> = conn
                .query_row(
                    "SELECT last_accessed_unix_ms FROM entities WHERE id = ?1",
                    params![ev.entity_id],
                    |r| r.get(0),
                )
                .optional()
                .map_err(|e| e.to_string())?
                .flatten();
            let used = la_now
                .map(|v| v > ev.la_before && v <= now_ms)
                .unwrap_or(false);
            conn.execute(
                "UPDATE preload_events SET used = ?2, resolved_ts = ?3 WHERE id = ?1",
                params![ev.id, used as i64, now_ms],
            )
            .map_err(|e| e.to_string())?;
            if used {
                used_ids.insert(ev.entity_id.clone());
            }
            events_resolved += 1;
        }
        let used_n = used_ids.len() as i64;
        let preloaded_n = preloaded.len() as i64;

        // Words of the session contexts (union, capped).
        let mut words: Vec<String> = Vec::new();
        for ev in evs.iter() {
            for w in words_of(&ev.context) {
                if !words.contains(&w) {
                    words.push(w);
                    if words.len() >= SESSION_WORDS_CAP {
                        break;
                    }
                }
            }
            if words.len() >= SESSION_WORDS_CAP {
                break;
            }
        }

        // Missed: entities read since the session began (anchor..now) that
        // were NOT preloaded. The sweep time bounds the usage period.
        let ctx_hash = &evs[0].context_hash;
        let missed: Vec<String> = {
            let (sql, key_param) = if skey.starts_with("ctx-") {
                (
                    "SELECT id FROM entities
                     WHERE archived = 0 AND last_accessed_unix_ms > ?1
                       AND last_accessed_unix_ms <= ?2
                       AND id NOT IN (SELECT entity_id FROM preload_events
                                      WHERE session_id = '' AND context_hash = ?3)
                     LIMIT ?4",
                    ctx_hash.as_str(),
                )
            } else {
                (
                    "SELECT id FROM entities
                     WHERE archived = 0 AND last_accessed_unix_ms > ?1
                       AND last_accessed_unix_ms <= ?2
                       AND id NOT IN (SELECT entity_id FROM preload_events
                                      WHERE session_id = ?3)
                     LIMIT ?4",
                    skey.as_str(),
                )
            };
            let mut st = conn.prepare(sql).map_err(|e| e.to_string())?;
            let rows: Vec<String> = st
                .query_map(params![anchor, now_ms, key_param, MISSED_CAP], |r| r.get(0))
                .map_err(|e| e.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?;
            drop(st);
            rows
        };
        let missed_n = missed.len() as i64;

        // Missed-by-trigger attribution: a missed entity whose own
        // recall_when matches the session context should have been served.
        let mut missed_by_trigger: HashMap<String, i64> = HashMap::new();
        let enc = db.encryption.as_ref();
        for mid in &missed {
            let Some(ent) = load_entity_by_id(&conn, mid, enc)? else {
                continue;
            };
            if let Some(trig) = matched_trigger(&ent.body_json, &words) {
                *missed_by_trigger.entry(trig).or_insert(0) += 1;
            }
        }

        let used_f = used_n as f64;
        let missed_f = missed_n as f64;
        let denom = used_f + missed_f;
        let precision = if preloaded_n > 0 {
            used_f / preloaded_n as f64
        } else {
            0.0
        };
        let recall = if denom > 0.0 { used_f / denom } else { 0.0 };
        let miss_rate = if denom > 0.0 { missed_f / denom } else { 0.0 };

        conn.execute(
            "INSERT OR REPLACE INTO preload_sessions
             (session_id, anchor_ts, preloaded_n, used_n, missed_n, precision,
              recall, miss_rate, context_words, missed_by_trigger_json,
              missed_ids_json, resolved_ts)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![
                skey,
                anchor,
                preloaded_n,
                used_n,
                missed_n,
                precision,
                recall,
                miss_rate,
                serde_json::to_string(&words).unwrap_or_else(|_| "[]".to_string()),
                serde_json::to_string(&missed_by_trigger).unwrap_or_else(|_| "{}".to_string()),
                serde_json::to_string(&missed).unwrap_or_else(|_| "[]".to_string()),
                now_ms
            ],
        )
        .map_err(|e| e.to_string())?;
        sessions_written += 1;
    }
    Ok(ResolutionSummary {
        events_resolved,
        sessions_written,
    })
}

/// Read-only aggregates. `scope`: "trigger" (per-trigger precision/recall,
/// with missed attribution summed from sessions), "session" (recent session
/// rows), or "overall". Separate from #872 serving-concentration metrics.
pub fn stats(db: &Database, scope: &str, limit: i64, since_days: i64) -> Result<Value, String> {
    let conn = db.conn().map_err(|e| e.to_string())?;
    let since_ms = now_ms() - since_days.max(0) * 86_400_000;
    let safe_limit = limit.clamp(1, 1000);
    match scope {
        "trigger" => {
            let mut stmt = conn
                .prepare(
                    "SELECT trigger_ref, entity_id, COUNT(*) AS served,
                            SUM(COALESCE(used, 0)) AS used, MAX(ts) AS last_served
                     FROM preload_events
                     WHERE used IS NOT NULL AND ts >= ?1
                     GROUP BY trigger_ref, entity_id
                     ORDER BY served DESC, trigger_ref ASC
                     LIMIT ?2",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![since_ms, safe_limit], |r| {
                    Ok((
                        r.get::<_, String>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, i64>(2)?,
                        r.get::<_, i64>(3)?,
                        r.get::<_, i64>(4)?,
                    ))
                })
                .map_err(|e| e.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?;
            // Missed attribution per trigger, summed across sessions.
            let mut missed_map: HashMap<String, i64> = HashMap::new();
            let mut s2 = conn
                .prepare(
                    "SELECT missed_by_trigger_json FROM preload_sessions WHERE resolved_ts >= ?1",
                )
                .map_err(|e| e.to_string())?;
            let json_rows: Vec<String> = s2
                .query_map(params![since_ms], |r| r.get(0))
                .map_err(|e| e.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?;
            for j in json_rows {
                if let Ok(Value::Object(map)) = serde_json::from_str(&j) {
                    for (k, v) in map {
                        if let Some(n) = v.as_i64() {
                            *missed_map.entry(k).or_insert(0) += n;
                        }
                    }
                }
            }
            let triggers: Vec<Value> = rows
                .into_iter()
                .map(|(trig, eid, served, used, last)| {
                    let precision = if served > 0 {
                        used as f64 / served as f64
                    } else {
                        0.0
                    };
                    let missed = missed_map.get(&trig).copied().unwrap_or(0);
                    let denom = used as f64 + missed as f64;
                    let recall = if denom > 0.0 {
                        used as f64 / denom
                    } else {
                        0.0
                    };
                    json!({
                        "trigger_ref": trig,
                        "entity_id": eid,
                        "served": served,
                        "used": used,
                        "precision": (precision * 1000.0).round() / 1000.0,
                        "missed_by_trigger": missed,
                        "recall": (recall * 1000.0).round() / 1000.0,
                        "last_served_unix_ms": last,
                    })
                })
                .collect();
            Ok(json!({ "scope": "trigger", "triggers": triggers }))
        }
        "session" => {
            let mut stmt = conn
                .prepare(
                    "SELECT session_id, anchor_ts, preloaded_n, used_n, missed_n,
                            precision, recall, miss_rate, resolved_ts
                     FROM preload_sessions
                     WHERE resolved_ts >= ?1
                     ORDER BY resolved_ts DESC
                     LIMIT ?2",
                )
                .map_err(|e| e.to_string())?;
            let rows = stmt
                .query_map(params![since_ms, safe_limit], |r| {
                    Ok(json!({
                        "session_id": r.get::<_, String>(0)?,
                        "anchor_ts": r.get::<_, i64>(1)?,
                        "preloaded": r.get::<_, i64>(2)?,
                        "used": r.get::<_, i64>(3)?,
                        "missed": r.get::<_, i64>(4)?,
                        "precision": r.get::<_, f64>(5)?,
                        "recall": r.get::<_, f64>(6)?,
                        "miss_rate": r.get::<_, f64>(7)?,
                        "resolved_ts": r.get::<_, i64>(8)?,
                    }))
                })
                .map_err(|e| e.to_string())?
                .collect::<Result<Vec<_>, _>>()
                .map_err(|e| e.to_string())?;
            Ok(json!({ "scope": "session", "sessions": rows }))
        }
        _ => {
            let (resolved, used): (i64, i64) = conn
                .query_row(
                    "SELECT COUNT(*), COALESCE(SUM(COALESCE(used,0)),0) FROM preload_events
                     WHERE used IS NOT NULL AND ts >= ?1",
                    params![since_ms],
                    |r| Ok((r.get(0)?, r.get(1)?)),
                )
                .map_err(|e| e.to_string())?;
            let unresolved: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM preload_events WHERE used IS NULL",
                    [],
                    |r| r.get(0),
                )
                .map_err(|e| e.to_string())?;
            let (sessions, used_s, missed_s): (i64, i64, i64) = conn
                .query_row(
                    "SELECT COUNT(*), COALESCE(SUM(used_n),0), COALESCE(SUM(missed_n),0)
                     FROM preload_sessions WHERE resolved_ts >= ?1",
                    params![since_ms],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .map_err(|e| e.to_string())?;
            let denom = used_s + missed_s;
            Ok(json!({
                "scope": "overall",
                "events_resolved": resolved,
                "events_used": used,
                "events_unresolved": unresolved,
                "sessions": sessions,
                "overall_precision": if resolved > 0 { (used as f64 / resolved as f64 * 1000.0).round() / 1000.0 } else { 0.0 },
                "overall_miss_rate": if denom > 0 { (missed_s as f64 / denom as f64 * 1000.0).round() / 1000.0 } else { 0.0 },
            }))
        }
    }
}

/// The offline proposal pass. Reads resolved sessions/events and inserts
/// PENDING proposals (governed: journaled, applied only via review approve).
/// Returns the newly created proposals.
pub fn propose(db: &Database, now_ms: i64, by: &str) -> Result<Vec<Value>, String> {
    let conn = db.conn().map_err(|e| e.to_string())?;
    let min_served = std::env::var("PERSEUS_VAULT_PRELOAD_MIN_SERVED")
        .ok()
        .and_then(|v| v.parse::<i64>().ok())
        .unwrap_or(MIN_SERVED_DEFAULT)
        .max(1);
    let retire_precision = std::env::var("PERSEUS_VAULT_PRELOAD_RETIRE_PRECISION")
        .ok()
        .and_then(|v| v.parse::<f64>().ok())
        .unwrap_or(RETIRE_PRECISION);
    let mut created: Vec<Value> = Vec::new();

    // ── retire: served >= min_served with precision below the bound ──────
    // NOTE: GLOB not LIKE — `_` is a LIKE wildcard, so '__%' would match
    // every trigger (two+ chars), silently excluding them all.
    let mut stmt = conn
        .prepare(
            "SELECT trigger_ref, entity_id, COUNT(*) AS served, SUM(COALESCE(used,0)) AS used
             FROM preload_events
             WHERE used IS NOT NULL AND trigger_ref NOT GLOB '__*'
             GROUP BY trigger_ref, entity_id
             HAVING COUNT(*) >= ?1",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![min_served], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)?,
                r.get::<_, i64>(3)?,
            ))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    drop(stmt);
    for (trig, eid, served, used) in rows {
        let precision = used as f64 / served as f64;
        if precision >= retire_precision {
            continue;
        }
        let pending: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM preload_proposals
                 WHERE entity_id = ?1 AND suggestion = 'retire' AND state = 'pending'",
                params![eid],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if pending > 0 {
            continue;
        }
        let rationale = json!({
            "served": served, "used": used,
            "precision": (precision * 1000.0).round() / 1000.0,
            "bound": retire_precision,
        })
        .to_string();
        let pid = new_id("pp");
        conn.execute(
            "INSERT INTO preload_proposals
             (id, entity_id, trigger_ref, suggestion, rationale, state,
              created_ts, decided_ts, decided_by, applied_ts, journal_event_id)
             VALUES (?1, ?2, ?3, 'retire', ?4, 'pending', ?5, NULL, '', NULL, '')",
            params![pid, eid, trig, rationale, now_ms],
        )
        .map_err(|e| e.to_string())?;
        created.push(
            json!({ "id": pid, "entity_id": eid, "trigger_ref": trig, "suggestion": "retire" }),
        );
    }

    // ── add_trigger: never-preloaded entity used in >= 2 sessions ────────
    let mut s2 = conn
        .prepare(
            "SELECT session_id, context_words, missed_ids_json FROM preload_sessions
             WHERE missed_n > 0",
        )
        .map_err(|e| e.to_string())?;
    let sess_rows: Vec<(String, String, String)> = s2
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    drop(s2);
    // entity -> (distinct session count, session index list)
    let mut missed_sessions: HashMap<String, Vec<usize>> = HashMap::new();
    for (i, (_, _, ids_json)) in sess_rows.iter().enumerate() {
        if let Ok(Value::Array(ids)) = serde_json::from_str::<Value>(ids_json) {
            for v in ids {
                if let Some(id) = v.as_str() {
                    missed_sessions.entry(id.to_string()).or_default().push(i);
                }
            }
        }
    }
    let enc = db.encryption.as_ref();
    for (eid, sessions) in missed_sessions {
        if (sessions.len() as i64) < ADD_TRIGGER_MIN_SESSIONS {
            continue;
        }
        // Distinct sessions actually count the row; the Vec may hold dupes
        // if a session lists the entity once (it can't) — keep it simple.
        let Some(ent) = load_entity_by_id(&conn, &eid, enc)? else {
            continue;
        };
        let has_triggers = serde_json::from_str::<Value>(&ent.body_json)
            .ok()
            .map(|v| {
                v.get("recall_when")
                    .and_then(|t| t.as_array())
                    .map(|a| !a.is_empty())
                    .unwrap_or(false)
            })
            .unwrap_or(false);
        if has_triggers {
            continue;
        }
        // Tuning gate: an entity edited by a governed review approve must be
        // re-observed (a fresh preload serve after the tuning) before
        // add_trigger may suggest mutating it again — the tuning write itself
        // bumps last_accessed and must not count as usage.
        let tuned: i64 = conn
            .query_row(
                "SELECT preload_tuned_unix_ms FROM entities WHERE id = ?1",
                params![eid],
                |r| r.get(0),
            )
            .unwrap_or(0);
        if tuned > 0 {
            let last_serve: Option<i64> = conn
                .query_row(
                    "SELECT MAX(ts) FROM preload_events WHERE entity_id = ?1",
                    params![eid],
                    |r| r.get(0),
                )
                .ok();
            if last_serve.unwrap_or(0) <= tuned {
                continue;
            }
        }
        let pending: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM preload_proposals
                 WHERE entity_id = ?1 AND suggestion = 'add_trigger' AND state = 'pending'",
                params![eid],
                |r| r.get(0),
            )
            .map_err(|e| e.to_string())?;
        if pending > 0 {
            continue;
        }
        // Word: most frequent across the involved sessions' context words.
        let mut freq: HashMap<String, i64> = HashMap::new();
        for &si in &sessions {
            if let Ok(Value::Array(ws)) = serde_json::from_str::<Value>(&sess_rows[si].1) {
                for w in ws {
                    if let Some(w) = w.as_str() {
                        *freq.entry(w.to_string()).or_insert(0) += 1;
                    }
                }
            }
        }
        let word = freq
            .into_iter()
            .max_by(|a, b| a.1.cmp(&b.1).then_with(|| b.0.cmp(&a.0)))
            .map(|(w, _)| w)
            .unwrap_or_default();
        if word.is_empty() {
            continue;
        }
        let rationale = json!({
            "sessions_used": sessions.len(),
            "word": word,
            "sessions": sess_rows.iter().enumerate()
                .filter(|(i, _)| sessions.contains(i))
                .map(|(_, (sid, _, _))| sid.clone()).collect::<Vec<_>>(),
        })
        .to_string();
        let pid = new_id("pp");
        conn.execute(
            "INSERT INTO preload_proposals
             (id, entity_id, trigger_ref, suggestion, rationale, state,
              created_ts, decided_ts, decided_by, applied_ts, journal_event_id)
             VALUES (?1, ?2, ?3, 'add_trigger', ?4, 'pending', ?5, NULL, '', NULL, '')",
            params![pid, eid, word, rationale, now_ms],
        )
        .map_err(|e| e.to_string())?;
        created.push(json!({ "id": pid, "entity_id": eid, "trigger_ref": word, "suggestion": "add_trigger" }));
    }

    if !created.is_empty() {
        let _ = db.journal(&JournalEvent {
            id: new_id("je"),
            event_type: "preload_proposals_created".to_string(),
            evaluated_json: json!({ "count": created.len(), "by": by }).to_string(),
            acted_json: String::new(),
            forward_json: String::new(),
            category: "preload-tuning".to_string(),
            key: format!("proposals-{}", now_ms),
            entity_id: String::new(),
            agent_id: by.to_string(),
            workspace_hash: String::new(),
            created_at_unix_ms: now_ms,
        });
    }
    Ok(created)
}

/// Pending proposals with entity identity for operator review.
pub fn review_list(db: &Database, limit: i64) -> Result<Value, String> {
    let conn = db.conn().map_err(|e| e.to_string())?;
    let mut stmt = conn
        .prepare(
            "SELECT p.id, p.entity_id, e.category, e.key, p.trigger_ref,
                    p.suggestion, p.rationale, p.created_ts
             FROM preload_proposals p
             LEFT JOIN entities e ON e.id = p.entity_id
             WHERE p.state = 'pending'
             ORDER BY p.created_ts ASC
             LIMIT ?1",
        )
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![limit.clamp(1, 1000)], |r| {
            Ok(json!({
                "proposal_id": r.get::<_, String>(0)?,
                "entity_id": r.get::<_, String>(1)?,
                "category": r.get::<_, Option<String>>(2)?,
                "key": r.get::<_, Option<String>>(3)?,
                "trigger_ref": r.get::<_, String>(4)?,
                "suggestion": r.get::<_, String>(5)?,
                "rationale": serde_json::from_str::<Value>(&r.get::<_, String>(6)?)
                    .unwrap_or(Value::Null),
                "created_ts": r.get::<_, i64>(7)?,
            }))
        })
        .map_err(|e| e.to_string())?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| e.to_string())?;
    Ok(json!({ "pending": rows }))
}

fn load_entity_by_id(
    conn: &Connection,
    id: &str,
    enc: Option<&crate::encryption::EncryptionManager>,
) -> Result<Option<Entity>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, category, key, body_json, status, type, tags,
                    decay_score, retrieval_count, layer, topic_path,
                    archived, archive_reason, links, verified, source,
                    created_at_unix_ms, last_accessed_unix_ms, NULL as embedding,
                    always_on, certainty, workspace_hash, agent_id, visibility,
                    follow_count, miss_count, follow_rate, efficacy_status,
                    epistemic_state
             FROM entities WHERE id = ?1",
        )
        .map_err(|e| e.to_string())?;
    let mut rows = stmt
        .query_map(params![id], |row| crate::db::entity_from_row(row, enc))
        .map_err(|e| e.to_string())?;
    let out = rows.next().transpose().map_err(|e| e.to_string())?;
    Ok(out)
}

/// Apply the body mutation for an approved proposal. `retire`: remove the
/// trigger string; `add_trigger`: append the word (case-insensitive dedup).
/// Fail-closed: any drift from the proposal's premise is an error, never a
/// silent rewrite.
fn apply_body_mutation(
    entity: &mut Entity,
    suggestion: &str,
    trigger_ref: &str,
) -> Result<(), String> {
    let mut body: Value = serde_json::from_str(&entity.body_json)
        .map_err(|e| format!("preload apply: unparseable body: {e}"))?;
    match suggestion {
        "retire" => {
            let arr = body
                .get_mut("recall_when")
                .and_then(|v| v.as_array_mut())
                .ok_or_else(|| "preload apply: entity has no recall_when".to_string())?;
            let before = arr.len();
            arr.retain(|t| {
                t.as_str()
                    .map(|s| s.to_lowercase() != trigger_ref)
                    .unwrap_or(true)
            });
            if arr.len() == before {
                return Err(format!(
                    "preload apply: trigger {trigger_ref:?} not present in recall_when"
                ));
            }
        }
        "add_trigger" => {
            // The whole point of add_trigger: the entity HAS no trigger yet.
            if body.get("recall_when").is_none() {
                body["recall_when"] = Value::Array(Vec::new());
            }
            let arr = body
                .get_mut("recall_when")
                .and_then(|v| v.as_array_mut())
                .ok_or_else(|| "preload apply: entity has no recall_when".to_string())?;
            if arr.iter().any(|t| {
                t.as_str()
                    .map(|s| s.eq_ignore_ascii_case(trigger_ref))
                    .unwrap_or(false)
            }) {
                return Err(format!(
                    "preload apply: trigger {trigger_ref:?} already present"
                ));
            }
            arr.push(Value::String(trigger_ref.to_string()));
        }
        other => return Err(format!("preload apply: unknown suggestion {other:?}")),
    }
    entity.body_json =
        serde_json::to_string(&body).map_err(|e| format!("preload apply: serialize: {e}"))?;
    Ok(())
}

/// Approve a pending proposal: applies the mutation through the audited
/// `remember` path (journal + entity_history provenance), then marks the
/// proposal applied. The ONLY mutation path for trigger tuning.
pub fn review_approve(
    db: &Database,
    proposal_id: &str,
    by: &str,
    now_ms: i64,
) -> Result<Value, String> {
    let conn = db.conn().map_err(|e| e.to_string())?;
    let (entity_id, trigger_ref, suggestion): (String, String, String) = conn
        .query_row(
            "SELECT entity_id, trigger_ref, suggestion FROM preload_proposals
             WHERE id = ?1",
            params![proposal_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| format!("unknown proposal: {proposal_id}"))?;
    let state: String = conn
        .query_row(
            "SELECT state FROM preload_proposals WHERE id = ?1",
            params![proposal_id],
            |r| r.get(0),
        )
        .map_err(|e| e.to_string())?;
    if state != "pending" {
        return Err(format!(
            "preload review: proposal {proposal_id} is {state}, not pending"
        ));
    }
    drop(conn);

    let conn = db.conn().map_err(|e| e.to_string())?;
    let enc = db.encryption.as_ref();
    let mut entity = load_entity_by_id(&conn, &entity_id, enc)?
        .ok_or_else(|| format!("preload review: entity {entity_id} missing or archived"))?;
    drop(conn);

    apply_body_mutation(&mut entity, &suggestion, &trigger_ref)?;
    let workspace_hash = entity.workspace_hash.clone();

    let event = JournalEvent {
        id: new_id("je"),
        event_type: "preload_tuning_applied".to_string(),
        evaluated_json: json!({
            "proposal": proposal_id, "suggestion": suggestion,
            "trigger_ref": trigger_ref, "by": by,
        })
        .to_string(),
        acted_json: String::new(),
        forward_json: String::new(),
        category: "preload-tuning".to_string(),
        key: proposal_id.to_string(),
        entity_id: entity_id.clone(),
        agent_id: by.to_string(),
        workspace_hash,
        created_at_unix_ms: now_ms,
    };
    db.journal(&event)
        .map_err(|e| format!("preload review journal: {e}"))?;

    let _ = db
        .remember(&entity)
        .map_err(|e| format!("preload review remember: {e}"))?;

    let conn = db.conn().map_err(|e| e.to_string())?;
    // Mark the entity as tuned: a governed tuning write rewrites the body
    // (and bumps last_accessed), so pre-tuning usage signal must not be read
    // as fresh usage by add_trigger until the entity is re-observed.
    conn.execute(
        "UPDATE entities SET preload_tuned_unix_ms = ?2 WHERE id = ?1",
        params![entity_id, now_ms],
    )
    .map_err(|e| e.to_string())?;
    conn.execute(
        "UPDATE preload_proposals
         SET state = 'applied', decided_ts = ?2, decided_by = ?3,
             applied_ts = ?2, journal_event_id = ?4
         WHERE id = ?1",
        params![proposal_id, now_ms, by, event.id],
    )
    .map_err(|e| e.to_string())?;
    Ok(json!({
        "proposal_id": proposal_id,
        "entity_id": entity_id,
        "suggestion": suggestion,
        "trigger_ref": trigger_ref,
        "state": "applied",
        "journal_event_id": event.id,
    }))
}

/// Dismiss a pending proposal (journaled; no entity mutation).
pub fn review_dismiss(
    db: &Database,
    proposal_id: &str,
    reason: &str,
    by: &str,
    now_ms: i64,
) -> Result<Value, String> {
    let conn = db.conn().map_err(|e| e.to_string())?;
    let (entity_id, trigger_ref, suggestion, state): (String, String, String, String) = conn
        .query_row(
            "SELECT entity_id, trigger_ref, suggestion, state FROM preload_proposals
             WHERE id = ?1",
            params![proposal_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| format!("unknown proposal: {proposal_id}"))?;
    if state != "pending" {
        return Err(format!(
            "preload review: proposal {proposal_id} is {state}, not pending"
        ));
    }
    let event = JournalEvent {
        id: new_id("je"),
        event_type: "preload_tuning_dismissed".to_string(),
        evaluated_json: json!({
            "proposal": proposal_id, "suggestion": suggestion,
            "trigger_ref": trigger_ref, "reason": reason, "by": by,
        })
        .to_string(),
        acted_json: String::new(),
        forward_json: String::new(),
        category: "preload-tuning".to_string(),
        key: proposal_id.to_string(),
        entity_id,
        agent_id: by.to_string(),
        workspace_hash: String::new(),
        created_at_unix_ms: now_ms,
    };
    db.journal(&event)
        .map_err(|e| format!("preload review dismiss journal: {e}"))?;
    conn.execute(
        "UPDATE preload_proposals
         SET state = 'dismissed', decided_ts = ?2, decided_by = ?3, journal_event_id = ?4
         WHERE id = ?1",
        params![proposal_id, now_ms, by, event.id],
    )
    .map_err(|e| e.to_string())?;
    Ok(json!({
        "proposal_id": proposal_id,
        "suggestion": suggestion,
        "state": "dismissed",
    }))
}

fn now_ms() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0)
}
