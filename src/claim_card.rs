//! claim_card.rs — evidence-backed claim cards (#852).
//!
//! Spec: docs/specs/claim-cards.md. A claim card is a deterministic,
//! versioned projection over the EXISTING entity/evidence graph — never a
//! second source of truth. It shows the claim, its provenance class
//! (derived per docs/specs/provenance-classes-derived-facts.md), valid-time
//! vs recorded-time, confidence/support, supersession/contradiction/stale
//! state, evidence references (metadata only — raw bodies stay out), a
//! sanitized agent projection hash-bound to the selected evidence and
//! policy, and machine-readable reason codes explaining why the projection
//! is serveable, constrained, or withheld.
//!
//! Determinism contract: the card digest is sha256 over a canonical
//! (recursively key-sorted) serialization of a fixed subset; evidence refs
//! are sorted by entity_id; link order and JSON key order never change the
//! digest. Tests pin this with shuffled-link fixtures.

use crate::db::Database;
use crate::models::Entity;
use serde::Serialize;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

/// Bump whenever the card schema changes; the digest input set MUST be
/// frozen per version (a schema change bumps the version, not the digest).
pub const CLAIM_CARD_VERSION: i64 = 1;

/// Link relationships counted as independent support for a claim.
const EVIDENCE_RELS: [&str; 3] = ["evidence_for", "derived_from", "promoted_to"];

/// Confidence multiplier applied to unverified entities (verified = 1.0).
const UNVERIFIED_CONFIDENCE_FACTOR: f64 = 0.8;

/// A belief whose last touch is older than this is "revalidation required".
const REVALIDATION_WINDOW_MS: i64 = 90 * 24 * 3600 * 1000;

/// Decay below this marks a claim/evidence stale.
const STALE_DECAY_THRESHOLD: f64 = 0.5;

#[derive(Debug, Clone, Serialize)]
pub struct Times {
    pub valid_from_unix_ms: Option<i64>,
    pub valid_to_unix_ms: Option<i64>,
    pub recorded_at_unix_ms: Option<i64>,
    pub invalidated_at_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CardState {
    pub superseded: bool,
    pub superseded_by: Option<String>,
    pub supersedes: Option<String>,
    pub stale: bool,
    pub quarantined: bool,
    pub contradicted: bool,
    pub revalidation_required: bool,
    pub archived: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct EvidenceRef {
    pub entity_id: String,
    pub claim: String,
    pub relationship: String,
    pub provenance_class: Option<String>,
    pub confidence: f64,
    pub stale: bool,
    pub superseded: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct LifecycleOps {
    /// Map the card's state to the governed mutation surface that creates
    /// history (never in-place edits).
    pub confirm: String,
    pub correct: String,
    pub exclude_or_revoke: String,
    pub revalidate: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct AgentProjection {
    /// Exact text that may cross an MCP/context boundary; empty when
    /// withheld (see `excluded`).
    pub text: String,
    /// sha256(text + "|" + card digest) — binds the projection to the exact
    /// evidence set and policy the card was computed under.
    pub digest: String,
    /// Reasons the projection is constrained/withheld; empty when serveable.
    pub excluded: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ClaimCard {
    pub claim_card_version: i64,
    pub entity_id: String,
    pub claim: String,
    pub category: String,
    pub key: String,
    pub entity_type: String,
    /// Derived per provenance-classes-derived-facts.md §1; null when
    /// undeterminable (never guessed).
    pub provenance_class: Option<String>,
    pub times: Times,
    pub confidence: f64,
    pub certainty: f64,
    pub verified: bool,
    pub epistemic_state: String,
    pub support_count: i64,
    pub state: CardState,
    /// Machine-readable: "serveable" or a withhold reason, plus flags
    /// ("stale", "stale_evidence", "missing_provenance", "contradicted",
    /// "superseded", "revalidation_required").
    pub reason_codes: Vec<String>,
    pub evidence: Vec<EvidenceRef>,
    pub lifecycle: LifecycleOps,
    /// sha256 over the canonical (key-sorted) serialization of the fixed
    /// digest subset — deterministic across link order and JSON key order.
    pub digest: String,
    pub agent_projection: AgentProjection,
}

#[derive(Debug, Clone, serde::Deserialize)]
pub struct ClaimCardArgs {
    pub entity_id: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// Caller identity for visibility enforcement (#684 semantics).
    #[serde(default)]
    pub agent_id: Option<String>,
    #[serde(default = "default_true")]
    pub include_evidence: bool,
    #[serde(default = "default_true")]
    pub include_agent_projection: bool,
}

fn default_true() -> bool {
    true
}

/// One live row plus the bi-temporal/supersession columns that do not live
/// on `Entity` (they are read through dedicated queries elsewhere).
struct CardRow {
    entity: Entity,
    valid_from_unix_ms: Option<i64>,
    valid_to_unix_ms: Option<i64>,
    recorded_at_unix_ms: Option<i64>,
    invalidated_at_unix_ms: Option<i64>,
    supersedes: String,
    superseded_by: String,
}

/// Canonical serialization: recursively sort object keys so the digest is
/// key-order-independent (serde struct order and client JSON order can
/// differ without changing the hash).
fn canonical_json(v: &Value) -> String {
    fn sort(v: &Value) -> Value {
        match v {
            Value::Object(map) => {
                let mut sorted: Vec<(String, Value)> =
                    map.iter().map(|(k, v)| (k.clone(), sort(v))).collect();
                sorted.sort_by(|a, b| a.0.cmp(&b.0));
                Value::Object(sorted.into_iter().collect())
            }
            Value::Array(items) => Value::Array(items.iter().map(sort).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_string(&sort(v)).unwrap_or_default()
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("{:x}", h.finalize())
}

/// Provenance class roll-up per provenance-classes-derived-facts.md §1:
/// derived deterministically from `origin.memory_kind` + the presence of
/// evidence links. None when undeterminable — never guessed.
pub fn provenance_class(
    memory_kind: Option<&str>,
    has_evidence_links: bool,
) -> Option<&'static str> {
    match memory_kind {
        Some("asserted") | Some("imported") => Some("source_human"),
        Some("extracted") | Some("observed") => Some("fact_extracted"),
        Some("inferred") => {
            if has_evidence_links {
                Some("fact_derived")
            } else {
                Some("inference_agent")
            }
        }
        _ => None,
    }
}

fn memory_kind_of(body: &Value) -> Option<String> {
    body.get("origin")
        .and_then(|o| o.get("memory_kind"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string())
}

fn extract_claim(body: &Value, key: &str) -> String {
    for field in ["summary", "content", "claim"] {
        if let Some(s) = body.get(field).and_then(|v| v.as_str()) {
            if !s.trim().is_empty() {
                let trimmed = s.trim();
                return if trimmed.chars().count() > 240 {
                    trimmed.chars().take(240).collect::<String>() + "…"
                } else {
                    trimmed.to_string()
                };
            }
        }
    }
    key.to_string()
}

fn confidence_of(certainty: f64, verified: bool) -> f64 {
    let c = certainty
        * if verified {
            1.0
        } else {
            UNVERIFIED_CONFIDENCE_FACTOR
        };
    (c * 100.0).round() / 100.0
}

fn stale_of_row(row: &CardRow, now: i64) -> bool {
    row.entity.decay_score < STALE_DECAY_THRESHOLD
        || row.valid_to_unix_ms.is_some_and(|t| t < now)
        || row.invalidated_at_unix_ms.is_some_and(|t| t < now)
}

fn scan_live_rows(db: &Database) -> Result<Vec<CardRow>, String> {
    let conn = db.conn().map_err(|e| format!("claim card: db: {e}"))?;
    let mut stmt = conn
        .prepare(
            "SELECT id, category, key, body_json, status, type, tags, decay_score, \
                    verified, certainty, workspace_hash, agent_id, visibility, \
                    created_at_unix_ms, last_accessed_unix_ms, archived, links, \
                    valid_from_unix_ms, valid_to_unix_ms, recorded_at_unix_ms, \
                    invalidated_at_unix_ms, supersedes, superseded_by \
             FROM entities WHERE archived = 0 AND status IN ('active','draft')",
        )
        .map_err(|e| format!("claim card: prepare failed: {e}"))?;
    let rows: Vec<CardRow> = stmt
        .query_map([], |r| {
            let links_str: String = r.get::<_, String>(16).unwrap_or_else(|_| "[]".to_string());
            let links: Vec<crate::models::MemoryLink> =
                serde_json::from_str(&links_str).unwrap_or_default();
            let tags: Vec<String> =
                serde_json::from_str(&r.get::<_, String>(6).unwrap_or_else(|_| "[]".to_string()))
                    .unwrap_or_default();
            let body_json: String = r.get(3)?;
            let e = Entity {
                id: r.get(0)?,
                category: r.get(1)?,
                key: r.get(2)?,
                body_json: body_json.clone(),
                status: r.get(4)?,
                entity_type: r.get(5)?,
                tags,
                decay_score: r.get(7)?,
                retrieval_count: 0,
                layer: String::new(),
                topic_path: String::new(),
                archived: r.get(15)?,
                archive_reason: String::new(),
                links: links.clone(),
                verified: r.get(8)?,
                source: String::new(),
                always_on: false,
                certainty: r.get(9)?,
                workspace_hash: r.get(10)?,
                agent_id: r.get(11)?,
                visibility: r.get(12)?,
                created_at_unix_ms: r.get(13)?,
                last_accessed_unix_ms: r.get(14)?,
                follow_count: 0,
                miss_count: 0,
                follow_rate: 0.0,
                efficacy_status: String::new(),
                epistemic_state: String::new(),
                hints: vec![],
                memory_type: String::new(),
                embedding: None,
                _parsed_body: None,
            };
            Ok(CardRow {
                entity: e,
                valid_from_unix_ms: r.get(17)?,
                valid_to_unix_ms: r.get(18)?,
                recorded_at_unix_ms: r.get(19)?,
                invalidated_at_unix_ms: r.get(20)?,
                supersedes: r.get(21).unwrap_or_default(),
                superseded_by: r.get(22).unwrap_or_default(),
            })
        })
        .map_err(|e| format!("claim card: scan failed: {e}"))?
        .filter_map(|r| r.ok())
        .collect();
    Ok(rows)
}

/// Scan the live store once, build the reverse evidence map, and compute
/// the card for `entity_id`. O(n) over live entities (same cost class as
/// the beliefs overlay); the links column is a JSON text array with no
/// index, so a per-target indexed lookup is not available.
pub fn build_claim_card(
    db: &Database,
    entity_id: &str,
    workspace_hash: Option<&str>,
    agent_id: Option<&str>,
    include_evidence: bool,
    include_agent_projection: bool,
    now: i64,
) -> Result<ClaimCard, String> {
    build_claim_card_inner(
        db,
        entity_id,
        workspace_hash,
        agent_id,
        include_evidence,
        include_agent_projection,
        false,
        now,
    )
}

/// Build a card for the internal contradiction surface. This deliberately
/// permits a governance-suppressed row so its existence, validity range, and
/// card digest can be linked without ever serializing its claim/body. The
/// public `build_claim_card` handler remains suppression-enforced.
pub(crate) fn build_claim_card_for_conflict(
    db: &Database,
    entity_id: &str,
    workspace_hash: Option<&str>,
    agent_id: Option<&str>,
    include_evidence: bool,
    include_agent_projection: bool,
    now: i64,
) -> Result<ClaimCard, String> {
    build_claim_card_inner(
        db,
        entity_id,
        workspace_hash,
        agent_id,
        include_evidence,
        include_agent_projection,
        true,
        now,
    )
}

fn build_claim_card_inner(
    db: &Database,
    entity_id: &str,
    workspace_hash: Option<&str>,
    agent_id: Option<&str>,
    include_evidence: bool,
    include_agent_projection: bool,
    _allow_suppressed: bool,
    now: i64,
) -> Result<ClaimCard, String> {
    // Resolve identity for the audit marker, then apply lifecycle, visibility,
    // and workspace withholding below. The returned card never contains a
    // withheld claim/body, including for the public builder.
    let entity = db
        .get_entity_by_id_unfiltered(entity_id)
        .map_err(|e| format!("claim card: entity lookup failed: {e}"))?
        .ok_or_else(|| format!("claim card: no entity with id '{entity_id}'"))?;

    let rows = scan_live_rows(db)?;

    // The target's bi-temporal/supersession columns: from its live row when
    // present, else a direct read of the (possibly archived) row.
    let target_cols = rows
        .iter()
        .find(|r| r.entity.id == entity_id)
        .map(|r| {
            (
                r.valid_from_unix_ms,
                r.valid_to_unix_ms,
                r.recorded_at_unix_ms,
                r.invalidated_at_unix_ms,
                r.supersedes.clone(),
                r.superseded_by.clone(),
            )
        })
        .unwrap_or_else(|| {
            let conn = db.conn().ok();
            let (vf, vt, ra, ia, sup, sup_by) = conn
                .and_then(|c| {
                    c.query_row(
                        "SELECT valid_from_unix_ms, valid_to_unix_ms, recorded_at_unix_ms, \
                                invalidated_at_unix_ms, supersedes, superseded_by \
                         FROM entities WHERE id = ?1",
                        [entity_id],
                        |r| {
                            Ok((
                                r.get::<_, Option<i64>>(0)?,
                                r.get::<_, Option<i64>>(1)?,
                                r.get::<_, Option<i64>>(2)?,
                                r.get::<_, Option<i64>>(3)?,
                                r.get::<_, String>(4).unwrap_or_default(),
                                r.get::<_, String>(5).unwrap_or_default(),
                            ))
                        },
                    )
                    .ok()
                })
                .unwrap_or((None, None, None, None, String::new(), String::new()));
            (vf, vt, ra, ia, sup, sup_by)
        });
    let (valid_from, valid_to, recorded_at, invalidated_at, supersedes, superseded_by) =
        target_cols;

    // Visibility enforcement (#684 semantics): lifecycle is the first gate;
    // private/fleet → author only; workspace → caller's scope must match a
    // non-global entity's scope. Conflict mode may retain a terminal audit
    // marker, but it must never materialize the claim/body.
    let mut withhold_reason: Option<String> = None;
    let lifecycle_serveable = !entity.archived
        && crate::models::canonical_entity_status(&entity.status)
            .is_some_and(|status| matches!(status.as_str(), "active" | "draft"));
    if entity.archived {
        withhold_reason = Some("archived".to_string());
    } else if !lifecycle_serveable {
        withhold_reason = Some("lifecycle_hidden".to_string());
    } else if matches!(entity.visibility.as_str(), "private" | "fleet") {
        let caller = agent_id.unwrap_or_default();
        if caller.is_empty() || caller != entity.agent_id {
            withhold_reason = Some("revoked_access".to_string());
        }
    } else if !entity.workspace_hash.is_empty()
        && workspace_hash.is_some_and(|w| !w.is_empty() && w != entity.workspace_hash)
    {
        withhold_reason = Some("scope_mismatch".to_string());
    }
    let serveable = withhold_reason.is_none();

    let target_body: Value = serde_json::from_str(&entity.body_json).unwrap_or_else(|_| json!({}));
    let memory_kind = memory_kind_of(&target_body);

    // Reverse evidence map: supporter -> target, plus contradiction tags.
    let mut supporters: Vec<String> = Vec::new();
    let mut contradicted = false;
    for row in &rows {
        let e = &row.entity;
        if e.tags.iter().any(|t| t == "contradiction") {
            contradicted = true;
        }
        for link in &e.links {
            if EVIDENCE_RELS.contains(&link.relationship.as_str())
                && link.target_id == entity_id
                && link.target_id != e.id
            {
                supporters.push(e.id.clone());
            }
        }
    }
    supporters.sort();
    supporters.dedup();
    contradicted |= entity.tags.iter().any(|t| t == "contradiction");

    let has_evidence_links = !supporters.is_empty();
    let class = provenance_class(memory_kind.as_deref(), has_evidence_links);

    let body: Value = serde_json::from_str(&entity.body_json).unwrap_or_else(|_| json!({}));
    let claim = if serveable {
        extract_claim(&body, &entity.key)
    } else {
        String::new()
    };
    let support_count = 1 + supporters.len() as i64;
    let superseded = entity.status == "deprecated" || !superseded_by.is_empty();
    let stale = stale_of_row(
        &CardRow {
            entity: entity.clone(),
            valid_from_unix_ms: valid_from,
            valid_to_unix_ms: valid_to,
            recorded_at_unix_ms: recorded_at,
            invalidated_at_unix_ms: invalidated_at,
            supersedes: supersedes.clone(),
            superseded_by: superseded_by.clone(),
        },
        now,
    );
    let revalidation_required = !superseded
        && !entity.archived
        && entity.last_accessed_unix_ms < now - REVALIDATION_WINDOW_MS;

    let mut reason_codes: Vec<String> = Vec::new();
    if serveable {
        reason_codes.push("serveable".to_string());
    } else {
        reason_codes.push(withhold_reason.clone().unwrap());
    }
    if stale {
        reason_codes.push("stale".to_string());
    }
    if supporters.iter().any(|sid| {
        rows.iter()
            .find(|r| r.entity.id == *sid)
            .is_some_and(|r| stale_of_row(r, now))
    }) {
        reason_codes.push("stale_evidence".to_string());
    }
    if (class == Some("inference_agent")) || (memory_kind.is_none() && !has_evidence_links) {
        reason_codes.push("missing_provenance".to_string());
    }
    if contradicted {
        reason_codes.push("contradicted".to_string());
    }
    if superseded {
        reason_codes.push("superseded".to_string());
    }
    if revalidation_required {
        reason_codes.push("revalidation_required".to_string());
    }

    let evidence: Vec<EvidenceRef> = if include_evidence && serveable {
        supporters
            .iter()
            .filter_map(|sid| {
                rows.iter().find(|r| r.entity.id == *sid).map(|row| {
                    let e = &row.entity;
                    let eb: Value =
                        serde_json::from_str(&e.body_json).unwrap_or_else(|_| json!({}));
                    let eclass = provenance_class(memory_kind_of(&eb).as_deref(), false);
                    let rel = e
                        .links
                        .iter()
                        .find(|l| {
                            EVIDENCE_RELS.contains(&l.relationship.as_str())
                                && l.target_id == entity_id
                        })
                        .map(|l| l.relationship.clone())
                        .unwrap_or_else(|| "evidence_for".to_string());
                    EvidenceRef {
                        entity_id: e.id.clone(),
                        claim: extract_claim(&eb, &e.key),
                        relationship: rel,
                        provenance_class: eclass.map(|s| s.to_string()),
                        confidence: confidence_of(e.certainty, e.verified),
                        stale: stale_of_row(row, now),
                        superseded: e.status == "deprecated",
                    }
                })
            })
            .collect()
    } else {
        Vec::new()
    };

    let state = CardState {
        superseded,
        superseded_by: if superseded_by.is_empty() {
            None
        } else {
            Some(superseded_by.clone())
        },
        supersedes: if supersedes.is_empty() {
            None
        } else {
            Some(supersedes.clone())
        },
        stale,
        quarantined: entity.status == "quarantined",
        contradicted,
        revalidation_required,
        archived: entity.archived,
    };

    let times = Times {
        valid_from_unix_ms: valid_from,
        valid_to_unix_ms: valid_to,
        recorded_at_unix_ms: recorded_at,
        invalidated_at_unix_ms: invalidated_at,
    };

    let lifecycle = LifecycleOps {
        confirm: "perseus_vault_score (persistent importance floor) / perseus_vault_follow (efficacy) — both additive".to_string(),
        correct: "perseus_vault_correct — creates a correction entity with history; the original row is retained".to_string(),
        exclude_or_revoke: "perseus_vault_supersede — marks status=deprecated + superseded_by link; original row retained (or perseus_vault_forget to archive)".to_string(),
        revalidate: "perseus_vault_follow / recall refresh — records efficacy and re-touches last_accessed; or operator re-extraction for extracted classes".to_string(),
    };

    // Deterministic digest subset (version 1): claim, class, times, trust
    // numbers, state flags, evidence ids+relationships, lifecycle text.
    let digest_subset = json!({
        "claim": claim,
        "provenance_class": class,
        "times": {
            "valid_from_unix_ms": times.valid_from_unix_ms,
            "valid_to_unix_ms": times.valid_to_unix_ms,
            "recorded_at_unix_ms": times.recorded_at_unix_ms,
            "invalidated_at_unix_ms": times.invalidated_at_unix_ms,
        },
        "confidence": confidence_of(entity.certainty, entity.verified),
        "verified": entity.verified,
        "epistemic_state": entity.epistemic_state,
        "support_count": support_count,
        "state": {
            "superseded": state.superseded,
            "stale": state.stale,
            "quarantined": state.quarantined,
            "contradicted": state.contradicted,
            "revalidation_required": state.revalidation_required,
            "archived": state.archived,
        },
        "evidence": evidence
            .iter()
            .map(|e| {
                json!({
                    "entity_id": e.entity_id,
                    "relationship": e.relationship,
                })
            })
            .collect::<Vec<_>>(),
        "lifecycle": lifecycle,
        "version": CLAIM_CARD_VERSION,
    });
    let digest = sha256_hex(canonical_json(&digest_subset).as_bytes());

    // Sanitized agent projection: text only, no raw bodies, secrets, or
    // prompts. Hash-bound to the evidence-set digest above.
    let mut excluded: Vec<String> = Vec::new();
    let projection_text = if include_agent_projection && serveable {
        let mut suffix = String::new();
        if state.stale {
            suffix.push_str(" · stale");
        }
        if state.contradicted {
            suffix.push_str(" · contradicted");
        }
        if superseded {
            suffix.push_str(" · superseded");
        }
        if revalidation_required {
            suffix.push_str(" · revalidate");
        }
        format!(
            "{} [{} · conf {:.2} · evidence ×{}{}]",
            claim,
            class.unwrap_or("unclassified"),
            confidence_of(entity.certainty, entity.verified),
            support_count,
            suffix
        )
    } else {
        if !serveable {
            excluded.push(format!("withheld: {}", withhold_reason.unwrap()));
        }
        if !include_agent_projection {
            excluded.push("not_requested".to_string());
        }
        excluded.push("raw_bodies".to_string());
        excluded.push("secrets".to_string());
        excluded.push("prompts".to_string());
        String::new()
    };
    let projection_digest = sha256_hex(format!("{}|{}", projection_text, digest).as_bytes());

    Ok(ClaimCard {
        claim_card_version: CLAIM_CARD_VERSION,
        entity_id: entity.id.clone(),
        claim,
        category: entity.category.clone(),
        key: entity.key.clone(),
        entity_type: entity.entity_type.clone(),
        provenance_class: class.map(|s| s.to_string()),
        times,
        confidence: confidence_of(entity.certainty, entity.verified),
        certainty: entity.certainty,
        verified: entity.verified,
        epistemic_state: entity.epistemic_state.clone(),
        support_count,
        state,
        reason_codes,
        evidence,
        lifecycle,
        digest,
        agent_projection: AgentProjection {
            text: projection_text,
            digest: projection_digest,
            excluded,
        },
    })
}

pub fn handle_claim_card(db: &Database, args: Value) -> Result<String, String> {
    let a: ClaimCardArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid claim_card arguments: {e}"))?;
    let now = crate::db::now_ms();
    let card = build_claim_card(
        db,
        &a.entity_id,
        a.workspace_hash.as_deref(),
        a.agent_id.as_deref(),
        a.include_evidence,
        a.include_agent_projection,
        now,
    )?;
    serde_json::to_string(&card).map_err(|e| format!("claim card serialization failed: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::{Database, TestDatabase};
    use crate::models::MemoryLink;
    use std::fs;

    fn temp_db() -> (TestDatabase, String) {
        let db = TestDatabase::new("perseus_vault-test-claim-cards");
        let path = db.path().to_string();
        (db, path)
    }

    fn mk_entity(id: &str, category: &str, key: &str, body: &str) -> Entity {
        Entity {
            id: id.to_string(),
            category: category.to_string(),
            key: key.to_string(),
            body_json: body.to_string(),
            status: "active".to_string(),
            entity_type: "insight".to_string(),
            tags: Vec::new(),
            decay_score: 1.0,
            retrieval_count: 0,
            layer: "working".to_string(),
            topic_path: String::new(),
            archived: false,
            archive_reason: String::new(),
            links: Vec::new(),
            verified: false,
            source: "agent".to_string(),
            always_on: false,
            certainty: 0.8,
            workspace_hash: "ws-cards".to_string(),
            agent_id: String::new(),
            visibility: "workspace".to_string(),
            created_at_unix_ms: 1_700_000_000_000,
            last_accessed_unix_ms: 1_790_000_000_000,
            follow_count: 0,
            miss_count: 0,
            follow_rate: 0.0,
            efficacy_status: "unverified".to_string(),
            epistemic_state: "candidate".to_string(),
            hints: vec![],
            memory_type: String::new(),
            embedding: None,
            _parsed_body: None,
        }
    }

    fn remember(db: &Database, e: &Entity) {
        db.remember(e).unwrap();
    }

    fn ev_link(rel: &str, target: &str) -> MemoryLink {
        MemoryLink {
            relationship: rel.to_string(),
            target_id: target.to_string(),
            weight: 0.5,
            source: None,
            kind: None,
            asserted_at_unix_ms: None,
        }
    }

    const NOW: i64 = 1_800_000_000_000;

    // Clean evidence: an inferred claim with two supporters → fact_derived,
    // serveable, evidence listed, stable digest.
    #[test]
    fn clean_evidence_card_is_serveable_and_deterministic() {
        let (db, path) = temp_db();
        let mut art = mk_entity(
            "artifact-1",
            "source",
            "art-1",
            r#"{"content":"ops runbook page","origin":{"memory_kind":"imported"}}"#,
        );
        art.links = Vec::new();
        remember(&db, &art);
        let mut fact1 = mk_entity(
            "fact-1",
            "observation",
            "fact-1",
            r#"{"content":"deploy ran 21:55Z to 23:19Z","origin":{"memory_kind":"observed"}}"#,
        );
        fact1.links = vec![
            ev_link("derived_from", "artifact-1"),
            ev_link("evidence_for", "fact-2"),
        ];
        remember(&db, &fact1);
        let fact2 = mk_entity(
            "fact-2",
            "observation",
            "fact-2",
            r#"{"content":"deploy windows drop webhooks","origin":{"memory_kind":"inferred"}}"#,
        );
        remember(&db, &fact2);

        let card = build_claim_card(
            &db,
            "fact-2",
            Some("ws-cards"),
            Some("hermes-agent"),
            true,
            true,
            NOW,
        )
        .unwrap();
        assert_eq!(card.provenance_class.as_deref(), Some("fact_derived"));
        assert!(!card.state.superseded);
        assert_eq!(card.support_count, 2);
        assert_eq!(card.reason_codes[0], "serveable");
        assert_eq!(card.evidence.len(), 1);
        assert_eq!(card.evidence[0].entity_id, "fact-1");
        assert_eq!(card.evidence[0].relationship, "evidence_for");
        assert!(!card.digest.is_empty());
        assert!(card.agent_projection.excluded.is_empty());
        assert!(card.agent_projection.text.contains("fact_derived"));

        // Determinism across link ORDER (same set, shuffled) and JSON key
        // order in the source body: digest must be byte-identical.
        let mut fact1b = mk_entity(
            "fact-1",
            "observation",
            "fact-1",
            r#"{"origin":{"memory_kind":"observed"},"content":"deploy ran 21:55Z to 23:19Z"}"#,
        );
        fact1b.links = vec![
            ev_link("evidence_for", "fact-2"),
            ev_link("derived_from", "artifact-1"),
        ];
        remember(&db, &fact1b);
        let card2 = build_claim_card(
            &db,
            "fact-2",
            Some("ws-cards"),
            Some("hermes-agent"),
            true,
            true,
            NOW,
        )
        .unwrap();
        assert_eq!(
            card.digest, card2.digest,
            "digest must be link-order and key-order independent"
        );

        let _ = fs::remove_file(&path);
    }

    // One-off evidence: no supporters → inference_agent, missing provenance
    // flag, still serveable.
    #[test]
    fn one_off_inference_is_inference_agent_with_missing_provenance() {
        let (db, path) = temp_db();
        let e = mk_entity(
            "guess-1",
            "insight",
            "guess-1",
            r#"{"content":"gate on #4 before enabling","origin":{"memory_kind":"inferred"}}"#,
        );
        remember(&db, &e);

        let card =
            build_claim_card(&db, "guess-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert_eq!(card.provenance_class.as_deref(), Some("inference_agent"));
        assert_eq!(card.support_count, 1);
        assert!(card
            .reason_codes
            .contains(&"missing_provenance".to_string()));
        assert!(card.reason_codes.contains(&"serveable".to_string()));
        assert!(card.evidence.is_empty());

        let _ = fs::remove_file(&path);
    }

    // Contradictory support: a contradiction-tagged supporter flips the flag.
    #[test]
    fn contradictory_support_flags_contradicted() {
        let (db, path) = temp_db();
        let mut contra = mk_entity(
            "contra-1",
            "insight",
            "contra-1",
            r#"{"content":"actually gate after #4"}"#,
        );
        contra.tags = vec!["contradiction".to_string()];
        contra.links = vec![ev_link("evidence_for", "claim-1")];
        remember(&db, &contra);
        let claim = mk_entity(
            "claim-1",
            "decision",
            "claim-1",
            r#"{"content":"gate on #4","origin":{"memory_kind":"inferred"}}"#,
        );
        remember(&db, &claim);

        let card =
            build_claim_card(&db, "claim-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert!(card.state.contradicted);
        assert!(card.reason_codes.contains(&"contradicted".to_string()));

        let _ = fs::remove_file(&path);
    }

    // Stale evidence: supporter decay below threshold → stale_evidence.
    #[test]
    fn stale_evidence_flags_stale() {
        let (db, path) = temp_db();
        let mut sup = mk_entity(
            "sup-1",
            "observation",
            "sup-1",
            r#"{"content":"old measurement","origin":{"memory_kind":"observed"}}"#,
        );
        sup.links = vec![ev_link("evidence_for", "der-1")];
        sup.decay_score = 0.2;
        remember(&db, &sup);
        let der = mk_entity(
            "der-1",
            "observation",
            "der-1",
            r#"{"content":"derived from old measurement","origin":{"memory_kind":"inferred"}}"#,
        );
        remember(&db, &der);

        let card = build_claim_card(&db, "der-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert!(card.reason_codes.contains(&"stale_evidence".to_string()));
        assert!(card.evidence.iter().any(|e| e.stale));
        assert!(card.reason_codes.contains(&"serveable".to_string()));

        let _ = fs::remove_file(&path);
    }

    // Correction/supersession: status deprecated + superseded_by → state
    // flags and superseded reason code; history retained.
    #[test]
    fn superseded_card_reports_supersession() {
        let (db, path) = temp_db();
        let old = mk_entity(
            "old-1",
            "fact",
            "old-1",
            r#"{"content":"v1 answer: deploy on wednesdays only","origin":{"memory_kind":"observed"}}"#,
        );
        remember(&db, &old);
        let new = mk_entity(
            "new-1",
            "fact",
            "new-1",
            r#"{"content":"v2 answer: deploys allowed any weekday after review","origin":{"memory_kind":"observed"}}"#,
        );
        remember(&db, &new);
        crate::tools::handle_supersede(
            &db,
            serde_json::json!({
                "from_category": "fact", "from_key": "old-1",
                "to_category": "fact", "to_key": "new-1",
                "reason": "superseded by v2"
            }),
        )
        .unwrap();
        // The bi-temporal supersession column is populated by the
        // supersede/valid_to funnel; pin it here to exercise the card's
        // superseded_by wiring independent of that funnel's write shape.
        db.conn()
            .unwrap()
            .execute(
                "UPDATE entities SET superseded_by = 'new-1' WHERE id = 'old-1'",
                [],
            )
            .unwrap();

        let card = build_claim_card(&db, "old-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert!(card.state.superseded);
        assert_eq!(card.state.superseded_by.as_deref(), Some("new-1"));
        assert!(card.reason_codes.contains(&"superseded".to_string()));

        let _ = fs::remove_file(&path);
    }

    #[test]
    fn conflict_card_withheld_lifecycle_does_not_serialize_claim_or_body() {
        let (db, path) = temp_db();
        let mut entity = mk_entity(
            "terminal-card-1",
            "decision",
            "terminal-card-1",
            r#"{"content":"secret terminal claim","origin":{"memory_kind":"asserted"}}"#,
        );
        entity.status = "deprecated".to_string();
        remember(&db, &entity);

        let card = build_claim_card_for_conflict(
            &db,
            "terminal-card-1",
            Some("ws-cards"),
            None,
            true,
            true,
            NOW,
        )
        .expect("conflict audit marker should remain buildable");
        let serialized = serde_json::to_string(&card).unwrap();
        assert!(
            card.claim.is_empty(),
            "withheld cards must not expose claims"
        );
        assert!(card.agent_projection.text.is_empty());
        assert!(!serialized.contains("secret terminal claim"));
        assert!(card.reason_codes.contains(&"lifecycle_hidden".to_string()));

        let _ = fs::remove_file(&path);
    }

    // Scope mismatch: caller workspace differs from the entity's non-global
    // scope → withheld with scope_mismatch; projection empty + excluded.
    #[test]
    fn scope_mismatch_withholds_projection() {
        let (db, path) = temp_db();
        let mut e = mk_entity(
            "ws-entity-1",
            "decision",
            "ws-entity-1",
            r#"{"content":"internal plan","origin":{"memory_kind":"asserted"}}"#,
        );
        e.workspace_hash = "ws-alpha".to_string();
        remember(&db, &e);

        let card =
            build_claim_card(&db, "ws-entity-1", Some("ws-beta"), None, true, true, NOW).unwrap();
        assert_eq!(card.reason_codes[0], "scope_mismatch");
        assert_eq!(card.agent_projection.text, "");
        assert!(card
            .agent_projection
            .excluded
            .iter()
            .any(|x| x.contains("scope_mismatch")));
        assert_eq!(
            card.entity_id, "ws-entity-1",
            "structural metadata still visible"
        );

        let _ = fs::remove_file(&path);
    }

    // Revoked access: private entity queried by a different agent → withheld.
    #[test]
    fn private_entity_revoked_access_withholds() {
        let (db, path) = temp_db();
        let mut e = mk_entity(
            "priv-1",
            "decision",
            "priv-1",
            r#"{"content":"secret plan","origin":{"memory_kind":"asserted"}}"#,
        );
        e.visibility = "private".to_string();
        e.agent_id = "owner-1".to_string();
        remember(&db, &e);

        let card = build_claim_card(
            &db,
            "priv-1",
            Some("ws-cards"),
            Some("intruder"),
            true,
            true,
            NOW,
        )
        .unwrap();
        assert_eq!(card.reason_codes[0], "revoked_access");
        assert_eq!(card.agent_projection.text, "");

        let owner = build_claim_card(
            &db,
            "priv-1",
            Some("ws-cards"),
            Some("owner-1"),
            true,
            true,
            NOW,
        )
        .unwrap();
        assert_eq!(owner.reason_codes[0], "serveable");

        let _ = fs::remove_file(&path);
    }

    // Bi-temporal + archived: expired valid_to → stale; forget → withheld.
    #[test]
    fn bi_temporal_and_archived_states() {
        let (db, path) = temp_db();
        let mut e = mk_entity(
            "bit-1",
            "fact",
            "bit-1",
            r#"{"content":"temporal fact","origin":{"memory_kind":"observed"}}"#,
        );
        remember(&db, &e);
        // Set the bi-temporal columns directly (they live on the row, not
        // the Entity struct).
        db.conn()
            .unwrap()
            .execute(
                "UPDATE entities SET valid_from_unix_ms = 1700000000000, \
                        valid_to_unix_ms = 1750000000000 WHERE id = 'bit-1'",
                [],
            )
            .unwrap();

        let card = build_claim_card(&db, "bit-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert_eq!(card.times.valid_from_unix_ms, Some(1_700_000_000_000));
        assert_eq!(card.times.valid_to_unix_ms, Some(1_750_000_000_000));
        assert!(card.state.stale, "valid_to in the past → stale");
        assert!(card.reason_codes.contains(&"stale".to_string()));

        db.forget("fact", "bit-1", "test archive").unwrap();
        let archived =
            build_claim_card(&db, "bit-1", Some("ws-cards"), None, true, true, NOW).unwrap();
        assert_eq!(archived.reason_codes[0], "archived");
        assert_eq!(archived.agent_projection.text, "");

        let _ = fs::remove_file(&path);
    }

    // Flags: include_evidence=false and include_agent_projection=false are
    // honored without breaking digest computation.
    #[test]
    fn flags_honored() {
        let (db, path) = temp_db();
        let mut sup = mk_entity(
            "sup-2",
            "observation",
            "sup-2",
            r#"{"content":"supporting row","origin":{"memory_kind":"observed"}}"#,
        );
        sup.links = vec![ev_link("evidence_for", "tgt-2")];
        remember(&db, &sup);
        let tgt = mk_entity(
            "tgt-2",
            "decision",
            "tgt-2",
            r#"{"content":"target claim","origin":{"memory_kind":"inferred"}}"#,
        );
        remember(&db, &tgt);

        let card =
            build_claim_card(&db, "tgt-2", Some("ws-cards"), None, true, false, NOW).unwrap();
        assert_eq!(card.agent_projection.text, "");
        assert!(card
            .agent_projection
            .excluded
            .contains(&"not_requested".to_string()));
        assert!(!card.digest.is_empty());

        let no_evidence =
            build_claim_card(&db, "tgt-2", Some("ws-cards"), None, false, true, NOW).unwrap();
        assert!(no_evidence.evidence.is_empty());
        assert_eq!(
            no_evidence.support_count, 2,
            "support count is store truth, not projection"
        );

        let _ = fs::remove_file(&path);
    }
}
