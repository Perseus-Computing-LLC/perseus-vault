use rusqlite::{params, OptionalExtension, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const DECLARED_GRAPH_SCHEMA_VERSION: i64 = 1;
const DECLARED_GRAPH_MAX_NODES: usize = 256;
const DECLARED_GRAPH_MAX_EDGES: usize = 512;
const DECLARED_GRAPH_MAX_SOURCE_KEY: usize = 256;
const DECLARED_GRAPH_MAX_REVISION: usize = 256;
const DECLARED_GRAPH_MAX_PREDICATE: usize = 128;
const DECLARED_GRAPH_MAX_CONTEXT: usize = 1024;
const DECLARED_GRAPH_MAX_SPAN_REF: usize = 1024;
const DECLARED_GRAPH_MAX_EXTERNAL_REF: usize = 2048;
const DECLARED_GRAPH_MAX_QUERY_LIMIT: i64 = 500;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeclaredGraphNodeInput {
    pub namespace: String,
    pub canonical_id: String,
    pub node_type: String,
    #[serde(default)]
    pub external_ref: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeclaredGraphEdgeInput {
    pub from: String,
    pub to: String,
    pub predicate: String,
    pub direction: String,
    #[serde(default)]
    pub context: Option<String>,
    #[serde(default)]
    pub source_span_ref: Option<String>,
    pub origin: String,
    pub support_state: String,
    #[serde(default)]
    pub valid_from_unix_ms: Option<i64>,
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeclaredGraphManifestRequest {
    pub schema_version: i64,
    pub operation: String,
    pub source_key: String,
    pub revision: String,
    pub content_sha256: String,
    #[serde(default)]
    pub source_span_ref: Option<String>,
    pub workspace_hash: String,
    #[serde(default)]
    pub valid_from_unix_ms: Option<i64>,
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
    pub policy: String,
    #[serde(default)]
    pub nodes: Vec<DeclaredGraphNodeInput>,
    #[serde(default)]
    pub edges: Vec<DeclaredGraphEdgeInput>,
    pub requesting_agent_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeclaredGraphAttestationRequest {
    pub schema_version: i64,
    pub workspace_hash: String,
    pub source_key: String,
    pub revision: String,
    pub edge_ids: Vec<String>,
    pub attestation_ref: String,
    pub attested_by: String,
    pub requesting_agent_id: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DeclaredGraphAttestationResult {
    pub schema_version: i64,
    pub outcome: String,
    pub manifest_id: String,
    pub edge_ids: Vec<String>,
    pub receipt_digest: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DeclaredGraphNodeView {
    pub node_id: String,
    pub namespace: String,
    pub canonical_id: String,
    pub node_type: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub external_ref: Option<String>,
    pub workspace_hash: String,
    pub state: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DeclaredGraphEdgeView {
    pub edge_id: String,
    pub manifest_id: String,
    pub source_id: String,
    pub source_key: String,
    pub source_revision: String,
    pub source_sha256: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub manifest_span_ref: Option<String>,
    pub from_node_id: String,
    pub to_node_id: String,
    pub from: String,
    pub to: String,
    pub predicate: String,
    pub direction: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_span_ref: Option<String>,
    pub workspace_hash: String,
    pub origin: String,
    pub attestation_state: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attested_by: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub attestation_ref: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub valid_from_unix_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub valid_to_unix_ms: Option<i64>,
    pub state: String,
    pub recorded_at_unix_ms: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DeclaredGraphManifestResult {
    pub schema_version: i64,
    pub outcome: String,
    pub manifest_id: String,
    pub source_id: String,
    pub node_ids: Vec<String>,
    pub edge_ids: Vec<String>,
    pub edges: Vec<DeclaredGraphEdgeView>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct DeclaredGraphQuery {
    pub workspace_hash: String,
    #[serde(default)]
    pub source_key: Option<String>,
    pub requesting_agent_id: String,
    #[serde(default)]
    pub include_history: bool,
    #[serde(default = "declared_graph_default_limit")]
    pub limit: i64,
}

fn declared_graph_default_limit() -> i64 {
    100
}

#[derive(Debug, Clone, Serialize)]
pub struct DeclaredGraphProjection {
    pub schema_version: i64,
    pub workspace_hash: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_key: Option<String>,
    pub nodes: Vec<DeclaredGraphNodeView>,
    pub edges: Vec<DeclaredGraphEdgeView>,
    pub truncated: bool,
}

fn declared_graph_validate_text(
    name: &str,
    value: &str,
    max_bytes: usize,
    required: bool,
) -> Result<(), String> {
    if required && value.trim().is_empty() {
        return Err(format!("declared graph {name} must be non-empty"));
    }
    if value.len() > max_bytes {
        return Err(format!("declared graph {name} exceeds {max_bytes} bytes"));
    }
    if value.chars().any(|c| c.is_control()) {
        return Err(format!(
            "declared graph {name} contains a control character"
        ));
    }
    Ok(())
}

fn declared_graph_sha256(value: Option<&str>) -> bool {
    value.is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    })
}

fn declared_graph_digest<T: Serialize>(value: &T) -> String {
    format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(value).unwrap_or_default())
    )
}

fn declared_graph_node_key(namespace: &str, canonical_id: &str) -> String {
    format!("{namespace}:{canonical_id}")
}

fn declared_graph_split_node_ref(value: &str) -> Result<(&str, &str), String> {
    let Some((namespace, canonical_id)) = value.split_once(":") else {
        return Err(format!(
            "declared graph node reference {value} must be namespace:canonical_id"
        ));
    };
    if namespace.is_empty() || canonical_id.is_empty() || canonical_id.contains(":") {
        return Err(format!(
            "declared graph node reference {value} is malformed"
        ));
    }
    Ok((namespace, canonical_id))
}

impl DeclaredGraphAttestationRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != DECLARED_GRAPH_SCHEMA_VERSION {
            return Err(format!(
                "unsupported declared graph attestation schema_version {}; expected {}",
                self.schema_version, DECLARED_GRAPH_SCHEMA_VERSION
            ));
        }
        declared_graph_validate_text(
            "attestation source_key",
            &self.source_key,
            DECLARED_GRAPH_MAX_SOURCE_KEY,
            true,
        )?;
        declared_graph_validate_text(
            "attestation revision",
            &self.revision,
            DECLARED_GRAPH_MAX_REVISION,
            true,
        )?;
        declared_graph_validate_text(
            "attestation workspace_hash",
            &self.workspace_hash,
            256,
            true,
        )?;
        declared_graph_validate_text(
            "attestation requesting_agent_id",
            &self.requesting_agent_id,
            256,
            true,
        )?;
        declared_graph_validate_text(
            "attestation_ref",
            &self.attestation_ref,
            DECLARED_GRAPH_MAX_SPAN_REF,
            true,
        )?;
        declared_graph_validate_text("attested_by", &self.attested_by, 256, true)?;
        if self.edge_ids.is_empty() || self.edge_ids.len() > DECLARED_GRAPH_MAX_EDGES {
            return Err(format!("declared graph attestation edge_ids must contain 1..={DECLARED_GRAPH_MAX_EDGES} entries"));
        }
        let mut ids = BTreeSet::new();
        for edge_id in &self.edge_ids {
            declared_graph_validate_text("attestation edge_id", edge_id, 128, true)?;
            if !ids.insert(edge_id) {
                return Err(format!(
                    "declared graph attestation repeats edge_id {edge_id}"
                ));
            }
        }
        Ok(())
    }
}

impl DeclaredGraphManifestRequest {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != DECLARED_GRAPH_SCHEMA_VERSION {
            return Err(format!(
                "unsupported declared graph schema_version {}; expected {}",
                self.schema_version, DECLARED_GRAPH_SCHEMA_VERSION
            ));
        }
        if !["upsert", "delete"].contains(&self.operation.as_str()) {
            return Err(format!(
                "unsupported declared graph operation {}",
                self.operation
            ));
        }
        if self.policy != "replace" {
            return Err(format!("unsupported declared graph policy {}", self.policy));
        }
        declared_graph_validate_text(
            "source_key",
            &self.source_key,
            DECLARED_GRAPH_MAX_SOURCE_KEY,
            true,
        )?;
        declared_graph_validate_text(
            "revision",
            &self.revision,
            DECLARED_GRAPH_MAX_REVISION,
            true,
        )?;
        declared_graph_validate_text("workspace_hash", &self.workspace_hash, 256, true)?;
        declared_graph_validate_text("requesting_agent_id", &self.requesting_agent_id, 256, true)?;
        if !declared_graph_sha256(Some(&self.content_sha256)) {
            return Err(
                "declared graph content_sha256 must be a lowercase SHA-256 digest".to_string(),
            );
        }
        if let Some(span) = self.source_span_ref.as_deref() {
            declared_graph_validate_text(
                "source_span_ref",
                span,
                DECLARED_GRAPH_MAX_SPAN_REF,
                false,
            )?;
        }
        if let (Some(from), Some(to)) = (self.valid_from_unix_ms, self.valid_to_unix_ms) {
            if from > to {
                return Err("declared graph manifest valid_from follows valid_to".to_string());
            }
        }
        if self.operation == "delete" {
            if !self.nodes.is_empty() || !self.edges.is_empty() {
                return Err(
                    "declared graph delete manifests must not carry nodes or edges".to_string(),
                );
            }
            return Ok(());
        }
        if self.nodes.is_empty() || self.nodes.len() > DECLARED_GRAPH_MAX_NODES {
            return Err(format!(
                "declared graph upsert nodes must contain 1..={DECLARED_GRAPH_MAX_NODES} entries"
            ));
        }
        if self.edges.is_empty() || self.edges.len() > DECLARED_GRAPH_MAX_EDGES {
            return Err(format!(
                "declared graph upsert edges must contain 1..={DECLARED_GRAPH_MAX_EDGES} entries"
            ));
        }
        let mut node_keys = BTreeSet::new();
        for node in &self.nodes {
            declared_graph_validate_text("node namespace", &node.namespace, 128, true)?;
            declared_graph_validate_text("node canonical_id", &node.canonical_id, 512, true)?;
            declared_graph_validate_text("node node_type", &node.node_type, 128, true)?;
            if node.namespace.contains(":") || node.canonical_id.contains(":") {
                return Err(
                    "declared graph node namespace and canonical_id may not contain colon"
                        .to_string(),
                );
            }
            if let Some(external_ref) = node.external_ref.as_deref() {
                declared_graph_validate_text(
                    "node external_ref",
                    external_ref,
                    DECLARED_GRAPH_MAX_EXTERNAL_REF,
                    false,
                )?;
            }
            let key = declared_graph_node_key(&node.namespace, &node.canonical_id);
            if !node_keys.insert(key.clone()) {
                return Err(format!("declared graph duplicate node identity {key}"));
            }
        }
        let mut edge_keys = BTreeSet::new();
        for edge in &self.edges {
            let from = declared_graph_split_node_ref(&edge.from)?;
            let to = declared_graph_split_node_ref(&edge.to)?;
            let from_key = declared_graph_node_key(from.0, from.1);
            let to_key = declared_graph_node_key(to.0, to.1);
            if !node_keys.contains(&from_key) || !node_keys.contains(&to_key) {
                return Err(format!(
                    "declared graph edge references undeclared node {} -> {}",
                    edge.from, edge.to
                ));
            }
            declared_graph_validate_text(
                "edge predicate",
                &edge.predicate,
                DECLARED_GRAPH_MAX_PREDICATE,
                true,
            )?;
            declared_graph_validate_text("edge direction", &edge.direction, 32, true)?;
            if !["forward", "reverse"].contains(&edge.direction.as_str()) {
                return Err(format!(
                    "declared graph edge direction {} is invalid",
                    edge.direction
                ));
            }
            if let Some(context) = edge.context.as_deref() {
                declared_graph_validate_text(
                    "edge context",
                    context,
                    DECLARED_GRAPH_MAX_CONTEXT,
                    false,
                )?;
            }
            if let Some(span) = edge.source_span_ref.as_deref() {
                declared_graph_validate_text(
                    "edge source_span_ref",
                    span,
                    DECLARED_GRAPH_MAX_SPAN_REF,
                    false,
                )?;
            }
            if edge.origin != "declared" {
                return Err(format!(
                    "declared graph manifest cannot write origin {}; expected declared",
                    edge.origin
                ));
            }
            if !["sourced", "supported"].contains(&edge.support_state.as_str()) {
                return Err(format!(
                    "declared graph support_state {} requires a separate explicit attestation path",
                    edge.support_state
                ));
            }
            if let (Some(from), Some(to)) = (edge.valid_from_unix_ms, edge.valid_to_unix_ms) {
                if from > to {
                    return Err(format!(
                        "declared graph edge {} -> {} valid_from follows valid_to",
                        edge.from, edge.to
                    ));
                }
            }
            let edge_key = format!(
                "{}|{}|{}|{}",
                edge.from, edge.predicate, edge.to, edge.direction
            );
            if !edge_keys.insert(edge_key.clone()) {
                return Err(format!(
                    "declared graph duplicate relation identity {edge_key}"
                ));
            }
        }
        Ok(())
    }
}

fn declared_graph_request_digest(request: &DeclaredGraphManifestRequest) -> String {
    declared_graph_digest(&json!({
        "schema_version": request.schema_version,
        "operation": request.operation,
        "source_key": request.source_key,
        "revision": request.revision,
        "content_sha256": request.content_sha256,
        "source_span_ref": request.source_span_ref,
        "workspace_hash": request.workspace_hash,
        "valid_from_unix_ms": request.valid_from_unix_ms,
        "valid_to_unix_ms": request.valid_to_unix_ms,
        "policy": request.policy,
        "nodes": request.nodes,
        "edges": request.edges,
    }))
}

fn declared_graph_source_id(workspace_hash: &str, source_key: &str) -> String {
    format!(
        "dgs-{}",
        declared_graph_digest(&json!([workspace_hash, source_key]))
    )
}

fn declared_graph_manifest_id(workspace_hash: &str, source_key: &str, revision: &str) -> String {
    format!(
        "dgm-{}",
        declared_graph_digest(&json!([workspace_hash, source_key, revision]))
    )
}

fn declared_graph_node_id(workspace_hash: &str, namespace: &str, canonical_id: &str) -> String {
    format!(
        "dgn-{}",
        declared_graph_digest(&json!([workspace_hash, namespace, canonical_id]))
    )
}

fn declared_graph_edge_id(
    manifest_id: &str,
    from_node_id: &str,
    predicate: &str,
    to_node_id: &str,
    direction: &str,
) -> String {
    format!(
        "dge-{}",
        declared_graph_digest(&json!([
            manifest_id,
            from_node_id,
            predicate,
            to_node_id,
            direction
        ]))
    )
}

const DECLARED_GRAPH_EDGE_SELECT: &str = "SELECT e.edge_id, e.manifest_id, m.source_id, m.source_key, m.revision, m.content_sha256, m.source_span_ref, e.from_node_id, e.to_node_id, fn.namespace || char(58) || fn.canonical_id, tn.namespace || char(58) || tn.canonical_id, e.predicate, e.direction, e.context, e.source_span_ref, e.workspace_hash, e.origin, e.support_state, e.attested_by, e.attestation_ref, e.valid_from_unix_ms, e.valid_to_unix_ms, e.state, e.recorded_at_unix_ms FROM declared_graph_edges e JOIN declared_graph_manifests m ON m.manifest_id = e.manifest_id JOIN declared_graph_nodes fn ON fn.node_id = e.from_node_id JOIN declared_graph_nodes tn ON tn.node_id = e.to_node_id ";

fn declared_graph_manifest_result(
    db: &crate::db::Database,
    manifest_id: &str,
    outcome: &str,
) -> Result<DeclaredGraphManifestResult, String> {
    let conn = db
        .conn()
        .map_err(|e| format!("declared graph result connection failed: {e}"))?;
    let (source_id, node_ids_json): (String, String) = conn
        .query_row(
            "SELECT source_id, node_ids_json FROM declared_graph_manifests WHERE manifest_id = ?1",
            params![manifest_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(|e| format!("declared graph result manifest lookup failed: {e}"))?;
    let mut stmt = conn
        .prepare(&format!(
            "{DECLARED_GRAPH_EDGE_SELECT} WHERE e.manifest_id = ?1 ORDER BY e.edge_id ASC"
        ))
        .map_err(|e| format!("declared graph result edge prepare failed: {e}"))?;
    let edges = stmt
        .query_map(params![manifest_id], |row| {
            Ok(DeclaredGraphEdgeView {
                edge_id: row.get(0)?,
                manifest_id: row.get(1)?,
                source_id: row.get(2)?,
                source_key: row.get(3)?,
                source_revision: row.get(4)?,
                source_sha256: row.get(5)?,
                manifest_span_ref: row.get(6)?,
                from_node_id: row.get(7)?,
                to_node_id: row.get(8)?,
                from: row.get(9)?,
                to: row.get(10)?,
                predicate: row.get(11)?,
                direction: row.get(12)?,
                context: row.get(13)?,
                source_span_ref: row.get(14)?,
                workspace_hash: row.get(15)?,
                origin: row.get(16)?,
                attestation_state: row.get(17)?,
                attested_by: row.get(18)?,
                attestation_ref: row.get(19)?,
                valid_from_unix_ms: row.get(20)?,
                valid_to_unix_ms: row.get(21)?,
                state: row.get(22)?,
                recorded_at_unix_ms: row.get(23)?,
            })
        })
        .map_err(|e| format!("declared graph result edge query failed: {e}"))?
        .collect::<rusqlite::Result<Vec<_>>>()
        .map_err(|e| format!("declared graph result edge decode failed: {e}"))?;
    let edge_ids = edges.iter().map(|edge| edge.edge_id.clone()).collect();
    let node_ids = serde_json::from_str(&node_ids_json)
        .map_err(|e| format!("declared graph node id list is malformed: {e}"))?;
    Ok(DeclaredGraphManifestResult {
        schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
        outcome: outcome.to_string(),
        manifest_id: manifest_id.to_string(),
        source_id,
        node_ids,
        edge_ids,
        edges,
    })
}

impl crate::db::Database {
    pub fn apply_declared_graph_manifest(
        &self,
        request: &DeclaredGraphManifestRequest,
    ) -> Result<DeclaredGraphManifestResult, String> {
        request.validate()?;
        let source_id = declared_graph_source_id(&request.workspace_hash, &request.source_key);
        let manifest_id = declared_graph_manifest_id(
            &request.workspace_hash,
            &request.source_key,
            &request.revision,
        );
        let request_digest = declared_graph_request_digest(request);
        let recorded_at = crate::db::now_ms();
        if request.operation == "upsert" {
            if request
                .valid_from_unix_ms
                .is_some_and(|value| value > recorded_at)
            {
                return Err("declared graph manifest is not yet valid at recorded time".to_string());
            }
            if request
                .valid_to_unix_ms
                .is_some_and(|value| value <= recorded_at)
            {
                return Err("declared graph manifest is stale at recorded time".to_string());
            }
            for edge in &request.edges {
                let edge_from = edge.valid_from_unix_ms.or(request.valid_from_unix_ms);
                let edge_to = edge.valid_to_unix_ms.or(request.valid_to_unix_ms);
                if edge_from.is_some_and(|value| value > recorded_at) {
                    return Err(format!(
                        "declared graph edge {} is not yet valid at recorded time",
                        edge.from.as_str()
                    ));
                }
                if edge_to.is_some_and(|value| value <= recorded_at) {
                    return Err(format!(
                        "declared graph edge {} is stale at recorded time",
                        edge.from.as_str()
                    ));
                }
            }
        }
        let conn = self
            .conn()
            .map_err(|e| format!("declared graph connection failed: {e}"))?;
        let tx = rusqlite::Transaction::new_unchecked(&conn, TransactionBehavior::Immediate)
            .map_err(|e| format!("declared graph transaction failed: {e}"))?;
        let existing: Option<String> = tx
            .query_row(
                "SELECT request_digest FROM declared_graph_manifests WHERE manifest_id = ?1",
                params![manifest_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|e| format!("declared graph replay lookup failed: {e}"))?;
        if let Some(existing_digest) = existing {
            if existing_digest != request_digest {
                return Err(
                    "declared graph revision replay conflicts with the retained manifest"
                        .to_string(),
                );
            }
            tx.commit()
                .map_err(|e| format!("declared graph replay commit failed: {e}"))?;
            return declared_graph_manifest_result(self, &manifest_id, "idempotent");
        }

        let prior_edge_state = if request.operation == "delete" {
            "tombstoned"
        } else {
            "superseded"
        };
        tx.execute("UPDATE declared_graph_edges SET state = ?1, updated_at_unix_ms = ?2 WHERE manifest_id IN (SELECT manifest_id FROM declared_graph_manifests WHERE workspace_hash = ?3 AND source_key = ?4 AND state = ?5) AND state = ?6", params![prior_edge_state, recorded_at, request.workspace_hash, request.source_key, "active", "active"]).map_err(|e| format!("declared graph prior edge update failed: {e}"))?;
        tx.execute("UPDATE declared_graph_manifests SET state = ?1, updated_at_unix_ms = ?2 WHERE workspace_hash = ?3 AND source_key = ?4 AND state = ?5", params![prior_edge_state, recorded_at, request.workspace_hash, request.source_key, "active"]).map_err(|e| format!("declared graph prior manifest update failed: {e}"))?;

        let mut node_ids = Vec::new();
        if request.operation == "upsert" {
            for node in &request.nodes {
                let node_id = declared_graph_node_id(
                    &request.workspace_hash,
                    &node.namespace,
                    &node.canonical_id,
                );
                let existing_node: Option<(String, Option<String>)> = tx.query_row("SELECT node_type, external_ref FROM declared_graph_nodes WHERE node_id = ?1 AND workspace_hash = ?2", params![node_id, request.workspace_hash], |row| Ok((row.get(0)?, row.get(1)?))).optional().map_err(|e| format!("declared graph node lookup failed: {e}"))?;
                if let Some((existing_type, existing_ref)) = existing_node {
                    if existing_type != node.node_type || existing_ref != node.external_ref {
                        return Err(format!(
                            "declared graph node collision for {}",
                            declared_graph_node_key(&node.namespace, &node.canonical_id)
                        ));
                    }
                } else {
                    tx.execute("INSERT INTO declared_graph_nodes (node_id, workspace_hash, namespace, canonical_id, node_type, external_ref, state, created_at_unix_ms, updated_at_unix_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?8)", params![node_id, request.workspace_hash, node.namespace, node.canonical_id, node.node_type, node.external_ref, "active", recorded_at]).map_err(|e| format!("declared graph node insert failed: {e}"))?;
                }
                node_ids.push(node_id);
            }
        }
        let node_ids_json = serde_json::to_string(&node_ids)
            .map_err(|e| format!("declared graph node id serialization failed: {e}"))?;
        let manifest_state = if request.operation == "delete" {
            "deleted"
        } else {
            "active"
        };
        let tombstoned_at = if request.operation == "delete" {
            Some(recorded_at)
        } else {
            None
        };
        tx.execute("INSERT INTO declared_graph_manifests (manifest_id, source_id, workspace_hash, source_key, revision, content_sha256, source_span_ref, policy, origin, state, request_digest, node_ids_json, valid_from_unix_ms, valid_to_unix_ms, recorded_at_unix_ms, updated_at_unix_ms, tombstoned_at_unix_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?15, ?16)", params![manifest_id, source_id, request.workspace_hash, request.source_key, request.revision, request.content_sha256, request.source_span_ref, request.policy, "declared", manifest_state, request_digest, node_ids_json, request.valid_from_unix_ms, request.valid_to_unix_ms, recorded_at, tombstoned_at]).map_err(|e| format!("declared graph manifest insert failed: {e}"))?;

        let mut inserted_edge_ids = Vec::new();
        if request.operation == "upsert" {
            for edge in &request.edges {
                let (from_namespace, from_canonical_id) =
                    declared_graph_split_node_ref(&edge.from)?;
                let (to_namespace, to_canonical_id) = declared_graph_split_node_ref(&edge.to)?;
                let from_node_id = declared_graph_node_id(
                    &request.workspace_hash,
                    from_namespace,
                    from_canonical_id,
                );
                let to_node_id =
                    declared_graph_node_id(&request.workspace_hash, to_namespace, to_canonical_id);
                let edge_id = declared_graph_edge_id(
                    &manifest_id,
                    &from_node_id,
                    &edge.predicate,
                    &to_node_id,
                    &edge.direction,
                );
                tx.execute("INSERT INTO declared_graph_edges (edge_id, manifest_id, workspace_hash, from_node_id, to_node_id, predicate, direction, context, source_span_ref, origin, support_state, attested_by, attestation_ref, valid_from_unix_ms, valid_to_unix_ms, state, recorded_at_unix_ms, updated_at_unix_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?17)", params![edge_id, manifest_id, request.workspace_hash, from_node_id, to_node_id, edge.predicate, edge.direction, edge.context, edge.source_span_ref, "declared", edge.support_state, Option::<String>::None, Option::<String>::None, edge.valid_from_unix_ms.or(request.valid_from_unix_ms), edge.valid_to_unix_ms.or(request.valid_to_unix_ms), "active", recorded_at]).map_err(|e| format!("declared graph edge insert failed: {e}"))?;
                inserted_edge_ids.push(edge_id);
            }
        }
        let event_id = format!(
            "dgev-{}",
            declared_graph_digest(&json!([manifest_id, request.operation, request_digest]))
        );
        let receipt_digest = declared_graph_digest(
            &json!({"schema_version": DECLARED_GRAPH_SCHEMA_VERSION, "event_id": event_id, "manifest_id": manifest_id, "operation": request.operation, "request_digest": request_digest, "recorded_at_unix_ms": recorded_at}),
        );
        tx.execute("INSERT INTO declared_graph_manifest_events (event_id, manifest_id, source_id, workspace_hash, source_key, revision, operation, state_after, request_digest, actor_agent_id, receipt_digest, recorded_at_unix_ms) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)", params![event_id, manifest_id, source_id, request.workspace_hash, request.source_key, request.revision, request.operation, manifest_state, request_digest, request.requesting_agent_id, receipt_digest, recorded_at]).map_err(|e| format!("declared graph event insert failed: {e}"))?;
        tx.commit()
            .map_err(|e| format!("declared graph commit failed: {e}"))?;
        let journal = crate::models::JournalEvent { id: format!("jrn-{}", uuid::Uuid::new_v4().simple()), event_type: "declared_graph_manifest".to_string(), evaluated_json: serde_json::to_string(&json!({"manifest_id": manifest_id, "source_id": source_id, "source_key": request.source_key, "revision": request.revision, "operation": request.operation, "edge_count": inserted_edge_ids.len()})).map_err(|e| e.to_string())?, acted_json: serde_json::to_string(&json!({"state": manifest_state, "receipt_digest": receipt_digest, "origin": "declared"})).map_err(|e| e.to_string())?, forward_json: "{\"next\":\"query or explicitly attest the declared graph\"}".to_string(), category: String::new(), key: request.source_key.clone(), entity_id: String::new(), agent_id: request.requesting_agent_id.clone(), workspace_hash: request.workspace_hash.clone(), created_at_unix_ms: recorded_at };
        self.journal(&journal)
            .map_err(|e| format!("declared graph journal write failed: {e}"))?;
        declared_graph_manifest_result(self, &manifest_id, "applied")
    }

    pub fn attest_declared_graph(
        &self,
        request: &DeclaredGraphAttestationRequest,
    ) -> Result<DeclaredGraphAttestationResult, String> {
        request.validate()?;
        let request_digest = declared_graph_digest(request);
        let recorded_at = crate::db::now_ms();
        let conn = self
            .conn()
            .map_err(|e| format!("declared graph attestation connection failed: {e}"))?;
        let tx = rusqlite::Transaction::new_unchecked(&conn, TransactionBehavior::Immediate)
            .map_err(|e| format!("declared graph attestation transaction failed: {e}"))?;
        let manifest: Option<String> = tx.query_row("SELECT manifest_id FROM declared_graph_manifests WHERE workspace_hash = ?1 AND source_key = ?2 AND revision = ?3 AND state = ?4", params![request.workspace_hash, request.source_key, request.revision, "active"], |row| row.get(0)).optional().map_err(|e| format!("declared graph attestation manifest lookup failed: {e}"))?;
        let manifest_id = manifest.ok_or_else(|| {
            "declared graph attestation requires an active manifest revision".to_string()
        })?;
        let existing: Option<String> = tx.query_row("SELECT receipt_digest FROM declared_graph_manifest_events WHERE manifest_id = ?1 AND operation = ?2 AND request_digest = ?3", params![manifest_id, "attest", request_digest], |row| row.get(0)).optional().map_err(|e| format!("declared graph attestation replay lookup failed: {e}"))?;
        if let Some(receipt_digest) = existing {
            tx.commit()
                .map_err(|e| format!("declared graph attestation replay commit failed: {e}"))?;
            return Ok(DeclaredGraphAttestationResult {
                schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
                outcome: "idempotent".to_string(),
                manifest_id,
                edge_ids: request.edge_ids.clone(),
                receipt_digest,
            });
        }
        for edge_id in &request.edge_ids {
            let state: Option<String> = tx.query_row("SELECT support_state FROM declared_graph_edges WHERE edge_id = ?1 AND manifest_id = ?2 AND workspace_hash = ?3 AND state = ?4", params![edge_id, manifest_id, request.workspace_hash, "active"], |row| row.get(0)).optional().map_err(|e| format!("declared graph attestation edge lookup failed: {e}"))?;
            match state.as_deref() {
                Some("sourced") | Some("supported") => {}
                Some(other) => return Err(format!("declared graph edge {edge_id} has non-attestable support_state {other}")),
                None => return Err(format!("declared graph attestation edge {edge_id} is missing, out of scope, or inactive")),
            }
        }
        for edge_id in &request.edge_ids {
            let changed = tx.execute("UPDATE declared_graph_edges SET support_state = ?1, attested_by = ?2, attestation_ref = ?3, updated_at_unix_ms = ?4 WHERE edge_id = ?5 AND manifest_id = ?6 AND workspace_hash = ?7 AND state = ?8", params!["attested", request.attested_by, request.attestation_ref, recorded_at, edge_id, manifest_id, request.workspace_hash, "active"]).map_err(|e| format!("declared graph attestation update failed: {e}"))?;
            if changed != 1 {
                return Err(format!(
                    "declared graph attestation lost edge {edge_id} before update"
                ));
            }
        }
        let event_id = format!(
            "dgev-{}",
            declared_graph_digest(&json!([manifest_id, "attest", request_digest]))
        );
        let receipt_digest = declared_graph_digest(
            &json!({"schema_version": DECLARED_GRAPH_SCHEMA_VERSION, "event_id": event_id, "manifest_id": manifest_id, "operation": "attest", "request_digest": request_digest, "recorded_at_unix_ms": recorded_at}),
        );
        tx.execute("INSERT INTO declared_graph_manifest_events (event_id, manifest_id, source_id, workspace_hash, source_key, revision, operation, state_after, request_digest, actor_agent_id, receipt_digest, recorded_at_unix_ms) SELECT ?1, m.manifest_id, m.source_id, m.workspace_hash, m.source_key, m.revision, ?2, m.state, ?3, ?4, ?5, ?6 FROM declared_graph_manifests m WHERE m.manifest_id = ?7", params![event_id, "attest", request_digest, request.requesting_agent_id, receipt_digest, recorded_at, manifest_id]).map_err(|e| format!("declared graph attestation event insert failed: {e}"))?;
        tx.commit()
            .map_err(|e| format!("declared graph attestation commit failed: {e}"))?;
        let journal = crate::models::JournalEvent { id: format!("jrn-{}", uuid::Uuid::new_v4().simple()), event_type: "declared_graph_attestation".to_string(), evaluated_json: serde_json::to_string(&json!({"manifest_id": manifest_id, "source_key": request.source_key, "revision": request.revision, "edge_count": request.edge_ids.len()})).map_err(|e| e.to_string())?, acted_json: serde_json::to_string(&json!({"state": "attested", "attestation_ref": request.attestation_ref, "attested_by": request.attested_by})).map_err(|e| e.to_string())?, forward_json: "{\"next\":\"query the attested declared graph\"}".to_string(), category: String::new(), key: request.source_key.clone(), entity_id: String::new(), agent_id: request.requesting_agent_id.clone(), workspace_hash: request.workspace_hash.clone(), created_at_unix_ms: recorded_at };
        self.journal(&journal)
            .map_err(|e| format!("declared graph attestation journal write failed: {e}"))?;
        Ok(DeclaredGraphAttestationResult {
            schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
            outcome: "applied".to_string(),
            manifest_id,
            edge_ids: request.edge_ids.clone(),
            receipt_digest,
        })
    }

    pub fn query_declared_graph(
        &self,
        query: &DeclaredGraphQuery,
    ) -> Result<DeclaredGraphProjection, Box<dyn std::error::Error>> {
        if query.workspace_hash.trim().is_empty() {
            return Err("declared graph query requires workspace_hash".into());
        }
        if query.requesting_agent_id.trim().is_empty() {
            return Err("declared graph query requires requesting_agent_id".into());
        }
        if !(1..=DECLARED_GRAPH_MAX_QUERY_LIMIT).contains(&query.limit) {
            return Err(format!(
                "declared graph query limit must be 1..={DECLARED_GRAPH_MAX_QUERY_LIMIT}"
            )
            .into());
        }
        if let Some(source_key) = query.source_key.as_deref() {
            declared_graph_validate_text(
                "query source_key",
                source_key,
                DECLARED_GRAPH_MAX_SOURCE_KEY,
                true,
            )?;
        }
        let conn = self.conn()?;
        let history_flag = if query.include_history { 1_i64 } else { 0_i64 };
        let sql = format!("{DECLARED_GRAPH_EDGE_SELECT} WHERE e.workspace_hash = ?1 AND (?2 IS NULL OR m.source_key = ?2) AND (?4 = 1 OR (m.state = ?5 AND e.state = ?6)) ORDER BY e.recorded_at_unix_ms ASC, e.edge_id ASC LIMIT ?3");
        let mut stmt = conn.prepare(&sql)?;
        let rows = stmt
            .query_map(
                params![
                    query.workspace_hash,
                    query.source_key,
                    query.limit + 1,
                    history_flag,
                    "active",
                    "active"
                ],
                |row| {
                    Ok(DeclaredGraphEdgeView {
                        edge_id: row.get(0)?,
                        manifest_id: row.get(1)?,
                        source_id: row.get(2)?,
                        source_key: row.get(3)?,
                        source_revision: row.get(4)?,
                        source_sha256: row.get(5)?,
                        manifest_span_ref: row.get(6)?,
                        from_node_id: row.get(7)?,
                        to_node_id: row.get(8)?,
                        from: row.get(9)?,
                        to: row.get(10)?,
                        predicate: row.get(11)?,
                        direction: row.get(12)?,
                        context: row.get(13)?,
                        source_span_ref: row.get(14)?,
                        workspace_hash: row.get(15)?,
                        origin: row.get(16)?,
                        attestation_state: row.get(17)?,
                        attested_by: row.get(18)?,
                        attestation_ref: row.get(19)?,
                        valid_from_unix_ms: row.get(20)?,
                        valid_to_unix_ms: row.get(21)?,
                        state: row.get(22)?,
                        recorded_at_unix_ms: row.get(23)?,
                    })
                },
            )?
            .collect::<rusqlite::Result<Vec<_>>>()?;
        let truncated = rows.len() as i64 > query.limit;
        let edges: Vec<_> = rows.into_iter().take(query.limit as usize).collect();
        let mut node_ids = Vec::with_capacity(edges.len() * 2);
        for edge in &edges {
            node_ids.push(edge.from_node_id.clone());
            node_ids.push(edge.to_node_id.clone());
        }
        node_ids.sort();
        node_ids.dedup();
        let mut nodes = Vec::with_capacity(node_ids.len());
        for node_id in node_ids {
            if let Some(node) = conn.query_row("SELECT node_id, namespace, canonical_id, node_type, external_ref, workspace_hash, state FROM declared_graph_nodes WHERE node_id = ?1", params![node_id], |row| Ok(DeclaredGraphNodeView { node_id: row.get(0)?, namespace: row.get(1)?, canonical_id: row.get(2)?, node_type: row.get(3)?, external_ref: row.get(4)?, workspace_hash: row.get(5)?, state: row.get(6)? })).optional()? { nodes.push(node); }
        }
        Ok(DeclaredGraphProjection {
            schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
            workspace_hash: query.workspace_hash.clone(),
            source_key: query.source_key.clone(),
            nodes,
            edges,
            truncated,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn node(namespace: &str, canonical_id: &str, node_type: &str) -> DeclaredGraphNodeInput {
        DeclaredGraphNodeInput {
            namespace: namespace.to_string(),
            canonical_id: canonical_id.to_string(),
            node_type: node_type.to_string(),
            external_ref: Some(format!("{namespace}:{canonical_id}")),
        }
    }

    fn manifest() -> DeclaredGraphManifestRequest {
        DeclaredGraphManifestRequest {
            schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
            operation: "upsert".to_string(),
            source_key: "service-manifest".to_string(),
            revision: "r1".to_string(),
            content_sha256: "a".repeat(64),
            source_span_ref: Some("artifact:manifest/span:0-120".to_string()),
            workspace_hash: "workspace-a".to_string(),
            valid_from_unix_ms: Some(1_000),
            valid_to_unix_ms: None,
            policy: "replace".to_string(),
            nodes: vec![
                node("service", "api", "service"),
                node("service", "db", "database"),
            ],
            edges: vec![DeclaredGraphEdgeInput {
                from: "service:api".to_string(),
                to: "service:db".to_string(),
                predicate: "DEPENDS_ON".to_string(),
                direction: "forward".to_string(),
                context: Some("API requires the database".to_string()),
                source_span_ref: Some("artifact:manifest/span:20-48".to_string()),
                origin: "declared".to_string(),
                support_state: "sourced".to_string(),
                valid_from_unix_ms: Some(1_000),
                valid_to_unix_ms: None,
            }],
            requesting_agent_id: "agent-a".to_string(),
        }
    }

    #[test]
    fn manifest_replay_is_idempotent_and_keeps_source_attestation_explicit() {
        let db = crate::db::TestDatabase::new("declared-graph-replay");
        let request = manifest();
        let first = db
            .apply_declared_graph_manifest(&request)
            .expect("first manifest application");
        let second = db
            .apply_declared_graph_manifest(&request)
            .expect("manifest replay");
        assert_eq!(first.outcome, "applied");
        assert_eq!(second.outcome, "idempotent");
        assert_eq!(first.manifest_id, second.manifest_id);
        assert_eq!(first.edge_ids, second.edge_ids);
        assert_eq!(first.edge_ids.len(), 1);
        assert_eq!(first.edges[0].origin, "declared");
        assert_eq!(first.edges[0].attestation_state, "sourced");
        assert_ne!(first.edges[0].attestation_state, "attested");
        assert_eq!(first.edges[0].source_revision, "r1");
        assert_eq!(first.edges[0].source_sha256, "a".repeat(64));
        assert_eq!(first.edges[0].workspace_hash, "workspace-a");
        assert!(first.edges[0].source_span_ref.is_some());
        let first_edge_json = serde_json::to_value(&first.edges[0]).expect("edge JSON");
        assert!(first_edge_json.get("raw_provider_body").is_none());

        let query = DeclaredGraphQuery {
            workspace_hash: "workspace-a".to_string(),
            source_key: None,
            requesting_agent_id: "agent-a".to_string(),
            include_history: false,
            limit: 50,
        };
        let projection = db
            .query_declared_graph(&query)
            .expect("declared graph projection");
        assert_eq!(projection.edges.len(), 1);
        assert_eq!(projection.edges[0].edge_id, first.edge_ids[0]);
        assert_eq!(projection.edges[0].predicate, "DEPENDS_ON");
        assert_eq!(projection.edges[0].attestation_state, "sourced");
        assert_eq!(projection.edges[0].origin, "declared");
        assert_eq!(projection.edges[0].source_revision, "r1");
        assert_eq!(projection.edges[0].source_sha256, "a".repeat(64));
        assert_eq!(projection.edges[0].workspace_hash, "workspace-a");
        let projection_edge_json =
            serde_json::to_value(&projection.edges[0]).expect("projection edge JSON");
        assert!(projection_edge_json.get("body").is_none());
        let encoded = serde_json::to_value(&projection).expect("projection JSON");
        assert_eq!(encoded["schema_version"], json!(1));
    }

    #[test]
    fn explicit_attestation_transitions_selected_edges_and_replays() {
        let db = crate::db::TestDatabase::new("declared-graph-attestation");
        let request = manifest();
        let applied = db
            .apply_declared_graph_manifest(&request)
            .expect("manifest application");
        let attestation = DeclaredGraphAttestationRequest {
            schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
            workspace_hash: "workspace-a".to_string(),
            source_key: "service-manifest".to_string(),
            revision: "r1".to_string(),
            edge_ids: applied.edge_ids.clone(),
            attestation_ref: "review:service-manifest:r1".to_string(),
            attested_by: "reviewer-a".to_string(),
            requesting_agent_id: "agent-a".to_string(),
        };
        let first = db
            .attest_declared_graph(&attestation)
            .expect("explicit attestation");
        let second = db
            .attest_declared_graph(&attestation)
            .expect("attestation replay");
        assert_eq!(first.outcome, "applied");
        assert_eq!(second.outcome, "idempotent");
        assert_eq!(first.edge_ids, applied.edge_ids);
        let projection = db
            .query_declared_graph(&DeclaredGraphQuery {
                workspace_hash: "workspace-a".to_string(),
                source_key: None,
                requesting_agent_id: "agent-a".to_string(),
                include_history: false,
                limit: 50,
            })
            .expect("attested projection");
        assert_eq!(projection.edges.len(), 1);
        assert_eq!(projection.edges[0].attestation_state, "attested");
        assert_eq!(
            projection.edges[0].attested_by.as_deref(),
            Some("reviewer-a")
        );
        assert_eq!(
            projection.edges[0].attestation_ref.as_deref(),
            Some("review:service-manifest:r1")
        );
    }

    #[test]
    fn public_handlers_round_trip_and_require_transport_identity() {
        let db = crate::db::TestDatabase::new("declared-graph-handlers");
        let request = manifest();
        let mut manifest_args = serde_json::to_value(&request).expect("manifest arguments");
        let applied_json = crate::tools::handle_declared_graph_manifest(&db, manifest_args.clone())
            .expect("public manifest handler");
        let applied: DeclaredGraphManifestResult =
            serde_json::from_str(&applied_json).expect("manifest handler JSON");
        assert_eq!(applied.outcome, "applied");

        let attestation_args = serde_json::to_value(DeclaredGraphAttestationRequest {
            schema_version: DECLARED_GRAPH_SCHEMA_VERSION,
            workspace_hash: request.workspace_hash.clone(),
            source_key: request.source_key.clone(),
            revision: request.revision.clone(),
            edge_ids: applied.edge_ids.clone(),
            attestation_ref: "review:handler:r1".to_string(),
            attested_by: "reviewer-handler".to_string(),
            requesting_agent_id: request.requesting_agent_id.clone(),
        })
        .expect("attestation arguments");
        let attested_json = crate::tools::handle_declared_graph_attest(&db, attestation_args)
            .expect("public attestation handler");
        let attested: DeclaredGraphAttestationResult =
            serde_json::from_str(&attested_json).expect("attestation handler JSON");
        assert_eq!(attested.outcome, "applied");

        let query_json = crate::tools::handle_declared_graph_query(
            &db,
            json!({
                "workspace_hash": request.workspace_hash,
                "requesting_agent_id": request.requesting_agent_id,
                "limit": 10
            }),
        )
        .expect("public query handler");
        let projection: serde_json::Value =
            serde_json::from_str(&query_json).expect("query handler JSON");
        assert_eq!(projection["edges"].as_array().map(Vec::len), Some(1));
        assert_eq!(projection["edges"][0]["attestation_state"], "attested");

        manifest_args
            .as_object_mut()
            .expect("manifest object")
            .remove("requesting_agent_id");
        let missing_identity = crate::tools::handle_declared_graph_manifest(&db, manifest_args)
            .expect_err("public manifest handler must require stamped identity");
        assert!(missing_identity.contains("requesting_agent_id"));
    }

    #[test]
    fn declared_graph_projection_is_opt_in_on_recall_traverse_and_context() {
        let db = crate::db::TestDatabase::new("declared-graph-presentation");
        let request = manifest();
        db.apply_declared_graph_manifest(&request)
            .expect("manifest application");

        let default_recall: serde_json::Value = serde_json::from_str(
            &crate::tools::handle_recall(
                &db,
                json!({
                    "query": "service",
                    "workspace_hash": "workspace-a",
                    "requesting_agent_id": "agent-a",
                    "limit": 10
                }),
            )
            .expect("default recall"),
        )
        .expect("default recall JSON");
        assert!(default_recall.get("declared_graph").is_none());

        let opted_recall: serde_json::Value = serde_json::from_str(
            &crate::tools::handle_recall(
                &db,
                json!({
                    "query": "service",
                    "workspace_hash": "workspace-a",
                    "requesting_agent_id": "agent-a",
                    "include_declared_graph": true,
                    "limit": 10
                }),
            )
            .expect("declared graph recall"),
        )
        .expect("declared graph recall JSON");
        assert_eq!(
            opted_recall["declared_graph"]["edges"]
                .as_array()
                .map(Vec::len),
            Some(1)
        );
        assert_eq!(
            opted_recall["declared_graph"]["edges"][0]["origin"],
            "declared"
        );
        assert_eq!(
            opted_recall["declared_graph"]["edges"][0]["source_revision"],
            "r1"
        );
        assert_eq!(
            opted_recall["declared_graph"]["edges"][0]["workspace_hash"],
            "workspace-a"
        );
        assert_eq!(
            opted_recall["declared_graph"]["edges"][0]["attestation_state"],
            "sourced"
        );

        crate::tools::handle_remember(
            &db,
            json!({
                "category": "service",
                "key": "api",
                "body_json": "{\"text\":\"service api\"}",
                "workspace_hash": "workspace-a",
                "agent_id": "agent-a"
            }),
        )
        .expect("traversal root");
        let default_traverse: serde_json::Value =
            serde_json::from_str(&crate::tools::handle_traverse(
                &db,
                json!({
                    "category": "service",
                    "key": "api",
                    "workspace_hash": "workspace-a",
                    "requesting_agent_id": "agent-a"
                }),
            ))
            .expect("default traverse JSON");
        assert!(default_traverse.get("declared_graph").is_none());

        let opted_traverse: serde_json::Value =
            serde_json::from_str(&crate::tools::handle_traverse(
                &db,
                json!({
                    "category": "service",
                    "key": "api",
                    "workspace_hash": "workspace-a",
                    "requesting_agent_id": "agent-a",
                    "include_declared_graph": true
                }),
            ))
            .expect("declared graph traverse JSON");
        assert_eq!(
            opted_traverse["declared_graph"]["edges"]
                .as_array()
                .map(Vec::len),
            Some(1),
            "traverse output: {opted_traverse}"
        );
        assert_eq!(
            opted_traverse["declared_graph"]["edges"][0]["attestation_state"],
            "sourced"
        );

        let default_context: serde_json::Value =
            serde_json::from_str(&crate::tools::handle_context(
                &db,
                json!({
                    "workspace_hash": "workspace-a",
                    "query": "service",
                    "requesting_agent_id": "agent-a"
                }),
            ))
            .expect("default context JSON");
        assert!(default_context.get("declared_graph").is_none());

        let opted_context: serde_json::Value = serde_json::from_str(&crate::tools::handle_context(
            &db,
            json!({
                "workspace_hash": "workspace-a",
                "query": "service",
                "requesting_agent_id": "agent-a",
                "include_declared_graph": true
            }),
        ))
        .expect("declared graph context JSON");
        assert_eq!(
            opted_context["declared_graph"]["edges"]
                .as_array()
                .map(Vec::len),
            Some(1)
        );
        assert_eq!(
            opted_context["declared_graph"]["edges"][0]["source_sha256"],
            "a".repeat(64)
        );
    }

    #[test]
    fn replacement_and_delete_keep_history_and_reject_stale_manifests() {
        let db = crate::db::TestDatabase::new("declared-graph-lifecycle");
        let first = manifest();
        let first_result = db
            .apply_declared_graph_manifest(&first)
            .expect("first revision");

        let mut second = first.clone();
        second.revision = "r2".to_string();
        second.content_sha256 = "b".repeat(64);
        second.edges[0].predicate = "OWNED_BY".to_string();
        let second_result = db
            .apply_declared_graph_manifest(&second)
            .expect("replacement revision");
        assert_ne!(first_result.manifest_id, second_result.manifest_id);

        let query = |include_history| {
            db.query_declared_graph(&DeclaredGraphQuery {
                workspace_hash: "workspace-a".to_string(),
                source_key: None,
                requesting_agent_id: "agent-a".to_string(),
                include_history,
                limit: 50,
            })
            .expect("graph query")
        };
        let active = query(false);
        assert_eq!(active.edges.len(), 1);
        assert_eq!(active.edges[0].source_revision, "r2");
        assert_eq!(active.edges[0].state, "active");

        let history = query(true);
        assert_eq!(history.edges.len(), 2);
        assert!(history
            .edges
            .iter()
            .any(|edge| edge.source_revision == "r1" && edge.state == "superseded"));
        assert!(history
            .edges
            .iter()
            .any(|edge| edge.source_revision == "r2" && edge.state == "active"));

        let mut deletion = second.clone();
        deletion.operation = "delete".to_string();
        deletion.revision = "r3".to_string();
        deletion.content_sha256 = "c".repeat(64);
        deletion.nodes.clear();
        deletion.edges.clear();
        let deleted = db
            .apply_declared_graph_manifest(&deletion)
            .expect("delete revision");
        assert_eq!(deleted.edge_ids.len(), 0);
        assert!(query(false).edges.is_empty());
        let deleted_history = query(true);
        assert!(deleted_history
            .edges
            .iter()
            .any(|edge| edge.source_revision == "r2" && edge.state == "tombstoned"));

        let mut stale = manifest();
        stale.source_key = "stale-manifest".to_string();
        stale.content_sha256 = "d".repeat(64);
        stale.valid_from_unix_ms = None;
        stale.valid_to_unix_ms = Some(1);
        let error = db
            .apply_declared_graph_manifest(&stale)
            .expect_err("stale manifest must fail closed");
        assert!(error.contains("stale"), "unexpected stale error: {error}");
    }
}
