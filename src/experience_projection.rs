//! Versioned, non-authoritative experience projections (#1173).
//!
//! Experience projections are compact derived metadata over canonical entities
//! and accepted serving/preload telemetry. They never contain entity bodies,
//! prompts, credentials, authorization material, or caller-supplied trust
//! claims. The canonical entity reader is the authority at every public read.

use crate::db::Database;
use crate::models::Entity;
use crate::validity::{self, ValidityInfo, ValidityWeights};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::HashSet;

/// Version of the serialized experience projection contract.
pub const SCHEMA_VERSION: i64 = 1;
/// Human-readable identifier included in answer-facing reports.
pub const PROJECTION_KIND: &str = "derived_experience_retrieval_projection";
const MAX_SOURCE_IDS: usize = 64;
const MAX_EVENT_IDS: usize = 128;
const MAX_PULSE_IDS: usize = 128;
const MAX_ID_CHARS: usize = 256;
const MAX_LAYER_CHARS: usize = 64;

/// A rebuild request. `source_entity_ids` are canonical IDs; event and pulse
/// references must resolve to Vault-owned telemetry rows before a projection is
/// written. Metrics are intentionally absent: they are derived below rather
/// than accepted as caller assertions.
#[derive(Debug, Clone, Deserialize)]
pub struct ExperienceProjectionRequest {
    pub schema_version: i64,
    pub experience_id: String,
    pub workspace_hash: String,
    pub graph_side: String,
    pub layer: String,
    #[serde(default)]
    pub source_entity_ids: Vec<String>,
    #[serde(default)]
    pub source_event_ids: Vec<String>,
    #[serde(default)]
    pub pulse_ids: Vec<String>,
    pub query_time_unix_ms: i64,
}

impl ExperienceProjectionRequest {
    fn normalized_for_rebuild(&self) -> Result<Self, String> {
        if self.schema_version != SCHEMA_VERSION {
            return Err(format!(
                "unsupported experience projection schema_version {}; expected {}",
                self.schema_version, SCHEMA_VERSION
            ));
        }
        let experience_id = valid_identifier("experience_id", &self.experience_id, MAX_ID_CHARS)?;
        let workspace_hash =
            valid_identifier("workspace_hash", &self.workspace_hash, MAX_ID_CHARS)?;
        let graph_side = match self.graph_side.trim() {
            "source" | "target" | "context" | "none" => self.graph_side.trim().to_string(),
            other => {
                return Err(format!(
                    "invalid graph_side '{other}': expected source, target, context, or none"
                ))
            }
        };
        let layer = valid_identifier("layer", &self.layer, MAX_LAYER_CHARS)?;
        let source_entity_ids = normalize_ids(
            "source_entity_ids",
            &self.source_entity_ids,
            MAX_SOURCE_IDS,
            true,
        )?;
        let source_event_ids = normalize_ids(
            "source_event_ids",
            &self.source_event_ids,
            MAX_EVENT_IDS,
            false,
        )?;
        let pulse_ids = normalize_ids("pulse_ids", &self.pulse_ids, MAX_PULSE_IDS, false)?;
        if source_event_ids.is_empty() && pulse_ids.is_empty() {
            return Err(
                "experience projection requires at least one accepted source_event_id or pulse_id"
                    .to_string(),
            );
        }
        if self.query_time_unix_ms < 0 {
            return Err("query_time_unix_ms must be non-negative".to_string());
        }
        Ok(Self {
            schema_version: self.schema_version,
            experience_id,
            workspace_hash,
            graph_side,
            layer,
            source_entity_ids,
            source_event_ids,
            pulse_ids,
            query_time_unix_ms: self.query_time_unix_ms,
        })
    }
}

/// Exact scope recorded on every projection. Vault currently uses the named
/// workspace as the tenant partition; keeping both fields explicit makes that
/// relationship visible and prevents an implicit global fallback.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProjectionScope {
    pub tenant_id: String,
    pub workspace_hash: String,
    pub principal_id: String,
    pub agent_id: String,
}

/// Bounded signals derived from canonical entity state and accepted telemetry.
/// These values rank a projection; they do not assert truth or admission.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProjectionSignals {
    pub activation: f64,
    pub utility: f64,
    pub preference: f64,
    pub confidence: f64,
}

/// Metadata returned for a source after the ordinary canonical read gates have
/// passed. No body, verification flag, or caller-supplied authority claim is
/// copied into this response.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ResolvedSource {
    pub id: String,
    pub category: String,
    pub key: String,
    pub workspace_hash: String,
    pub agent_id: String,
    pub status: String,
    pub validity_grade: String,
    pub validity_value: f64,
}

#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ProjectionFallback {
    pub mode: String,
    pub reason: String,
}

/// Public report for both rebuild and read. The report is explicitly a derived
/// retrieval projection; `evidence_authority` tells consumers that canonical
/// source resolution, not these ranking fields, is authoritative.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct ExperienceProjectionReport {
    pub projection_kind: String,
    pub schema_version: i64,
    pub projection_version: i64,
    pub projection_revision: i64,
    pub projection_id: String,
    pub experience_id: String,
    pub scope: ProjectionScope,
    pub graph_side: String,
    pub layer: String,
    pub source_entity_ids: Vec<String>,
    pub source_event_ids: Vec<String>,
    pub pulse_ids: Vec<String>,
    pub observed_at_unix_ms: i64,
    pub signals: ProjectionSignals,
    pub source_digest: String,
    pub projection_digest: String,
    pub state: String,
    pub read_mode: String,
    pub evidence_authority: String,
    pub resolved_sources: Vec<ResolvedSource>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fallback: Option<ProjectionFallback>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rebuild_event_id: Option<String>,
}

#[derive(Debug, Clone)]
struct StoredProjection {
    projection_id: String,
    schema_version: i64,
    projection_version: i64,
    projection_revision: i64,
    experience_id: String,
    tenant_id: String,
    workspace_hash: String,
    principal_id: String,
    agent_id: String,
    graph_side: String,
    layer: String,
    source_event_ids: Vec<String>,
    pulse_ids: Vec<String>,
    activation: f64,
    utility: f64,
    preference: f64,
    confidence: f64,
    source_digest: String,
    projection_digest: String,
    state: String,
    state_reason: String,
    observed_at_unix_ms: i64,
    updated_at_unix_ms: i64,
}

#[derive(Debug, Default)]
struct AcceptedRefs {
    count: usize,
}

#[derive(Debug)]
struct ResolvedInputs {
    entities: Vec<Entity>,
    sources: Vec<ResolvedSource>,
    agent_id: String,
    source_digest: String,
}

#[derive(Debug, Serialize)]
struct SourceMaterial {
    id: String,
    category: String,
    key: String,
    workspace_hash: String,
    agent_id: String,
    status: String,
    layer: String,
    memory_type: String,
    body_sha256: String,
    created_at_unix_ms: i64,
    follow_rate: f64,
    epistemic_state: String,
    validity_grade: String,
    validity_value: f64,
}

#[derive(Debug, Serialize)]
struct ProjectionMaterial<'a> {
    schema_version: i64,
    projection_version: i64,
    projection_id: &'a str,
    experience_id: &'a str,
    tenant_id: &'a str,
    workspace_hash: &'a str,
    principal_id: &'a str,
    agent_id: &'a str,
    graph_side: &'a str,
    layer: &'a str,
    source_entity_ids: &'a [String],
    source_event_ids: &'a [String],
    pulse_ids: &'a [String],
    observed_at_unix_ms: i64,
    signals: &'a ProjectionSignals,
    source_digest: &'a str,
}

fn valid_identifier(label: &str, value: &str, max_chars: usize) -> Result<String, String> {
    let value = value.trim();
    if value.is_empty() {
        return Err(format!("{label} must be non-empty"));
    }
    if value.chars().count() > max_chars || value.chars().any(char::is_control) {
        return Err(format!(
            "{label} is invalid or exceeds {max_chars} characters"
        ));
    }
    Ok(value.to_string())
}

fn normalize_ids(
    label: &str,
    values: &[String],
    max_items: usize,
    required: bool,
) -> Result<Vec<String>, String> {
    if values.len() > max_items {
        return Err(format!("{label} may contain at most {max_items} entries"));
    }
    let mut out = Vec::with_capacity(values.len());
    let mut seen = HashSet::new();
    for value in values {
        let value = valid_identifier(label, value, MAX_ID_CHARS)?;
        if !seen.insert(value.clone()) {
            return Err(format!("{label} contains duplicate id '{value}'"));
        }
        out.push(value);
    }
    out.sort();
    if required && out.is_empty() {
        return Err(format!("{label} must contain at least one id"));
    }
    Ok(out)
}

fn digest_json<T: Serialize>(value: &T) -> String {
    let encoded = serde_json::to_vec(value).unwrap_or_default();
    format!("{:x}", Sha256::digest(encoded))
}

fn projection_id(experience_id: &str, workspace_hash: &str, principal_id: &str) -> String {
    let material = format!(
        "perseus-vault-experience-projection|{}|{}|{}|{}",
        SCHEMA_VERSION, workspace_hash, principal_id, experience_id
    );
    format!("exp-{}", &crate::db::sha256_hex(&material)[..32])
}

fn trust_confidence(entity: &Entity) -> f64 {
    match entity.epistemic_state.trim() {
        "verified" => 1.0,
        "corroborated" => 0.75,
        "candidate" | "" => 0.5,
        "defensively_recalled" => 0.25,
        // Rejected rows cannot pass the canonical public source reader. Keep
        // the defensive value for a direct unit caller rather than trusting it.
        "rejected" => 0.0,
        _ => 0.5,
    }
}

fn derive_signals(entities: &[Entity], accepted_ref_count: usize) -> ProjectionSignals {
    let activation = (accepted_ref_count as f64 / 8.0).clamp(0.0, 1.0);
    let utility = if entities.is_empty() {
        0.0
    } else {
        let total: f64 = entities
            .iter()
            .map(|entity| {
                if entity.follow_rate.is_finite() {
                    entity.follow_rate.clamp(0.0, 1.0)
                } else {
                    0.0
                }
            })
            .sum();
        (total / entities.len() as f64).clamp(0.0, 1.0)
    };
    let preference_count = entities
        .iter()
        .filter(|entity| {
            entity.memory_type == "preference"
                || entity.entity_type == "preference"
                || entity.category == "preference"
        })
        .count();
    let preference = if entities.is_empty() {
        0.0
    } else {
        (preference_count as f64 / entities.len() as f64).clamp(0.0, 1.0)
    };
    let confidence = entities
        .iter()
        .map(trust_confidence)
        .fold(1.0, f64::min)
        .clamp(0.0, 1.0);
    ProjectionSignals {
        activation,
        utility,
        preference,
        confidence,
    }
}

fn source_material(entity: &Entity, validity: &ValidityInfo) -> SourceMaterial {
    SourceMaterial {
        id: entity.id.clone(),
        category: entity.category.clone(),
        key: entity.key.clone(),
        workspace_hash: entity.workspace_hash.clone(),
        agent_id: entity.agent_id.clone(),
        status: entity.status.clone(),
        layer: entity.layer.clone(),
        memory_type: entity.memory_type.clone(),
        body_sha256: crate::db::sha256_hex(&entity.body_json),
        created_at_unix_ms: entity.created_at_unix_ms,
        follow_rate: if entity.follow_rate.is_finite() {
            entity.follow_rate.clamp(0.0, 1.0)
        } else {
            0.0
        },
        epistemic_state: entity.epistemic_state.clone(),
        validity_grade: validity.grade.clone(),
        validity_value: validity.freshness,
    }
}

fn source_digest(entities: &[Entity], observed_at_unix_ms: i64) -> String {
    let weights = ValidityWeights::default();
    let materials: Vec<SourceMaterial> = entities
        .iter()
        .map(|entity| {
            let validity = validity::score(
                observed_at_unix_ms,
                entity.created_at_unix_ms,
                crate::db::entity_expiry_ms(&entity.body_json),
                &entity.workspace_hash,
                Some(&entity.workspace_hash),
                &entity.epistemic_state,
                &entity.status,
                &weights,
            );
            source_material(entity, &validity)
        })
        .collect();
    digest_json(&materials)
}

fn projection_digest(
    req: &ExperienceProjectionRequest,
    projection_id: &str,
    principal_id: &str,
    agent_id: &str,
    signals: &ProjectionSignals,
    source_digest: &str,
) -> String {
    digest_json(&ProjectionMaterial {
        schema_version: SCHEMA_VERSION,
        projection_version: SCHEMA_VERSION,
        projection_id,
        experience_id: &req.experience_id,
        tenant_id: &req.workspace_hash,
        workspace_hash: &req.workspace_hash,
        principal_id,
        agent_id,
        graph_side: &req.graph_side,
        layer: &req.layer,
        source_entity_ids: &req.source_entity_ids,
        source_event_ids: &req.source_event_ids,
        pulse_ids: &req.pulse_ids,
        observed_at_unix_ms: req.query_time_unix_ms,
        signals,
        source_digest,
    })
}

fn resolved_source(
    entity: &Entity,
    observed_at_unix_ms: i64,
    workspace_hash: &str,
) -> ResolvedSource {
    let validity = validity::score(
        observed_at_unix_ms,
        entity.created_at_unix_ms,
        crate::db::entity_expiry_ms(&entity.body_json),
        &entity.workspace_hash,
        Some(workspace_hash),
        &entity.epistemic_state,
        &entity.status,
        &ValidityWeights::default(),
    );
    ResolvedSource {
        id: entity.id.clone(),
        category: entity.category.clone(),
        key: entity.key.clone(),
        workspace_hash: entity.workspace_hash.clone(),
        agent_id: entity.agent_id.clone(),
        status: entity.status.clone(),
        validity_grade: validity.grade,
        validity_value: validity.freshness,
    }
}

fn resolve_sources(
    db: &Database,
    source_entity_ids: &[String],
    workspace_hash: &str,
    principal_id: &str,
    observed_at_unix_ms: i64,
) -> Result<ResolvedInputs, String> {
    let mut entities = Vec::with_capacity(source_entity_ids.len());
    let mut sources = Vec::with_capacity(source_entity_ids.len());
    let mut agent_id: Option<String> = None;
    for id in source_entity_ids {
        let entity = db
            .get_entity_by_id_for_requester(id, Some(principal_id))
            .map_err(|error| format!("canonical source lookup failed: {error}"))?
            .ok_or_else(|| format!("canonical source '{id}' is unavailable to this principal"))?;
        if entity.workspace_hash != workspace_hash {
            return Err(format!(
                "canonical source '{id}' is outside workspace_hash scope"
            ));
        }
        let validity = validity::score(
            observed_at_unix_ms,
            entity.created_at_unix_ms,
            crate::db::entity_expiry_ms(&entity.body_json),
            &entity.workspace_hash,
            Some(workspace_hash),
            &entity.epistemic_state,
            &entity.status,
            &ValidityWeights::default(),
        );
        if validity.scope_match != "exact"
            || validity.superseded
            || validity.expired
            || validity.grade == "context_invalid"
            || crate::db::entity_expiry_ms(&entity.body_json)
                .is_some_and(|expiry| expiry <= observed_at_unix_ms)
        {
            return Err(format!(
                "canonical source '{id}' is not valid at query_time_unix_ms"
            ));
        }
        match &agent_id {
            Some(existing) if existing != &entity.agent_id => {
                return Err(
                    "experience projection cannot merge canonical sources from different agents"
                        .to_string(),
                )
            }
            None => agent_id = Some(entity.agent_id.clone()),
            _ => {}
        }
        sources.push(resolved_source(
            &entity,
            observed_at_unix_ms,
            workspace_hash,
        ));
        entities.push(entity);
    }
    let agent_id = agent_id.unwrap_or_default();
    let digest = source_digest(&entities, observed_at_unix_ms);
    Ok(ResolvedInputs {
        entities,
        sources,
        agent_id,
        source_digest: digest,
    })
}

fn validate_layer(entities: &[Entity], layer: &str) -> Result<(), String> {
    if layer == "mixed" {
        return Ok(());
    }
    if entities.iter().all(|entity| entity.layer == layer) {
        Ok(())
    } else {
        Err(format!(
            "layer '{layer}' does not match every canonical source; use layer='mixed' for a mixed source set"
        ))
    }
}

/// Only Vault-owned, already-recorded telemetry is accepted as a relationship
/// basis. A serving event must come from one batch and the principal that built
/// the projection. A pulse is a preload event tied to the same source scope.
fn validate_event_refs(
    db: &Database,
    req: &ExperienceProjectionRequest,
    principal_id: &str,
) -> Result<AcceptedRefs, String> {
    let conn = db
        .conn()
        .map_err(|error| format!("experience event lookup failed: {error}"))?;
    let source_ids: HashSet<&str> = req.source_entity_ids.iter().map(String::as_str).collect();
    let mut batches = HashSet::new();
    let mut sessions = HashSet::new();
    let mut accepted = AcceptedRefs::default();

    for event_id in &req.source_event_ids {
        let event: Option<(String, String, String, String)> = conn
            .query_row(
                "SELECT entity_id, workspace_hash, profile, batch_id
                 FROM served_events WHERE id = ?1",
                params![event_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .optional()
            .map_err(|error| format!("source event lookup failed: {error}"))?;
        let Some((entity_id, workspace_hash, profile, batch_id)) = event else {
            return Err(format!(
                "source_event_id '{event_id}' is not an accepted serving event"
            ));
        };
        if workspace_hash != req.workspace_hash
            || !source_ids.contains(entity_id.as_str())
            || (!profile.is_empty() && profile != principal_id)
            || batch_id.trim().is_empty()
        {
            return Err(format!(
                "source_event_id '{event_id}' is outside the projection scope"
            ));
        }
        batches.insert(batch_id);
        accepted.count += 1;
    }
    if batches.len() > 1 {
        return Err("source_event_ids must belong to one accepted serving batch".to_string());
    }

    for pulse_id in &req.pulse_ids {
        let pulse: Option<(String, String, String)> = conn
            .query_row(
                "SELECT entity_id, workspace_hash, session_id
                 FROM preload_events WHERE id = ?1",
                params![pulse_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .optional()
            .map_err(|error| format!("pulse lookup failed: {error}"))?;
        let Some((entity_id, workspace_hash, session_id)) = pulse else {
            return Err(format!(
                "pulse_id '{pulse_id}' is not an accepted preload event"
            ));
        };
        if workspace_hash != req.workspace_hash
            || !source_ids.contains(entity_id.as_str())
            || session_id.trim().is_empty()
        {
            return Err(format!(
                "pulse_id '{pulse_id}' is outside the projection scope"
            ));
        }
        sessions.insert(session_id);
        accepted.count += 1;
    }
    if sessions.len() > 1 {
        return Err("pulse_ids must belong to one accepted preload session".to_string());
    }
    Ok(accepted)
}

fn stored_projection_from_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<StoredProjection> {
    let source_event_ids_json: String = row.get(11)?;
    let pulse_ids_json: String = row.get(12)?;
    Ok(StoredProjection {
        projection_id: row.get(0)?,
        schema_version: row.get(1)?,
        projection_version: row.get(2)?,
        projection_revision: row.get(3)?,
        experience_id: row.get(4)?,
        tenant_id: row.get(5)?,
        workspace_hash: row.get(6)?,
        principal_id: row.get(7)?,
        agent_id: row.get(8)?,
        graph_side: row.get(9)?,
        layer: row.get(10)?,
        source_event_ids: serde_json::from_str(&source_event_ids_json).unwrap_or_default(),
        pulse_ids: serde_json::from_str(&pulse_ids_json).unwrap_or_default(),
        activation: row.get(13)?,
        utility: row.get(14)?,
        preference: row.get(15)?,
        confidence: row.get(16)?,
        source_digest: row.get(17)?,
        projection_digest: row.get(18)?,
        state: row.get(19)?,
        state_reason: row.get(20)?,
        observed_at_unix_ms: row.get(21)?,
        updated_at_unix_ms: row.get(22)?,
    })
}

fn load_projection(
    conn: &Connection,
    experience_id: &str,
    workspace_hash: &str,
    principal_id: &str,
) -> rusqlite::Result<Option<StoredProjection>> {
    conn.query_row(
        "SELECT projection_id, schema_version, projection_version,
                projection_revision, experience_id, tenant_id, workspace_hash,
                principal_id, agent_id, graph_side, layer,
                source_event_ids_json, pulse_ids_json, activation, utility,
                preference, confidence, source_digest, projection_digest, state,
                state_reason, observed_at_unix_ms, updated_at_unix_ms
         FROM experience_projections
         WHERE tenant_id = ?1 AND workspace_hash = ?2
           AND principal_id = ?3 AND experience_id = ?4",
        params![workspace_hash, workspace_hash, principal_id, experience_id],
        stored_projection_from_row,
    )
    .optional()
}

fn load_source_ids(conn: &Connection, projection_id: &str) -> rusqlite::Result<Vec<String>> {
    let mut stmt = conn.prepare(
        "SELECT source_entity_id FROM experience_projection_sources
         WHERE projection_id = ?1 ORDER BY source_entity_id ASC",
    )?;
    let rows = stmt.query_map(params![projection_id], |row| row.get(0))?;
    rows.collect()
}

fn report_from_stored(
    stored: &StoredProjection,
    source_entity_ids: Vec<String>,
    sources: Vec<ResolvedSource>,
    read_mode: &str,
    fallback: Option<ProjectionFallback>,
    rebuild_event_id: Option<String>,
) -> ExperienceProjectionReport {
    ExperienceProjectionReport {
        projection_kind: PROJECTION_KIND.to_string(),
        schema_version: stored.schema_version,
        projection_version: stored.projection_version,
        projection_revision: stored.projection_revision,
        projection_id: stored.projection_id.clone(),
        experience_id: stored.experience_id.clone(),
        scope: ProjectionScope {
            tenant_id: stored.tenant_id.clone(),
            workspace_hash: stored.workspace_hash.clone(),
            principal_id: stored.principal_id.clone(),
            agent_id: stored.agent_id.clone(),
        },
        graph_side: stored.graph_side.clone(),
        layer: stored.layer.clone(),
        source_entity_ids,
        source_event_ids: stored.source_event_ids.clone(),
        pulse_ids: stored.pulse_ids.clone(),
        observed_at_unix_ms: stored.observed_at_unix_ms,
        signals: ProjectionSignals {
            activation: stored.activation,
            utility: stored.utility,
            preference: stored.preference,
            confidence: stored.confidence,
        },
        source_digest: stored.source_digest.clone(),
        projection_digest: stored.projection_digest.clone(),
        state: stored.state.clone(),
        read_mode: read_mode.to_string(),
        evidence_authority: "canonical_source_resolution".to_string(),
        resolved_sources: sources,
        fallback,
        rebuild_event_id,
    }
}

fn fallback_report(
    projection: Option<&StoredProjection>,
    experience_id: &str,
    workspace_hash: &str,
    principal_id: &str,
    observed_at_unix_ms: i64,
    state: &str,
    reason: &str,
) -> ExperienceProjectionReport {
    let (
        projection_id,
        projection_version,
        projection_revision,
        graph_side,
        layer,
        source_digest,
        projection_digest,
        tenant_id,
        agent_id,
    ) = projection
        .map(|stored| {
            (
                stored.projection_id.clone(),
                stored.projection_version,
                stored.projection_revision,
                stored.graph_side.clone(),
                stored.layer.clone(),
                stored.source_digest.clone(),
                stored.projection_digest.clone(),
                stored.tenant_id.clone(),
                stored.agent_id.clone(),
            )
        })
        .unwrap_or_else(|| {
            (
                String::new(),
                SCHEMA_VERSION,
                0,
                "none".to_string(),
                "mixed".to_string(),
                String::new(),
                String::new(),
                workspace_hash.to_string(),
                String::new(),
            )
        });
    ExperienceProjectionReport {
        projection_kind: PROJECTION_KIND.to_string(),
        schema_version: SCHEMA_VERSION,
        projection_version,
        projection_revision,
        projection_id,
        experience_id: experience_id.to_string(),
        scope: ProjectionScope {
            tenant_id,
            workspace_hash: workspace_hash.to_string(),
            principal_id: principal_id.to_string(),
            agent_id,
        },
        graph_side,
        layer,
        source_entity_ids: Vec::new(),
        source_event_ids: Vec::new(),
        pulse_ids: Vec::new(),
        observed_at_unix_ms,
        signals: ProjectionSignals {
            activation: 0.0,
            utility: 0.0,
            preference: 0.0,
            confidence: 0.0,
        },
        source_digest,
        projection_digest,
        state: state.to_string(),
        read_mode: "canonical_fallback".to_string(),
        evidence_authority: "canonical_source_resolution".to_string(),
        resolved_sources: Vec::new(),
        fallback: Some(ProjectionFallback {
            mode: "canonical_retrieval".to_string(),
            reason: reason.to_string(),
        }),
        rebuild_event_id: None,
    }
}

/// Rebuild and durably upsert one projection. Every write is a single SQLite
/// transaction covering the current row, source relation rows, and rebuild
/// ledger entry. Replaying the same canonical state/config is idempotent.
pub fn rebuild(
    db: &Database,
    request: &ExperienceProjectionRequest,
    principal_id: &str,
) -> Result<ExperienceProjectionReport, String> {
    let req = request.normalized_for_rebuild()?;
    let principal_id = valid_identifier("principal_id", principal_id, MAX_ID_CHARS)?;
    let inputs = resolve_sources(
        db,
        &req.source_entity_ids,
        &req.workspace_hash,
        &principal_id,
        req.query_time_unix_ms,
    )?;
    validate_layer(&inputs.entities, &req.layer)?;
    let accepted = validate_event_refs(db, &req, &principal_id)?;
    let signals = derive_signals(&inputs.entities, accepted.count);
    let projection_id = projection_id(&req.experience_id, &req.workspace_hash, &principal_id);
    let projection_digest = projection_digest(
        &req,
        &projection_id,
        &principal_id,
        &inputs.agent_id,
        &signals,
        &inputs.source_digest,
    );
    let source_event_ids_json = serde_json::to_string(&req.source_event_ids)
        .map_err(|error| format!("source event serialization failed: {error}"))?;
    let pulse_ids_json = serde_json::to_string(&req.pulse_ids)
        .map_err(|error| format!("pulse serialization failed: {error}"))?;
    let source_ids_json = serde_json::to_string(&req.source_entity_ids)
        .map_err(|error| format!("source serialization failed: {error}"))?;
    let event_id = format!(
        "exp-event-{}",
        &crate::db::sha256_hex(&format!(
            "{}|rebuild|{}|{}|{}",
            projection_id, inputs.source_digest, source_event_ids_json, pulse_ids_json
        ))[..32]
    );
    let conn = db
        .conn()
        .map_err(|error| format!("experience projection write connection failed: {error}"))?;
    let tx = conn
        .unchecked_transaction()
        .map_err(|error| format!("experience projection transaction failed: {error}"))?;
    let previous: Option<(i64, String, String)> = tx
        .query_row(
            "SELECT projection_revision, source_digest, projection_digest
             FROM experience_projections WHERE projection_id = ?1",
            params![projection_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()
        .map_err(|error| format!("experience projection read-before-write failed: {error}"))?;
    let revision = match previous.as_ref() {
        Some((revision, old_source, old_projection))
            if old_source == &inputs.source_digest && old_projection == &projection_digest =>
        {
            *revision
        }
        Some((revision, _, _)) => revision + 1,
        None => 1,
    };
    tx.execute(
        "INSERT INTO experience_projections
           (projection_id, schema_version, projection_version, projection_revision,
            experience_id, tenant_id, workspace_hash, principal_id, agent_id,
            graph_side, layer, source_event_ids_json, pulse_ids_json,
            activation, utility, preference, confidence, source_digest,
            projection_digest, state, state_reason, observed_at_unix_ms,
            built_at_unix_ms, updated_at_unix_ms)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                 ?14, ?15, ?16, ?17, ?18, ?19, 'active', '', ?20, ?20, ?20)
         ON CONFLICT(projection_id) DO UPDATE SET
            schema_version = excluded.schema_version,
            projection_version = excluded.projection_version,
            projection_revision = excluded.projection_revision,
            experience_id = excluded.experience_id,
            tenant_id = excluded.tenant_id,
            workspace_hash = excluded.workspace_hash,
            principal_id = excluded.principal_id,
            agent_id = excluded.agent_id,
            graph_side = excluded.graph_side,
            layer = excluded.layer,
            source_event_ids_json = excluded.source_event_ids_json,
            pulse_ids_json = excluded.pulse_ids_json,
            activation = excluded.activation,
            utility = excluded.utility,
            preference = excluded.preference,
            confidence = excluded.confidence,
            source_digest = excluded.source_digest,
            projection_digest = excluded.projection_digest,
            state = 'active',
            state_reason = '',
            observed_at_unix_ms = excluded.observed_at_unix_ms,
            updated_at_unix_ms = excluded.updated_at_unix_ms",
        params![
            projection_id,
            SCHEMA_VERSION,
            SCHEMA_VERSION,
            revision,
            req.experience_id,
            req.workspace_hash,
            req.workspace_hash,
            principal_id,
            inputs.agent_id,
            req.graph_side,
            req.layer,
            source_event_ids_json,
            pulse_ids_json,
            signals.activation,
            signals.utility,
            signals.preference,
            signals.confidence,
            inputs.source_digest,
            projection_digest,
            req.query_time_unix_ms,
        ],
    )
    .map_err(|error| format!("experience projection upsert failed: {error}"))?;
    tx.execute(
        "DELETE FROM experience_projection_sources WHERE projection_id = ?1",
        params![projection_id],
    )
    .map_err(|error| format!("experience source relation reset failed: {error}"))?;
    for entity in &inputs.entities {
        tx.execute(
            "INSERT INTO experience_projection_sources
               (projection_id, source_entity_id, source_digest)
             VALUES (?1, ?2, ?3)",
            params![
                projection_id,
                entity.id,
                crate::db::sha256_hex(&entity.body_json)
            ],
        )
        .map_err(|error| format!("experience source relation write failed: {error}"))?;
    }
    tx.execute(
        "INSERT OR IGNORE INTO experience_projection_events
           (event_id, projection_id, event_kind, source_entity_ids_json,
            source_event_ids_json, pulse_ids_json, source_digest,
            projection_digest, tenant_id, workspace_hash, principal_id,
            agent_id, recorded_at_unix_ms)
         VALUES (?1, ?2, 'rebuild', ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        params![
            event_id,
            projection_id,
            source_ids_json,
            source_event_ids_json,
            pulse_ids_json,
            inputs.source_digest,
            projection_digest,
            req.workspace_hash,
            req.workspace_hash,
            principal_id,
            inputs.agent_id,
            req.query_time_unix_ms,
        ],
    )
    .map_err(|error| format!("experience rebuild ledger write failed: {error}"))?;
    tx.commit()
        .map_err(|error| format!("experience projection commit failed: {error}"))?;

    let stored = StoredProjection {
        projection_id,
        schema_version: SCHEMA_VERSION,
        projection_version: SCHEMA_VERSION,
        projection_revision: revision,
        experience_id: req.experience_id,
        tenant_id: req.workspace_hash.clone(),
        workspace_hash: req.workspace_hash,
        principal_id,
        agent_id: inputs.agent_id,
        graph_side: req.graph_side,
        layer: req.layer,
        source_event_ids: req.source_event_ids,
        pulse_ids: req.pulse_ids,
        activation: signals.activation,
        utility: signals.utility,
        preference: signals.preference,
        confidence: signals.confidence,
        source_digest: inputs.source_digest,
        projection_digest,
        state: "active".to_string(),
        state_reason: String::new(),
        observed_at_unix_ms: req.query_time_unix_ms,
        updated_at_unix_ms: req.query_time_unix_ms,
    };
    Ok(report_from_stored(
        &stored,
        inputs
            .entities
            .iter()
            .map(|entity| entity.id.clone())
            .collect(),
        inputs.sources,
        "rebuild",
        None,
        Some(event_id),
    ))
}

/// Read one projection through canonical source resolution. A missing, stale,
/// quarantined, scope-mismatched, expired, superseded, or digest-mismatched
/// source returns a canonical fallback marker and never emits projection
/// signals as evidence.
pub fn read(
    db: &Database,
    experience_id: &str,
    workspace_hash: &str,
    principal_id: &str,
) -> Result<ExperienceProjectionReport, String> {
    let experience_id = valid_identifier("experience_id", experience_id, MAX_ID_CHARS)?;
    let workspace_hash = valid_identifier("workspace_hash", workspace_hash, MAX_ID_CHARS)?;
    let principal_id = valid_identifier("principal_id", principal_id, MAX_ID_CHARS)?;
    let observed_at_unix_ms = crate::db::now_ms();
    let conn = db
        .conn()
        .map_err(|error| format!("experience projection read connection failed: {error}"))?;
    let Some(stored) = load_projection(&conn, &experience_id, &workspace_hash, &principal_id)
        .map_err(|error| format!("experience projection lookup failed: {error}"))?
    else {
        return Ok(fallback_report(
            None,
            &experience_id,
            &workspace_hash,
            &principal_id,
            observed_at_unix_ms,
            "missing",
            "projection_missing_use_canonical_retrieval",
        ));
    };
    if stored.state != "active" {
        return Ok(fallback_report(
            Some(&stored),
            &experience_id,
            &workspace_hash,
            &principal_id,
            stored.observed_at_unix_ms,
            &stored.state,
            &format!("projection_state_{}", stored.state),
        ));
    }
    let source_entity_ids = load_source_ids(&conn, &stored.projection_id)
        .map_err(|error| format!("experience source relation lookup failed: {error}"))?;
    if source_entity_ids.is_empty()
        || stored.schema_version != SCHEMA_VERSION
        || stored.projection_version != SCHEMA_VERSION
        || stored.tenant_id != workspace_hash
        || stored.workspace_hash != workspace_hash
        || stored.principal_id != principal_id
    {
        mark_projection_stale_with_conn(
            &conn,
            &stored.projection_id,
            "projection_scope_or_schema_mismatch",
        )
        .map_err(|error| format!("experience stale marker failed: {error}"))?;
        return Ok(fallback_report(
            Some(&stored),
            &experience_id,
            &workspace_hash,
            &principal_id,
            stored.observed_at_unix_ms,
            "stale",
            "projection_scope_or_schema_mismatch",
        ));
    }
    drop(conn);

    let inputs = match resolve_sources(
        db,
        &source_entity_ids,
        &workspace_hash,
        &principal_id,
        stored.observed_at_unix_ms,
    ) {
        Ok(inputs) => inputs,
        Err(_) => {
            let conn = db
                .conn()
                .map_err(|error| format!("experience stale marker connection failed: {error}"))?;
            mark_projection_stale_with_conn(
                &conn,
                &stored.projection_id,
                "canonical_source_unavailable",
            )
            .map_err(|error| format!("experience stale marker failed: {error}"))?;
            return Ok(fallback_report(
                Some(&stored),
                &experience_id,
                &workspace_hash,
                &principal_id,
                stored.observed_at_unix_ms,
                "stale",
                "canonical_source_unavailable",
            ));
        }
    };
    if validate_layer(&inputs.entities, &stored.layer).is_err() {
        let conn = db
            .conn()
            .map_err(|error| format!("experience stale marker connection failed: {error}"))?;
        mark_projection_stale_with_conn(&conn, &stored.projection_id, "canonical_layer_changed")
            .map_err(|error| format!("experience stale marker failed: {error}"))?;
        return Ok(fallback_report(
            Some(&stored),
            &experience_id,
            &workspace_hash,
            &principal_id,
            stored.observed_at_unix_ms,
            "stale",
            "canonical_layer_changed",
        ));
    }
    let req = ExperienceProjectionRequest {
        schema_version: SCHEMA_VERSION,
        experience_id: stored.experience_id.clone(),
        workspace_hash: stored.workspace_hash.clone(),
        graph_side: stored.graph_side.clone(),
        layer: stored.layer.clone(),
        source_entity_ids: source_entity_ids.clone(),
        source_event_ids: stored.source_event_ids.clone(),
        pulse_ids: stored.pulse_ids.clone(),
        query_time_unix_ms: stored.observed_at_unix_ms,
    };
    if validate_event_refs(db, &req, &principal_id).is_err() {
        let conn = db
            .conn()
            .map_err(|error| format!("experience stale marker connection failed: {error}"))?;
        mark_projection_stale_with_conn(&conn, &stored.projection_id, "accepted_event_unavailable")
            .map_err(|error| format!("experience stale marker failed: {error}"))?;
        return Ok(fallback_report(
            Some(&stored),
            &experience_id,
            &workspace_hash,
            &principal_id,
            stored.observed_at_unix_ms,
            "stale",
            "accepted_event_unavailable",
        ));
    }
    let signals = derive_signals(
        &inputs.entities,
        stored.source_event_ids.len() + stored.pulse_ids.len(),
    );
    let expected_digest = projection_digest(
        &req,
        &stored.projection_id,
        &principal_id,
        &inputs.agent_id,
        &signals,
        &inputs.source_digest,
    );
    if inputs.agent_id != stored.agent_id
        || inputs.source_digest != stored.source_digest
        || expected_digest != stored.projection_digest
    {
        let conn = db
            .conn()
            .map_err(|error| format!("experience stale marker connection failed: {error}"))?;
        mark_projection_stale_with_conn(
            &conn,
            &stored.projection_id,
            "canonical_source_digest_changed",
        )
        .map_err(|error| format!("experience stale marker failed: {error}"))?;
        return Ok(fallback_report(
            Some(&stored),
            &experience_id,
            &workspace_hash,
            &principal_id,
            stored.observed_at_unix_ms,
            "stale",
            "canonical_source_digest_changed",
        ));
    }
    Ok(report_from_stored(
        &stored,
        source_entity_ids,
        inputs.sources,
        "read",
        None,
        None,
    ))
}

fn mark_projection_state_with_conn(
    conn: &Connection,
    projection_id: &str,
    state: &str,
    reason: &str,
) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE experience_projections
         SET state = ?1, state_reason = ?2, updated_at_unix_ms = ?3
         WHERE projection_id = ?4",
        params![state, reason, crate::db::now_ms(), projection_id],
    )
}

fn mark_projection_stale_with_conn(
    conn: &Connection,
    projection_id: &str,
    reason: &str,
) -> rusqlite::Result<usize> {
    mark_projection_state_with_conn(conn, projection_id, "stale", reason)
}

/// Mark projections depending on canonical sources stale. Called by canonical
/// write/update paths after their transaction commits.
pub(crate) fn mark_stale_sources_with_conn(
    conn: &Connection,
    source_entity_ids: &[String],
    reason: &str,
) -> rusqlite::Result<usize> {
    let mut affected = 0usize;
    for source_entity_id in source_entity_ids {
        affected += conn.execute(
            "UPDATE experience_projections
             SET state = 'stale', state_reason = ?1, updated_at_unix_ms = ?2
             WHERE projection_id IN (
               SELECT projection_id FROM experience_projection_sources
               WHERE source_entity_id = ?3
             ) AND state != 'quarantined'",
            params![reason, crate::db::now_ms(), source_entity_id],
        )?;
    }
    Ok(affected)
}

/// Quarantine dependent projections when a source leaves the serveable set
/// through forget/prune/expiry/status transitions.
pub(crate) fn quarantine_sources_with_conn(
    conn: &Connection,
    source_entity_ids: &[String],
    reason: &str,
) -> rusqlite::Result<usize> {
    let mut affected = 0usize;
    for source_entity_id in source_entity_ids {
        affected += conn.execute(
            "UPDATE experience_projections
             SET state = 'quarantined', state_reason = ?1, updated_at_unix_ms = ?2
             WHERE projection_id IN (
               SELECT projection_id FROM experience_projection_sources
               WHERE source_entity_id = ?3
             ) AND state != 'quarantined'",
            params![reason, crate::db::now_ms(), source_entity_id],
        )?;
    }
    Ok(affected)
}

/// Quarantine every projection with an archived canonical source. This is used
/// by bulk archive paths where the affected IDs are selected by SQL.
pub(crate) fn quarantine_archived_sources_with_conn(
    conn: &Connection,
    reason: &str,
) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE experience_projections
         SET state = 'quarantined', state_reason = ?1, updated_at_unix_ms = ?2
         WHERE state != 'quarantined'
           AND EXISTS (
             SELECT 1
             FROM experience_projection_sources s
             JOIN entities e ON e.id = s.source_entity_id
             WHERE s.projection_id = experience_projections.projection_id
               AND e.archived = 1
           )",
        params![reason, crate::db::now_ms()],
    )
}

/// History retention can remove the canonical version a future replay might
/// need. Marking all projections stale is conservative; the next read falls
/// back to canonical retrieval until an explicit rebuild proves the current
/// source/event set again.
pub(crate) fn mark_all_stale_with_conn(conn: &Connection, reason: &str) -> rusqlite::Result<usize> {
    conn.execute(
        "UPDATE experience_projections
         SET state = 'stale', state_reason = ?1, updated_at_unix_ms = ?2
         WHERE state = 'active'",
        params![reason, crate::db::now_ms()],
    )
}

/// Delete only derived projection rows and their ledger/relation rows for
/// erased canonical IDs. Canonical entity history is owned by the caller and
/// is not touched here.
pub(crate) fn delete_sources_with_conn(
    conn: &Connection,
    source_entity_ids: &[String],
) -> rusqlite::Result<usize> {
    let mut deleted = 0usize;
    for source_entity_id in source_entity_ids {
        deleted += conn.execute(
            "DELETE FROM experience_projections
             WHERE projection_id IN (
               SELECT projection_id FROM experience_projection_sources
               WHERE source_entity_id = ?1
             )",
            params![source_entity_id],
        )?;
    }
    Ok(deleted)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::db::TestDatabase;
    use rusqlite::params;

    fn seed_source(db: &mut TestDatabase, id: &str, workspace: &str, agent: &str) -> (Entity, i64) {
        let mut entity = crate::db::tests::make_entity(
            id,
            "experience",
            &format!("source-{id}"),
            r#"{"note":"canonical source"}"#,
        );
        entity.workspace_hash = workspace.to_string();
        entity.agent_id = agent.to_string();
        let anchor = entity.created_at_unix_ms + 1;
        db.remember_skip_dedup(&entity).expect("source persists");
        (entity, anchor)
    }

    fn seed_served_event(
        db: &TestDatabase,
        id: &str,
        entity_id: &str,
        workspace: &str,
        agent: &str,
        anchor: i64,
    ) {
        let conn = db.conn().expect("test connection");
        conn.execute(
            "INSERT INTO served_events
             (id, ts_unix_ms, batch_id, profile, workspace_hash, entity_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                id,
                anchor,
                format!("batch-{id}"),
                agent,
                workspace,
                entity_id
            ],
        )
        .expect("accepted serving event persists");
    }

    fn request(source_id: &str, event_id: &str, anchor: i64) -> ExperienceProjectionRequest {
        ExperienceProjectionRequest {
            schema_version: SCHEMA_VERSION,
            experience_id: "experience-1".to_string(),
            workspace_hash: "workspace-a".to_string(),
            graph_side: "source".to_string(),
            layer: "working".to_string(),
            source_entity_ids: vec![source_id.to_string()],
            source_event_ids: vec![event_id.to_string()],
            pulse_ids: vec![],
            query_time_unix_ms: anchor,
        }
    }

    #[test]
    fn rebuild_is_idempotent_and_read_resolves_canonical_source() {
        let mut db = TestDatabase::new("experience-projection");
        db.disable_auto_embedding();
        let (entity, anchor) =
            seed_source(&mut db, "experience-source-1", "workspace-a", "agent-a");
        seed_served_event(
            &db,
            "served-experience-1",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        let req = request(&entity.id, "served-experience-1", anchor);
        let first = rebuild(&db, &req, "agent-a").expect("first rebuild");
        let second = rebuild(&db, &req, "agent-a").expect("idempotent rebuild");
        assert_eq!(first.projection_id, second.projection_id);
        assert_eq!(first.projection_digest, second.projection_digest);
        assert_eq!(first.projection_revision, 1);
        assert_eq!(first.state, "active");
        assert_eq!(first.resolved_sources.len(), 1);
        assert_eq!(first.resolved_sources[0].id, entity.id);
        let serialized = serde_json::to_string(&first).expect("projection JSON");
        assert!(!serialized.contains("canonical source"));
        assert!(!serialized.contains("verified"));

        let read = read(&db, "experience-1", "workspace-a", "agent-a").expect("projection read");
        assert_eq!(read.state, "active");
        assert_eq!(read.read_mode, "read");
        assert_eq!(read.source_entity_ids, vec![entity.id]);
        assert_eq!(read.evidence_authority, "canonical_source_resolution");
        let conn = db.conn().expect("test connection");
        let event_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM experience_projection_events",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(event_count, 1, "duplicate rebuilds share one ledger event");
    }

    #[test]
    fn read_from_another_workspace_degrades_to_canonical_fallback() {
        let mut db = TestDatabase::new("experience-projection-scope");
        db.disable_auto_embedding();
        let (entity, anchor) =
            seed_source(&mut db, "experience-source-scope", "workspace-a", "agent-a");
        seed_served_event(
            &db,
            "served-experience-scope",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        rebuild(
            &db,
            &request(&entity.id, "served-experience-scope", anchor),
            "agent-a",
        )
        .expect("scoped rebuild");
        let fallback = read(&db, "experience-1", "workspace-b", "agent-a").expect("fallback read");
        assert_eq!(fallback.state, "missing");
        assert_eq!(fallback.read_mode, "canonical_fallback");
        assert_eq!(
            fallback.fallback.as_ref().map(|value| value.mode.as_str()),
            Some("canonical_retrieval")
        );
        assert!(fallback.source_entity_ids.is_empty());
    }

    #[test]
    fn rebuild_rejects_unrelated_event_and_forged_schema_version() {
        let mut db = TestDatabase::new("experience-projection-reject");
        db.disable_auto_embedding();
        let (entity, anchor) = seed_source(
            &mut db,
            "experience-source-reject",
            "workspace-a",
            "agent-a",
        );
        seed_served_event(
            &db,
            "served-other",
            "unrelated",
            "workspace-a",
            "agent-a",
            anchor,
        );
        let mut req = request(&entity.id, "served-other", anchor);
        let err = rebuild(&db, &req, "agent-a").expect_err("unrelated event must fail closed");
        assert!(err.contains("outside the projection scope"));
        req.schema_version = 99;
        let err = rebuild(&db, &req, "agent-a").expect_err("unknown schema must fail closed");
        assert!(err.contains("unsupported experience projection schema_version"));
    }

    #[test]
    fn source_change_is_detected_even_without_a_lifecycle_hook() {
        let mut db = TestDatabase::new("experience-projection-stale");
        db.disable_auto_embedding();
        let (mut entity, anchor) =
            seed_source(&mut db, "experience-source-stale", "workspace-a", "agent-a");
        seed_served_event(
            &db,
            "served-stale",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        let req = request(&entity.id, "served-stale", anchor);
        rebuild(&db, &req, "agent-a").expect("initial rebuild");
        entity.body_json = r#"{"note":"changed canonical source"}"#.to_string();
        db.remember_skip_dedup(&entity).expect("source update");
        let read = read(&db, "experience-1", "workspace-a", "agent-a").expect("stale read");
        assert_eq!(read.state, "stale");
        assert_eq!(read.read_mode, "canonical_fallback");
        assert!(read.resolved_sources.is_empty());
    }

    #[test]
    fn forget_quarantines_projection_without_deleting_canonical_row() {
        let mut db = TestDatabase::new("experience-projection-forget");
        db.disable_auto_embedding();
        let (entity, anchor) = seed_source(
            &mut db,
            "experience-source-forget",
            "workspace-a",
            "agent-a",
        );
        seed_served_event(
            &db,
            "served-forget",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        rebuild(
            &db,
            &request(&entity.id, "served-forget", anchor),
            "agent-a",
        )
        .expect("initial rebuild");
        assert!(db
            .forget(
                "experience",
                "source-experience-source-forget",
                "test forget"
            )
            .expect("forget source"));
        let report = read(&db, "experience-1", "workspace-a", "agent-a")
            .expect("quarantined projection read");
        assert_eq!(report.state, "quarantined");
        assert_eq!(report.read_mode, "canonical_fallback");
        assert_eq!(
            report.fallback.as_ref().map(|value| value.mode.as_str()),
            Some("canonical_retrieval")
        );
        let canonical = db
            .get_entity_by_id_unfiltered(&entity.id)
            .expect("canonical lookup")
            .expect("canonical row retained");
        assert!(
            canonical.archived,
            "forget retains the canonical archived row"
        );
    }

    #[test]
    fn supersession_invalidation_quarantines_projection_and_retains_history() {
        let mut db = TestDatabase::new("experience-projection-supersession");
        db.disable_auto_embedding();
        let (loser, anchor) =
            seed_source(&mut db, "experience-source-loser", "workspace-a", "agent-a");
        let (winner, _) = seed_source(
            &mut db,
            "experience-source-winner",
            "workspace-a",
            "agent-a",
        );
        seed_served_event(
            &db,
            "served-loser",
            &loser.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        rebuild(&db, &request(&loser.id, "served-loser", anchor), "agent-a")
            .expect("initial rebuild");
        assert!(db
            .invalidate_entity(&loser.id, &winner.id)
            .expect("supersede source"));
        let report = read(&db, "experience-1", "workspace-a", "agent-a")
            .expect("quarantined projection read");
        assert_eq!(report.state, "quarantined");
        assert_eq!(report.read_mode, "canonical_fallback");
        let conn = db.conn().expect("test connection");
        let history_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM entity_history WHERE id = ?1",
                params![loser.id],
                |row| row.get(0),
            )
            .expect("history lookup");
        assert_eq!(history_count, 1, "supersession retains canonical history");
    }

    #[test]
    fn decay_archive_quarantines_projection_without_deleting_canonical_row() {
        let mut db = TestDatabase::new("experience-projection-decay");
        db.disable_auto_embedding();
        let (entity, anchor) =
            seed_source(&mut db, "experience-source-decay", "workspace-a", "agent-a");
        seed_served_event(
            &db,
            "served-decay",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        rebuild(&db, &request(&entity.id, "served-decay", anchor), "agent-a")
            .expect("initial rebuild");
        let conn = db.conn().expect("test connection");
        conn.execute(
            "UPDATE entities SET last_accessed_unix_ms = 0, decay_score = 1.0 WHERE id = ?1",
            params![entity.id],
        )
        .expect("age source");
        let decay = db.decay_tick().expect("decay tick");
        assert!(
            decay.auto_archived >= 1,
            "source must cross decay threshold"
        );
        let report = read(&db, "experience-1", "workspace-a", "agent-a")
            .expect("quarantined projection read");
        assert_eq!(report.state, "quarantined");
        assert_eq!(report.read_mode, "canonical_fallback");
        let canonical = db
            .get_entity_by_id_unfiltered(&entity.id)
            .expect("canonical lookup")
            .expect("canonical row retained");
        assert!(
            canonical.archived,
            "decay retains the canonical archived row"
        );
    }

    #[test]
    fn projection_survives_database_reopen_with_stable_digest() {
        let mut db = TestDatabase::new("experience-projection-reopen");
        db.disable_auto_embedding();
        let (entity, anchor) = seed_source(
            &mut db,
            "experience-source-reopen",
            "workspace-a",
            "agent-a",
        );
        seed_served_event(
            &db,
            "served-reopen",
            &entity.id,
            "workspace-a",
            "agent-a",
            anchor,
        );
        let request = request(&entity.id, "served-reopen", anchor);
        let before = rebuild(&db, &request, "agent-a").expect("initial rebuild");
        let path = db.path().to_string();
        drop(db);

        let reopened = crate::db::Database::open(&path).expect("reopen database");
        let after = read(&reopened, "experience-1", "workspace-a", "agent-a")
            .expect("reopened projection read");
        assert_eq!(after.state, "active");
        assert_eq!(after.projection_id, before.projection_id);
        assert_eq!(after.projection_digest, before.projection_digest);
        assert_eq!(after.source_digest, before.source_digest);
        drop(reopened);
        let _ = std::fs::remove_file(path);
    }
}
