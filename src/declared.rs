//! #923: declared exact-match retrieval arm.
//!
//! A deterministic, read-side "declared retrieval contract": operators
//! declare typed fields for a category (scalar exact-match and string_list
//! membership, plus facet-eligible fields), and retrieval answers
//! exact-equality filters with NO ranking. The declarations are advisory
//! retrieval metadata only — they never gate writes, and categories without
//! a declaration behave exactly as before.
//!
//! Storage: declarations live as ordinary governed entities in the reserved
//! category `declared_schema` (key = declared category name). They are
//! written only through `perseus_vault_declared_schema_set` (fail-closed
//! validation) and are fully governed by the normal lifecycle (FTS-indexed,
//! audited, suppressible, forgettable).
//!
//! Field values are read from each entity's own `body_json` top-level keys
//! at query time. A declared scalar field must hold a JSON string on a
//! matching entity; a declared string_list field must hold a JSON array of
//! strings. Entities whose values do not conform to their declaration are
//! simply not matchable through that field (read-side lenient), while
//! malformed *filters* are rejected fail-closed.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

/// Reserved category holding declarations (key = declared category name).
pub const DECLARED_SCHEMA_CATEGORY: &str = "declared_schema";
/// Fields a category may declare at most.
pub const MAX_DECLARED_FIELDS: usize = 32;
/// Facet-eligible fields at most.
pub const MAX_DECLARED_FACETS: usize = 16;
/// Query-guidance string cap.
pub const MAX_QUERY_GUIDANCE_LEN: usize = 500;
/// Facet distinct-value cap (truthful and bounded): values beyond this are
/// rolled into a single `"other"` bucket with its own count.
pub const FACET_VALUE_CAP: usize = 50;

/// Body keys the vault itself manages on remember — declaring a field with
/// one of these names is refused fail-closed (the value would be ambiguous).
const RESERVED_FIELD_NAMES: &[&str] = &[
    "id",
    "category",
    "key",
    "recall_when",
    "origin",
    "external_refs",
    "expires_at",
];

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DeclaredFieldType {
    /// Exact string equality against a top-level body_json string value.
    Scalar,
    /// Array membership: entity matches if any of its strings equals any of
    /// the filter's strings.
    StringList,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DeclaredField {
    pub name: String,
    #[serde(rename = "type")]
    pub field_type: DeclaredFieldType,
    /// Facet-eligible: `declared_query` may request truthful, bounded
    /// distinct-value counts for this field.
    #[serde(default)]
    pub facet: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct DeclaredSchema {
    /// The category this contract describes.
    pub category: String,
    pub fields: Vec<DeclaredField>,
    /// Advisory query guidance for the category (what agents should ask).
    #[serde(default)]
    pub query_guidance: String,
    /// Monotonic revision; bumped on every set.
    pub version: i64,
}

impl DeclaredSchema {
    pub fn field(&self, name: &str) -> Option<&DeclaredField> {
        self.fields.iter().find(|f| f.name == name)
    }
}

/// Validate + normalize a declaration. Fail-closed: every structural
/// violation is an error, never a silent fix.
pub fn validate_declared_schema(
    category: &str,
    fields: &[DeclaredField],
    query_guidance: &str,
) -> Result<DeclaredSchema, String> {
    let category = category.trim();
    if category.is_empty() {
        return Err("declared schema: category must be non-empty".to_string());
    }
    if category == DECLARED_SCHEMA_CATEGORY {
        return Err(format!(
            "declared schema: '{category}' is a reserved category"
        ));
    }
    if fields.is_empty() {
        return Err("declared schema: at least one field is required".to_string());
    }
    if fields.len() > MAX_DECLARED_FIELDS {
        return Err(format!(
            "declared schema: too many fields ({} max {MAX_DECLARED_FIELDS})",
            fields.len()
        ));
    }
    let mut seen: std::collections::HashSet<&str> = std::collections::HashSet::new();
    let mut facet_count = 0usize;
    for f in fields {
        let name = f.name.trim();
        if name.is_empty() {
            return Err("declared schema: field names must be non-empty".to_string());
        }
        if RESERVED_FIELD_NAMES.contains(&name) {
            return Err(format!(
                "declared schema: field name '{name}' is reserved by the vault"
            ));
        }
        if !seen.insert(name) {
            return Err(format!("declared schema: duplicate field name '{name}'"));
        }
        if f.facet {
            facet_count += 1;
        }
    }
    if facet_count > MAX_DECLARED_FACETS {
        return Err(format!(
            "declared schema: too many facet-eligible fields ({facet_count} max {MAX_DECLARED_FACETS})"
        ));
    }
    let guidance = query_guidance.trim().to_string();
    if guidance.len() > MAX_QUERY_GUIDANCE_LEN {
        return Err(format!(
            "declared schema: query_guidance too long ({} bytes max {MAX_QUERY_GUIDANCE_LEN})",
            guidance.len()
        ));
    }
    Ok(DeclaredSchema {
        category: category.to_string(),
        fields: fields
            .iter()
            .map(|f| DeclaredField {
                name: f.name.trim().to_string(),
                field_type: f.field_type,
                facet: f.facet,
            })
            .collect(),
        query_guidance: guidance,
        version: 1,
    })
}

/// Parse a stored declaration body (`{"schema": {...}}`).
pub fn parse_declared_schema(body_json: &str) -> Result<DeclaredSchema, String> {
    let value: serde_json::Value = serde_json::from_str(body_json)
        .map_err(|e| format!("declared schema: stored body is not JSON: {e}"))?;
    let inner = value
        .get("schema")
        .ok_or_else(|| "declared schema: stored body missing \"schema\"".to_string())?;
    let schema: DeclaredSchema = serde_json::from_value(inner.clone())
        .map_err(|e| format!("declared schema: stored declaration is malformed: {e}"))?;
    Ok(schema)
}

/// Load the declaration for a category, if any.
pub fn load_declared_schema(
    db: &crate::db::Database,
    category: &str,
) -> Result<Option<DeclaredSchema>, String> {
    let Some(entity) = db
        .get_entity(DECLARED_SCHEMA_CATEGORY, category)
        .map_err(|e| format!("declared schema: read failed: {e}"))?
    else {
        return Ok(None);
    };
    parse_declared_schema(&entity.body_json).map(Some)
}

/// Persist a declaration via the governed remember path (skip-dedup, like
/// other reserved entities). Returns the stored schema with its revision.
pub fn declared_schema_set(
    db: &crate::db::Database,
    category: &str,
    fields: &[DeclaredField],
    query_guidance: &str,
) -> Result<DeclaredSchema, String> {
    let mut schema = validate_declared_schema(category, fields, query_guidance)?;
    if let Some(existing) = load_declared_schema(db, &schema.category)? {
        schema.version = existing.version + 1;
    }
    let body = serde_json::json!({ "schema": schema });
    let now = crate::db::now_ms();
    let entity = crate::models::Entity {
        id: format!("decl-{}", uuid::Uuid::new_v4().simple()),
        category: DECLARED_SCHEMA_CATEGORY.to_string(),
        key: schema.category.clone(),
        body_json: serde_json::to_string(&body).map_err(|e| e.to_string())?,
        status: "active".to_string(),
        entity_type: "declared_schema".to_string(),
        tags: vec![],
        decay_score: 1.0,
        retrieval_count: 0,
        layer: "working".to_string(),
        topic_path: String::new(),
        archived: false,
        archive_reason: String::new(),
        links: vec![],
        verified: false,
        source: "declared_schema_set".to_string(),
        always_on: false,
        certainty: 1.0,
        workspace_hash: String::new(),
        agent_id: String::new(),
        visibility: "workspace".to_string(),
        created_at_unix_ms: now,
        last_accessed_unix_ms: now,
        follow_count: 0,
        miss_count: 0,
        follow_rate: 0.0,
        efficacy_status: "unverified".to_string(),
        epistemic_state: crate::models::default_epistemic_state(),
        hints: vec![],
        embedding: None,
        _parsed_body: None,
    };
    db.remember_skip_dedup(&entity)
        .map_err(|e| format!("declared schema: store failed: {e}"))?;
    Ok(schema)
}

/// A single declared filter: for a scalar field the value is an exact string
/// to equal; for a string_list field it is an array of strings, any of which
/// must appear in the entity's list (membership).
#[derive(Debug, Clone)]
pub struct DeclaredFilter {
    pub field: String,
    pub value: serde_json::Value,
}

/// Exact-match scan over one category's live entities. `filters` are
/// AND-combined; entities are returned in deterministic order
/// (created_at_unix_ms ASC, id ASC) — the arm serves no ranking.
pub fn declared_candidates(
    db: &crate::db::Database,
    schema: &DeclaredSchema,
    filters: &[DeclaredFilter],
    workspace_hash: Option<&str>,
) -> Result<Vec<crate::models::Entity>, String> {
    // Validate filters against the declaration first (fail-closed).
    for f in filters {
        let field = schema.field(&f.field).ok_or_else(|| {
            format!(
                "declared query: unknown field '{}' for category '{}' (declared: {:?})",
                f.field,
                schema.category,
                schema
                    .fields
                    .iter()
                    .map(|x| x.name.as_str())
                    .collect::<Vec<_>>()
            )
        })?;
        match field.field_type {
            DeclaredFieldType::Scalar => {
                if !f.value.is_string() {
                    return Err(format!(
                        "declared query: filter for scalar field '{}' must be a string",
                        f.field
                    ));
                }
            }
            DeclaredFieldType::StringList => {
                let arr = f.value.as_array().ok_or_else(|| {
                    format!(
                        "declared query: filter for string_list field '{}' must be an array of strings",
                        f.field
                    )
                })?;
                if arr.iter().any(|v| !v.is_string()) {
                    return Err(format!(
                        "declared query: filter for string_list field '{}' must contain only strings",
                        f.field
                    ));
                }
            }
        }
    }

    let entities = {
        let conn = db.conn().map_err(|e| e.to_string())?;
        let mut stmt = conn
            .prepare(
                "SELECT id, category, key, body_json, status, type, tags, decay_score,
                        retrieval_count, layer, topic_path, archived, archive_reason, links,
                        verified, source, created_at_unix_ms, last_accessed_unix_ms,
                        NULL as embedding, always_on, certainty, workspace_hash, agent_id,
                        visibility, follow_count, miss_count, follow_rate, efficacy_status,
                        epistemic_state, hints
                 FROM entities WHERE category = ?1 AND archived = 0",
            )
            .map_err(|e| e.to_string())?;
        let enc = db.encryption.as_ref();
        let mut entities: Vec<crate::models::Entity> = Vec::new();
        let rows = stmt
            .query_map([schema.category.as_str()], |row| {
                crate::db::entity_from_row(row, enc)
            })
            .map_err(|e| e.to_string())?;
        for row in rows {
            let entity = row.map_err(|e| e.to_string())?;
            if let Some(ws) = workspace_hash {
                if !ws.is_empty()
                    && !entity.workspace_hash.is_empty()
                    && entity.workspace_hash != ws
                {
                    continue;
                }
            }
            if filters.is_empty() || entity_matches_filters(&entity, schema, filters)? {
                entities.push(entity);
            }
        }
        entities
    };
    let mut entities = db.filter_suppressed(entities).map_err(|e| e.to_string())?;
    entities.sort_by(|a, b| {
        a.created_at_unix_ms
            .cmp(&b.created_at_unix_ms)
            .then_with(|| a.id.cmp(&b.id))
    });
    Ok(entities)
}

fn entity_matches_filters(
    entity: &crate::models::Entity,
    schema: &DeclaredSchema,
    filters: &[DeclaredFilter],
) -> Result<bool, String> {
    let body: serde_json::Value = serde_json::from_str(&entity.body_json).map_err(|e| {
        format!(
            "declared query: entity {} has non-JSON body: {e}",
            entity.id
        )
    })?;
    for f in filters {
        let field = schema
            .field(&f.field)
            .ok_or_else(|| format!("declared query: unknown field '{}'", f.field))?;
        let Some(value) = body.get(&f.field) else {
            return Ok(false); // entity lacks the field -> cannot match
        };
        match field.field_type {
            DeclaredFieldType::Scalar => {
                if value != &f.value {
                    return Ok(false);
                }
            }
            DeclaredFieldType::StringList => {
                let Some(list) = value.as_array() else {
                    return Ok(false); // declared list but stored value is not a list
                };
                let wanted: std::collections::HashSet<&str> = f
                    .value
                    .as_array()
                    .map(|arr| arr.iter().filter_map(|v| v.as_str()).collect())
                    .unwrap_or_default();
                let has = list
                    .iter()
                    .any(|v| v.as_str().map(|s| wanted.contains(s)).unwrap_or(false));
                if !has {
                    return Ok(false);
                }
            }
        }
    }
    Ok(true)
}

/// One facet bucket: a distinct value and how many entities carry it.
#[derive(Debug, Clone, Serialize, PartialEq)]
pub struct FacetCount {
    pub value: String,
    pub count: i64,
}

/// Truthful, bounded facet counts for the requested facet-eligible fields.
/// Counts are computed over the category rows that pass every filter EXCEPT
/// the facet's own (the standard refine-by-facet semantics), capped at
/// `FACET_VALUE_CAP` distinct values with an `"other"` roll-up bucket.
pub fn facet_counts(
    db: &crate::db::Database,
    schema: &DeclaredSchema,
    filters: &[DeclaredFilter],
    facet_fields: &[String],
    workspace_hash: Option<&str>,
) -> Result<HashMap<String, Vec<FacetCount>>, String> {
    let mut out: HashMap<String, Vec<FacetCount>> = HashMap::new();
    for name in facet_fields {
        let field = schema.field(name).ok_or_else(|| {
            format!(
                "declared query: facet field '{}' is not declared for category '{}'",
                name, schema.category
            )
        })?;
        if !field.facet {
            return Err(format!(
                "declared query: field '{}' is not facet-eligible (declare facet: true)",
                name
            ));
        }
        let others: Vec<DeclaredFilter> = filters
            .iter()
            .filter(|f| f.field != *name)
            .cloned()
            .collect();
        let rows = declared_candidates(db, schema, &others, workspace_hash)?;
        let mut counts: std::collections::BTreeMap<String, usize> =
            std::collections::BTreeMap::new();
        let mut other = 0usize;
        for entity in rows {
            let body: serde_json::Value = serde_json::from_str(&entity.body_json)
                .map_err(|e| format!("declared query: entity {} body: {e}", entity.id))?;
            let Some(value) = body.get(name) else {
                continue;
            };
            match field.field_type {
                DeclaredFieldType::Scalar => {
                    if let Some(s) = value.as_str() {
                        if counts.len() < FACET_VALUE_CAP || counts.contains_key(s) {
                            *counts.entry(s.to_string()).or_insert(0) += 1;
                        } else {
                            other += 1;
                        }
                    }
                }
                DeclaredFieldType::StringList => {
                    if let Some(list) = value.as_array() {
                        for v in list {
                            if let Some(s) = v.as_str() {
                                if counts.len() < FACET_VALUE_CAP || counts.contains_key(s) {
                                    *counts.entry(s.to_string()).or_insert(0) += 1;
                                } else {
                                    other += 1;
                                }
                            }
                        }
                    }
                }
            }
        }
        let mut buckets: Vec<FacetCount> = counts
            .into_iter()
            .map(|(value, count)| FacetCount {
                value,
                count: count as i64,
            })
            .collect();
        if other > 0 {
            buckets.push(FacetCount {
                value: "other".to_string(),
                count: other as i64,
            });
        }
        out.insert(name.to_string(), buckets);
    }
    Ok(out)
}
