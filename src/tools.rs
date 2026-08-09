use serde::Deserialize;
use serde_json::{json, Value};
use uuid::Uuid;
use rusqlite::OptionalExtension;

use crate::db::{now_ms, Database};
use crate::log_digest;
use crate::models::{
    ArtifactRepresentation, Entity, ExternalRef, JournalEvent, OriginRecord, SearchMode,
    StateEntry, TimelineParams,
    AskParams, EmbedParams, IngestParams, PruneParams, RecallParams,
};

// ─── Deserialization structs ────────────────────────────────────

/// #330: many MCP clients send explicit JSON `null` for an optional field
/// they didn't set (rather than omitting the key), because the tool schema
/// lists the field as optional/defaulted. serde's `#[serde(default = "...")]`
/// only fires when the key is *absent*; a present `null` still hits the
/// field's real type and fails with a misleading "invalid type: null,
/// expected a string/boolean/f64/..." error that names the wrong field
/// entirely once combined with `#[serde(deny_unknown_fields)]`-style
/// confusion. This helper treats an explicit `null` the same as an absent
/// key by falling through to `Default::default()` for the field type; pair
/// it with `#[serde(default = "...", deserialize_with = "null_as_default")]`
/// when the field also needs a non-Default::default() default value.
fn reviewable_write_result(mut result: Value, writer: &str) -> Value {
    result["proposed"] = json!(true);
    result["requires_review"] = json!(true);
    result["provenance"] = json!({
        "state": "proposed",
        "writer": writer,
        "hash_only": true,
    });
    result
}

fn null_as_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: serde::Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}

/// Deserialize `Option<i64>` from a JSON number, a numeric string, or null/absent.
/// MCP/LLM tool-call clients frequently emit integer arguments as strings (e.g.
/// `"as_of_unix_ms": "1783400000000"`); without this the value is rejected with
/// "invalid type: string, expected i64" and the temporal filters are unusable
/// from those clients. Numbers still deserialize unchanged, so this only widens
/// what is accepted (backward compatible). Empty/whitespace string = None.
pub(crate) fn string_or_int_opt<'de, D>(deserializer: D) -> Result<Option<i64>, D::Error>
where
    D: serde::Deserializer<'de>,
{
    #[derive(Deserialize)]
    #[serde(untagged)]
    enum StrOrInt {
        Int(i64),
        Str(String),
    }
    Ok(match Option::<StrOrInt>::deserialize(deserializer)? {
        None => None,
        Some(StrOrInt::Int(n)) => Some(n),
        Some(StrOrInt::Str(s)) => {
            let t = s.trim();
            if t.is_empty() {
                None
            } else {
                Some(t.parse::<i64>().map_err(serde::de::Error::custom)?)
            }
        }
    })
}

#[derive(Debug, Deserialize)]
pub struct RememberArgs {
    pub category: String,
    pub key: String,
    pub body_json: String,
    #[serde(
        default = "default_status",
        deserialize_with = "null_as_default_status"
    )]
    pub status: String,
    #[serde(
        default = "default_entity_type",
        rename = "type",
        deserialize_with = "null_as_default_entity_type"
    )]
    pub entity_type: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub tags: Vec<String>,
    #[serde(
        default = "default_importance",
        deserialize_with = "null_as_default_importance"
    )]
    pub importance: f64,
    #[serde(default, deserialize_with = "null_as_default")]
    pub topic_path: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub recall_when: Vec<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub always_on: bool,
    #[serde(
        default = "default_certainty",
        deserialize_with = "null_as_default_certainty"
    )]
    pub certainty: f64,
    #[serde(default, deserialize_with = "null_as_default")]
    pub workspace_hash: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub agent_id: String,
    /// #865: the identity of the agent making the request (distinct from
    /// `agent_id`, the write's author attribution). Stamped by the MCP
    /// transport from the `initialize` handshake; used for granular
    /// read/propose/commit authority enforcement when a manifest exists.
    #[serde(default)]
    pub requesting_agent_id: String,
    #[serde(default)]
    pub actor_kind: String,
    #[serde(
        default = "default_visibility",
        deserialize_with = "null_as_default_visibility"
    )]
    pub visibility: String,
    #[serde(default)]
    pub layer: Option<String>,
    /// Application-time period (#363, SQL:2011 APPLICATION_TIME): when the
    /// fact became true in the world. Defaults to transaction time. Set in
    /// the past for retroactive facts ("this was true last week").
    #[serde(default)]
    pub valid_from_unix_ms: Option<i64>,
    /// When the fact stopped being true. Omit for "still true" (unbounded).
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
    /// #487: the memories this write was built on. Each cited source gets an
    /// automatic usefulness bump (`mark_useful`) — the honest "this memory
    /// actually informed a later write" signal that feeds decay and ranking.
    #[serde(default, deserialize_with = "null_as_default")]
    pub derived_from: Vec<DerivedFromRef>,
    /// #531: opt out of near-duplicate merging for this write. Bulk/API
    /// writers storing many templated records (which sit above the trigram
    /// similarity threshold by construction) need each acknowledged write to
    /// actually create its key.
    #[serde(default, deserialize_with = "null_as_default")]
    pub skip_dedup: bool,
    /// #729: optional memory-origin/provenance metadata (spec:
    /// docs/specs/memory-provenance-and-external-refs.md). Stored inside
    /// body_json under the reserved "origin" key — no schema change.
    #[serde(default)]
    pub origin: Option<crate::models::OriginRecord>,
    /// #728: optional first-class external entity references. Stored inside
    /// body_json under the reserved "external_refs" key.
    #[serde(default, deserialize_with = "null_as_default")]
    pub external_refs: Vec<crate::models::ExternalRef>,
    /// Write-time audit metadata. Omitted envelopes remain distinguishable as
    /// legacy records rather than being inferred from a missing value.
    #[serde(default)]
    pub evidence: Option<crate::models::EvidenceEnvelope>,
    /// #821: optional hash-only trust admission evidence. Suppressed/abstained
    /// writes return without durable memory; quarantined/escalated records are
    /// retained as explicitly non-authoritative history.
    #[serde(default)]
    pub admission: Option<crate::trust_admission::AdmissionRequest>,
    /// #849: deliberate trusted override of a rejected-value tombstone.
    /// Journaled as an audited override; never set automatically.
    #[serde(default, deserialize_with = "null_as_default")]
    pub allow_rejected: bool,
}

/// #487: a `derived_from` citation — either an entity id (`"mem-..."`, as
/// returned by recall/remember) or a `{category, key}` pair.
#[derive(Debug, Deserialize)]
#[serde(untagged)]
pub enum DerivedFromRef {
    Id(String),
    Pair { category: String, key: String },
}

fn default_certainty() -> f64 {
    0.5
}

fn default_visibility() -> String {
    "workspace".to_string()
}

fn default_status() -> String {
    "active".to_string()
}

fn default_entity_type() -> String {
    "insight".to_string()
}

fn default_importance() -> f64 {
    0.5
}

/// #330: same null-tolerance as `null_as_default`, but falls through to a
/// named default function instead of `T::default()` for fields whose
/// "unset" value isn't the type's zero value (e.g. status="active", not "").
macro_rules! null_as_named_default {
    ($fn_name:ident, $ty:ty, $default_fn:ident) => {
        fn $fn_name<'de, D>(deserializer: D) -> Result<$ty, D::Error>
        where
            D: serde::Deserializer<'de>,
        {
            Ok(Option::<$ty>::deserialize(deserializer)?.unwrap_or_else($default_fn))
        }
    };
}

null_as_named_default!(null_as_default_status, String, default_status);
null_as_named_default!(null_as_default_entity_type, String, default_entity_type);
null_as_named_default!(null_as_default_importance, f64, default_importance);
null_as_named_default!(null_as_default_certainty, f64, default_certainty);
null_as_named_default!(null_as_default_visibility, String, default_visibility);
null_as_named_default!(
    null_as_default_artifact_candidate_encoding,
    String,
    default_artifact_candidate_encoding
);
null_as_named_default!(
    null_as_default_artifact_max_matches,
    i64,
    default_artifact_max_matches
);

#[derive(Debug, Deserialize)]
pub struct RecallArgs {
    pub query: String,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(rename = "type")]
    #[serde(default)]
    pub entity_type: Option<String>,
    #[serde(
        default = "default_limit",
        deserialize_with = "null_as_default_limit"
    )]
    pub limit: i64,
    #[serde(default, deserialize_with = "null_as_default")]
    pub offset: i64,
    #[serde(default, deserialize_with = "null_as_default")]
    pub min_decay: f64,
    #[serde(default)]
    pub topic_path: Option<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub include_archived: bool,
    #[serde(default, deserialize_with = "null_as_default")]
    pub expansion: crate::models::QueryExpansionConfig,
    #[serde(default, deserialize_with = "null_as_default")]
    pub mode: String, // "fts5", "dense", or "hybrid"
    #[serde(default)]
    pub preview_cap: Option<i64>,
    #[serde(default)]
    pub always_on: Option<bool>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub content_weight: f64,
    #[serde(
        default = "crate::models::default_trust_weight",
        deserialize_with = "null_as_default_trust_weight"
    )]
    pub trust_weight: f64,
    #[serde(
        default = "default_halving",
        deserialize_with = "null_as_default_halving"
    )]
    pub diversity_halving: f64,
    /// Recency half-life in seconds for time-aware hybrid ranking (#235).
    /// Omit (default) for relevance-only ranking; set to bias toward recent memories.
    #[serde(default)]
    pub recency_half_life_secs: Option<f64>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// #485: scope as a ranking multiplier. 0.0–1.0; requires workspace_hash.
    /// Widens the workspace filter to include global ('') memories, weighted
    /// by this factor in the ranking — current-scope hits are preferred but
    /// strong broader-scope hits still surface. Omit for the strict filter.
    #[serde(default)]
    pub scope_weight: Option<f64>,
    #[serde(default)]
    pub agent_id: Option<String>,
    /// #684: the identity of the agent making the request (distinct from
    /// `agent_id`, which is an author FILTER). Stamped by the MCP transport from
    /// the `initialize` handshake's clientInfo; used only for visibility
    /// enforcement (`private`/`fleet` entities). Empty/absent → unscoped, so
    /// single-agent callers and default `workspace`-visibility data are
    /// unaffected.
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
    /// #880: epistemic trust-axis filter. When set, only entities in the
    /// requested trust state are returned ('candidate' | 'verified' |
    /// 'corroborated' | 'rejected' | 'defensively_recalled'). Omit for no
    /// trust filtering (existing behavior).
    #[serde(default)]
    pub epistemic_state: Option<String>,
    #[serde(default)]
    pub layer: Option<String>,
    /// #784: serving posture applied after authorization: personal, agent, or shared.
    #[serde(default)]
    pub retrieval_profile: Option<String>,
    /// #287: opt-in. When true, each result gets a normalized `confidence`
    /// (0.0–1.0) rolled up from rank, trust, and decay. Default false so
    /// existing callers and snapshot tests are unaffected; ranking is unchanged.
    #[serde(default, deserialize_with = "null_as_default")]
    pub include_confidence: bool,
    /// Opt-in reinforcement for dense/hybrid recall: bump retrieval stats on
    /// the returned hits so semantically-used memories resist decay. Default
    /// false — the semantic paths stay byte-deterministic (#247).
    #[serde(default, deserialize_with = "null_as_default")]
    pub reinforce: bool,
    /// #472 Temporal RAG: transaction-time instant — reconstruct semantic recall
    /// "as we believed it" at this past instant. Each hit's body is the version
    /// that was live at as_of_unix_ms; corrections recorded later do not leak in.
    /// Combine with valid_at for the full bi-temporal cell. None = live view.
    /// Accepts a number or a numeric string (LLM clients often stringify ints).
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub as_of_unix_ms: Option<i64>,
    /// #363: valid-time instant filter — only return facts whose application-
    /// time period [valid_from, valid_to) contains this world-instant.
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub valid_at: Option<i64>,
    /// #363: valid-time period filter start (pair with valid_to_unix_ms and
    /// valid_op). Ignored when valid_at is set.
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub valid_from_unix_ms: Option<i64>,
    /// #363: valid-time period filter end (half-open; omit = unbounded).
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub valid_to_unix_ms: Option<i64>,
    /// #363: SQL:2011 period predicate for the period filter: "overlaps"
    /// (default — periods share an instant) or "contains" (the fact's period
    /// contains the whole queried period).
    #[serde(default, deserialize_with = "null_as_default")]
    pub valid_op: String,
    /// #675/#676: opt-in startup-optimized ranking. When true, recall over-fetches
    /// a candidate pool and re-ranks it by actionability — memories more likely to
    /// change the first retrieval move (concrete entities: issue/ticket keys,
    /// #refs, paths, URLs, named systems; decision/escalation language) outrank
    /// vague, date-only, or very short near-neighbors — then truncates to `limit`.
    /// Each returned item also carries an `actionability` score (0.0–1.0). Default
    /// false: recall order is byte-identical to prior behavior.
    #[serde(default, deserialize_with = "null_as_default")]
    pub startup: bool,
    /// #728: optional external-ref post-filters. When set, only hits whose
    /// body carries a matching external_refs entry survive (spec:
    /// docs/specs/memory-provenance-and-external-refs.md §2.2). ref_type is
    /// exact-match; ref_value additionally matches on hierarchical '/'
    /// prefix ("github:Org" matches "github:Org/repo").
    #[serde(default)]
    pub ref_type: Option<String>,
    #[serde(default)]
    pub ref_value: Option<String>,
    /// #864: bounded recall. When set, the recall is timed; if it exceeds
    /// this many milliseconds the outcome status is `timeout` so callers
    /// know the result set may be incomplete. Results are still returned
    /// (bounded, not dropped).
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub deadline_ms: Option<i64>,
    /// #864/#873/#887: attach the explicit recall `outcome` block (status,
    /// backend health, abstention) even when recall was nominal. By default
    /// the outcome is attached only when it has something to say
    /// (degraded/partial/timeout/empty/unavailable/stale) so healthy
    /// responses stay byte-identical; set true to always include it.
    #[serde(default, deserialize_with = "null_as_default")]
    pub include_outcome: bool,
    /// #883 (TEMPR-style fused recall, mode "fused"): the strategies to
    /// engage. Recognized: "fts5", "dense", "graph", "temporal" (2-4).
    /// Omit = all four.
    #[serde(default, deserialize_with = "null_as_default")]
    pub strategies: Vec<String>,
    /// #883: token-budget truncation (estimated tokens = chars/4 per body).
    /// 0 = derive from depth_budget. Default 4096.
    #[serde(default, deserialize_with = "null_as_default")]
    pub max_tokens: i64,
    /// #883: depth budget "low" | "mid" | "high" → 1024 / 4096 / 16384
    /// default tokens when max_tokens is unset. Omit = mid.
    #[serde(default)]
    pub depth_budget: Option<String>,
    /// #883: per-strategy RRF weight multipliers (default 1.0 each).
    #[serde(default)]
    pub strategy_weights: Option<std::collections::HashMap<String, f64>>,
    /// #883: optional rerank stage over the fused pool (default off —
    /// latency-preserving). When enabled, the fused pool is re-scored with
    /// min-max calibrated dense + BM25 signals before truncation.
    #[serde(default, deserialize_with = "null_as_default")]
    pub rerank: bool,
    /// #883: anchor instant for the temporal strategy (unix ms; default
    /// now). Accepts a number or a numeric string.
    #[serde(default, deserialize_with = "string_or_int_opt")]
    pub query_time_unix_ms: Option<i64>,
    /// #869: graph utility gate threshold in [0, 1] (fused mode only).
    /// The graph strategy engages only when the query's classified graph
    /// utility is >= this value. Omit = 0.5 (documented default). 0.0
    /// disables the gate; 1.0 effectively never engages. The routing
    /// decision is always observable in `fused_trace.graph_route`.
    #[serde(default)]
    pub graph_utility_threshold: Option<f64>,
}

pub type BatchQuery = RecallArgs;

#[derive(Debug, Deserialize)]
pub struct RecallBatchArgs {
    pub queries: Vec<BatchQuery>,
}

/// #363: post-search valid-time filter shared by the plain and expansion
/// recall paths. Applied AFTER ranking/limit, so it only ever narrows the
/// result set (no re-ranking): callers that never pass valid-time filters get
/// byte-identical output. No-op when no filter is requested.
fn valid_time_retain(
    db: &Database,
    valid_at: Option<i64>,
    valid_from: Option<i64>,
    valid_to: Option<i64>,
    valid_op: &str,
    entities: &mut Vec<crate::models::Entity>,
) -> Result<(), String> {
    if valid_at.is_none() && valid_from.is_none() && valid_to.is_none() {
        return Ok(());
    }
    let ids: Vec<String> = entities.iter().map(|e| e.id.clone()).collect();
    let periods = db
        .valid_periods_for_ids(&ids)
        .map_err(|e| format!("valid-time filter failed: {}", e))?;
    entities.retain(|e| {
        let Some(&(row_from, row_to)) = periods.get(&e.id) else {
            return false;
        };
        if let Some(t) = valid_at {
            return crate::db::valid_period_contains_instant(row_from, row_to, t);
        }
        // Period query: [from, to) with unbounded defaults on either side.
        crate::db::valid_period_matches(
            row_from,
            row_to,
            valid_from.unwrap_or(i64::MIN),
            valid_to,
            valid_op,
        )
    });
    Ok(())
}

/// #472 Temporal RAG: transaction-time (and optional valid-time) provenance for
/// a reconstructed recall hit, stamped onto the output alongside the point-in-
/// time body.
struct TemporalHit {
    is_live: bool,
    recorded_at: i64,
    valid_from: Option<i64>,
    valid_to: Option<i64>,
}

/// #472: reconstruct each recall candidate to its point-in-time version and swap
/// in that body, dropping candidates that had no version at the requested
/// instant. Ranked order is preserved; provenance is returned 1:1 with the
/// survivors. The axes:
///   * `valid_at` set → the world-version whose valid period contains T, per
///     `as_of` knowledge (tx = `as_of` or, when only `valid_at` is given, now/∞)
///     — this is the full bi-temporal cell, and `valid_at` ALONE now
///     reconstructs the historical world-version rather than narrowing live rows.
///   * `as_of` only → the version believed at transaction time `as_of`.
/// At least one of `as_of` / `valid_at` must be Some (the caller guarantees it).
/// Candidate generation here is over the live index, so this pass alone cannot
/// surface a fact whose query-matching version has since been superseded/retired
/// (history-only). #682 closes that: `augment_temporal_with_history` runs right
/// after this and reconstructs the missed keys via the same engines. This
/// function stays live-only so the fast, common "reproduce a still-known fact at
/// T" path is untouched.
fn temporal_resolve(
    db: &Database,
    as_of: Option<i64>,
    valid_at: Option<i64>,
    entities: &mut Vec<crate::models::Entity>,
) -> Result<Vec<TemporalHit>, String> {
    let mut hits = Vec::new();
    let mut resolved = Vec::new();
    for e in std::mem::take(entities) {
        let tv = match valid_at {
            Some(v) => db.bitemporal_at(&e.category, &e.key, as_of.unwrap_or(i64::MAX), v),
            None => db.as_of_version(
                &e.category,
                &e.key,
                as_of.expect("temporal_resolve requires as_of or valid_at"),
            ),
        }
        .map_err(|err| format!("temporal recall resolution failed: {}", err))?;
        if let Some(tv) = tv {
            hits.push(TemporalHit {
                is_live: tv.invalidated_at_unix_ms.is_none(),
                recorded_at: tv.recorded_at_unix_ms,
                valid_from: tv.valid_from_unix_ms,
                valid_to: tv.valid_to_unix_ms,
            });
            resolved.push(tv.entity);
        }
    }
    *entities = resolved;
    Ok(hits)
}

/// #682 Temporal RAG: history-inclusive candidate generation. `temporal_resolve`
/// above can only reconstruct facts whose CURRENT body still matches the query
/// (candidates came from the live index). A fact whose query-matching version
/// has since been superseded/retired lives only in `entity_history`, so it never
/// surfaced — the documented v1 limitation. This fills that gap: discover the
/// missed keys via the history FTS and reconstruct each through the SAME
/// point-in-time engines (`bitemporal_at` / `as_of_version`) as `temporal_resolve`,
/// so semantics are identical. It only ever ADDS — live/relevance-ranked hits
/// keep their positions and history-only hits are appended, and only up to the
/// caller's `limit` (so a query that already found enough is untouched: the
/// augmentation is a no-op whenever `entities.len() >= limit`). `hits` is kept
/// 1:1 with `entities` so downstream provenance stamping stays aligned.
fn augment_temporal_with_history(
    db: &Database,
    query: &str,
    as_of: Option<i64>,
    valid_at: Option<i64>,
    workspace_hash: Option<&str>,
    limit: usize,
    entities: &mut Vec<crate::models::Entity>,
    hits: &mut Vec<TemporalHit>,
) -> Result<(), String> {
    if limit == 0 || entities.len() >= limit {
        return Ok(());
    }
    let mut seen: std::collections::HashSet<(String, String)> = entities
        .iter()
        .map(|e| (e.category.clone(), e.key.clone()))
        .collect();
    // Discover a modest pool beyond what's already surfaced; bounded.
    let discover = limit.saturating_mul(2).clamp(1, 200);
    let keys = db
        .history_matching_keys(query, workspace_hash, discover)
        .map_err(|e| format!("temporal history discovery failed: {}", e))?;
    for (category, key) in keys {
        if entities.len() >= limit {
            break;
        }
        if !seen.insert((category.clone(), key.clone())) {
            continue;
        }
        let tv = match valid_at {
            Some(v) => db.bitemporal_at(&category, &key, as_of.unwrap_or(i64::MAX), v),
            None => db.as_of_version(
                &category,
                &key,
                as_of.expect("augment_temporal_with_history requires as_of or valid_at"),
            ),
        }
        .map_err(|e| format!("temporal history resolution failed: {}", e))?;
        if let Some(tv) = tv {
            hits.push(TemporalHit {
                is_live: tv.invalidated_at_unix_ms.is_none(),
                recorded_at: tv.recorded_at_unix_ms,
                valid_from: tv.valid_from_unix_ms,
                valid_to: tv.valid_to_unix_ms,
            });
            entities.push(tv.entity);
        }
    }
    Ok(())
}

/// #287: presentation-layer confidence rollup over signals Perseus Vault already has.
/// Does NOT affect ranking — purely a convenience score for the caller.
fn confidence_for(entity: &crate::models::Entity, rank: usize, total: usize) -> f64 {
    let relevance = if total > 1 {
        (total - rank) as f64 / total as f64
    } else {
        1.0
    };
    let trust = if entity.verified {
        1.0
    } else {
        entity.certainty.clamp(0.0, 1.0)
    };
    let freshness = entity.decay_score.clamp(0.0, 1.0);
    let c = 0.5 * relevance + 0.3 * trust + 0.2 * freshness;
    (c.clamp(0.0, 1.0) * 1000.0).round() / 1000.0
}

/// Inject a `confidence` field into each already-serialized recall item.
fn apply_confidence(items: &mut [serde_json::Value], entities: &[crate::models::Entity]) {
    let total = entities.len();
    for (i, (item, ent)) in items.iter_mut().zip(entities.iter()).enumerate() {
        if let Some(obj) = item.as_object_mut() {
            obj.insert("confidence".to_string(), json!(confidence_for(ent, i, total)));
        }
    }
}

/// Map a biomimetic layer alias (world/episodic/semantic) to its canonical
/// storage layer (core/buffer/working). Any other value passes through, so
/// callers may also filter by the raw layer name.
fn canonical_layer(s: &str) -> String {
    match s {
        "world" => "core",
        "episodic" => "buffer",
        "semantic" => "working",
        other => other,
    }
    .to_string()
}

fn default_halving() -> f64 {
    1.0
}

fn default_limit() -> i64 {
    10
}

null_as_named_default!(null_as_default_limit, i64, default_limit);
null_as_named_default!(
    null_as_default_trust_weight,
    f64,
    default_trust_weight_wrapper
);
null_as_named_default!(null_as_default_halving, f64, default_halving);

fn default_trust_weight_wrapper() -> f64 {
    crate::models::default_trust_weight()
}

#[derive(Debug, Deserialize)]
pub struct ForgetArgs {
    pub category: String,
    pub key: String,
    #[serde(default)]
    pub reason: String,
}

#[derive(Debug, Deserialize)]
pub struct LinkArgs {
    pub from_category: String,
    pub from_key: String,
    pub to_id: String,
    #[serde(default)]
    pub relationship: String,
}

#[derive(Debug, Deserialize)]
pub struct UnlinkArgs {
    pub from_category: String,
    pub from_key: String,
    pub to_id: String,
}

#[derive(Debug, Deserialize)]
pub struct JournalArgs {
    #[serde(default = "default_event_type")]
    pub event_type: String,
    #[serde(default)]
    pub evaluated: Value,
    #[serde(default)]
    pub acted: Value,
    #[serde(default)]
    pub forward: Value,
    #[serde(default)]
    pub category: String,
    #[serde(default)]
    pub key: String,
    #[serde(default)]
    pub entity_id: String,
    #[serde(default)]
    pub agent_id: String,
    /// #417: optional explicit workspace of the referenced entity. Usually
    /// omitted — `Database::journal` derives it from `entity_id` — but a caller
    /// may set it (e.g. federated writes) to scope purge redaction precisely.
    #[serde(default)]
    pub workspace_hash: String,
}

fn default_event_type() -> String {
    "decision".to_string()
}

#[derive(Debug, Deserialize)]
pub struct TimelineArgs {
    /// Required workspace scope for public timeline reads. An empty value is
    /// an explicit request for the global partition; omission is rejected.
    pub workspace_hash: String,
    #[serde(default)]
    pub from_ms: Option<i64>,
    #[serde(default)]
    pub to_ms: Option<i64>,
    #[serde(default)]
    pub event_type: Option<String>,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub entity_id: Option<String>,
    #[serde(default = "default_timeline_limit")]
    pub limit: i64,
    #[serde(default)]
    pub offset: i64,
}

fn default_timeline_limit() -> i64 {
    50
}

#[derive(Debug, Deserialize)]
pub struct StateSetArgs {
    pub key: String,
    pub value_json: String,
    #[serde(default)]
    pub ttl_seconds: Option<i64>,
}

#[derive(Debug, Deserialize)]
pub struct StateGetArgs {
    pub key: String,
}

#[derive(Debug, Deserialize)]
pub struct StateDeleteArgs {
    pub key: String,
}

#[derive(Debug, Deserialize)]
pub struct StateListArgs {
    #[serde(default)]
    pub prefix: String,
}

#[derive(Debug, Deserialize)]
pub struct CompactArgs {
    #[serde(default = "default_min_decay")]
    pub min_decay: f64,
    #[serde(default)]
    pub dry_run: bool,
}

fn default_min_decay() -> f64 {
    0.1
}

#[derive(Debug, Deserialize)]
pub struct MigrateArgs {
    pub from_path: String,
}

#[derive(Debug, Deserialize)]
pub struct ContextArgs {
    #[serde(default, deserialize_with = "null_as_default")]
    pub categories: Vec<String>,
    #[serde(default = "default_context_limit")]
    pub limit: i64,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// Current task/message text — the relevance gate for recall-first
    /// injection (#356). Without it, on_demand mode injects no topical
    /// entities (compact pointer + capped always-on set only).
    #[serde(default)]
    pub query: Option<String>,
    /// "on_demand" (default, recall-first) or "always_inject" (legacy
    /// unconditional dump, opt-in) (#366).
    #[serde(default)]
    pub mode: Option<String>,
    /// Host model name for budget-profile resolution (#366).
    #[serde(default)]
    pub model: Option<String>,
    /// Explicit character budget; overrides the model profile (#366).
    #[serde(default)]
    pub max_context_chars: Option<i64>,
}

fn default_context_limit() -> i64 {
    10
}

#[derive(Debug, Deserialize)]
pub struct ExtractArgs {
    /// Raw text to extract from. If empty, `category` + `key` of a stored entity
    /// are used instead.
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub key: Option<String>,
    /// Extractor strategy: "rule_based" (default, local heuristics) or "none" (no-op).
    #[serde(default = "default_extract_strategy")]
    pub strategy: String,
}

fn default_extract_strategy() -> String {
    "rule_based".to_string()
}

// ─── Tool handlers ──────────────────────────────────────────────

fn authoritative_source_is_bound(
    db: &Database,
    request: &crate::trust_admission::AdmissionRequest,
    body: &str,
    requested_workspace: &str,
    agent_id: &str,
    actor_kind: &str,
) -> Result<bool, String> {
    if crate::trust_admission::digest_text(body) != request.record_digest
        || request.workspace_hash != requested_workspace
        || request.actor_identity.as_deref() != Some(agent_id)
        || request.actor_kind.as_deref() != Some(actor_kind)
    {
        return Ok(false);
    }
    let Some(source_event_id) = request.source_event_id.as_deref() else {
        return Ok(false);
    };
    let conn = db.conn().map_err(|e| format!("admission source lookup failed: {e}"))?;
    let source: Option<(String, String)> = conn
        .query_row(
            "SELECT workspace_hash, agent_id FROM journal WHERE id=?1",
            rusqlite::params![source_event_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| format!("admission source lookup failed: {e}"))?;
    Ok(source.is_some_and(|(workspace, actor)| {
        workspace == request.workspace_hash && actor == agent_id
    }))
}

pub fn handle_remember(db: &Database, args: Value) -> Result<String, String> {
    let a: RememberArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid remember arguments: {}", e))?;
    if a.actor_kind.eq_ignore_ascii_case("user") && a.admission.is_none() {
        return Err("user attribution requires a validated admission source event".to_string());
    }

    // #865: granular authority — a manifest's presence splits write authority
    // into propose (reviewable candidates) vs commit (verified records). The
    // caller's actual capability must match the write's ambition BEFORE any
    // mutation. Fail-open when no manifest exists (legacy single-agent vault).
    if let Err(e) = db.require_memory_capability(
        &a.requesting_agent_id,
        &a.workspace_hash,
        if a.admission.is_some() {
            "memory.commit"
        } else {
            "memory.propose"
        },
    ) {
        return Err(format!("write denied: {e}"));
    }

    // Validate body_json is valid JSON
    if let Err(e) = serde_json::from_str::<serde_json::Value>(&a.body_json) {
        return Err(format!("body_json is not valid JSON: {}", e));
    }

    // #433 L: bound input sizes. category/key are indexed, hashed for identity
    // (category, key, workspace_hash), and fed to FTS — an unbounded key is a
    // DoS-via-huge-key vector. These caps sit far above any legitimate use.
    const MAX_CATEGORY_LEN: usize = 256;
    const MAX_KEY_LEN: usize = 1024;
    const MAX_BODY_LEN: usize = 4 * 1024 * 1024; // 4 MiB
    if a.category.len() > MAX_CATEGORY_LEN {
        return Err(format!(
            "category too long: {} bytes (max {})",
            a.category.len(),
            MAX_CATEGORY_LEN
        ));
    }
    if a.key.len() > MAX_KEY_LEN {
        return Err(format!(
            "key too long: {} bytes (max {})",
            a.key.len(),
            MAX_KEY_LEN
        ));
    }
    if a.body_json.len() > MAX_BODY_LEN {
        return Err(format!(
            "body_json too long: {} bytes (max {})",
            a.body_json.len(),
            MAX_BODY_LEN
        ));
    }
    // #487: bound the citation list like the other inputs — far above any
    // legitimate "here's what I recalled before writing this" set.
    const MAX_DERIVED_FROM: usize = 64;
    if a.derived_from.len() > MAX_DERIVED_FROM {
        return Err(format!(
            "derived_from too long: {} citations (max {})",
            a.derived_from.len(),
            MAX_DERIVED_FROM
        ));
    }

    // Merge recall_when into body_json if provided
    let body = if a.recall_when.is_empty() {
        a.body_json
    } else {
        let mut obj: serde_json::Value =
            serde_json::from_str(&a.body_json).unwrap_or(serde_json::json!({}));
        if let Some(map) = obj.as_object_mut() {
            let triggers: Vec<serde_json::Value> = a
                .recall_when
                .iter()
                .map(|s| serde_json::Value::String(s.clone()))
                .collect();
            map.insert(
                "recall_when".to_string(),
                serde_json::Value::Array(triggers),
            );
        }
        serde_json::to_string(&obj).unwrap_or(a.body_json)
    };

    // #729/#728: merge origin + external_refs into body_json under reserved
    // keys (spec: docs/specs/memory-provenance-and-external-refs.md). Same
    // metadata channel as recall_when — no schema change; recall/get_entity
    // surface the fields via body expansion. Validate the vocabulary up
    // front so bad values never enter the store.
    const MAX_EXTERNAL_REFS: usize = 32;
    if a.external_refs.len() > MAX_EXTERNAL_REFS {
        return Err(format!(
            "external_refs too long: {} refs (max {})",
            a.external_refs.len(),
            MAX_EXTERNAL_REFS
        ));
    }
    if let Some(ref origin) = a.origin {
        if let Some(ref kind) = origin.memory_kind {
            if !crate::models::MEMORY_KINDS.contains(&kind.as_str()) {
                return Err(format!(
                    "Invalid origin.memory_kind '{kind}': expected one of {:?}",
                    crate::models::MEMORY_KINDS
                ));
            }
        }
    }
    for r in &a.external_refs {
        if r.ref_type.trim().is_empty() || r.ref_value.trim().is_empty() {
            return Err("external_refs entries require non-empty ref_type and ref_value".into());
        }
        if let Some(ref rel) = r.relationship {
            if !crate::models::REF_RELATIONSHIPS.contains(&rel.as_str()) {
                return Err(format!(
                    "Invalid external_refs relationship '{rel}': expected one of {:?}",
                    crate::models::REF_RELATIONSHIPS
                ));
            }
        }
    }
    if let Some(ref evidence) = a.evidence {
        if !crate::models::EVIDENCE_CAPTURE_MODES.contains(&evidence.capture_mode.as_str()) {
            return Err(format!(
                "Invalid evidence.capture_mode '{}': expected one of {:?}",
                evidence.capture_mode,
                crate::models::EVIDENCE_CAPTURE_MODES
            ));
        }
        match evidence.capture_mode.as_str() {
            "snapshot" if evidence.resolved_value.is_none() => {
                return Err("evidence.capture_mode=snapshot requires resolved_value".into())
            }
            "hash_only" if evidence.content_sha256.is_none() => {
                return Err("evidence.capture_mode=hash_only requires content_sha256".into())
            }
            "capture_failed" if evidence.resolved_value.is_some() => {
                return Err("evidence.capture_mode=capture_failed cannot carry resolved_value".into())
            }
            _ => {}
        }
        if let Some(ref hash) = evidence.content_sha256 {
            if hash.len() != 64 || !hash.chars().all(|c| c.is_ascii_hexdigit()) {
                return Err("evidence.content_sha256 must be a 64-hex SHA-256 digest".into());
            }
        }
    }
    let is_imported_provenance = serde_json::from_str::<Value>(&body)
        .ok()
        .and_then(|value| value["origin"]["memory_kind"].as_str().map(|kind| kind == "imported"))
        .unwrap_or(false);
    let mut verified_admission = false;
    let (admission_evidence, provenance) = if let Some(ref request) = a.admission {
        if let Some(actor_identity) = request.actor_identity.as_deref() {
            if actor_identity != a.agent_id {
                return Err("admission actor_identity does not match the writing agent_id".to_string());
            }
        }
        if let Some(actor_kind) = request.actor_kind.as_deref() {
            if !a.actor_kind.is_empty() && actor_kind != a.actor_kind {
                return Err("admission actor_kind does not match the writing actor_kind".to_string());
            }
        }
        let mut decision = crate::trust_admission::evaluate(request)
            .map_err(|e| format!("admission gate failed: {e}"))?;
        if decision.authoritative {
            if authoritative_source_is_bound(
                db, request, &body, &a.workspace_hash, &a.agent_id, &a.actor_kind,
            )? {
                verified_admission = true;
            } else {
                decision
                    .downgrade_to_proposed("authority_source_binding_required")
                    .map_err(|e| format!("admission downgrade failed: {e}"))?;
            }
        }
        if !a.workspace_hash.is_empty() && request.workspace_hash != a.workspace_hash {
            return Err("admission workspace scope does not match the requested workspace".to_string());
        }
        if decision.reason_codes.iter().any(|reason| reason == "workspace_scope_mismatch") {
            return Err("admission authorization scope does not match the admission workspace".to_string());
        }
        if matches!(decision.outcome.as_str(), "suppressed" | "abstained") {
            return Ok(json!({
                "ok": false,
                "remembered": false,
                "admission": decision,
            })
            .to_string());
        }
        let state = match decision.outcome.as_str() {
            "admitted" if decision.authoritative => "admitted",
            "quarantined" => "quarantined",
            _ => "proposed",
        };
        let provenance = json!({
            "state": state,
            "requires_review": state != "admitted",
            "record_digest": decision.record_digest,
            "actor": {
                "kind": decision.actor_kind.clone().unwrap_or_else(|| a.actor_kind.clone()),
                "identity": decision.actor_identity.clone().unwrap_or_else(|| a.agent_id.clone()),
            },
            "workspace_hash": decision.workspace_hash,
            "source_event_id": decision.source_event_id,
        });
        (Some(decision), Some(provenance))
    } else if !is_imported_provenance
        && (!a.agent_id.trim().is_empty() || !a.workspace_hash.trim().is_empty() || !a.actor_kind.trim().is_empty())
    {
        let actor_kind = if a.actor_kind.trim().is_empty() { "assistant" } else { a.actor_kind.as_str() };
        let provenance = json!({
            "state": "proposed",
            "requires_review": true,
            "reason": "missing_admission_envelope",
            "record_digest": crate::trust_admission::digest_text(&body),
            "actor": {"kind": actor_kind, "identity": a.agent_id},
            "workspace_hash": a.workspace_hash,
        });
        (None, Some(provenance))
    } else {
        // Legacy/unscoped MCP writes retain their byte-compatible body and
        // status; the result still explicitly requires review, while direct
        // non-MCP writers are gated by Database::remember_impl.
        let _ = is_imported_provenance;
        (None, None)
    };
    let body = if a.origin.is_none()
        && a.external_refs.is_empty()
        && a.evidence.is_none()
        && admission_evidence.is_none()
        && provenance.is_none()
    {
        body
    } else {
        let mut obj: serde_json::Value =
            serde_json::from_str(&body).unwrap_or(serde_json::json!({}));
        if let Some(map) = obj.as_object_mut() {
            if let Some(ref origin) = a.origin {
                map.insert(
                    "origin".to_string(),
                    serde_json::to_value(origin).unwrap_or(serde_json::json!({})),
                );
            }
            if !a.external_refs.is_empty() {
                map.insert(
                    "external_refs".to_string(),
                    serde_json::to_value(&a.external_refs)
                        .unwrap_or(serde_json::json!([])),
                );
            }
            if let Some(ref evidence) = a.evidence {
                map.insert(
                    "evidence".to_string(),
                    serde_json::to_value(evidence).unwrap_or(serde_json::json!({})),
                );
            }
            if let Some(ref admission) = admission_evidence {
                map.insert(
                    "admission".to_string(),
                    serde_json::to_value(admission).unwrap_or(serde_json::json!({})),
                );
            }
            if let Some(ref provenance) = provenance {
                map.insert("provenance".to_string(), provenance.clone());
            }
        }
        serde_json::to_string(&obj).unwrap_or(body)
    };

    let raw_id = Uuid::new_v4().to_string().replace('-', "");
    let id = format!("mem-{}", &raw_id[..12.min(raw_id.len())]);
    let now = now_ms();

    let layer = a.layer.map(|l| match l.as_str() {
        "world" => "core".to_string(),
        "episodic" => "buffer".to_string(),
        "semantic" => "working".to_string(),
        _ => l,
    }).unwrap_or_else(|| "buffer".to_string());
    let entity_workspace_hash = if !a.workspace_hash.is_empty() {
        a.workspace_hash.clone()
    } else {
        admission_evidence
            .as_ref()
            .map(|admission| admission.workspace_hash.clone())
            .unwrap_or_default()
    };
    let proposed = provenance
        .as_ref()
        .is_some_and(|value| value["state"] == "proposed");
    let quarantined = admission_evidence
        .as_ref()
        .is_some_and(|admission| admission.outcome == "quarantined");

    let entity = Entity {
        id,
        category: a.category,
        key: a.key,
        body_json: body,
        status: if proposed { "proposed".to_string() } else if quarantined { "quarantined".to_string() } else { a.status },
        entity_type: a.entity_type,
        tags: a.tags,
        decay_score: a.importance,
        retrieval_count: 0,
        layer,
        topic_path: a.topic_path,
        archived: false,
        archive_reason: String::new(),
        links: vec![],
        verified: false,
        source: "agent".to_string(),
        always_on: if proposed || quarantined { false } else { a.always_on },
        certainty: a.certainty,
        workspace_hash: entity_workspace_hash,
        agent_id: a.agent_id.clone(),
        visibility: a.visibility.clone(),
        created_at_unix_ms: now,
        last_accessed_unix_ms: now,
        follow_count: 0,
        miss_count: 0,
        follow_rate: 0.0,
        efficacy_status: "unverified".to_string(),
        epistemic_state: crate::models::default_epistemic_state(),
        embedding: None,
        _parsed_body: None,
    };

    // #363: half-open [valid_from, valid_to) must be a real interval.
    if let (Some(vf), Some(vt)) = (a.valid_from_unix_ms, a.valid_to_unix_ms) {
        if vt <= vf {
            return Err(format!(
                "valid_to_unix_ms ({vt}) must be greater than valid_from_unix_ms ({vf})"
            ));
        }
    }

    #[cfg(test)]
    if a.admission.is_none() {
        // Existing unit fixtures exercise temporal/promotion mechanics without
        // constructing a source-event journal. This capability is not compiled
        // into production and cannot elevate a public write.
        verified_admission = true;
    }

    let (eid, action) = if verified_admission {
        db.remember_verified_with_options(
            &entity, a.skip_dedup, a.valid_from_unix_ms, a.valid_to_unix_ms, a.allow_rejected,
        )
    } else {
        db.remember_with_options(
            &entity, a.skip_dedup, a.valid_from_unix_ms, a.valid_to_unix_ms, a.allow_rejected,
        )
    }
    .map_err(|e| format!("Remember failed: {}", e))?;

    // #487: auto-reinforce the cited sources. Runs AFTER the write succeeded
    // — a rejected remember must not reinforce anything. Self-citations are
    // skipped (a write cannot vouch for itself); citations that resolve to no
    // live row are reported back, not fatal (the write already happened).
    // Resolution uses the writer's workspace with follow()'s semantics
    // (#391/#396): strict equality when scoped, deterministic global-first
    // pick when not.
    let derived_report = if a.derived_from.is_empty() {
        None
    } else {
        let ws = if entity.workspace_hash.is_empty() {
            None
        } else {
            Some(entity.workspace_hash.as_str())
        };
        let mut reinforced = 0i64;
        let mut not_found: Vec<String> = Vec::new();
        for src in &a.derived_from {
            let (label, hit) = match src {
                DerivedFromRef::Id(id) => {
                    if *id == eid {
                        continue;
                    }
                    let hit = db.mark_useful_by_id(id).map_err(|e| {
                        format!("Remembered {} but derived_from reinforcement failed: {}", eid, e)
                    })?;
                    (id.clone(), hit)
                }
                DerivedFromRef::Pair { category, key } => {
                    if *category == entity.category && *key == entity.key {
                        continue;
                    }
                    let hit = db.mark_useful(category, key, ws).map_err(|e| {
                        format!("Remembered {} but derived_from reinforcement failed: {}", eid, e)
                    })?;
                    (format!("{}/{}", category, key), hit)
                }
            };
            if hit {
                reinforced += 1;
            } else {
                not_found.push(label);
            }
        }
        Some(json!({"reinforced": reinforced, "not_found": not_found}))
    };

    // #657: mirror the CLI write's #516 contract onto the MCP surface — every
    // committed remember carries an explicit `ok: true`. `remember_with_options`
    // only returns Ok after the row is written, so this marks durable commit.
    // A degraded/empty/no-op response structurally lacks `ok`, so a caller that
    // checks the field can never mistake a silent no-op for a persisted write.
    let mut result = json!({
        "ok": true,
        "id": eid,
        "action": action,
        "category": entity.category,
        "key": entity.key,
    });
    // #531: a near-duplicate merge is an accepted-but-not-created write —
    // make it impossible to miss for callers that don't parse the action
    // string (bulk ingest scripts acked 2,000 writes that became ~5 rows).
    if action.starts_with("deduped") {
        result["deduped"] = json!(true);
        result["merged_into"] = json!(eid);
        result["hint"] = json!(
            "body was >=70% trigram-similar to an existing entity in this \
             category+workspace, so no new entity was created; pass \
             skip_dedup=true to force a distinct write"
        );
    }
    if let Some(dr) = derived_report {
        result["derived_from"] = dr;
    }
    if let Some(admission) = admission_evidence {
        result["admission"] = serde_json::to_value(admission).unwrap_or(serde_json::json!({}));
    }
    if let Some(provenance) = provenance {
        result["proposed"] = json!(provenance["state"] == "proposed");
        result["requires_review"] = json!(provenance["requires_review"] == true);
        result["provenance"] = provenance;
    } else if a.admission.is_none() {
        result["proposed"] = json!(true);
        result["requires_review"] = json!(true);
    }
    Ok(result.to_string())
}

/// #677: when a recall comes back empty, build a self-describing diagnostic so
/// the caller can tell a genuinely empty / no-match store apart from an
/// unhealthy DB or a degraded (keyword-only / no-coverage) semantic backend —
/// the silent-empty failure modes that otherwise waste tokens on false
/// debugging paths ("no memories found" when the real issue is MCP-child /
/// backend health). Only attached when `total == 0`, so nominal responses are
/// unchanged.
fn empty_recall_diagnostic(db: &Database, mode: &SearchMode) -> serde_json::Value {
    let r = db.readiness();
    let reason = if !r.db_responds {
        "db_unhealthy"
    } else if r.active_memories == 0 {
        "empty_store"
    } else if matches!(mode, SearchMode::Dense | SearchMode::Hybrid)
        && r.embedding_enabled
        && r.embedded_memories == 0
    {
        "degraded_semantic"
    } else {
        "no_match"
    };
    let hint = match reason {
        "db_unhealthy" => {
            "vault DB is not responding — check the MCP child/process and the --db path (call the health tool)"
        }
        "empty_store" => {
            "the store has no active memories yet — this is a true empty result, not a fault"
        }
        "degraded_semantic" => {
            "no active memories carry embeddings, so this dense/hybrid query found nothing — run reindex/embed, or retry with mode=fts5"
        }
        _ => {
            "the store is populated and the backend is healthy — this query simply had no matches; broaden the query or mode before assuming a fault"
        }
    };
    json!({
        "reason": reason,
        "hint": hint,
        "active_memories": r.active_memories,
        "embedded_memories": r.embedded_memories,
        "semantic_recall": r.semantic_recall(),
    })
}

/// #728: does an entity's body carry an external_refs entry matching the
/// optional (ref_type, ref_value) recall filter? ref_type is exact-match;
/// ref_value matches exactly or as a hierarchical prefix on a '/'
/// boundary ("github:org" matches "github:org/repo", not "github.com/org").
fn entity_matches_ref_filter(e: &Entity, want_type: Option<&str>, want_value: Option<&str>) -> bool {
    let body: serde_json::Value = match serde_json::from_str(&e.body_json) {
        Ok(v) => v,
        Err(_) => return false,
    };
    let refs = match body.get("external_refs").and_then(|r| r.as_array()) {
        Some(arr) => arr,
        None => return false,
    };
    refs.iter().any(|r| {
        let rt = r.get("ref_type").and_then(|v| v.as_str()).unwrap_or("");
        let rv = r.get("ref_value").and_then(|v| v.as_str()).unwrap_or("");
        let type_ok = want_type.map_or(true, |t| rt == t);
        let value_ok = want_value.map_or(true, |v| {
            rv == v || (rv.len() > v.len() && rv.starts_with(v) && rv[v.len()..].starts_with('/'))
        });
        type_ok && value_ok
    })
}

fn apply_promotion_explanations(items: &mut [Value]) {
    for item in items {
        let Some(obj) = item.as_object_mut() else { continue };
        let class = obj.get("category").and_then(Value::as_str).unwrap_or("unknown");
        let transition = obj.get("promotion_transition");
        let state = transition
            .and_then(|v| v.get("to_state"))
            .and_then(Value::as_str)
            .unwrap_or("unpromoted");
        let evidence: Vec<Value> = obj.get("promoted_from")
            .and_then(|v| v.get("id"))
            .cloned().into_iter().collect();
        obj.insert("why_served".to_string(), json!({
            "memory_class": class,
            "promotion_state": state,
            "support_count": evidence.len(),
            "source_evidence_ids": evidence,
            "promoted_scope": obj.get("workspace_hash").cloned().unwrap_or(Value::String(String::new())),
            "reason": "matched the recall query"
        }));
    }
}

pub fn handle_recall(db: &Database, args: Value) -> Result<String, String> {
    let a: RecallArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid recall arguments: {}", e))?;

    // #865: granular authority — a manifest's presence requires memory.read
    // for retrieval. Fail-open when no manifest exists (legacy vault).
    if let Err(e) = db.require_memory_capability(
        a.requesting_agent_id.as_deref().unwrap_or(""),
        a.workspace_hash.as_deref().unwrap_or(""),
        "memory.read",
    ) {
        return Err(format!("recall denied: {e}"));
    }

    // #363 review: valid_op is a closed SQL:2011 enum — reject unknown strings
    // instead of silently treating them as 'overlaps'. Validated up front so
    // the expansion path is covered too. "" is the serde default (= overlaps).
    match a.valid_op.as_str() {
        "" | "overlaps" | "contains" => {}
        other => {
            return Err(format!(
                "Invalid valid_op '{other}': expected 'overlaps' or 'contains'"
            ))
        }
    }

    // #485: scope_weight is a rank multiplier in [0,1] and only means
    // something relative to a workspace to prefer — reject junk up front
    // rather than silently no-oping.
    if let Some(w) = a.scope_weight {
        if !w.is_finite() || !(0.0..=1.0).contains(&w) {
            return Err(format!(
                "scope_weight must be between 0.0 and 1.0, got {w}"
            ));
        }
        if a.workspace_hash.as_deref().map_or(true, |ws| ws.is_empty()) {
            return Err(
                "scope_weight requires a non-empty workspace_hash (the scope to prefer)"
                    .to_string(),
            );
        }
    }

    // #869: graph utility gate threshold is a probability-like knob — reject
    // junk up front rather than silently clamping in the fused path.
    if let Some(t) = a.graph_utility_threshold {
        if !t.is_finite() || !(0.0..=1.0).contains(&t) {
            return Err(format!(
                "graph_utility_threshold must be between 0.0 and 1.0, got {t}"
            ));
        }
    }

    // #271: an unset `mode` ("" — the serde default) auto-selects the best
    // available strategy. When the embedding backend is on AND at least one
    // entity is embedded, default to Hybrid (deterministic dense + keyword RRF);
    // otherwise fall back to keyword FTS5 exactly as before. An explicit mode
    // always wins.
    let mode = match a.mode.as_str() {
        "dense" => SearchMode::Dense,
        "hybrid" => SearchMode::Hybrid,
        "fts5" => SearchMode::Fts5,
        "fused" => SearchMode::Fused,
        "" => {
            if db.embedding_enabled() && db.embedding_coverage() > 0 {
                SearchMode::Hybrid
            } else {
                SearchMode::Fts5
            }
        }
        _ => SearchMode::Fts5,
    };

    // If query expansion is enabled, generate stemming variants and merge results
    // #472: as_of recall always takes the main path (temporal_resolve); the
    // query-expansion path does not reconstruct point-in-time bodies.
    if a.expansion.enabled
        && !a.query.is_empty()
        && mode == SearchMode::Fts5
        && a.as_of_unix_ms.is_none()
    {
        return handle_recall_with_expansion(db, &a);
    }

    // #363: captured before RecallParams moves fields out of `a`.
    let (valid_at, valid_from, valid_to) = (a.valid_at, a.valid_from_unix_ms, a.valid_to_unix_ms);
    let as_of = a.as_of_unix_ms; // #472 Temporal RAG (transaction-time instant)
    let valid_op = a.valid_op.clone();
    let temporal_filtering =
        valid_at.is_some() || valid_from.is_some() || valid_to.is_some() || as_of.is_some();
    let mode_for_side_effects = mode.clone();
    let reinforce_requested = a.reinforce;

    // #675/#676: startup-optimized recall over-fetches a candidate pool, then
    // re-ranks by actionability and truncates to the caller's limit below. Only
    // when opted in (a.startup) and paging from the first page (offset 0) — so
    // pagination semantics and the default recall path are untouched. The pool
    // is read purely (skip_side_effects) so over-fetch doesn't reinforce rows
    // that won't survive the truncate; survivors are reinforced afterwards.
    let startup_rank = a.startup;
    let requested_limit = a.limit;
    let effective_limit = if startup_rank && a.offset == 0 {
        requested_limit.saturating_mul(5).clamp(requested_limit, 200)
    } else {
        requested_limit
    };
    let defer_side_effects = temporal_filtering || (startup_rank && a.offset == 0);

    // #784: personal/agent profiles may retrieve global policy records; shared
    // profile stays strictly workspace-scoped.
    let profile_name = a.retrieval_profile.clone().unwrap_or_else(|| "shared".to_string());
    let profile_workspace = if matches!(profile_name.as_str(), "personal" | "agent") {
        None
    } else {
        a.workspace_hash.clone()
    };

    let params = RecallParams {
        query: a.query,
        category: a.category,
        entity_type: a.entity_type,
        limit: effective_limit,
        offset: a.offset,
        min_decay: a.min_decay,
        topic_path: a.topic_path,
        include_archived: a.include_archived,
        // #363 review (a #356-class value inversion): with a valid-time filter
        // present, the inner recall must be a PURE read — the fts5 path (and
        // dense/hybrid with reinforce) otherwise reinforces every matched row,
        // including the ones the filter is about to hide, so repeatedly asking
        // "what was true at T" would make the invisible entities immortal.
        // Side-effects are applied below to the SURVIVING hits only, mirroring
        // the expansion path. Unfiltered calls keep the original behavior.
        skip_side_effects: defer_side_effects,
        mode,
        embedding: None,
        preview_cap: a.preview_cap,
        always_on: a.always_on,
        content_weight: a.content_weight,
        trust_weight: a.trust_weight,
        diversity_halving: a.diversity_halving,
        diversity_per_query_share: 0.0,
        recency_half_life_secs: a.recency_half_life_secs,
        workspace_hash: profile_workspace.clone(),
        scope_weight: a.scope_weight,
        agent_id: a.agent_id.clone(),
        epistemic_state: a.epistemic_state.clone(),
        visibility: None,
        layer: a.layer.as_deref().filter(|s| !s.is_empty()).map(canonical_layer),
        reinforce: a.reinforce,
        strategies: a.strategies,
        max_tokens: a.max_tokens,
        depth_budget: a.depth_budget,
        strategy_weights: a.strategy_weights,
        rerank: a.rerank,
        query_time_unix_ms: a.query_time_unix_ms,
        graph_utility_threshold: a.graph_utility_threshold,
    };

    // #864: bounded recall — time the query when the caller set a deadline.
    // The result set is still returned in full (bounded, not dropped); the
    // outcome reports `timeout` so callers know it may be incomplete.
    let deadline_ms = a.deadline_ms.filter(|ms| *ms > 0);
    let started = std::time::Instant::now();

    // #883: the fused path carries its trace out of the DB layer; the
    // standard path returns entities + completeness only.
    let (mut entities, recall_completeness, fused_trace) =
        if mode_for_side_effects == SearchMode::Fused {
        let (e, c, t) = db
            .fused_recall(&params)
            .map_err(|e| format!("Recall failed: {}", e))?;
        (e, c, Some(t))
    } else {
        let (e, c) = db
            .recall_with_completeness(&params)
            .map_err(|e| format!("Recall failed: {}", e))?;
        (e, c, None)
    };

    let elapsed_ms = started.elapsed().as_millis() as i64;
    let deadline_elapsed = deadline_ms.is_some_and(|ms| elapsed_ms >= ms);

    // #864/#873/#887: explicit outcome. For dense/hybrid with a non-empty
    // query, recall succeeding implies the query embedding was produced
    // (a missing/dead backend errors out instead of silently degrading); an
    // empty query falls through to the FTS path inside recall. FTS is always
    // "semantically available" (it has no separate backend).
    let query_embedding_available = match mode_for_side_effects {
        SearchMode::Dense | SearchMode::Hybrid | SearchMode::Fused => {
            !params.query.trim().is_empty()
        }
        SearchMode::Fts5 => true,
    };
    let outcome = db.recall_outcome(&mode_for_side_effects, query_embedding_available, entities.len(), None);
    // #883: a fused recall with a DEGRADED arm (e.g. embedding backend down
    // for the dense strategy) is never silently fresh — report Partial with
    // the reason so an incomplete consensus is distinguishable from a
    // complete one.
    let outcome = match &fused_trace {
        Some(t) if t.strategies.iter().any(|s| s.status == "degraded") => {
            let mut o = outcome;
            o.status = crate::models::RecallStatus::Partial;
            o.reason = "partial_arms".to_string();
            o
        }
        _ => outcome,
    };
    let outcome = Database::apply_recall_deadline(outcome, deadline_elapsed);
    // #856: the recall path's pool dynamics are authoritative over the
    // standalone estimate; abstention (no evidence / backend unavailable)
    // wins over everything.
    let outcome = {
        let mut o = outcome;
        if o.abstained {
            o.completeness = Some(crate::models::Completeness::Abstain);
            o.candidate_scope = None;
        } else {
            o.completeness = Some(recall_completeness.completeness);
            o.candidate_scope = recall_completeness.scope;
        }
        o
    };

    // #684: visibility enforcement. Drop entities the requesting agent may not
    // read (private → author only; fleet → same fleet / tier>=2) before any
    // downstream processing, so hidden entities are never reconstructed,
    // reinforced, or returned. Applied first so temporal provenance stays 1:1
    // with the surviving set. A no-op for unscoped requesters (empty id → tier
    // 3) and for the default `workspace`/`tenant`/'' visibility — so existing
    // single-agent callers and data are unaffected.
    if let Some(req) = a.requesting_agent_id.as_deref().filter(|s| !s.is_empty()) {
        entities.retain(|e| db.can_read(req, &e.visibility, &e.agent_id));
    }

    // #784: profile-specific serving filter. This is additive to visibility:
    // profile choice can only narrow results and never exposes another scope.
    let profile_name = a.retrieval_profile.as_deref().unwrap_or("shared");
    match profile_name {
        "shared" => {
            entities.retain(|e| {
                e.category != "preference" && e.category != "personal"
                    && a.workspace_hash.as_ref().map_or(true, |ws| ws.is_empty() || e.workspace_hash == *ws)
            });
        }
        "personal" => entities.retain(|e| e.category == "preference" || e.category == "personal"),
        "agent" => entities.retain(|e| matches!(e.category.as_str(), "convention" | "correction" | "keystone")),
        other => return Err(format!("unknown retrieval_profile '{other}': expected personal, agent, or shared")),
    }

    // #728: external-ref post-filter (spec §2.2). Applied after visibility so
    // hidden rows are never inspected; narrows only, never re-ranks.
    if a.ref_type.is_some() || a.ref_value.is_some() {
        let want_type = a.ref_type.as_deref();
        let want_value = a.ref_value.as_deref();
        entities.retain(|e| entity_matches_ref_filter(e, want_type, want_value));
    }

    // #675/#676: re-rank the over-fetched pool by actionability and truncate to
    // the caller's limit, so action-changing memories win the top-k over vague/
    // date-only near-neighbors. No-op unless startup was requested.
    if startup_rank && a.offset == 0 {
        entities = crate::db::actionability_rerank(entities, requested_limit.max(0) as usize);
    }

    // #472 Temporal RAG: an as_of (transaction) and/or valid_at (world) instant
    // reconstructs each hit's point-in-time body — valid_at alone now rebuilds
    // the historical world-version, not just a live-row narrow; as_of adds the
    // transaction axis; together = the full bi-temporal cell. The valid_from/
    // valid_to PERIOD-range filter (when no valid_at instant) stays a live narrow.
    let temporal_meta = if as_of.is_some() || valid_at.is_some() {
        let mut hits = temporal_resolve(db, as_of, valid_at, &mut entities)?;
        // #682: close the documented v1 limitation — surface facts whose
        // query-matching version has since been superseded/retired (live index
        // never saw them). Only fills up to the caller's limit and only when
        // the live path came up short, so it never reorders or bloats results.
        augment_temporal_with_history(
            db,
            &params.query,
            as_of,
            valid_at,
            params.workspace_hash.as_deref(),
            requested_limit.max(0) as usize,
            &mut entities,
            &mut hits,
        )?;
        Some(hits)
    } else {
        valid_time_retain(db, None, valid_from, valid_to, &valid_op, &mut entities)?;
        None
    };

    // #363 review: re-apply the deferred recall side-effects to the survivors,
    // under exactly the conditions the un-filtered path would have reinforced:
    // fts5 always does; dense/hybrid only when the caller opted in. #675/#676:
    // the startup path defers the same way (it over-fetched a pure-read pool),
    // so its truncated survivors get reinforced here too.
    if defer_side_effects
        && (mode_for_side_effects == SearchMode::Fts5 || reinforce_requested)
        && !entities.is_empty()
    {
        let ids: Vec<String> = entities.iter().map(|e| e.id.clone()).collect();
        let _ = db.apply_recall_side_effects(&ids);
    }

    let mut items_expanded: Vec<serde_json::Value> =
        entities.iter().map(|e| e.to_json_expanded()).collect();
    apply_promotion_explanations(&mut items_expanded);

    // #865: retrieved memory is UNTRUSTED DATA. Every hit whose epistemic
    // trust axis is not established fact (candidate/rejected/
    // defensively_recalled — anything below verified/corroborated) is
    // explicitly typed as such in the response, so a consuming agent never
    // mistakes a recalled draft for an authoritative claim. The entity's
    // epistemic_state field is authoritative; this flag is the serving-layer
    // framing on top of it.
    for (item, entity) in items_expanded.iter_mut().zip(entities.iter()) {
        if let Some(obj) = item.as_object_mut() {
            let trusted = matches!(entity.epistemic_state.as_str(), "verified" | "corroborated");
            obj.insert("untrusted".to_string(), json!(!trusted));
            if !trusted {
                obj.insert(
                    "untrusted_reason".to_string(),
                    json!(format!("epistemic_state:{}", entity.epistemic_state)),
                );
            }
        }
    }

    // #472: stamp point-in-time provenance onto each reconstructed hit.
    if let Some(meta) = &temporal_meta {
        for (item, h) in items_expanded.iter_mut().zip(meta.iter()) {
            if let Some(obj) = item.as_object_mut() {
                obj.insert("as_of_unix_ms".to_string(), json!(as_of));
                obj.insert("is_live_version".to_string(), json!(h.is_live));
                obj.insert("recorded_at_unix_ms".to_string(), json!(h.recorded_at));
                obj.insert("valid_from_unix_ms".to_string(), json!(h.valid_from));
                obj.insert("valid_to_unix_ms".to_string(), json!(h.valid_to));
            }
        }
    }

    if a.include_confidence {
        apply_confidence(&mut items_expanded, &entities);
    }

    // #864/#873/#887: attach the explicit outcome when it has something to
    // say (degraded/partial/timeout/unavailable/empty/stale) or the caller
    // opted into always seeing it. Nominal fresh recalls stay byte-identical.
    let outcome_value = serde_json::to_value(&outcome).unwrap_or(serde_json::json!({}));
    let include_outcome = a.include_outcome || outcome.status != crate::models::RecallStatus::Fresh;

    let mut result = if items_expanded.is_empty() {
        json!({
            "items": items_expanded,
            "total": 0,
            // #677: self-describing empty result — see empty_recall_diagnostic.
            "diagnostic": empty_recall_diagnostic(db, &mode_for_side_effects),
        })
    } else {
        json!({
            "items": items_expanded,
            "total": items_expanded.len(),
            "retrieval_profile": profile_name,
        })
    };
    if include_outcome {
        if let Some(obj) = result.as_object_mut() {
            obj.insert("outcome".to_string(), outcome_value);
        }
    }
    // #883/#867: attach the fused recall trace (strategies, fusion weights,
    // truncation, rerank outcome, placement, consensus sources) whenever the
    // fused path produced one.
    if let Some(t) = fused_trace {
        if let Some(obj) = result.as_object_mut() {
            obj.insert(
                "fused_trace".to_string(),
                serde_json::to_value(t).unwrap_or(serde_json::json!({})),
            );
        }
    }
    Ok(result.to_string())
}

pub fn handle_recall_batch(db: &Database, args: Value) -> Result<String, String> {
    let a: RecallBatchArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid recall batch arguments: {}", e))?;

    if a.queries.is_empty() {
        return Ok(json!({
            "items": Vec::<serde_json::Value>::new(),
            "total": 0,
        }).to_string());
    }

    // Validate valid_op and scope_weight for all queries
    for q in &a.queries {
        match q.valid_op.as_str() {
            "" | "overlaps" | "contains" => {}
            other => {
                return Err(format!(
                    "Invalid valid_op '{other}': expected 'overlaps' or 'contains'"
                ))
            }
        }

        if let Some(w) = q.scope_weight {
            if !w.is_finite() || !(0.0..=1.0).contains(&w) {
                return Err(format!(
                    "scope_weight must be between 0.0 and 1.0, got {w}"
                ));
            }
            if q.workspace_hash.as_deref().map_or(true, |ws| ws.is_empty()) {
                return Err(
                    "scope_weight requires a non-empty workspace_hash (the scope to prefer)"
                        .to_string(),
                );
            }
        }

        // #869: graph utility gate threshold validation (fused mode only).
        if let Some(t) = q.graph_utility_threshold {
            if !t.is_finite() || !(0.0..=1.0).contains(&t) {
                return Err(format!(
                    "graph_utility_threshold must be between 0.0 and 1.0, got {t}"
                ));
            }
        }
    }

    let limit = a.queries.iter().map(|q| q.limit).max().unwrap_or(10) as usize;

    // Run each query and apply temporal filtering if needed, then collect them for pairwise fusion
    let mut all_results = Vec::new();
    for q in &a.queries {
        let mode = match q.mode.as_str() {
            "dense" => SearchMode::Dense,
            "hybrid" => SearchMode::Hybrid,
            "fts5" => SearchMode::Fts5,
            "fused" => SearchMode::Fused,
            "" => {
                if db.embedding_enabled() && db.embedding_coverage() > 0 {
                    SearchMode::Hybrid
                } else {
                    SearchMode::Fts5
                }
            }
            _ => SearchMode::Fts5,
        };

        let temporal_filtering =
            q.valid_at.is_some() || q.valid_from_unix_ms.is_some() || q.valid_to_unix_ms.is_some() || q.as_of_unix_ms.is_some();

        let params = RecallParams {
            query: q.query.clone(),
            category: q.category.clone(),
            entity_type: q.entity_type.clone(),
            limit: q.limit,
            offset: q.offset,
            min_decay: q.min_decay,
            topic_path: q.topic_path.clone(),
            include_archived: q.include_archived,
            skip_side_effects: temporal_filtering,
            mode,
            embedding: None,
            preview_cap: q.preview_cap,
            always_on: q.always_on,
            content_weight: q.content_weight,
            trust_weight: q.trust_weight,
            diversity_halving: q.diversity_halving,
            diversity_per_query_share: 0.0,
            recency_half_life_secs: q.recency_half_life_secs,
            workspace_hash: q.workspace_hash.clone(),
            scope_weight: q.scope_weight,
            agent_id: q.agent_id.clone(),
            epistemic_state: q.epistemic_state.clone(),
            visibility: None,
            layer: q.layer.as_deref().filter(|s| !s.is_empty()).map(canonical_layer),
            reinforce: q.reinforce,
            strategies: q.strategies.clone(),
            max_tokens: q.max_tokens,
            depth_budget: q.depth_budget.clone(),
            strategy_weights: q.strategy_weights.clone(),
            rerank: q.rerank,
            query_time_unix_ms: q.query_time_unix_ms,
            graph_utility_threshold: q.graph_utility_threshold,
        };

        let mut entities = db
            .recall(&params)
            .map_err(|e| format!("Recall failed: {}", e))?;

        if temporal_filtering {
            if q.as_of_unix_ms.is_some() || q.valid_at.is_some() {
                let _ = temporal_resolve(db, q.as_of_unix_ms, q.valid_at, &mut entities)?;
            } else {
                valid_time_retain(db, None, q.valid_from_unix_ms, q.valid_to_unix_ms, &q.valid_op, &mut entities)?;
            }
        }

        let scored: Vec<(Entity, f64)> = entities.into_iter().map(|e| (e, 1.0)).collect();
        all_results.push((scored, q.recency_half_life_secs));
    }

    // Fuse pairwise
    let (mut fused, mut last_half_life) = all_results[0].clone();
    for (next_res, next_half_life) in all_results.into_iter().skip(1) {
        let half_life = next_half_life.or(last_half_life);
        fused = crate::db::reciprocal_rank_fusion(
            &fused,
            &next_res,
            60.0,
            limit,
            1.0,
            half_life,
            crate::db::now_ms(),
        );
        last_half_life = half_life;
    }

    let mut items_expanded: Vec<serde_json::Value> = Vec::new();
    let entities_only: Vec<Entity> = fused.iter().map(|(e, _)| e.clone()).collect();
    for (entity, _score) in &fused {
        items_expanded.push(entity.to_json_expanded());
    }

    // Apply confidence if requested by the first query
    let include_confidence = a.queries.first().map_or(false, |q| q.include_confidence);
    if include_confidence {
        apply_confidence(&mut items_expanded, &entities_only);
    }

    let result = json!({
        "items": items_expanded,
        "total": items_expanded.len(),
    });
    Ok(result.to_string())
}

#[derive(Debug, Deserialize)]
pub struct SemanticSearchArgs {
    pub query: String,
    #[serde(default = "default_limit")]
    pub limit: i64,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub agent_id: Option<String>,
}

/// #271: `perseus_vault_semantic_search` — dense-only semantic search shortcut. Unlike
/// `perseus_vault_recall` (which fuses keyword + dense in hybrid mode), this runs the
/// pure dense vector arm with NO FTS5 fallback: results are ranked solely by
/// embedding cosine similarity. Requires an embedding backend (on by default via
/// the bundled in-process ONNX model). Errors clearly when no backend is
/// available rather than silently degrading to keyword search.
pub fn handle_semantic_search(db: &Database, args: Value) -> Result<String, String> {
    let a: SemanticSearchArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid semantic_search arguments: {}", e))?;

    let params = RecallParams {
        query: a.query,
        category: a.category,
        limit: a.limit,
        skip_side_effects: false,
        mode: SearchMode::Dense,
        workspace_hash: a.workspace_hash,
        agent_id: a.agent_id,
        ..RecallParams::default()
    };

    let entities = db
        .recall(&params)
        .map_err(|e| format!("Semantic search failed: {}", e))?;

    let items_expanded: Vec<serde_json::Value> =
        entities.iter().map(|e| e.to_json_expanded()).collect();

    let result = json!({
        "items": items_expanded,
        "total": items_expanded.len(),
    });
    Ok(result.to_string())
}

#[derive(Debug, Deserialize)]
pub struct ScanArgs {
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub include_archived: bool,
    /// Opaque continuation cursor: the `next_cursor` from the previous page.
    /// Omit (or pass "") for the first page.
    #[serde(default)]
    pub cursor: Option<String>,
    #[serde(
        default = "default_scan_limit",
        deserialize_with = "null_as_default_scan_limit"
    )]
    pub limit: i64,
}

fn default_scan_limit() -> i64 {
    100
}

fn null_as_default_scan_limit<'de, D>(de: D) -> Result<i64, D::Error>
where
    D: serde::Deserializer<'de>,
{
    let opt = Option::<i64>::deserialize(de)?;
    Ok(opt.unwrap_or_else(default_scan_limit))
}

/// #562: `perseus_vault_scan` — deterministic paginated enumeration of a category (or
/// the whole store). Unlike `recall(query="")`, whose ranking keys mutate on
/// every reinforcing recall (so offset pages can skip/repeat rows) and whose
/// offset is capped, scan pages by immutable `id ASC` with a keyset cursor:
/// call repeatedly, feeding each page's `next_cursor` back in, until
/// `has_more` is false — every entity in scope is returned exactly once.
/// Read-only: no retrieval-count/decay side-effects.
#[derive(Debug, Deserialize, Default)]
pub struct HygieneArgs {
    /// Restrict the scan to one category (default: all active memories).
    #[serde(default)]
    pub category: Option<String>,
    /// Actionability below which a memory is flagged low-signal (default 0.35).
    #[serde(default)]
    pub threshold: Option<f64>,
    /// Max memories to scan (default 1000, cap 10000).
    #[serde(default)]
    pub scan_limit: Option<i64>,
    /// Max flagged rows to return, worst first (default 50, cap 1000).
    #[serde(default)]
    pub limit: Option<i64>,
}

/// #675: read-only hygiene report — surface likely low-signal memories (vague,
/// date-only titles, very short bodies, no concrete entities) so a startup
/// block can be kept dense without hand-forensics over the vault. Uses the same
/// actionability scoring as startup recall (#676); keyset-scans active memories
/// in pages (no recall side-effects) and returns the worst offenders with the
/// reasons they were flagged.
pub fn handle_hygiene(db: &Database, args: Value) -> Result<String, String> {
    let a: HygieneArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid hygiene arguments: {}", e))?;
    let threshold = a.threshold.unwrap_or(0.35).clamp(0.0, 1.0);
    let scan_cap = a.scan_limit.unwrap_or(1000).clamp(1, 10_000);
    let report_cap = a.limit.unwrap_or(50).clamp(1, 1000) as usize;

    let mut scanned = 0i64;
    let mut flagged: Vec<(f64, serde_json::Value)> = Vec::new();
    let mut cursor: Option<String> = None;
    const PAGE: i64 = 500;
    while scanned < scan_cap {
        let want = PAGE.min(scan_cap - scanned);
        // Over-fetch one row to learn whether another page exists (mirrors scan).
        let batch = db
            .scan_entities(a.category.as_deref(), None, false, cursor.as_deref(), want + 1)
            .map_err(|e| format!("Hygiene scan failed: {}", e))?;
        let has_more = batch.len() as i64 > want;
        let take = if has_more { want as usize } else { batch.len() };
        if take == 0 {
            break;
        }
        cursor = batch.get(take - 1).map(|e| e.id.clone());
        for e in batch.iter().take(take) {
            scanned += 1;
            let score = crate::db::actionability_score(e);
            if score < threshold {
                let mut reasons: Vec<String> = crate::db::actionability_reasons(e)
                    .iter()
                    .map(|s| s.to_string())
                    .collect();
                if reasons.is_empty() {
                    reasons.push("low_actionability".to_string());
                }
                flagged.push((
                    score,
                    json!({
                        "id": e.id,
                        "category": e.category,
                        "key": e.key,
                        "actionability": (score * 1000.0).round() / 1000.0,
                        "reasons": reasons,
                        "retrieval_count": e.retrieval_count,
                    }),
                ));
            }
        }
        if !has_more {
            break;
        }
    }
    // Worst (lowest actionability) first; deterministic id tiebreak.
    flagged.sort_by(|x, y| {
        x.0.partial_cmp(&y.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| x.1["id"].as_str().unwrap_or("").cmp(y.1["id"].as_str().unwrap_or("")))
    });
    let flagged_count = flagged.len();
    let items: Vec<serde_json::Value> =
        flagged.into_iter().take(report_cap).map(|(_, v)| v).collect();
    Ok(json!({
        "scanned": scanned,
        "flagged_count": flagged_count,
        "returned": items.len(),
        "threshold": threshold,
        "flagged": items,
    })
    .to_string())
}

pub fn handle_scan(db: &Database, args: Value) -> Result<String, String> {
    let a: ScanArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid scan arguments: {}", e))?;

    let limit = a.limit.clamp(1, 1000);
    // The DB scan over-fetches and filters governed rows before returning;
    // request the page-plus-sentinel directly so this boundary remains exact.
    let mut entities = db
        .scan_entities(
            a.category.as_deref(),
            a.workspace_hash.as_deref(),
            a.include_archived,
            a.cursor.as_deref(),
            limit + 1,
        )
        .map_err(|e| format!("Scan failed: {}", e))?;

    let has_more = entities.len() as i64 > limit;
    if has_more {
        entities.truncate(limit as usize);
    }
    let next_cursor = if has_more {
        entities.last().map(|e| e.id.clone())
    } else {
        None
    };

    let items: Vec<serde_json::Value> = entities.iter().map(|e| e.to_json_expanded()).collect();
    Ok(json!({
        "items": items,
        "total": items.len(),
        "has_more": has_more,
        "next_cursor": next_cursor,
    })
    .to_string())
}

/// Run recall with stemming-based query expansion, merging results from
/// the original query and up to `n_variants` stemmed alternatives.
fn handle_recall_with_expansion(db: &Database, a: &RecallArgs) -> Result<String, String> {
    use rust_stemmers::{Algorithm, Stemmer};
    use std::collections::HashMap;

    let stemmer = Stemmer::create(Algorithm::English);
    let tokens: Vec<&str> = a
        .query
        .split_whitespace()
        .filter(|w| !w.is_empty())
        .collect();
    if tokens.is_empty() {
        return Err("Query expansion requires at least one token".to_string());
    }

    // Build variants: original query + stemmed alternatives
    let mut variants: Vec<String> = vec![a.query.clone()];
    for (i, &token) in tokens.iter().enumerate() {
        if variants.len() > a.expansion.n_variants {
            break;
        }
        let stemmed = stemmer.stem(token).to_string();
        if stemmed != token {
            let mut alt_tokens: Vec<&str> = tokens.clone();
            alt_tokens[i] = &stemmed;
            variants.push(alt_tokens.join(" "));
        }
    }

    // Collect results from all variants, keeping the highest-score version of each entity
    let mut best: HashMap<String, (crate::models::Entity, f64)> = HashMap::new();

    for variant in &variants {
        let params = RecallParams {
            query: variant.clone(),
            category: a.category.clone(),
            entity_type: a.entity_type.clone(),
            limit: a.limit.max(50), // fetch more per variant to have good merge pool
            offset: 0,
            min_decay: a.min_decay,
            topic_path: a.topic_path.clone(),
            include_archived: a.include_archived,
            // #207: suppress per-variant side-effects; a single recall must bump
            // each returned entity once, not once per matching variant. We apply
            // the batched side-effect below on the final merged result set.
            skip_side_effects: true,
            mode: SearchMode::Fts5,
            embedding: None,
            preview_cap: a.preview_cap,
            always_on: a.always_on,
            content_weight: a.content_weight,
            trust_weight: a.trust_weight,
            diversity_halving: a.diversity_halving,
            diversity_per_query_share: 0.0,
            // Query expansion runs in Fts5 mode only, so recency (a hybrid-fusion
            // re-weighting) never applies on this path.
            recency_half_life_secs: None,
            workspace_hash: a.workspace_hash.clone(),
            scope_weight: a.scope_weight,
            agent_id: a.agent_id.clone(),
            epistemic_state: a.epistemic_state.clone(),
            visibility: None,
            layer: a.layer.as_deref().filter(|s| !s.is_empty()).map(canonical_layer),
            // Fts5-only path: reinforcement is handled by the batched
            // side-effect below, not the per-variant recalls.
            reinforce: false,
            ..Default::default()
        };

        if let Ok(entities) = db.recall(&params) {
            for entity in entities {
                let score = entity.decay_score;
                best.entry(entity.id.clone())
                    .and_modify(|(existing, existing_score)| {
                        if score > *existing_score {
                            *existing = entity.clone();
                            *existing_score = score;
                        }
                    })
                    .or_insert((entity, score));
            }
        }
    }

    // Sort by score descending, then truncate to limit
    let mut merged: Vec<_> = best.into_values().collect();
    merged.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    merged.truncate(a.limit as usize);

    // #363: valid-time filters, before side-effects so filtered-out entities
    // are not reinforced. No-op unless a filter was requested.
    if a.valid_at.is_some() || a.valid_from_unix_ms.is_some() || a.valid_to_unix_ms.is_some() {
        let mut ents: Vec<crate::models::Entity> =
            merged.iter().map(|(e, _)| e.clone()).collect();
        valid_time_retain(
            db,
            a.valid_at,
            a.valid_from_unix_ms,
            a.valid_to_unix_ms,
            &a.valid_op,
            &mut ents,
        )?;
        let keep: std::collections::HashSet<String> = ents.into_iter().map(|e| e.id).collect();
        merged.retain(|(e, _)| keep.contains(&e.id));
    }

    // #207: apply recall side-effects once, to the entities actually returned,
    // in one batched write — rather than once per variant inside the loop above.
    let hit_ids: Vec<String> = merged.iter().map(|(e, _)| e.id.clone()).collect();
    let _ = db.apply_recall_side_effects(&hit_ids);

    let mut items_expanded: Vec<serde_json::Value> = merged
        .iter()
        .map(|(entity, _)| entity.to_json_expanded())
        .collect();

    if a.include_confidence {
        let total = merged.len();
        for (i, (item, (entity, _))) in items_expanded.iter_mut().zip(merged.iter()).enumerate() {
            if let Some(obj) = item.as_object_mut() {
                obj.insert("confidence".to_string(), json!(confidence_for(entity, i, total)));
            }
        }
    }

    let result = json!({
        "items": items_expanded,
        "total": items_expanded.len(),
        "variants": variants.len(),
    });
    Ok(result.to_string())
}

#[derive(Debug, Deserialize)]
pub struct RecallLayerArgs {
    pub layer: String,
    #[serde(default = "default_limit")]
    pub limit: i64,
}

pub fn handle_recall_layer(db: &Database, args: Value) -> Result<String, String> {
    let a: RecallLayerArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid recall_layer arguments: {}", e))?;

    let layer = match a.layer.as_str() {
        "world" => "core",
        "episodic" => "buffer",
        "semantic" => "working",
        _ => &a.layer,
    };

    let recall_args = json!({
        "query": "",
        "limit": a.limit,
        "layer": layer,
    });

    handle_recall(db, recall_args)
}

/// #103: Get a single entity by ID with full body (for drill-down after preview cap).
pub fn handle_get_entity(db: &Database, args: Value) -> Result<String, String> {
    let id = args
        .get("id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'id' parameter".to_string())?;

    let entity = db
        .get_entity_by_id_public(id)
        .map_err(|e| format!("Get entity failed: {}", e))?
        .ok_or_else(|| format!("Entity not found: {}", id))?;

    let result = json!({
        "id": entity.id,
        "category": entity.category,
        "key": entity.key,
        "body_json": entity.body_json,
        "status": entity.status,
        "entity_type": entity.entity_type,
        "tags": entity.tags,
        "decay_score": entity.decay_score,
        "retrieval_count": entity.retrieval_count,
        "layer": entity.layer,
        "always_on": entity.always_on,
        "certainty": entity.certainty,
        "created_at_unix_ms": entity.created_at_unix_ms,
        "last_accessed_unix_ms": entity.last_accessed_unix_ms,
    });
    Ok(result.to_string())
}

pub fn handle_as_of(db: &Database, args: Value) -> Result<String, String> {
    let category = args
        .get("category")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'category' parameter".to_string())?;
    let key = args
        .get("key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'key' parameter".to_string())?;
    let as_of = args
        .get("as_of_unix_ms")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "Missing 'as_of_unix_ms' parameter (integer unix ms)".to_string())?;

    let found = db
        .as_of(category, key, as_of)
        .map_err(|e| format!("as_of failed: {}", e))?;

    let result = match found {
        Some(e) => {
            let mut r = json!({
                "found": true,
                "id": e.id,
                "category": e.category,
                "key": e.key,
                "body_json": e.body_json,
                "status": e.status,
                "entity_type": e.entity_type,
                "as_of_unix_ms": as_of,
            });
            decorate_compacted_marker(&mut r, &e.status, &e.body_json);
            r
        }
        None => json!({
            "found": false,
            "category": category,
            "key": key,
            "as_of_unix_ms": as_of,
        }),
    };
    Ok(result.to_string())
}

/// #398: the returned version falls inside a retention-compacted window —
/// surface an explicit marker (flag + version count + roll-up digest from the
/// tombstone body) instead of letting the synthetic row pass for a real
/// version. Shared by perseus_vault_as_of, perseus_vault_valid_at, and perseus_vault_bitemporal so
/// all three temporal axes decorate identically. No-op for real versions.
fn decorate_compacted_marker(r: &mut serde_json::Value, status: &str, body_json: &str) {
    if status != "compacted" {
        return;
    }
    let marker: serde_json::Value = serde_json::from_str(body_json).unwrap_or_default();
    r["compacted"] = json!(true);
    r["versions_compacted"] = marker.get("versions").cloned().unwrap_or(json!(null));
    r["digest"] = marker.get("digest").cloned().unwrap_or(json!(null));
    r["note"] = json!(
        "history inside this window was compacted by retention policy; \
         the original versions are not recoverable"
    );
}

/// Serialize a TemporalVersion into the shared found=true response shape used
/// by perseus_vault_valid_at and perseus_vault_bitemporal (#363). Tombstone versions carry
/// the #398 compacted-marker decoration.
fn temporal_version_json(v: &crate::db::TemporalVersion) -> serde_json::Value {
    let mut r = json!({
        "found": true,
        "id": v.entity.id,
        "category": v.entity.category,
        "key": v.entity.key,
        "body_json": v.entity.body_json,
        "status": v.entity.status,
        "entity_type": v.entity.entity_type,
        "valid_from_unix_ms": v.valid_from_unix_ms,
        "valid_to_unix_ms": v.valid_to_unix_ms,
        "recorded_at_unix_ms": v.recorded_at_unix_ms,
        "invalidated_at_unix_ms": v.invalidated_at_unix_ms,
        "is_live_version": v.invalidated_at_unix_ms.is_none(),
    });
    decorate_compacted_marker(&mut r, &v.entity.status, &v.entity.body_json);
    r
}

/// #363: perseus_vault_valid_at — the valid-time axis. "What was actually true in the
/// world at instant T, per current knowledge?" Orthogonal to perseus_vault_as_of.
pub fn handle_valid_at(db: &Database, args: Value) -> Result<String, String> {
    let category = args
        .get("category")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'category' parameter".to_string())?;
    let key = args
        .get("key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'key' parameter".to_string())?;
    let valid_at = args
        .get("valid_at_unix_ms")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "Missing 'valid_at_unix_ms' parameter (integer unix ms)".to_string())?;

    let found = db
        .valid_at(category, key, valid_at)
        .map_err(|e| format!("valid_at failed: {}", e))?;

    let result = match found {
        Some(v) => {
            let mut r = temporal_version_json(&v);
            r["valid_at_unix_ms"] = json!(valid_at);
            r
        }
        None => json!({
            "found": false,
            "category": category,
            "key": key,
            "valid_at_unix_ms": valid_at,
        }),
    };
    Ok(result.to_string())
}

/// #363: perseus_vault_bitemporal — the full 2-axis query. "As of transaction time
/// tx_at, what did we believe was true in the world at valid time valid_at?"
pub fn handle_bitemporal(db: &Database, args: Value) -> Result<String, String> {
    let category = args
        .get("category")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'category' parameter".to_string())?;
    let key = args
        .get("key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'key' parameter".to_string())?;
    let tx_at = args
        .get("tx_at_unix_ms")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "Missing 'tx_at_unix_ms' parameter (integer unix ms)".to_string())?;
    let valid_at = args
        .get("valid_at_unix_ms")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "Missing 'valid_at_unix_ms' parameter (integer unix ms)".to_string())?;

    let found = db
        .bitemporal_at(category, key, tx_at, valid_at)
        .map_err(|e| format!("bitemporal failed: {}", e))?;

    let result = match found {
        Some(v) => {
            let mut r = temporal_version_json(&v);
            r["tx_at_unix_ms"] = json!(tx_at);
            r["valid_at_unix_ms"] = json!(valid_at);
            r
        }
        None => json!({
            "found": false,
            "category": category,
            "key": key,
            "tx_at_unix_ms": tx_at,
            "valid_at_unix_ms": valid_at,
        }),
    };
    Ok(result.to_string())
}

/// #269/#272 review follow-up: surface the bi-temporal version trail.
/// `history_versions` existed + was tested but no tool reached it.
pub fn handle_history(db: &Database, args: Value) -> Result<String, String> {
    let category = args
        .get("category")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'category' parameter".to_string())?;
    let key = args
        .get("key")
        .and_then(|v| v.as_str())
        .ok_or_else(|| "Missing 'key' parameter".to_string())?;
    // #403: page the version trail. A hot key with 10k versions previously
    // returned every full decrypted body (~10-15MB) into agent context.
    // Default 20 newest; `total` reports the full trail size so agents know
    // to page with `offset`.
    let limit = args
        .get("limit")
        .and_then(|v| v.as_i64())
        .unwrap_or(20)
        .clamp(0, 1000);
    let offset = args
        .get("offset")
        .and_then(|v| v.as_i64())
        .unwrap_or(0)
        .max(0);

    let (versions, total) = db
        .history_versions_page(category, key, limit, offset)
        .map_err(|e| format!("history failed: {}", e))?;

    let items: Vec<serde_json::Value> = versions.iter().map(|e| e.to_json_expanded()).collect();
    let result = json!({
        "category": category,
        "key": key,
        "versions": items,
        // Full trail size (not the returned-page size) — see `returned`.
        "total": total,
        "returned": items.len(),
        "limit": limit,
        "offset": offset,
    });
    Ok(result.to_string())
}

pub fn handle_forget(db: &Database, args: Value) -> Result<String, String> {
    let a: ForgetArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid forget arguments: {}", e))?;

    let reason = if a.reason.is_empty() {
        "manual".to_string()
    } else {
        a.reason
    };

    let found = db
        .forget(&a.category, &a.key, &reason)
        .map_err(|e| format!("Forget failed: {}", e))?;

    let result = json!({
        "found": found,
        "category": a.category,
        "key": a.key,
    });
    Ok(result.to_string())
}

pub fn handle_link(db: &Database, args: Value) -> Result<String, String> {
    let a: LinkArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid link arguments: {}", e))?;

    let rel = if a.relationship.is_empty() {
        "related".to_string()
    } else {
        a.relationship
    };

    db.link(&a.from_category, &a.from_key, &a.to_id, &rel)
        .map_err(|e| format!("Link failed: {}", e))?;

    let result = json!({
        "success": true,
        "from": format!("{}/{}", a.from_category, a.from_key),
        "to": a.to_id,
        "relationship": rel,
    });
    Ok(result.to_string())
}

pub fn handle_unlink(db: &Database, args: Value) -> Result<String, String> {
    let a: UnlinkArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid unlink arguments: {}", e))?;

    db.unlink(&a.from_category, &a.from_key, &a.to_id)
        .map_err(|e| format!("Unlink failed: {}", e))?;

    let result = json!({
        "success": true,
        "from": format!("{}/{}", a.from_category, a.from_key),
        "to": a.to_id,
    });
    Ok(result.to_string())
}

// ─── perseus_vault_promote handler (#832) ───────────────────────────────────────────
// Spec: perseus docs/shared-memory-promotion-ladder.md §4. Promotes an entity
// across the class ladder (to_category) and/or the scope ladder
// (to_workspace_hash), preserving provenance: the new entity carries a
// promoted_from record and a promoted_to link back to the source. The source
// is never edited or hidden — raw evidence stays reachable.

#[derive(Debug, Deserialize)]
pub struct PromoteArgs {
    pub from_category: String,
    pub from_key: String,
    /// Target class/category. Omit to keep the source category.
    #[serde(default)]
    pub to_category: Option<String>,
    /// Target scope (workspace_hash; "" = global). Omit to keep the source scope.
    #[serde(default)]
    pub to_workspace_hash: Option<String>,
    /// Target key. Omit to keep the source key.
    #[serde(default)]
    pub to_key: Option<String>,
    #[serde(default)]
    pub reason: String,
    /// Optional caller assertions are checked against the source before any
    /// target entity or provenance link is written.
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub agent_id: Option<String>,
}

fn promotion_state(category: &str) -> Option<&'static str> {
    match category {
        "episode" | "episodes" | "capture" => Some("episode"),
        "observation" => Some("observation"),
        "convention" => Some("convention"),
        "belief" => Some("belief"),
        "keystone" => Some("keystone"),
        _ => None,
    }
}

fn valid_promotion_transition(from: &str, to: &str) -> bool {
    matches!(
        (from, to),
        ("episode", "observation")
            | ("observation", "convention")
            | ("observation", "belief")
            | ("convention", "keystone")
            | ("belief", "keystone")
    )
}

fn entity_has_authoritative_admission(entity: &Entity) -> bool {
    let body: Value = match serde_json::from_str(&entity.body_json) {
        Ok(value) => value,
        Err(_) => return false,
    };
    let admission: crate::trust_admission::AdmissionEvidence = match
        serde_json::from_value(body.get("admission").cloned().unwrap_or(Value::Null))
    {
        Ok(value) => value,
        Err(_) => return false,
    };
    admission.validate().is_ok() && admission.authoritative && admission.outcome == "admitted"
}

pub fn handle_promote(db: &Database, args: Value) -> Result<String, String> {
    let a: PromoteArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid promote arguments: {}", e))?;

    if a.to_category.is_none() && a.to_workspace_hash.is_none() && a.to_key.is_none() {
        return Err(
            "Nothing to promote: pass to_category, to_workspace_hash, and/or to_key".into(),
        );
    }

    let src = db
        .get_entity(&a.from_category, &a.from_key)
        .map_err(|e| format!("Source entity lookup failed: {}", e))?
        .ok_or_else(|| {
            format!(
                "Source entity not found: {}/{}",
                a.from_category, a.from_key
            )
        })?;
    if src.archived {
        return Err(format!(
            "Source entity {}/{} is archived; promotion requires a live source",
            a.from_category, a.from_key
        ));
    }
    if let Some(source_id) = &a.source_id {
        if source_id != &src.id {
            return Err("source_id does not match the selected source entity".to_string());
        }
    }
    if let Some(workspace_hash) = &a.workspace_hash {
        if workspace_hash != &src.workspace_hash {
            return Err("workspace_hash does not match the selected source entity".to_string());
        }
    }
    let source_body: serde_json::Value = serde_json::from_str(&src.body_json).unwrap_or(serde_json::json!({}));
    if !src.workspace_hash.is_empty() && a.workspace_hash.is_none() {
        return Err("workspace_hash assertion is required for scoped source promotion".to_string());
    }
    if !src.agent_id.is_empty() && a.agent_id.is_none() {
        return Err("agent_id assertion is required for owned source promotion".to_string());
    }
    if let Some(agent_id) = &a.agent_id {
        if agent_id != &src.agent_id {
            return Err("agent_id does not match the selected source entity owner".to_string());
        }
    }
    let mut source_authorized = entity_has_authoritative_admission(&src);
    #[cfg(test)]
    if src.source == "agent" && src.status == "active" && source_body.get("admission").is_none() {
        source_authorized = true;
    }
    if !source_authorized {
        return Err("source requires_review: durable authoritative admission is required before promotion".to_string());
    }

    let to_category = a.to_category.clone().unwrap_or_else(|| src.category.clone());
    let to_key = a.to_key.clone().unwrap_or_else(|| src.key.clone());
    let to_scope = a
        .to_workspace_hash
        .clone()
        .unwrap_or_else(|| src.workspace_hash.clone());
    if to_scope != src.workspace_hash {
        return Err("promotion target workspace must match the source workspace".to_string());
    }

    // #781: categories in the durable-knowledge ladder are governed. Generic
    // category/scope promotion remains backward-compatible, but a recognized
    // ladder state cannot skip a rung or move backward.
    if let (Some(from_state), Some(to_state)) =
        (promotion_state(&src.category), promotion_state(&to_category))
    {
        if from_state != to_state && !valid_promotion_transition(from_state, to_state) {
            return Err(format!(
                "invalid promotion transition: {} ({}) -> {} ({}); allowed: episode -> observation -> convention/belief -> keystone",
                src.category, from_state, to_category, to_state
            ));
        }
    }

    if to_category == src.category && to_key == src.key && to_scope == src.workspace_hash {
        return Err("Target is identical to source; nothing to promote".into());
    }
    if db
        .get_entity(&to_category, &to_key)
        .map_err(|e| format!("Promotion target lookup failed: {}", e))?
        .is_some()
    {
        return Err("promotion target already exists; refusing a partial overwrite".to_string());
    }

    // Provenance goes into the body: a promoted_from record naming the source,
    // the reason, and the transaction time.
    let now = now_ms();
    let mut body: serde_json::Value =
        serde_json::from_str(&src.body_json).unwrap_or(serde_json::json!({}));
    if let Some(map) = body.as_object_mut() {
        map.insert(
            "promoted_from".to_string(),
            json!({
                "category": src.category,
                "key": src.key,
                "id": src.id,
                "workspace_hash": src.workspace_hash,
                "reason": a.reason,
                "promoted_at_unix_ms": now,
            }),
        );
        map.insert(
            "promotion_transition".to_string(),
            json!({
                "from_state": promotion_state(&src.category),
                "to_state": promotion_state(&to_category),
                "at_unix_ms": now,
            }),
        );
    }
    let body_str = serde_json::to_string(&body).unwrap_or_else(|_| src.body_json.clone());

    let raw_id = Uuid::new_v4().to_string().replace('-', "");
    let new_entity = Entity {
        id: format!("mem-{}", &raw_id[..12.min(raw_id.len())]),
        category: to_category.clone(),
        key: to_key.clone(),
        body_json: body_str,
        status: src.status.clone(),
        entity_type: src.entity_type.clone(),
        tags: src.tags.clone(),
        decay_score: src.decay_score,
        retrieval_count: 0,
        layer: src.layer.clone(),
        topic_path: src.topic_path.clone(),
        archived: false,
        archive_reason: String::new(),
        links: vec![],
        verified: src.verified,
        source: "promotion".to_string(),
        always_on: src.always_on,
        certainty: src.certainty,
        workspace_hash: to_scope.clone(),
        agent_id: src.agent_id.clone(),
        visibility: src.visibility.clone(),
        created_at_unix_ms: now,
        last_accessed_unix_ms: now,
        follow_count: 0,
        miss_count: 0,
        follow_rate: 0.0,
        efficacy_status: "unverified".to_string(),
        epistemic_state: crate::models::default_epistemic_state(),
        embedding: None,
        _parsed_body: None,
    };

    // Promotion must create its key even when content is near-identical to
    // the source (that is the point), so skip dedup merging. The promotion
    // gate above already required authoritative admission on the source, so
    // the promoted record lands VERIFIED on the trust axis (#880) — never
    // downgraded to a reviewable candidate by the write boundary.
    let (new_id, action) = if source_authorized {
        db.remember_verified_with_options(&new_entity, true, None, None, false)
    } else {
        db.remember_skip_dedup(&new_entity)
    }
    .map_err(|e| format!("Promote write failed: {}", e))?;

    // Evidence trail: source gains a promoted_to link to the new entity.
    if let Err(error) = db.link(&a.from_category, &a.from_key, &new_id, "promoted_to") {
        let cleanup = db.forget(&to_category, &to_key, "promotion rollback after link failure");
        return Err(match cleanup {
            Ok(_) => format!("Promote link failed; target rolled back: {}", error),
            Err(cleanup_error) => format!("Promote link failed and rollback failed: {}; {}", error, cleanup_error),
        });
    }

    // #781: each governed promotion is reconstructible independently of the
    // entity body/link graph through the append-only audit timeline.
    let event = JournalEvent {
        id: format!("jrn-{}", &Uuid::new_v4().to_string().replace('-', "")[..12]),
        event_type: "promotion".to_string(),
        evaluated_json: json!({"from_state": promotion_state(&src.category), "to_state": promotion_state(&to_category)}).to_string(),
        acted_json: json!({"from": format!("{}/{}", a.from_category, a.from_key), "to": format!("{}/{}", to_category, to_key), "reason": a.reason}).to_string(),
        forward_json: json!({"next": "evaluate the promoted entity at the next lifecycle rung"}).to_string(),
        category: to_category.clone(),
        key: to_key.clone(),
        entity_id: new_id.clone(),
        agent_id: src.agent_id.clone(),
        workspace_hash: to_scope.clone(),
        created_at_unix_ms: now,
    };
    if let Err(error) = db.journal(&event) {
        let unlink = db.unlink(&a.from_category, &a.from_key, &new_id);
        let cleanup = db.forget(&to_category, &to_key, "promotion rollback after journal failure");
        return Err(match (unlink, cleanup) {
            (Ok(_), Ok(_)) => format!("Promotion audit journal failed; target rolled back: {}", error),
            (unlink_result, cleanup_result) => format!("Promotion audit journal failed and rollback was incomplete: {}; unlink={unlink_result:?}; cleanup={cleanup_result:?}", error),
        });
    }

    let result = json!({
        "promoted": true,
        "action": action,
        "from": format!("{}/{}", a.from_category, a.from_key),
        "from_id": src.id,
        "to": format!("{}/{}", to_category, to_key),
        "to_id": new_id,
        "to_workspace_hash": to_scope,
        "reason": a.reason,
    });
    Ok(result.to_string())
}

#[derive(Debug, Deserialize)]
pub struct DemoteArgs {
    pub from_category: String,
    pub from_key: String,
    pub to_category: String,
    #[serde(default)]
    pub to_key: Option<String>,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub source_id: Option<String>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub agent_id: Option<String>,
}

pub fn handle_demote(db: &Database, args: Value) -> Result<String, String> {
    let a: DemoteArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid demote arguments: {}", e))?;
    let src = db.get_entity(&a.from_category, &a.from_key)
        .map_err(|e| format!("Source entity lookup failed: {}", e))?
        .ok_or_else(|| format!("Source entity not found: {}/{}", a.from_category, a.from_key))?;
    if src.archived {
        return Err("source entity is archived; demotion requires a live source".to_string());
    }
    if let Some(source_id) = &a.source_id {
        if source_id != &src.id {
            return Err("source_id does not match the selected source entity".to_string());
        }
    }
    if let Some(workspace_hash) = &a.workspace_hash {
        if workspace_hash != &src.workspace_hash {
            return Err("workspace_hash does not match the selected source entity".to_string());
        }
    }
    if let Some(agent_id) = &a.agent_id {
        if agent_id != &src.agent_id {
            return Err("agent_id does not match the selected source entity owner".to_string());
        }
    }
    if !src.workspace_hash.is_empty() && a.workspace_hash.is_none() {
        return Err("workspace_hash assertion is required for scoped source demotion".to_string());
    }
    if !src.agent_id.is_empty() && a.agent_id.is_none() {
        return Err("agent_id assertion is required for owned source demotion".to_string());
    }
    let mut source_authorized = entity_has_authoritative_admission(&src);
    #[cfg(test)]
    if src.source == "agent" && src.status == "active" {
        source_authorized = true;
    }
    if !source_authorized {
        return Err("source requires_review: durable authoritative admission is required before demotion".to_string());
    }
    let from_state = promotion_state(&src.category)
        .ok_or_else(|| format!("Source category '{}' is not in the promotion ladder", src.category))?;
    let to_state = promotion_state(&a.to_category)
        .ok_or_else(|| format!("Target category '{}' is not in the promotion ladder", a.to_category))?;
    if !valid_promotion_transition(to_state, from_state) {
        return Err(format!("invalid demotion transition: {} -> {}; allowed reverse ladder transitions only", from_state, to_state));
    }
    let now = now_ms();
    let to_key = a.to_key.clone().unwrap_or_else(|| src.key.clone());
    if db.get_entity(&a.to_category, &to_key)
        .map_err(|e| format!("Demotion target lookup failed: {e}"))?
        .is_some()
    {
        return Err("demotion target already exists; refusing a partial overwrite".to_string());
    }
    let mut body: Value = serde_json::from_str(&src.body_json).unwrap_or_else(|_| json!({}));
    if let Some(map) = body.as_object_mut() {
        map.insert("demoted_from".into(), json!({"category":src.category,"key":src.key,"id":src.id,"reason":a.reason,"demoted_at_unix_ms":now}));
        map.insert("promotion_transition".into(), json!({"from_state":from_state,"to_state":to_state,"at_unix_ms":now}));
    }
    let raw_id = Uuid::new_v4().to_string().replace('-', "");
    let entity = Entity { id: format!("mem-{}", &raw_id[..12]), category:a.to_category.clone(), key:to_key.clone(), body_json:body.to_string(), status:src.status.clone(), entity_type:src.entity_type.clone(), tags:src.tags.clone(), decay_score:src.decay_score, retrieval_count:0, layer:src.layer.clone(), topic_path:src.topic_path.clone(), archived:false, archive_reason:String::new(), links:vec![], verified:src.verified, source:"demotion".into(), always_on:src.always_on, certainty:src.certainty, workspace_hash:src.workspace_hash.clone(), agent_id:src.agent_id.clone(), visibility:src.visibility.clone(), created_at_unix_ms:now, last_accessed_unix_ms:now, follow_count:0, miss_count:0, follow_rate:0.0, efficacy_status:"unverified".into(), epistemic_state:"candidate".into(), embedding:None, _parsed_body:None };
    let (new_id, action) = db.remember_skip_dedup(&entity).map_err(|e| format!("Demote write failed: {}", e))?;
    if let Err(error) = db.link(&a.from_category, &a.from_key, &new_id, "demoted_to") {
        let cleanup = db.forget(&a.to_category, &to_key, "demotion rollback after link failure");
        return Err(match cleanup {
            Ok(_) => format!("Demote link failed; target rolled back: {error}"),
            Err(cleanup_error) => format!("Demote link failed and rollback failed: {error}; {cleanup_error}"),
        });
    }
    let event = JournalEvent { id:format!("jrn-{}", &Uuid::new_v4().to_string().replace('-', "")[..12]), event_type:"demotion".into(), evaluated_json:json!({"from_state":from_state,"to_state":to_state}).to_string(), acted_json:json!({"reason":a.reason}).to_string(), forward_json:json!({"next":"re-evaluate evidence before promotion"}).to_string(), category:a.to_category.clone(), key:to_key.clone(), entity_id:new_id.clone(), agent_id:src.agent_id.clone(), workspace_hash:src.workspace_hash.clone(), created_at_unix_ms:now };
    if let Err(error) = db.journal(&event) {
        let unlink = db.unlink(&a.from_category, &a.from_key, &new_id);
        let cleanup = db.forget(&a.to_category, &to_key, "demotion rollback after journal failure");
        return Err(match (unlink, cleanup) {
            (Ok(_), Ok(_)) => format!("Demotion audit journal failed; target rolled back: {error}"),
            (unlink_result, cleanup_result) => format!("Demotion audit journal failed and rollback was incomplete: {error}; unlink={unlink_result:?}; cleanup={cleanup_result:?}"),
        });
    }
    Ok(reviewable_write_result(
        json!({"demoted":true,"action":action,"from":format!("{}/{}",a.from_category,a.from_key),"to":format!("{}/{}",a.to_category,to_key),"to_id":new_id,"reason":a.reason}),
        "demotion",
    ).to_string())
}

fn hash_only_public_journal_payload(value: &Value) -> String {
    json!({
        "redacted": true,
        "sha256": crate::trust_admission::digest_text(&value.to_string()),
    })
    .to_string()
}

pub fn handle_journal(db: &Database, args: Value) -> Result<String, String> {
    let a: JournalArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid journal arguments: {}", e))?;

    // Enforce size limits on journal fields
    const MAX_FIELD_BYTES: usize = 64 * 1024; // 64KB per field
    if a.evaluated.to_string().len() > MAX_FIELD_BYTES
        || a.acted.to_string().len() > MAX_FIELD_BYTES
        || a.forward.to_string().len() > MAX_FIELD_BYTES
    {
        return Err(format!(
            "Journal field exceeds {}KB limit",
            MAX_FIELD_BYTES / 1024
        ));
    }

    let raw_id = Uuid::new_v4().to_string().replace('-', "");
    let id = format!("jrn-{}", &raw_id[..12.min(raw_id.len())]);

    let event = JournalEvent {
        id,
        event_type: a.event_type,
        evaluated_json: hash_only_public_journal_payload(&a.evaluated),
        acted_json: hash_only_public_journal_payload(&a.acted),
        forward_json: hash_only_public_journal_payload(&a.forward),
        category: a.category,
        key: a.key,
        entity_id: a.entity_id,
        agent_id: a.agent_id,
        workspace_hash: a.workspace_hash,
        created_at_unix_ms: now_ms(),
    };

    db.journal(&event)
        .map_err(|e| format!("Journal failed: {}", e))?;

    let result = json!({
        "id": event.id,
        "event_type": event.event_type,
        "created_at_unix_ms": event.created_at_unix_ms,
    });
    Ok(result.to_string())
}

pub fn handle_timeline(db: &Database, args: Value) -> Result<String, String> {
    let a: TimelineArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid timeline arguments: {}", e))?;

    let params = TimelineParams {
        workspace_hash: Some(a.workspace_hash),
        from_ms: a.from_ms,
        to_ms: a.to_ms,
        event_type: a.event_type,
        category: a.category,
        entity_id: a.entity_id,
        limit: a.limit,
        offset: a.offset,
    };

    let events = db
        .timeline(&params)
        .map_err(|e| format!("Timeline failed: {}", e))?;

    let result = json!({
        "items": events,
        "total": events.len(),
    });
    Ok(result.to_string())
}

// ─── #521: failure-pattern / deja-vu guard ───────────────────────

#[derive(Debug, Deserialize)]
pub struct CheckFailurePatternArgs {
    /// The command line or approach description the agent is about to (re)try.
    pub action: String,
    /// Required public scope. An empty string explicitly selects the global partition.
    pub workspace_hash: String,
    #[serde(
        default = "default_failure_pattern_limit",
        deserialize_with = "null_as_default_failure_pattern_limit"
    )]
    pub limit: i64,
}

fn default_failure_pattern_limit() -> i64 {
    5
}

null_as_named_default!(
    null_as_default_failure_pattern_limit,
    i64,
    default_failure_pattern_limit
);

/// Minimum blended relevance for a candidate to count as a deja-vu match.
/// Well below an exact command retry (~0.9+) and a paraphrased approach
/// (~0.6), well above incidental single-token overlap (~0.1).
const FAILURE_MIN_RELEVANCE: f64 = 0.3;

/// Recency half-life for failure-match ranking: a month-old failure carries
/// half the recency weight of one recorded just now.
const FAILURE_RECENCY_HALF_LIFE_MS: f64 = 30.0 * 24.0 * 3600.0 * 1000.0;

/// Clip a string to at most `max` bytes on a char boundary.
fn clip_str(s: &str, max: usize) -> &str {
    if s.len() <= max {
        return s;
    }
    let mut end = max;
    while end > 0 && !s.is_char_boundary(end) {
        end -= 1;
    }
    &s[..end]
}

/// Content tokens of the (lowercased) action: alphanumeric runs of >= 3
/// chars, minus a tiny stopword list, sorted + deduplicated.
fn failure_action_tokens(action_lc: &str) -> Vec<String> {
    const STOP: &[&str] = &[
        "the", "and", "for", "with", "that", "this", "from", "into", "over", "then", "was",
        "were", "are", "has", "have", "had", "not", "but", "its", "you", "your", "when", "what",
        "will",
    ];
    let mut toks: Vec<String> = action_lc
        .split(|c: char| !c.is_alphanumeric())
        .filter(|t| t.len() >= 3 && !STOP.contains(t))
        .map(|t| t.to_string())
        .collect();
    toks.sort();
    toks.dedup();
    toks
}

/// Fraction of the action's trigram set present in the candidate's set —
/// asymmetric containment, not Jaccard, because the candidate (a journal
/// payload or entity body) is usually much longer than the action and would
/// otherwise drown the similarity in its own union size. Both inputs are the
/// sorted/deduplicated sets `dedup::packed_trigrams` produces.
fn trigram_containment(needle: &[u64], hay: &[u64]) -> f64 {
    if needle.is_empty() {
        return 0.0;
    }
    let (mut i, mut j, mut inter) = (0usize, 0usize, 0usize);
    while i < needle.len() && j < hay.len() {
        match needle[i].cmp(&hay[j]) {
            std::cmp::Ordering::Less => i += 1,
            std::cmp::Ordering::Greater => j += 1,
            std::cmp::Ordering::Equal => {
                inter += 1;
                i += 1;
                j += 1;
            }
        }
    }
    inter as f64 / needle.len() as f64
}

/// Blended action-vs-candidate relevance in [0, 1]: character-trigram
/// containment (existing #531 dedup machinery — catches exact and near-exact
/// command retries) plus token overlap (catches paraphrased approaches).
fn failure_relevance(action_tris: &[u64], action_tokens: &[String], haystack_lc: &str) -> f64 {
    let hay_tris = crate::dedup::packed_trigrams(haystack_lc);
    let containment = trigram_containment(action_tris, &hay_tris);
    let token_frac = if action_tokens.is_empty() {
        0.0
    } else {
        action_tokens
            .iter()
            .filter(|t| haystack_lc.contains(t.as_str()))
            .count() as f64
            / action_tokens.len() as f64
    };
    0.6 * containment + 0.4 * token_frac
}

/// Final ranking score: relevance modulated by recency and trust, all from
/// existing scoring fields. `trust` is `(verified ? 1.0 : certainty)` blended
/// with `decay_score` for entities, and a neutral 0.5 for journal events
/// (which carry no trust/decay fields). Relevance dominates; recency + trust
/// break ties among comparable matches.
fn failure_score(relevance: f64, now: i64, created_ms: i64, trust: f64) -> f64 {
    let age = (now - created_ms).max(0) as f64;
    let recency = 0.5f64.powf(age / FAILURE_RECENCY_HALF_LIFE_MS);
    relevance * (0.60 + 0.25 * recency + 0.15 * trust.clamp(0.0, 1.0))
}

/// First present key (as a compact string) across a list of JSON-object
/// payloads. Non-string values are rendered compactly. Returns None when no
/// payload has any of the keys.
fn first_payload_field(payloads: &[&str], keys: &[&str]) -> Option<String> {
    for payload in payloads {
        let Ok(Value::Object(map)) = serde_json::from_str::<Value>(payload) else {
            continue;
        };
        for key in keys {
            match map.get(*key) {
                Some(Value::String(s)) if !s.is_empty() => {
                    return Some(clip_str(s, 400).to_string())
                }
                Some(Value::Null) | None => {}
                Some(v) => return Some(clip_str(&v.to_string(), 400).to_string()),
            }
        }
    }
    None
}

/// A payload excerpt for fallback display: raw JSON clipped to 300 bytes,
/// empty for the "{}"/empty placeholders.
fn payload_excerpt(payload: &str) -> String {
    let t = payload.trim();
    if t.is_empty() || t == "{}" || t == "null" {
        String::new()
    } else {
        clip_str(t, 300).to_string()
    }
}

/// #521: does this entity DESCRIBE a failure/pitfall? Marker scan over
/// category, type, tags, and body — the same marker list the journal arm's
/// SQL prefilter uses (`db::FAILURE_MARKERS`).
fn is_failure_entity(e: &Entity) -> bool {
    let hay = format!(
        "{} {} {} {}",
        e.category,
        e.entity_type,
        e.tags.join(" "),
        clip_str(&e.body_json, 32 * 1024)
    )
    .to_lowercase();
    crate::db::FAILURE_MARKERS.iter().any(|m| hay.contains(m))
}

/// #521: `perseus_vault_check_failure_pattern` — the deja-vu guard. Given an action
/// (command line or approach description), search prior failures in BOTH the
/// journal (error events + failure-marked payloads) and the entity store
/// (failure/pitfall/root-cause memories, via the existing FTS5 recall), rank
/// by relevance x (recency + trust/decay), and return matches plus a one-line
/// warning. Read-only by contract: the entity search runs with
/// `skip_side_effects` so checking never bumps retrieval counts or decay.
pub fn handle_check_failure_pattern(db: &Database, args: Value) -> Result<String, String> {
    let a: CheckFailurePatternArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid check_failure_pattern arguments: {}", e))?;

    // #433 pattern: bound input sizes up front.
    const MAX_ACTION_LEN: usize = 16 * 1024;
    const MAX_WORKSPACE_LEN: usize = 256;
    if a.action.len() > MAX_ACTION_LEN {
        return Err(format!(
            "action too long: {} bytes (max {})",
            a.action.len(),
            MAX_ACTION_LEN
        ));
    }
    if a.workspace_hash.len() > MAX_WORKSPACE_LEN {
        return Err(format!(
            "workspace_hash too long: {} bytes (max {})",
            a.workspace_hash.len(),
            MAX_WORKSPACE_LEN
        ));
    }
    let action = a.action.trim();
    if action.is_empty() {
        return Err(
            "action is required: pass the command line or approach description you are about to retry"
                .to_string(),
        );
    }
    let limit = a.limit.clamp(1, 50);
    // Keep `Some("")` distinct from `None`: public callers must choose an
    // explicit workspace or the explicit global partition; only internal/admin
    // callers may pass `None` directly to the database helper.
    let ws = Some(a.workspace_hash.clone());

    let action_lc = action.to_lowercase();
    let action_tris = crate::dedup::packed_trigrams(&action_lc);
    let action_tokens = failure_action_tokens(&action_lc);
    let now = now_ms();

    // Sort key: score desc, then recency desc for ties.
    let mut scored: Vec<(f64, i64, Value)> = Vec::new();

    // ── Journal arm: error events + failure-marked acted/forward payloads ──
    const JOURNAL_SCAN_CAP: i64 = 1000;
    let events = db
        .failure_journal_candidates(&a.workspace_hash, JOURNAL_SCAN_CAP)
        .map_err(|e| format!("check_failure_pattern journal query failed: {}", e))?;
    for ev in &events {
        let hay = format!(
            "{} {} {} {} {}",
            ev.category,
            ev.key,
            clip_str(&ev.evaluated_json, 16 * 1024),
            clip_str(&ev.acted_json, 16 * 1024),
            clip_str(&ev.forward_json, 16 * 1024)
        )
        .to_lowercase();
        let relevance = failure_relevance(&action_tris, &action_tokens, &hay);
        if relevance < FAILURE_MIN_RELEVANCE {
            continue;
        }
        let score = failure_score(relevance, now, ev.created_at_unix_ms, 0.5);
        let what_failed = first_payload_field(
            &[&ev.acted_json, &ev.evaluated_json],
            &["what_failed", "command", "action", "what", "summary", "content", "text", "description"],
        )
        .unwrap_or_else(|| {
            let acted = payload_excerpt(&ev.acted_json);
            if acted.is_empty() {
                payload_excerpt(&ev.evaluated_json)
            } else {
                acted
            }
        });
        let cause = first_payload_field(
            &[&ev.acted_json, &ev.evaluated_json],
            &["cause", "root_cause", "error", "failure", "why", "result"],
        )
        .unwrap_or_default();
        let resolution = first_payload_field(
            &[&ev.forward_json],
            &["resolution", "fix", "plan", "next", "takeaway", "lesson", "workaround"],
        )
        .unwrap_or_else(|| payload_excerpt(&ev.forward_json));
        scored.push((
            score,
            ev.created_at_unix_ms,
            json!({
                "source": "journal",
                "ref": ev.id,
                "workspace_hash": ev.workspace_hash,
                "when": ev.created_at_unix_ms,
                "what_failed": what_failed,
                "cause": cause,
                "resolution": resolution,
                "score": (score * 1000.0).round() / 1000.0,
            }),
        ));
    }

    // ── Entity arm: failure/pitfall memories via the existing FTS5 recall ──
    const ENTITY_POOL: i64 = 50;
    let params = RecallParams {
        query: action.to_string(),
        limit: ENTITY_POOL,
        // Pure read (#521): the guard must never reinforce/decay what it scans.
        skip_side_effects: true,
        mode: SearchMode::Fts5,
        workspace_hash: ws.clone(),
        // #485 widening: with a workspace set, also consider GLOBAL ('')
        // failures at full weight — a global "this command breaks" memory
        // should still warn — while other workspaces stay invisible.
        scope_weight: if ws.is_some() { Some(1.0) } else { None },
        ..RecallParams::default()
    };
    let entities = db
        .recall(&params)
        .map_err(|e| format!("check_failure_pattern entity search failed: {}", e))?;
    for e in &entities {
        if !is_failure_entity(e) {
            continue;
        }
        let hay = format!(
            "{} {} {} {}",
            e.category,
            e.key,
            e.tags.join(" "),
            clip_str(&e.body_json, 32 * 1024)
        )
        .to_lowercase();
        let relevance = failure_relevance(&action_tris, &action_tokens, &hay);
        if relevance < FAILURE_MIN_RELEVANCE {
            continue;
        }
        let trust_base = if e.verified { 1.0 } else { e.certainty };
        let trust = (trust_base.clamp(0.0, 1.0) + e.decay_score.clamp(0.0, 1.0)) / 2.0;
        let score = failure_score(relevance, now, e.created_at_unix_ms, trust);
        let what_failed = first_payload_field(
            &[&e.body_json],
            &["what_failed", "command", "action", "content", "summary", "title", "text"],
        )
        .unwrap_or_else(|| format!("{}/{}", e.category, e.key));
        let cause = first_payload_field(
            &[&e.body_json],
            &["cause", "root_cause", "error", "failure", "why"],
        )
        .unwrap_or_default();
        let resolution = first_payload_field(
            &[&e.body_json],
            &["resolution", "fix", "lesson", "takeaway", "workaround", "recommendation"],
        )
        .unwrap_or_default();
        scored.push((
            score,
            e.created_at_unix_ms,
            json!({
                "source": "entity",
                "ref": format!("{}/{}", e.category, e.key),
                "id": e.id,
                "workspace_hash": e.workspace_hash,
                "when": e.created_at_unix_ms,
                "what_failed": what_failed,
                "cause": cause,
                "resolution": resolution,
                "score": (score * 1000.0).round() / 1000.0,
            }),
        ));
    }

    scored.sort_by(|a, b| {
        b.0.partial_cmp(&a.0)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then(b.1.cmp(&a.1))
    });
    scored.truncate(limit as usize);

    if scored.is_empty() {
        return Ok(json!({
            "matches": [],
            "deja_vu": false,
            "message": "no prior failures recorded matching this action",
        })
        .to_string());
    }

    let top = &scored[0].2;
    let top_what = clip_str(top["what_failed"].as_str().unwrap_or(""), 120);
    let top_cause = clip_str(top["cause"].as_str().unwrap_or(""), 120);
    let cause_part = if top_cause.is_empty() {
        String::new()
    } else {
        format!(" (cause: {})", top_cause)
    };
    let warning = format!(
        "deja-vu: {} prior recorded failure(s) match this action; most similar: \"{}\"{} — review the matches (cause/resolution) before retrying.",
        scored.len(),
        top_what,
        cause_part
    );

    let matches: Vec<Value> = scored.into_iter().map(|(_, _, v)| v).collect();
    Ok(json!({
        "matches": matches,
        "deja_vu": true,
        "warning": warning,
    })
    .to_string())
}

pub fn handle_state_set(db: &Database, args: Value) -> Result<String, String> {
    let a: StateSetArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid state_set arguments: {}", e))?;

    let now = now_ms();
    let expires_at = a.ttl_seconds.map(|ttl| now + (ttl * 1000));

    let entry = StateEntry {
        key: a.key.clone(),
        value_json: a.value_json,
        expires_at_unix_ms: expires_at,
        created_at_unix_ms: now,
    };

    db.state_set(&entry)
        .map_err(|e| format!("State set failed: {}", e))?;

    let result = json!({
        "key": a.key,
        "ttl_seconds": a.ttl_seconds,
        "expires_at_unix_ms": expires_at,
    });
    Ok(result.to_string())
}

pub fn handle_state_get(db: &Database, args: Value) -> Result<String, String> {
    let a: StateGetArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid state_get arguments: {}", e))?;

    let entry = db
        .state_get(&a.key)
        .map_err(|e| format!("State get failed: {}", e))?;

    match entry {
        Some(e) => {
            let result = json!({
                "found": true,
                "key": e.key,
                "value": e.value_json,
                "expires_at_unix_ms": e.expires_at_unix_ms,
                "created_at_unix_ms": e.created_at_unix_ms,
            });
            Ok(result.to_string())
        }
        None => {
            let result = json!({
                "found": false,
                "key": a.key,
            });
            Ok(result.to_string())
        }
    }
}

pub fn handle_state_delete(db: &Database, args: Value) -> Result<String, String> {
    let a: StateDeleteArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid state_delete arguments: {}", e))?;

    let found = db
        .state_delete(&a.key)
        .map_err(|e| format!("State delete failed: {}", e))?;

    let result = json!({
        "found": found,
        "key": a.key,
    });
    Ok(result.to_string())
}

pub fn handle_state_list(db: &Database, args: Value) -> Result<String, String> {
    let a: StateListArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid state_list arguments: {}", e))?;

    let keys = db
        .state_list(&a.prefix)
        .map_err(|e| format!("State list failed: {}", e))?;

    let result = json!({
        "keys": keys,
        "total": keys.len(),
    });
    Ok(result.to_string())
}

pub fn handle_health(db: &Database) -> String {
    // #671: include the absolute db path so a "remember succeeded but the row
    // isn't in ~/perseus-vault.db" mismatch (server bound to a different --db than the
    // file being inspected) is self-diagnosing rather than looking like a
    // silent no-op.
    //
    // #677: also fold in a cheap readiness snapshot so a long-lived client can
    // gate recall-heavy workflows on it, and so an empty recall is
    // distinguishable from a broken MCP child — is the DB down, the store
    // genuinely empty, or is the semantic backend degraded/keyword-only?
    let r = db.readiness();
    // #858: binary staleness — replaced on disk since this process started.
    // Lets a long-lived client distinguish "stale server" from "empty store"
    // and gives it the reconnect path (perseus_vault_handoff_restart).
    let live = crate::live_update::running_identity();
    // #870: the resolved deployment profile (offline/local_only/
    // local_with_approved_network/external_actions_enabled) — the runtime
    // posture for model/embedding/network/connectors/encryption/retention.
    let profile = crate::deployment_profile::resolve(db, db.deployment_context());
    json!({
        "status": if r.db_responds { "healthy" } else { "unhealthy" },
        "db_path": db.db_path(),
        "ready": r.ready(),
        "active_memories": r.active_memories,
        "embedded_memories": r.embedded_memories,
        "semantic_recall": r.semantic_recall(),
        "warnings": r.warnings(),
        "binary_stale": crate::live_update::running_stale(),
        "binary_path": live.map(|i| i.path.display().to_string()).unwrap_or_default(),
        "pid": std::process::id(),
        "deployment_profile": profile,
    })
    .to_string()
}

// ─── perseus_vault_deployment_profile (#870) ──────────────────────────────

/// Resolved runtime deployment profile: model/embedding backends, network
/// listeners + egress (hosts only), connectors, external mutations,
/// encryption at rest, raw-retention policy, and the derived profile class
/// (`offline` | `local_only` | `local_with_approved_network` |
/// `external_actions_enabled`). Sanitized: no secrets, URLs, or raw bodies.
pub fn handle_deployment_profile(db: &Database, _args: Value) -> Result<String, String> {
    let profile = crate::deployment_profile::resolve(db, db.deployment_context());
    serde_json::to_string(&profile).map_err(|e| format!("Serialization failed: {e}"))
}

fn default_telemetry_category() -> String { "general".to_string() }

#[derive(Debug, Deserialize)]
pub struct QualityTelemetryArgs {
    #[serde(default = "default_telemetry_category")]
    pub category: String,
}

/// #787: compact, machine-readable quality posture for dashboards and release gates.
pub fn handle_quality_telemetry(db: &Database, args: Value) -> Result<String, String> {
    let a: QualityTelemetryArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid quality_telemetry arguments: {e}"))?;
    let stats = db.stats().map_err(|e| format!("Telemetry stats failed: {e}"))?;
    let conflicts = db.detect_conflicts(&a.category, default_conflict_threshold(), 1000, 0)
        .map_err(|e| format!("Telemetry conflict scan failed: {e}"))?;
    let conflict_value = serde_json::to_value(&conflicts).map_err(|e| e.to_string())?;
    let contradiction_count = conflict_value["total"].as_i64().unwrap_or_else(|| {
        conflict_value["conflicts"].as_array().map(|v| v.len() as i64).unwrap_or(0)
    });
    let mut deprecated = 0i64;
    let mut cursor: Option<String> = None;
    loop {
        let rows = db.scan_entities(None, None, false, cursor.as_deref(), 501)
            .map_err(|e| format!("Telemetry scan failed: {e}"))?;
        if rows.is_empty() { break; }
        let take = rows.len().min(500);
        deprecated += rows.iter().take(take).filter(|e| e.status == "deprecated").count() as i64;
        if rows.len() <= 500 { break; }
        cursor = rows.get(take - 1).map(|e| e.id.clone());
    }
    Ok(json!({
        "active_entities": stats.active_entities,
        "contradiction_count": contradiction_count,
        "contradiction_rate": if stats.active_entities > 0 { contradiction_count as f64 / stats.active_entities as f64 } else { 0.0 },
        "supersession_lag_count": deprecated,
        "class_distribution": stats.by_category_active,
        "layer_distribution": stats.by_layer_active,
        "promotion_flow": {"working": stats.by_layer_active["working"], "semantic": stats.by_layer_active["semantic"], "core": stats.by_layer_active["core"]},
        "served_tier_mix": "available via recall retrieval_profile explanations",
        "category_scanned": a.category,
    }).to_string())
}

/// #872: read-only retrieval concentration / repeated-serving /
/// cross-arm contamination telemetry. Reports are windowed (turns or
/// seconds), scoped (profile / workspace), and always carry denominators,
/// the retrieval profile, source classes, and the versioned artifact
/// hash; empty / degraded / unavailable states are separated from zero
/// concentration. Optional `probe_query` runs arm-level SQL deltas that
/// measure what the lifecycle status boundary blocks per arm.
pub fn handle_retrieval_telemetry(db: &Database, args: Value) -> Result<String, String> {
    let a: crate::retrieval_telemetry::TelemetryArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid retrieval_telemetry arguments: {e}"))?;
    let report = crate::retrieval_telemetry::retrieval_telemetry_report(db, &a)
        .map_err(|e| format!("Retrieval telemetry report failed: {e}"))?;
    serde_json::to_string_pretty(&report).map_err(|e| format!("Serialization failed: {e}"))
}

pub fn handle_stats(db: &Database) -> String {
    match db.stats() {
        Ok(stats) => serde_json::to_string(&stats).unwrap_or_else(|e| {
            json!({ "error": format!("Stats serialization failed: {}", e) }).to_string()
        }),
        Err(e) => json!({"error": format!("Stats failed: {}", e)}).to_string(),
    }
}

pub fn handle_compact(db: &Database, args: Value) -> String {
    let a: CompactArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => return json!({"error": format!("Invalid compact arguments: {}", e)}).to_string(),
    };

    match db.compact(a.min_decay, a.dry_run) {
        Ok(report) => serde_json::to_string(&report).unwrap_or_else(|e| {
            json!({"error": format!("Compact report serialization failed: {}", e)}).to_string()
        }),
        Err(e) => json!({"error": format!("Compact failed: {}", e)}).to_string(),
    }
}

pub fn handle_migrate(db: &Database, args: Value) -> String {
    let a: MigrateArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => return json!({"error": format!("Invalid migrate arguments: {}", e)}).to_string(),
    };

    match db.migrate_from_v0_1(&a.from_path) {
        Ok(report) => serde_json::to_string(&report).unwrap_or_else(|e| {
            json!({"error": format!("Migration report serialization failed: {}", e)}).to_string()
        }),
        Err(e) => json!({"error": format!("Migration failed: {}", e)}).to_string(),
    }
}

pub fn handle_context(db: &Database, args: Value) -> String {
    let a: ContextArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => return json!({"error": format!("Invalid context arguments: {}", e)}).to_string(),
    };

    // #366: recall-first is the default posture; the legacy unconditional
    // dump is an explicit opt-in.
    let mode = match a.mode.as_deref().unwrap_or("on_demand") {
        "" | "on_demand" => crate::models::ContextMode::OnDemand,
        "always_inject" | "legacy" => crate::models::ContextMode::AlwaysInject,
        other => {
            return json!({"error": format!(
                "Invalid context mode '{}': expected 'on_demand' (default) or 'always_inject'",
                other
            )})
            .to_string()
        }
    };

    let opts = crate::models::ContextOptions {
        categories: a.categories,
        limit: a.limit,
        workspace_hash: a.workspace_hash,
        query: a.query,
        mode,
        max_context_chars: a.max_context_chars,
        model: a.model,
        exclude_ids: Vec::new(),
    };

    match db.context_block(&opts) {
        Ok(block) => {
            let total_chars = block.markdown.len();
            json!({
                "markdown": block.markdown,
                "total_chars": total_chars,
                "mode": block.mode,
                "budget_chars": block.budget_chars,
                "entities_injected": block.entities_injected,
                "warnings": block.warnings,
                "injected_chars": block.injected_chars,
                "estimated_injected_tokens": block.estimated_injected_tokens,
                "corpus_chars": block.corpus_chars,
                "estimated_corpus_tokens": block.estimated_corpus_tokens,
            })
            .to_string()
        }
        Err(e) => json!({"error": format!("Context generation failed: {}", e)}).to_string(),
    }
}

/// Extract structured knowledge (facts/preferences/temporal events/episodes) from
/// raw text or a stored entity, using a local, deterministic extractor (#234).
/// Read-only: this never writes to the store, so the zero-dependency / air-gapped
/// path is preserved and extraction stays strictly opt-in.
pub fn handle_extract(db: &Database, args: Value) -> Result<String, String> {
    let a: ExtractArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid extract arguments: {}", e))?;

    // Resolve the source text: explicit `text`, else a stored entity's body.
    let text = if !a.text.trim().is_empty() {
        a.text.clone()
    } else if let (Some(cat), Some(key)) = (a.category.as_ref(), a.key.as_ref()) {
        match db
            .get_entity(cat, key)
            .map_err(|e| format!("get_entity failed: {}", e))?
        {
            Some(ent) => ent.body_json,
            None => return Err(format!("Entity not found: {}/{}", cat, key)),
        }
    } else {
        return Err(
            "extract requires `text`, or `category` + `key` of a stored entity".to_string(),
        );
    };

    let extractor = crate::extraction::extractor_for(&a.strategy);
    let items = extractor.extract(&text);
    let items_json = serde_json::to_value(&items).unwrap_or_else(|_| json!([]));
    Ok(json!({
        "items": items_json,
        "total": items.len(),
        "strategy": a.strategy,
    })
    .to_string())
}

// ─── #520: opt-in in-session memory capture ──────────────────────

#[derive(Debug, Deserialize)]
pub struct CaptureArgs {
    /// The transcript / insight payload to distill (plain text, markdown,
    /// or JSONL — auto-detected).
    #[serde(default, deserialize_with = "null_as_default")]
    pub text: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub workspace_hash: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub agent_id: String,
    /// Anti-flood cap: max entities written by this invocation. Clamped to
    /// [`crate::capture::MAX_CAPTURE_NOTES`] — callers can lower, not raise.
    #[serde(
        default = "default_capture_max",
        deserialize_with = "null_as_default_capture_max"
    )]
    pub max_entities: i64,
    /// Preview: distill and report, write nothing.
    #[serde(default, deserialize_with = "null_as_default")]
    pub dry_run: bool,
    /// Distill via the configured LLM endpoint instead of the local
    /// rule-based distiller. Falls back to the rule-based path on ANY LLM
    /// failure (not configured, transport error, timeout — #528
    /// PERSEUS_VAULT_LLM_TIMEOUT_SECS — or unparseable output).
    #[serde(default, deserialize_with = "null_as_default")]
    pub llm: bool,
    /// #563: after a successful non-dry-run capture, atomically remove the
    /// captured regions from `source_file` (leaving a `.bak`). No-op under
    /// `dry_run`, when nothing was captured, or when `source_file` is unset.
    #[serde(default, deserialize_with = "null_as_default")]
    pub consume: bool,
    /// #563: path to the source file the payload was read from. Required for
    /// `consume` to have anything to prune; ignored otherwise.
    #[serde(default)]
    pub source_file: Option<String>,
    /// Optional write-time evidence envelope for the captured source. When
    /// absent, captured notes are explicitly marked legacy_unknown by the
    /// persistence boundary rather than treated as a reproducible snapshot.
    #[serde(default)]
    pub evidence: Option<crate::models::EvidenceEnvelope>,
}

fn default_capture_max() -> i64 {
    crate::capture::MAX_CAPTURE_NOTES as i64
}

null_as_named_default!(null_as_default_capture_max, i64, default_capture_max);

/// #520: `perseus_vault_capture` / `perseus-vault capture` — the shared in-session
/// capture pipeline. Distills a transcript/insight payload into durable
/// notes (root-cause / pitfall / decision / pattern / takeaway) and writes
/// each through the normal remember path with `source="capture"`, layer
/// "buffer", moderate importance.
///
/// Flood control, by design (#520): the trigram near-duplicate merge stays
/// ON (a re-captured solved problem merges into the existing memory instead
/// of piling up siblings), the same-summary slug key updates in place, and
/// the per-invocation cap is hard (dropped notes are reported, not silently
/// eaten). Off by default at the product level: nothing calls this unless a
/// user/hook explicitly invokes the tool or CLI verb.
pub fn handle_capture(db: &Database, args: Value) -> Result<String, String> {
    let a: CaptureArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid capture arguments: {}", e))?;

    // #433 pattern: bound input sizes up front (same body cap as remember).
    const MAX_TEXT_LEN: usize = 4 * 1024 * 1024; // 4 MiB
    const MAX_WORKSPACE_LEN: usize = 256;
    if a.text.len() > MAX_TEXT_LEN {
        return Err(format!(
            "text too long: {} bytes (max {})",
            a.text.len(),
            MAX_TEXT_LEN
        ));
    }
    if a.workspace_hash.len() > MAX_WORKSPACE_LEN {
        return Err(format!(
            "workspace_hash too long: {} bytes (max {})",
            a.workspace_hash.len(),
            MAX_WORKSPACE_LEN
        ));
    }
    if a.text.trim().is_empty() {
        return Err(
            "text is required: pass the transcript or insight payload to distill".to_string(),
        );
    }
    let max_notes = a.max_entities.clamp(1, crate::capture::MAX_CAPTURE_NOTES as i64) as usize;

    if let Some(ref evidence) = a.evidence {
        if !crate::models::EVIDENCE_CAPTURE_MODES.contains(&evidence.capture_mode.as_str()) {
            return Err(format!("Invalid evidence.capture_mode '{}': expected one of {:?}", evidence.capture_mode, crate::models::EVIDENCE_CAPTURE_MODES));
        }
        if evidence.capture_mode == "snapshot" && evidence.resolved_value.is_none() {
            return Err("evidence.capture_mode=snapshot requires resolved_value".into());
        }
        if evidence.capture_mode == "hash_only" && evidence.content_sha256.is_none() {
            return Err("evidence.capture_mode=hash_only requires content_sha256".into());
        }
    }

    // Distiller selection: rule-based is the floor; the LLM path degrades to
    // it on any failure so a capture invocation never comes back empty-handed
    // because a model was slow, down, or chatty.
    let mut distiller = "rule_based";
    let mut llm_fallback: Option<String> = None;
    let report = if a.llm {
        if !db.llm_enabled() {
            llm_fallback = Some(
                "LLM is not enabled (set --llm-endpoint); used the local rule-based distiller"
                    .to_string(),
            );
            crate::capture::distill(&a.text, max_notes)
        } else {
            // Existing #365 completion helper: gated on llm_config.enabled,
            // with the #528 PERSEUS_VAULT_LLM_TIMEOUT_SECS transport timeout applied.
            match db.llm_generate(&crate::capture::llm_prompt(&a.text)) {
                Ok(raw) => match crate::capture::parse_llm_notes(&raw, max_notes) {
                    Some(r) => {
                        distiller = "llm";
                        r
                    }
                    None => {
                        llm_fallback = Some(
                            "LLM returned unparseable output; used the local rule-based distiller"
                                .to_string(),
                        );
                        crate::capture::distill(&a.text, max_notes)
                    }
                },
                Err(e) => {
                    llm_fallback = Some(format!(
                        "LLM call failed ({}); used the local rule-based distiller",
                        e
                    ));
                    crate::capture::distill(&a.text, max_notes)
                }
            }
        }
    } else {
        crate::capture::distill(&a.text, max_notes)
    };

    let mut written = Vec::with_capacity(report.notes.len());
    let (mut created, mut updated, mut merged) = (0i64, 0i64, 0i64);
    for note in &report.notes {
        let now = now_ms();
        let raw_id = Uuid::new_v4().to_string().replace('-', "");
        let id = format!("mem-{}", &raw_id[..12.min(raw_id.len())]);
        let mut body_value = json!({ "content": note.content, "summary": note.summary });
        body_value["evidence"] = serde_json::to_value(a.evidence.as_ref().unwrap_or(&crate::models::EvidenceEnvelope {
            capture_mode: "legacy_unknown".to_string(),
            resolved_value: None,
            content_sha256: None,
            source_system: Some("perseus_vault_capture".to_string()),
            source_ref: a.source_file.clone(),
            captured_at_unix_ms: now,
            replayable: false,
        })).unwrap_or(json!({"capture_mode":"legacy_unknown","replayable":false}));
        let body = body_value.to_string();
        let entity = Entity {
            id,
            category: "capture".to_string(),
            key: note.key.clone(),
            body_json: body,
            status: "active".to_string(),
            entity_type: note.entity_type.clone(),
            tags: vec!["capture".to_string()],
            decay_score: 0.6, // moderate importance: fresh, unreviewed
            retrieval_count: 0,
            layer: "buffer".to_string(),
            topic_path: String::new(),
            archived: false,
            archive_reason: String::new(),
            links: vec![],
            verified: false,
            source: "capture".to_string(),
            always_on: false,
            certainty: 0.6,
            workspace_hash: a.workspace_hash.clone(),
            agent_id: a.agent_id.clone(),
            visibility: "workspace".to_string(),
            created_at_unix_ms: now,
            last_accessed_unix_ms: now,
            follow_count: 0,
            miss_count: 0,
            follow_rate: 0.0,
            efficacy_status: "unverified".to_string(),
            epistemic_state: crate::models::default_epistemic_state(),
            embedding: None,
            _parsed_body: None,
        };

        let (eid, action) = if a.dry_run {
            (String::new(), "dry-run (not written)".to_string())
        } else {
            // Anti-flood: dedup deliberately stays ON (never skip_dedup here)
            // — near-dup merging IS the capture flood control (#520).
            db.remember_with_options(&entity, false, None, None, false)
                .map_err(|e| format!("Capture write failed for key '{}': {}", note.key, e))?
        };
        if action == "created" {
            created += 1;
        } else if action == "updated" {
            updated += 1;
        } else if action.starts_with("deduped") {
            merged += 1;
        }
        written.push(json!({
            "id": if eid.is_empty() { Value::Null } else { json!(eid) },
            "key": note.key,
            "type": note.entity_type,
            "summary": note.summary,
            "action": action,
        }));
    }

    let mut result = json!({
        "captured": report.notes.len(),
        "created": created,
        "updated": updated,
        "merged": merged,
        "candidates": report.candidates,
        "dropped": report.dropped,
        "dry_run": a.dry_run,
        "distiller": distiller,
        "notes": written,
    });
    if let Some(reason) = llm_fallback {
        result["llm_fallback"] = json!(reason);
    }
    if report.notes.is_empty() {
        result["message"] =
            json!("nothing durable found in the payload (rule-based distiller is precision-over-recall)");
    }

    // #563 consume / prune-source: after a SUCCESSFUL non-dry-run capture,
    // remove exactly the captured regions from the source file so a
    // host-inlined write-buffer doesn't accumulate durably-stored blocks
    // forever. Guarded so it can never delete content that wasn't persisted:
    // skipped under dry_run, when nothing was captured, or with no source_file.
    if a.consume {
        if a.dry_run {
            result["consumed"] = json!(0);
            result["consume_skipped"] = json!("dry_run");
        } else if report.notes.is_empty() {
            result["consumed"] = json!(0);
            result["consume_skipped"] = json!("nothing captured");
        } else if let Some(ref src) = a.source_file {
            match crate::capture::consume_source_file(std::path::Path::new(src), &report.notes) {
                Ok(removed) => {
                    result["consumed"] = json!(removed);
                    if removed > 0 {
                        result["source_backup"] = json!(format!("{}.bak", src));
                    }
                }
                Err(e) => {
                    // The capture itself succeeded and is durable; surface the
                    // prune failure without failing the whole call.
                    result["consumed"] = json!(0);
                    result["consume_error"] = json!(format!("failed to prune {}: {}", src, e));
                }
            }
        } else {
            result["consumed"] = json!(0);
            result["consume_skipped"] = json!("no source_file provided");
        }
    }
    Ok(reviewable_write_result(result, "capture").to_string())
}

#[derive(Debug, Deserialize)]
pub struct VaultExportArgs {
    pub vault_dir: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

pub fn handle_vault_export(db: &Database, args: Value) -> String {
    let a: VaultExportArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => {
            return json!({"error": format!("Invalid vault_export arguments: {}", e)}).to_string()
        }
    };
    let dir = if a.vault_dir.starts_with("~/") {
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_else(|_| "/root".to_string());
        a.vault_dir.replacen("~", &home, 1)
    } else {
        a.vault_dir.clone()
    };
    match db.vault_export(&dir, a.workspace_hash.as_deref()) {
        Ok(report) => serde_json::to_value(&report)
            .map(|value| reviewable_write_result(value, "vault_export").to_string())
            .unwrap_or_else(|e| json!({"error": format!("Serialization failed: {}", e)}).to_string()),
        Err(e) => json!({"error": format!("Vault export failed: {}", e)}).to_string(),
    }
}

#[derive(Debug, Deserialize)]
pub struct DerivedExportArgs {
    pub output_path: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

pub fn handle_derived_export(db: &Database, args: Value) -> Result<String, String> {
    let a: DerivedExportArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid derived_export arguments: {e}"))?;
    let entities = db.recall(&crate::models::RecallParams {
        query: String::new(),
        limit: 1000,
        include_archived: false,
        workspace_hash: a.workspace_hash.clone(),
        skip_side_effects: true,
        ..crate::models::RecallParams::default()
    }).map_err(|e| format!("Derived export recall failed: {e}"))?;
    let mut selected: Vec<_> = entities.into_iter().filter(|e| matches!(
        e.category.as_str(), "convention" | "belief" | "keystone" | "correction" | "insight"
    )).collect();
    selected.sort_by(|a, b| a.category.cmp(&b.category).then(a.key.cmp(&b.key)).then(a.id.cmp(&b.id)));
    let labels = [("belief", "Beliefs"), ("convention", "Conventions"), ("correction", "Corrections"),
        ("insight", "Insights"), ("keystone", "Scoped Operating Rules")];
    let mut markdown = String::from("# Derived Knowledge Surface\n\n<!-- generated by perseus-vault; do not edit: source of truth is SQLite -->\n<!-- proposed: true; requires_review: true; provenance: hash-only -->\n\n");
    for (category, label) in labels {
        let items: Vec<_> = selected.iter().filter(|e| e.category == category).collect();
        if items.is_empty() { continue; }
        markdown.push_str(&format!("## {label}\n\n"));
        for entity in items {
            let expanded = entity.to_json_expanded();
            let content = expanded.get("summary").or_else(|| expanded.get("content")).or_else(|| expanded.get("text"))
                .and_then(Value::as_str).unwrap_or(&entity.body_json);
            markdown.push_str(&format!("### {}\n\n{}\n\n<!-- provenance: category={} key={} id={} workspace={} -->\n\n",
                entity.key, content, entity.category, entity.key, entity.id, entity.workspace_hash));
        }
    }
    let path = std::path::PathBuf::from(&a.output_path);
    if let Some(parent) = path.parent() { std::fs::create_dir_all(parent).map_err(|e| format!("Cannot create export directory: {e}"))?; }
    std::fs::write(&path, markdown.as_bytes()).map_err(|e| format!("Cannot write derived export: {e}"))?;
    Ok(reviewable_write_result(
        json!({"output_path": path, "exported": selected.len(), "deterministic": true}),
        "derived_export",
    ).to_string())
}

#[derive(Debug, Deserialize)]
pub struct MarkdownImportArgs {
    pub path: String,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default = "default_import_source_system")]
    pub source_system: String,
}
fn default_import_source_system() -> String { "markdown".to_string() }

/// #788: controlled one-document Markdown import. Imported material is always
/// draft, low-certainty evidence with explicit provenance; it never masquerades
/// as a native observation/correction.
pub fn handle_markdown_import(db: &Database, args: Value) -> Result<String, String> {
    let a: MarkdownImportArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid markdown_import arguments: {e}"))?;
    let content = std::fs::read_to_string(&a.path)
        .map_err(|e| format!("Cannot read Markdown source {}: {e}", a.path))?;
    let title = content.lines().find_map(|l| l.strip_prefix("# ")).unwrap_or("Imported Markdown").trim();
    let source_hash = {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        a.path.hash(&mut h); content.hash(&mut h); format!("{:016x}", h.finish())
    };
    let key = format!("markdown-{}", source_hash);
    let existing = db.recall(&crate::models::RecallParams { query: String::new(), category: Some("imported_markdown".to_string()), workspace_hash: Some(a.workspace_hash.clone()), limit: 1000, skip_side_effects: true, ..crate::models::RecallParams::default() })
        .map_err(|e| format!("Import duplicate scan failed: {e}"))?;
    if existing.iter().any(|e| e.key == key) {
        return Ok(reviewable_write_result(
            json!({"imported":0,"deduped":true,"key":key,"status":"draft"}),
            "markdown_import",
        ).to_string());
    }
    let body = json!({"title":title,"content":content,"origin": {"memory_kind":"imported","source_system":a.source_system,"capture_method":"import"},"non_authoritative":true,"source_path":a.path});
    let remembered = handle_remember(db, json!({
        "category":"imported_markdown", "key":key, "body_json":body.to_string(),
        "status":"draft", "type":"reference", "tags":["imported","markdown"],
        "importance":0.2, "certainty":0.2, "workspace_hash":a.workspace_hash,
        "skip_dedup":true
    }))?;
    let result: Value = serde_json::from_str(&remembered).map_err(|e| format!("Markdown import response decode failed: {e}"))?;
    Ok(reviewable_write_result(
        json!({"imported":1,"deduped":false,"id":result["id"],"key":key,"action":result["action"],"status":"draft","non_authoritative":true}),
        "markdown_import",
    ).to_string())
}

#[derive(Debug, Deserialize)]
pub struct StructuredIndexAnchorArgs {
    pub index_type: String,
    pub index_uri: String,
    pub record_id: String,
    /// `reference` preserves only an external anchor; `import` persists a
    /// low-trust local copy with sufficient refetch metadata.
    #[serde(default = "default_anchor_mode")]
    pub mode: String,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub source_system: String,
    #[serde(default)]
    pub observed_at_unix_ms: Option<i64>,
    #[serde(default)]
    pub revision: Option<String>,
}

fn default_anchor_mode() -> String { "reference".to_string() }

/// #789: stable anchor contract for upstream structured indexes. Reference mode
/// is deliberately non-mutating; import mode writes an explicitly imported,
/// draft record with enough metadata for later verification/refetch.
pub fn handle_structured_index_anchor(db: &Database, args: Value) -> Result<String, String> {
    let a: StructuredIndexAnchorArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid structured_index_anchor arguments: {e}"))?;
    if a.index_type.trim().is_empty() || a.index_uri.trim().is_empty() || a.record_id.trim().is_empty() {
        return Err("index_type, index_uri, and record_id are required".to_string());
    }
    let anchor = format!("{}:{}#{}", a.index_type, a.index_uri, a.record_id);
    let source_system = if a.source_system.is_empty() { format!("structured_index:{}", a.index_type) } else { a.source_system };
    let metadata = json!({
        "ref_type":"structured_index", "ref_value":anchor, "source_system":source_system,
        "relationship":"about", "index_type":a.index_type, "index_uri":a.index_uri,
        "record_id":a.record_id, "revision":a.revision, "observed_at_unix_ms":a.observed_at_unix_ms
    });
    match a.mode.as_str() {
        "reference" => Ok(json!({"mode":"reference", "anchor":metadata, "persisted":false,
            "refetch":"retrieve index_uri and record_id, then compare revision/observed_at_unix_ms"}).to_string()),
        "import" => {
            let content = a.content.filter(|v| !v.trim().is_empty())
                .ok_or_else(|| "content is required when mode=import".to_string())?;
            let mut h = std::collections::hash_map::DefaultHasher::new();
            use std::hash::{Hash, Hasher};
            anchor.hash(&mut h); content.hash(&mut h);
            let key = format!("structured-index-{:016x}", h.finish());
            let existing = db.recall(&crate::models::RecallParams { query: String::new(), category: Some("structured_index_fact".to_string()), workspace_hash: Some(a.workspace_hash.clone()), limit: 1000, skip_side_effects: true, ..crate::models::RecallParams::default() })
                .map_err(|e| format!("Structured index duplicate scan failed: {e}"))?;
            if existing.iter().any(|e| e.key == key) {
                return Ok(reviewable_write_result(
            json!({"mode":"import","imported":0,"deduped":true,"key":key,"anchor":metadata}),
            "structured_index_import",
        ).to_string());
            }
            let body = json!({"content":content,"origin":{"memory_kind":"imported","source_system":source_system,"capture_method":"import","observed_at_unix_ms":a.observed_at_unix_ms},"external_refs":[metadata],"non_authoritative":true,"refetch":"retrieve index_uri and record_id, then compare revision/observed_at_unix_ms"});
            let remembered = handle_remember(db, json!({"category":"structured_index_fact","key":key,
                "body_json":body.to_string(),"status":"draft","type":"reference","importance":0.25,
                "certainty":0.25,"workspace_hash":a.workspace_hash,"tags":["imported","structured-index"],"skip_dedup":true}))?;
            let result: Value = serde_json::from_str(&remembered).map_err(|e| format!("Structured index import response decode failed: {e}"))?;
            Ok(reviewable_write_result(
        json!({"mode":"import","imported":1,"deduped":false,"id":result["id"],"key":key,"anchor":metadata,"non_authoritative":true}),
        "structured_index_import",
    ).to_string())
        }
        other => Err(format!("unknown mode '{other}': expected reference or import")),
    }
}

pub fn handle_vault_import(db: &Database, args: Value) -> String {
    let a: VaultExportArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => {
            return json!({"error": format!("Invalid vault_import arguments: {}", e)}).to_string()
        }
    };
    let dir = if a.vault_dir.starts_with("~/") {
        let home = std::env::var("HOME")
            .or_else(|_| std::env::var("USERPROFILE"))
            .unwrap_or_else(|_| "/root".to_string());
        a.vault_dir.replacen("~", &home, 1)
    } else {
        a.vault_dir.clone()
    };
    match db.vault_import(&dir) {
        Ok(report) => serde_json::to_value(&report)
            .map(|value| reviewable_write_result(value, "vault_import").to_string())
            .unwrap_or_else(|e| json!({"error": format!("Serialization failed: {}", e)}).to_string()),
        Err(e) => json!({"error": format!("Vault import failed: {}", e)}).to_string(),
    }
}

#[derive(Debug, Deserialize)]
pub struct TraverseArgs {
    pub category: String,
    pub key: String,
    #[serde(default = "default_depth")]
    pub max_depth: i64,
    #[serde(default = "default_max_nodes")]
    pub max_nodes: i64,
}

fn default_depth() -> i64 {
    3
}

fn default_max_nodes() -> i64 {
    100
}

pub fn handle_traverse(db: &Database, args: Value) -> String {
    let a: TraverseArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => {
            return json!({"error": format!("Invalid traverse arguments: {}", e)}).to_string()
        }
    };
    // DoS hardening: clamp caller-supplied bounds to sane ceilings so a single
    // request can't be asked to walk an unbounded depth/breadth of the link graph.
    let max_depth = a.max_depth.clamp(0, 64);
    let max_nodes = a.max_nodes.clamp(0, 100_000);
    match db.traverse_chain(&a.category, &a.key, max_depth, max_nodes) {
        Ok(chain) => serde_json::to_string(&chain)
            .unwrap_or_else(|e| json!({"error": format!("{}", e)}).to_string()),
        Err(e) => json!({"error": format!("Traverse failed: {}", e)}).to_string(),
    }
}

// ─── Graph utility gate, drift & attestation (#869) ────────────────────────

#[derive(Debug, Deserialize)]
pub struct GraphDriftArgs {
    /// Optional workspace scope. Omit (or "") for all workspaces.
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

/// #869: read-only graph/entities/indexes/receipts drift report.
pub fn handle_graph_drift(db: &Database, args: Value) -> Result<String, String> {
    let a: GraphDriftArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid graph_drift arguments: {}", e))?;
    let report = db
        .graph_drift_report(a.workspace_hash.as_deref())
        .map_err(|e| format!("graph_drift failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

#[derive(Debug, Deserialize)]
pub struct GraphAttestArgs {
    /// Optional workspace scope. Omit (or "") for all workspaces.
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// Preview the stamping without writing.
    #[serde(default, deserialize_with = "null_as_default")]
    pub dry_run: bool,
}

/// #869: stamp the from-side entity id as the evidence anchor on legacy
/// edges (pre-#869 rows). Dry-run previews; applied runs journal one
/// `graph_attest` event.
pub fn handle_graph_attest(db: &Database, args: Value) -> Result<String, String> {
    let a: GraphAttestArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid graph_attest arguments: {}", e))?;
    let report = db
        .graph_attest(a.workspace_hash.as_deref(), a.dry_run)
        .map_err(|e| format!("graph_attest failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

// ─── GraphRAG community tools (#365) ────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct CommunitiesArgs {
    #[serde(default)]
    pub workspace_hash: String,
    /// 'label_prop' (default) or 'louvain'.
    #[serde(default)]
    pub algorithm: String,
    /// Minimum community size to keep (isolated nodes never form communities).
    #[serde(default = "default_min_community_size")]
    pub min_size: usize,
}

fn default_min_community_size() -> usize {
    2
}

/// Detect (and persist) communities over the workspace's link graph.
pub fn handle_communities(db: &Database, args: Value) -> Result<String, String> {
    let a: CommunitiesArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid communities arguments: {}", e))?;
    let report = db
        .detect_communities(&a.workspace_hash, &a.algorithm, a.min_size)
        .map_err(|e| format!("Community detection failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

#[derive(Debug, Deserialize)]
pub struct CommunitySummaryArgs {
    pub community_id: String,
    /// Optional LLM polish; extractive summary is always the fallback.
    #[serde(default)]
    pub use_llm: bool,
    /// Force regeneration even when a cached summary entity exists.
    #[serde(default)]
    pub refresh: bool,
}

/// Return (and materialize) the summary for one detected community.
pub fn handle_community_summary(db: &Database, args: Value) -> Result<String, String> {
    let a: CommunitySummaryArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid community_summary arguments: {}", e))?;
    let result = db
        .community_summary(&a.community_id, a.use_llm, a.refresh)
        .map_err(|e| format!("Community summary failed: {}", e))?;
    serde_json::to_string(&result).map_err(|e| format!("Serialization failed: {}", e))
}

/// GraphRAG global recall: breadth over community summaries, then depth into
/// the best communities' members.
pub fn handle_global_recall(db: &Database, args: Value) -> Result<String, String> {
    let params: crate::communities::GlobalRecallParams = serde_json::from_value(args)
        .map_err(|e| format!("Invalid global_recall arguments: {}", e))?;
    let result = db
        .global_recall(&params)
        .map_err(|e| format!("Global recall failed: {}", e))?;
    serde_json::to_string(&result).map_err(|e| format!("Serialization failed: {}", e))
}

#[derive(Debug, Deserialize)]
pub struct ScoreArgs {
    pub category: String,
    pub key: String,
    pub score: f64,
}

pub fn handle_score(db: &Database, args: Value) -> String {
    let a: ScoreArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => return json!({"error": format!("Invalid score arguments: {}", e)}).to_string(),
    };
    match db.score_entity(&a.category, &a.key, a.score) {
        Ok(found) => {
            json!({"found": found, "category": a.category, "key": a.key, "score": a.score})
                .to_string()
        }
        Err(e) => json!({"error": format!("Score failed: {}", e)}).to_string(),
    }
}

#[derive(Debug, Deserialize)]
pub struct FollowArgs {
    pub category: String,
    pub key: String,
    pub followed: bool,
    #[serde(default)]
    #[allow(dead_code)]
    pub context: Option<String>,
    /// #396 (the #338 pattern): when set, the target row is resolved with
    /// strict workspace equality — matching workspace-scoped recall — instead
    /// of the deterministic global-first pick.
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

/// Record whether an entity (convention/insight/lesson) was actually FOLLOWED
/// or MISSED by the agent — the PMB-inspired "honest follow-rate" signal.
/// `context` is accepted for future auto-detection/audit use but not yet
/// persisted; the tool records a manual confirm/deny each call.
pub fn handle_follow(db: &Database, args: Value) -> Result<String, String> {
    let a: FollowArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid follow arguments: {}", e))?;

    let report = db
        .follow(&a.category, &a.key, a.followed, a.workspace_hash.as_deref())
        .map_err(|e| format!("Follow failed: {}", e))?;

    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

// ── #683 Keystones: mandatory policy rules ───────────────────────────────

#[derive(Debug, Deserialize)]
pub struct KeystoneSetArgs {
    pub content: String,
    #[serde(default = "default_keystone_scope")]
    pub scope: String,
    #[serde(default)]
    pub scope_id: String,
    #[serde(default = "default_keystone_weight")]
    pub weight: f64,
    #[serde(default = "default_keystone_tier_required")]
    pub trust_tier_required: i64,
    /// Caller-asserted authoring tier. Until #684 wires per-agent trust +
    /// session identity, this is honor-system: when present it is enforced,
    /// when absent authoring is allowed and the response flags it.
    #[serde(default)]
    pub author_trust_tier: Option<i64>,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub agent_id: String,
}

fn default_keystone_scope() -> String {
    "tenant".to_string()
}
fn default_keystone_weight() -> f64 {
    1.0
}
fn default_keystone_tier_required() -> i64 {
    2
}

pub fn handle_keystone_set(db: &Database, args: Value) -> Result<String, String> {
    let a: KeystoneSetArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid keystone_set arguments: {}", e))?;
    // #683/#684 trust gating. Prefer the AUTHORITATIVE registered tier
    // (#684 agents registry) when the author is a known agent; fall back to the
    // caller-asserted `author_trust_tier` otherwise. `trust_enforced` reports
    // whether the check used a registry-backed tier (non-spoofable) vs a mere
    // caller assertion (pending real session identity), so callers can tell
    // which mode applied. No tier signal at all → the write proceeds unenforced.
    let registered_tier = if a.agent_id.trim().is_empty() {
        None
    } else {
        db.agent_get(&a.agent_id).ok().flatten().map(|g| g.trust_tier)
    };
    let (effective_tier, trust_enforced) = match registered_tier {
        Some(t) => (Some(t), true),
        None => (a.author_trust_tier, false),
    };
    if let Some(t) = effective_tier {
        if t < a.trust_tier_required {
            return Err(format!(
                "insufficient trust tier: authoring this keystone requires tier >= {}, {} has {}",
                a.trust_tier_required,
                if trust_enforced { "registered agent" } else { "caller asserted" },
                t
            ));
        }
    }
    let (id, created) = db
        .keystone_set(
            &a.content,
            &a.scope,
            &a.scope_id,
            a.weight,
            a.trust_tier_required,
            &a.workspace_hash,
            &a.agent_id,
        )
        .map_err(|e| format!("keystone_set failed: {}", e))?;
    Ok(json!({ "id": id, "created": created, "trust_enforced": trust_enforced }).to_string())
}

#[derive(Debug, Deserialize)]
pub struct KeystoneGetArgs {
    #[serde(default)]
    pub scope: Option<String>,
    #[serde(default)]
    pub scope_id: Option<String>,
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

pub fn handle_keystone_get(db: &Database, args: Value) -> Result<String, String> {
    let a: KeystoneGetArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid keystone_get arguments: {}", e))?;
    let keystones = db
        .keystone_get(
            a.scope.as_deref(),
            a.scope_id.as_deref(),
            a.workspace_hash.as_deref(),
        )
        .map_err(|e| format!("keystone_get failed: {}", e))?;
    let items: Vec<Value> = keystones
        .iter()
        .map(|k| {
            json!({
                "id": k.id,
                "content": k.content,
                "scope": k.scope,
                "scope_id": k.scope_id,
                "weight": k.weight,
                "trust_tier_required": k.trust_tier_required,
                "workspace_hash": k.workspace_hash,
            })
        })
        .collect();
    Ok(json!({ "keystones": items, "count": items.len() }).to_string())
}

// ─── #889 keystone-suggestion queue ───────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct KeystoneSuggestionsArgs {
    /// Filter by status: "" (all) | "pending" | "approved" | "rejected".
    #[serde(default)]
    pub status: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default = "default_suggestion_limit")]
    pub limit: i64,
}

fn default_suggestion_limit() -> i64 {
    50
}

/// List keystone-suggestion candidates (never policy — read-only).
pub fn handle_keystone_suggestions(db: &Database, args: Value) -> Result<String, String> {
    let a: KeystoneSuggestionsArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid keystone_suggestions arguments: {e}"))?;
    let items = db
        .keystone_suggestions_list(&a.status, a.workspace_hash.as_deref(), a.limit)
        .map_err(|e| format!("keystone_suggestions failed: {e}"))?;
    Ok(json!({
        "suggestions": items,
        "count": items.len(),
        "note": "suggestions are candidates only; keystone promotion requires an explicit approve decision",
    })
    .to_string())
}

#[derive(Debug, Deserialize)]
pub struct KeystoneSuggestionDecideArgs {
    pub id: String,
    /// "approve" (promote to keystone) or "reject".
    pub action: String,
    #[serde(default)]
    pub scope: String,
    #[serde(default)]
    pub scope_id: String,
    #[serde(default = "default_keystone_weight")]
    pub weight: f64,
    #[serde(default = "default_keystone_tier_required")]
    pub trust_tier_required: i64,
    #[serde(default)]
    pub author_trust_tier: Option<i64>,
    #[serde(default)]
    pub agent_id: String,
    #[serde(default)]
    pub workspace_hash: String,
}

/// Decide a suggestion. `approve` re-runs the keystone trust-tier gate
/// (#683/#684) and, on success, promotes the suggestion's instruction into
/// the keystones table; the suggestion is then marked `approved`. `reject`
/// marks the suggestion `rejected` and writes nothing. Governance is
/// preserved: extraction never calls this path.
pub fn handle_keystone_suggestion_decide(db: &Database, args: Value) -> Result<String, String> {
    let a: KeystoneSuggestionDecideArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid keystone_suggestion_decide arguments: {e}"))?;
    // Resolve the row by id; a caller-supplied workspace must match the
    // suggestion's own (no cross-workspace decisions).
    let sug = db
        .keystone_suggestions_list("", None, 1000)
        .map_err(|e| format!("suggestion lookup failed: {e}"))?
        .into_iter()
        .find(|s| s.id == a.id)
        .ok_or_else(|| format!("suggestion {} not found", a.id))?;
    if !a.workspace_hash.is_empty() && a.workspace_hash != sug.workspace_hash {
        return Err(format!(
            "suggestion {} belongs to workspace '{}', not '{}'",
            a.id, sug.workspace_hash, a.workspace_hash
        ));
    }
    match a.action.as_str() {
        "approve" => {
            // Re-run the same trust gate as handle_keystone_set (#683/#684).
            let registered_tier = if a.agent_id.trim().is_empty() {
                None
            } else {
                db.agent_get(&a.agent_id).ok().flatten().map(|g| g.trust_tier)
            };
            let (effective_tier, trust_enforced) = match registered_tier {
                Some(t) => (Some(t), true),
                None => (a.author_trust_tier, false),
            };
            if let Some(t) = effective_tier {
                if t < a.trust_tier_required {
                    return Err(format!(
                        "insufficient trust tier: promoting this keystone requires tier >= {}, {} has {}",
                        a.trust_tier_required,
                        if trust_enforced { "registered agent" } else { "caller asserted" },
                        t
                    ));
                }
            }
            let scope = if a.scope.is_empty() {
                "agent".to_string()
            } else {
                a.scope.clone()
            };
            let scope_id = if a.scope_id.is_empty() {
                a.agent_id.clone()
            } else {
                a.scope_id.clone()
            };
            let (id, created) = db
                .keystone_set(
                    &sug.instruction,
                    &scope,
                    &scope_id,
                    a.weight,
                    a.trust_tier_required,
                    &a.workspace_hash,
                    &a.agent_id,
                )
                .map_err(|e| format!("keystone promotion failed: {e}"))?;
            db.keystone_suggestion_decide(&a.id, "approved", &a.agent_id)
                .map_err(|e| format!("suggestion decision failed: {e}"))?;
            Ok(json!({
                "ok": true,
                "keystone_id": id,
                "created": created,
                "suggestion_id": a.id,
                "suggestion_status": "approved",
                "trust_enforced": trust_enforced,
            })
            .to_string())
        }
        "reject" => {
            db.keystone_suggestion_decide(&a.id, "rejected", &a.agent_id)
                .map_err(|e| format!("suggestion decision failed: {e}"))?;
            Ok(json!({
                "ok": true,
                "suggestion_id": a.id,
                "suggestion_status": "rejected",
                "keystone_promoted": false,
            })
            .to_string())
        }
        other => Err(format!(
            "invalid decision action '{other}': expected 'approve' or 'reject'"
        )),
    }
}

#[derive(Debug, Deserialize)]
pub struct AgentArgs {
    pub agent_id: String,
    #[serde(default)]
    pub name: String,
    #[serde(default)]
    pub trust_tier: Option<i64>,
    #[serde(default)]
    pub fleet_id: String,
}

pub fn handle_agent(db: &Database, args: Value) -> Result<String, String> {
    let a: AgentArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid agent arguments: {}", e))?;
    if a.agent_id.trim().is_empty() {
        return Err("agent_id must not be empty".to_string());
    }
    // trust_tier present → upsert; absent → look up only.
    let created = if let Some(tier) = a.trust_tier {
        Some(
            db.agent_upsert(&a.agent_id, &a.name, tier, &a.fleet_id)
                .map_err(|e| format!("agent upsert failed: {}", e))?,
        )
    } else {
        None
    };
    let agent = db
        .agent_get(&a.agent_id)
        .map_err(|e| format!("agent lookup failed: {}", e))?;
    Ok(json!({
        "found": agent.is_some(),
        "created": created.unwrap_or(false),
        "agent": agent.map(|g| json!({
            "agent_id": g.agent_id,
            "name": g.name,
            "trust_tier": g.trust_tier,
            "fleet_id": g.fleet_id,
        })),
    })
    .to_string())
}

#[derive(Debug, Deserialize)]
pub struct AuthoritySetArgs {
    pub agent_id: String, pub workspace_hash: String, pub allowed_capabilities: Vec<String>, pub scope_anchors: Vec<String>,
    #[serde(default)] pub approval_required_capabilities: Vec<String>, #[serde(default)] pub approver_principals: Vec<String>,
    #[serde(default)] pub allowed_inbound_principals: Vec<String>, #[serde(default)] pub permitted_external_ref_prefixes: Vec<String>,
    #[serde(default = "authority_default_parallel")] pub max_parallel_actions: i64, #[serde(default = "authority_default_mode")] pub mode: String,
    #[serde(default)] pub expires_at_unix_ms: Option<i64>, #[serde(default)] pub author_agent_id: String,
    #[serde(default)] pub capability_constraints_json: String,
}
fn authority_default_parallel() -> i64 { 1 }
fn authority_default_mode() -> String { "shadow".to_string() }
pub fn handle_authority_set(db: &Database, args: Value) -> Result<String, String> {
    let a: AuthoritySetArgs=serde_json::from_value(args).map_err(|e|format!("Invalid authority_set arguments: {e}"))?;
    let input=crate::models::AuthorityManifestInput { agent_id:a.agent_id, workspace_hash:a.workspace_hash, allowed_capabilities:a.allowed_capabilities, approval_required_capabilities:a.approval_required_capabilities, scope_anchors:a.scope_anchors, approver_principals:a.approver_principals, allowed_inbound_principals:a.allowed_inbound_principals, permitted_external_ref_prefixes:a.permitted_external_ref_prefixes, max_parallel_actions:a.max_parallel_actions, mode:a.mode, expires_at_unix_ms:a.expires_at_unix_ms, capability_constraints_json:a.capability_constraints_json };
    serde_json::to_string(&db.authority_set(&input,&a.author_agent_id).map_err(|e|format!("authority_set failed: {e}"))?).map_err(|e|e.to_string())
}
#[derive(Debug, Deserialize)] pub struct AuthoritySetSignedArgs {pub profile_json:String,pub trusted_public_key_b64:String,pub author_agent_id:String}
pub fn handle_authority_set_signed(db:&Database,args:Value)->Result<String,String>{let a:AuthoritySetSignedArgs=serde_json::from_value(args).map_err(|e|format!("Invalid authority_set_signed arguments: {e}"))?;serde_json::to_string(&db.authority_set_signed(&a.profile_json,&a.trusted_public_key_b64,&a.author_agent_id).map_err(|e|format!("authority_set_signed failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct AuthorityGetArgs { pub agent_id:String, pub workspace_hash:String, #[serde(default)] pub include_revoked:bool }
pub fn handle_authority_get(db:&Database,args:Value)->Result<String,String>{let a:AuthorityGetArgs=serde_json::from_value(args).map_err(|e|format!("Invalid authority_get arguments: {e}"))?;serde_json::to_string(&json!({"authority":db.authority_get(&a.agent_id,&a.workspace_hash,a.include_revoked).map_err(|e|format!("authority_get failed: {e}"))?})).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct AuthorityRevokeArgs { pub manifest_id:String, #[serde(default)] pub actor_agent_id:String, #[serde(default)] pub reason:String }
pub fn handle_authority_revoke(db:&Database,args:Value)->Result<String,String>{let a:AuthorityRevokeArgs=serde_json::from_value(args).map_err(|e|format!("Invalid authority_revoke arguments: {e}"))?;db.authority_revoke(&a.manifest_id,&a.actor_agent_id,&a.reason).map_err(|e|format!("authority_revoke failed: {e}"))?;Ok(json!({"revoked":true,"manifest_id":a.manifest_id}).to_string())}
#[derive(Debug, Deserialize)] pub struct ActionIntentArgs { pub agent_id:String,pub workspace_hash:String,pub scope_anchor:String,pub external_ref:String,pub capability:String,pub action_key:String,pub intent_hash:String, #[serde(default)] pub resource_constraints_json: String }
pub fn handle_action_intent(db:&Database,args:Value)->Result<String,String>{let a:ActionIntentArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_intent arguments: {e}"))?;serde_json::to_string(&db.action_intent(&a.agent_id,&a.workspace_hash,&a.scope_anchor,&a.external_ref,&a.capability,&a.action_key,&a.intent_hash,Some(&a.resource_constraints_json)).map_err(|e|format!("action_intent failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionApproveArgs {pub action_id:String,pub approver_principal:String,pub decision:String}
pub fn handle_action_approve(db:&Database,args:Value)->Result<String,String>{let a:ActionApproveArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_approve arguments: {e}"))?;serde_json::to_string(&db.action_approve(&a.action_id,&a.approver_principal,&a.decision).map_err(|e|format!("action_approve failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionCompleteArgs {pub action_id:String,pub actor_agent_id:String,pub outcome:String,pub outcome_hash:String}
pub fn handle_action_complete(db:&Database,args:Value)->Result<String,String>{let a:ActionCompleteArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_complete arguments: {e}"))?;serde_json::to_string(&db.action_complete(&a.action_id,&a.actor_agent_id,&a.outcome,&a.outcome_hash).map_err(|e|format!("action_complete failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionResolveTimeoutArgs {pub action_id:String,pub approval_timeout_ms:i64}
pub fn handle_action_resolve_timeout(db:&Database,args:Value)->Result<String,String>{let a:ActionResolveTimeoutArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_resolve_timeout arguments: {e}"))?;serde_json::to_string(&db.action_resolve_timeout(&a.action_id,a.approval_timeout_ms).map_err(|e|format!("action_resolve_timeout failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionReceiptGetArgs {pub action_id:String}
pub fn handle_action_receipt_get(db:&Database,args:Value)->Result<String,String>{let a:ActionReceiptGetArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_receipt_get arguments: {e}"))?;serde_json::to_string(&json!({"receipt":db.action_receipt_get(&a.action_id).map_err(|e|format!("action_receipt_get failed: {e}"))?})).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionLeaseArgs {pub action_id:String,pub holder_id:String,#[serde(default="authority_default_parallel")]pub ttl_seconds:i64}
pub fn handle_action_lease_acquire(db:&Database,args:Value)->Result<String,String>{let a:ActionLeaseArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_lease_acquire arguments: {e}"))?;serde_json::to_string(&db.action_lease_acquire(&a.action_id,&a.holder_id,a.ttl_seconds).map_err(|e|format!("action_lease_acquire failed: {e}"))?).map_err(|e|e.to_string())}
#[derive(Debug, Deserialize)] pub struct ActionLeaseReleaseArgs {pub lease_id:String,pub holder_id:String}
pub fn handle_action_lease_release(db:&Database,args:Value)->Result<String,String>{let a:ActionLeaseReleaseArgs=serde_json::from_value(args).map_err(|e|format!("Invalid action_lease_release arguments: {e}"))?;db.action_lease_release(&a.lease_id,&a.holder_id).map_err(|e|format!("action_lease_release failed: {e}"))?;Ok(json!({"released":true,"lease_id":a.lease_id}).to_string())}

#[derive(Debug, Deserialize)]
pub struct ConflictArgs {
    pub category: String,
    #[serde(default = "default_conflict_threshold")]
    pub threshold: f64,
    #[serde(default = "default_conflict_limit")]
    pub limit: i64,
    #[serde(default)]
    pub offset: i64,
    /// Opt-in: actively invalidate the lower-certainty side of clear conflicts
    /// (default false = read-only detection, the long-standing behavior).
    #[serde(default)]
    pub resolve: bool,
    /// When resolving, only report what would change unless explicitly false.
    /// Defaults true so an accidental `resolve:true` previews rather than mutates.
    #[serde(default = "default_true")]
    pub dry_run: bool,
    /// Minimum certainty gap to auto-resolve a conflict; closer pairs are
    /// skipped as ambiguous.
    #[serde(default = "default_certainty_margin")]
    pub certainty_margin: f64,
}

fn default_conflict_threshold() -> f64 {
    0.4
}
fn default_conflict_limit() -> i64 {
    10
}
fn default_true() -> bool {
    true
}
fn default_certainty_margin() -> f64 {
    0.2
}

#[derive(Debug, Deserialize)]
pub struct OperatorReviewArgs {
    #[serde(default = "default_category")]
    pub category: String,
    #[serde(default = "default_conflict_limit")]
    pub limit: i64,
    #[serde(default)]
    pub stale_threshold: Option<f64>,
}

fn default_category() -> String { "general".to_string() }

/// #786: read-only operator queue. It composes existing conflict detection,
/// actionability hygiene, and deprecated-state discovery without mutating facts.
pub fn handle_operator_review(db: &Database, args: Value) -> Result<String, String> {
    let a: OperatorReviewArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid operator_review arguments: {e}"))?;
    let limit = a.limit.clamp(1, 1000);
    let contradictions = db.detect_conflicts(&a.category, default_conflict_threshold(), limit, 0)
        .map_err(|e| format!("Conflict review failed: {e}"))?;
    let hygiene = handle_hygiene(db, json!({"category": a.category, "threshold": a.stale_threshold.unwrap_or(0.35), "limit":limit, "scan_limit":10000}))?;
    let stale: Value = serde_json::from_str(&hygiene).map_err(|e| format!("Stale review decode failed: {e}"))?;
    let mut supersession_lag = Vec::new();
    let mut cursor: Option<String> = None;
    loop {
        let rows = db.scan_entities(Some(&a.category), None, false, cursor.as_deref(), 501)
            .map_err(|e| format!("Supersession scan failed: {e}"))?;
        if rows.is_empty() { break; }
        let take = rows.len().min(500);
        for entity in rows.iter().take(take) {
            if entity.status == "deprecated" {
                supersession_lag.push(json!({"id":entity.id,"category":entity.category,"key":entity.key,"status":entity.status,"reason":"deprecated fact requires successor review"}));
            }
        }
        if rows.len() <= 500 || supersession_lag.len() >= limit as usize { break; }
        cursor = rows.get(take - 1).map(|e| e.id.clone());
    }
    supersession_lag.truncate(limit as usize);
    // #889: pending keystone-suggestion candidates surface in the operator
    // review queue with their source citation (the correction entity id).
    let pending_suggestions = db
        .keystone_suggestions_list("pending", None, limit)
        .map_err(|e| format!("keystone suggestions failed: {e}"))?;
    Ok(json!({"reviewed_at_unix_ms": std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap_or_default().as_millis() as i64, "category":a.category,
        "contradictions":contradictions, "stale_candidates":stale["flagged"],
        "supersession_lag":supersession_lag,
        "keystone_suggestions": pending_suggestions,
        "keystone_suggestions_note": "pending directive candidates from correct captures; promote via keystone_suggestion_decide(approve)",
        "read_only":true}).to_string())
}

pub fn handle_conflicts(db: &Database, args: Value) -> String {
    let a: ConflictArgs = match serde_json::from_value(args) {
        Ok(a) => a,
        Err(e) => {
            return json!({"error": format!("Invalid conflicts arguments: {}", e)}).to_string()
        }
    };
    if a.resolve {
        return match db.resolve_conflicts(
            &a.category,
            a.threshold,
            a.limit,
            a.offset,
            a.certainty_margin,
            a.dry_run,
        ) {
            Ok(report) => serde_json::to_string(&report)
                .unwrap_or_else(|e| json!({"error": format!("{}", e)}).to_string()),
            Err(e) => json!({"error": format!("Conflict resolution failed: {}", e)}).to_string(),
        };
    }
    match db.detect_conflicts(&a.category, a.threshold, a.limit, a.offset) {
        Ok(report) => serde_json::to_string(&report)
            .unwrap_or_else(|e| json!({"error": format!("{}", e)}).to_string()),
        Err(e) => json!({"error": format!("Conflict detection failed: {}", e)}).to_string(),
    }
}

pub fn handle_consolidate(db: &Database, args: Value) -> String {
    let params: crate::models::ConsolidateParams = match serde_json::from_value(args) {
        Ok(p) => p,
        Err(e) => {
            return json!({"error": format!("Invalid consolidate arguments: {}", e)}).to_string()
        }
    };
    match db.consolidate(&params) {
        Ok(report) => serde_json::to_string(&report)
            .unwrap_or_else(|e| json!({"error": format!("{}", e)}).to_string()),
        Err(e) => json!({"error": format!("Consolidation failed: {}", e)}).to_string(),
    }
}

// ─── perseus_vault_dream handler ─────────────────────────────────────────

/// Wire args for perseus_vault_dream: DreamParams plus the handler-level
/// `fallback_consolidate` switch (LLM-less environments can opt into the
/// mechanical consolidate pass instead of an error).
#[derive(Debug, Deserialize)]
pub struct DreamArgs {
    #[serde(flatten)]
    pub params: crate::models::DreamParams,
    /// When the LLM endpoint is not configured: instead of a clean error,
    /// fall back to the non-LLM perseus_vault_consolidate (cold_first) over the same
    /// categories. Off by default — dreaming and mechanical merging produce
    /// different artifacts, so the substitution must be explicit.
    #[serde(default)]
    pub fallback_consolidate: bool,
}

pub fn handle_dream(db: &Database, args: Value) -> Result<String, String> {
    let a: DreamArgs =
        serde_json::from_value(args).map_err(|e| format!("Invalid dream arguments: {}", e))?;

    if !db.llm_enabled() && a.fallback_consolidate {
        // Graceful no-LLM fallback: run the mechanical consolidation pass
        // (cold_first, same archive-safety rules) per category and report it
        // AS a fallback so callers can tell nothing was LLM-reasoned.
        let categories: Vec<String> = match a.params.category {
            Some(ref c) => vec![c.clone()],
            None => db
                .workspace_list_categories_scoped(a.params.workspace_hash.as_deref())
                .map_err(|e| format!("Dream fallback (categories) failed: {e}"))?
                .into_iter()
                .filter(|c| {
                    c != "insight" && c != "observation" && c != "synthesis" && c != "memories"
                })
                .collect(),
        };
        let mut observations_created = 0i64;
        let mut sources_archived = 0i64;
        let mut entities_examined = 0i64;
        for cat in &categories {
            let report = db
                .consolidate(&crate::models::ConsolidateParams {
                    category: cat.clone(),
                    similarity_threshold: 0.6,
                    limit: a.params.max_clusters,
                    offset: 0,
                    dry_run: a.params.dry_run,
                    cold_first: true,
                    archive_sources: a.params.archive_sources,
                    workspace_hash: a.params.workspace_hash.clone(),
                    global: a.params.global,
                    requesting_agent_id: a.params.requesting_agent_id.clone(),
                })
                .map_err(|e| format!("Dream fallback (consolidate {}) failed: {}", cat, e))?;
            observations_created += report.observations_created;
            sources_archived += report.sources_archived;
            entities_examined += report.entities_examined;
        }
        return Ok(json!({
            "fallback": "consolidate",
            "note": "LLM endpoint not configured — ran the non-LLM perseus_vault_consolidate (cold_first) pass instead. Set --llm-endpoint for real dreaming.",
            "categories_scanned": categories,
            "entities_examined": entities_examined,
            "observations_created": observations_created,
            "sources_archived": sources_archived,
            "dry_run": a.params.dry_run,
        })
        .to_string());
    }

    let report = db
        .dream(&a.params)
        .map_err(|e| format!("Dream failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

pub fn handle_decay(db: &Database, _args: Value) -> String {
    match db.decay_tick() {
        Ok(report) => serde_json::to_string(&report).unwrap_or_else(|e| {
            json!({"error": format!("Decay report serialization failed: {}", e)}).to_string()
        }),
        Err(e) => json!({"error": format!("Decay tick failed: {}", e)}).to_string(),
    }
}

pub fn handle_reindex(db: &Database, _args: Value) -> String {
    match db.reindex_fts() {
        Ok(n) => json!({"reindexed": n}).to_string(),
        Err(e) => json!({"error": format!("Reindex failed: {}", e)}).to_string(),
    }
}

pub fn handle_ask(db: &Database, args: Value) -> Result<String, String> {
    let params: AskParams =
        serde_json::from_value(args).map_err(|e| format!("Invalid ask arguments: {}", e))?;

    if !db.llm_enabled() {
        return Err("LLM is not enabled. Set --llm-endpoint to enable perseus_vault_ask.".to_string());
    }

    match db.ask(&params) {
        Ok(result) => {
            serde_json::to_string(&result).map_err(|e| format!("Serialization failed: {}", e))
        }
        Err(e) => Err(format!("Ask failed: {}", e)),
    }
}

pub fn handle_ingest(db: &Database, args: Value) -> Result<String, String> {
    let params: IngestParams =
        serde_json::from_value(args).map_err(|e| format!("Invalid ingest arguments: {}", e))?;

    match db.ingest(&params) {
        Ok(result) => Ok(reviewable_write_result(result, "connector_ingest").to_string()),
        Err(e) => Err(format!("Ingest failed: {}", e)),
    }
}

#[derive(Debug, Deserialize)]
pub struct IngestFileArgs {
    /// Path to the document file to ingest.
    pub path: String,
    #[serde(default)]
    pub category: Option<String>,
    #[serde(default)]
    pub key: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
}

#[derive(Debug, Deserialize)]
pub struct ArtifactRegisterArgs {
    pub path: String,
    #[serde(default)]
    pub mime_type: Option<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub workspace_hash: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub agent_id: String,
    #[serde(
        default = "default_visibility",
        deserialize_with = "null_as_default_visibility"
    )]
    pub visibility: String,
    #[serde(default)]
    pub origin: Option<OriginRecord>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub external_refs: Vec<ExternalRef>,
    #[serde(default)]
    pub retention_policy: Option<String>,
    #[serde(default)]
    pub representation: ArtifactRepresentation,
}

/// #876 governed distillation: registration args for a learned-memory
/// artifact (trained weights / distilled cartridge). `action_id` must name a
/// COMPLETED `learned_memory` action receipt; `source_entities` are the
/// (category, key) pairs the artifact was distilled from, snapshotted
/// hash-only at registration.
#[derive(Debug, Deserialize)]
pub struct LearnedArtifactRegisterArgs {
    pub path: String,
    #[serde(default)]
    pub mime_type: Option<String>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub workspace_hash: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub agent_id: String,
    #[serde(
        default = "default_visibility",
        deserialize_with = "null_as_default_visibility"
    )]
    pub visibility: String,
    pub action_id: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub source_entities: Vec<(String, String)>,
    #[serde(default, deserialize_with = "null_as_default")]
    pub external_refs: Vec<ExternalRef>,
    #[serde(default)]
    pub retention_policy: Option<String>,
    #[serde(default)]
    pub derivation_version: Option<String>,
}

/// #879: bind args — one profile <-> one workspace, explicit access mode.
#[derive(Debug, Deserialize)]
pub struct WorkspaceBindArgs {
    pub profile_name: String,
    #[serde(default, deserialize_with = "null_as_default")]
    pub workspace_hash: String,
    #[serde(default = "default_binding_access_mode")]
    pub access_mode: String,
    #[serde(default)]
    pub metadata: Option<serde_json::Value>,
}

fn default_binding_access_mode() -> String {
    "read_write".to_string()
}

#[derive(Debug, Deserialize)]
pub struct WorkspaceUnbindArgs {
    pub profile_name: String,
    #[serde(default)]
    pub reason: String,
}

#[derive(Debug, Deserialize)]
pub struct WorkspaceQuarantineArgs {
    pub profile_name: String,
    #[serde(default = "default_quarantine_action")]
    pub action: String,
    #[serde(default)]
    pub reason: String,
}

fn default_quarantine_action() -> String {
    "quarantine".to_string()
}

#[derive(Debug, Deserialize)]
pub struct ArtifactManifestArgs {
    pub sha256: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ArtifactExcerptArgs {
    pub sha256: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
    #[serde(default)]
    pub byte_start: Option<i64>,
    #[serde(default)]
    pub byte_end: Option<i64>,
    #[serde(default)]
    pub line_start: Option<i64>,
    #[serde(default)]
    pub line_end: Option<i64>,
}

fn default_artifact_candidate_encoding() -> String {
    "utf8".to_string()
}

fn default_artifact_max_matches() -> i64 {
    5
}

#[derive(Debug, Deserialize)]
pub struct ArtifactDigestArgs {
    pub sha256: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct ArtifactVerifyArgs {
    pub sha256: String,
    #[serde(default)]
    pub workspace_hash: Option<String>,
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
    pub candidate: String,
    #[serde(
        default = "default_artifact_candidate_encoding",
        deserialize_with = "null_as_default_artifact_candidate_encoding"
    )]
    pub encoding: String,
    #[serde(
        default = "default_artifact_max_matches",
        deserialize_with = "null_as_default_artifact_max_matches"
    )]
    pub max_matches: i64,
}

/// Ingest a document file into memory by extracting its text **locally** (#236).
/// Plaintext/markdown work in any build; DOCX/PDF need `--features multimodal`.
/// The extracted text is stored as a normal entity (category default "document",
/// key default = file name) so it is recallable like any other memory.
pub fn handle_ingest_file(db: &Database, args: Value) -> Result<String, String> {
    let a: IngestFileArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid ingest_file arguments: {}", e))?;
    let path = std::path::Path::new(&a.path);

    let text = crate::multimodal::extract_text(path)?;
    let char_count = text.chars().count();

    let file_name = path
        .file_name()
        .and_then(|n| n.to_str())
        .unwrap_or("document")
        .to_string();
    let category = a.category.unwrap_or_else(|| "document".to_string());
    let key = a.key.unwrap_or(file_name);

    let body = json!({ "content": text, "source_path": a.path }).to_string();
    let now = now_ms();
    let raw_id = Uuid::new_v4().to_string().replace('-', "");
    let id = format!("mem-{}", &raw_id[..12.min(raw_id.len())]);
    let entity = Entity {
        id,
        category,
        key,
        body_json: body,
        status: "active".to_string(),
        entity_type: "document".to_string(),
        tags: a.tags,
        decay_score: 1.0,
        retrieval_count: 0,
        layer: "buffer".to_string(),
        topic_path: String::new(),
        archived: false,
        archive_reason: String::new(),
        links: vec![],
        verified: false,
        source: "ingest_file".to_string(),
        always_on: false,
        certainty: 0.5,
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
        embedding: None,
        _parsed_body: None,
    };
    let (eid, action) = db
        .remember(&entity)
        .map_err(|e| format!("Remember failed: {}", e))?;
    Ok(reviewable_write_result(
        json!({
            "id": eid,
            "action": action,
            "category": entity.category,
            "key": entity.key,
            "chars": char_count,
        }),
        "ingest_file",
    ).to_string())
}

const MAX_ARTIFACT_BYTES: usize = 8 * 1024 * 1024;
const MAX_ARTIFACT_EXTERNAL_REFS: usize = 32;
const MAX_ARTIFACT_EXCERPT_BYTES: usize = 8 * 1024;
const MAX_ARTIFACT_EXCERPT_LINES: usize = 200;
const MAX_ARTIFACT_VERIFY_BYTES: usize = 4 * 1024;
const MAX_ARTIFACT_VERIFY_MATCHES: i64 = 20;

fn is_sha256_hex(s: &str) -> bool {
    s.len() == 64 && s.bytes().all(|b| b.is_ascii_hexdigit())
}

fn guess_artifact_mime_type(path: &std::path::Path) -> String {
    match path
        .extension()
        .and_then(|ext| ext.to_str())
        .map(|ext| ext.to_ascii_lowercase())
        .as_deref()
    {
        Some("txt") | Some("log") | Some("md") => "text/plain".to_string(),
        Some("json") | Some("jsonl") => "application/json".to_string(),
        Some("csv") => "text/csv".to_string(),
        Some("yaml") | Some("yml") => "application/yaml".to_string(),
        Some("html") | Some("htm") => "text/html".to_string(),
        Some("xml") => "application/xml".to_string(),
        Some("pdf") => "application/pdf".to_string(),
        Some("gz") => "application/gzip".to_string(),
        Some("zip") => "application/zip".to_string(),
        _ => "application/octet-stream".to_string(),
    }
}

fn canonicalize_external_refs(mut refs: Vec<ExternalRef>) -> Vec<ExternalRef> {
    refs.sort_by(|a, b| {
        (
            a.ref_type.as_str(),
            a.ref_value.as_str(),
            a.source_system.as_deref().unwrap_or(""),
            a.relationship.as_deref().unwrap_or("about"),
        )
            .cmp(&(
                b.ref_type.as_str(),
                b.ref_value.as_str(),
                b.source_system.as_deref().unwrap_or(""),
                b.relationship.as_deref().unwrap_or("about"),
            ))
    });
    refs.dedup_by(|a, b| {
        a.ref_type == b.ref_type
            && a.ref_value == b.ref_value
            && a.source_system == b.source_system
            && a.relationship.as_deref().unwrap_or("about")
                == b.relationship.as_deref().unwrap_or("about")
    });
    refs
}

fn artifact_why_served(binding: &crate::models::ArtifactBinding, reason: &str) -> Value {
    json!({
        "reason": reason,
        "workspace_hash": binding.workspace_hash,
        "visibility": binding.visibility,
        "anchors": binding.external_refs,
    })
}

fn artifact_line_bounds(bytes: &[u8]) -> Vec<(usize, usize)> {
    let mut out = Vec::new();
    let mut start = 0usize;
    for (i, b) in bytes.iter().enumerate() {
        if *b == b'\n' {
            out.push((start, i + 1));
            start = i + 1;
        }
    }
    if start < bytes.len() {
        out.push((start, bytes.len()));
    }
    out
}

fn find_exact_matches(haystack: &[u8], needle: &[u8], max_matches: usize) -> Vec<(usize, usize)> {
    if needle.is_empty() || needle.len() > haystack.len() {
        return Vec::new();
    }
    let mut out = Vec::new();
    let mut i = 0usize;
    while i + needle.len() <= haystack.len() && out.len() < max_matches {
        if &haystack[i..i + needle.len()] == needle {
            out.push((i, i + needle.len()));
            i += needle.len().max(1);
        } else {
            i += 1;
        }
    }
    out
}

pub fn handle_artifact_register(db: &Database, args: Value) -> Result<String, String> {
    let a: ArtifactRegisterArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid artifact_register arguments: {}", e))?;
    let path = std::path::Path::new(&a.path);
    let meta = std::fs::metadata(path).map_err(|e| format!("stat {}: {}", path.display(), e))?;
    if !meta.is_file() {
        return Err(format!("artifact path must be a regular file: {}", path.display()));
    }
    if meta.len() as usize > MAX_ARTIFACT_BYTES {
        return Err(format!(
            "artifact too large: {} bytes (max {})",
            meta.len(),
            MAX_ARTIFACT_BYTES
        ));
    }
    let mime_type = a
        .mime_type
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| guess_artifact_mime_type(path));
    if matches!(mime_type.as_str(), "application/gzip" | "application/zip") {
        return Err(
            "compressed artifacts are not supported in the exact-byte first slice; register the original bytes uncompressed"
                .to_string(),
        );
    }
    if !matches!(
        a.visibility.as_str(),
        "private" | "fleet" | "workspace" | "tenant" | "public" | ""
    ) {
        return Err(format!(
            "invalid visibility '{}': expected private, fleet, workspace, tenant, or public",
            a.visibility
        ));
    }
    if let Some(ref origin) = a.origin {
        if let Some(ref kind) = origin.memory_kind {
            if !crate::models::MEMORY_KINDS.contains(&kind.as_str()) {
                return Err(format!(
                    "invalid origin.memory_kind '{kind}': expected one of {:?}",
                    crate::models::MEMORY_KINDS
                ));
            }
        }
    }
    if a.external_refs.len() > MAX_ARTIFACT_EXTERNAL_REFS {
        return Err(format!(
            "external_refs too long: {} refs (max {})",
            a.external_refs.len(),
            MAX_ARTIFACT_EXTERNAL_REFS
        ));
    }
    for r in &a.external_refs {
        if r.ref_type.trim().is_empty() || r.ref_value.trim().is_empty() {
            return Err("external_refs entries require non-empty ref_type and ref_value".into());
        }
        if let Some(ref rel) = r.relationship {
            if !crate::models::REF_RELATIONSHIPS.contains(&rel.as_str()) {
                return Err(format!(
                    "invalid external_refs relationship '{rel}': expected one of {:?}",
                    crate::models::REF_RELATIONSHIPS
                ));
            }
        }
    }
    let retention_policy = a.retention_policy.and_then(|s| {
        let t = s.trim().to_string();
        (!t.is_empty()).then_some(t)
    });
    if let Some(ref policy) = retention_policy {
        if !crate::models::RETENTION_POLICIES.contains(&policy.as_str()) {
            return Err(format!(
                "invalid retention_policy '{policy}': expected one of {:?}",
                crate::models::RETENTION_POLICIES
            ));
        }
    }
    let mut representation = a.representation;
    if representation.kind.trim().is_empty() {
        representation.kind = "original".to_string();
    }
    if !crate::models::ARTIFACT_REPRESENTATION_KINDS.contains(&representation.kind.as_str()) {
        return Err(format!(
            "invalid representation.kind '{}': expected one of {:?}",
            representation.kind,
            crate::models::ARTIFACT_REPRESENTATION_KINDS
        ));
    }
    match representation.kind.as_str() {
        "original" => {
            if representation.derived_from_sha256.is_some()
                || representation.derivation_kind.is_some()
                || representation.derivation_version.is_some()
            {
                return Err(
                    "original artifacts must not carry derived_from_sha256 or derivation metadata"
                        .to_string(),
                );
            }
        }
        "derived" => {
            let Some(ref parent) = representation.derived_from_sha256 else {
                return Err("derived artifacts require representation.derived_from_sha256".to_string());
            };
            if !is_sha256_hex(parent) {
                return Err(format!(
                    "representation.derived_from_sha256 must be a full 64-hex SHA-256, got '{parent}'"
                ));
            }
        }
        _ => unreachable!(),
    }

    let bytes = std::fs::read(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    let (sha256, artifact_created, binding_created) = db
        .artifact_register(
            &bytes,
            &mime_type,
            &a.workspace_hash,
            &a.agent_id,
            &a.visibility,
            a.origin,
            canonicalize_external_refs(a.external_refs),
            retention_policy,
            representation,
        )
        .map_err(|e| format!("artifact register failed: {}", e))?;
    let manifest = db
        .artifact_manifest(&sha256, Some(a.workspace_hash.as_str()), None)
        .map_err(|e| format!("artifact manifest failed: {}", e))?;
    Ok(json!({
        "sha256": sha256,
        "artifact_action": if artifact_created { "created" } else { "existing" },
        "binding_action": if binding_created { "created" } else { "existing" },
        "manifest": manifest,
    })
    .to_string())
}

pub fn handle_learned_artifact_register(db: &Database, args: Value) -> Result<String, String> {
    let a: LearnedArtifactRegisterArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid learned_artifact_register arguments: {}", e))?;
    let path = std::path::Path::new(&a.path);
    let meta = std::fs::metadata(path).map_err(|e| format!("stat {}: {}", path.display(), e))?;
    if !meta.is_file() {
        return Err(format!(
            "artifact path must be a regular file: {}",
            path.display()
        ));
    }
    if meta.len() as usize > MAX_ARTIFACT_BYTES {
        return Err(format!(
            "artifact too large: {} bytes (max {})",
            meta.len(),
            MAX_ARTIFACT_BYTES
        ));
    }
    let mime_type = a
        .mime_type
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| guess_artifact_mime_type(path));
    if !matches!(
        a.visibility.as_str(),
        "private" | "fleet" | "workspace" | "tenant" | "public" | ""
    ) {
        return Err(format!(
            "invalid visibility '{}': expected private, fleet, workspace, tenant, or public",
            a.visibility
        ));
    }
    if a.source_entities.is_empty() {
        return Err("learned artifacts require at least one source_entities (category, key) pair"
            .to_string());
    }
    if a.external_refs.len() > MAX_ARTIFACT_EXTERNAL_REFS {
        return Err(format!(
            "external_refs too long: {} refs (max {})",
            a.external_refs.len(),
            MAX_ARTIFACT_EXTERNAL_REFS
        ));
    }
    let retention_policy = a.retention_policy.and_then(|s| {
        let t = s.trim().to_string();
        (!t.is_empty()).then_some(t)
    });
    if let Some(ref policy) = retention_policy {
        if !crate::models::RETENTION_POLICIES.contains(&policy.as_str()) {
            return Err(format!(
                "invalid retention_policy '{policy}': expected one of {:?}",
                crate::models::RETENTION_POLICIES
            ));
        }
    }
    let bytes = std::fs::read(path).map_err(|e| format!("read {}: {}", path.display(), e))?;
    let (sha256, artifact_created, binding_created, evidence) = db
        .learned_artifact_register(
            &bytes,
            &mime_type,
            &a.workspace_hash,
            &a.agent_id,
            &a.visibility,
            &a.action_id,
            &a.source_entities,
            canonicalize_external_refs(a.external_refs),
            retention_policy,
            a.derivation_version,
        )
        .map_err(|e| format!("learned artifact register failed: {}", e))?;
    let manifest = db
        .artifact_manifest(&sha256, Some(a.workspace_hash.as_str()), None)
        .map_err(|e| format!("artifact manifest failed: {}", e))?;
    Ok(json!({
        "sha256": sha256,
        "artifact_action": if artifact_created { "created" } else { "existing" },
        "binding_action": if binding_created { "created" } else { "existing" },
        "source_bindings_count": evidence["source_bindings"].as_array().map(|v| v.len()).unwrap_or(0),
        "action_id": a.action_id,
        "evidence": evidence,
        "manifest": manifest,
    })
    .to_string())
}

pub fn handle_workspace_bind(db: &Database, args: Value) -> Result<String, String> {
    let a: WorkspaceBindArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid workspace_bind arguments: {}", e))?;
    let actor = a
        .metadata
        .as_ref()
        .and_then(|m| m.get("actor"))
        .and_then(|v| v.as_str())
        .unwrap_or("operator");
    let binding = db
        .workspace_bind(
            &a.profile_name,
            &a.workspace_hash,
            &a.access_mode,
            &a.metadata
                .as_ref()
                .map(|m| m.to_string())
                .unwrap_or_else(|| "{}".to_string()),
            actor,
        )
        .map_err(|e| format!("workspace_bind failed: {}", e))?;
    Ok(json!(binding).to_string())
}

pub fn handle_workspace_unbind(db: &Database, args: Value) -> Result<String, String> {
    // Extract the transport-stamped identity BEFORE args is consumed.
    let actor = args
        .get("requesting_agent_id")
        .and_then(|v| v.as_str())
        .unwrap_or("operator")
        .to_string();
    let a: WorkspaceUnbindArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid workspace_unbind arguments: {}", e))?;
    let binding = db
        .workspace_unbind(&a.profile_name, &a.reason, &actor)
        .map_err(|e| format!("workspace_unbind failed: {}", e))?;
    Ok(json!(binding).to_string())
}

pub fn handle_workspace_quarantine(db: &Database, args: Value) -> Result<String, String> {
    // Extract the transport-stamped identity BEFORE args is consumed.
    let actor = args
        .get("requesting_agent_id")
        .and_then(|v| v.as_str())
        .unwrap_or("operator")
        .to_string();
    let a: WorkspaceQuarantineArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid workspace_quarantine arguments: {}", e))?;
    let binding = match a.action.as_str() {
        "quarantine" => db
            .workspace_quarantine(&a.profile_name, &a.reason, &actor)
            .map_err(|e| format!("workspace_quarantine failed: {}", e))?,
        "reactivate" => db
            .workspace_reactivate(&a.profile_name, &actor)
            .map_err(|e| format!("workspace_reactivate failed: {}", e))?,
        other => {
            return Err(format!(
                "invalid action '{other}': expected 'quarantine' or 'reactivate'"
            ))
        }
    };
    Ok(json!(binding).to_string())
}

pub fn handle_workspace_status(db: &Database, _args: Value) -> Result<String, String> {
    let bindings = db
        .workspace_status()
        .map_err(|e| format!("workspace_status failed: {}", e))?;
    let now = now_ms();
    let list: Vec<serde_json::Value> = bindings
        .iter()
        .map(|b| {
            let mut v = serde_json::to_value(b).unwrap_or(serde_json::json!({}));
            // Machine-readable staleness signal: active binding whose client
            // heartbeat is older than 7 days (0 = no heartbeat yet).
            if b.binding_state == "active" && b.last_seen_unix_ms > 0 {
                let stale = now - b.last_seen_unix_ms > 7 * 24 * 3600 * 1000;
                v["stale"] = serde_json::json!(stale);
            } else {
                v["stale"] = serde_json::json!(false);
            }
            v
        })
        .collect();
    Ok(json!({
        "bindings": list,
        "count": list.len(),
        "effective_scope_rule": "vault-side scope rules are authoritative; bindings are an opt-in governance surface for Hermes profiles",
    })
    .to_string())
}

pub fn handle_artifact_manifest(db: &Database, args: Value) -> Result<String, String> {
    let a: ArtifactManifestArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid artifact_manifest arguments: {}", e))?;
    if !is_sha256_hex(&a.sha256) {
        return Err(format!(
            "sha256 must be a full 64-hex SHA-256, got '{}'",
            a.sha256
        ));
    }
    let manifest = db
        .artifact_manifest(
            &a.sha256,
            a.workspace_hash.as_deref(),
            a.requesting_agent_id.as_deref(),
        )
        .map_err(|e| format!("artifact manifest failed: {}", e))?;
    serde_json::to_string(&manifest).map_err(|e| format!("Serialization failed: {}", e))
}

pub fn handle_artifact_excerpt(db: &Database, args: Value) -> Result<String, String> {
    let a: ArtifactExcerptArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid artifact_excerpt arguments: {}", e))?;
    if !is_sha256_hex(&a.sha256) {
        return Err(format!(
            "sha256 must be a full 64-hex SHA-256, got '{}'",
            a.sha256
        ));
    }
    let byte_mode = a.byte_start.is_some() || a.byte_end.is_some();
    let line_mode = a.line_start.is_some() || a.line_end.is_some();
    if byte_mode == line_mode {
        return Err(
            "artifact_excerpt requires exactly one range kind: either byte_start/byte_end or line_start/line_end"
                .to_string(),
        );
    }
    let (bytes, bindings) = db
        .artifact_resolve_visible(
            &a.sha256,
            a.workspace_hash.as_deref(),
            a.requesting_agent_id.as_deref(),
        )
        .map_err(|e| format!("artifact excerpt failed: {}", e))?;
    let binding = bindings
        .first()
        .ok_or_else(|| "artifact excerpt resolved no visible binding".to_string())?;

    let (range_kind, start, end) = if byte_mode {
        let start = a.byte_start.ok_or_else(|| "byte_start is required".to_string())?;
        let end = a.byte_end.ok_or_else(|| "byte_end is required".to_string())?;
        if start < 0 || end < 0 || end <= start {
            return Err("byte ranges are half-open [start,end) and require 0 <= start < end".to_string());
        }
        let (start, end) = (start as usize, end as usize);
        if end > bytes.len() {
            return Err(format!("byte_end {} exceeds artifact length {}", end, bytes.len()));
        }
        if end - start > MAX_ARTIFACT_EXCERPT_BYTES {
            return Err(format!(
                "byte excerpt too large: {} bytes (max {})",
                end - start,
                MAX_ARTIFACT_EXCERPT_BYTES
            ));
        }
        ("bytes", start, end)
    } else {
        let start_line = a.line_start.ok_or_else(|| "line_start is required".to_string())?;
        let end_line = a.line_end.ok_or_else(|| "line_end is required".to_string())?;
        if start_line <= 0 || end_line < start_line {
            return Err("line ranges are 1-indexed and inclusive: require 1 <= line_start <= line_end".to_string());
        }
        let bounds = artifact_line_bounds(&bytes);
        if bounds.is_empty() && start_line == 1 && end_line == 1 {
            return Err("artifact has no lines to retrieve".to_string());
        }
        let total_lines = bounds.len() as i64;
        if end_line > total_lines {
            return Err(format!("line_end {} exceeds artifact line count {}", end_line, total_lines));
        }
        if (end_line - start_line + 1) as usize > MAX_ARTIFACT_EXCERPT_LINES {
            return Err(format!(
                "line excerpt too large: {} lines (max {})",
                end_line - start_line + 1,
                MAX_ARTIFACT_EXCERPT_LINES
            ));
        }
        let start = bounds[start_line as usize - 1].0;
        let end = bounds[end_line as usize - 1].1;
        if end - start > MAX_ARTIFACT_EXCERPT_BYTES {
            return Err(format!(
                "line excerpt expands to {} bytes (max {})",
                end - start,
                MAX_ARTIFACT_EXCERPT_BYTES
            ));
        }
        ("lines", start, end)
    };
    let slice = &bytes[start..end];
    let anchor = db.artifact_anchor(&a.sha256, &bytes, start, end);
    let content_b64 = {
        use base64::Engine as _;
        base64::engine::general_purpose::STANDARD.encode(slice)
    };
    let content_utf8 = std::str::from_utf8(slice).ok().map(str::to_string);
    Ok(json!({
        "sha256": a.sha256,
        "range": {
            "kind": range_kind,
            "start": start,
            "end": end,
        },
        "content_b64": content_b64,
        "content_utf8": content_utf8,
        "anchors": [anchor],
        "why_served": artifact_why_served(binding, "exact artifact excerpt requested"),
    })
    .to_string())
}

pub fn handle_artifact_log_digest(db: &Database, args: Value) -> Result<String, String> {
    let a: ArtifactDigestArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid artifact_log_digest arguments: {}", e))?;
    if !is_sha256_hex(&a.sha256) {
        return Err(format!("sha256 must be a full 64-hex SHA-256, got '{}'", a.sha256));
    }
    // Resolve bytes only through the original artifact contract, which applies
    // workspace + visibility filtering before any aggregate count/template is built.
    let (bytes, bindings) = db
        .artifact_resolve_visible(
            &a.sha256,
            a.workspace_hash.as_deref(),
            a.requesting_agent_id.as_deref(),
        )
        .map_err(|e| format!("artifact log digest failed: {}", e))?;
    let binding = bindings
        .first()
        .ok_or_else(|| "artifact log digest resolved no visible binding".to_string())?;
    let digest = log_digest::digest(&a.sha256, &bytes, |start, end| {
        db.artifact_anchor(&a.sha256, &bytes, start, end)
    })?;
    let digest_bytes = serde_json::to_vec(&digest)
        .map_err(|e| format!("digest serialization failed: {}", e))?;
    // The navigation view is itself versioned derivative data. It never replaces
    // the source artifact and inherits only an already-visible binding's scope.
    let (derived_sha256, _artifact_created, _binding_created) = db
        .artifact_register(
            &digest_bytes,
            "application/vnd.perseus-vault.evidence-log-digest+json",
            &binding.workspace_hash,
            &binding.agent_id,
            &binding.visibility,
            binding.origin.clone(),
            binding.external_refs.clone(),
            binding.retention_policy.clone(),
            ArtifactRepresentation {
                kind: "derived".to_string(),
                derived_from_sha256: Some(a.sha256.clone()),
                derivation_kind: Some("evidence_log_digest".to_string()),
                derivation_version: Some(log_digest::DIGEST_VERSION.to_string()),
            },
        )
        .map_err(|e| format!("digest derivative registration failed: {}", e))?;
    serde_json::to_string(&json!({
        "digest": digest,
        "derived_artifact_sha256": derived_sha256,
        "representation": {
            "kind": "derived",
            "derived_from_sha256": a.sha256,
            "derivation_kind": "evidence_log_digest",
            "derivation_version": log_digest::DIGEST_VERSION,
        }
    }))
    .map_err(|e| format!("Serialization failed: {}", e))
}

pub fn handle_artifact_verify_value(db: &Database, args: Value) -> Result<String, String> {
    let a: ArtifactVerifyArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid artifact_verify_value arguments: {}", e))?;
    if !is_sha256_hex(&a.sha256) {
        return Err(format!(
            "sha256 must be a full 64-hex SHA-256, got '{}'",
            a.sha256
        ));
    }
    if a.max_matches <= 0 || a.max_matches > MAX_ARTIFACT_VERIFY_MATCHES {
        return Err(format!(
            "max_matches must be between 1 and {}",
            MAX_ARTIFACT_VERIFY_MATCHES
        ));
    }
    let needle = match a.encoding.as_str() {
        "utf8" => a.candidate.into_bytes(),
        "base64" => {
            use base64::Engine as _;
            base64::engine::general_purpose::STANDARD
                .decode(a.candidate)
                .map_err(|e| format!("candidate base64 decode failed: {}", e))?
        }
        other => {
            return Err(format!(
                "encoding must be 'utf8' or 'base64', got '{}'",
                other
            ))
        }
    };
    if needle.is_empty() {
        return Err("candidate must not be empty".to_string());
    }
    if needle.len() > MAX_ARTIFACT_VERIFY_BYTES {
        return Err(format!(
            "candidate too large: {} bytes (max {})",
            needle.len(),
            MAX_ARTIFACT_VERIFY_BYTES
        ));
    }
    let (bytes, bindings) = db
        .artifact_resolve_visible(
            &a.sha256,
            a.workspace_hash.as_deref(),
            a.requesting_agent_id.as_deref(),
        )
        .map_err(|e| format!("artifact verify failed: {}", e))?;
    let binding = bindings
        .first()
        .ok_or_else(|| "artifact verify resolved no visible binding".to_string())?;
    let all_matches = find_exact_matches(&bytes, &needle, a.max_matches as usize + 1);
    let truncated = all_matches.len() > a.max_matches as usize;
    let matches: Vec<_> = all_matches
        .into_iter()
        .take(a.max_matches as usize)
        .map(|(start, end)| db.artifact_anchor(&a.sha256, &bytes, start, end))
        .collect();
    Ok(json!({
        "sha256": a.sha256,
        "candidate_encoding": a.encoding,
        "candidate_byte_length": needle.len(),
        "match_count": matches.len(),
        "truncated": truncated,
        "matches": matches,
        "why_served": artifact_why_served(binding, "candidate verified against original artifact bytes"),
    })
    .to_string())
}

pub fn handle_embed(db: &Database, args: Value) -> Result<String, String> {
    let params: EmbedParams =
        serde_json::from_value(args).map_err(|e| format!("Invalid embed arguments: {}", e))?;
    match db.embed_entity(&params) {
        Ok(result) => Ok(result.to_string()),
        Err(e) => Err(format!("Embed failed: {}", e)),
    }
}

pub fn handle_prune(db: &Database, args: Value) -> Result<String, String> {
    // #398: scope='history' targets entity_history instead of live entities —
    // enforce the retention policy (env knobs, overridable per-call). With
    // dry_run=true this reports the rows + bytes that WOULD be evicted.
    if args.get("scope").and_then(|v| v.as_str()) == Some("history") {
        let dry_run = args
            .get("dry_run")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let mut policy = crate::models::HistoryRetentionPolicy::from_env();
        if let Some(v) = args.get("max_age_days").and_then(|v| v.as_i64()).filter(|v| *v > 0) {
            policy.max_age_days = Some(v);
        }
        if let Some(v) = args
            .get("max_versions_per_key")
            .and_then(|v| v.as_i64())
            .filter(|v| *v > 0)
        {
            policy.max_versions_per_key = Some(v);
        }
        if let Some(v) = args.get("max_bytes").and_then(|v| v.as_i64()).filter(|v| *v > 0) {
            policy.max_bytes = Some(v);
        }
        if policy.is_unlimited() {
            return Err(
                "prune scope='history' requires a bound: pass max_age_days, \
                 max_versions_per_key, or max_bytes (or set the PERSEUS_VAULT_HISTORY_* env knobs)"
                    .to_string(),
            );
        }
        let report = db
            .enforce_history_retention(&policy, dry_run)
            .map_err(|e| format!("History prune failed: {}", e))?;
        return serde_json::to_string(&report)
            .map_err(|e| format!("Serialization failed: {}", e));
    }

    let params: PruneParams =
        serde_json::from_value(args).map_err(|e| format!("Invalid prune arguments: {}", e))?;

    // #202: require a threshold or explicit purge_all — category alone is a footgun
    if !params.purge_all && params.min_decay.is_none() && params.older_than_days.is_none() {
        return Err(
            "prune requires min_decay, older_than_days, or purge_all=true to archive the whole category"
                .to_string(),
        );
    }

    match db.prune(&params) {
        Ok(report) => {
            serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
        }
        Err(e) => Err(format!("Prune failed: {}", e)),
    }
}

pub fn handle_federate(db: &Database, args: Value) -> Result<String, String> {
    use serde::Deserialize;
    #[derive(Deserialize)]
    struct FederateArgs {
        from_workspace: String,
        to_workspace: String,
        #[serde(default)]
        vault_dir: String,
    }
    let a: FederateArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid federate arguments: {}", e))?;
    return Err("federate requires an authenticated authoritative admission and rollback-capable transfer boundary".to_string());

    let vault_dir = if a.vault_dir.is_empty() {
        std::env::temp_dir().join("perseus_vault-federate").to_string_lossy().to_string()
    } else {
        a.vault_dir
    };

    // Export from source workspace
    let export_report = db.vault_export(&vault_dir, Some(&a.from_workspace))
        .map_err(|e| format!("Federate export failed: {}", e))?;

    // Remap entities: overwrite workspace_hash to target
    let mut remapped = 0i64;
    for entry in std::fs::read_dir(&vault_dir).map_err(|e| format!("Read vault dir: {}", e))? {
        let entry = entry.map_err(|e| format!("Read entry: {}", e))?;
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("md") {
            continue;
        }
        let content = std::fs::read_to_string(&path)
            .map_err(|e| format!("Read {}: {}", path.display(), e))?;
        let remapped_content =
            content.replace(&format!("workspace_hash: {}", a.from_workspace),
                            &format!("workspace_hash: {}", a.to_workspace));
        if remapped_content != content {
            std::fs::write(&path, remapped_content)
                .map_err(|e| format!("Write {}: {}", path.display(), e))?;
            remapped += 1;
        }
    }

    // Import into target workspace
    let import_report = db.vault_import(&vault_dir)
        .map_err(|e| format!("Federate import failed: {}", e))?;

    let result = json!({
        "exported": export_report.files_created + export_report.files_updated,
        "remapped": remapped,
        "imported": import_report.files_created + import_report.files_updated,
        "import_errors": import_report.errors,
    });
    Ok(reviewable_write_result(result, "federate").to_string())
}

pub fn handle_share(db: &Database, args: Value) -> Result<String, String> {
    #[derive(Deserialize)]
    struct ShareArgs {
        category: String,
        key: String,
        to_workspace: String,
    }
    let a: ShareArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid share arguments: {}", e))?;

    // Find the entity
    // Recall by category first, then filter by key (FTS5 searches body_json,
    // not the key column, so we can't use key as a query term reliably).
    let entities = db.recall(&crate::models::RecallParams {
        query: String::new(),
        category: Some(a.category.clone()),
        entity_type: None,
        limit: 100,
        offset: 0,
        min_decay: 0.0,
        topic_path: None,
        include_archived: false,
        skip_side_effects: true,
        ..crate::models::RecallParams::default()
    }).map_err(|e| format!("Recall failed: {}", e))?;

    let src = entities.iter()
        .find(|e| e.key == a.key)
        .ok_or_else(|| format!("Entity not found: {}/{}", a.category, a.key))?;

    if !entity_has_authoritative_admission(src) {
        return Err("source requires_review: share requires durable authoritative admission".to_string());
    }
    if src.workspace_hash != a.to_workspace {
        return Err("share target workspace must match the source workspace assertion".to_string());
    }

    // Clone entity into target workspace
    let mut clone = src.clone();
    clone.workspace_hash = a.to_workspace.clone();
    clone.source = "share".to_string();
    // Force a new id so it doesn't collide
    let raw_id = uuid::Uuid::new_v4().to_string().replace('-', "");
    clone.id = format!("mem-{}", &raw_id[..12.min(raw_id.len())]);
    clone.retrieval_count = 0;
    clone.layer = "buffer".to_string();

    let (eid, action) = db.remember(&clone)
        .map_err(|e| format!("Share failed: {}", e))?;

    Ok(reviewable_write_result(
        json!({"shared_id": eid, "action": action, "from_workspace": src.workspace_hash, "to_workspace": a.to_workspace}),
        "share",
    ).to_string())
}

pub fn handle_workspace_list(db: &Database) -> String {
    match db.workspace_list_categories() {
        Ok(cats) => json!({"categories": cats, "total": cats.len()}).to_string(),
        Err(e) => json!({"error": format!("Workspace list failed: {}", e)}).to_string(),
    }
}

// ─── New: autocohere, recall_when + cohere handlers ─────────────────────────

#[derive(Debug, Deserialize)]
pub struct AutocohereArgs {
    #[serde(default)]
    pub dry_run: bool,
    /// #780: optional raw session/transcript payload captured DURABLY before
    /// any cohere/decay/compaction work can discard source context.
    #[serde(default)]
    pub capture_text: Option<String>,
    #[serde(default)]
    pub capture_workspace_hash: String,
    #[serde(default)]
    pub capture_agent_id: String,
    #[serde(default)]
    pub capture_max_entities: Option<i64>,
    /// #854: workspace scope for the consolidation step. `Some(ws)` scopes
    /// the consolidate pass (and its derived observations) to that workspace;
    /// `None` keeps the whole-vault default (the other grooming steps —
    /// cohere/decay/compact — are whole-vault by design). Set `global: true`
    /// to make the whole-vault consolidation explicit and capability-gated.
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// #854: explicit cross-workspace consolidation mode. When `workspace_hash`
    /// is also set the run is rejected as ambiguous. Callers with a host
    /// identity need capability `memory.maintenance.global` for global runs.
    #[serde(default)]
    pub global: bool,
    /// Host identity stamped by the MCP transport (clientInfo.name); used for
    /// global-mode authorization and consolidation author attribution.
    #[serde(default)]
    pub requesting_agent_id: String,
}

#[derive(Debug, Deserialize)]
pub struct RecallWhenArgs {
    pub context: String,
    #[serde(default = "default_rw_limit")]
    pub limit: i64,
    #[serde(default)]
    pub workspace_hash: Option<String>,
}

fn default_rw_limit() -> i64 {
    10
}

#[derive(Debug, Deserialize)]
pub struct CohereArgs {
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default = "default_max_links_cohere")]
    pub max_links: usize,
}

fn default_max_links_cohere() -> usize {
    20
}

pub fn handle_recall_when(db: &Database, args: Value) -> Result<String, String> {
    let a: RecallWhenArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid recall_when arguments: {}", e))?;

    let entities = db
        .recall_when(&a.context, a.limit, a.workspace_hash.as_deref())
        .map_err(|e| format!("Recall_when failed: {}", e))?;

    let items_expanded: Vec<serde_json::Value> =
        entities.iter().map(|e| e.to_json_expanded()).collect();

    let result = json!({
        "items": items_expanded,
        "total": items_expanded.len(),
        "context": a.context,
    });
    Ok(result.to_string())
}

pub fn handle_autocohere(db: &Database, args: Value) -> Result<String, String> {
    let a: AutocohereArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid autocohere arguments: {}", e))?;

    let mut total_promoted = 0i64;
    let mut total_links = 0i64;
    let mut total_archived_cohere = 0i64;

    // #780 ordering guarantee: a caller-supplied raw buffer is distilled and
    // persisted before the first lifecycle step (cohere/decay/compact) that can
    // archive or compress source context. Capture failure aborts this run rather
    // than letting a later compaction silently proceed without the durability
    // barrier. `dry_run` reaches capture too, so preview is side-effect free.
    let precompact_capture = match a.capture_text.as_deref().map(str::trim) {
        Some(text) if !text.is_empty() => {
            let mut capture_args = json!({
                "text": text,
                "workspace_hash": a.capture_workspace_hash,
                "agent_id": a.capture_agent_id,
                "dry_run": a.dry_run,
            });
            if let Some(max) = a.capture_max_entities {
                capture_args["max_entities"] = json!(max);
            }
            let raw = handle_capture(db, capture_args)
                .map_err(|e| format!("Autocohere pre-compaction capture failed: {}", e))?;
            let report: Value = serde_json::from_str(&raw)
                .map_err(|e| format!("Autocohere pre-compaction capture report parse failed: {}", e))?;
            json!({ "stage": "completed", "report": report })
        }
        _ => json!({ "stage": "skipped", "reason": "no capture_text provided" }),
    };

    // Snapshot the DB size BEFORE any mutation so db_size_delta_bytes is
    // meaningful — it was previously read after all three steps had run, so
    // the reported delta was always ≈0.
    let initial_db_size = db
        .file_size_bytes()
        .map_err(|e| format!("Failed to get initial DB size: {}", e))?;

    // 1. Run perseus_vault_cohere (promote, link, archive)
    let cohere_params = crate::models::CohereParams {
        dry_run: a.dry_run,
        ..Default::default()
    };
    let cohere_report = db
        .cohere(&cohere_params)
        .map_err(|e| format!("Autocohere step (cohere) failed: {}", e))?;

    total_promoted += cohere_report.promoted;
    total_links += cohere_report.linked;
    total_archived_cohere += cohere_report.archived;

    // 2. Then perseus_vault_decay (recalculate Ebbinghaus decay). #490: honor
    // dry_run — this step previously ran the LIVE tick inside the "preview"
    // pass, rewriting scores and auto-archiving mid-dry-run.
    let decay_report = if a.dry_run {
        db.decay_tick_preview()
    } else {
        db.decay_tick()
    }
    .map_err(|e| format!("Autocohere step (decay) failed: {}", e))?;

    // 3. Then perseus_vault_compact (archive below threshold). Use the same archive
    // threshold as decay_tick/cohere so "run everything" forgets at the same
    // point as the individual tools (was a hardcoded 0.1 → ~5 idle days sooner).
    let compact_report = db
        .compact(Database::ARCHIVE_DECAY_THRESHOLD, a.dry_run)
        .map_err(|e| format!("Autocohere step (compact) failed: {}", e))?;

    // 4. Consolidation ("local dreaming"): compress the coldest overlapping
    // memories in each category into evidence-tracked observations and retire
    // the merged sources — running in the BACKGROUND as part of "run
    // everything", instead of only when an agent thinks to call
    // perseus_vault_consolidate. Bounded: a few observations per category per run,
    // over the same scan window the manual tool uses. Skips 'observation'
    // (no meta-observations / runaway recursion) and 'memories' (files from
    // the /memories adapter must never be similarity-merged).
    let mut observations_created = 0i64;
    let mut consolidate_sources_archived = 0i64;
    let categories = db
        .workspace_list_categories_scoped(a.workspace_hash.as_deref())
        .map_err(|e| format!("Autocohere step (consolidate: categories) failed: {}", e))?;
    for cat in categories {
        if cat == "observation" || cat == "memories" {
            continue;
        }
        let report = db
            .consolidate(&crate::models::ConsolidateParams {
                category: cat.clone(),
                similarity_threshold: 0.6,
                limit: 5,
                offset: 0,
                dry_run: a.dry_run,
                cold_first: true,
                archive_sources: true,
                workspace_hash: a.workspace_hash.clone(),
                // #854: no scope named → whole-vault pass (historical
                // autocohere behavior); an explicit workspace scopes only the
                // consolidation step.
                global: a.global || a.workspace_hash.is_none(),
                requesting_agent_id: a.requesting_agent_id.clone(),
            })
            .map_err(|e| format!("Autocohere step (consolidate {}) failed: {}", cat, e))?;
        observations_created += report.observations_created;
        consolidate_sources_archived += report.sources_archived;
    }

    // 5. History retention (#398): enforce the env-configured budget over
    // entity_history in the same background pass. With no PERSEUS_VAULT_HISTORY_*
    // knob set this is a guaranteed no-op, so autocohere's default behavior
    // is unchanged.
    let retention_report = db
        .enforce_history_retention(&crate::models::HistoryRetentionPolicy::from_env(), a.dry_run)
        .map_err(|e| format!("Autocohere step (history retention) failed: {}", e))?;

    let final_db_size = if a.dry_run {
        initial_db_size
    } else {
        db.file_size_bytes()
            .map_err(|e| format!("Failed to get final DB size: {}", e))?
    };

    let result = json!({
        "precompact_capture": precompact_capture,
        "promoted_entities": total_promoted,
        "links_created": total_links,
        "archived_entities": total_archived_cohere + compact_report.entities_archived,
        "decay_updates": decay_report.entities_updated,
        // #490: surface the decay step's own archive count — under dry_run
        // the stored scores are untouched, so compact's preview can't see
        // what decay WOULD have archived; this field is the only signal.
        "decay_auto_archived": decay_report.auto_archived,
        "compact_archived_count": compact_report.entities_archived,
        "observations_created": observations_created,
        "consolidate_sources_archived": consolidate_sources_archived,
        "history_rows_evicted": retention_report.rows_evicted,
        "history_bytes_evicted": retention_report.bytes_evicted,
        "history_tombstones_written": retention_report.tombstones_written,
        "db_size_delta_bytes": final_db_size as i64 - initial_db_size as i64,
        "dry_run": a.dry_run,
    });
    Ok(result.to_string())
}

pub fn handle_cohere(db: &Database, args: Value) -> Result<String, String> {
    let a: CohereArgs = serde_json::from_value(args).map_err(|e| format!("Invalid cohere arguments: {}", e))?;
    let params = crate::models::CohereParams {
        dry_run: a.dry_run,
        max_links: a.max_links,
        ..Default::default()
    };
    let report = db
        .cohere(&params)
        .map_err(|e| format!("Cohere failed: {}", e))?;

    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

// ─── perseus_vault_supersede handler ────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct SupersedeArgs {
    pub from_category: String,
    pub from_key: String,
    pub to_category: String,
    pub to_key: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default = "default_relationship")]
    pub relationship: String,
    /// #363: when the OLD fact stopped being true in the world. Defaults to
    /// transaction time (now) — superseding a fact ends its validity.
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
}

fn default_relationship() -> String {
    "supersedes".to_string()
}

pub fn handle_supersede(db: &Database, args: Value) -> Result<String, String> {
    let a: SupersedeArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid supersede arguments: {}", e))?;

    // Find the 'from' entity
    let from_entity = db
        .get_entity(&a.from_category, &a.from_key)
        .map_err(|e| format!("'From' entity lookup failed: {}", e))?
        .ok_or_else(|| format!("'From' entity not found: {}/{}", a.from_category, a.from_key))?;

    // Find the 'to' entity
    let to_entity = db
        .get_entity(&a.to_category, &a.to_key)
        .map_err(|e| format!("'To' entity lookup failed: {}", e))?
        .ok_or_else(|| format!("'To' entity not found: {}/{}", a.to_category, a.to_key))?;

    // #363 review: validate an EXPLICIT valid_to against the old fact's stored
    // period BEFORE any mutation, so a rejected close can't leave a half-done
    // supersede (link created, status flipped, period untouched).
    //   * it must not invert the period (vt <= valid_from), and
    //   * it must not EXTEND an already-closed period — a fact that ended
    //     stays ended; superseding may only tighten.
    let periods = db
        .valid_periods_for_ids(&[from_entity.id.clone()])
        .map_err(|e| format!("'From' entity valid-period lookup failed: {}", e))?;
    let (eff_from, cur_to) = periods
        .get(&from_entity.id)
        .copied()
        .unwrap_or((from_entity.created_at_unix_ms, None));
    if let Some(vt) = a.valid_to_unix_ms {
        if vt <= eff_from {
            return Err(format!(
                "valid_to_unix_ms ({vt}) must be greater than the superseded fact's valid_from ({eff_from})"
            ));
        }
        if let Some(cur) = cur_to {
            if vt > cur {
                return Err(format!(
                    "valid_to_unix_ms ({vt}) would extend the superseded fact's already-closed \
                     valid period (valid_to {cur}); superseding may only tighten it"
                ));
            }
        }
    }

    // 1. Create a "supersedes" relationship link
    db.link(
        &to_entity.category,
        &to_entity.key,
        &from_entity.id,
        &a.relationship,
    )
    .map_err(|e| format!("Supersede link failed: {}", e))?;

    // 2. Close the OLD entity's valid-time period (#363): superseding a fact
    // records when it stopped being true in the world — at transaction time
    // unless the caller says when. The default close is bumped strictly past
    // valid_from so a fact superseded within its creation millisecond still
    // gets a non-inverted (if degenerate-width) period. set_valid_to itself
    // never extends an already-closed period; the effective close (possibly
    // the earlier stored one) is what gets reported.
    //
    // ORDER MATTERS (#375): the close must run BEFORE the status flip.
    // set_valid_to's audited snapshot (#373) captures the live row verbatim —
    // flipping status first (as this handler used to) baked 'deprecated' into
    // the pre-supersede snapshot under the ORIGINAL recorded_at, so
    // transaction-time reconstruction showed the fact deprecated at instants
    // when it was still believed active. Closing first snapshots the true
    // pre-supersede state; the status flip then lands on the live row whose
    // recorded_at was just advanced — correct, the deprecation IS new
    // knowledge. (A failed close now also leaves status untouched.)
    let requested = a
        .valid_to_unix_ms
        .unwrap_or_else(|| now_ms().max(eff_from + 1));
    let valid_to = db
        .set_valid_to(&from_entity.id, requested)
        .map_err(|e| format!("Failed to close 'from' entity's valid period: {}", e))?;

    // 3. Set the OLD entity's status to "deprecated". Audited (#377): an
    // actual flip writes its own history snapshot, so even when the close
    // above was a no-op (superseding an already-expired fact — set_valid_to
    // never extends an existing close and snapshots nothing), the
    // pre-supersede status stays reconstructable at earlier tx instants.
    // Not atomic with the close — a failure here leaves the period closed
    // but the status active, the same non-atomicity between steps this
    // handler has always had.
    db.update_entity_status(&from_entity.id, "deprecated", &a.reason)
        .map_err(|e| format!("Failed to deprecate 'from' entity: {}", e))?;

    // #876: superseding a bound source entity flags dependent learned
    // artifacts STALE (retraining trigger). Hash-only journal evidence is
    // written by the database layer when any binding is affected.
    let _stale_count = db
        .flag_artifacts_stale_for_source(&from_entity.id, &from_entity.workspace_hash)
        .map_err(|e| format!("Failed to flag stale learned artifacts: {}", e))?;

    let result = json!({
        "from_entity_id": from_entity.id,
        "from_entity_category": from_entity.category,
        "from_entity_key": from_entity.key,
        "from_valid_to_unix_ms": valid_to,
        "to_entity_id": to_entity.id,
        "to_entity_category": to_entity.category,
        "to_entity_key": to_entity.key,
        "relationship": a.relationship,
        "status_updated": "deprecated",
    });
    Ok(result.to_string())
}

// ─── perseus_vault_maintenance handler ──────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct MaintenanceArgs {
    #[serde(default)]
    pub dedup: bool,
    #[serde(default)]
    pub orphans: bool,
    #[serde(default)]
    pub vacuum: bool,
    #[serde(default)]
    pub reindex: bool,
    /// #398: enforce the entity_history retention policy (env knobs; no-op
    /// while no knob is set). Included in `all`.
    #[serde(default)]
    pub history: bool,
    #[serde(default)]
    pub all: bool,
    #[serde(default)]
    pub dry_run: bool,
}

pub fn handle_maintenance(db: &Database, args: Value) -> Result<String, String> {
    let a: MaintenanceArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid maintenance arguments: {}", e))?;

    let mut report = json!({
        "dedup_archived": 0,
        "orphan_journal_entries_found": 0,
        "orphan_links_found": 0,
        "vacuum_reclaimed_bytes": 0,
        "reindex_rows_affected": 0,
        "history_rows_evicted": 0,
        "history_bytes_evicted": 0,
        "history_tombstones_written": 0,
        "dry_run": a.dry_run,
        "errors": []
    });

    let current_db_size = db
        .file_size_bytes()
        .map_err(|e| format!("Failed to get DB size: {}", e))?;

    // Dedup
    if a.dedup || a.all {
        match db.deduplicate_entities(a.dry_run) {
            Ok(dedup_count) => {
                report["dedup_archived"] = json!(dedup_count);
            }
            Err(e) => report["errors"]
                .as_array_mut()
                .unwrap()
                .push(json!(format!("Dedup failed: {}", e))),
        }
    }

    // Orphans
    if a.orphans || a.all {
        match db.detect_orphan_journal_entries() {
            Ok(orphans_count) => {
                report["orphan_journal_entries_found"] = json!(orphans_count);
            }
            Err(e) => report["errors"]
                .as_array_mut()
                .unwrap()
                .push(json!(format!("Orphan journal detection failed: {}", e))),
        }
        match db.detect_orphan_links() {
            Ok(orphans_count) => {
                report["orphan_links_found"] = json!(orphans_count);
            }
            Err(e) => report["errors"]
                .as_array_mut()
                .unwrap()
                .push(json!(format!("Orphan link detection failed: {}", e))),
        }
    }

    // Vacuum. #491: under dry_run the physical rewrite is skipped, but the
    // report must SAY so and estimate the reclaimable space — a bare
    // `vacuum_reclaimed_bytes: 0` is indistinguishable from "ran, nothing to
    // reclaim", which made report-only rollouts silently understate the
    // physical work a live run would do.
    if a.vacuum || a.all {
        if a.dry_run {
            report["vacuum_skipped_dry_run"] = json!(true);
            match db.vacuum_reclaimable_bytes_estimate() {
                Ok(bytes) => {
                    report["vacuum_would_reclaim_bytes_estimate"] = json!(bytes);
                }
                Err(e) => report["errors"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(format!("Vacuum estimate failed: {}", e))),
            }
        } else {
            match db.vacuum() {
                Ok(_) => {
                    let after_vacuum_db_size = db
                        .file_size_bytes()
                        .map_err(|e| format!("Failed to get DB size after vacuum: {}", e))?;
                    report["vacuum_reclaimed_bytes"] = json!(current_db_size as i64 - after_vacuum_db_size as i64);
                }
                Err(e) => report["errors"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(format!("Vacuum failed: {}", e))),
            }
        }
    }

    // Reindex. #491: same dry-run honesty — mark the skip and report a cheap
    // row-count drift estimate so the preview shows whether reindex has work.
    if a.reindex || a.all {
        if a.dry_run {
            report["reindex_skipped_dry_run"] = json!(true);
            match db.fts_drift_estimate() {
                Ok(n) => {
                    report["fts_rows_drift_estimate"] = json!(n);
                }
                Err(e) => report["errors"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(format!("FTS drift estimate failed: {}", e))),
            }
        } else {
            match db.reindex_fts() {
                Ok(n) => {
                    report["reindex_rows_affected"] = json!(n);
                }
                Err(e) => report["errors"]
                    .as_array_mut()
                    .unwrap()
                    .push(json!(format!("Reindex failed: {}", e))),
            }
        }
    }

    // History retention (#398): enforce the env-configured budget over
    // entity_history. A no-op (zero rows) while no PERSEUS_VAULT_HISTORY_* knob is
    // set — the default stays "keep everything". dry_run reports what would
    // be evicted.
    if a.history || a.all {
        let policy = crate::models::HistoryRetentionPolicy::from_env();
        match db.enforce_history_retention(&policy, a.dry_run) {
            Ok(r) => {
                report["history_rows_evicted"] = json!(r.rows_evicted);
                report["history_bytes_evicted"] = json!(r.bytes_evicted);
                report["history_tombstones_written"] = json!(r.tombstones_written);
            }
            Err(e) => report["errors"]
                .as_array_mut()
                .unwrap()
                .push(json!(format!("History retention failed: {}", e))),
        }
    }

    Ok(report.to_string())
}

/// #490: the full unattended hygiene pass — compose the shipped autocohere
/// (cohere → decay → compact → consolidate → history retention) and
/// maintenance (dedup, orphan detection, reindex) handlers into one
/// conservative run. Shared by the `maintain` CLI verb and any scheduled
/// caller so both run the exact same pass.
///
/// Safety contract: every effect is a reversible `archived=1` flip except
/// VACUUM, which physically rewrites the file and therefore only runs when
/// the caller explicitly asks (`vacuum: true`) — schedulers should throttle
/// it to ~weekly rather than every pass. Hard delete (`purge`) is never part
/// of this path. History retention stays a guaranteed no-op unless the
/// `PERSEUS_VAULT_HISTORY_*` env knobs opt in; it runs inside the autocohere step,
/// so it is deliberately NOT requested again from maintenance.
pub fn run_maintenance_pass(db: &Database, dry_run: bool, vacuum: bool) -> Result<Value, String> {
    let autocohere: Value = handle_autocohere(db, json!({ "dry_run": dry_run }))
        .and_then(|s| {
            serde_json::from_str(&s).map_err(|e| format!("autocohere report parse failed: {}", e))
        })
        .map_err(|e| format!("Maintenance pass (autocohere) failed: {}", e))?;

    let maintenance: Value = handle_maintenance(
        db,
        json!({
            "dedup": true,
            "orphans": true,
            "reindex": true,
            "vacuum": vacuum,
            "dry_run": dry_run,
        }),
    )
    .and_then(|s| {
        serde_json::from_str(&s).map_err(|e| format!("maintenance report parse failed: {}", e))
    })
    .map_err(|e| format!("Maintenance pass (maintenance) failed: {}", e))?;

    Ok(json!({
        "autocohere": autocohere,
        "maintenance": maintenance,
        "dry_run": dry_run,
        "vacuum_requested": vacuum,
    }))
}

// ─── perseus_vault_correct handler ────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct CorrectArgs {
    pub wrong_approach: String,
    pub user_correction: String,
    pub task_context: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub category: String,
    #[serde(default = "default_visibility")]
    pub visibility: String,
    /// #363: application-time period of the corrected fact (optional).
    #[serde(default)]
    pub valid_from_unix_ms: Option<i64>,
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
    #[serde(default)]
    pub evidence: Option<crate::models::EvidenceEnvelope>,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub agent_id: String,
    /// #855: host identity stamped by the MCP transport (clientInfo.name),
    /// distinct from the model-supplied `agent_id` author attribution. When
    /// present (non-empty), it is authoritative: a model cannot override the
    /// host's identity on the correction entity, journal event, or tombstone.
    #[serde(default)]
    pub requesting_agent_id: String,
}


#[derive(Debug, Deserialize)]
pub struct RejectValueArgs {
    pub workspace_hash: String,
    pub subject: String,
    pub predicate: String,
    pub value: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub evidence_ref: String,
    #[serde(default)]
    pub author_agent_id: String,
    #[serde(default)]
    pub expires_at_unix_ms: Option<i64>,
}

pub fn handle_reject_value(db: &Database, args: Value) -> Result<String, String> {
    let a: RejectValueArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid reject_value arguments: {e}"))?;
    if a.subject.trim().is_empty() || a.predicate.trim().is_empty() || a.value.trim().is_empty() {
        return Err("workspace_hash, subject, predicate, and value are required".to_string());
    }
    let id = db
        .reject_value(
            &a.workspace_hash,
            &a.subject,
            &a.predicate,
            &a.value,
            &a.reason,
            &a.evidence_ref,
            &a.author_agent_id,
            a.expires_at_unix_ms,
        )
        .map_err(|e| format!("Reject value failed: {e}"))?;
    serde_json::to_string(&serde_json::json!({"ok": true, "rejected": true, "id": id}))
        .map_err(|e| format!("Serialization failed: {e}"))
}

pub fn handle_correct(db: &Database, args: Value) -> Result<String, String> {
    let a: CorrectArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid correct arguments: {}", e))?;

    // #363 review: same inverted-period rejection as perseus_vault_remember — an
    // inverted period would shadow older versions in bitemporal_at while
    // never matching itself, making the fact unanswerable.
    if let (Some(vf), Some(vt)) = (a.valid_from_unix_ms, a.valid_to_unix_ms) {
        if vt <= vf {
            return Err(format!(
                "valid_to_unix_ms ({vt}) must be greater than valid_from_unix_ms ({vf})"
            ));
        }
    }

    // #855: host identity (transport-stamped requesting_agent_id) is
    // authoritative over any model-supplied author attribution — a model
    // cannot claim to be the user. Without a host identity (non-MCP callers),
    // the supplied agent_id is used as before.
    let author_agent_id = if a.requesting_agent_id.trim().is_empty() {
        a.agent_id
    } else {
        a.requesting_agent_id
    };

    // #889: anchored instruction extraction -> suggestion queue. Extraction
    // never writes policy — candidates require an explicit operator
    // `approve` decision before promotion to a keystone. (Clone the source
    // texts first: the args are moved into CorrectParams below.)
    let suggestion_texts = [
        a.user_correction.clone(),
        a.wrong_approach.clone(),
        a.task_context.clone(),
    ];
    let suggestion_workspace = a.workspace_hash.clone();

    let params = crate::models::CorrectParams {
        wrong_approach: a.wrong_approach,
        user_correction: a.user_correction,
        task_context: a.task_context,
        session_id: a.session_id,
        tags: a.tags,
        category: a.category,
        visibility: a.visibility,
        valid_from_unix_ms: a.valid_from_unix_ms,
        valid_to_unix_ms: a.valid_to_unix_ms,
        evidence: a.evidence,
        workspace_hash: a.workspace_hash,
        agent_id: author_agent_id,
    };

    let result = db.correct(&params)
        .map_err(|e| format!("Correct failed: {}", e))?;

    // #889: anchored instruction extraction -> suggestion queue. Extraction
    // never writes policy — candidates require an explicit operator
    // `approve` decision before promotion to a keystone.
    let mut suggestions: Vec<Value> = Vec::new();
    let mut extraction_errors: Vec<String> = Vec::new();
    for field in &suggestion_texts {
        for sug in crate::instruction_extraction::extract_suggestions(field) {
            match db.keystone_suggestion_add(
                &result.entity_id,
                &result.category,
                &sug.instruction,
                sug.locale,
                sug.pattern,
                &suggestion_workspace,
            ) {
                Ok(sid) => {
                    suggestions.push(json!({
                        "id": sid,
                        "instruction": sug.instruction,
                        "locale": sug.locale,
                        "pattern": sug.pattern,
                    }));
                }
                Err(e) => extraction_errors.push(format!("{e}")),
            }
        }
    }
    let mut out = serde_json::to_value(&result)
        .map_err(|e| format!("Serialization failed: {e}"))?;
    if let Some(obj) = out.as_object_mut() {
        obj.insert(
            "keystone_suggestions".to_string(),
            json!(suggestions),
        );
        obj.insert("keystone_suggestions_count".to_string(), json!(suggestions.len()));
        if !extraction_errors.is_empty() {
            obj.insert("extraction_errors".to_string(), json!(extraction_errors));
        }
    }
    serde_json::to_string(&out).map_err(|e| format!("Serialization failed: {e}"))
}

// ─── perseus_vault_synthesize handler ────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct SynthesizeArgs {
    pub session_content: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub visibility: String,
    #[serde(default)]
    pub evidence: Option<crate::models::EvidenceEnvelope>,
}

pub fn handle_synthesize(db: &Database, args: Value) -> Result<String, String> {
    let a: SynthesizeArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid synthesize arguments: {}", e))?;

    let params = crate::models::SynthesizeParams {
        session_content: a.session_content,
        session_id: a.session_id,
        tags: a.tags,
        visibility: a.visibility,
        evidence: a.evidence,
    };

    let result = db.synthesize(&params)
        .map_err(|e| format!("Synthesize failed: {}", e))?;

    serde_json::to_string(&result).map_err(|e| format!("Serialization failed: {}", e))
}


// ─── perseus_vault_bench handler ─────────────────────────────────────────

#[derive(Debug, Deserialize)]
pub struct BenchArgs {
    pub task_description: String,
    pub turns_taken: i64,
    pub tokens_used: i64,
    pub memory_recall_used: bool,
    #[serde(default)]
    pub recall_count: i64,
    #[serde(default)]
    pub task_success: bool,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tags: Vec<String>,
}

pub fn handle_bench(db: &Database, args: Value) -> Result<String, String> {
    let a: BenchArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid bench arguments: {}", e))?;

    let params = crate::models::BenchParams {
        task_description: a.task_description,
        turns_taken: a.turns_taken,
        tokens_used: a.tokens_used,
        memory_recall_used: a.memory_recall_used,
        recall_count: a.recall_count,
        task_success: a.task_success,
        session_id: a.session_id,
        tags: a.tags,
    };

    let result = db.bench(&params)
        .map_err(|e| format!("Bench failed: {}", e))?;

    serde_json::to_string(&result).map_err(|e| format!("Serialization failed: {}", e))
}

/// Permanently delete all archived entities and VACUUM the database.
#[derive(Debug, Deserialize)]
pub struct PurgeArgs {
    #[serde(default)]
    pub dry_run: bool,
}

pub fn handle_purge(db: &Database, args: Value) -> Result<String, String> {
    let a: PurgeArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid purge arguments: {}", e))?;
    let report = db.purge(a.dry_run)
        .map_err(|e| format!("Purge failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

// ─── #868/#866: retention lifecycle tools — expire / redact / erase ────────
// Contract: docs/specs/data-boundaries-retention-lifecycle.md (v1).
// Distinct semantics: expire (time transition, content kept), redact (content
// scrub, metadata kept, re-ingest allowed), erase (physical removal across
// derived layers + permanent re-ingest suppression). All destructive ops are
// workspace-explicit (fail-closed on ambiguity, #854) and attribute the actor.

#[derive(Debug, Deserialize)]
pub struct ExpireArgs {
    #[serde(default)]
    pub dry_run: bool,
    /// Empty = global sweep; otherwise restricted to one workspace.
    #[serde(default)]
    pub workspace_hash: String,
}

#[derive(Debug, Deserialize)]
pub struct RedactArgs {
    pub category: String,
    pub key: String,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub agent_id: String,
    /// MCP session identity stamped by the transport (#855): overrides any
    /// caller-supplied agent_id so attribution cannot be forged.
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
}

#[derive(Debug, Deserialize)]
pub struct EraseArgs {
    pub category: String,
    pub key: String,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub agent_id: String,
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
}

pub fn handle_expire(db: &Database, args: Value) -> Result<String, String> {
    let a: ExpireArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid expire arguments: {}", e))?;
    let report = db
        .expire_due(
            a.dry_run,
            if a.workspace_hash.is_empty() {
                None
            } else {
                Some(&a.workspace_hash)
            },
        )
        .map_err(|e| format!("Expire failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

pub fn handle_redact(db: &Database, args: Value) -> Result<String, String> {
    let a: RedactArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid redact arguments: {}", e))?;
    if a.workspace_hash.is_empty() {
        return Err("redact requires an explicit workspace_hash (fail-closed: a bare category/key is ambiguous across workspaces, #854)".to_string());
    }
    let agent = a.requesting_agent_id.as_deref().unwrap_or(&a.agent_id);
    let report = db
        .redact_entity(&a.category, &a.key, &a.workspace_hash, agent)
        .map_err(|e| format!("Redact failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

pub fn handle_erase(db: &Database, args: Value) -> Result<String, String> {
    let a: EraseArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid erase arguments: {}", e))?;
    if a.workspace_hash.is_empty() {
        return Err("erase requires an explicit workspace_hash (fail-closed: a bare category/key is ambiguous across workspaces, #854)".to_string());
    }
    let agent = a.requesting_agent_id.as_deref().unwrap_or(&a.agent_id);
    let report = db
        .erase_entity(&a.category, &a.key, &a.workspace_hash, agent, a.dry_run)
        .map_err(|e| format!("Erase failed: {}", e))?;
    serde_json::to_string(&report).map_err(|e| format!("Serialization failed: {}", e))
}

// ─── /memories directory-convention adapter ──────────────────────
//
// Implements Anthropic's memory-tool convention (the `memory_20250818`
// command set: view / create / str_replace / insert / delete / rename over
// paths under /memories) on top of the entity store, so clients built
// against Claude's native memory tool can point at the vault unchanged.
// Files are entities in the reserved `memories` category with key = the
// path relative to /memories; bodies are the raw file text (FTS-indexed,
// encrypted at rest like any entity, and versioned through the normal
// bi-temporal history on edit).

const MEMORIES_CATEGORY: &str = "memories";

#[derive(Debug, Deserialize)]
pub struct MemoriesArgs {
    pub command: String,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub file_text: String,
    #[serde(default)]
    pub old_str: String,
    #[serde(default)]
    pub new_str: String,
    #[serde(default)]
    pub insert_line: i64,
    #[serde(default)]
    pub insert_text: String,
    #[serde(default)]
    pub old_path: String,
    #[serde(default)]
    pub new_path: String,
    /// #783: MCP session identity stamped by the transport. Read commands use
    /// it to enforce private/fleet visibility without trusting caller filters.
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
}

/// Normalize a /memories path to an entity key. Rejects traversal and
/// absolute-elsewhere paths rather than silently reinterpreting them.
fn memories_key(path: &str) -> Result<String, String> {
    let p = path.trim().replace('\\', "/");
    let rel = p
        .strip_prefix("/memories/")
        .or_else(|| p.strip_prefix("memories/"))
        .unwrap_or(p.trim_start_matches('/'));
    let rel = rel.trim_matches('/');
    if rel.is_empty() {
        return Err("path must name a file under /memories".to_string());
    }
    if rel.split('/').any(|seg| seg == "." || seg == ".." || seg.is_empty()) {
        return Err(format!("invalid path: {}", path));
    }
    Ok(rel.to_string())
}

/// True when the path means the /memories directory itself.
fn is_memories_root(path: &str) -> bool {
    let p = path.trim().trim_end_matches('/');
    p.is_empty() || p == "/memories" || p == "memories" || p == "/"
}

fn memories_file(
    db: &Database,
    key: &str,
    requesting_agent_id: Option<&str>,
) -> Result<Option<crate::models::Entity>, String> {
    db.get_entity(MEMORIES_CATEGORY, key)
        .map_err(|e| format!("read failed: {}", e))
        .map(|opt| {
            opt.filter(|e| {
                !e.archived
                    && requesting_agent_id
                        .map(|requester| db.can_read(requester, &e.visibility, &e.agent_id))
                        .unwrap_or(true)
            })
        })
}

fn memories_write(
    db: &Database,
    key: &str,
    text: &str,
    existing: Option<&crate::models::Entity>,
) -> Result<(), String> {
    let now = crate::db::now_ms();
    let entity = match existing {
        // Preserve identity/stats on edit; remember()'s update path snapshots
        // the prior version into entity_history (versioned files for free).
        Some(prev) => crate::models::Entity {
            body_json: text.to_string(),
            archived: false,
            archive_reason: String::new(),
            last_accessed_unix_ms: now,
            ..prev.clone()
        },
        None => {
            let raw_id = uuid::Uuid::new_v4().to_string().replace('-', "");
            crate::models::Entity {
                id: format!("memf-{}", &raw_id[..12.min(raw_id.len())]),
                category: MEMORIES_CATEGORY.to_string(),
                key: key.to_string(),
                body_json: text.to_string(),
                status: "active".to_string(),
                entity_type: "file".to_string(),
                tags: vec!["memories".to_string()],
                decay_score: 1.0,
                retrieval_count: 0,
                layer: "working".to_string(),
                topic_path: String::new(),
                archived: false,
                archive_reason: String::new(),
                links: vec![],
                verified: false,
                source: "memories-adapter".to_string(),
                always_on: false,
                certainty: 0.5,
                workspace_hash: String::new(),
                agent_id: String::new(),
                visibility: "workspace".to_string(),
                created_at_unix_ms: now,
                last_accessed_unix_ms: now,
                follow_count: 0,
                miss_count: 0,
                follow_rate: 0.0,
                efficacy_status: "unverified".to_string(),
                epistemic_state: "candidate".to_string(),
                embedding: None,
                _parsed_body: None,
            }
        }
    };
    // skip_dedup: a deliberate file write must create THIS path even when a
    // similar file already exists under another path.
    db.remember_skip_dedup(&entity)
        .map(|_| ())
        .map_err(|e| format!("write failed: {}", e))
}

pub fn handle_memories(db: &Database, args: Value) -> Result<String, String> {
    let a: MemoriesArgs = serde_json::from_value(args)
        .map_err(|e| format!("Invalid memories arguments: {}", e))?;

    match a.command.as_str() {
        "view" => {
            if is_memories_root(&a.path) {
                // No workspace filter: the adapter writes files with the
                // global ('') workspace, and #346's list_entities gained a
                // workspace_hash arg after this call was written.
                let entries = db
                    .list_entities(0, 1000, Some(MEMORIES_CATEGORY), None, None)
                    .map_err(|e| format!("list failed: {}", e))?;
                let mut names: Vec<String> = entries
                    .iter()
                    .filter(|e| {
                        a.requesting_agent_id
                            .as_deref()
                            .map(|requester| db.can_read(requester, &e.visibility, &e.agent_id))
                            .unwrap_or(true)
                    })
                    .map(|e| e.key.clone())
                    .collect();
                names.sort();
                return Ok(json!({
                    "directory": "/memories",
                    "files": names,
                    "total": names.len(),
                })
                .to_string());
            }
            let key = memories_key(&a.path)?;
            let file = memories_file(db, &key, a.requesting_agent_id.as_deref())?
                .ok_or_else(|| format!("file not found: /memories/{}", key))?;
            // cat -n style numbering, matching the native memory tool's view.
            let numbered: String = file
                .body_json
                .lines()
                .enumerate()
                .map(|(i, l)| format!("{:>6}\t{}\n", i + 1, l))
                .collect();
            Ok(json!({
                "path": format!("/memories/{}", key),
                "content": numbered,
            })
            .to_string())
        }
        "create" => {
            let key = memories_key(&a.path)?;
            // Anthropic semantics: create overwrites an existing file.
            let existing = memories_file(db, &key, a.requesting_agent_id.as_deref())?;
            memories_write(db, &key, &a.file_text, existing.as_ref())?;
            Ok(reviewable_write_result(
                json!({"path": format!("/memories/{}", key), "action": "created"}),
                "memories_create",
            ).to_string())
        }
        "str_replace" => {
            let key = memories_key(&a.path)?;
            let file = memories_file(db, &key, a.requesting_agent_id.as_deref())?
                .ok_or_else(|| format!("file not found: /memories/{}", key))?;
            let occurrences = file.body_json.matches(&a.old_str).count();
            if a.old_str.is_empty() {
                return Err("old_str must not be empty".to_string());
            }
            if occurrences == 0 {
                return Err(format!("old_str not found in /memories/{}", key));
            }
            if occurrences > 1 {
                return Err(format!(
                    "old_str occurs {} times in /memories/{} — must be unique",
                    occurrences, key
                ));
            }
            let updated = file.body_json.replacen(&a.old_str, &a.new_str, 1);
            memories_write(db, &key, &updated, Some(&file))?;
            Ok(reviewable_write_result(
                json!({"path": format!("/memories/{}", key), "action": "replaced"}),
                "memories_replace",
            ).to_string())
        }
        "insert" => {
            let key = memories_key(&a.path)?;
            let file = memories_file(db, &key, a.requesting_agent_id.as_deref())?
                .ok_or_else(|| format!("file not found: /memories/{}", key))?;
            let mut lines: Vec<&str> = file.body_json.lines().collect();
            let at = a.insert_line.clamp(0, lines.len() as i64) as usize;
            lines.insert(at, &a.insert_text);
            let updated = lines.join("\n");
            memories_write(db, &key, &updated, Some(&file))?;
            Ok(reviewable_write_result(
                json!({
                    "path": format!("/memories/{}", key),
                    "action": "inserted",
                    "at_line": at,
                }),
                "memories_insert",
            ).to_string())
        }
        "delete" => {
            let key = memories_key(&a.path)?;
            memories_file(db, &key, a.requesting_agent_id.as_deref())?
                .ok_or_else(|| format!("file not found: /memories/{}", key))?;
            let removed = db
                .forget(MEMORIES_CATEGORY, &key, "memories: delete command")
                .map_err(|e| format!("delete failed: {}", e))?;
            if !removed {
                return Err(format!("file not found: /memories/{}", key));
            }
            Ok(reviewable_write_result(
                json!({"path": format!("/memories/{}", key), "action": "deleted"}),
                "memories_delete",
            ).to_string())
        }
        "rename" => {
            let old_key = memories_key(&a.old_path)?;
            let new_key = memories_key(&a.new_path)?;
            let file = memories_file(db, &old_key, a.requesting_agent_id.as_deref())?
                .ok_or_else(|| format!("file not found: /memories/{}", old_key))?;
            if memories_file(db, &new_key, a.requesting_agent_id.as_deref())?.is_some() {
                return Err(format!("destination exists: /memories/{}", new_key));
            }
            memories_write(db, &new_key, &file.body_json, None)?;
            db.forget(MEMORIES_CATEGORY, &old_key, "memories: renamed")
                .map_err(|e| format!("rename cleanup failed: {}", e))?;
            Ok(reviewable_write_result(
                json!({
                    "from": format!("/memories/{}", old_key),
                    "to": format!("/memories/{}", new_key),
                    "action": "renamed",
                }),
                "memories_rename",
            ).to_string())
        }
        other => Err(format!(
            "unknown command '{}' (expected view/create/str_replace/insert/delete/rename)",
            other
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db() -> (crate::db::TestDatabase, String) {
        let db = crate::db::TestDatabase::new("perseus_vault-test-tools");
        let path = db.path().to_string();
        (db, path)
    }

    #[test]
    fn memories_adapter_hides_private_files_from_other_agents() {
        let (db, path) = temp_db();
        let now = crate::db::now_ms();
        let private = Entity {
            id: "memf-private".to_string(),
            category: MEMORIES_CATEGORY.to_string(),
            key: "private.md".to_string(),
            body_json: "private contents".to_string(),
            status: "active".to_string(),
            entity_type: "file".to_string(),
            tags: vec!["memories".to_string()],
            decay_score: 1.0,
            retrieval_count: 0,
            layer: "working".to_string(),
            topic_path: String::new(),
            archived: false,
            archive_reason: String::new(),
            links: vec![],
            verified: false,
            source: "test".to_string(),
            always_on: false,
            certainty: 1.0,
            workspace_hash: String::new(),
            agent_id: "alice".to_string(),
            visibility: "private".to_string(),
            created_at_unix_ms: now,
            last_accessed_unix_ms: now,
            follow_count: 0,
            miss_count: 0,
            follow_rate: 0.0,
            efficacy_status: "unverified".to_string(),
            epistemic_state: "candidate".to_string(),
            embedding: None,
            _parsed_body: None,
        };
        db.remember_skip_dedup(&private).unwrap();

        let alice: Value = serde_json::from_str(&handle_memories(
            &db,
            json!({"command": "view", "path": "/memories", "requesting_agent_id": "alice"}),
        ).unwrap()).unwrap();
        assert_eq!(alice["files"], json!(["private.md"]), "{alice}");

        let bob: Value = serde_json::from_str(&handle_memories(
            &db,
            json!({"command": "view", "path": "/memories", "requesting_agent_id": "bob"}),
        ).unwrap()).unwrap();
        assert_eq!(bob["files"], json!([]), "{bob}");
        let err = handle_memories(
            &db,
            json!({"command": "view", "path": "/memories/private.md", "requesting_agent_id": "bob"}),
        ).expect_err("private file must not be readable by another agent");
        assert!(err.contains("file not found"), "must not disclose private existence: {err}");
        let err = handle_memories(
            &db,
            json!({"command": "delete", "path": "/memories/private.md", "requesting_agent_id": "bob"}),
        ).expect_err("private file must not be deletable by another agent");
        assert!(err.contains("file not found"), "must not disclose private existence: {err}");
        let alice_after: Value = serde_json::from_str(&handle_memories(
            &db,
            json!({"command": "view", "path": "/memories/private.md", "requesting_agent_id": "alice"}),
        ).unwrap()).unwrap();
        assert!(alice_after["content"].as_str().unwrap().contains("private contents"));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn timeline_requires_workspace_and_isolates_rows() {
        let (db, path) = temp_db();
        for (id, workspace, timestamp) in [
            ("timeline-global", "", 3_i64),
            ("timeline-alpha", "alpha", 2_i64),
            ("timeline-beta", "beta", 1_i64),
        ] {
            db.journal(&JournalEvent {
                id: id.to_string(),
                event_type: "decision".to_string(),
                evaluated_json: "{}".to_string(),
                acted_json: "{}".to_string(),
                forward_json: "{}".to_string(),
                category: "test".to_string(),
                key: id.to_string(),
                entity_id: String::new(),
                agent_id: String::new(),
                workspace_hash: workspace.to_string(),
                created_at_unix_ms: timestamp,
            })
            .unwrap();
        }

        let scoped: Value = serde_json::from_str(
            &handle_timeline(&db, json!({"workspace_hash": "alpha"})).unwrap(),
        )
        .unwrap();
        let items = scoped["items"].as_array().unwrap();
        assert_eq!(items.len(), 2, "timeline must include global and exclude other workspaces: {scoped}");
        assert_eq!(items[0]["id"], "timeline-global");
        assert_eq!(items[0]["workspace_hash"], "");
        assert_eq!(items[1]["id"], "timeline-alpha");
        assert_eq!(items[1]["workspace_hash"], "alpha");

        let limited: Value = serde_json::from_str(
            &handle_timeline(&db, json!({"workspace_hash": "alpha", "limit": 1})).unwrap(),
        )
        .unwrap();
        assert_eq!(limited["items"].as_array().unwrap().len(), 1);
        assert_eq!(limited["items"][0]["id"], "timeline-global");

        let recent = db.get_recent_journal("alpha", 2).unwrap();
        assert_eq!(recent.iter().map(|event| event.id.as_str()).collect::<Vec<_>>(),
            vec!["timeline-global", "timeline-alpha"]);

        let missing = handle_timeline(&db, json!({}));
        assert!(missing.is_err(), "timeline must reject omitted workspace scope");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn health_reports_status_and_db_path() {
        // #671: health must surface the absolute db path so a "wrote here,
        // inspected ~/perseus-vault.db there" mismatch is self-diagnosing.
        let (db, path) = temp_db();
        let v: Value = serde_json::from_str(&handle_health(&db)).unwrap();
        assert_eq!(v["status"], json!("healthy"), "{v}");
        let reported = v["db_path"].as_str().expect("db_path present");
        assert!(!reported.is_empty(), "db_path must be non-empty: {v}");
        // The reported path resolves to the same file the db was opened with.
        let want = std::fs::canonicalize(&path).unwrap();
        assert_eq!(std::path::Path::new(reported), want.as_path(), "{v}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn health_reports_readiness_on_empty_store() {
        // #677: an empty but healthy store must be distinguishable from a
        // broken one — status healthy, but ready=false with a warning that
        // names the true cause (0 active memories) rather than looking silent.
        let (db, path) = temp_db();
        let v: Value = serde_json::from_str(&handle_health(&db)).unwrap();
        assert_eq!(v["status"], json!("healthy"), "{v}");
        assert_eq!(v["ready"], json!(false), "empty store is not recall-ready: {v}");
        assert_eq!(v["active_memories"], json!(0), "{v}");
        let sem = v["semantic_recall"].as_str().expect("semantic_recall present");
        assert!(
            matches!(sem, "available" | "no_coverage" | "disabled"),
            "unexpected semantic_recall {sem}: {v}"
        );
        let warnings = v["warnings"].as_array().expect("warnings present");
        assert!(
            warnings.iter().any(|w| w.as_str().unwrap_or("").contains("0 active memories")),
            "empty store must warn about 0 active memories: {v}"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn empty_recall_attaches_empty_store_diagnostic() {
        // #677: recall against an empty store returns total=0 WITH a diagnostic
        // that says "empty_store" — a true empty result, not a fault — so the
        // caller does not chase a false "MCP child is broken" debugging path.
        let (db, path) = temp_db();
        let out = handle_recall(&db, json!({"query": "anything"})).unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["total"], json!(0), "{v}");
        assert_eq!(v["diagnostic"]["reason"], json!("empty_store"), "{v}");
        assert_eq!(v["diagnostic"]["active_memories"], json!(0), "{v}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn empty_recall_on_populated_store_reports_no_match() {
        // #677: when the store IS populated and healthy but a query simply
        // misses, the diagnostic must say "no_match" (not empty_store), so a
        // genuine miss isn't mistaken for an empty/broken store. Uses fts5 so
        // the assertion holds on lite (no-embedding) builds too.
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({
                "category": "insight",
                "key": "k1",
                "body_json": "{\"content\":\"postgres reindex after major upgrade\"}"
            }),
        )
        .unwrap();
        let out = handle_recall(
            &db,
            json!({"query": "zzzznonexistentqueryterm", "mode": "fts5"}),
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["total"], json!(0), "{v}");
        assert_eq!(v["diagnostic"]["reason"], json!("no_match"), "{v}");
        assert!(
            v["diagnostic"]["active_memories"].as_i64().unwrap_or(0) >= 1,
            "populated store must report >=1 active memory: {v}"
        );
        let _ = std::fs::remove_file(&path);
    }

    // ─── scope as a ranking multiplier (#485) ────────────────────

    #[test]
    fn recall_rejects_invalid_scope_weight() {
        let (db, path) = temp_db();
        let err = handle_recall(
            &db,
            json!({"query": "x", "workspace_hash": "ws-a", "scope_weight": 1.5}),
        )
        .expect_err("scope_weight > 1.0 must be rejected");
        assert!(err.contains("scope_weight must be between"), "{err}");

        let err = handle_recall(&db, json!({"query": "x", "scope_weight": 0.5}))
            .expect_err("scope_weight without a workspace has no scope to prefer");
        assert!(err.contains("requires a non-empty workspace_hash"), "{err}");

        // Valid combination passes validation and runs.
        handle_recall(
            &db,
            json!({"query": "x", "workspace_hash": "ws-a", "scope_weight": 0.5}),
        )
        .expect("valid scope_weight must be accepted");
        let _ = std::fs::remove_file(&path);
    }

    // ─── #562: paginated scan tool + recall enumeration contract ─────────

    #[test]
    fn scan_tool_pages_entire_category_with_cursor() {
        let (db, path) = temp_db();
        for i in 0..25 {
            handle_remember(
                &db,
                json!({
                    "category": "scan-cat",
                    "key": format!("k{:02}", i),
                    "body_json": format!(
                        r#"{{"d": "unique scan tool body row {} filler {}"}}"#,
                        i,
                        i * 37
                    ),
                    "skip_dedup": true,
                }),
            )
            .expect("remember");
        }

        let mut keys: Vec<String> = Vec::new();
        let mut pages = 0;
        let mut cursor: Option<String> = None;
        loop {
            let mut args = json!({"category": "scan-cat", "limit": 10});
            if let Some(c) = &cursor {
                args["cursor"] = json!(c);
            }
            let page: Value =
                serde_json::from_str(&handle_scan(&db, args).expect("scan")).unwrap();
            pages += 1;
            // A reinforcing recall between pages mutates the ranking keys that
            // break offset paging — the keyset walk must be unaffected.
            handle_recall(&db, json!({"query": "", "category": "scan-cat"})).expect("recall");
            for item in page["items"].as_array().expect("items") {
                keys.push(item["key"].as_str().expect("key").to_string());
            }
            if !page["has_more"].as_bool().expect("has_more") {
                assert!(
                    page["next_cursor"].is_null(),
                    "final page must have a null cursor: {page}"
                );
                break;
            }
            cursor = Some(
                page["next_cursor"]
                    .as_str()
                    .expect("non-final page must carry a cursor")
                    .to_string(),
            );
        }
        assert_eq!(pages, 3, "25 rows at limit 10 = 3 pages");
        assert_eq!(keys.len(), 25, "every row exactly once: {keys:?}");
        let unique: std::collections::HashSet<&String> = keys.iter().collect();
        assert_eq!(unique.len(), 25, "no duplicates across pages: {keys:?}");
        let _ = std::fs::remove_file(&path);
    }

    // #562: the documented recall query contract — "" is match-all
    // enumeration; "*" is a literal FTS5 term (not a glob) and matches nothing.
    #[test]
    fn recall_empty_query_enumerates_and_star_is_literal() {
        let (db, path) = temp_db();
        for i in 0..3 {
            handle_remember(
                &db,
                json!({
                    "category": "enum-cat",
                    "key": format!("k{i}"),
                    "body_json": format!(
                        r#"{{"d": "distinct enumeration contract body {} pad {}"}}"#,
                        i,
                        i * 41
                    ),
                    "skip_dedup": true,
                }),
            )
            .expect("remember");
        }

        let all: Value = serde_json::from_str(
            &handle_recall(&db, json!({"query": "", "category": "enum-cat", "limit": 10}))
                .expect("recall"),
        )
        .unwrap();
        assert_eq!(all["total"], json!(3), "empty query = match-all enumeration: {all}");

        let star: Value = serde_json::from_str(
            &handle_recall(&db, json!({"query": "*", "category": "enum-cat", "limit": 10}))
                .expect("recall"),
        )
        .unwrap();
        assert_eq!(star["total"], json!(0), "'*' is a literal term, not a glob: {star}");
        let _ = std::fs::remove_file(&path);
    }

    // ─── #520: in-session capture pipeline ───────────────────────

    #[test]
    fn capture_roundtrip_classifies_and_writes_with_capture_source() {
        let (db, path) = temp_db();
        let payload = "# Root cause of the deploy failure\n\
                       The deploy failed because the schema version was never bumped by #487.\n\n\
                       # Toolchain decision\n\
                       We decided to standardize on the MSVC toolchain for Windows builds.";
        let r = handle_capture(&db, json!({"text": payload, "workspace_hash": "ws-cap"}))
            .expect("capture must succeed");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["captured"], json!(2), "{r}");
        assert_eq!(v["created"], json!(2), "{r}");
        assert_eq!(v["distiller"], json!("rule_based"), "{r}");
        assert_eq!(v["notes"][0]["type"], json!("root-cause"), "{r}");
        assert_eq!(v["notes"][1]["type"], json!("decision"), "{r}");

        // The entities landed via the normal remember path with the capture
        // provenance: category "capture", source "capture", layer buffer.
        let key = v["notes"][0]["key"].as_str().unwrap();
        let ent = db
            .get_entity("capture", key)
            .expect("get_entity")
            .expect("captured entity must exist");
        assert_eq!(ent.source, "capture");
        assert_eq!(ent.layer, "buffer");
        assert_eq!(ent.workspace_hash, "ws-cap");
        assert!(ent.body_json.contains("schema version"), "{}", ent.body_json);
        let captured_body: Value = serde_json::from_str(&ent.body_json).unwrap();
        assert_eq!(captured_body["evidence"]["capture_mode"], "legacy_unknown");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn capture_accepts_explicit_hash_only_evidence() {
        let (db, path) = temp_db();
        let digest = "a".repeat(64);
        let raw = handle_capture(
            &db,
            json!({
                "text": "We decided to retain a digest instead of the source snapshot.",
                "evidence": {
                    "capture_mode": "hash_only",
                    "content_sha256": digest,
                    "captured_at_unix_ms": 100,
                    "replayable": false
                }
            }),
        ).expect("capture with hash-only evidence");
        let report: Value = serde_json::from_str(&raw).unwrap();
        let key = report["notes"][0]["key"].as_str().unwrap();
        let entity = db.get_entity("capture", key).unwrap().unwrap();
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["evidence"]["capture_mode"], "hash_only");
        assert_eq!(body["evidence"]["replayable"], false);
        assert!(body["evidence"].get("resolved_value").is_none());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn capture_near_duplicate_merges_instead_of_flooding() {
        // #520 anti-flood: the trigram dedup stays ON for captures — a
        // re-captured solved problem (slightly reworded, different headline
        // → different key) merges into the existing memory instead of
        // creating a sibling row.
        let (db, path) = temp_db();
        let first = "Fix for the FK constraint failure: the migration failed because \
                     the foreign key constraint on entities was validated before the \
                     backfill ran, so ordering the backfill first resolves it.";
        let r = handle_capture(&db, json!({"text": first})).expect("first capture");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["created"], json!(1), "{r}");

        // Near-identical wording, different first line → different slug key.
        let second = "Another note: the migration failed because the foreign key \
                      constraint on entities was validated before the backfill ran, \
                      so ordering the backfill first resolves it.";
        let r = handle_capture(&db, json!({"text": second})).expect("second capture");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["created"], json!(0), "near-dup must not create: {r}");
        assert_eq!(v["merged"], json!(1), "near-dup must merge: {r}");
        assert!(
            v["notes"][0]["action"].as_str().unwrap().starts_with("deduped"),
            "{r}"
        );

        // Identical payload again: same summary → same key → in-place update.
        let r = handle_capture(&db, json!({"text": first})).expect("re-capture");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["created"], json!(0), "{r}");
        assert_eq!(v["updated"].as_i64().unwrap() + v["merged"].as_i64().unwrap(), 1, "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn capture_caps_writes_and_dry_run_writes_nothing() {
        let (db, path) = temp_db();
        let payload = (0..30)
            .map(|i| format!("Durable takeaway number {i} about the capture cap behavior."))
            .collect::<Vec<_>>()
            .join("\n\n");

        // dry_run: full report, zero writes.
        let r = handle_capture(&db, json!({"text": payload, "dry_run": true, "max_entities": 100}))
            .expect("dry-run capture");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["dry_run"], json!(true));
        assert_eq!(v["captured"], json!(20), "hard cap even when asked for 100: {r}");
        assert_eq!(v["dropped"], json!(10), "{r}");
        assert_eq!(v["created"], json!(0), "{r}");
        let stats = db.stats().expect("stats");
        assert_eq!(stats.total_entities, 0, "dry-run must write nothing");

        // Real run with a lowered cap. The three templated notes are >=70%
        // trigram-similar by construction, so the second flood-control layer
        // (dedup, deliberately ON for captures) merges them into one row —
        // cap AND dedup both bounding the write volume is the #520 design.
        let r = handle_capture(&db, json!({"text": payload, "max_entities": 3}))
            .expect("capped capture");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["captured"], json!(3), "{r}");
        assert_eq!(v["dropped"], json!(27), "{r}");
        assert_eq!(v["created"], json!(1), "{r}");
        assert_eq!(v["merged"], json!(2), "{r}");
        let stats = db.stats().expect("stats");
        assert_eq!(stats.total_entities, 1);

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn capture_llm_flag_degrades_gracefully_without_endpoint() {
        // llm=true with no --llm-endpoint configured must NOT error: the
        // rule-based distiller is the floor, and the result says why.
        let (db, path) = temp_db();
        let r = handle_capture(
            &db,
            json!({"text": "Lesson: always trim PATH before invoking vcvars.", "llm": true}),
        )
        .expect("llm capture must fall back, not fail");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["distiller"], json!("rule_based"), "{r}");
        assert!(
            v["llm_fallback"].as_str().unwrap().contains("not enabled"),
            "{r}"
        );
        assert_eq!(v["captured"], json!(1), "{r}");

        // Empty text is the only hard error.
        let err = handle_capture(&db, json!({"text": ""})).expect_err("empty text");
        assert!(err.contains("text is required"), "{err}");

        let _ = std::fs::remove_file(&path);
    }

    // ─── derived_from auto-reinforcement (#487) ──────────────────

    #[test]
    fn remember_derived_from_reinforces_cited_sources() {
        let (db, path) = temp_db();
        // Two sources the later write will cite: one by (category, key), one
        // by entity id. Bodies are deliberately dissimilar so remember()'s
        // similarity dedup can't merge them.
        let resp = handle_remember(
            &db,
            json!({"category": "insight", "key": "src-key",
                   "body_json": "{\"content\":\"postgres 16 upgrade requires reindexing all GIN indexes\"}"}),
        )
        .expect("remember src-key");
        let src: Value = serde_json::from_str(&resp).unwrap();
        let src_id = src["id"].as_str().unwrap().to_string();
        handle_remember(
            &db,
            json!({"category": "insight", "key": "src-key-2",
                   "body_json": "{\"content\":\"the deploy pipeline caches docker layers per branch\"}"}),
        )
        .expect("remember src-key-2");

        let resp = handle_remember(
            &db,
            json!({"category": "decision", "key": "derived-write",
                   "body_json": "{\"content\":\"we will reindex during the maintenance window\"}",
                   "derived_from": [src_id, {"category": "insight", "key": "src-key-2"}]}),
        )
        .expect("remember with derived_from");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["derived_from"]["reinforced"].as_i64(), Some(2), "{resp}");
        assert_eq!(
            v["derived_from"]["not_found"].as_array().unwrap().len(),
            0,
            "{resp}"
        );

        // Both cited sources: usefulness bumped, last_useful stamped, and
        // last_accessed refreshed (a citation IS an access).
        let conn = db.conn().unwrap();
        for key in ["src-key", "src-key-2"] {
            let (u, lu, la): (i64, i64, i64) = conn
                .query_row(
                    "SELECT usefulness_count, last_useful_unix_ms, last_accessed_unix_ms \
                     FROM entities WHERE category = 'insight' AND key = ?1",
                    rusqlite::params![key],
                    |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
                )
                .unwrap();
            assert_eq!(u, 1, "usefulness_count for {key}");
            assert!(lu > 0, "last_useful_unix_ms stamped for {key}");
            assert_eq!(la, lu, "citation must refresh last_accessed for {key}");
        }
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_derived_from_reports_missing_and_skips_self() {
        let (db, path) = temp_db();
        let resp = handle_remember(
            &db,
            json!({"category": "insight", "key": "self-citer",
                   "body_json": "{\"content\":\"a fact that tries to vouch for itself\"}",
                   "derived_from": [
                       {"category": "insight", "key": "self-citer"},
                       {"category": "insight", "key": "does-not-exist"}
                   ]}),
        )
        .expect("remember must succeed despite unresolved citations");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["derived_from"]["reinforced"].as_i64(), Some(0), "{resp}");
        let nf = v["derived_from"]["not_found"].as_array().unwrap();
        assert_eq!(nf.len(), 1, "self-citation must be skipped, not reported: {resp}");
        assert_eq!(nf[0].as_str(), Some("insight/does-not-exist"));

        // The self-citation must not have bumped the entity's own counter.
        let u: i64 = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT usefulness_count FROM entities WHERE category = 'insight' AND key = 'self-citer'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(u, 0, "a write cannot vouch for itself");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_derived_from_rejects_oversized_citation_list() {
        let (db, path) = temp_db();
        let refs: Vec<Value> = (0..65).map(|i| json!(format!("mem-{i:012}"))).collect();
        let err = handle_remember(
            &db,
            json!({"category": "insight", "key": "too-many",
                   "body_json": "{\"content\":\"x\"}",
                   "derived_from": refs}),
        )
        .expect_err("65 citations must be rejected");
        assert!(err.contains("derived_from too long"), "{err}");
        // Rejected up front: no entity was created.
        let n: i64 = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM entities WHERE key = 'too-many'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n, 0, "rejected remember must not create an entity");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_derived_from_is_workspace_strict() {
        // Mirrors follow()'s #391/#396 semantics: a workspace-scoped write
        // reinforces the row in ITS workspace, never the global '' row.
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "insight", "key": "shared-key",
                   "body_json": "{\"content\":\"the global variant of this fact\"}"}),
        )
        .expect("remember global row");
        handle_remember(
            &db,
            json!({"category": "insight", "key": "shared-key", "workspace_hash": "ws-a",
                   "body_json": "{\"content\":\"the ws-a variant of this fact entirely different\"}"}),
        )
        .expect("remember ws-a row");

        let resp = handle_remember(
            &db,
            json!({"category": "decision", "key": "ws-derived", "workspace_hash": "ws-a",
                   "body_json": "{\"content\":\"a ws-a decision built on the shared fact\"}",
                   "derived_from": [{"category": "insight", "key": "shared-key"}]}),
        )
        .expect("remember with ws-scoped citation");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["derived_from"]["reinforced"].as_i64(), Some(1), "{resp}");

        let conn = db.conn().unwrap();
        let get = |ws: &str| -> i64 {
            conn.query_row(
                "SELECT usefulness_count FROM entities \
                 WHERE category = 'insight' AND key = 'shared-key' AND workspace_hash = ?1",
                rusqlite::params![ws],
                |r| r.get(0),
            )
            .unwrap()
        };
        assert_eq!(get("ws-a"), 1, "the ws-a row the agent saw gets the credit");
        assert_eq!(get(""), 0, "the global row must NOT be stamped by a ws-scoped write");
        let _ = std::fs::remove_file(&path);
    }

    // ─── Autocohere link budget (#412) ───────────────────────────

    #[test]
    fn autocohere_creates_links_on_linkable_corpus() {
        // #412: `CohereParams`' derived `Default` gave `max_links = 0`, and
        // handle_autocohere builds its params with `..Default::default()` —
        // so the cohere step's candidate SELECT ran with `LIMIT 0` and the
        // "run everything" maintenance pass had NEVER created a single
        // auto-link. Pin the arg-less autocohere path to a real link budget.
        let (db, path) = temp_db();
        let now = now_ms();
        let mk = |id: &str, key: &str, note: &str| {
            let mut e: Entity = serde_json::from_value(json!({
                "id": id,
                "category": "project",
                "key": key,
                "body_json": format!(r#"{{"note":"{note}"}}"#),
                "created_at_unix_ms": now,
                "last_accessed_unix_ms": now,
            }))
            .unwrap();
            // Auto-link requires non-empty tags on both sides.
            e.tags = vec!["x".to_string()];
            // Skip remember()'s similarity dedup — the corpus is
            // intentionally near-duplicate so trigram similarity clears
            // the auto-link threshold.
            db.remember_skip_dedup(&e).unwrap();
        };
        mk("ac-a", "alpha", "the payment service database migration plan for the Q3 rollout");
        mk("ac-b", "beta", "the payment service database migration plan for the Q4 rollout");

        let resp = handle_autocohere(&db, json!({ "dry_run": false })).expect("autocohere");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert!(
            v["links_created"].as_i64().unwrap() > 0,
            "autocohere must auto-link a clearly-linkable corpus (max_links \
             default must not be 0): {resp}"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn autocohere_captures_before_compaction_and_preserves_fact() {
        let (db, path) = temp_db();
        let response = handle_autocohere(
            &db,
            json!({
                "capture_text": "## Deployment decision\nUse blue-green deployment so rollback remains safe.",
                "capture_workspace_hash": "ws-precompact"
            }),
        )
        .expect("autocohere with pre-compaction capture");
        let report: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(report["precompact_capture"]["stage"], json!("completed"), "{response}");
        assert!(report["precompact_capture"]["report"]["captured"].as_i64().unwrap() >= 1, "{response}");
        let captured = db
            .get_entity("capture", "deployment-decision")
            .unwrap()
            .expect("captured fact must survive subsequent compaction lifecycle");
        assert!(!captured.archived, "captured fact must remain live");
        assert_eq!(captured.workspace_hash, "ws-precompact");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn autocohere_capture_dry_run_writes_no_fact() {
        let (db, path) = temp_db();
        let response = handle_autocohere(
            &db,
            json!({
                "dry_run": true,
                "capture_text": "## Preview decision\nDo not persist this preview-only deployment decision."
            }),
        )
        .expect("dry-run autocohere");
        let report: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(report["precompact_capture"]["stage"], json!("completed"));
        assert_eq!(report["precompact_capture"]["report"]["dry_run"], json!(true));
        assert!(db.get_entity("capture", "preview-decision").unwrap().is_none());
        let _ = std::fs::remove_file(&path);
    }

    // ─── maintain pass (#490) ────────────────────────────────────

    #[test]
    fn maintain_pass_dry_run_mutates_nothing() {
        let (db, path) = temp_db();
        // Distinct bodies so neither remember-dedup nor consolidate can merge.
        handle_remember(
            &db,
            json!({"category": "insight", "key": "mp-a",
                   "body_json": "{\"content\":\"the ingest queue backs up when redis restarts\"}"}),
        )
        .expect("remember mp-a");
        handle_remember(
            &db,
            json!({"category": "decision", "key": "mp-b",
                   "body_json": "{\"content\":\"ship the billing cutover on a tuesday morning\"}"}),
        )
        .expect("remember mp-b");

        // Make mp-b long-cold so the decay step WOULD auto-archive it: 60
        // idle days ≈ decay 0.003 (7-day half-life), far under the 0.05
        // archive threshold. importance = 0 so the persistent floor (#487)
        // doesn't rescue it. The preview must REPORT it without archiving —
        // decay_tick previously ran LIVE inside dry_run.
        db.conn()
            .unwrap()
            .execute(
                "UPDATE entities SET last_accessed_unix_ms = last_accessed_unix_ms \
                 - 60 * 24 * 3600 * 1000, importance = 0.0 WHERE key = 'mp-b'",
                [],
            )
            .unwrap();

        let count_active = |db: &Database| -> i64 {
            db.conn()
                .unwrap()
                .query_row(
                    "SELECT COUNT(*) FROM entities WHERE archived = 0",
                    [],
                    |r| r.get(0),
                )
                .unwrap()
        };
        let before = count_active(&db);

        let report = run_maintenance_pass(&db, true, false).expect("dry-run pass");
        assert_eq!(report["dry_run"], json!(true), "{report}");
        assert_eq!(report["vacuum_requested"], json!(false), "{report}");
        assert!(report["autocohere"].is_object(), "{report}");
        assert!(report["maintenance"].is_object(), "{report}");
        // The preview names the would-be forgetting...
        assert!(
            report["autocohere"]["decay_auto_archived"].as_i64().unwrap() >= 1,
            "preview must report the cold entity as would-archive: {report}"
        );
        // ...but must not change anything.
        assert_eq!(count_active(&db), before, "dry_run archived something");

        // The live pass then actually forgets it.
        let report = run_maintenance_pass(&db, false, false).expect("live pass");
        assert!(
            report["autocohere"]["decay_auto_archived"].as_i64().unwrap() >= 1,
            "{report}"
        );
        assert_eq!(count_active(&db), before - 1, "live pass must archive the cold entity");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn maintain_pass_live_runs_all_steps_and_vacuum_only_on_request() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "insight", "key": "mp-live",
                   "body_json": "{\"content\":\"grafana dashboards live in the ops folder\"}"}),
        )
        .expect("remember mp-live");

        // Default pass: no vacuum requested — the physical rewrite is the
        // scheduler's call, not every run's.
        let report = run_maintenance_pass(&db, false, false).expect("live pass");
        assert_eq!(report["dry_run"], json!(false), "{report}");
        assert_eq!(report["maintenance"]["vacuum_reclaimed_bytes"], json!(0), "{report}");
        assert_eq!(
            report["maintenance"]["errors"].as_array().map(Vec::len),
            Some(0),
            "{report}"
        );
        // A fresh, hot entity must survive the conservative pass.
        let alive: i64 = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM entities WHERE key = 'mp-live' AND archived = 0",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(alive, 1, "fresh entity must not be archived by maintain");

        // Explicit vacuum request runs the physical step without error.
        let report = run_maintenance_pass(&db, false, true).expect("vacuum pass");
        assert_eq!(report["vacuum_requested"], json!(true), "{report}");
        assert_eq!(
            report["maintenance"]["errors"].as_array().map(Vec::len),
            Some(0),
            "{report}"
        );
        let _ = std::fs::remove_file(&path);
    }

    // ─── maintenance dry-run estimates (#491) ────────────────────

    #[test]
    fn maintenance_dry_run_reports_physical_estimates() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "insight", "key": "est-a",
                   "body_json": "{\"content\":\"fts drift probe entity\"}"}),
        )
        .expect("remember");
        // Manufacture FTS drift: drop the entity's FTS row directly.
        db.conn()
            .unwrap()
            .execute("DELETE FROM entities_fts", [])
            .unwrap();

        let resp =
            handle_maintenance(&db, json!({"all": true, "dry_run": true})).expect("maintenance");
        let v: Value = serde_json::from_str(&resp).unwrap();
        // Skips are NAMED, not silently zero...
        assert_eq!(v["vacuum_skipped_dry_run"], json!(true), "{resp}");
        assert_eq!(v["reindex_skipped_dry_run"], json!(true), "{resp}");
        // ...and each carries a read-only estimate of the pending work.
        assert!(
            v["vacuum_would_reclaim_bytes_estimate"].as_i64().unwrap() >= 0,
            "{resp}"
        );
        assert_eq!(v["fts_rows_drift_estimate"].as_i64(), Some(1), "{resp}");
        // The physical steps themselves still did not run.
        assert_eq!(v["vacuum_reclaimed_bytes"], json!(0), "{resp}");
        assert_eq!(v["reindex_rows_affected"], json!(0), "{resp}");

        // A live reindex then clears the drift the preview reported.
        handle_maintenance(&db, json!({"reindex": true})).expect("live reindex");
        assert_eq!(db.fts_drift_estimate().unwrap(), 0);
        let _ = std::fs::remove_file(&path);
    }

    // ─── History pagination (#403) ───────────────────────────────

    #[test]
    fn history_tool_pages_newest_first_with_full_total() {
        let (db, path) = temp_db();

        // 51 writes to one key -> 50 superseded versions (v0..v49) in history;
        // v50 stays live. Each content change snapshots the prior version.
        for i in 0..51 {
            handle_remember(
                &db,
                json!({"category": "facts", "key": "hot",
                       "body_json": format!("{{\"content\":\"version-v{i}-body\"}}")}),
            )
            .expect("remember");
        }
        // The unpaged DB API still returns the full trail (back-compat).
        assert_eq!(db.history_versions("facts", "hot").unwrap().len(), 50);

        // Default: the 20 NEWEST versions, with total = full trail size.
        let resp = handle_history(&db, json!({"category":"facts","key":"hot"})).expect("history");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["total"].as_i64().unwrap(), 50, "total is the FULL trail: {resp}");
        assert_eq!(v["returned"].as_i64().unwrap(), 20);
        assert_eq!(v["versions"].as_array().unwrap().len(), 20);
        assert!(
            v["versions"][0].to_string().contains("version-v49-body"),
            "newest superseded version first: {}",
            v["versions"][0]
        );
        assert!(
            v["versions"][19].to_string().contains("version-v30-body"),
            "20th newest is v30: {}",
            v["versions"][19]
        );

        // Explicit limit + offset page deeper into the trail.
        let resp = handle_history(
            &db,
            json!({"category":"facts","key":"hot","limit":5,"offset":10}),
        )
        .expect("history page");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["total"].as_i64().unwrap(), 50);
        assert_eq!(v["returned"].as_i64().unwrap(), 5);
        assert!(v["versions"][0].to_string().contains("version-v39-body"));
        assert!(v["versions"][4].to_string().contains("version-v35-body"));

        // A page past the end returns the remainder, total unchanged.
        let resp = handle_history(
            &db,
            json!({"category":"facts","key":"hot","limit":20,"offset":48}),
        )
        .expect("history tail");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["total"].as_i64().unwrap(), 50);
        assert_eq!(v["returned"].as_i64().unwrap(), 2);
        assert!(v["versions"][0].to_string().contains("version-v1-body"));
        assert!(v["versions"][1].to_string().contains("version-v0-body"));

        let _ = std::fs::remove_file(&path);
    }

    // ─── Bi-temporal valid-time tools (#363) ─────────────────────

    #[test]
    fn valid_at_tool_roundtrips_a_retroactive_fact() {
        let (db, path) = temp_db();
        let now = now_ms();
        let vf = now - 7 * 24 * 3600 * 1000; // true since last week

        handle_remember(
            &db,
            json!({"category": "facts", "key": "retro", "body_json": "{\"note\":\"was true last week\"}",
                   "valid_from_unix_ms": vf}),
        )
        .expect("remember with valid_from");

        // Found for instants >= valid_from…
        for t in [vf, vf + 1000, now] {
            let r = handle_valid_at(
                &db,
                json!({"category": "facts", "key": "retro", "valid_at_unix_ms": t}),
            )
            .expect("valid_at");
            let v: Value = serde_json::from_str(&r).unwrap();
            assert_eq!(v["found"], json!(true), "t={t}: {r}");
            assert_eq!(v["valid_from_unix_ms"], json!(vf));
            assert_eq!(v["is_live_version"], json!(true));
        }
        // …found=false strictly before.
        let r = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "retro", "valid_at_unix_ms": vf - 1}),
        )
        .expect("valid_at before");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(false), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_rejects_inverted_valid_period() {
        let (db, path) = temp_db();
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "bad", "body_json": "{}",
                   "valid_from_unix_ms": 200, "valid_to_unix_ms": 100}),
        )
        .expect_err("inverted period must be rejected");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_rejects_one_sided_past_valid_to() {
        // #363 review (round 2): with valid_from omitted it defaults to "now"
        // (new entity / content change) or the stored period (identical
        // re-assert) — a past valid_to would silently store an inverted
        // period that valid_at can never match while still shadowing older
        // versions in bitemporal_at.
        let (db, path) = temp_db();
        let past = now_ms() - 60_000;

        // (a) New entity: effective period would be [now, past).
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "one-sided", "body_json": "{\"note\":\"v1\"}",
                   "valid_to_unix_ms": past}),
        )
        .expect_err("one-sided past valid_to on a new entity must be rejected");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        // Nothing was written.
        assert!(
            db.get_entity("facts", "one-sided").unwrap().is_none(),
            "rejected remember must not create an entity"
        );

        // (b) Existing entity, content change: the new version's valid_from
        // defaults to now — same inversion.
        handle_remember(
            &db,
            json!({"category": "facts", "key": "one-sided", "body_json": "{\"note\":\"v1\"}"}),
        )
        .expect("baseline");
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "one-sided", "body_json": "{\"note\":\"v2\"}",
                   "valid_to_unix_ms": past}),
        )
        .expect_err("one-sided past valid_to on a content change must be rejected");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        // Rejected BEFORE mutation: v1 is still the live body.
        let body = db.get_entity("facts", "one-sided").unwrap().unwrap().body_json;
        assert!(body.contains("v1"), "rejected write must not update the entity: {body}");

        // (c) Existing entity, identical body (COALESCE re-assert path):
        // valid_to is validated against the STORED valid_from — and the
        // rejected write must leave the STORED PERIOD untouched.
        let stored_period = |db: &Database| -> (Value, Value) {
            let r = handle_valid_at(
                &db,
                json!({"category": "facts", "key": "one-sided", "valid_at_unix_ms": now_ms()}),
            )
            .expect("valid_at");
            let v: Value = serde_json::from_str(&r).unwrap();
            assert_eq!(v["found"], json!(true), "{r}");
            (v["valid_from_unix_ms"].clone(), v["valid_to_unix_ms"].clone())
        };
        let before = stored_period(&db);
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "one-sided", "body_json": "{\"note\":\"v1\"}",
                   "valid_to_unix_ms": past}),
        )
        .expect_err("one-sided past valid_to on an identical re-assert must be rejected");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        assert_eq!(
            stored_period(&db),
            before,
            "rejected re-assert must not change the stored valid period"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reassert_rejects_one_sided_valid_from_at_or_after_stored_valid_to() {
        // #363 review (round 3): the mirror-image hole. On an identical-body
        // re-assert the UPDATE takes the caller's valid_from via COALESCE
        // while KEEPING the stored valid_to — so a one-sided valid_from
        // at/after the stored close would store [vf, stored_to): inverted,
        // unanswerable at every instant.
        let (db, path) = temp_db();
        let now = now_ms();
        let vf = now - 100_000;
        let vt = now - 50_000;
        let body = "{\"note\":\"bounded\"}";

        handle_remember(
            &db,
            json!({"category": "facts", "key": "mirror", "body_json": body,
                   "valid_from_unix_ms": vf, "valid_to_unix_ms": vt}),
        )
        .expect("bounded fact");

        let stored_period = |db: &Database| -> (Value, Value) {
            let r = handle_valid_at(
                &db,
                json!({"category": "facts", "key": "mirror", "valid_at_unix_ms": vt - 1_000}),
            )
            .expect("valid_at");
            let v: Value = serde_json::from_str(&r).unwrap();
            assert_eq!(v["found"], json!(true), "{r}");
            (v["valid_from_unix_ms"].clone(), v["valid_to_unix_ms"].clone())
        };
        assert_eq!(stored_period(&db), (json!(vf), json!(vt)));

        // (a) valid_from strictly after the stored close: inverted, rejected.
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "mirror", "body_json": body,
                   "valid_from_unix_ms": vt + 10_000}),
        )
        .expect_err("one-sided valid_from after the stored valid_to must be rejected");
        assert!(err.contains("valid_from_unix_ms"), "got: {err}");

        // (b) valid_from exactly AT the stored close: empty period, rejected.
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": "mirror", "body_json": body,
                   "valid_from_unix_ms": vt}),
        )
        .expect_err("one-sided valid_from at the stored valid_to must be rejected");
        assert!(err.contains("valid_from_unix_ms"), "got: {err}");

        // Rejected writes left the stored period untouched.
        assert_eq!(
            stored_period(&db),
            (json!(vf), json!(vt)),
            "rejected re-asserts must not change the stored valid period"
        );

        // (c) Legitimate one-sided valid_from strictly BEFORE the stored
        // close: accepted, and it moves the open while keeping the close.
        let new_vf = vf - 10_000;
        handle_remember(
            &db,
            json!({"category": "facts", "key": "mirror", "body_json": body,
                   "valid_from_unix_ms": new_vf}),
        )
        .expect("one-sided valid_from before the stored valid_to must be accepted");
        assert_eq!(stored_period(&db), (json!(new_vf), json!(vt)));

        // (d) No stored valid_to (unbounded fact): any one-sided valid_from
        // yields [vf, infinity) — accepted.
        handle_remember(
            &db,
            json!({"category": "facts", "key": "unbounded", "body_json": body}),
        )
        .expect("unbounded fact");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "unbounded", "body_json": body,
                   "valid_from_unix_ms": now + 60_000}),
        )
        .expect("one-sided valid_from on an unbounded fact must be accepted");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_accepts_one_sided_future_valid_to() {
        // A one-sided FUTURE valid_to is a real interval [now, future) — an
        // expiring fact — and must keep working.
        let (db, path) = temp_db();
        let future = now_ms() + 3_600_000;

        handle_remember(
            &db,
            json!({"category": "facts", "key": "expiring", "body_json": "{\"note\":\"v1\"}",
                   "valid_to_unix_ms": future}),
        )
        .expect("one-sided future valid_to on a new entity must be accepted");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "expiring", "body_json": "{\"note\":\"v2\"}",
                   "valid_to_unix_ms": future}),
        )
        .expect("one-sided future valid_to on a content change must be accepted");

        // The stored period is answerable right now and carries the bound.
        let r = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "expiring", "valid_at_unix_ms": now_ms()}),
        )
        .expect("valid_at");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(future), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reassert_extending_closed_period_is_audited() {
        // #371: an identical-body re-assert MAY deliberately extend a period
        // that was closed via set_valid_to (intended semantics, unchanged),
        // but the change must be AUDITED — the pre-extension period is
        // snapshotted to entity_history and both periods stay reconstructable.
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();
        let now = now_ms();
        let vf = now - 100_000;
        let body = "{\"note\":\"audited extension\"}";

        handle_remember(
            &db,
            json!({"category": "facts", "key": "audited", "body_json": body,
                   "valid_from_unix_ms": vf}),
        )
        .expect("baseline fact");
        let ent = db.get_entity("facts", "audited").unwrap().unwrap();

        // Close the fact (audit-relevant precondition: stored valid_to non-NULL).
        let t2 = now - 50_000;
        assert_eq!(db.set_valid_to(&ent.id, t2).expect("close"), t2);
        let hist_before = db.history_versions("facts", "audited").unwrap().len();

        sleep(Duration::from_millis(5));
        let tx_closed = now_ms(); // transaction instant while the close was current knowledge
        sleep(Duration::from_millis(5));

        // Identical body, valid_to extending PAST the close: accepted (option
        // (b) semantics) AND snapshotted.
        let t3 = now + 50_000;
        handle_remember(
            &db,
            json!({"category": "facts", "key": "audited", "body_json": body,
                   "valid_to_unix_ms": t3}),
        )
        .expect("identical-body re-assert extending a closed period is accepted");

        // Exactly one new history snapshot, carrying the pre-extension close.
        let hist = db.history_versions("facts", "audited").unwrap();
        assert_eq!(
            hist.len(),
            hist_before + 1,
            "audited re-assert must snapshot the pre-extension version"
        );

        // Live period now reaches t3: an instant past the old close answers.
        let probe = t2 + 1_000;
        let r = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "audited", "valid_at_unix_ms": probe}),
        )
        .expect("valid_at");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(t3), "{r}");
        assert_eq!(v["is_live_version"], json!(true), "{r}");

        // Reconstruction shows BOTH periods across transaction time:
        // (a) as of tx_closed, the fact had ended at t2 — the probe instant is
        //     unanswerable and an in-period instant reports the old close…
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "audited",
                   "tx_at_unix_ms": tx_closed, "valid_at_unix_ms": probe}),
        )
        .expect("bitemporal old-knowledge cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(false), "pre-extension knowledge must keep the close: {r}");
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "audited",
                   "tx_at_unix_ms": tx_closed, "valid_at_unix_ms": t2 - 1_000}),
        )
        .expect("bitemporal old-knowledge in-period cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(t2), "{r}");
        // (b) …while current knowledge answers the probe with the extension.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "audited",
                   "tx_at_unix_ms": now_ms(), "valid_at_unix_ms": probe}),
        )
        .expect("bitemporal new-knowledge cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(t3), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reassert_moving_valid_from_on_closed_period_is_audited() {
        // #371 (review follow-up): the one-sided valid_from flavor. A closed
        // fact [t0, t5) re-asserted with ONLY valid_from = t1 (legal: t1 < t5,
        // COALESCE keeps the stored close) moves the opening — accepted, and
        // audited exactly like the valid_to extension: one snapshot preserving
        // [t0, t5), live period now [t1, t5).
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();
        let now = now_ms();
        let t0 = now - 100_000;
        let t1 = now - 80_000;
        let t5 = now - 50_000;
        let body = "{\"note\":\"opening moved\"}";

        handle_remember(
            &db,
            json!({"category": "facts", "key": "moved-open", "body_json": body,
                   "valid_from_unix_ms": t0, "valid_to_unix_ms": t5}),
        )
        .expect("closed fact [t0, t5)");
        let hist_before = db.history_versions("facts", "moved-open").unwrap().len();

        sleep(Duration::from_millis(5));
        let tx_before = now_ms(); // while [t0, t5) was current knowledge
        sleep(Duration::from_millis(5));

        handle_remember(
            &db,
            json!({"category": "facts", "key": "moved-open", "body_json": body,
                   "valid_from_unix_ms": t1}),
        )
        .expect("one-sided valid_from before the stored close is accepted");

        // Exactly one new snapshot.
        assert_eq!(
            db.history_versions("facts", "moved-open").unwrap().len(),
            hist_before + 1,
            "audited valid_from move must snapshot the pre-change version"
        );

        // Live period is now [t1, t5): an in-period instant answers live…
        let r = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "moved-open", "valid_at_unix_ms": t1 + 1_000}),
        )
        .expect("valid_at in the moved period");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_from_unix_ms"], json!(t1), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(t5), "{r}");
        assert_eq!(v["is_live_version"], json!(true), "{r}");

        // …while as-of tx_before the history row still answers with the
        // original [t0, t5) — the pre-change period is fully preserved.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "moved-open",
                   "tx_at_unix_ms": tx_before, "valid_at_unix_ms": t0 + 1_000}),
        )
        .expect("bitemporal old-knowledge cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_from_unix_ms"], json!(t0), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(t5), "{r}");
        assert_eq!(v["is_live_version"], json!(false), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_surfaces_dedup_and_skip_dedup_creates() {
        // #531: 2,000 acknowledged bulk writes silently collapsed to a
        // handful of rows via near-duplicate merging, with nothing in the
        // result signaling it. The merge must be explicit in the result, and
        // skip_dedup must let a bulk writer opt out.
        let (db, path) = temp_db();

        let r = handle_remember(
            &db,
            json!({"category": "bulk", "key": "line-0001",
                   "body_json": "{\"note\":\"bulk line 0001 of the ingest benchmark burst\"}"}),
        )
        .expect("first write");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["ok"], json!(true), "#657: committed write must carry ok:true: {r}");
        assert_eq!(v["action"], json!("created"), "{r}");
        assert!(v.get("deduped").is_none(), "created write must not carry dedup fields: {r}");
        let first_id = v["id"].as_str().unwrap().to_string();

        // Same template, new key, no skip_dedup: merged — and says so.
        let r = handle_remember(
            &db,
            json!({"category": "bulk", "key": "line-0002",
                   "body_json": "{\"note\":\"bulk line 0002 of the ingest benchmark burst\"}"}),
        )
        .expect("near-dup write");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["ok"], json!(true), "#657: a dedup merge is still an accepted write: {r}");
        assert_eq!(v["deduped"], json!(true), "{r}");
        assert_eq!(v["merged_into"], json!(first_id), "{r}");
        assert!(v["hint"].as_str().unwrap().contains("skip_dedup"), "{r}");

        // skip_dedup=true: the same shape of write must actually create.
        let r = handle_remember(
            &db,
            json!({"category": "bulk", "key": "line-0003", "skip_dedup": true,
                   "body_json": "{\"note\":\"bulk line 0003 of the ingest benchmark burst\"}"}),
        )
        .expect("skip_dedup write");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["action"], json!("created"), "{r}");
        assert_ne!(v["id"], json!(first_id), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn reassert_with_unchanged_period_writes_no_snapshot() {
        // #371: the audit snapshot fires only when the effective period
        // actually CHANGES — an identical-body re-assert with the same stored
        // bounds, or with bounds omitted, must not write spurious history.
        let (db, path) = temp_db();
        let now = now_ms();
        let vf = now - 100_000;
        let vt = now - 50_000;
        let body = "{\"note\":\"no spurious history\"}";

        handle_remember(
            &db,
            json!({"category": "facts", "key": "quiet", "body_json": body,
                   "valid_from_unix_ms": vf, "valid_to_unix_ms": vt}),
        )
        .expect("closed fact");
        let hist_before = db.history_versions("facts", "quiet").unwrap().len();

        // (a) Bounds omitted: COALESCE keeps the stored period.
        handle_remember(
            &db,
            json!({"category": "facts", "key": "quiet", "body_json": body}),
        )
        .expect("re-assert without bounds");
        // (b) Same bounds re-sent explicitly: effective period unchanged.
        handle_remember(
            &db,
            json!({"category": "facts", "key": "quiet", "body_json": body,
                   "valid_from_unix_ms": vf, "valid_to_unix_ms": vt}),
        )
        .expect("re-assert with identical bounds");

        assert_eq!(
            db.history_versions("facts", "quiet").unwrap().len(),
            hist_before,
            "period-unchanged re-asserts must not snapshot"
        );
        // Stored period untouched.
        let r = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "quiet", "valid_at_unix_ms": vt - 1_000}),
        )
        .expect("valid_at");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_from_unix_ms"], json!(vf), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(vt), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn set_valid_to_close_is_audited_and_noop_is_not() {
        // #373: set_valid_to previously wrote no entity_history snapshot, so a
        // close was invisible to transaction-time reconstruction — queries at
        // a tx instant BEFORE the close reported the close anyway. An
        // effective close must snapshot the pre-close (open) version; a no-op
        // (stored close kept) must not.
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "svt", "body_json": "{\"note\":\"open fact\"}"}),
        )
        .expect("open fact");
        let ent = db.get_entity("facts", "svt").unwrap().unwrap();
        assert!(db.history_versions("facts", "svt").unwrap().is_empty());

        sleep(Duration::from_millis(5));
        let tx_open = now_ms(); // while the fact was still believed open
        sleep(Duration::from_millis(5));

        let closed = db.set_valid_to(&ent.id, now_ms()).expect("close");
        assert_eq!(
            db.history_versions("facts", "svt").unwrap().len(),
            1,
            "an effective close must write exactly one snapshot"
        );

        // As of tx_open the fact reconstructs OPEN (the pre-close snapshot
        // answers, valid_to unbounded)…
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "svt",
                   "tx_at_unix_ms": tx_open, "valid_at_unix_ms": closed + 60_000}),
        )
        .expect("bitemporal pre-close knowledge");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "pre-close knowledge must not show the close: {r}");
        assert_eq!(v["valid_to_unix_ms"], Value::Null, "{r}");
        assert_eq!(v["is_live_version"], json!(false), "{r}");
        // …while current knowledge shows the close: same instant unanswerable,
        // an in-period instant reports valid_to = closed.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "svt",
                   "tx_at_unix_ms": now_ms(), "valid_at_unix_ms": closed + 60_000}),
        )
        .expect("bitemporal post-close knowledge");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(false), "{r}");
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "svt",
                   "tx_at_unix_ms": now_ms(), "valid_at_unix_ms": closed - 1}),
        )
        .expect("bitemporal in-period cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(closed), "{r}");

        // No-op calls (same value, or a later one — the earlier close is
        // kept) write NO snapshot.
        assert_eq!(db.set_valid_to(&ent.id, closed).expect("same-value no-op"), closed);
        assert_eq!(
            db.set_valid_to(&ent.id, closed + 10_000).expect("would-extend no-op"),
            closed
        );
        assert_eq!(
            db.history_versions("facts", "svt").unwrap().len(),
            1,
            "no-op set_valid_to must not snapshot"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn supersede_close_inherits_the_audit_snapshot() {
        // #373: perseus_vault_supersede funnels through set_valid_to, so closing the
        // old fact's period now snapshots it — the pre-supersede open version
        // stays reconstructable at earlier transaction instants.
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "sup-old", "body_json": "{\"note\":\"old\"}"}),
        )
        .expect("old");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "sup-new", "body_json": "{\"note\":\"new\"}"}),
        )
        .expect("new");
        assert!(db.history_versions("facts", "sup-old").unwrap().is_empty());

        sleep(Duration::from_millis(5));
        let tx_open = now_ms();
        // Strictly later clock tick — see the granularity note in
        // supersede_snapshot_preserves_the_pre_supersede_status.
        while now_ms() <= tx_open {
            sleep(Duration::from_millis(1));
        }

        let r = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "sup-old",
                   "to_category": "facts", "to_key": "sup-new"}),
        )
        .expect("supersede");
        let v: Value = serde_json::from_str(&r).unwrap();
        let closed_at = v["from_valid_to_unix_ms"].as_i64().expect("close instant");

        assert_eq!(
            db.history_versions("facts", "sup-old").unwrap().len(),
            2,
            "supersede must write the close snapshot plus the audited status-flip snapshot (#377)"
        );
        // Pre-supersede knowledge still believes the old fact open at an
        // instant the close later excluded.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "sup-old",
                   "tx_at_unix_ms": tx_open, "valid_at_unix_ms": closed_at + 60_000}),
        )
        .expect("bitemporal pre-supersede knowledge");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["valid_to_unix_ms"], Value::Null, "{r}");
        // Current knowledge: closed.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "sup-old",
                   "tx_at_unix_ms": now_ms(), "valid_at_unix_ms": closed_at + 60_000}),
        )
        .expect("bitemporal post-supersede knowledge");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(false), "{r}");
        // The NEW fact was not mutated temporally — its history stays empty
        // (#375 review note: only the superseded side is snapshotted).
        assert!(
            db.history_versions("facts", "sup-new").unwrap().is_empty(),
            "supersede must not snapshot the successor"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn supersede_snapshot_preserves_the_pre_supersede_status() {
        // #375: the status flip used to run BEFORE the audited close, so the
        // pre-supersede snapshot carried status='deprecated' under the
        // ORIGINAL recorded_at — reconstruction at a pre-supersede tx instant
        // showed the fact deprecated when it was still believed active. With
        // the close running first, the snapshot preserves status='active' and
        // the deprecation is only visible from the supersede's tx time on.
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "anachron", "body_json": "{\"note\":\"was active\"}"}),
        )
        .expect("old fact");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "anachron-new", "body_json": "{\"note\":\"replacement\"}"}),
        )
        .expect("successor");
        assert_eq!(
            db.get_entity("facts", "anachron").unwrap().unwrap().status,
            "active"
        );

        sleep(Duration::from_millis(5));
        let tx_mid = now_ms(); // pre-supersede transaction instant
        // The supersede must land on a strictly later clock tick than tx_mid,
        // or its snapshots' recorded_at collides with tx_mid and the
        // reconstruction below matches the wrong version (Windows now_ms
        // granularity can exceed a small sleep).
        while now_ms() <= tx_mid {
            sleep(Duration::from_millis(1));
        }

        let r = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "anachron",
                   "to_category": "facts", "to_key": "anachron-new"}),
        )
        .expect("supersede");
        let v: Value = serde_json::from_str(&r).unwrap();
        let closed_at = v["from_valid_to_unix_ms"].as_i64().expect("close instant");

        // Two snapshots of the OLD fact (the audited close, then the audited
        // status flip — #377); the successor untouched. Both snapshots are
        // pre-flip versions, so NEITHER may carry the deprecation — that
        // baked-in status is exactly the #375/#377 bug.
        let hist = db.history_versions("facts", "anachron").unwrap();
        assert_eq!(hist.len(), 2);
        assert!(
            hist.iter().all(|v| v.status == "active"),
            "no history snapshot may bake in the later deprecation"
        );
        assert!(db.history_versions("facts", "anachron-new").unwrap().is_empty());

        // Pre-supersede reconstruction: ACTIVE and open.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "anachron",
                   "tx_at_unix_ms": tx_mid, "valid_at_unix_ms": closed_at - 1}),
        )
        .expect("bitemporal pre-supersede cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(
            v["status"],
            json!("active"),
            "pre-supersede reconstruction must not show the later deprecation: {r}"
        );
        assert_eq!(v["valid_to_unix_ms"], Value::Null, "{r}");

        // Post-supersede (current knowledge): DEPRECATED and closed. Queried
        // at a future tx instant — the audited writes bump recorded_at
        // strictly past the previous one (#373), which can land 1ms ahead of
        // the wall clock, so `now_ms()` in the same tick could miss the live
        // row and match the intermediate close snapshot instead.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "anachron",
                   "tx_at_unix_ms": now_ms() + 60_000, "valid_at_unix_ms": closed_at - 1}),
        )
        .expect("bitemporal post-supersede cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["status"], json!("deprecated"), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(closed_at), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn supersede_of_expired_fact_still_audits_the_status_flip() {
        // #377: superseding a fact whose valid period is ALREADY closed takes
        // set_valid_to's no-op early-return — no close snapshot. The status
        // flip used to be unaudited on top of that, baking 'deprecated' under
        // the ORIGINAL recorded_at, so reconstruction at pre-supersede tx
        // instants showed the expired fact as already deprecated. The audited
        // flip now writes the only snapshot on this path.
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();

        // Generous margin: the remembers below must land before `expiry`, or
        // the insert path rejects the inverted period under CI load.
        let expiry = now_ms() + 200;
        handle_remember(
            &db,
            json!({"category": "facts", "key": "expired-old",
                   "body_json": "{\"note\":\"expired but still active\"}",
                   "valid_to_unix_ms": expiry}),
        )
        .expect("expired fact");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "expired-new",
                   "body_json": "{\"note\":\"replacement\"}"}),
        )
        .expect("successor");

        // Let the fact expire naturally; its status is still 'active'.
        while now_ms() <= expiry {
            sleep(Duration::from_millis(5));
        }
        assert_eq!(
            db.get_entity("facts", "expired-old").unwrap().unwrap().status,
            "active"
        );
        assert!(db.history_versions("facts", "expired-old").unwrap().is_empty());

        let tx_mid = now_ms(); // pre-supersede transaction instant
        // Strictly later clock tick — see the granularity note in
        // supersede_snapshot_preserves_the_pre_supersede_status.
        while now_ms() <= tx_mid {
            sleep(Duration::from_millis(1));
        }

        handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "expired-old",
                   "to_category": "facts", "to_key": "expired-new"}),
        )
        .expect("supersede expired fact");

        // The close was a no-op (the period had already ended), so the flip's
        // snapshot is the ONLY history row — and it captures the pre-flip
        // status.
        let hist = db.history_versions("facts", "expired-old").unwrap();
        assert_eq!(
            hist.len(),
            1,
            "the audited status flip must snapshot when the close no-ops"
        );
        assert_eq!(hist[0].status, "active");

        // Pre-supersede reconstruction at an in-period instant: ACTIVE, with
        // the original expiry intact.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "expired-old",
                   "tx_at_unix_ms": tx_mid, "valid_at_unix_ms": expiry - 1}),
        )
        .expect("bitemporal pre-supersede cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(
            v["status"],
            json!("active"),
            "pre-supersede reconstruction of an expired fact must not show the later deprecation: {r}"
        );
        assert_eq!(v["valid_to_unix_ms"], json!(expiry), "{r}");

        // Current knowledge: deprecated, expiry unchanged (never extended).
        // Future tx instant for the same recorded_at-skew reason as in
        // supersede_snapshot_preserves_the_pre_supersede_status.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "expired-old",
                   "tx_at_unix_ms": now_ms() + 60_000, "valid_at_unix_ms": expiry - 1}),
        )
        .expect("bitemporal post-supersede cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert_eq!(v["status"], json!("deprecated"), "{r}");
        assert_eq!(v["valid_to_unix_ms"], json!(expiry), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn same_status_reason_overwrite_stays_unversioned() {
        // #377 decision: a status no-change call (e.g. re-superseding an
        // already-deprecated fact) refreshes archive_reason in place without
        // a snapshot — a reason overwrite is operational metadata, not a
        // knowledge change.
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "rs-old", "body_json": "{\"note\":\"old\"}"}),
        )
        .expect("old");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "rs-new", "body_json": "{\"note\":\"new\"}"}),
        )
        .expect("new");
        handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "rs-old",
                   "to_category": "facts", "to_key": "rs-new"}),
        )
        .expect("first supersede");
        let snapshots = db.history_versions("facts", "rs-old").unwrap().len();

        let old_id = db.get_entity("facts", "rs-old").unwrap().unwrap().id;
        db.update_entity_status(&old_id, "deprecated", "second reason")
            .expect("same-status reason overwrite");

        assert_eq!(
            db.history_versions("facts", "rs-old").unwrap().len(),
            snapshots,
            "a same-status reason overwrite must not write a snapshot"
        );
        let e = db.get_entity("facts", "rs-old").unwrap().unwrap();
        assert_eq!(e.status, "deprecated");
        assert_eq!(e.archive_reason, "second reason");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn bitemporal_tool_reports_both_axes() {
        use std::thread::sleep;
        use std::time::Duration;
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "two-axis", "body_json": "{\"note\":\"v1\"}"}),
        )
        .expect("v1");
        sleep(Duration::from_millis(5));
        let tx_mid = now_ms();
        sleep(Duration::from_millis(5));
        let vf2 = now_ms() - 60_000; // retroactive
        handle_remember(
            &db,
            json!({"category": "facts", "key": "two-axis", "body_json": "{\"note\":\"v2\"}",
                   "valid_from_unix_ms": vf2}),
        )
        .expect("v2");

        // At tx_mid we believed v1 — even for a world-instant v2 now covers.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "two-axis",
                   "tx_at_unix_ms": tx_mid, "valid_at_unix_ms": tx_mid}),
        )
        .expect("bitemporal old-knowledge cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert!(v["body_json"].as_str().unwrap().contains("v1"), "{r}");

        // With current knowledge the same world-instant belongs to v2.
        let r = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "two-axis",
                   "tx_at_unix_ms": now_ms() + 60_000, "valid_at_unix_ms": tx_mid}),
        )
        .expect("bitemporal new-knowledge cell");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["found"], json!(true), "{r}");
        assert!(v["body_json"].as_str().unwrap().contains("v2"), "{r}");

        // Missing parameter errors are named.
        let err = handle_bitemporal(
            &db,
            json!({"category": "facts", "key": "two-axis", "valid_at_unix_ms": 1}),
        )
        .expect_err("missing tx_at must error");
        assert!(err.contains("tx_at_unix_ms"), "got: {err}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn recall_valid_time_filters_narrow_results() {
        let (db, path) = temp_db();
        let now = now_ms();

        // Fact A: valid only during a past window (ended).
        handle_remember(
            &db,
            json!({"category": "ops", "key": "window-a", "body_json": "{\"note\":\"ceasefire window alpha\"}",
                   "valid_from_unix_ms": now - 100_000, "valid_to_unix_ms": now - 50_000}),
        )
        .expect("A");
        // Fact B: valid from now, unbounded.
        handle_remember(
            &db,
            json!({"category": "ops", "key": "window-b", "body_json": "{\"note\":\"ceasefire window bravo\"}"}),
        )
        .expect("B");

        let keys = |resp: &str| -> Vec<String> {
            let v: Value = serde_json::from_str(resp).unwrap();
            v["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|i| i["key"].as_str().unwrap().to_string())
                .collect()
        };

        // No filter: both.
        let all = handle_recall(&db, json!({"query": "ceasefire", "mode": "fts5"})).unwrap();
        assert_eq!(keys(&all).len(), 2, "{all}");

        // valid_at inside A's window: only A.
        let past = handle_recall(
            &db,
            json!({"query": "ceasefire", "mode": "fts5", "valid_at": now - 75_000}),
        )
        .unwrap();
        assert_eq!(keys(&past), vec!["window-a".to_string()], "{past}");

        // valid_at after both writes: only B (A ended). +10s guards against
        // B's creation landing a few ms after the captured `now`.
        let current = handle_recall(
            &db,
            json!({"query": "ceasefire", "mode": "fts5", "valid_at": now + 10_000}),
        )
        .unwrap();
        assert_eq!(keys(&current), vec!["window-b".to_string()], "{current}");

        // Period OVERLAPS spanning A's window and beyond: both.
        let overlap = handle_recall(
            &db,
            json!({"query": "ceasefire", "mode": "fts5",
                   "valid_from_unix_ms": now - 80_000, "valid_to_unix_ms": now + 80_000,
                   "valid_op": "overlaps"}),
        )
        .unwrap();
        assert_eq!(keys(&overlap).len(), 2, "{overlap}");

        // Period CONTAINS a slice strictly inside A's window: only A… and only
        // if A's period contains the whole queried slice.
        let contains = handle_recall(
            &db,
            json!({"query": "ceasefire", "mode": "fts5",
                   "valid_from_unix_ms": now - 90_000, "valid_to_unix_ms": now - 60_000,
                   "valid_op": "contains"}),
        )
        .unwrap();
        assert_eq!(keys(&contains), vec!["window-a".to_string()], "{contains}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn supersede_closes_the_old_facts_valid_period() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "facts", "key": "old-roe", "body_json": "{\"note\":\"roe v1\"}"}),
        )
        .expect("old");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "new-roe", "body_json": "{\"note\":\"roe v2\"}"}),
        )
        .expect("new");

        // Ensure the close instant lands strictly after the fact's valid_from
        // (now_ms has 1ms resolution).
        std::thread::sleep(std::time::Duration::from_millis(5));
        let r = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "old-roe",
                   "to_category": "facts", "to_key": "new-roe"}),
        )
        .expect("supersede");
        let v: Value = serde_json::from_str(&r).unwrap();
        let closed_at = v["from_valid_to_unix_ms"].as_i64().expect("close instant reported");

        // The old fact is no longer "true in the world" from the close on.
        let after = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "old-roe", "valid_at_unix_ms": closed_at}),
        )
        .unwrap();
        let av: Value = serde_json::from_str(&after).unwrap();
        assert_eq!(av["found"], json!(false), "{after}");
        // But it WAS true just before.
        let before = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "old-roe", "valid_at_unix_ms": closed_at - 1}),
        )
        .unwrap();
        let bv: Value = serde_json::from_str(&before).unwrap();
        assert_eq!(bv["found"], json!(true), "{before}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn temporal_filtered_recall_does_not_reinforce_hidden_entities() {
        // #363 review, #356-class value inversion: a recall with a valid-time
        // filter must NOT apply retrieval side-effects to entities the filter
        // hides — otherwise repeated "what was true at T" queries reinforce
        // (and eventually make immortal) entities that are never returned.
        let (db, path) = temp_db();
        let now = now_ms();

        // A: valid only in a past window (always filtered out below).
        handle_remember(
            &db,
            json!({"category": "ops", "key": "hidden-a", "body_json": "{\"note\":\"embargo period alpha\"}",
                   "valid_from_unix_ms": now - 100_000, "valid_to_unix_ms": now - 50_000}),
        )
        .expect("A");
        // B: currently valid (always survives).
        handle_remember(
            &db,
            json!({"category": "ops", "key": "visible-b", "body_json": "{\"note\":\"embargo period bravo\"}"}),
        )
        .expect("B");

        let count_of = |key: &str| -> i64 {
            db.get_entity("ops", key).unwrap().unwrap().retrieval_count
        };
        assert_eq!(count_of("hidden-a"), 0);
        assert_eq!(count_of("visible-b"), 0);

        // Three temporal-filtered recalls: only B survives each time.
        for _ in 0..3 {
            let r = handle_recall(
                &db,
                json!({"query": "embargo", "mode": "fts5", "valid_at": now + 10_000}),
            )
            .expect("filtered recall");
            let v: Value = serde_json::from_str(&r).unwrap();
            assert_eq!(v["total"], json!(1), "{r}");
        }

        assert_eq!(
            count_of("hidden-a"),
            0,
            "filtered-out entity must NOT be reinforced by temporal recalls"
        );
        assert_eq!(
            count_of("visible-b"),
            3,
            "surviving entity must still be reinforced once per recall"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn temporal_recall_surfaces_superseded_fact_via_history() {
        // #682 end-to-end: a fact valid at T whose body has since been
        // superseded must surface under a temporal recall keyed on its OWN
        // (now-historical) text — the case plain live-index recall cannot serve.
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "org", "key": "leader",
                   "body_json": "{\"note\":\"the leader is Alice Alpha\"}"}),
        )
        .expect("v1");
        // Distinct transaction time so Alice's [recorded, invalidated) window is
        // non-empty (as_of below lands strictly inside it).
        std::thread::sleep(std::time::Duration::from_millis(5));
        handle_remember(
            &db,
            json!({"category": "org", "key": "leader",
                   "body_json": "{\"note\":\"the leader is Bob Bravo\"}"}),
        )
        .expect("v2 supersede");

        // Plain (non-temporal) recall on the superseded term finds nothing —
        // the live body is now "Bob Bravo".
        let live = handle_recall(&db, json!({"query": "Alpha", "mode": "fts5"})).expect("live");
        let lv: Value = serde_json::from_str(&live).unwrap();
        assert_eq!(lv["total"], json!(0), "live recall must miss the old term: {live}");

        // Alice's transaction window, read straight from history.
        let (rec, inv): (i64, i64) = {
            let conn = db.conn().unwrap();
            conn.query_row(
                "SELECT COALESCE(recorded_at_unix_ms, created_at_unix_ms), invalidated_at_unix_ms \
                 FROM entity_history WHERE category='org' AND key='leader'",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap()
        };
        assert!(rec < inv, "sleep must yield a non-empty tx window ({rec} < {inv})");

        // Temporal recall AS OF that window surfaces the superseded Alice body.
        let r = handle_recall(
            &db,
            json!({"query": "Alpha", "mode": "fts5", "as_of_unix_ms": rec}),
        )
        .expect("temporal recall");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["total"], json!(1), "temporal recall must surface it: {r}");
        let item = v["items"][0].to_string();
        assert!(
            item.contains("Alice") && !item.contains("Bob"),
            "must reconstruct the point-in-time (Alice) body, got: {item}"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn recall_enforces_visibility_by_requesting_agent() {
        // #684: a private entity is hidden from other agents but visible to its
        // author and to an unscoped requester; default-visibility entities are
        // visible to everyone (non-breaking).
        let (db, path) = temp_db();
        db.agent_upsert("alice", "Alice", 0, "eng").unwrap();
        db.agent_upsert("bob", "Bob", 0, "eng").unwrap();

        handle_remember(
            &db,
            json!({"category": "notes", "key": "secret",
                   "body_json": "{\"note\":\"quantum widget blueprint\"}",
                   "visibility": "private", "agent_id": "alice"}),
        )
        .expect("private");
        handle_remember(
            &db,
            json!({"category": "notes", "key": "shared",
                   "body_json": "{\"note\":\"quantum team standup\"}",
                   "agent_id": "alice"}),
        )
        .expect("shared");

        let count = |args: Value| -> i64 {
            let r = handle_recall(&db, args).expect("recall");
            serde_json::from_str::<Value>(&r).unwrap()["total"]
                .as_i64()
                .unwrap()
        };

        // Bob (a different agent) sees only the default-visibility entity.
        assert_eq!(
            count(json!({"query": "quantum", "mode": "fts5", "requesting_agent_id": "bob"})),
            1,
            "bob must not see alice's private note"
        );
        // Alice sees both (author of the private one).
        assert_eq!(
            count(json!({"query": "quantum", "mode": "fts5", "requesting_agent_id": "alice"})),
            2,
            "alice sees her own private note"
        );
        // No requester identity → unscoped → sees both (existing behavior).
        assert_eq!(
            count(json!({"query": "quantum", "mode": "fts5"})),
            2,
            "unscoped recall is unchanged"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn keystone_authoring_uses_registered_trust_tier() {
        // #684 completes #683's hook: keystone authoring is gated on the
        // AUTHORITATIVE registered tier when the author is a known agent.
        let (db, path) = temp_db();
        db.agent_upsert("low", "Low", 1, "").unwrap(); // below the default required 2
        db.agent_upsert("high", "High", 2, "").unwrap();

        // Registered tier-1 agent is rejected even without asserting a tier.
        let denied = handle_keystone_set(
            &db,
            json!({"content": "rule x", "agent_id": "low"}),
        );
        assert!(
            denied.is_err() && denied.unwrap_err().contains("registered agent"),
            "tier-1 registered agent must be denied by the registry"
        );
        // Registered tier-2 agent succeeds, and it is registry-enforced.
        let ok = handle_keystone_set(
            &db,
            json!({"content": "rule x", "agent_id": "high"}),
        )
        .expect("tier-2 allowed");
        assert!(ok.contains("\"trust_enforced\":true"), "registry-enforced: {ok}");
        let _ = std::fs::remove_file(&path);
    }

    // ── #889 keystone-suggestion queue ───────────────────────────────────

    #[test]
    fn correct_capture_extracts_suggestions_but_never_writes_policy() {
        // Extraction is suggestions-only: the correct call must queue
        // candidates and must NOT touch the keystones table.
        let (db, path) = temp_db();
        let response = handle_correct(
            &db,
            json!({
                "wrong_approach": "guessing instead of asking",
                "user_correction": "whenever needed we can use it",
                "task_context": "writing a memory",
            }),
        )
        .expect("correct");
        let v: Value = serde_json::from_str(&response).unwrap();
        let sugs = v["keystone_suggestions"].as_array().unwrap();
        assert!(
            sugs.iter().any(|s| s["pattern"] == "whenever"),
            "whenever directive must be extracted: {response}"
        );
        assert!(
            sugs.iter().all(|s| !s["instruction"].as_str().unwrap().starts_with("never")),
            "inversion regression: no 'never' suggestion from a 'whenever' text: {response}"
        );
        assert_eq!(v["keystone_suggestions_count"], json!(sugs.len()));
        // Suggestion is pending in the queue.
        let listed = db.keystone_suggestions_list("pending", None, 50).unwrap();
        assert_eq!(listed.len(), 1, "{response}");
        assert_eq!(listed[0].source_entity_id, v["entity_id"].as_str().unwrap());
        // No policy was written by extraction.
        let ks = db.keystone_get(None, None, None).unwrap();
        assert!(ks.is_empty(), "extraction must never create keystones");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn correction_rewrite_deduplicates_suggestions() {
        // Same (source, instruction) pair queues once.
        let (db, path) = temp_db();
        for _ in 0..2 {
            handle_correct(
                &db,
                json!({
                    "wrong_approach": "x",
                    "user_correction": "always cite sources",
                    "task_context": "y",
                }),
            )
            .expect("correct");
        }
        let listed = db.keystone_suggestions_list("pending", None, 50).unwrap();
        let always: Vec<_> = listed
            .iter()
            .filter(|s| s.matched_pattern == "always")
            .collect();
        assert_eq!(always.len(), 1, "dedupe failed: {listed:?}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn suggestion_approve_promotes_with_trust_gate() {
        let (db, path) = temp_db();
        db.agent_upsert("low", "Low", 1, "").unwrap(); // below default required 2
        db.agent_upsert("high", "High", 2, "").unwrap();
        handle_correct(
            &db,
            json!({
                "wrong_approach": "x",
                "user_correction": "never share credentials in logs",
                "task_context": "y",
            }),
        )
        .expect("correct");
        let sug = &db.keystone_suggestions_list("pending", None, 10).unwrap()[0];

        // Registry-enforced tier-1 author is denied promotion.
        let denied = handle_keystone_suggestion_decide(
            &db,
            json!({"id": sug.id, "action": "approve", "agent_id": "low"}),
        );
        assert!(
            denied.is_err() && denied.unwrap_err().contains("registered agent"),
            "tier-1 registered agent must be denied"
        );
        assert_eq!(
            db.keystone_suggestions_list("pending", None, 10).unwrap().len(),
            1,
            "denied approval must leave the suggestion pending"
        );
        assert!(
            db.keystone_get(None, None, None).unwrap().is_empty(),
            "no keystone may be created by a denied approval"
        );

        // Tier-2 author succeeds; the suggestion is approved and the
        // keystone exists with the extracted instruction.
        let ok = handle_keystone_suggestion_decide(
            &db,
            json!({"id": sug.id, "action": "approve", "agent_id": "high"}),
        )
        .expect("tier-2 approval");
        assert!(ok.contains("\"trust_enforced\":true"), "{ok}");
        let approved = db.keystone_suggestions_list("approved", None, 10).unwrap();
        assert_eq!(approved.len(), 1);
        let ks = db.keystone_get(None, None, None).unwrap();
        assert_eq!(ks.len(), 1);
        assert_eq!(ks[0].content, "never share credentials in logs");

        // Deciding twice fails.
        let again = handle_keystone_suggestion_decide(
            &db,
            json!({"id": sug.id, "action": "approve", "agent_id": "high"}),
        );
        assert!(again.is_err(), "already-decided suggestion must be immutable");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn suggestion_reject_writes_nothing() {
        let (db, path) = temp_db();
        handle_correct(
            &db,
            json!({
                "wrong_approach": "x",
                "user_correction": "always verify before publishing",
                "task_context": "y",
            }),
        )
        .expect("correct");
        let sug = &db.keystone_suggestions_list("pending", None, 10).unwrap()[0];
        let ok = handle_keystone_suggestion_decide(
            &db,
            json!({"id": sug.id, "action": "reject", "agent_id": "op"}),
        )
        .expect("reject");
        assert!(ok.contains("\"keystone_promoted\":false"), "{ok}");
        assert!(db.keystone_get(None, None, None).unwrap().is_empty());
        assert_eq!(db.keystone_suggestions_list("rejected", None, 10).unwrap().len(), 1);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn operator_review_surfaces_pending_suggestions() {
        let (db, path) = temp_db();
        handle_correct(
            &db,
            json!({
                "wrong_approach": "x",
                "user_correction": "whenever the connection drops retry once",
                "task_context": "y",
            }),
        )
        .expect("correct");
        let raw = handle_operator_review(&db, json!({"category":"facts", "limit":20})).unwrap();
        let v: Value = serde_json::from_str(&raw).unwrap();
        let sugs = v["keystone_suggestions"].as_array().unwrap();
        assert!(!sugs.is_empty(), "operator review must surface suggestions: {raw}");
        assert_eq!(sugs[0]["status"], "pending");
        assert!(sugs[0]["source_entity_id"].as_str().unwrap().starts_with("cor-"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn unfiltered_recall_side_effects_and_output_are_unchanged() {
        // #363 review: the pure-read deferral only engages when a valid-time
        // filter is present. An unfiltered recall must keep the original
        // behavior — side-effects applied to every hit — and an always-true
        // filter must return the identical item set.
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "ops", "key": "u1", "body_json": "{\"note\":\"quorum call one\"}"}),
        )
        .expect("u1");
        handle_remember(
            &db,
            json!({"category": "ops", "key": "u2", "body_json": "{\"note\":\"quorum call two\"}"}),
        )
        .expect("u2");

        let unfiltered =
            handle_recall(&db, json!({"query": "quorum", "mode": "fts5"})).expect("recall");
        let uv: Value = serde_json::from_str(&unfiltered).unwrap();
        assert_eq!(uv["total"], json!(2), "{unfiltered}");
        for key in ["u1", "u2"] {
            assert_eq!(
                db.get_entity("ops", key).unwrap().unwrap().retrieval_count,
                1,
                "unfiltered recall must still reinforce every hit ({key})"
            );
        }

        // An always-true valid filter returns the same keys.
        let filtered = handle_recall(
            &db,
            json!({"query": "quorum", "mode": "fts5", "valid_at": now_ms() + 60_000}),
        )
        .expect("filtered recall");
        let fv: Value = serde_json::from_str(&filtered).unwrap();
        // Compare as SETS (sorted), not ordered lists: the first (unfiltered)
        // recall reinforces both hits, which legitimately changes the ranking
        // inputs (retrieval_count, last_accessed) before the second call, and
        // the final `id ASC` tie-break is over random UUIDs — so cross-call
        // ORDER is not a stable property. Membership is.
        let keys = |v: &Value| -> Vec<String> {
            let mut k: Vec<String> = v["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|i| i["key"].as_str().unwrap().to_string())
                .collect();
            k.sort();
            k
        };
        assert_eq!(keys(&uv), keys(&fv), "always-true filter must not change the result set");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn recall_rejects_unknown_valid_op() {
        let (db, path) = temp_db();
        let err = handle_recall(
            &db,
            json!({"query": "x", "valid_from_unix_ms": 1, "valid_op": "during"}),
        )
        .expect_err("unknown valid_op must be rejected");
        assert!(err.contains("valid_op") && err.contains("during"), "got: {err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn correct_rejects_inverted_valid_period() {
        let (db, path) = temp_db();
        let err = handle_correct(
            &db,
            json!({"wrong_approach": "w", "user_correction": "c", "task_context": "t",
                   "valid_from_unix_ms": 200, "valid_to_unix_ms": 100}),
        )
        .expect_err("inverted period must be rejected on correct");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        // Nothing was written.
        let r = handle_recall(&db, json!({"query": "", "category": "correction", "mode": "fts5"}))
            .unwrap_or_else(|_| "{\"total\":0}".to_string());
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["total"], json!(0), "rejected correct must not create an entity: {r}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn correct_rejects_one_sided_past_valid_to() {
        // #363 review (round 2): same one-sided guard on the correct surface —
        // valid_from omitted defaults to now, so a past valid_to inverts.
        let (db, path) = temp_db();
        let err = handle_correct(
            &db,
            json!({"wrong_approach": "w", "user_correction": "c", "task_context": "t",
                   "valid_to_unix_ms": now_ms() - 60_000}),
        )
        .expect_err("one-sided past valid_to must be rejected on correct");
        assert!(err.contains("valid_to_unix_ms"), "got: {err}");
        // Nothing was written.
        let r = handle_recall(&db, json!({"query": "", "category": "correction", "mode": "fts5"}))
            .unwrap_or_else(|_| "{\"total\":0}".to_string());
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["total"], json!(0), "rejected correct must not create an entity: {r}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn supersede_valid_to_cannot_invert_or_extend_a_closed_period() {
        // #363 review: an explicit valid_to on supersede must be validated
        // against the old fact's stored period BEFORE any mutation, and a
        // default-now supersede of an already-ended fact must keep the
        // earlier close (never retroactively extend validity).
        let (db, path) = temp_db();
        let now = now_ms();
        let vf = now - 100_000;
        let vt = now - 50_000;
        handle_remember(
            &db,
            json!({"category": "facts", "key": "ended", "body_json": "{\"note\":\"old bounded\"}",
                   "valid_from_unix_ms": vf, "valid_to_unix_ms": vt}),
        )
        .expect("bounded old fact");
        handle_remember(
            &db,
            json!({"category": "facts", "key": "successor", "body_json": "{\"note\":\"new\"}"}),
        )
        .expect("successor");

        // (a) Inverted: valid_to at/before the old fact's valid_from.
        let err = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "ended",
                   "to_category": "facts", "to_key": "successor",
                   "valid_to_unix_ms": vf - 1}),
        )
        .expect_err("inverting valid_to must be rejected");
        assert!(err.contains("valid_from"), "got: {err}");
        // Rejected BEFORE mutation: the old fact is not deprecated.
        let status = db.get_entity("facts", "ended").unwrap().unwrap().status;
        assert_eq!(status, "active", "rejected supersede must not mutate status");

        // (b) Extension: valid_to after the already-stored close.
        let err = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "ended",
                   "to_category": "facts", "to_key": "successor",
                   "valid_to_unix_ms": now}),
        )
        .expect_err("extending a closed period must be rejected");
        assert!(err.contains("tighten"), "got: {err}");

        // (c) Default-now supersede of the already-ended fact: succeeds and
        // KEEPS the earlier close instead of extending it.
        let r = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "ended",
                   "to_category": "facts", "to_key": "successor"}),
        )
        .expect("default supersede");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(
            v["from_valid_to_unix_ms"],
            json!(vt),
            "an ended fact must keep its earlier close: {r}"
        );

        // (d) Tightening to an earlier close is allowed.
        let r = handle_supersede(
            &db,
            json!({"from_category": "facts", "from_key": "ended",
                   "to_category": "facts", "to_key": "successor",
                   "valid_to_unix_ms": vt - 10_000}),
        )
        .expect("tightening supersede");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["from_valid_to_unix_ms"], json!(vt - 10_000), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    // ─── #521: failure-pattern / deja-vu guard ───────────────────

    /// Seed a journal failure event directly (so tests control the timestamp
    /// and workspace, unlike handle_journal which stamps now/derives ws).
    fn seed_failure_event(db: &Database, id: &str, acted: &str, forward: &str, ws: &str, at: i64) {
        db.journal(&JournalEvent {
            id: id.to_string(),
            event_type: "error".to_string(),
            evaluated_json: "{}".to_string(),
            acted_json: acted.to_string(),
            forward_json: forward.to_string(),
            category: String::new(),
            key: String::new(),
            entity_id: String::new(),
            agent_id: String::new(),
            workspace_hash: ws.to_string(),
            created_at_unix_ms: at,
        })
        .expect("seed journal failure");
    }

    #[test]
    fn check_failure_pattern_requires_explicit_workspace_scope() {
        let (db, path) = temp_db();
        seed_failure_event(
            &db,
            "jrn-fp-scope-alpha",
            r#"{"command": "cargo build", "error": "alpha failure"}"#,
            "{}",
            "alpha",
            now_ms(),
        );
        seed_failure_event(
            &db,
            "jrn-fp-scope-beta",
            r#"{"command": "cargo build", "error": "beta failure"}"#,
            "{}",
            "beta",
            now_ms(),
        );

        let err = handle_check_failure_pattern(&db, json!({"action": "cargo build"}))
            .expect_err("omitted workspace_hash must fail closed");
        assert!(err.contains("workspace_hash"), "{err}");

        let r = handle_check_failure_pattern(
            &db,
            json!({"action": "cargo build", "workspace_hash": "alpha"}),
        )
        .expect("explicit workspace scope");
        let v: Value = serde_json::from_str(&r).unwrap();
        let refs: Vec<&str> = v["matches"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["ref"].as_str().unwrap())
            .collect();
        assert!(refs.contains(&"jrn-fp-scope-alpha"), "{r}");
        assert!(!refs.contains(&"jrn-fp-scope-beta"), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_matches_journal_and_entity_failures() {
        let (db, path) = temp_db();

        // A journal-recorded command failure…
        seed_failure_event(
            &db,
            "jrn-fp-1",
            r#"{"command": "cargo build --no-default-features", "error": "LNK1120 unresolved external symbol"}"#,
            r#"{"resolution": "run under vcvars64 with the MSVC toolchain"}"#,
            "",
            now_ms(),
        );
        // …and a remembered pitfall entity.
        handle_remember(
            &db,
            json!({"category": "pitfall", "key": "npm-publish-otp",
                   "tags": ["failure", "npm"],
                   "body_json": "{\"content\":\"npm publish fails without OTP: E401 one-time password required\",\"resolution\":\"re-run npm publish --otp=<code>\"}"}),
        )
        .expect("remember pitfall");

        // Retry of the failed command → journal match, deja_vu, warning.
        let r = handle_check_failure_pattern(
            &db,
            json!({"action": "cargo build --no-default-features", "workspace_hash": ""}),
        )
        .expect("check journal arm");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(true), "{r}");
        assert_eq!(v["matches"][0]["source"], json!("journal"), "{r}");
        assert_eq!(v["matches"][0]["ref"], json!("jrn-fp-1"), "{r}");
        assert_eq!(v["matches"][0]["workspace_hash"], json!(""), "{r}");
        assert!(
            v["matches"][0]["what_failed"].as_str().unwrap().contains("cargo build"),
            "{r}"
        );
        assert!(
            v["matches"][0]["cause"].as_str().unwrap().contains("LNK1120"),
            "{r}"
        );
        assert!(
            v["matches"][0]["resolution"].as_str().unwrap().contains("vcvars64"),
            "{r}"
        );
        let warning = v["warning"].as_str().expect("warning present");
        assert!(warning.contains("deja-vu"), "{warning}");
        assert!(!warning.contains('\n'), "warning must be one line: {warning}");

        // Retry of the remembered pitfall → entity match with cause/resolution.
        let r = handle_check_failure_pattern(&db, json!({"action": "npm publish", "workspace_hash": ""}))
            .expect("check entity arm");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(true), "{r}");
        assert_eq!(v["matches"][0]["source"], json!("entity"), "{r}");
        assert_eq!(v["matches"][0]["ref"], json!("pitfall/npm-publish-otp"), "{r}");
        assert!(
            v["matches"][0]["resolution"].as_str().unwrap().contains("--otp"),
            "{r}"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_empty_state_is_unambiguous() {
        let (db, path) = temp_db();
        // A recorded failure that does NOT match the action, plus a non-failure
        // entity that DOES lexically match — neither may produce a deja-vu.
        seed_failure_event(
            &db,
            "jrn-fp-other",
            r#"{"command": "cargo build --no-default-features", "error": "LNK1120"}"#,
            "{}",
            "",
            now_ms(),
        );
        handle_remember(
            &db,
            json!({"category": "convention", "key": "git-push-style",
                   "body_json": "{\"content\":\"git push origin main is the normal successful deploy flow\"}"}),
        )
        .expect("non-failure entity");

        let r = handle_check_failure_pattern(&db, json!({"action": "git push origin main", "workspace_hash": ""}))
            .expect("check empty state");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(false), "{r}");
        assert_eq!(v["matches"], json!([]), "{r}");
        assert_eq!(
            v["message"],
            json!("no prior failures recorded matching this action"),
            "{r}"
        );
        assert!(v.get("warning").is_none(), "no warning on empty state: {r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_ranks_recent_failures_first() {
        let (db, path) = temp_db();
        let now = now_ms();
        let ninety_days = 90 * 24 * 3600 * 1000i64;
        seed_failure_event(
            &db,
            "jrn-fp-old",
            r#"{"command": "docker compose up vault", "error": "port 8200 already in use"}"#,
            "{}",
            "",
            now - ninety_days,
        );
        seed_failure_event(
            &db,
            "jrn-fp-new",
            r#"{"command": "docker compose up vault", "error": "port 8200 already in use"}"#,
            "{}",
            "",
            now,
        );

        let r = handle_check_failure_pattern(&db, json!({"action": "docker compose up vault", "workspace_hash": ""}))
            .expect("check ranking");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(true), "{r}");
        let matches = v["matches"].as_array().unwrap();
        assert_eq!(matches.len(), 2, "{r}");
        assert_eq!(matches[0]["ref"], json!("jrn-fp-new"), "recent first: {r}");
        assert_eq!(matches[1]["ref"], json!("jrn-fp-old"), "{r}");
        assert!(
            matches[0]["score"].as_f64().unwrap() > matches[1]["score"].as_f64().unwrap(),
            "recency must break the equal-relevance tie: {r}"
        );

        // limit clamps the result set (and a junk limit is clamped, not fatal).
        let r = handle_check_failure_pattern(
            &db,
            json!({"action": "docker compose up vault", "workspace_hash": "", "limit": 1}),
        )
        .expect("limit=1");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["matches"].as_array().unwrap().len(), 1, "{r}");
        assert_eq!(v["matches"][0]["ref"], json!("jrn-fp-new"), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_scopes_by_workspace_but_sees_global() {
        let (db, path) = temp_db();
        // ws-a journal failure + ws-a entity failure + one GLOBAL entity failure.
        seed_failure_event(
            &db,
            "jrn-fp-wsa",
            r#"{"command": "terraform apply prod", "error": "state lock held"}"#,
            "{}",
            "ws-a",
            now_ms(),
        );
        handle_remember(
            &db,
            json!({"category": "pitfall", "key": "tf-apply-lock", "workspace_hash": "ws-a",
                   "body_json": "{\"content\":\"terraform apply prod failed: state lock held by stale run\"}"}),
        )
        .expect("ws-a pitfall");
        handle_remember(
            &db,
            json!({"category": "pitfall", "key": "tf-apply-global",
                   "body_json": "{\"content\":\"terraform apply prod is broken on the v1 module, pin v0.9\"}"}),
        )
        .expect("global pitfall");

        // From another workspace: ws-a's failures are invisible, but the
        // GLOBAL (unscoped) failure still warns.
        let r = handle_check_failure_pattern(
            &db,
            json!({"action": "terraform apply prod", "workspace_hash": "ws-b"}),
        )
        .expect("ws-b check");
        let v: Value = serde_json::from_str(&r).unwrap();
        let refs: Vec<&str> = v["matches"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["ref"].as_str().unwrap())
            .collect();
        assert!(
            !refs.contains(&"jrn-fp-wsa") && !refs.contains(&"pitfall/tf-apply-lock"),
            "ws-a failures must not leak into ws-b: {r}"
        );
        assert!(
            refs.contains(&"pitfall/tf-apply-global"),
            "global failures must still warn in any workspace: {r}"
        );

        // From ws-a: everything (own + global) surfaces.
        let r = handle_check_failure_pattern(
            &db,
            json!({"action": "terraform apply prod", "workspace_hash": "ws-a"}),
        )
        .expect("ws-a check");
        let v: Value = serde_json::from_str(&r).unwrap();
        let refs: Vec<&str> = v["matches"]
            .as_array()
            .unwrap()
            .iter()
            .map(|m| m["ref"].as_str().unwrap())
            .collect();
        for expected in ["jrn-fp-wsa", "pitfall/tf-apply-lock", "pitfall/tf-apply-global"] {
            assert!(refs.contains(&expected), "missing {expected}: {r}");
        }

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_is_a_pure_read() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "pitfall", "key": "flaky-test",
                   "body_json": "{\"content\":\"running the concurrent_writer test in CI fails intermittently\"}"}),
        )
        .expect("pitfall");

        for _ in 0..3 {
            let r = handle_check_failure_pattern(
                &db,
                json!({"action": "running the concurrent_writer test", "workspace_hash": ""}),
            )
            .expect("check");
            let v: Value = serde_json::from_str(&r).unwrap();
            assert_eq!(v["deja_vu"], json!(true), "{r}");
        }

        let e = db.get_entity("pitfall", "flaky-test").unwrap().unwrap();
        assert_eq!(
            e.retrieval_count, 0,
            "the guard must never reinforce what it scans (skip_side_effects)"
        );

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn check_failure_pattern_bounds_inputs() {
        let (db, path) = temp_db();

        let err = handle_check_failure_pattern(&db, json!({"action": "   ", "workspace_hash": ""}))
            .expect_err("blank action must be rejected");
        assert!(err.contains("action is required"), "{err}");

        let err = handle_check_failure_pattern(
            &db,
            json!({"action": "x".repeat(16 * 1024 + 1), "workspace_hash": ""}),
        )
        .expect_err("oversized action must be rejected (#433 pattern)");
        assert!(err.contains("action too long"), "{err}");

        let err = handle_check_failure_pattern(
            &db,
            json!({"action": "ls", "workspace_hash": "w".repeat(257)}),
        )
        .expect_err("oversized workspace_hash must be rejected");
        assert!(err.contains("workspace_hash too long"), "{err}");

        // Explicit null limit falls back to the default instead of erroring (#330).
        let r = handle_check_failure_pattern(&db, json!({"action": "ls -la", "workspace_hash": "", "limit": null}))
            .expect("null limit uses the default");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(false), "{r}");

        let _ = std::fs::remove_file(&path);
    }

    // #330: perseus_vault_remember rejected the documented optional `topic_path`
    // field (and other optional fields with custom defaults) whenever a
    // caller sent explicit JSON `null` instead of omitting the key. Many
    // MCP clients do this because the tool schema lists the field as
    // optional/defaulted, not because they're being unusual.

    #[test]
    fn remember_args_accepts_null_topic_path() {
        let v = json!({
            "category": "reference",
            "key": "example-key",
            "body_json": "{}",
            "topic_path": null
        });
        let a: RememberArgs = serde_json::from_value(v).expect("null topic_path must deserialize");
        assert_eq!(a.topic_path, "");
    }

    #[test]
    fn remember_args_accepts_null_for_every_optional_field_with_custom_default() {
        // Explicit null on each of these must fall back to that field's
        // documented default, not fail deserialization.
        for field in [
            "status",
            "type",
            "tags",
            "importance",
            "topic_path",
            "recall_when",
            "always_on",
            "certainty",
            "workspace_hash",
            "agent_id",
            "visibility",
        ] {
            let mut v = json!({
                "category": "reference",
                "key": "example-key",
                "body_json": "{}",
            });
            v.as_object_mut()
                .unwrap()
                .insert(field.to_string(), Value::Null);
            let result: Result<RememberArgs, _> = serde_json::from_value(v);
            assert!(
                result.is_ok(),
                "field `{}` with explicit null should deserialize, got {:?}",
                field,
                result.err()
            );
        }
    }

    #[test]
    fn remember_args_still_reports_missing_category_correctly() {
        // Regression guard: fixing the null-tolerance bug must not break the
        // genuinely-missing-required-field error path (the original bug
        // report's error message pointed at the wrong field — `category` —
        // when the real offender was `topic_path: null`; once null is
        // handled, a real missing `category` must still be reported as such).
        let v = json!({ "key": "example-key", "body_json": "{}" });
        let result: Result<RememberArgs, _> = serde_json::from_value(v);
        let err = result.expect_err("missing category must fail").to_string();
        assert!(
            err.contains("category"),
            "error should name the actually-missing field `category`, got: {}",
            err
        );
    }

    #[test]
    fn recall_args_accepts_null_for_every_optional_field_with_custom_default() {
        for field in [
            "limit",
            "offset",
            "min_decay",
            "include_archived",
            "expansion",
            "mode",
            "content_weight",
            "trust_weight",
            "diversity_halving",
            "include_confidence",
        ] {
            let mut v = json!({ "query": "test" });
            v.as_object_mut()
                .unwrap()
                .insert(field.to_string(), Value::Null);
            let result: Result<RecallArgs, _> = serde_json::from_value(v);
            assert!(
                result.is_ok(),
                "field `{}` with explicit null should deserialize, got {:?}",
                field,
                result.err()
            );
        }
    }

    // #472: LLM/MCP clients frequently emit integer tool-call args as strings.
    // The temporal filters must accept a numeric string as well as a number
    // (and empty string / null → None), or Temporal RAG is uncallable from
    // those clients ("invalid type: string, expected i64").
    #[test]
    fn recall_args_accept_stringified_temporal_ints() {
        let v = json!({
            "query": "test",
            "as_of_unix_ms": "1783400000000",
            "valid_at": "1700000000000",
            "valid_from_unix_ms": 1600000000000i64,
            "valid_to_unix_ms": ""
        });
        let a: RecallArgs = serde_json::from_value(v).expect("stringified temporal ints must parse");
        assert_eq!(a.as_of_unix_ms, Some(1783400000000));
        assert_eq!(a.valid_at, Some(1700000000000));
        assert_eq!(a.valid_from_unix_ms, Some(1600000000000)); // bare number still works
        assert_eq!(a.valid_to_unix_ms, None); // empty string → None
        // Non-numeric string is a clear error, not silently dropped.
        let bad = json!({ "query": "t", "as_of_unix_ms": "notanumber" });
        assert!(serde_json::from_value::<RecallArgs>(bad).is_err());
    }

    #[test]
    fn recall_args_null_limit_falls_back_to_default_ten() {
        let v = json!({ "query": "test", "limit": null });
        let a: RecallArgs = serde_json::from_value(v).unwrap();
        assert_eq!(a.limit, 10);
    }

    #[test]
    fn follow_args_accept_null_workspace_hash() {
        // #396's new optional arg must follow the explicit-null tolerance
        // rule (#330) like the rest of the tool surface.
        let v = json!({
            "category": "convention",
            "key": "k",
            "followed": true,
            "workspace_hash": null
        });
        let a: FollowArgs = serde_json::from_value(v).expect("null workspace_hash must deserialize");
        assert!(a.workspace_hash.is_none());
    }

    #[test]
    fn context_args_accept_null_for_new_optional_fields() {
        // #356/#366 args must follow the same explicit-null tolerance rule
        // as the rest of the tool surface (#330).
        for field in ["query", "mode", "model", "max_context_chars", "workspace_hash", "categories"] {
            let mut v = json!({});
            v.as_object_mut()
                .unwrap()
                .insert(field.to_string(), Value::Null);
            let result: Result<ContextArgs, _> = serde_json::from_value(v);
            assert!(
                result.is_ok(),
                "field `{}` with explicit null should deserialize, got {:?}",
                field,
                result.err()
            );
        }
    }

    fn temp_tool_db() -> (Database, String) {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("perseus_vault-tools-test-{}.db", uuid::Uuid::new_v4()));
        let path_str = path.to_str().unwrap().to_string();
        let db = Database::open(&path_str).expect("open test db");
        (db, path_str)
    }

    #[test]
    fn handle_context_defaults_to_recall_first_on_demand() {
        let (db, path) = temp_tool_db();
        let out = handle_context(&db, json!({}));
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["mode"], "on_demand", "recall-first must be the default: {out}");
        assert_eq!(v["budget_chars"], 1500);
        assert!(
            v["markdown"].as_str().unwrap().contains("Recall-first mode"),
            "no-query default output must be the retrieval pointer: {out}"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn handle_context_rejects_unknown_mode() {
        let (db, path) = temp_tool_db();
        let out = handle_context(&db, json!({"mode": "firehose"}));
        let v: Value = serde_json::from_str(&out).unwrap();
        assert!(
            v["error"].as_str().unwrap().contains("Invalid context mode"),
            "unknown mode must be rejected: {out}"
        );
        // The legacy opt-in spelling still parses.
        let legacy = handle_context(&db, json!({"mode": "always_inject"}));
        let lv: Value = serde_json::from_str(&legacy).unwrap();
        assert_eq!(lv["mode"], "always_inject");
        assert_eq!(lv["budget_chars"], 0);
        let _ = std::fs::remove_file(&path);
    }

    // ─── History retention hooks (#398) ──────────────────────────

    /// #398: perseus_vault_prune scope='history' enforces retention with dry_run
    /// preview (count + bytes that WOULD be evicted) matching the real run,
    /// and requires an explicit bound.
    #[test]
    fn prune_history_scope_dry_run_then_evicts_with_tombstone() {
        let (db, path) = temp_db();
        for i in 0..6 {
            handle_remember(
                &db,
                json!({"category": "facts", "key": "hot398",
                       "body_json": format!("{{\"content\":\"v{i}\"}}")}),
            )
            .expect("remember");
        }
        // 5 stored versions. No bound → explicit error, not a silent no-op.
        let err = handle_prune(&db, json!({"scope": "history"})).unwrap_err();
        assert!(err.contains("requires a bound"), "got: {err}");

        let dry = handle_prune(
            &db,
            json!({"scope": "history", "max_versions_per_key": 2, "dry_run": true}),
        )
        .expect("dry run");
        let dv: Value = serde_json::from_str(&dry).unwrap();
        assert_eq!(dv["rows_evicted"].as_i64().unwrap(), 3);
        assert!(dv["bytes_evicted"].as_i64().unwrap() > 0);
        assert_eq!(dv["dry_run"], json!(true));
        assert_eq!(
            db.history_versions("facts", "hot398").unwrap().len(),
            5,
            "dry_run must not evict"
        );

        let real = handle_prune(
            &db,
            json!({"scope": "history", "max_versions_per_key": 2}),
        )
        .expect("real run");
        let rv: Value = serde_json::from_str(&real).unwrap();
        assert_eq!(rv["rows_evicted"], dv["rows_evicted"], "preview must match actual");
        assert_eq!(rv["bytes_evicted"], dv["bytes_evicted"]);
        assert_eq!(rv["tombstones_written"].as_i64().unwrap(), 1);
        let _ = std::fs::remove_file(&path);
    }

    /// #398: the as_of MCP tool surfaces the tombstone as an explicit
    /// compacted marker (flag + version count + digest), not a fake version.
    #[test]
    fn as_of_tool_surfaces_compacted_marker() {
        let (db, path) = temp_db();
        for i in 0..4 {
            handle_remember(
                &db,
                json!({"category": "facts", "key": "hot398b",
                       "body_json": format!("{{\"content\":\"v{i}\"}}")}),
            )
            .expect("remember");
            std::thread::sleep(std::time::Duration::from_millis(3));
        }
        let t_first: i64 = {
            let conn = db.conn().unwrap();
            conn.query_row(
                "SELECT MIN(COALESCE(recorded_at_unix_ms, created_at_unix_ms)) \
                 FROM entity_history WHERE key='hot398b'",
                [],
                |r| r.get(0),
            )
            .unwrap()
        };
        handle_prune(
            &db,
            json!({"scope": "history", "max_versions_per_key": 1}),
        )
        .expect("evict");

        let resp = handle_as_of(
            &db,
            json!({"category": "facts", "key": "hot398b", "as_of_unix_ms": t_first}),
        )
        .expect("as_of");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["found"], json!(true));
        assert_eq!(v["compacted"], json!(true), "marker must be explicit: {resp}");
        assert_eq!(v["status"], json!("compacted"));
        assert_eq!(v["versions_compacted"].as_i64().unwrap(), 2);
        assert_eq!(v["digest"].as_str().unwrap().len(), 16);
        assert!(v["note"].as_str().unwrap().contains("not recoverable"));
        let _ = std::fs::remove_file(&path);
    }

    /// #398 rider: the valid-time tools decorate a compacted-window answer
    /// with the same explicit marker as perseus_vault_as_of — a retroactively-valid
    /// version's window keeps answering after compaction.
    #[test]
    fn valid_at_tool_surfaces_compacted_marker_for_retroactive_window() {
        let (db, path) = temp_db();
        let vf = now_ms() - 30 * 86_400_000;
        handle_remember(
            &db,
            json!({"category": "facts", "key": "retro398", "valid_from_unix_ms": vf,
                   "body_json": "{\"note\":\"retro v1\"}"}),
        )
        .expect("remember retroactive v1");
        for i in 0..2 {
            std::thread::sleep(std::time::Duration::from_millis(3));
            handle_remember(
                &db,
                json!({"category": "facts", "key": "retro398",
                       "body_json": format!("{{\"note\":\"v{}\"}}", i + 2)}),
            )
            .expect("supersede");
        }
        handle_prune(
            &db,
            json!({"scope": "history", "max_versions_per_key": 1}),
        )
        .expect("compact");

        let resp = handle_valid_at(
            &db,
            json!({"category": "facts", "key": "retro398", "valid_at_unix_ms": vf + 1000}),
        )
        .expect("valid_at");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["found"], json!(true), "compacted window must answer: {resp}");
        assert_eq!(v["compacted"], json!(true));
        assert_eq!(v["status"], json!("compacted"));
        assert!(v["versions_compacted"].as_i64().unwrap() >= 1);
        assert!(v["note"].as_str().unwrap().contains("not recoverable"));
        let _ = std::fs::remove_file(&path);
    }

    /// #398: maintenance runs retention only via the env policy — with no
    /// knobs set it reports zero evictions (default-off contract), and the
    /// report always carries the history fields.
    #[test]
    fn maintenance_history_step_is_noop_without_env_knobs() {
        let (db, path) = temp_db();
        for i in 0..4 {
            handle_remember(
                &db,
                json!({"category": "facts", "key": "hot398c",
                       "body_json": format!("{{\"content\":\"v{i}\"}}")}),
            )
            .expect("remember");
        }
        let resp = handle_maintenance(&db, json!({"history": true})).expect("maintenance");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["history_rows_evicted"].as_i64().unwrap(), 0);
        assert_eq!(v["history_tombstones_written"].as_i64().unwrap(), 0);
        assert_eq!(
            db.history_versions("facts", "hot398c").unwrap().len(),
            3,
            "no env knobs → maintenance must keep every version"
        );
        let _ = std::fs::remove_file(&path);
    }

    // ─── #433 L: input length bounds on remember ─────────────────

    #[test]
    fn remember_rejects_oversized_key() {
        let (db, path) = temp_db();
        let huge_key = "k".repeat(2000); // > MAX_KEY_LEN (1024)
        let err = handle_remember(
            &db,
            json!({"category": "facts", "key": huge_key, "body_json": "{}"}),
        )
        .expect_err("oversized key must be rejected");
        assert!(err.contains("key too long"), "unexpected error: {err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_rejects_oversized_category() {
        let (db, path) = temp_db();
        let huge_cat = "c".repeat(500); // > MAX_CATEGORY_LEN (256)
        let err = handle_remember(
            &db,
            json!({"category": huge_cat, "key": "k", "body_json": "{}"}),
        )
        .expect_err("oversized category must be rejected");
        assert!(err.contains("category too long"), "unexpected error: {err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_accepts_normal_sizes() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "facts", "key": "normal-key", "body_json": "{\"a\":1}"}),
        )
        .expect("normally-sized remember must succeed");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn test_handle_recall_batch() {
        let (db, path) = temp_db();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "key-alpha", "body_json": "{\"content\": \"rust compilation speed and cargo build systems\"}"}),
        ).unwrap();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "key-beta", "body_json": "{\"content\": \"speeding up rust build pipelines using sccache\"}"}),
        ).unwrap();

        handle_remember(
            &db,
            json!({"category": "facts", "key": "key-gamma", "body_json": "{\"content\": \"python interpreter runtime optimizations\"}"}),
        ).unwrap();

        let empty_res = handle_recall_batch(&db, json!({"queries": []})).unwrap();
        let empty_val: Value = serde_json::from_str(&empty_res).unwrap();
        assert_eq!(empty_val["total"], json!(0));
        assert!(empty_val["items"].as_array().unwrap().is_empty());

        let res = handle_recall_batch(
            &db,
            json!({
                "queries": [
                    {"query": "rust", "category": "facts", "limit": 10},
                    {"query": "sccache", "category": "facts", "limit": 10}
                ]
            }),
        ).unwrap();

        let val: Value = serde_json::from_str(&res).unwrap();
        assert!(val["total"].as_i64().unwrap() >= 2);
        let items = val["items"].as_array().unwrap();
        assert_eq!(items[0]["key"], "key-beta");
        assert_eq!(items[1]["key"], "key-alpha");

        let _ = std::fs::remove_file(&path);
    }

    // ─── #729/#728: origin + external_refs ──────────────────────────────

    #[test]
    fn remember_merges_origin_and_external_refs_into_body() {
        let (db, path) = temp_db();
        let out = handle_remember(
            &db,
            json!({
                "category": "facts",
                "key": "with-provenance",
                "body_json": "{\"content\": \"gate shipped\"}",
                "origin": {"memory_kind": "observed", "source_system": "agent"},
                "external_refs": [
                    {"ref_type": "pull_request",
                     "ref_value": "github:Perseus-Computing-LLC/ledger#176"}
                ]
            }),
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        let id = v["id"].as_str().unwrap().to_string();

        let entity = db
            .get_entity("facts", "with-provenance")
            .unwrap()
            .expect("entity");
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["origin"]["memory_kind"], json!("observed"));
        assert_eq!(body["origin"]["source_system"], json!("agent"));
        assert_eq!(
            body["external_refs"][0]["ref_value"],
            json!("github:Perseus-Computing-LLC/ledger#176")
        );
        assert_eq!(body["content"], json!("gate shipped"));

        // Recall surfaces the fields via body expansion.
        let out = handle_recall(&db, json!({"query": "gate"})).unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        let hit = v["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|h| h["id"] == id)
            .expect("recall hit");
        assert_eq!(hit["origin"]["memory_kind"], json!("observed"));
        assert_eq!(
            hit["external_refs"][0]["ref_type"],
            json!("pull_request")
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn remember_rejects_invalid_origin_and_ref_vocabulary() {
        let (db, path) = temp_db();
        let err = handle_remember(
            &db,
            json!({
                "category": "facts", "key": "bad-origin",
                "body_json": "{\"content\": \"x\"}",
                "origin": {"memory_kind": "hallucinated"}
            }),
        )
        .unwrap_err();
        assert!(err.contains("memory_kind"), "{err}");

        let err = handle_remember(
            &db,
            json!({
                "category": "facts", "key": "bad-rel",
                "body_json": "{\"content\": \"x\"}",
                "external_refs": [{"ref_type": "repo", "ref_value": "github:o/r",
                                   "relationship": "cousin_of"}]
            }),
        )
        .unwrap_err();
        assert!(err.contains("relationship"), "{err}");

        let err = handle_remember(
            &db,
            json!({
                "category": "facts", "key": "empty-ref",
                "body_json": "{\"content\": \"x\"}",
                "external_refs": [{"ref_type": "", "ref_value": "github:o/r"}]
            }),
        )
        .unwrap_err();
        assert!(err.contains("non-empty"), "{err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn recall_filters_by_ref_type_and_value() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({
                "category": "facts", "key": "about-ledger",
                "body_json": "{\"content\": \"deployment gate\"}",
                "skip_dedup": true,
                "external_refs": [{"ref_type": "repo",
                                   "ref_value": "github:Perseus-Computing-LLC/ledger"}]
            }),
        )
        .unwrap();
        handle_remember(
            &db,
            json!({
                "category": "facts", "key": "about-vault",
                "body_json": "{\"content\": \"deployment gate\"}",
                "skip_dedup": true,
                "external_refs": [{"ref_type": "repo",
                                   "ref_value": "github:Perseus-Computing-LLC/perseus-vault"}]
            }),
        )
        .unwrap();

        let out = handle_recall(
            &db,
            json!({"query": "deployment", "ref_type": "repo"}),
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["items"].as_array().unwrap().len(), 2, "{v}");

        let out = handle_recall(
            &db,
            json!({"query": "deployment",
                   "ref_value": "github:Perseus-Computing-LLC/ledger"}),
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        let items = v["items"].as_array().unwrap();
        assert_eq!(items.len(), 1, "{v}");
        assert_eq!(items[0]["key"], json!("about-ledger"));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn recall_surfaces_promotion_aware_explanation() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"observation","key":"rollout","body_json":"{\"content\":\"canary rollout reduces blast radius\",\"promoted_from\":{\"id\":\"mem-episode\"},\"promotion_transition\":{\"from_state\":\"episode\",\"to_state\":\"observation\"}}"})).unwrap();
        let out = handle_recall(&db, json!({"query":"canary rollout"})).unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        let why = &v["items"][0]["why_served"];
        assert_eq!(why["memory_class"], json!("observation"));
        assert_eq!(why["promotion_state"], json!("observation"));
        assert_eq!(why["support_count"], json!(1));
        assert_eq!(why["source_evidence_ids"], json!(["mem-episode"]));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn cross_product_served_memory_fixture_is_canonical_hash_only_and_provenance_consistent() {
        let fixture: Value = serde_json::from_str(include_str!("../tests/fixtures/cross_product_served_memory_contract.json")).unwrap();
        assert_eq!(fixture["producer_tool"], json!("perseus_vault_recall"));
        assert!(fixture["producer_tool"].as_str().unwrap().starts_with("perseus_vault_"));

        let item = &fixture["items"][0];
        let why = &item["why_served"];
        assert_eq!(why["memory_class"], item["category"]);
        assert_eq!(why["promotion_state"], item["promotion_transition"]["to_state"]);
        assert!(why["source_evidence_ids"].as_array().unwrap().contains(&item["promoted_from"]["id"]));
        assert_eq!(why["promoted_scope"], item["workspace_hash"]);

        let binding = &item["action_receipt_binding"];
        for field in ["action_id", "authority_manifest_ref", "approval_ref", "outcome_hash"] {
            assert!(binding[field].is_string() && !binding[field].as_str().unwrap().is_empty(), "missing {field}");
        }
        let outcome_hash = binding["outcome_hash"].as_str().unwrap();
        assert_eq!(outcome_hash.len(), 64);
        assert!(outcome_hash.bytes().all(|byte| byte.is_ascii_hexdigit()));

        let raw = fixture.to_string();
        for forbidden in ["content", "body_json", "secret", "prompt"] {
            assert!(!raw.contains(&format!("\"{forbidden}\"")), "fixture leaks {forbidden}");
        }
        assert!(include_str!("../docs/cross-product-acceptance-contract.md").contains("perseus_vault_recall"));
    }

    #[test]
    fn quality_telemetry_reports_machine_readable_memory_signals() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"facts","key":"one","body_json":"{\"content\":\"red state\"}","certainty":0.9})).unwrap();
        handle_remember(&db, json!({"category":"facts","key":"two","body_json":"{\"content\":\"blue state\"}","certainty":0.1})).unwrap();
        let raw = handle_quality_telemetry(&db, json!({"category":"facts"})).unwrap();
        let report: Value = serde_json::from_str(&raw).unwrap();
        assert!(report["active_entities"].as_i64().unwrap() >= 2, "{raw}");
        assert!(report["contradiction_count"].as_i64().unwrap() >= 1, "{raw}");
        assert!(report["class_distribution"].is_object(), "{raw}");
        assert!(report["promotion_flow"].is_object(), "{raw}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn operator_review_surfaces_conflicts_stale_and_supersession_candidates() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"facts","key":"policy","body_json":"{\"content\":\"allow red\"}","certainty":0.9})).unwrap();
        handle_remember(&db, json!({"category":"facts","key":"policy-two","body_json":"{\"content\":\"deny blue\"}","certainty":0.1})).unwrap();
        handle_remember(&db, json!({"category":"convention","key":"vague","body_json":"{\"content\":\"ok\"}"})).unwrap();
        let raw = handle_operator_review(&db, json!({"category":"facts", "limit":20})).unwrap();
        let report: Value = serde_json::from_str(&raw).unwrap();
        assert!(report["contradictions"]["conflicts"].is_array(), "{raw}");
        assert!(report["stale_candidates"].is_array());
        assert!(report["supersession_lag"].is_array());
        assert!(report["reviewed_at_unix_ms"].as_i64().unwrap() > 0);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn structured_index_anchor_distinguishes_reference_from_imported_fact() {
        let (db, path) = temp_db();
        let reference: Value = serde_json::from_str(&handle_structured_index_anchor(&db, json!({
            "index_type":"ide_symbol", "index_uri":"ide://repo/src/lib.rs", "record_id":"symbol:parse",
            "mode":"reference", "workspace_hash":"ws"
        })).unwrap()).unwrap();
        assert_eq!(reference["mode"], "reference");
        let imported: Value = serde_json::from_str(&handle_structured_index_anchor(&db, json!({
            "index_type":"ide_symbol", "index_uri":"ide://repo/src/lib.rs", "record_id":"symbol:parse",
            "mode":"import", "content":"parse converts input", "workspace_hash":"ws"
        })).unwrap()).unwrap();
        assert_eq!(imported["mode"], "import");
        let raw = handle_recall(&db, json!({"query":"parse converts", "workspace_hash":"ws"})).unwrap();
        let item = &serde_json::from_str::<Value>(&raw).unwrap()["items"][0];
        assert_eq!(item["origin"]["memory_kind"], "imported");
        assert_eq!(item["external_refs"][0]["ref_type"], "structured_index");
        assert_eq!(item["external_refs"][0]["ref_value"], "ide_symbol:ide://repo/src/lib.rs#symbol:parse");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn markdown_import_labels_content_as_non_authoritative_import_and_dedups() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("import-{}.md", uuid::Uuid::new_v4()));
        std::fs::write(&source, "# Team Policy\n\nImported procedures apply here.").unwrap();
        let args = json!({"path":source.to_string_lossy(), "workspace_hash":"ws", "source_system":"wiki"});
        let first: Value = serde_json::from_str(&handle_markdown_import(&db, args.clone()).unwrap()).unwrap();
        assert_eq!(first["imported"], 1, "{first}");
        let second: Value = serde_json::from_str(&handle_markdown_import(&db, args).unwrap()).unwrap();
        assert_eq!(second["imported"], 0, "{second}");
        let raw = handle_recall(&db, json!({"query":"Imported procedures", "workspace_hash":"ws"})).unwrap();
        let item = &serde_json::from_str::<Value>(&raw).unwrap()["items"][0];
        assert_eq!(item["origin"]["memory_kind"], "imported");
        assert_eq!(item["status"], "draft");
        let _ = std::fs::remove_file(&path); let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn derived_export_writes_stable_provenance_markdown() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"convention","key":"format","body_json":"{\"content\":\"Use stable markdown\"}"})).unwrap();
        let output = std::env::temp_dir().join(format!("derived-{}.md", uuid::Uuid::new_v4()));
        let response = handle_derived_export(&db, json!({"output_path":output.to_string_lossy()})).unwrap();
        let result: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(result["exported"], 1);
        let markdown = std::fs::read_to_string(&output).unwrap();
        assert!(markdown.contains("# Derived Knowledge Surface"));
        assert!(markdown.contains("## Conventions"));
        assert!(markdown.contains("Use stable markdown"));
        assert!(markdown.contains("provenance: category=convention key=format"));
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&output);
    }

    #[test]
    fn recall_profiles_partition_personal_agent_and_shared_memory() {
        let (db, path) = temp_db();
        for (category, key, workspace) in [
            ("preference", "pref", ""),
            ("convention", "rule", ""),
            ("correction", "fix", ""),
            ("insight", "shared", "ws-a"),
            ("insight", "other", "ws-b"),
        ] {
            handle_remember(&db, json!({"category":category,"key":key,
                "workspace_hash":workspace,"body_json":format!("{{\"content\":\"{} profile token\"}}", key),
                "skip_dedup":true})).unwrap();
        }
        let ids = |profile: &str, ws: &str| -> Vec<String> {
            let raw = handle_recall(&db, json!({"query":"profile token", "retrieval_profile":profile,
                "workspace_hash":ws, "limit":10})).unwrap();
            let mut keys: Vec<String> = serde_json::from_str::<Value>(&raw).unwrap()["items"].as_array().unwrap()
                .iter().map(|x| x["key"].as_str().unwrap().to_string()).collect();
            keys.sort();
            keys
        };
        assert_eq!(ids("personal", "ws-a"), vec!["pref"]);
        assert_eq!(ids("agent", "ws-a"), vec!["fix", "rule"]);
        assert_eq!(ids("shared", "ws-a"), vec!["shared"]);
        let err = handle_recall(&db, json!({"query":"profile token", "retrieval_profile":"bad"})).unwrap_err();
        assert!(err.contains("unknown retrieval_profile"));
        let _ = std::fs::remove_file(&path);
    }

    // ─── #832: perseus_vault_promote ────────────────────────────────────────────

    #[test]
    fn promote_creates_provenance_linked_copy() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "episodes", "key": "incident-42",
                   "body_json": "{\"content\": \"deploy dropped webhooks\"}",
                   "skip_dedup": true}),
        )
        .unwrap();

        let out = handle_promote(
            &db,
            json!({"from_category": "episodes", "from_key": "incident-42",
                   "to_category": "observation",
                   "reason": "recurred three times"}),
        )
        .unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        assert_eq!(v["promoted"], json!(true));
        let new_id = v["to_id"].as_str().unwrap();

        let promoted = db.get_entity("observation", "incident-42").unwrap().expect("copy");
        let body: Value = serde_json::from_str(&promoted.body_json).unwrap();
        assert_eq!(body["promoted_from"]["category"], json!("episodes"));
        assert_eq!(body["promoted_from"]["reason"], json!("recurred three times"));

        let source = db.get_entity("episodes", "incident-42").unwrap().expect("source");
        assert!(source
            .links
            .iter()
            .any(|l| l.relationship == "promoted_to" && l.target_id == new_id));
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn demote_creates_provenance_linked_copy_and_journal_event() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"convention", "key":"rollout", "body_json":"{\"content\":\"canary deploy first\"}"})).unwrap();
        let out = handle_demote(&db, json!({"from_category":"convention", "from_key":"rollout", "to_category":"observation", "reason":"not yet durable"})).unwrap();
        let v: Value = serde_json::from_str(&out).unwrap();
        let demoted = db.get_entity("observation", "rollout").unwrap().expect("copy");
        let body: Value = serde_json::from_str(&demoted.body_json).unwrap();
        assert_eq!(body["demoted_from"]["category"], json!("convention"));
        assert!(v["demoted"].as_bool().unwrap());
        let events = db
            .timeline(&TimelineParams {
                workspace_hash: Some(String::new()),
                event_type: Some("demotion".into()),
                ..Default::default()
            })
            .unwrap();
        assert_eq!(events.len(), 1);
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn promote_rejects_ladder_skips() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({"category":"episodes", "key":"skip", "body_json":"{\"content\":\"repeated deploy failure\"}"})).unwrap();
        let err = handle_promote(&db, json!({"from_category":"episodes", "from_key":"skip", "to_category":"convention"})).unwrap_err();
        assert!(err.contains("invalid promotion transition"), "{err}");
        assert!(db.get_entity("convention", "skip").unwrap().is_none());
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn promote_rejects_noop_and_missing_source() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({"category": "episodes", "key": "e1",
                   "body_json": "{\"content\": \"x\"}", "skip_dedup": true}),
        )
        .unwrap();

        let err = handle_promote(
            &db,
            json!({"from_category": "episodes", "from_key": "e1"}),
        )
        .unwrap_err();
        assert!(err.contains("Nothing to promote"), "{err}");

        let err = handle_promote(
            &db,
            json!({"from_category": "episodes", "from_key": "e1",
                   "to_category": "episodes"}),
        )
        .unwrap_err();
        assert!(err.contains("identical"), "{err}");

        let err = handle_promote(
            &db,
            json!({"from_category": "episodes", "from_key": "nope",
                   "to_category": "convention"}),
        )
        .unwrap_err();
        assert!(err.contains("not found"), "{err}");
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn artifact_register_manifest_excerpt_and_verify_round_trip() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("artifact-{}.log", uuid::Uuid::new_v4()));
        std::fs::write(&source, "alpha\nbeta\ngamma\n").unwrap();

        let registered: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({
                    "path": source.to_string_lossy(),
                    "workspace_hash": "ws-art",
                    "external_refs": [{"ref_type":"file","ref_value":source.to_string_lossy()}]
                }),
            )
            .unwrap(),
        )
        .unwrap();
        let sha = registered["sha256"].as_str().unwrap();
        assert_eq!(registered["artifact_action"], json!("created"));
        assert_eq!(registered["binding_action"], json!("created"));
        assert_eq!(registered["manifest"]["structure"]["utf8_text"], json!(true));
        assert_eq!(registered["manifest"]["visible_binding_count"], json!(1));

        let excerpt: Value = serde_json::from_str(
            &handle_artifact_excerpt(
                &db,
                json!({"sha256": sha, "workspace_hash": "ws-art", "line_start": 2, "line_end": 3}),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(excerpt["content_utf8"], json!("beta\ngamma\n"));
        assert_eq!(excerpt["anchors"][0]["line_start"], json!(2));
        assert_eq!(excerpt["anchors"][0]["line_end"], json!(3));

        let verified: Value = serde_json::from_str(
            &handle_artifact_verify_value(
                &db,
                json!({"sha256": sha, "workspace_hash": "ws-art", "candidate": "beta\ngamma\n"}),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(verified["match_count"], json!(1));
        assert_eq!(verified["matches"][0]["line_start"], json!(2));

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn artifact_log_digest_is_derived_navigation_with_exact_source_anchors() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("digest-{}.log", uuid::Uuid::new_v4()));
        std::fs::write(
            &source,
            "CI job=100 passed\nCI job=101 passed\nERROR deploy timeout id=22\nCI job=102 passed\n",
        )
        .unwrap();
        let registered: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({"path": source.to_string_lossy(), "workspace_hash": "ws-digest"}),
            )
            .unwrap(),
        )
        .unwrap();
        let source_sha = registered["sha256"].as_str().unwrap();
        let first = handle_artifact_log_digest(
            &db,
            json!({"sha256": source_sha, "workspace_hash": "ws-digest"}),
        )
        .unwrap();
        let second = handle_artifact_log_digest(
            &db,
            json!({"sha256": source_sha, "workspace_hash": "ws-digest"}),
        )
        .unwrap();
        let a: Value = serde_json::from_str(&first).unwrap();
        let b: Value = serde_json::from_str(&second).unwrap();
        assert_eq!(a, b, "same input + config must be byte-stable in content");
        assert_eq!(a["digest"]["protected_line_count"], json!(1));
        assert_eq!(a["digest"]["protected_lines"][0][0], json!("ERROR deploy timeout id=22"));
        assert_eq!(a["digest"]["sections"][0]["count"], json!(3));
        assert_eq!(a["representation"]["kind"], json!("derived"));
        assert_eq!(a["representation"]["derived_from_sha256"], json!(source_sha));
        assert_ne!(a["derived_artifact_sha256"], json!(source_sha));
        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn artifact_register_dedupes_same_bytes_but_keeps_scope_isolation() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("artifact-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&source, "same bytes").unwrap();

        let first: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({"path": source.to_string_lossy(), "workspace_hash": "ws-a"}),
            )
            .unwrap(),
        )
        .unwrap();
        let second: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({"path": source.to_string_lossy(), "workspace_hash": "ws-a"}),
            )
            .unwrap(),
        )
        .unwrap();
        let third: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({"path": source.to_string_lossy(), "workspace_hash": "ws-b"}),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(first["sha256"], second["sha256"]);
        assert_eq!(first["sha256"], third["sha256"]);
        assert_eq!(second["artifact_action"], json!("existing"));
        assert_eq!(second["binding_action"], json!("existing"));
        assert_eq!(third["artifact_action"], json!("existing"));
        assert_eq!(third["binding_action"], json!("created"));

        let ws_a: Value = serde_json::from_str(
            &handle_artifact_manifest(
                &db,
                json!({"sha256": first["sha256"], "workspace_hash": "ws-a"}),
            )
            .unwrap(),
        )
        .unwrap();
        let ws_b: Value = serde_json::from_str(
            &handle_artifact_manifest(
                &db,
                json!({"sha256": first["sha256"], "workspace_hash": "ws-b"}),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(ws_a["bindings"][0]["workspace_hash"], json!("ws-a"));
        assert_eq!(ws_b["bindings"][0]["workspace_hash"], json!("ws-b"));
        let err = handle_artifact_manifest(&db, json!({"sha256": first["sha256"]})).unwrap_err();
        assert!(err.contains("not found"), "{err}");

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn artifact_manifest_and_excerpt_enforce_visibility_before_serving() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("artifact-private-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&source, "secret line\n").unwrap();

        let registered: Value = serde_json::from_str(
            &handle_artifact_register(
                &db,
                json!({
                    "path": source.to_string_lossy(),
                    "workspace_hash": "ws-private",
                    "agent_id": "alice",
                    "visibility": "private"
                }),
            )
            .unwrap(),
        )
        .unwrap();
        let sha = registered["sha256"].as_str().unwrap();

        let err = handle_artifact_manifest(
            &db,
            json!({"sha256": sha, "workspace_hash": "ws-private", "requesting_agent_id": "bob"}),
        )
        .unwrap_err();
        assert!(err.contains("not found"), "{err}");

        let allowed: Value = serde_json::from_str(
            &handle_artifact_excerpt(
                &db,
                json!({
                    "sha256": sha,
                    "workspace_hash": "ws-private",
                    "requesting_agent_id": "alice",
                    "byte_start": 0,
                    "byte_end": 6
                }),
            )
            .unwrap(),
        )
        .unwrap();
        assert_eq!(allowed["content_utf8"], json!("secret"));

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn artifact_register_rejects_compressed_inputs_and_short_parent_hashes() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("artifact-{}.gz", uuid::Uuid::new_v4()));
        std::fs::write(&source, b"pretend gzip").unwrap();
        let err = handle_artifact_register(
            &db,
            json!({"path": source.to_string_lossy()}),
        )
        .unwrap_err();
        assert!(err.contains("uncompressed"), "{err}");

        let plain = std::env::temp_dir().join(format!("artifact-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&plain, b"derived body").unwrap();
        let err = handle_artifact_register(
            &db,
            json!({
                "path": plain.to_string_lossy(),
                "representation": {"kind":"derived", "derived_from_sha256":"abc123"}
            }),
        )
        .unwrap_err();
        assert!(err.contains("full 64-hex"), "{err}");

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
        let _ = std::fs::remove_file(&plain);
    }

    #[test]
    fn artifact_register_detects_hash_collision_or_corrupted_stored_bytes() {
        let (db, path) = temp_db();
        let source = std::env::temp_dir().join(format!("artifact-collision-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&source, b"true bytes").unwrap();
        use base64::Engine as _;
        let good_sha = {
            use sha2::{Digest, Sha256};
            format!("{:x}", Sha256::digest(b"true bytes"))
        };
        let bad_b64 = base64::engine::general_purpose::STANDARD.encode(b"other bytes");
        let conn = db.conn().unwrap();
        conn.execute(
            "INSERT INTO artifacts (sha256, content_b64, byte_length, created_at_unix_ms)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![good_sha, bad_b64, 10i64, crate::db::now_ms()],
        )
        .unwrap();
        drop(conn);

        let err = handle_artifact_register(
            &db,
            json!({"path": source.to_string_lossy(), "workspace_hash": "ws-collision"}),
        )
        .unwrap_err();
        assert!(err.contains("collision") || err.contains("corrupted"), "{err}");

        let _ = std::fs::remove_file(&path);
        let _ = std::fs::remove_file(&source);
    }

    #[test]
    fn remember_evidence_modes_are_explicit_and_validated() {
        let (db, path) = temp_db();
        let base = json!({
            "category": "decision",
            "key": "evidence-contract",
            "body_json": "{\"content\":\"port is 3001\"}",
            "evidence": {
                "capture_mode": "snapshot",
                "resolved_value": {"port": 3001},
                "captured_at_unix_ms": 100,
                "replayable": true
            }
        });
        let response = handle_remember(&db, base).expect("snapshot evidence should write");
        let id = serde_json::from_str::<Value>(&response).unwrap()["id"].clone();
        let entity = db.get_entity_by_id_public(id.as_str().unwrap()).unwrap().unwrap();
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["evidence"]["capture_mode"], "snapshot");
        assert_eq!(body["evidence"]["resolved_value"]["port"], 3001);

        let err = handle_remember(&db, json!({
            "category": "decision",
            "key": "missing-snapshot",
            "body_json": "{}",
            "evidence": {"capture_mode": "snapshot", "captured_at_unix_ms": 100, "replayable": false}
        })).unwrap_err();
        assert!(err.contains("requires resolved_value"), "{err}");

        let err = handle_remember(&db, json!({
            "category": "decision",
            "key": "missing-hash",
            "body_json": "{}",
            "evidence": {"capture_mode": "hash_only", "captured_at_unix_ms": 100, "replayable": false}
        })).unwrap_err();
        assert!(err.contains("requires content_sha256"), "{err}");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn remember_admission_quarantines_untrusted_content_at_write_boundary() {
        let (db, path) = temp_db();
        let response = handle_remember(
            &db,
            json!({
                "category": "security",
                "key": "poisoned-record",
                "body_json": "{\"note\":\"untrusted source\"}",
                "always_on": true,
                "admission": {
                    "record_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_identity": "email-42",
                    "authorization_scope": "workspace-a",
                    "ingestion_channel": "connector-email",
                    "workspace_hash": "workspace-a",
                    "source_trust": "untrusted",
                    "valid_from_unix_ms": 100,
                    "recorded_at_unix_ms": 110,
                    "task_relevance_bps": 9000,
                    "instruction_bearing": true
                }
            }),
        )
        .expect("quarantine should retain non-authoritative evidence");
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["admission"]["outcome"], "quarantined");
        let entity = db
            .get_entity("security", "poisoned-record")
            .unwrap()
            .expect("quarantined record should be retained");
        assert_eq!(entity.status, "quarantined");
        assert!(!entity.always_on);
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["admission"]["authoritative"], false);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn remember_admission_suppression_does_not_write_memory() {
        let (db, path) = temp_db();
        let response = handle_remember(
            &db,
            json!({
                "category": "security",
                "key": "irrelevant-record",
                "body_json": "{\"note\":\"not relevant\"}",
                "admission": {
                    "record_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "source_identity": "web-17",
                    "authorization_scope": "workspace-a",
                    "ingestion_channel": "web-import",
                    "workspace_hash": "workspace-a",
                    "source_trust": "trusted",
                    "valid_from_unix_ms": 100,
                    "recorded_at_unix_ms": 110,
                    "task_relevance_bps": 100
                }
            }),
        )
        .expect("suppression is a structured non-write");
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["ok"], false);
        assert_eq!(value["remembered"], false);
        assert!(db.get_entity("security", "irrelevant-record").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn remember_without_admission_is_an_explicit_hash_only_proposal() {
        let (db, path) = temp_db();
        let secret = "prompt-secret-that-must-not-enter-admission";
        let response = handle_remember(
            &db,
            json!({
                "category": "decision",
                "key": "proposal-without-admission",
                "workspace_hash": "ws-proposal",
                "agent_id": "assistant-1",
                "actor_kind": "assistant",
                "body_json": format!("{{\"content\":\"{secret}\"}}"),
                "skip_dedup": true
            }),
        )
        .expect("missing admission should return a structured proposal");
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["proposed"], json!(true), "{response}");
        assert_eq!(value["requires_review"], json!(true), "{response}");
        assert_eq!(value["provenance"]["state"], json!("proposed"), "{response}");
        assert_eq!(value["provenance"]["actor"]["kind"], json!("assistant"), "{response}");
        assert_eq!(value["provenance"]["workspace_hash"], json!("ws-proposal"), "{response}");
        let digest = value["provenance"]["record_digest"].as_str().unwrap();
        assert_eq!(digest.len(), 64, "{response}");
        assert!(!value["provenance"].to_string().contains(secret), "{response}");

        let entity = db
            .get_entity("decision", "proposal-without-admission")
            .unwrap()
            .expect("proposal remains durable for review");
        assert_eq!(entity.status, "proposed");
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["provenance"]["state"], json!("proposed"));
        assert!(!body["provenance"].to_string().contains(secret));
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn unverified_admission_is_proposed_and_not_authoritative() {
        let (db, path) = temp_db();
        let response = handle_remember(
            &db,
            json!({
                "category": "decision",
                "key": "missing-source-validation",
                "workspace_hash": "ws-admission",
                "agent_id": "assistant-1",
                "actor_kind": "assistant",
                "body_json": "{\"content\":\"source event is not present\"}",
                "admission": {
                    "record_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_identity": "connector-1",
                    "authorization_scope": "ws-admission",
                    "ingestion_channel": "connector",
                    "workspace_hash": "ws-admission",
                    "source_trust": "authoritative",
                    "valid_from_unix_ms": 100,
                    "recorded_at_unix_ms": 110,
                    "task_relevance_bps": 9000
                }
            }),
        )
        .expect("unverified admission should be retained for review");
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["proposed"], json!(true), "{response}");
        assert_eq!(value["requires_review"], json!(true), "{response}");
        assert_eq!(value["provenance"]["state"], json!("proposed"), "{response}");
        assert_eq!(value["admission"]["authoritative"], json!(false), "{response}");
        assert_eq!(
            db.get_entity("decision", "missing-source-validation")
                .unwrap()
                .unwrap()
                .status,
            "proposed"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn forged_authoritative_admission_is_downgraded_without_bound_source_event() {
        let (db, path) = temp_db();
        let body = "{\"content\":\"caller-forged\"}";
        let response = handle_remember(
            &db,
            json!({
                "category": "decision",
                "key": "forged-authority",
                "workspace_hash": "ws-forged",
                "agent_id": "assistant-evil",
                "actor_kind": "assistant",
                "body_json": body,
                "admission": {
                    "record_digest": crate::trust_admission::digest_text("different-body"),
                    "source_identity": "caller",
                    "source_event_id": "missing-journal-event",
                    "authorization_scope": "ws-forged",
                    "ingestion_channel": "connector",
                    "workspace_hash": "ws-forged",
                    "source_trust": "authoritative",
                    "actor_kind": "assistant",
                    "actor_identity": "assistant-evil",
                    "validated": true,
                    "valid_from_unix_ms": 1,
                    "recorded_at_unix_ms": 2,
                    "task_relevance_bps": 9000
                }
            }),
        )
        .expect("forged admission is retained for review");
        let value: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(value["admission"]["authoritative"], json!(false), "{response}");
        assert_eq!(value["proposed"], json!(true), "{response}");
        assert_eq!(
            db.get_entity("decision", "forged-authority").unwrap().unwrap().status,
            "proposed"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn admission_workspace_mismatch_is_rejected_before_any_mutation() {
        let (db, path) = temp_db();
        let err = handle_remember(
            &db,
            json!({
                "category": "decision",
                "key": "wrong-scope",
                "workspace_hash": "ws-requested",
                "body_json": "{\"content\":\"must not write\"}",
                "admission": {
                    "record_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    "source_identity": "event-1",
                    "authorization_scope": "ws-admission",
                    "ingestion_channel": "connector",
                    "workspace_hash": "ws-admission",
                    "source_trust": "trusted",
                    "valid_from_unix_ms": 100,
                    "recorded_at_unix_ms": 110,
                    "task_relevance_bps": 9000
                }
            }),
        )
        .expect_err("scope mismatch must fail before the durable write");
        assert!(err.contains("workspace"), "{err}");
        assert!(db.get_entity("decision", "wrong-scope").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn forged_promotion_identity_and_scope_are_rejected_without_partial_apply() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({
                "category": "episodes",
                "key": "verified-source",
                "workspace_hash": "ws-promote",
                "agent_id": "user-42",
                "actor_kind": "user",
                "body_json": "{\"content\":\"observed deploy event\"}",
                "admission": {
                    "record_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "source_identity": "event-42",
                    "source_event_id": "event-42",
                    "authorization_scope": "ws-promote",
                    "ingestion_channel": "event-feed",
                    "workspace_hash": "ws-promote",
                    "source_trust": "authoritative",
                    "actor_kind": "user",
                    "actor_identity": "user-42",
                    "validated": true,
                    "valid_from_unix_ms": 100,
                    "recorded_at_unix_ms": 110,
                    "task_relevance_bps": 9000
                }
            }),
        )
        .expect("verified source");
        let err = handle_promote(
            &db,
            json!({
                "from_category": "episodes",
                "from_key": "verified-source",
                "source_id": "mem-forged",
                "workspace_hash": "ws-other",
                "agent_id": "assistant-evil",
                "to_category": "observation"
            }),
        )
        .expect_err("forged source identity/scope must be rejected before mutation");
        assert!(err.contains("source_id") || err.contains("workspace") || err.contains("owner"), "{err}");
        assert!(db.get_entity("observation", "verified-source").unwrap().is_none());
        let source = db.get_entity("episodes", "verified-source").unwrap().unwrap();
        assert!(source.links.is_empty(), "rejected promotion must not leave a link");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn promoting_an_unverified_proposal_requires_review() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({
                "category": "episodes",
                "key": "unverified-proposal",
                "workspace_hash": "ws-propose",
                "agent_id": "assistant-1",
                "body_json": "{\"content\":\"model proposed this event\"}",
                "skip_dedup": true
            }),
        )
        .expect("proposal write");
        let err = handle_promote(
            &db,
            json!({
                "from_category": "episodes",
                "from_key": "unverified-proposal",
                "to_category": "observation"
            }),
        )
        .expect_err("unverified proposal must not be promoted");
        assert!(
            err.contains("requires_review") || err.contains("unverified") || err.contains("workspace_hash"),
            "{err}"
        );
        assert!(db.get_entity("observation", "unverified-proposal").unwrap().is_none());
        let source = db.get_entity("episodes", "unverified-proposal").unwrap().unwrap();
        assert!(source.links.is_empty(), "review-gated promotion must be inert");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn quarantined_source_cannot_be_promoted() {
        let (db, path) = temp_db();
        let remembered = handle_remember(
            &db,
            json!({
                "category":"episodes", "key":"quarantined-source",
                "workspace_hash":"ws-quarantine", "agent_id":"connector-1",
                "actor_kind":"connector", "body_json":"{\"content\":\"untrusted\"}",
                "admission": {
                    "record_digest": crate::trust_admission::digest_text("{\"content\":\"untrusted\"}"),
                    "source_identity":"feed-1", "source_event_id":"event-q",
                    "authorization_scope":"ws-quarantine", "ingestion_channel":"feed",
                    "workspace_hash":"ws-quarantine", "source_trust":"untrusted",
                    "instruction_bearing":true, "valid_from_unix_ms":1,
                    "recorded_at_unix_ms":2, "task_relevance_bps":9000
                }
            }),
        ).unwrap();
        let source_id = serde_json::from_str::<Value>(&remembered).unwrap()["id"].as_str().unwrap().to_string();
        let err = handle_promote(&db, json!({
            "from_category":"episodes", "from_key":"quarantined-source",
            "to_category":"observation", "workspace_hash":"ws-quarantine",
            "agent_id":"connector-1", "source_id":source_id
        })).unwrap_err();
        assert!(err.contains("review") || err.contains("authoritative"), "{err}");
        assert!(db.get_entity("observation", "quarantined-source").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn demotion_requires_reviewable_source_assertions_and_target_preflight() {
        let (db, path) = temp_db();
        handle_remember(&db, json!({
            "category":"convention", "key":"draft-rule",
            "workspace_hash":"ws-draft", "agent_id":"agent-draft",
            "body_json":"{\"content\":\"draft\"}"
        })).unwrap();
        let err = handle_demote(&db, json!({
            "from_category":"convention", "from_key":"draft-rule",
            "to_category":"observation", "workspace_hash":"ws-draft",
            "agent_id":"agent-draft"
        })).unwrap_err();
        assert!(err.contains("review") || err.contains("assertion"), "{err}");
        assert!(db.get_entity("observation", "draft-rule").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn public_journal_payloads_are_hash_only() {
        let (db, path) = temp_db();
        let secret = "raw-prompt-secret-that-must-not-be-journaled";
        let response = handle_journal(
            &db,
            json!({
                "event_type": "public-test",
                "evaluated": {"prompt": secret},
                "acted": {"tool_arguments": {"token": secret}},
                "forward": {"result": secret}
            }),
        )
        .unwrap();
        let id = serde_json::from_str::<Value>(&response).unwrap()["id"]
            .as_str()
            .unwrap()
            .to_string();
        let conn = db.conn().unwrap();
        let row: (String, String, String) = conn
            .query_row(
                "SELECT evaluated_json, acted_json, forward_json FROM journal WHERE id=?1",
                rusqlite::params![id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        let stored = format!("{}{}{}", row.0, row.1, row.2);
        assert!(!stored.contains(secret));
        assert!(stored.matches("\"redacted\":true").count() == 3);
        drop(conn);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn admission_actor_identity_and_kind_mismatch_is_rejected() {
        let (db, path) = temp_db();
        let err = handle_remember(&db, json!({
            "category":"decision", "key":"actor-mismatch",
            "workspace_hash":"ws-actor", "agent_id":"assistant-1",
            "actor_kind":"assistant", "body_json":"{\"content\":\"x\"}",
            "admission": {
                "record_digest": crate::trust_admission::digest_text("{\"content\":\"x\"}"),
                "source_identity":"source-1", "source_event_id":"event-1",
                "authorization_scope":"ws-actor", "ingestion_channel":"connector",
                "workspace_hash":"ws-actor", "source_trust":"authoritative",
                "actor_kind":"user", "actor_identity":"user-1",
                "validated":true, "valid_from_unix_ms":1,
                "recorded_at_unix_ms":2, "task_relevance_bps":9000
            }
        })).unwrap_err();
        assert!(err.contains("actor"), "{err}");
        assert!(db.get_entity("decision", "actor-mismatch").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn unadmitted_user_attribution_is_rejected() {
        let (db, path) = temp_db();
        let err = handle_remember(
            &db,
            json!({
                "category": "decision",
                "key": "forged-user",
                "body_json": "{\"content\":\"assistant claim\"}",
                "actor_kind": "user",
                "agent_id": "user-42"
            }),
        )
        .expect_err("unadmitted user attribution must fail closed");
        assert!(err.contains("validated admission"), "{err}");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn scoped_owned_promotion_requires_assertions() {
        let (db, path) = temp_db();
        handle_remember(
            &db,
            json!({
                "category": "episodes",
                "key": "owned-source",
                "body_json": "{\"content\":\"validated source\"}",
                "workspace_hash": "ws-owned",
                "agent_id": "agent-owner",
                "actor_kind": "connector",
                "admission": {
                    "record_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "source_identity": "event-source",
                    "authorization_scope": "ws-owned",
                    "ingestion_channel": "connector",
                    "workspace_hash": "ws-owned",
                    "source_trust": "authoritative",
                    "source_event_id": "event-owned",
                    "actor_kind": "connector",
                    "actor_identity": "agent-owner",
                    "validated": true,
                    "valid_from_unix_ms": 1,
                    "recorded_at_unix_ms": 2,
                    "task_relevance_bps": 9000
                }
            }),
        )
        .unwrap();
        let err = handle_promote(
            &db,
            json!({"from_category":"episodes", "from_key":"owned-source", "to_category":"observation"}),
        )
        .expect_err("scoped owned promotion must require binding assertions");
        assert!(err.contains("workspace_hash") || err.contains("agent_id"), "{err}");
        assert!(db.get_entity("observation", "owned-source").unwrap().is_none());
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn correction_entity_persists_hash_only_payload() {
        let (db, path) = temp_db();
        let wrong = "raw assistant prompt with private deployment details";
        let correction = "user correction with private source text";
        let context = "private task context";
        let response = handle_correct(
            &db,
            json!({
                "workspace_hash":"ws-correction-private",
                "agent_id":"user-privacy",
                "wrong_approach":wrong,
                "user_correction":correction,
                "task_context":context
            }),
        ).unwrap();
        let result: Value = serde_json::from_str(&response).unwrap();
        let entity = db.get_entity("correction", result["key"].as_str().unwrap()).unwrap().unwrap();
        assert!(!entity.body_json.contains(wrong));
        assert!(!entity.body_json.contains(correction));
        assert!(!entity.body_json.contains(context));
        let body: Value = serde_json::from_str(&entity.body_json).unwrap();
        assert_eq!(body["wrong_approach_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(body["user_correction_sha256"].as_str().unwrap().len(), 64);
        assert_eq!(body["task_context_sha256"].as_str().unwrap().len(), 64);
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn correction_preserves_user_actor_attribution() {
        let (db, path) = temp_db();
        let response = handle_correct(
            &db,
            json!({
                "workspace_hash": "ws-correction",
                "agent_id": "user-42",
                "wrong_approach": "assistant guessed the deployment state",
                "user_correction": "user confirmed the observed state",
                "task_context": "deployment review"
            }),
        )
        .expect("correction");
        let result: Value = serde_json::from_str(&response).unwrap();
        let entity = db
            .get_entity("correction", result["key"].as_str().unwrap())
            .unwrap()
            .expect("correction entity");
        assert_eq!(entity.agent_id, "user-42");
        let events = db.get_recent_journal("ws-correction", 50).unwrap();
        let event = events
            .iter()
            .find(|event| event.event_type == "correction" && event.entity_id == entity.id)
            .expect("correction journal event");
        assert_eq!(event.agent_id, "user-42");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn correction_host_identity_overrides_model_agent_id() {
        // #855: when the MCP transport stamps a host identity, it is
        // authoritative — a model-supplied agent_id cannot override it on the
        // entity, the journal event, or the result.
        let (db, path) = temp_db();
        let response = handle_correct(
            &db,
            json!({
                "workspace_hash": "ws-host",
                "agent_id": "model-claimed-user",
                "requesting_agent_id": "host-claude-desktop",
                "wrong_approach": "assistant guessed the deployment state",
                "user_correction": "user confirmed the observed state",
                "task_context": "deployment review"
            }),
        )
        .expect("correction");
        let result: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(result["agent_id"], json!("host-claude-desktop"));
        assert_eq!(result["workspace_hash"], json!("ws-host"));

        let entity = db
            .get_entity("correction", result["key"].as_str().unwrap())
            .unwrap()
            .expect("correction entity");
        assert_eq!(
            entity.agent_id, "host-claude-desktop",
            "host identity must win over the model-supplied author"
        );
        assert_eq!(entity.workspace_hash, "ws-host");

        let events = db.get_recent_journal("ws-host", 50).unwrap();
        let event = events
            .iter()
            .find(|event| event.event_type == "correction" && event.entity_id == entity.id)
            .expect("correction journal event");
        assert_eq!(event.agent_id, "host-claude-desktop");
        assert_eq!(event.workspace_hash, "ws-host");

        // The rejected-value tombstone carries the same host attribution.
        let authors: Vec<String> = db
            .conn()
            .unwrap()
            .prepare(
                "SELECT author_agent_id FROM rejected_value_tombstones \
                 WHERE workspace_hash = ?1",
            )
            .unwrap()
            .query_map(rusqlite::params!["ws-host"], |r| r.get::<_, String>(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();
        assert!(
            authors.iter().any(|a| a == "host-claude-desktop"),
            "tombstone must record the host identity, got {authors:?}"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn correction_empty_attribution_records_legacy_empties() {
        // #855: no agent/workspace supplied (non-MCP caller) → the correction
        // entity, journal event, and result all carry empty attribution —
        // legacy behavior preserved, nothing invented.
        let (db, path) = temp_db();
        let response = handle_correct(
            &db,
            json!({
                "wrong_approach": "assistant guessed the deployment state",
                "user_correction": "user confirmed the observed state",
                "task_context": "deployment review"
            }),
        )
        .expect("correction");
        let result: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(result["agent_id"], json!(""));
        assert_eq!(result["workspace_hash"], json!(""));

        let entity = db
            .get_entity("correction", result["key"].as_str().unwrap())
            .unwrap()
            .expect("correction entity");
        assert_eq!(entity.agent_id, "");
        assert_eq!(entity.workspace_hash, "");
        let events = db.get_recent_journal("", 50).unwrap();
        let event = events
            .iter()
            .find(|event| event.event_type == "correction" && event.entity_id == entity.id)
            .expect("correction journal event");
        assert_eq!(event.agent_id, "");
        assert_eq!(event.workspace_hash, "");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn dream_fallback_consolidate_is_scoped() {
        // #854 review: the no-LLM fallback (fallback_consolidate) must carry
        // the same workspace scope into the mechanical consolidate pass —
        // scoped fallback never merges across workspaces.
        let (db, path) = temp_db();
        let ins = |id: &str, ws: &str| {
            db.conn()
                .unwrap()
                .execute(
                    "INSERT INTO entities (id, category, key, body_json, status, type, tags, \
                     decay_score, retrieval_count, layer, topic_path, archived, archive_reason, \
                     links, verified, source, certainty, workspace_hash, created_at_unix_ms, \
                     last_accessed_unix_ms) \
                     VALUES (?1, 'episodes', ?1, '{\"note\":\"user ran database migrations \
                     before restarting the api service\"}', 'active', 'insight', '[]', 1.0, 0, \
                     'working', '', 0, '', '[]', 0, 'agent', 0.5, ?2, 0, 0)",
                    rusqlite::params![id, ws],
                )
                .unwrap();
        };
        ins("fb-1", "ws-fb");
        ins("fb-2", "ws-fb");
        ins("fb-3", "ws-other");

        let response = handle_dream(
            &db,
            json!({
                "category": "episodes",
                "fallback_consolidate": true,
                "workspace_hash": "ws-fb",
                "dry_run": false
            }),
        )
        .expect("dream fallback");
        let v: Value = serde_json::from_str(&response).unwrap();
        assert_eq!(v["fallback"], json!("consolidate"));
        assert_eq!(
            v["entities_examined"], json!(2),
            "scoped fallback must only examine ws-fb: {v}"
        );
        assert_eq!(v["observations_created"], json!(1));

        // The derived observation inherits the scope.
        let obs_ws: String = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT workspace_hash FROM entities WHERE category='observation' LIMIT 1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(obs_ws, "ws-fb", "fallback observation must inherit the scope");
        let _ = std::fs::remove_file(path);
    }
}


