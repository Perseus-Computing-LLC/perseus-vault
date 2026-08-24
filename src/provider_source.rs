use crate::db::Database;
use rusqlite::{params, OptionalExtension, Transaction};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

pub const PROVIDER_SOURCE_SCHEMA_VERSION: i64 = 1;
pub const PROVIDER_SOURCE_EVENT_TYPES: [&str; 5] =
    ["upsert", "comment", "reply", "attachment", "delete"];
pub const PROVIDER_SOURCE_STATES: [&str; 2] = ["active", "deleted"];

/// Hash-only provider metadata that can be embedded in an existing Entity
/// body by capture/import paths. Provider bodies never belong in this type.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProviderSourceReference {
    pub source_id: String,
    pub provider: String,
    pub kind: String,
    pub external_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub canonical_uri: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thread_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_event_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author: Option<String>,
    pub revision: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observed_at_unix_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_created_at_unix_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub provider_updated_at_unix_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_span_ref: Option<String>,
    pub workspace_hash: String,
    pub visibility: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retention_policy: Option<String>,
    pub capture_method: String,
    pub authority_agent_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub entity_id: Option<String>,
    pub state: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_at_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProviderSourceEventRequest {
    pub schema_version: i64,
    pub event_type: String,
    pub provider: String,
    pub kind: String,
    pub external_id: String,
    #[serde(default)]
    pub canonical_uri: Option<String>,
    #[serde(default)]
    pub thread_id: Option<String>,
    #[serde(default)]
    pub parent_id: Option<String>,
    #[serde(default)]
    pub provider_event_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub author: Option<String>,
    pub revision: String,
    #[serde(default)]
    pub expected_revision: Option<String>,
    #[serde(default)]
    pub observed_at_unix_ms: Option<i64>,
    #[serde(default)]
    pub provider_created_at_unix_ms: Option<i64>,
    #[serde(default)]
    pub provider_updated_at_unix_ms: Option<i64>,
    #[serde(default)]
    pub content_sha256: Option<String>,
    #[serde(default)]
    pub source_span_ref: Option<String>,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default = "default_source_visibility")]
    pub visibility: String,
    #[serde(default)]
    pub retention_policy: Option<String>,
    #[serde(default = "default_capture_method")]
    pub capture_method: String,
    /// Stamped from MCP initialize.clientInfo.name at the transport boundary.
    #[serde(default)]
    pub requesting_agent_id: String,
    #[serde(default)]
    pub entity_id: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ProviderSourceEventResult {
    pub schema_version: i64,
    pub outcome: String,
    pub event_type: String,
    pub event_id: String,
    pub receipt_digest: String,
    pub recorded_at_unix_ms: i64,
    pub source: ProviderSourceReference,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub previous_revision: Option<String>,
    pub entity_archived: bool,
}

fn default_source_visibility() -> String {
    "workspace".to_string()
}

fn default_capture_method() -> String {
    "event_feed".to_string()
}

impl ProviderSourceEventRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != PROVIDER_SOURCE_SCHEMA_VERSION {
            return Err(format!(
                "unsupported provider-source schema_version {}; expected {}",
                self.schema_version, PROVIDER_SOURCE_SCHEMA_VERSION
            ));
        }
        if !PROVIDER_SOURCE_EVENT_TYPES.contains(&self.event_type.as_str()) {
            return Err(format!(
                "unsupported provider-source event_type {}; expected one of {:?}",
                self.event_type, PROVIDER_SOURCE_EVENT_TYPES
            ));
        }
        for (name, value, max) in [
            ("provider", self.provider.as_str(), 64usize),
            ("kind", self.kind.as_str(), 64),
            ("external_id", self.external_id.as_str(), 1024),
            ("revision", self.revision.as_str(), 256),
            ("capture_method", self.capture_method.as_str(), 64),
            (
                "requesting_agent_id",
                self.requesting_agent_id.as_str(),
                256,
            ),
        ] {
            validate_text(name, value, max, true)?;
        }
        validate_text("workspace_hash", &self.workspace_hash, 256, false)?;
        validate_text_opt("canonical_uri", self.canonical_uri.as_deref(), 2048)?;
        validate_text_opt("thread_id", self.thread_id.as_deref(), 1024)?;
        validate_text_opt("parent_id", self.parent_id.as_deref(), 1024)?;
        validate_text_opt("provider_event_id", self.provider_event_id.as_deref(), 1024)?;
        validate_text_opt("author", self.author.as_deref(), 256)?;
        validate_text_opt("expected_revision", self.expected_revision.as_deref(), 256)?;
        validate_text_opt("source_span_ref", self.source_span_ref.as_deref(), 1024)?;
        if !["private", "workspace", "public"].contains(&self.visibility.as_str()) {
            return Err(format!(
                "invalid provider-source visibility {}",
                self.visibility
            ));
        }
        if !self.capture_method.is_ascii() {
            return Err("capture_method must be ASCII".to_string());
        }
        for (name, value) in [
            ("observed_at_unix_ms", self.observed_at_unix_ms),
            (
                "provider_created_at_unix_ms",
                self.provider_created_at_unix_ms,
            ),
            (
                "provider_updated_at_unix_ms",
                self.provider_updated_at_unix_ms,
            ),
        ] {
            if value.is_some_and(|v| v < 0) {
                return Err(format!("{name} must be non-negative"));
            }
        }
        if let (Some(created), Some(updated)) = (
            self.provider_created_at_unix_ms,
            self.provider_updated_at_unix_ms,
        ) {
            if updated < created {
                return Err(
                    "provider_updated_at_unix_ms precedes provider_created_at_unix_ms".to_string(),
                );
            }
        }
        if self.event_type != "delete" && !is_sha256(self.content_sha256.as_deref()) {
            return Err(
                "content_sha256 is required and must be a lowercase SHA-256 digest for non-delete events"
                    .to_string(),
            );
        }
        if let Some(hash) = self.content_sha256.as_deref() {
            if !is_sha256(Some(hash)) {
                return Err("content_sha256 must be a lowercase SHA-256 digest".to_string());
            }
        }
        if let Some(policy) = self.retention_policy.as_deref() {
            if !crate::models::RETENTION_POLICIES.contains(&policy) {
                return Err(format!("invalid retention_policy {}", policy));
            }
        }
        if (self.event_type == "reply" || self.event_type == "comment")
            && (self.thread_id.as_deref().unwrap_or("").is_empty()
                || self.parent_id.as_deref().unwrap_or("").is_empty())
        {
            return Err("comment/reply events require thread_id and parent_id".to_string());
        }
        Ok(())
    }
}

fn validate_text(name: &str, value: &str, max: usize, required: bool) -> Result<(), String> {
    if required && value.trim().is_empty() {
        return Err(format!("{name} must be non-empty"));
    }
    if value.len() > max {
        return Err(format!("{name} exceeds {max} bytes"));
    }
    if value.chars().any(|c| c.is_control()) {
        return Err(format!("{name} contains a control character"));
    }
    Ok(())
}

fn validate_text_opt(name: &str, value: Option<&str>, max: usize) -> Result<(), String> {
    if let Some(value) = value {
        validate_text(name, value, max, false)?;
    }
    Ok(())
}

fn is_sha256(value: Option<&str>) -> bool {
    value.is_some_and(|v| {
        v.len() == 64
            && v.bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
    })
}

fn digest_json(value: &serde_json::Value) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).unwrap_or_default())
    )
}

fn source_id_for_parts(workspace_hash: &str, provider: &str, external_id: &str) -> String {
    format!(
        "src-{}",
        digest_json(&json!([workspace_hash, provider, external_id]))
    )
}

fn event_id_for(source_id: &str, revision: &str) -> String {
    format!("evt-{}", digest_json(&json!([source_id, revision])))
}

fn request_digest(request: &ProviderSourceEventRequest, source_id: &str) -> String {
    digest_json(&json!({
        "schema_version": request.schema_version,
        "source_id": source_id,
        "request": request,
    }))
}

fn receipt_digest(
    event_id: &str,
    event_type: &str,
    source: &ProviderSourceReference,
    previous_revision: Option<&str>,
    recorded_at_unix_ms: i64,
) -> String {
    digest_json(&json!({
        "schema_version": PROVIDER_SOURCE_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "source": source,
        "previous_revision": previous_revision,
        "recorded_at_unix_ms": recorded_at_unix_ms,
    }))
}

impl ProviderSourceReference {
    pub(crate) fn validate(&self) -> Result<(), String> {
        for (name, value, max) in [
            ("provider", self.provider.as_str(), 64usize),
            ("kind", self.kind.as_str(), 64),
            ("external_id", self.external_id.as_str(), 1024),
            ("revision", self.revision.as_str(), 256),
            ("capture_method", self.capture_method.as_str(), 64),
            ("authority_agent_id", self.authority_agent_id.as_str(), 256),
        ] {
            validate_text(name, value, max, true)?;
        }
        validate_text("source_id", &self.source_id, 80, true)?;
        validate_text("workspace_hash", &self.workspace_hash, 256, false)?;
        validate_text_opt("canonical_uri", self.canonical_uri.as_deref(), 2048)?;
        validate_text_opt("thread_id", self.thread_id.as_deref(), 1024)?;
        validate_text_opt("parent_id", self.parent_id.as_deref(), 1024)?;
        validate_text_opt("provider_event_id", self.provider_event_id.as_deref(), 1024)?;
        validate_text_opt("author", self.author.as_deref(), 256)?;
        validate_text_opt("source_span_ref", self.source_span_ref.as_deref(), 1024)?;
        validate_text_opt("entity_id", self.entity_id.as_deref(), 256)?;
        if !["private", "workspace", "public"].contains(&self.visibility.as_str()) {
            return Err(format!(
                "invalid provider-source visibility {}",
                self.visibility
            ));
        }
        if !PROVIDER_SOURCE_STATES.contains(&self.state.as_str()) {
            return Err(format!("invalid provider-source state {}", self.state));
        }
        if let Some(hash) = self.content_sha256.as_deref() {
            if !is_sha256(Some(hash)) {
                return Err("content_sha256 must be a lowercase SHA-256 digest".to_string());
            }
        }
        if let Some(policy) = self.retention_policy.as_deref() {
            if !crate::models::RETENTION_POLICIES.contains(&policy) {
                return Err(format!("invalid retention_policy {}", policy));
            }
        }
        let expected = source_id_for_parts(&self.workspace_hash, &self.provider, &self.external_id);
        if self.source_id != expected {
            return Err("source_id does not match provider identity".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone)]
struct CurrentSource {
    source: ProviderSourceReference,
    receipt_digest: String,
    recorded_at_unix_ms: i64,
}

#[derive(Debug, Clone)]
struct StoredEvent {
    source: ProviderSourceReference,
    event_id: String,
    event_type: String,
    receipt_digest: String,
    recorded_at_unix_ms: i64,
    previous_revision: Option<String>,
    request_digest: String,
}

fn current_source(
    tx: &Transaction<'_>,
    source_id: &str,
) -> rusqlite::Result<Option<CurrentSource>> {
    tx.query_row(
        "SELECT source_id, provider, kind, external_id, canonical_uri, thread_id,
                parent_id, provider_event_id, author, revision, observed_at_unix_ms,
                provider_created_at_unix_ms, provider_updated_at_unix_ms,
                content_sha256, source_span_ref, workspace_hash, visibility,
                retention_policy, capture_method, authority_agent_id, entity_id,
                state, deleted_at_unix_ms, receipt_digest, recorded_at_unix_ms
         FROM provider_sources WHERE source_id=?1",
        params![source_id],
        |row| {
            Ok(CurrentSource {
                source: ProviderSourceReference {
                    source_id: row.get(0)?,
                    provider: row.get(1)?,
                    kind: row.get(2)?,
                    external_id: row.get(3)?,
                    canonical_uri: row.get(4)?,
                    thread_id: row.get(5)?,
                    parent_id: row.get(6)?,
                    provider_event_id: row.get(7)?,
                    author: row.get(8)?,
                    revision: row.get(9)?,
                    observed_at_unix_ms: row.get(10)?,
                    provider_created_at_unix_ms: row.get(11)?,
                    provider_updated_at_unix_ms: row.get(12)?,
                    content_sha256: row.get(13)?,
                    source_span_ref: row.get(14)?,
                    workspace_hash: row.get(15)?,
                    visibility: row.get(16)?,
                    retention_policy: row.get(17)?,
                    capture_method: row.get(18)?,
                    authority_agent_id: row.get(19)?,
                    entity_id: row.get(20)?,
                    state: row.get(21)?,
                    deleted_at_unix_ms: row.get(22)?,
                },
                receipt_digest: row.get(23)?,
                recorded_at_unix_ms: row.get(24)?,
            })
        },
    )
    .optional()
}

fn stored_event(
    tx: &Transaction<'_>,
    source_id: &str,
    revision: &str,
) -> rusqlite::Result<Option<StoredEvent>> {
    tx.query_row(
        "SELECT source_id, provider, kind, external_id, canonical_uri, thread_id,
                parent_id, provider_event_id, author, revision, observed_at_unix_ms,
                provider_created_at_unix_ms, provider_updated_at_unix_ms,
                content_sha256, source_span_ref, workspace_hash, visibility,
                retention_policy, capture_method, authority_agent_id, state_after,
                deleted_at_unix_ms, event_id, event_type, receipt_digest,
                recorded_at_unix_ms, previous_revision, request_digest, entity_id
         FROM provider_source_events
         WHERE source_id=?1 AND revision=?2",
        params![source_id, revision],
        |row| {
            Ok(StoredEvent {
                source: ProviderSourceReference {
                    source_id: row.get(0)?,
                    provider: row.get(1)?,
                    kind: row.get(2)?,
                    external_id: row.get(3)?,
                    canonical_uri: row.get(4)?,
                    thread_id: row.get(5)?,
                    parent_id: row.get(6)?,
                    provider_event_id: row.get(7)?,
                    author: row.get(8)?,
                    revision: row.get(9)?,
                    observed_at_unix_ms: row.get(10)?,
                    provider_created_at_unix_ms: row.get(11)?,
                    provider_updated_at_unix_ms: row.get(12)?,
                    content_sha256: row.get(13)?,
                    source_span_ref: row.get(14)?,
                    workspace_hash: row.get(15)?,
                    visibility: row.get(16)?,
                    retention_policy: row.get(17)?,
                    capture_method: row.get(18)?,
                    authority_agent_id: row.get(19)?,
                    state: row.get(20)?,
                    deleted_at_unix_ms: row.get(21)?,
                    entity_id: row.get(28)?,
                },
                event_id: row.get(22)?,
                event_type: row.get(23)?,
                receipt_digest: row.get(24)?,
                recorded_at_unix_ms: row.get(25)?,
                previous_revision: row.get(26)?,
                request_digest: row.get(27)?,
            })
        },
    )
    .optional()
}

#[allow(clippy::too_many_arguments)]
fn result_from_source(
    outcome: &str,
    event_type: &str,
    event_id: &str,
    receipt_digest: &str,
    recorded_at_unix_ms: i64,
    source: ProviderSourceReference,
    previous_revision: Option<String>,
    entity_archived: bool,
) -> ProviderSourceEventResult {
    ProviderSourceEventResult {
        schema_version: PROVIDER_SOURCE_SCHEMA_VERSION,
        outcome: outcome.to_string(),
        event_type: event_type.to_string(),
        event_id: event_id.to_string(),
        receipt_digest: receipt_digest.to_string(),
        recorded_at_unix_ms,
        source,
        previous_revision,
        entity_archived,
    }
}

fn validate_entity_binding(
    tx: &Transaction<'_>,
    entity_id: Option<&str>,
    workspace_hash: &str,
    existing_entity_id: Option<&str>,
) -> Result<Option<String>, String> {
    let Some(entity_id) = entity_id else {
        return Ok(existing_entity_id.map(str::to_string));
    };
    if let Some(existing) = existing_entity_id {
        if existing != entity_id {
            return Err("provider source entity binding cannot be rebound".to_string());
        }
    }
    let row: Option<String> = tx
        .query_row(
            "SELECT workspace_hash FROM entities WHERE id=?1",
            params![entity_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| format!("provider source entity lookup failed: {e}"))?;
    let Some(entity_workspace) = row else {
        return Err("provider source entity_id does not identify an entity".to_string());
    };
    if entity_workspace != workspace_hash {
        return Err("provider source entity workspace does not match source workspace".to_string());
    }
    Ok(Some(entity_id.to_string()))
}

fn archive_entity(
    tx: &Transaction<'_>,
    entity_id: Option<&str>,
    workspace_hash: &str,
    source_id: &str,
) -> Result<bool, String> {
    let Some(entity_id) = entity_id else {
        return Ok(false);
    };
    let exists: Option<i64> = tx
        .query_row(
            "SELECT archived FROM entities WHERE id=?1 AND workspace_hash=?2",
            params![entity_id, workspace_hash],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| format!("provider source delete entity lookup failed: {e}"))?;
    if exists.is_none() {
        return Err("provider source delete entity is outside source workspace".to_string());
    }
    tx.execute(
        "UPDATE entities SET archived=1, archive_reason=?1, last_accessed_unix_ms=?2
         WHERE id=?3 AND workspace_hash=?4 AND archived=0",
        params![
            format!("provider_source_deleted:{source_id}"),
            crate::db::now_ms(),
            entity_id,
            workspace_hash
        ],
    )
    .map_err(|e| format!("provider source delete archive failed: {e}"))?;
    tx.execute(
        "DELETE FROM entities_fts WHERE rowid IN
         (SELECT rowid FROM entities WHERE id=?1 AND workspace_hash=?2 AND archived=1)",
        params![entity_id, workspace_hash],
    )
    .map_err(|e| format!("provider source delete FTS cleanup failed: {e}"))?;
    Ok(true)
}

fn apply_provider_source_event(
    db: &Database,
    request: &ProviderSourceEventRequest,
) -> Result<ProviderSourceEventResult, String> {
    request.validate()?;
    let source_id = source_id_for_parts(
        &request.workspace_hash,
        &request.provider,
        &request.external_id,
    );
    let event_id = event_id_for(&source_id, &request.revision);
    let request_digest = request_digest(request, &source_id);
    let conn = db
        .conn()
        .map_err(|e| format!("provider source connection failed: {e}"))?;
    let tx = conn
        .unchecked_transaction()
        .map_err(|e| format!("provider source transaction failed: {e}"))?;
    let current = current_source(&tx, &source_id)
        .map_err(|e| format!("provider source current lookup failed: {e}"))?;
    let existing_entity_id = current
        .as_ref()
        .and_then(|row| row.source.entity_id.clone());

    if let Some(event) = stored_event(&tx, &source_id, &request.revision)
        .map_err(|e| format!("provider source replay lookup failed: {e}"))?
    {
        if event.request_digest != request_digest {
            return Err(
                "provider source revision replay conflicts with the retained event".to_string(),
            );
        }
        let entity_archived = event.source.state == "deleted" && event.source.entity_id.is_some();
        tx.commit()
            .map_err(|e| format!("provider source replay commit failed: {e}"))?;
        return Ok(result_from_source(
            "idempotent",
            &event.event_type,
            &event.event_id,
            &event.receipt_digest,
            event.recorded_at_unix_ms,
            event.source,
            event.previous_revision,
            entity_archived,
        ));
    }

    if let Some(ref current) = current {
        if request.expected_revision.as_deref() != Some(current.source.revision.as_str()) {
            let race_digest = digest_json(&json!({
                "outcome": "revision_race",
                "event_id": event_id,
                "current_receipt": current.receipt_digest,
            }));
            tx.commit()
                .map_err(|e| format!("provider source race commit failed: {e}"))?;
            return Ok(result_from_source(
                "revision_race",
                &request.event_type,
                &event_id,
                &race_digest,
                current.recorded_at_unix_ms,
                current.source.clone(),
                Some(current.source.revision.clone()),
                false,
            ));
        }
    } else if request.expected_revision.is_some() {
        return Err("provider source revision race: expected an existing source".to_string());
    }

    let entity_id = validate_entity_binding(
        &tx,
        request.entity_id.as_deref(),
        &request.workspace_hash,
        existing_entity_id.as_deref(),
    )?;
    let previous_revision = current.as_ref().map(|row| row.source.revision.clone());
    let effective_content_sha256 = request.content_sha256.clone().or_else(|| {
        current
            .as_ref()
            .and_then(|row| row.source.content_sha256.clone())
    });
    let state = if request.event_type == "delete" {
        "deleted"
    } else {
        "active"
    };
    let recorded_at_unix_ms = crate::db::now_ms();
    let deleted_at_unix_ms = (request.event_type == "delete").then_some(recorded_at_unix_ms);
    let source = ProviderSourceReference {
        source_id: source_id.clone(),
        provider: request.provider.clone(),
        kind: request.kind.clone(),
        external_id: request.external_id.clone(),
        canonical_uri: request.canonical_uri.clone(),
        thread_id: request.thread_id.clone(),
        parent_id: request.parent_id.clone(),
        provider_event_id: request.provider_event_id.clone(),
        author: request.author.clone(),
        revision: request.revision.clone(),
        observed_at_unix_ms: request.observed_at_unix_ms,
        provider_created_at_unix_ms: request.provider_created_at_unix_ms,
        provider_updated_at_unix_ms: request.provider_updated_at_unix_ms,
        content_sha256: effective_content_sha256,
        source_span_ref: request.source_span_ref.clone(),
        workspace_hash: request.workspace_hash.clone(),
        visibility: request.visibility.clone(),
        retention_policy: request.retention_policy.clone(),
        capture_method: request.capture_method.clone(),
        authority_agent_id: request.requesting_agent_id.clone(),
        entity_id,
        state: state.to_string(),
        deleted_at_unix_ms,
    };
    source.validate()?;
    let receipt_digest = receipt_digest(
        &event_id,
        &request.event_type,
        &source,
        previous_revision.as_deref(),
        recorded_at_unix_ms,
    );
    let entity_archived = if request.event_type == "delete" {
        archive_entity(
            &tx,
            source.entity_id.as_deref(),
            &request.workspace_hash,
            &source_id,
        )?
    } else {
        false
    };

    if current.is_some() {
        let changed = tx
            .execute(
                "UPDATE provider_sources SET
                     kind=?1, canonical_uri=?2, thread_id=?3, parent_id=?4,
                     provider_event_id=?5, author=?6, revision=?7, observed_at_unix_ms=?8,
                     provider_created_at_unix_ms=?9, provider_updated_at_unix_ms=?10,
                     content_sha256=?11, source_span_ref=?12, visibility=?13,
                     retention_policy=?14, capture_method=?15,
                     authority_agent_id=?16, entity_id=?17, state=?18,
                     deleted_at_unix_ms=?19, current_event_id=?20,
                     receipt_digest=?21, recorded_at_unix_ms=?22,
                     updated_at_unix_ms=?23
                 WHERE source_id=?24 AND revision=?25",
                params![
                    &source.kind,
                    &source.canonical_uri,
                    &source.thread_id,
                    &source.parent_id,
                    &source.provider_event_id,
                    &source.author,
                    &source.revision,
                    source.observed_at_unix_ms,
                    source.provider_created_at_unix_ms,
                    source.provider_updated_at_unix_ms,
                    &source.content_sha256,
                    &source.source_span_ref,
                    &source.visibility,
                    &source.retention_policy,
                    &source.capture_method,
                    &source.authority_agent_id,
                    &source.entity_id,
                    &source.state,
                    source.deleted_at_unix_ms,
                    &event_id,
                    &receipt_digest,
                    recorded_at_unix_ms,
                    recorded_at_unix_ms,
                    &source_id,
                    previous_revision.as_deref().unwrap_or("")
                ],
            )
            .map_err(|e| format!("provider source CAS update failed: {e}"))?;
        if changed != 1 {
            return Err("provider source CAS update lost the current revision".to_string());
        }
    } else {
        tx.execute(
            "INSERT INTO provider_sources
             (source_id, workspace_hash, provider, kind, external_id,
              canonical_uri, thread_id, parent_id, provider_event_id, author, revision,
              observed_at_unix_ms, provider_created_at_unix_ms,
              provider_updated_at_unix_ms, content_sha256, source_span_ref,
              visibility, retention_policy, capture_method, authority_agent_id,
              entity_id, state, deleted_at_unix_ms, current_event_id,
              receipt_digest, recorded_at_unix_ms, updated_at_unix_ms)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                     ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23,
                     ?24, ?25, ?26, ?27)",
            params![
                &source.source_id,
                &source.workspace_hash,
                &source.provider,
                &source.kind,
                &source.external_id,
                &source.canonical_uri,
                &source.thread_id,
                &source.parent_id,
                &source.provider_event_id,
                &source.author,
                &source.revision,
                source.observed_at_unix_ms,
                source.provider_created_at_unix_ms,
                source.provider_updated_at_unix_ms,
                &source.content_sha256,
                &source.source_span_ref,
                &source.visibility,
                &source.retention_policy,
                &source.capture_method,
                &source.authority_agent_id,
                &source.entity_id,
                &source.state,
                source.deleted_at_unix_ms,
                &event_id,
                &receipt_digest,
                recorded_at_unix_ms,
                recorded_at_unix_ms
            ],
        )
        .map_err(|e| format!("provider source insert failed: {e}"))?;
    }

    tx.execute(
        "INSERT INTO provider_source_events
         (event_id, source_id, workspace_hash, provider, kind, external_id,
          canonical_uri, thread_id, parent_id, provider_event_id, author, revision,
          expected_revision, event_type, observed_at_unix_ms,
          provider_created_at_unix_ms, provider_updated_at_unix_ms,
          content_sha256, source_span_ref, visibility, retention_policy,
          capture_method, authority_agent_id, entity_id, state_after,
          deleted_at_unix_ms, previous_revision, request_digest,
          receipt_digest, recorded_at_unix_ms)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13,
                 ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22, ?23,
                 ?24, ?25, ?26, ?27, ?28, ?29, ?30)",
        params![
            &event_id,
            &source.source_id,
            &source.workspace_hash,
            &source.provider,
            &source.kind,
            &source.external_id,
            &source.canonical_uri,
            &source.thread_id,
            &source.parent_id,
            &source.provider_event_id,
            &source.author,
            &source.revision,
            &request.expected_revision,
            &request.event_type,
            source.observed_at_unix_ms,
            source.provider_created_at_unix_ms,
            source.provider_updated_at_unix_ms,
            &source.content_sha256,
            &source.source_span_ref,
            &source.visibility,
            &source.retention_policy,
            &source.capture_method,
            &source.authority_agent_id,
            &source.entity_id,
            &source.state,
            source.deleted_at_unix_ms,
            &previous_revision,
            &request_digest,
            &receipt_digest,
            recorded_at_unix_ms
        ],
    )
    .map_err(|e| format!("provider source event insert failed: {e}"))?;
    tx.commit()
        .map_err(|e| format!("provider source commit failed: {e}"))?;
    Ok(result_from_source(
        if request.event_type == "delete" {
            "deleted"
        } else {
            "applied"
        },
        &request.event_type,
        &event_id,
        &receipt_digest,
        recorded_at_unix_ms,
        source,
        previous_revision,
        entity_archived,
    ))
}

fn sanitized_source_projection(source: &ProviderSourceReference) -> serde_json::Value {
    json!({
        "source_id": &source.source_id,
        "provider": &source.provider,
        "kind": &source.kind,
        "external_id": &source.external_id,
        "canonical_uri": source.canonical_uri.as_ref(),
        "thread_id": source.thread_id.as_ref(),
        "parent_id": source.parent_id.as_ref(),
        "provider_event_id": source.provider_event_id.as_ref(),
        "author": source.author.as_ref(),
        "revision": &source.revision,
        "observed_at_unix_ms": source.observed_at_unix_ms,
        "provider_created_at_unix_ms": source.provider_created_at_unix_ms,
        "provider_updated_at_unix_ms": source.provider_updated_at_unix_ms,
        "content_sha256": source.content_sha256.as_ref(),
        "source_span_ref": source.source_span_ref.as_ref(),
        "workspace_hash": &source.workspace_hash,
        "visibility": &source.visibility,
        "retention_policy": source.retention_policy.as_ref(),
        "capture_method": &source.capture_method,
        "authority_agent_id": &source.authority_agent_id,
        "entity_id": source.entity_id.as_ref(),
        "state": &source.state,
        "deleted_at_unix_ms": source.deleted_at_unix_ms,
    })
}

impl Database {
    /// Return the current active provider identity bound to an entity.
    /// Provider bodies and event receipts are excluded from this projection.
    pub(crate) fn provider_source_projection(
        &self,
        entity_id: &str,
        workspace_hash: Option<&str>,
        requesting_agent_id: Option<&str>,
    ) -> Result<Option<serde_json::Value>, Box<dyn std::error::Error>> {
        if entity_id.trim().is_empty() {
            return Ok(None);
        }
        let conn = self.conn()?;
        let source: Option<ProviderSourceReference> = conn
            .query_row(
                "SELECT source_id, provider, kind, external_id, canonical_uri,
                        thread_id, parent_id, provider_event_id, author, revision,
                        observed_at_unix_ms, provider_created_at_unix_ms,
                        provider_updated_at_unix_ms, content_sha256, source_span_ref,
                        workspace_hash, visibility, retention_policy, capture_method,
                        authority_agent_id, entity_id, state, deleted_at_unix_ms
                 FROM provider_sources
                 WHERE entity_id=?1 AND state=?2
                   AND (?3 IS NULL OR workspace_hash=?3)
                   AND EXISTS (SELECT 1 FROM entities e
                               WHERE e.id=?1 AND e.archived=0)
                 ORDER BY updated_at_unix_ms DESC LIMIT 1",
                params![entity_id, "active", workspace_hash],
                |row| {
                    Ok(ProviderSourceReference {
                        source_id: row.get(0)?,
                        provider: row.get(1)?,
                        kind: row.get(2)?,
                        external_id: row.get(3)?,
                        canonical_uri: row.get(4)?,
                        thread_id: row.get(5)?,
                        parent_id: row.get(6)?,
                        provider_event_id: row.get(7)?,
                        author: row.get(8)?,
                        revision: row.get(9)?,
                        observed_at_unix_ms: row.get(10)?,
                        provider_created_at_unix_ms: row.get(11)?,
                        provider_updated_at_unix_ms: row.get(12)?,
                        content_sha256: row.get(13)?,
                        source_span_ref: row.get(14)?,
                        workspace_hash: row.get(15)?,
                        visibility: row.get(16)?,
                        retention_policy: row.get(17)?,
                        capture_method: row.get(18)?,
                        authority_agent_id: row.get(19)?,
                        entity_id: row.get(20)?,
                        state: row.get(21)?,
                        deleted_at_unix_ms: row.get(22)?,
                    })
                },
            )
            .optional()?;
        let Some(source) = source else {
            return Ok(None);
        };
        if !self.requester_can_read(
            requesting_agent_id,
            &source.visibility,
            &source.authority_agent_id,
        ) {
            return Ok(None);
        }
        source
            .validate()
            .map_err(|e| format!("invalid retained provider source: {e}"))?;
        Ok(Some(sanitized_source_projection(&source)))
    }

    pub fn apply_provider_source_event(
        &self,
        request: &ProviderSourceEventRequest,
    ) -> Result<ProviderSourceEventResult, String> {
        apply_provider_source_event(self, request)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request(event_type: &str, external_id: &str, revision: &str) -> ProviderSourceEventRequest {
        ProviderSourceEventRequest {
            schema_version: PROVIDER_SOURCE_SCHEMA_VERSION,
            event_type: event_type.to_string(),
            provider: "slack".to_string(),
            kind: "message".to_string(),
            external_id: external_id.to_string(),
            canonical_uri: Some("slack://C123/42".to_string()),
            thread_id: Some("thread-root".to_string()),
            parent_id: Some("parent-root".to_string()),
            provider_event_id: Some(format!("evt-{external_id}-{revision}")),
            author: Some("source-author".to_string()),
            revision: revision.to_string(),
            expected_revision: None,
            observed_at_unix_ms: Some(1_000),
            provider_created_at_unix_ms: Some(900),
            provider_updated_at_unix_ms: Some(1_000),
            content_sha256: Some("a".repeat(64)),
            source_span_ref: Some("artifact:aaaa/span:0-10".to_string()),
            workspace_hash: "workspace-a".to_string(),
            visibility: "workspace".to_string(),
            retention_policy: Some("archive_when_superseded".to_string()),
            capture_method: "event_feed".to_string(),
            requesting_agent_id: "agent-a".to_string(),
            entity_id: None,
        }
    }

    #[test]
    fn replay_of_same_provider_revision_is_idempotent() {
        let db = crate::db::TestDatabase::new("provider-source-replay");
        let first = db
            .apply_provider_source_event(&request("upsert", "ticket-1", "r1"))
            .expect("first source event");
        let second = db
            .apply_provider_source_event(&request("upsert", "ticket-1", "r1"))
            .expect("replayed source event");
        assert_eq!(first.outcome, "applied");
        assert_eq!(second.outcome, "idempotent");
        assert_eq!(first.event_id, second.event_id);
        assert_eq!(first.receipt_digest, second.receipt_digest);
    }

    #[test]
    fn revision_race_fails_closed_without_overwriting_current_source() {
        let db = crate::db::TestDatabase::new("provider-source-race");
        db.apply_provider_source_event(&request("upsert", "ticket-1", "r1"))
            .expect("seed source");
        let mut raced = request("upsert", "ticket-1", "r2");
        let race = db
            .apply_provider_source_event(&raced)
            .expect("race is a bounded outcome");
        assert_eq!(race.outcome, "revision_race");
        assert_eq!(race.source.revision, "r1");
        raced.expected_revision = Some("r1".to_string());
        let applied = db
            .apply_provider_source_event(&raced)
            .expect("compare-and-swap update");
        assert_eq!(applied.outcome, "applied");
        assert_eq!(applied.source.revision, "r2");
    }

    #[test]
    fn replies_are_first_class_sources_with_parent_thread_lineage() {
        let db = crate::db::TestDatabase::new("provider-source-reply");
        let mut root = request("upsert", "root-1", "r1");
        root.thread_id = None;
        root.parent_id = None;
        root.author = Some("root-author".to_string());
        let root_result = db.apply_provider_source_event(&root).expect("root source");
        let mut reply_request = request("reply", "reply-1", "r1");
        reply_request.author = Some("reply-author".to_string());
        let reply = db
            .apply_provider_source_event(&reply_request)
            .expect("reply source");
        assert_ne!(root_result.source.source_id, reply.source.source_id);
        assert_eq!(reply.source.thread_id.as_deref(), Some("thread-root"));
        assert_eq!(reply.source.parent_id.as_deref(), Some("parent-root"));
        assert_eq!(reply.source.author.as_deref(), Some("reply-author"));
        let mut attachment = request("attachment", "attachment-1", "r1");
        attachment.kind = "attachment".to_string();
        let attachment_result = db
            .apply_provider_source_event(&attachment)
            .expect("attachment source");
        assert_ne!(reply.source.source_id, attachment_result.source.source_id);
        assert_eq!(
            attachment_result.source.author.as_deref(),
            Some("source-author")
        );
    }

    #[test]
    fn deletion_archives_bound_entity_and_retains_tombstone_receipt() {
        let db = crate::db::TestDatabase::new("provider-source-delete");
        let mut entity = crate::db::tests::make_entity(
            "provider-entity-1",
            "provider",
            "slack-root",
            r#"{"content":"source-backed memory"}"#,
        );
        entity.workspace_hash = "workspace-a".to_string();
        entity.agent_id = "agent-a".to_string();
        db.remember_skip_dedup(&entity).expect("seed entity");
        let mut upsert = request("upsert", "root-1", "r1");
        upsert.entity_id = Some(entity.id.clone());
        db.apply_provider_source_event(&upsert)
            .expect("source bind");
        let mut delete = request("delete", "root-1", "r2");
        delete.expected_revision = Some("r1".to_string());
        delete.content_sha256 = None;
        delete.entity_id = Some(entity.id.clone());
        let result = db
            .apply_provider_source_event(&delete)
            .expect("source delete");
        assert_eq!(result.outcome, "deleted");
        assert!(result.entity_archived);
        let stored = db
            .get_entity_by_id_unfiltered(&entity.id)
            .expect("read archived entity")
            .expect("entity retained as tombstone");
        assert!(stored.archived);
        let conn = db.conn().expect("connection");
        let events: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM provider_source_events WHERE source_id=?1",
                rusqlite::params![result.source.source_id],
                |row| row.get(0),
            )
            .expect("event history");
        assert_eq!(events, 2);
    }

    #[test]
    fn sanitized_source_projection_is_scope_gated_and_allowlisted() {
        let db = crate::db::TestDatabase::new("provider-source-projection");
        let mut entity = crate::db::tests::make_entity(
            "provider-entity-projection",
            "provider",
            "projection-note",
            r#"{"content":"source-backed memory"}"#,
        );
        entity.workspace_hash = "workspace-a".to_string();
        entity.agent_id = "agent-a".to_string();
        db.remember_skip_dedup(&entity).expect("seed entity");
        let mut source = request("reply", "reply-projection", "r1");
        source.visibility = "private".to_string();
        source.entity_id = Some(entity.id.clone());
        db.apply_provider_source_event(&source)
            .expect("source event");

        assert!(db
            .provider_source_projection(&entity.id, Some("workspace-a"), None)
            .expect("anonymous projection")
            .is_none());
        let projection = db
            .provider_source_projection(&entity.id, Some("workspace-a"), Some("agent-a"))
            .expect("owner projection")
            .expect("visible source projection");
        assert_eq!(projection["provider"], "slack");
        assert_eq!(projection["external_id"], "reply-projection");
        assert_eq!(projection["thread_id"], "thread-root");
        assert_eq!(projection["parent_id"], "parent-root");
        assert_eq!(projection["author"], "source-author");
        assert!(projection.get("raw_provider_body").is_none());
        assert!(projection.get("body").is_none());
        assert!(projection.get("payload").is_none());
    }

    #[test]
    fn context_opt_in_renders_hash_only_provider_lineage() {
        let db = crate::db::TestDatabase::new("provider-source-context");
        let mut entity = crate::db::tests::make_entity(
            "provider-entity-context",
            "provider",
            "context-note",
            r#"{"content":"source-backed memory"}"#,
        );
        entity.workspace_hash = "workspace-a".to_string();
        entity.agent_id = "agent-a".to_string();
        db.remember_skip_dedup(&entity).expect("seed entity");
        let mut source = request("comment", "comment-context", "r1");
        source.entity_id = Some(entity.id.clone());
        db.apply_provider_source_event(&source)
            .expect("source event");
        let block = db
            .context_block(&crate::models::ContextOptions {
                limit: 10,
                mode: crate::models::ContextMode::AlwaysInject,
                workspace_hash: Some("workspace-a".to_string()),
                requesting_agent_id: Some("agent-a".to_string()),
                include_provider_source: true,
                ..Default::default()
            })
            .expect("context block");
        assert!(block.markdown.contains("provider_source"));
        assert!(block.markdown.contains("comment-context"));
        assert!(block.markdown.contains("source-author"));
        assert!(!block.markdown.contains("raw_provider_body"));
    }

    #[test]
    fn provider_collision_and_scope_mismatch_fail_closed() {
        let db = crate::db::TestDatabase::new("provider-source-collision-scope");
        let slack = request("upsert", "shared-id", "r1");
        let mut jira = request("upsert", "shared-id", "r1");
        jira.provider = "jira".to_string();
        jira.kind = "ticket".to_string();
        jira.thread_id = None;
        jira.parent_id = None;
        let slack_result = db
            .apply_provider_source_event(&slack)
            .expect("slack source");
        let jira_result = db.apply_provider_source_event(&jira).expect("jira source");
        assert_ne!(slack_result.source.source_id, jira_result.source.source_id);

        let mut entity = crate::db::tests::make_entity(
            "provider-entity-scope-mismatch",
            "provider",
            "scope-mismatch",
            r#"{"content":"scoped source"}"#,
        );
        entity.workspace_hash = "workspace-b".to_string();
        entity.agent_id = "agent-b".to_string();
        db.remember_skip_dedup(&entity).expect("seed scoped entity");
        let mut mismatched = request("upsert", "scope-mismatch", "r1");
        mismatched.entity_id = Some(entity.id);
        let error = db
            .apply_provider_source_event(&mismatched)
            .expect_err("cross-workspace source binding");
        assert!(error.contains("workspace"), "{error}");
    }

    #[test]
    fn tool_handler_round_trips_hash_only_event_envelope() {
        let db = crate::db::TestDatabase::new("provider-source-tool-handler");
        let mut request = request("upsert", "handler-1", "r1");
        request.workspace_hash.clear();
        let output = crate::tools::handle_provider_source_event(
            &db,
            serde_json::to_value(&request).expect("request JSON"),
        )
        .expect("provider source handler");
        let value: serde_json::Value = serde_json::from_str(&output).expect("result JSON");
        assert_eq!(value["outcome"], "applied");
        assert_eq!(value["source"]["author"], "source-author");
        assert!(value.get("raw_provider_body").is_none());
        assert!(value["source"].get("payload").is_none());
    }

    #[test]
    fn malformed_ids_and_unknown_fields_fail_closed() {
        let mut invalid = request("upsert", "", "r1");
        assert!(invalid.validate().is_err());
        invalid = request("upsert", "source-1", "r1");
        invalid.content_sha256 = Some("A".repeat(64));
        assert!(invalid.validate().is_err());
        invalid = request("bogus", "source-1", "r1");
        assert!(invalid.validate().is_err());
        let unknown = serde_json::json!({
            "schema_version": 1,
            "event_type": "upsert",
            "provider": "slack",
            "kind": "message",
            "external_id": "source-1",
            "revision": "r1",
            "content_sha256": "a".repeat(64),
            "requesting_agent_id": "agent-a",
            "raw_provider_body": "must-not-cross-boundary"
        });
        assert!(serde_json::from_value::<ProviderSourceEventRequest>(unknown).is_err());
    }
}
