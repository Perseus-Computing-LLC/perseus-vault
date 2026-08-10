use serde::{Deserialize, Serialize};

/// An entity stored in the entities table.
/// Idempotent by UNIQUE(category, key) — INSERT OR REPLACE semantics.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Entity {
    pub id: String,
    pub category: String,
    pub key: String,
    pub body_json: String,
    #[serde(default = "default_status")]
    pub status: String,
    #[serde(rename = "type", default = "default_entity_type")]
    pub entity_type: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default = "default_decay_score")]
    pub decay_score: f64,
    #[serde(default)]
    pub retrieval_count: i64,
    #[serde(default = "default_layer")]
    pub layer: String,
    #[serde(default)]
    pub topic_path: String,
    #[serde(default)]
    pub archived: bool,
    #[serde(default)]
    pub archive_reason: String,
    #[serde(default)]
    pub links: Vec<MemoryLink>,
    #[serde(default)]
    pub verified: bool,
    #[serde(default = "default_source")]
    pub source: String,
    #[serde(default)]
    pub always_on: bool,
    /// Certainty for typed entities (0.0-1.0). Used by perseus_vault_conflicts:
    /// low-certainty entities on the same topic are a conflict signal.
    #[serde(default = "default_certainty")]
    pub certainty: f64,
    /// Workspace scope identifier (v1.2.0). Empty = global/unscoped.
    /// Entities are invisible across workspaces when a scope is set.
    #[serde(default)]
    pub workspace_hash: String,
    /// Agent identity (v1.2.0). Tracks which agent wrote this entity.
    /// Used for agent attribution and context filtering.
    #[serde(default)]
    pub agent_id: String,
    /// Visibility: 'private', 'workspace', or 'public' (v1.2.0)
    #[serde(default = "default_visibility")]
    pub visibility: String,
    pub created_at_unix_ms: i64,
    pub last_accessed_unix_ms: i64,
    /// Efficacy tracking (v2.10.0 — PMB-inspired follow-rate scoring). How many
    /// times this entity (typically a convention/insight/lesson) was confirmed
    /// or auto-detected as actually FOLLOWED vs missed by the agent.
    #[serde(default)]
    pub follow_count: i64,
    #[serde(default)]
    pub miss_count: i64,
    /// follow_count / (follow_count + miss_count); 0.0 when no attempts yet.
    #[serde(default)]
    pub follow_rate: f64,
    /// 'unverified' | 'useful' | 'dead' — set once enough attempts accrue.
    #[serde(default = "default_efficacy_status")]
    pub efficacy_status: String,
    /// Epistemic trust axis (#880), orthogonal to lifecycle `status`:
    /// 'candidate' | 'verified' | 'corroborated' | 'rejected' |
    /// 'defensively_recalled'. Default 'candidate' — a record may be useful
    /// without being established fact; verified/corroborated require
    /// admission evidence or operator promotion.
    #[serde(default = "default_epistemic_state")]
    pub epistemic_state: String,
    #[serde(skip)]
    #[allow(dead_code)]
    pub embedding: Option<Vec<f32>>,
    #[serde(skip, default)]
    pub _parsed_body: Option<serde_json::Value>,
}

impl Entity {
    pub fn to_json_expanded(&self) -> serde_json::Value {
        let mut val = serde_json::to_value(self).unwrap_or_else(|_| serde_json::json!({}));
        let body_val = self
            ._parsed_body
            .as_ref()
            .cloned()
            .or_else(|| serde_json::from_str::<serde_json::Value>(&self.body_json).ok());
        if let Some(serde_json::Value::Object(map)) = body_val {
            if let Some(obj) = val.as_object_mut() {
                for (k, v) in map {
                    if k != "id" && k != "category" && k != "key" && k != "body_json" && k != "type"
                    {
                        obj.insert(k, v);
                    }
                }
            }
        }
        val
    }
}

fn default_status() -> String {
    "active".to_string()
}

fn default_entity_type() -> String {
    "insight".to_string()
}

fn default_decay_score() -> f64 {
    1.0
}

fn default_layer() -> String {
    "working".to_string()
}

fn default_source() -> String {
    "agent".to_string()
}

fn default_certainty() -> f64 {
    0.5
}

fn default_efficacy_status() -> String {
    "unverified".to_string()
}

/// Epistemic trust axis default (#880): useful-but-unverified until admission
/// evidence or operator promotion proves the claim.
pub fn default_epistemic_state() -> String {
    "candidate".to_string()
}

/// The canonical epistemic state vocabulary (#880).
pub const EPISTEMIC_STATES: [&str; 5] = [
    "candidate",
    "verified",
    "corroborated",
    "rejected",
    "defensively_recalled",
];

/// Default recall trust weight. Non-zero so verified sources are preferred
/// over unverified AI drafts everywhere by default; kept low so it acts as a
/// tie-breaker rather than overriding relevance/recency.
pub fn default_trust_weight() -> f64 {
    0.15
}

fn default_visibility() -> String {
    "workspace".to_string()
}

/// A link between two entities. Stored as JSON array in entities.links.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MemoryLink {
    pub target_id: String,
    #[serde(default)]
    pub relationship: String,
    #[serde(default = "default_weight")]
    pub weight: f64,
    /// #869: evidence anchor for the edge. Every programmatic write path
    /// stamps the from-side entity id (the record that asserts the edge);
    /// callers may supply a richer anchor (source event / external ref).
    /// Links WITHOUT this metadata are NOT serveable by the graph recall
    /// arms (`graph_expand` gates on it) — they surface only via the
    /// `graph_drift` report and `graph_attest` migration tool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source: Option<String>,
}

fn default_weight() -> f64 {
    0.5
}

/// A journal event — append-only log entry.
/// Structured as: what was evaluated → what was done → what's next.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JournalEvent {
    pub id: String,
    #[serde(default = "default_event_type")]
    pub event_type: String,
    #[serde(default)]
    pub evaluated_json: String,
    #[serde(default)]
    pub acted_json: String,
    #[serde(default)]
    pub forward_json: String,
    #[serde(default)]
    pub category: String,
    #[serde(default)]
    pub key: String,
    #[serde(default)]
    pub entity_id: String,
    pub agent_id: String,
    /// Workspace of the entity this event refers to, stamped at write time so
    /// `purge` can scope journal redaction per-workspace (#417). Empty for
    /// workspace-agnostic system events (dream/synthesis) and for legacy rows
    /// written before the SCHEMA_VERSION 11 migration added the column.
    #[serde(default)]
    pub workspace_hash: String,
    pub created_at_unix_ms: i64,
}

fn default_event_type() -> String {
    "decision".to_string()
}

/// #683: a Keystone — a mandatory policy rule fetched deterministically at
/// session start and obeyed over conflicting instructions. Merged across scope
/// (tenant < fleet < agent) with weight-based conflict resolution.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Keystone {
    pub id: String,
    pub content: String,
    pub scope: String,
    pub scope_id: String,
    pub weight: f64,
    pub trust_tier_required: i64,
    pub workspace_hash: String,
    pub author_agent_id: String,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
}

/// #889: a candidate directive/keystone suggestion extracted from a
/// `correct` capture by word-boundary-anchored patterns. Suggestions are
/// never policy: only an operator `approve` decision promotes one to the
/// keystones table.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KeystoneSuggestion {
    pub id: String,
    pub source_entity_id: String,
    pub source_category: String,
    pub instruction: String,
    pub pattern_locale: String,
    pub matched_pattern: String,
    pub status: String,
    pub created_at_unix_ms: i64,
    pub decided_at_unix_ms: Option<i64>,
    pub decided_by: Option<String>,
    pub workspace_hash: String,
}

/// #684: a registered agent — identity + a trust tier (0-3) that gates
/// sensitive ops and drives visibility enforcement on reads.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Agent {
    pub agent_id: String,
    pub name: String,
    pub trust_tier: i64,
    pub fleet_id: String,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
}

/// A versioned authority manifest that constrains an agent's actions inside one
/// workspace. It carries opaque trusted-scope anchors; callers never self-authorize
/// arbitrary workspace or repository strings.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorityManifestInput {
    pub agent_id: String,
    pub workspace_hash: String,
    #[serde(default)]
    pub allowed_capabilities: Vec<String>,
    #[serde(default)]
    pub approval_required_capabilities: Vec<String>,
    #[serde(default)]
    pub scope_anchors: Vec<String>,
    #[serde(default)]
    pub approver_principals: Vec<String>,
    #[serde(default)]
    pub allowed_inbound_principals: Vec<String>,
    #[serde(default)]
    pub permitted_external_ref_prefixes: Vec<String>,
    #[serde(default = "default_max_parallel_actions")]
    pub max_parallel_actions: i64,
    #[serde(default = "default_authority_mode")]
    pub mode: String,
    #[serde(default)]
    pub expires_at_unix_ms: Option<i64>,
    #[serde(default)]
    pub capability_constraints_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorityManifest {
    pub id: String,
    pub agent_id: String,
    pub workspace_hash: String,
    pub version: i64,
    pub allowed_capabilities: Vec<String>,
    pub approval_required_capabilities: Vec<String>,
    pub scope_anchors: Vec<String>,
    pub approver_principals: Vec<String>,
    pub allowed_inbound_principals: Vec<String>,
    pub permitted_external_ref_prefixes: Vec<String>,
    pub max_parallel_actions: i64,
    pub mode: String,
    pub expires_at_unix_ms: Option<i64>,
    pub revoked_at_unix_ms: Option<i64>,
    pub created_at_unix_ms: i64,
    #[serde(default)]
    pub capability_constraints_json: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuthorizedAction {
    pub id: String,
    pub manifest_id: String,
    pub manifest_version: i64,
    pub agent_id: String,
    pub workspace_hash: String,
    pub scope_anchor: String,
    pub external_ref: String,
    pub capability: String,
    pub action_key: String,
    pub intent_hash: String,
    pub outcome_hash: String,
    pub status: String,
    pub approval_required: bool,
    pub approval_ref: String,
    pub created_at_unix_ms: i64,
    pub updated_at_unix_ms: i64,
    #[serde(default)]
    pub resource_constraints_json: String,
    #[serde(default)]
    pub resource_constraints_hash: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ActionLease {
    pub id: String,
    pub action_id: String,
    pub workspace_hash: String,
    pub action_key: String,
    pub holder_id: String,
    pub expires_at_unix_ms: i64,
    pub released_at_unix_ms: Option<i64>,
    pub created_at_unix_ms: i64,
}

fn default_max_parallel_actions() -> i64 {
    1
}

fn default_authority_mode() -> String {
    "shadow".to_string()
}

/// A key-value state entry with optional TTL.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct StateEntry {
    pub key: String,
    #[serde(default)]
    pub value_json: String,
    pub expires_at_unix_ms: Option<i64>,
    pub created_at_unix_ms: i64,
}

/// Parameters for entity recall queries.
#[derive(Clone)]
pub struct RecallParams {
    pub query: String,
    pub category: Option<String>,
    pub entity_type: Option<String>,
    pub limit: i64,
    pub offset: i64,
    pub min_decay: f64,
    pub topic_path: Option<String>,
    pub include_archived: bool,
    pub skip_side_effects: bool,
    pub mode: SearchMode,
    pub embedding: Option<Vec<f32>>,
    /// If set, truncate body_json at this many chars and append drill-down footer.
    /// BrainDB-inspired: prevents large bodies from silently flooding context.
    pub preview_cap: Option<i64>,
    /// If Some, only return entities where always_on matches (for context injection).
    pub always_on: Option<bool>,
    /// Additive boost weight for content witness signal (0.0 = disabled).
    /// Computes substring-match score against body_json, damped by body length.
    pub content_weight: f64,
    /// Additive boost weight for provenance/trust signal (0.0 = disabled).
    /// Verified sources are boosted fully; unverified entities are boosted in
    /// proportion to their certainty, so trusted sources outrank AI drafts on
    /// the same topic. Never penalizes.
    pub trust_weight: f64,
    /// Per-keyword halving quota for result diversity (1.0 = disabled).
    /// Each distinct matched keyword gets ceil(max_results × halving^n) slots.
    pub diversity_halving: f64,
    /// Per-query reservation share for multi-query diversity (0.0 = disabled).
    #[allow(dead_code)]
    pub diversity_per_query_share: f64,
    /// Recency half-life in seconds for time-aware hybrid ranking (#235).
    /// When `Some(hl)` with `hl > 0`, the RRF fusion score of each hybrid result
    /// is multiplied by a time-decay factor `0.5^(age / hl)`, where `age` is the
    /// time since the entity was created. A memory `hl` seconds old keeps half its
    /// fused weight, so recent context outranks an older but lexically-similar hit.
    /// `None` (default) preserves the existing relevance-only ranking exactly.
    /// Only applies to `SearchMode::Hybrid`; entities with an unset (<= 0)
    /// `created_at_unix_ms` are never penalized.
    pub recency_half_life_secs: Option<f64>,
    /// Workspace scope filter (v1.2.0). When Some, only entities with a
    /// matching workspace_hash are returned. None = no workspace filtering.
    pub workspace_hash: Option<String>,
    /// #485: scope as a ranking multiplier, not just a filter. When Some(w)
    /// (0.0–1.0) AND `workspace_hash` is Some(non-empty ws), the workspace
    /// predicate widens from strict equality to "current workspace OR global
    /// ('')", and broader-scope (global) hits are weighted by `w` in the
    /// scored paths (hybrid RRF fusion, dense similarity) — current-scope
    /// hits outrank equally-relevant global hits, but a strong global hit
    /// can still surface instead of being silently invisible. The widening
    /// only ever adds GLOBAL rows: other workspaces' rows stay excluded
    /// (#338/#339 scoping is unchanged). On the score-less FTS5 keyword
    /// ordering the preference is a stable two-tier sort (current scope
    /// first). None (default) keeps the strict filter — zero params, zero
    /// behavior change.
    pub scope_weight: Option<f64>,
    /// Agent identity filter (v1.2.0). When Some, only entities with a
    /// matching agent_id are returned. None = no agent filtering.
    pub agent_id: Option<String>,
    /// Epistemic trust-axis filter (#880). When Some, only entities whose
    /// epistemic_state matches are returned (e.g. "candidate" to surface
    /// useful-but-unverified records, "verified"/"corroborated" to restrict
    /// to established fact). None = no trust filtering. Applied on the FTS,
    /// dense, and hybrid paths alike so the semantic arms cannot leak
    /// cross-trust results.
    pub epistemic_state: Option<String>,
    /// Visibility filter (v1.2.0). When Some, only entities with matching
    /// visibility are returned. None = no visibility filter.
    // Reserved: the recall query does not yet apply this filter and the MCP
    // RecallArgs has no visibility field, so it is always None in practice.
    // Kept so the filter can be wired without a signature change.
    #[allow(dead_code)]
    pub visibility: Option<String>,
    pub layer: Option<String>,
    /// Opt-in reinforcement for Dense/Hybrid recall. The semantic paths are
    /// side-effect-free by default so repeated recalls over a frozen DB stay
    /// byte-deterministic (#247) — the cost is that memories only ever found
    /// semantically decay as if unused. When true, the returned hits receive
    /// the same retrieval_count/last_accessed/decay-boost side-effects the
    /// fts5 path applies, trading determinism for "used memories resist
    /// decay". Ignored when skip_side_effects is set. No effect on the fts5
    /// path, which already reinforces unless skip_side_effects.
    pub reinforce: bool,
    /// #883 (TEMPR-style fused recall): the strategies to engage when
    /// `mode == Fused`. Recognized: "fts5", "dense", "graph", "temporal"
    /// (2–4 strategies). Empty = all four. Unknown names are rejected.
    pub strategies: Vec<String>,
    /// #883: token-budget truncation. Results are accumulated in fused rank
    /// order until their estimated token cost (chars/4 per body) reaches
    /// `max_tokens`; the remainder is dropped and reported in the trace.
    /// 0 = derive from `depth_budget`.
    pub max_tokens: i64,
    /// #883: depth budget low | mid | high → default token caps
    /// 1024 / 4096 / 16384 when `max_tokens` is unset. None = mid.
    pub depth_budget: Option<String>,
    /// #883: per-strategy RRF weight multipliers (default 1.0 each).
    /// An arm that found nothing contributes nothing regardless of weight.
    pub strategy_weights: Option<std::collections::HashMap<String, f64>>,
    /// #883: optional rerank stage over the fused pool (default off —
    /// latency-preserving). When enabled, the fused top pool is re-scored
    /// with rank-calibrated dense + BM25 agreement signals (1/(1+rank),
    /// scale-free) before truncation; the fused RRF order is kept when no
    /// score-bearing arm is available.
    pub rerank: bool,
    /// #883: anchor instant for the temporal strategy (unix ms; default
    /// now). Candidates whose created_at is nearest to this instant rank
    /// first within the temporal arm; bi-temporal semantics keep the
    /// version current at the instant when a (category, key) has several.
    pub query_time_unix_ms: Option<i64>,
    /// #869: graph utility gate threshold in [0, 1]. The fused path engages
    /// the graph arm only when the query's classified graph utility is >=
    /// this value. None = 0.5 (the documented default). 0.0 disables the
    /// gate (always engage when the strategy is requested); 1.0 effectively
    /// never engages. The routing decision is always observable in the
    /// fused trace's `graph_route`.
    pub graph_utility_threshold: Option<f64>,
    /// #860: validity-aware recall profile. `"validity"` re-ranks the fused
    /// pool by a deterministic validity multiplier (freshness decay, scope
    /// match, provenance class, supersession, expiry proximity) and
    /// annotates every delivered item with its validity info; `"default"` /
    /// None keeps the relevance-only ordering. On non-fused modes the
    /// profile only enables item annotation (no re-ranking). Unknown
    /// profiles are rejected fail-closed.
    pub profile: Option<String>,
    /// #860: annotate delivered items with their validity info (grade,
    /// freshness, scope match, provenance class, superseded, expiring/
    /// expired, multiplier, signals). Off by default so response bytes stay
    /// stable unless asked. Implied by `profile: "validity"`.
    pub validity_annotate: bool,
    /// #886: hierarchical tier ordering — mental models first, then
    /// consolidated observations, then raw facts (stable within each tier).
    /// Reorders the RETURNED list only; ranking membership is untouched, so
    /// default behavior (false) is byte-identical. The ask path enables it
    /// internally so curated summaries are consulted before raw facts.
    pub tier_order: bool,
}

/// Search mode for recall: FTS5 keyword, dense vector, hybrid fusion, or
/// fused multi-strategy (TEMPR-style: FTS + dense + graph + temporal with
/// RRF fusion and token-budget truncation, #883).
#[derive(Debug, Clone, Deserialize, Default, PartialEq)]
pub enum SearchMode {
    #[default]
    Fts5,
    Dense,
    Hybrid,
    Fused,
}

/// #864/#873/#887: explicit recall outcome. Every recall/projection path
/// reports HOW it went, so an empty result is never mistaken for a healthy
/// "no match" and a degraded semantic backend is never mistaken for an
/// empty store. `status` is the high-signal summary:
///   - `fresh`        — backend healthy, results (possibly zero) served
///   - `partial`      — some arms served; others degraded/skipped (e.g.
///                      hybrid with keyword arm dropped)
///   - `timeout`      — a caller-supplied deadline elapsed before the
///                      result set was complete
///   - `unavailable`  — a required backend (embedding provider, DB) is
///                      down; the empty result is a FAULT, not a fact
///   - `empty`        — genuinely no matching evidence in a healthy store
///   - `stale`        — the store is healthy but its derived index is
///                      behind (pending embed jobs / low coverage), so
///                      semantic recall may be incomplete
/// `abstained` is the #887 no-evidence signal: when true, the caller MUST
/// NOT treat the absence of hits as a "best guess" answer — there is
/// simply no evidence, and the `reason` says why.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum RecallStatus {
    #[default]
    Fresh,
    Partial,
    Timeout,
    Unavailable,
    Empty,
    Stale,
}

impl RecallStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            RecallStatus::Fresh => "fresh",
            RecallStatus::Partial => "partial",
            RecallStatus::Timeout => "timeout",
            RecallStatus::Unavailable => "unavailable",
            RecallStatus::Empty => "empty",
            RecallStatus::Stale => "stale",
        }
    }
}

/// #873: embedding/vector backend health snapshot attached to every recall
/// outcome, so dense/hybrid callers can tell "no evidence" from "the
/// semantic backend is not serving".
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct EmbeddingBackendHealth {
    /// Whether a dense-embedding backend is configured/enabled at all.
    pub enabled: bool,
    /// Whether an embedding could actually be produced for the query
    /// (backend reachable, dimension compatible). False on lite builds and
    /// when the provider errored.
    pub query_embedding_available: bool,
    /// Active (non-archived) entities carrying a stored embedding.
    pub embedded_memories: i64,
    /// Active entities total — coverage = embedded/active.
    pub active_memories: i64,
    /// Enqueued-but-unfinished background embed jobs (#864 observability).
    pub pending_embed_jobs: u64,
}

impl EmbeddingBackendHealth {
    /// Coverage fraction 0.0-1.0; 0 when there are no active memories.
    pub fn coverage(&self) -> f64 {
        if self.active_memories <= 0 {
            0.0
        } else {
            (self.embedded_memories as f64 / self.active_memories as f64).min(1.0)
        }
    }
}

/// #856: how complete the returned top-k is, relative to the scoped
/// population the caller asked for.
///
/// - `Exact` — the ranking was computed over the complete (embedded) scoped
///   population: the dense scan was exhausted (or the sig cache covered every
///   row), or the FTS path ranked the full match set.
/// - `Bounded` — the candidate pool was capped by the scan bound, but the
///   requested `limit` in-scope hits were still found (the top-k may miss
///   deeper in-scope rows, but nothing the caller asked for was lost).
/// - `Partial` — the pool was capped AND fewer than `limit` in-scope hits
///   were found: the returned set is an INCOMPLETE top-k; more in-scope
///   results may exist beyond the pool.
/// - `Abstain` — no evidence / backend unavailable; there is nothing to be
///   complete about (#887 abstention semantics).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, Default)]
pub enum Completeness {
    #[default]
    Exact,
    Bounded,
    Partial,
    Abstain,
}

/// #856: the effective candidate scope behind a recall's completeness label.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct CandidateScope {
    /// Embedded rows actually examined by the dense arm this call.
    pub scanned: i64,
    /// Total embedded (non-archived) rows when known; `None` when the scan
    /// was bounded and the true population is beyond the pool.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedded_population: Option<i64>,
    /// The pool bound that applied (dense scan ceiling / candidate pool);
    /// `None` when the scan was exhausted (no bound bit).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pool_bound: Option<i64>,
}

/// #864: the full recall outcome — status, backend health, abstention, and
/// why. Serialized onto recall/ask responses as `outcome`.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct RecallOutcome {
    pub status: RecallStatus,
    pub abstained: bool,
    /// Machine-readable reason (e.g. "no_match", "db_unhealthy",
    /// "embedding_unavailable", "deadline_elapsed", "partial_arms").
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub reason: String,
    /// Caller-supplied deadline exceeded (bounded recall, #864).
    #[serde(default, skip_serializing_if = "is_false")]
    pub deadline_elapsed: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend_health: Option<EmbeddingBackendHealth>,
    /// #856: top-k completeness (exact/bounded/partial/abstain) and the
    /// effective candidate scope. `None` when the caller did not request the
    /// outcome or the path does not produce one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub completeness: Option<Completeness>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub candidate_scope: Option<CandidateScope>,
}

/// #856: completeness metadata returned alongside a recall (dense/hybrid
/// paths) so callers can tell "exact top-k" from "bounded/partial".
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Default)]
pub struct RecallCompleteness {
    pub completeness: Completeness,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<CandidateScope>,
}

/// #883/#867: fused multi-strategy recall trace. Every fused recall reports
/// how each strategy performed, what was fused at what weights, what the
/// token budget kept and dropped, whether the optional rerank stage applied,
/// and the final placement — so an empty or surprising result is never
/// opaque and retrieval quality is measurable end to end.
#[derive(Debug, Clone, Serialize, Default)]
pub struct FusedTrace {
    /// The verbatim caller query. The fused path never rewrites it (#867:
    /// query preservation — exact identifiers survive untouched).
    pub original_query: String,
    /// Query expansions applied (always empty in v1: no rewriting).
    pub expansions: Vec<String>,
    /// One entry per engaged strategy, in engagement order, with the
    /// strategy's own pre-fusion ranking (top entity ids).
    pub strategies: Vec<FusedStrategyTrace>,
    pub fusion: FusedFusionTrace,
    pub truncation: FusedTruncationTrace,
    pub rerank: FusedRerankTrace,
    /// Final placement: entity ids in delivered order, after fusion, rerank,
    /// filters, and token-budget truncation.
    pub placement: Vec<String>,
    /// State filters that were active during this recall (workspace,
    /// epistemic, layer, category, entity_type, agent, archived).
    pub state_filters: Vec<String>,
    /// Entity id -> strategies that surfaced it (consensus map). Only for
    /// entities that survived into the delivered set.
    pub sources: std::collections::BTreeMap<String, Vec<String>>,
    /// #869: the graph utility gate decision for this recall. Present when
    /// the caller requested the "graph" strategy; records the routing
    /// reason, whether the arm engaged, and how many edges the serve-time
    /// gates (evidence/scope/expiry) skipped.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub graph_route: Option<GraphRouteTrace>,
    /// #860: validity-aware profile outcome. Present when the caller
    /// requested `profile: "validity"`; records the weights applied, the
    /// grade distribution over the fused pool, and how many candidates were
    /// flagged context-invalid.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub validity: Option<ValidityTrace>,
}

/// #860: observable validity-profile trace attached to a fused recall.
#[derive(Debug, Clone, Serialize, Default)]
pub struct ValidityTrace {
    /// "validity" when the profile engaged.
    pub profile: String,
    /// "validity-multiplier-v1" — deterministic multiplier over
    /// freshness/scope/provenance/supersession/expiry signals.
    pub method: String,
    /// The exact weights used (freshness half-life, bonuses, penalties).
    pub weights: crate::validity::ValidityWeights,
    /// Grade distribution over the fused pool ("valid"/"stale"/
    /// "context_invalid" -> count).
    pub grade_counts: std::collections::BTreeMap<String, usize>,
    /// How many pool candidates were flagged context-invalid (superseded,
    /// expired, or below the freshness floor).
    pub flagged_context_invalid: usize,
}

/// #869: observable graph utility-gate decision attached to a fused recall.
#[derive(Debug, Clone, Serialize, Default)]
pub struct GraphRouteTrace {
    /// The classified utility score in [0, 1].
    pub utility: f64,
    /// Dominant question shape: "multi_hop" | "global" | "temporal" |
    /// "entity_centric" | "relational" | "ordinary" | "no_signal".
    pub reason: String,
    /// Whether the graph arm engaged (utility >= threshold).
    pub selected: bool,
    /// Empty when engaged; otherwise why the arm was skipped
    /// ("low_utility", "no_signal").
    pub skipped_reason: String,
    /// Edges skipped by the evidence gate (no `source` anchor).
    pub unattested_edges_skipped: usize,
    /// Edges skipped by the scope gate (target workspace outside
    /// {source workspace, global}).
    pub out_of_scope_edges_skipped: usize,
    /// Linked targets skipped because they are expired.
    pub expired_targets_skipped: usize,
    /// Linked targets that are missing entirely or archived (drift).
    pub dangling_targets_skipped: usize,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct FusedStrategyTrace {
    pub strategy: String,
    /// How many candidates the arm produced (pre-fusion).
    pub candidates: usize,
    /// The arm's own ranking (entity ids, best first).
    pub top: Vec<String>,
    /// "ok" | "degraded" | "empty" — degraded = the arm could not run
    /// (e.g. embedding backend down for dense).
    pub status: String,
    /// Wall-clock latency of the arm in ms.
    pub latency_ms: f64,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct FusedFusionTrace {
    /// The RRF constant k used.
    pub rrf_k: f64,
    /// Effective per-strategy RRF weights (after zeroing empty arms).
    pub weights: std::collections::BTreeMap<String, f64>,
    /// Pre-truncation fused size (the RRF pool).
    pub fused_count: usize,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct FusedTruncationTrace {
    /// The token budget applied (estimated tokens = chars/4 per body).
    pub budget_tokens: i64,
    /// Estimated tokens consumed by the delivered set.
    pub estimated_tokens_used: i64,
    pub retained: usize,
    pub dropped: usize,
}

#[derive(Debug, Clone, Serialize, Default)]
pub struct FusedRerankTrace {
    /// Whether the caller requested the rerank stage.
    pub enabled: bool,
    /// Whether the stage actually re-scored the pool (false = fell back to
    /// RRF order because calibration was impossible).
    pub applied: bool,
    /// "rankcal-dense-bm25" (v1 calibrator: rank-derived 1/(1+rank) signals,
    /// scale-free by construction) — provider cross-encoder is the
    /// documented extension point.
    pub method: String,
    pub note: String,
}

fn is_false(v: &bool) -> bool {
    !*v
}

/// Configuration for FTS5 query expansion using stemming variants.
#[derive(Debug, Clone, Deserialize, Default)]
pub struct QueryExpansionConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_n_variants")]
    pub n_variants: usize,
}

fn default_n_variants() -> usize {
    1
}

/// Configuration for AES-256-GCM encryption at rest.
#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)]
pub struct EncryptionConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_key_file")]
    pub key_file: String,
}

#[allow(dead_code)]
fn default_key_file() -> String {
    "~/.perseus-vault/secret.key".to_string()
}

impl Default for RecallParams {
    fn default() -> Self {
        Self {
            query: String::new(),
            category: None,
            entity_type: None,
            limit: 10,
            offset: 0,
            min_decay: 0.0,
            topic_path: None,
            include_archived: false,
            skip_side_effects: false,
            mode: SearchMode::Fts5,
            embedding: None,
            preview_cap: None,
            always_on: None,
            content_weight: 0.0,
            trust_weight: default_trust_weight(),
            diversity_halving: 1.0,
            diversity_per_query_share: 0.0,
            recency_half_life_secs: None,
            workspace_hash: None,
            scope_weight: None,
            agent_id: None,
            epistemic_state: None,
            visibility: None,
            layer: None,
            reinforce: false,
            strategies: Vec::new(),
            max_tokens: 0,
            depth_budget: None,
            strategy_weights: None,
            rerank: false,
            query_time_unix_ms: None,
            graph_utility_threshold: None,
            profile: None,
            validity_annotate: false,
            tier_order: false,
        }
    }
}

/// Injection posture for `context`/`prepare` blocks (#366).
///
/// `OnDemand` (the default) is recall-first: the block contains a hard-capped
/// always-on set plus entities that are *topically relevant to the supplied
/// query* (recall_when trigger matches + stopword-filtered keyword matches),
/// clamped to a per-model character budget. Without a query, no topical
/// entities are injected at all — the block is a compact retrieval pointer,
/// which also keeps it byte-stable across unrelated vault writes
/// (prefix-cache friendly).
///
/// `AlwaysInject` is the legacy pre-#356 behavior — unconditional top-N
/// dump ranked by retrieval_count/recency, no relevance gating — kept as an
/// explicit opt-in for backward compatibility.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum ContextMode {
    #[default]
    OnDemand,
    AlwaysInject,
}

impl ContextMode {
    pub fn as_str(&self) -> &'static str {
        match self {
            ContextMode::OnDemand => "on_demand",
            ContextMode::AlwaysInject => "always_inject",
        }
    }
}

/// Options for building a `context`/`prepare` injection block (#356/#366).
#[derive(Debug, Clone, Default)]
pub struct ContextOptions {
    /// Categories to include. Empty = all categories.
    pub categories: Vec<String>,
    /// Max topical entities in the block body.
    pub limit: i64,
    /// Workspace scope filter — same exact-match semantics as recall.
    pub workspace_hash: Option<String>,
    /// Current task/message text. In `OnDemand` mode this is the relevance
    /// gate: only entities whose `recall_when` triggers or indexed content
    /// match it are injected. Ignored in `AlwaysInject` mode.
    pub query: Option<String>,
    /// Injection posture. Defaults to recall-first (`OnDemand`).
    pub mode: ContextMode,
    /// Explicit character budget for the rendered block. Overrides `model`
    /// profile resolution. In `AlwaysInject` mode clamping only happens when
    /// this is set (legacy output is otherwise unclamped).
    pub max_context_chars: Option<i64>,
    /// Host model name for budget-profile resolution (e.g.
    /// "claude-opus-4-8"). Unknown/absent models use the default profile.
    pub model: Option<String>,
    /// Entity ids to exclude from the topical section (used by `prepare`,
    /// which renders recall_when hits in its own section and must not show
    /// them twice).
    pub exclude_ids: Vec<String>,
    /// #875: caller session id for preload usage telemetry. Empty when
    /// unknown — events then group into a pseudo-session per context hash.
    pub session_id: String,
}

/// A rendered context block plus injection metadata (#366).
#[derive(Debug, Clone, Serialize)]
pub struct ContextBlock {
    pub markdown: String,
    /// "on_demand" or "always_inject".
    pub mode: String,
    /// Resolved character budget (0 = unclamped legacy output).
    pub budget_chars: i64,
    /// Number of entities actually injected (always-on + topical).
    pub entities_injected: i64,
    /// Soft warnings: always-on cap overflow, budget truncation.
    pub warnings: Vec<String>,
    pub injected_chars: i64,
    pub estimated_injected_tokens: i64,
    pub corpus_chars: i64,
    pub estimated_corpus_tokens: i64,
}

/// Parameters for timeline queries over the journal.
pub struct TimelineParams {
    /// `None` is reserved for internal/admin callers. Public readers must
    /// provide `Some(workspace_hash)`, with `Some("")` selecting only the
    /// explicit global partition.
    pub workspace_hash: Option<String>,
    pub from_ms: Option<i64>,
    pub to_ms: Option<i64>,
    pub event_type: Option<String>,
    pub category: Option<String>,
    pub entity_id: Option<String>,
    pub limit: i64,
    pub offset: i64,
}

impl Default for TimelineParams {
    fn default() -> Self {
        Self {
            workspace_hash: None,
            from_ms: None,
            to_ms: None,
            event_type: None,
            category: None,
            entity_id: None,
            limit: 50,
            offset: 0,
        }
    }
}

/// Migration report from v0.1.x → v0.2.0.
#[derive(Debug, Clone, Serialize)]
pub struct MigrationReport {
    pub total_old_memories: i64,
    pub entities_created: i64,
    pub entities_updated: i64,
    pub errors: Vec<String>,
    pub completed_at_unix_ms: i64,
}

/// Vault export/import report.
#[derive(Debug, Clone, Serialize)]
pub struct VaultReport {
    pub files_created: i64,
    pub files_updated: i64,
    pub errors: Vec<String>,
    pub vault_dir: String,
    pub completed_at_unix_ms: i64,
}

/// Decay tick report.
#[derive(Debug, Clone, Serialize)]
pub struct DecayReport {
    pub entities_checked: i64,
    pub entities_updated: i64,
    pub auto_archived: i64,
    pub completed_at_unix_ms: i64,
}

/// Result of recording a follow/miss observation against an entity
/// (v2.10.0 — PMB-inspired efficacy scoring).
#[derive(Debug, Clone, Serialize)]
pub struct FollowReport {
    pub found: bool,
    pub category: String,
    pub key: String,
    pub follow_count: i64,
    pub miss_count: i64,
    pub follow_rate: f64,
    pub efficacy_status: String,
}

/// Compact report.
#[derive(Debug, Clone, Serialize)]
pub struct CompactReport {
    pub entities_archived: i64,
    pub entities_examined: i64,
    pub dry_run: bool,
    pub completed_at_unix_ms: i64,
}

/// Purge report — permanently deletes archived entities and runs VACUUM.
#[derive(Debug, Clone, Serialize)]
pub struct PurgeReport {
    pub entities_deleted: i64,
    /// #398: superseded versions of the purged entities removed from
    /// entity_history — purge's "actually remove" contract now covers history.
    pub history_rows_deleted: i64,
    /// #398: journal rows referencing the purged entities whose payloads were
    /// scrubbed in place (rows are kept so the audit hash chain stays valid).
    pub journal_rows_redacted: i64,
    /// #876: learned-artifact bindings revoked because their source entity
    /// was physically removed (serve paths refuse revoked bindings).
    pub artifact_bindings_revoked: i64,
    pub bytes_freed: i64,
    pub dry_run: bool,
    pub completed_at_unix_ms: i64,
}

/// Lifecycle axis vocabulary (#868): the `status` field on entities is the
/// lifecycle axis, orthogonal to the epistemic trust axis (#880). The ops
/// that transition these states are distinct: expire (time-based, content
/// kept), supersede (correctness replacement, history kept), forget (logical
/// delete), redact (content scrub, metadata kept), erase (physical removal
/// across derived layers + re-ingest suppression). See
/// docs/specs/data-boundaries-retention-lifecycle.md (v1).
pub const LIFECYCLE_STATES: [&str; 9] = [
    "active",
    "proposed",
    "superseded",
    "resolved",
    "quarantined",
    "expired",
    "redacted",
    "logically_deleted",
    "physically_erased",
];

/// Expire report (#868) — time-based lifecycle sweep: rows past
/// `expires_at_unix_ms` transition to status='expired'. Content and history
/// are retained; recall already excludes expired rows.
#[derive(Debug, Clone, Serialize)]
pub struct ExpireReport {
    pub entities_expired: i64,
    /// Empty string = global sweep; otherwise restricted to one workspace.
    pub workspace_hash: String,
    pub dry_run: bool,
    pub completed_at_unix_ms: i64,
}

/// Redact report (#868) — content scrub with metadata retention. The body is
/// replaced by a hash-only marker, history + FTS text are deleted, and a
/// hash-only `redacted` journal event is appended. Re-ingest of the same
/// value remains allowed (redaction ≠ erasure).
#[derive(Debug, Clone, Serialize)]
pub struct RedactReport {
    pub found: bool,
    pub entity_id: String,
    /// sha256 of the scrubbed body (normalized like rejection digests), kept
    /// as hash-only audit evidence.
    pub value_sha256: String,
    pub history_deleted: i64,
    pub fts_cleaned: i64,
    pub journal_event_id: String,
    pub workspace_hash: String,
    pub completed_at_unix_ms: i64,
}

/// Erase report (#868/#866) — physical erasure of one (category, key,
/// workspace) identity across ALL derived layers, plus permanent re-ingest
/// suppression via tombstone + governance overlay mandate. Journal content
/// referencing the erased rows is redacted; a hash-only `erased` event is
/// appended. Contract: docs/specs/data-boundaries-retention-lifecycle.md.
#[derive(Debug, Clone, Serialize)]
pub struct EraseReport {
    pub entities_erased: i64,
    pub history_deleted: i64,
    pub fts_cleaned: i64,
    /// Community memberships (member_ids entries) removed.
    pub community_memberships_cleaned: i64,
    /// Community rows deleted because the erased entity was their last member.
    pub community_rows_deleted: i64,
    /// Inbound link edges (other rows pointing at the erased id) removed.
    pub inbound_links_cleaned: i64,
    /// Derived entities (beliefs/observation/synthesis) citing the erased
    /// source via evidence links, now quarantined pending operator review.
    pub derived_quarantined: i64,
    /// Journal rows scrubbed in place (payloads → {}, chain tuple preserved).
    pub journal_rows_redacted: i64,
    pub journal_event_id: String,
    /// sha256 of the erased body (hash-only evidence, contract §6.2).
    pub value_sha256: String,
    pub workspace_hash: String,
    pub dry_run: bool,
    /// False if the permanent governance mandate could not be installed —
    /// content is gone but the re-ingest guard needs operator attention.
    pub governance_mandate_ok: bool,
    pub completed_at_unix_ms: i64,
}

/// #398: entity_history retention knobs. Every knob defaults OFF (None =
/// unlimited), preserving the pre-#398 keep-everything behavior — enabling a
/// bound is a deliberate operator decision, made via env:
///   * `PERSEUS_VAULT_HISTORY_MAX_AGE_DAYS` — evict versions invalidated more than N
///     days ago.
///   * `PERSEUS_VAULT_HISTORY_MAX_VERSIONS_PER_KEY` — keep at most N stored versions
///     per (category, key, workspace); oldest evicted first. Hot state-like
///     keys are the pathological case (10k supersedes of one key = +14.5MB).
///   * `PERSEUS_VAULT_HISTORY_MAX_BYTES` — global budget over stored history body
///     bytes; globally-oldest versions evicted until under budget.
///   * `PERSEUS_VAULT_HISTORY_TOMBSTONES` — default ON: an evicted run is replaced by
///     one synthetic tombstone row (see Database::enforce_history_retention)
///     so time-travel inside the window answers "compacted", not silence.
///     Set to `0`/`false` for hard delete (the degenerate mode).
/// Enforcement runs only in maintenance paths (perseus_vault_maintenance --all,
/// perseus_vault_autocohere, perseus_vault_prune scope=history) — never on the write path.
#[derive(Debug, Clone)]
pub struct HistoryRetentionPolicy {
    pub max_age_days: Option<i64>,
    pub max_versions_per_key: Option<i64>,
    pub max_bytes: Option<i64>,
    pub tombstones: bool,
}

impl Default for HistoryRetentionPolicy {
    fn default() -> Self {
        Self {
            max_age_days: None,
            max_versions_per_key: None,
            max_bytes: None,
            tombstones: true,
        }
    }
}

impl HistoryRetentionPolicy {
    /// Read the policy from the environment. Unset / non-numeric / <= 0
    /// values leave a knob OFF.
    pub fn from_env() -> Self {
        fn env_pos(name: &str) -> Option<i64> {
            std::env::var(name)
                .ok()
                .and_then(|v| v.trim().parse::<i64>().ok())
                .filter(|v| *v > 0)
        }
        let tombstones = !matches!(
            std::env::var("PERSEUS_VAULT_HISTORY_TOMBSTONES")
                .unwrap_or_default()
                .trim()
                .to_ascii_lowercase()
                .as_str(),
            "0" | "false" | "off" | "no"
        );
        Self {
            max_age_days: env_pos("PERSEUS_VAULT_HISTORY_MAX_AGE_DAYS"),
            max_versions_per_key: env_pos("PERSEUS_VAULT_HISTORY_MAX_VERSIONS_PER_KEY"),
            max_bytes: env_pos("PERSEUS_VAULT_HISTORY_MAX_BYTES"),
            tombstones,
        }
    }

    /// True when no bound is configured — enforcement is a no-op.
    pub fn is_unlimited(&self) -> bool {
        self.max_age_days.is_none()
            && self.max_versions_per_key.is_none()
            && self.max_bytes.is_none()
    }

    /// Echo for reports.
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "max_age_days": self.max_age_days,
            "max_versions_per_key": self.max_versions_per_key,
            "max_bytes": self.max_bytes,
            "tombstones": self.tombstones,
        })
    }
}

/// Report from a history-retention enforcement pass (#398).
#[derive(Debug, Clone, Serialize)]
pub struct HistoryRetentionReport {
    /// History rows removed (or, in dry_run, that WOULD be removed).
    pub rows_evicted: i64,
    /// Stored body bytes of the evicted rows (LENGTH(body_json); row/index
    /// overhead excluded).
    pub bytes_evicted: i64,
    /// Tombstone roll-up rows written (0 when tombstones are disabled).
    pub tombstones_written: i64,
    /// Distinct (category, key, workspace) groups touched.
    pub keys_affected: i64,
    pub dry_run: bool,
    /// The policy that was enforced.
    pub policy: serde_json::Value,
}

/// Parameters for the coherence daemon pass.
#[derive(Debug, Clone, Deserialize)]
pub struct CohereParams {
    #[serde(default)]
    pub dry_run: bool,
    #[serde(default = "default_max_links")]
    pub max_links: usize,
    #[serde(default)]
    pub promote_threshold: i64,
    #[serde(default = "default_archive_threshold")]
    pub archive_threshold: f64,
    /// #486: opt-in cross-scope promotion — promote a fact independently
    /// observed in >= `cross_scope_k` distinct workspaces up to the global
    /// ('') scope, linking back to the per-scope evidence. OFF by default:
    /// absence of these args = pre-#486 behavior exactly.
    #[serde(default)]
    pub cross_scope_promote: bool,
    /// Minimum distinct workspaces before a recurring fact is promoted
    /// (0 sentinel → default 3; clamped to >= 2 — "seen twice" is not a
    /// pattern worth generalizing).
    #[serde(default)]
    pub cross_scope_k: i64,
    /// Trigram similarity for "same fact across scopes" (0.0 sentinel → the
    /// remember() dedup threshold, 0.7).
    #[serde(default)]
    pub cross_scope_similarity: f64,
}

// Manual Default so `..Default::default()` construction (autocohere) gets the
// SAME link budget as the MCP arg path's serde default. The derived Default
// gave max_links = 0 → candidate_budget 0 → `LIMIT 0` — autocohere's cohere
// step never created a single link (#412). promote_threshold = 0 and
// archive_threshold = 0.0 stay as-is: cohere treats those sentinels as
// "fall through to WORKING_THRESHOLD / ARCHIVE_DECAY_THRESHOLD".
impl Default for CohereParams {
    fn default() -> Self {
        Self {
            dry_run: false,
            max_links: default_max_links(),
            promote_threshold: 0,
            archive_threshold: 0.0,
            cross_scope_promote: false,
            cross_scope_k: 0,
            cross_scope_similarity: 0.0,
        }
    }
}

fn default_max_links() -> usize {
    20
}
fn default_archive_threshold() -> f64 {
    0.05
}
#[allow(dead_code)]
fn default_promote_threshold() -> i64 {
    3
}

/// Coherence daemon report — results of an auto-grooming pass.
#[derive(Debug, Clone, Serialize)]
pub struct CohereReport {
    pub promoted: i64,
    pub decayed: i64,
    pub linked: i64,
    pub archived: i64,
    pub entities_examined: i64,
    pub dry_run: bool,
    pub completed_at_unix_ms: i64,
    /// #486: cross-scope promotion outcome (all 0 / absent-in-spirit unless
    /// `cross_scope_promote` was requested). Additive fields — older readers
    /// keep working.
    pub cross_scope_clusters: i64,
    pub cross_scope_promoted: i64,
    pub cross_scope_skipped_existing: i64,
}

/// Cheap, deterministic content digest of the recall-visible entity set (#256).
/// Used as a cache key for resolved `@memory` outputs: stable while DB state is
/// unchanged, changes iff that state changes.
/// Digest semantics (#835): byte identity only — a matching digest proves the
/// set is the set; it says nothing about validity, authority, or freshness,
/// which come from entity state, never the digest.
#[derive(Debug, Clone, Serialize)]
pub struct StateDigest {
    /// 16-hex-char FNV-1a digest over (id, body_json) of non-archived entities.
    pub digest: String,
    /// Number of non-archived entities folded into the digest.
    pub entity_count: u64,
}

/// Full database statistics.
#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub total_entities: i64,
    /// #493: entities with archived = 0 — the set every user-facing read
    /// (list_entities, count_entities, recall) already operates on.
    /// `total_entities` stays archived-inclusive for compatibility.
    pub active_entities: i64,
    /// #493: entities with archived = 1 (forgotten/decayed), so consumers
    /// can show "N hidden" instead of a silently inflated total.
    pub archived_entities: i64,
    pub by_category: serde_json::Value,
    pub by_type: serde_json::Value,
    pub by_layer: serde_json::Value,
    /// #493: grouped counts restricted to active (archived = 0) rows; the
    /// unsuffixed groups above remain archived-inclusive for compatibility.
    pub by_category_active: serde_json::Value,
    pub by_type_active: serde_json::Value,
    pub by_layer_active: serde_json::Value,
    pub total_journal_events: i64,
    pub total_state_entries: i64,
    pub db_file_size_bytes: u64,
    pub oldest_unix_ms: Option<i64>,
    pub newest_unix_ms: Option<i64>,
    /// Detected link-graph communities currently persisted (#365 GraphRAG).
    pub total_communities: i64,
    /// Partition modularity of the most recent community-detection run
    /// (None until `perseus_vault_communities` has run at least once).
    pub graph_modularity: Option<f64>,
    /// #398: superseded versions stored in entity_history (incl. tombstones).
    pub total_history_rows: i64,
    /// #398: stored history body bytes (SUM(LENGTH(body_json)); row/index
    /// overhead excluded). The growth signal retention knobs bound.
    pub history_bytes: i64,
    /// #398: top-10 (category, key) pairs by stored version count —
    /// `[{category, key, versions, bytes}]` — the hot keys to cap first.
    pub top_history_keys: serde_json::Value,
}

/// #677: cheap readiness snapshot surfaced by the `health` tool and the
/// empty-recall diagnostic, so a silent-empty result is self-explaining
/// (unhealthy DB vs genuinely empty store vs keyword-only / no-coverage
/// semantic posture) instead of looking like a broken MCP child. Every field
/// is a cheap covering-index count or a config read — safe to call before a
/// recall-heavy workflow as a heartbeat.
#[derive(Debug, Clone, Serialize)]
pub struct Readiness {
    /// `SELECT 1` succeeded against the pool.
    pub db_responds: bool,
    /// Non-archived entity count — the set recall actually reads.
    pub active_memories: i64,
    /// Non-archived entities carrying a stored dense embedding.
    pub embedded_memories: i64,
    /// Whether a dense-embedding backend is active (false on lite builds).
    pub embedding_enabled: bool,
}

impl Readiness {
    /// The store can serve a non-empty recall: the DB answers and at least one
    /// active memory exists.
    pub fn ready(&self) -> bool {
        self.db_responds && self.active_memories > 0
    }

    /// Coarse semantic-recall posture for dense/hybrid callers:
    /// - `"available"`   — backend on and at least one embedded memory
    /// - `"no_coverage"` — backend on but nothing embedded yet (falls back to keyword)
    /// - `"disabled"`    — no dense backend (keyword-only build/config)
    pub fn semantic_recall(&self) -> &'static str {
        if !self.embedding_enabled {
            "disabled"
        } else if self.embedded_memories > 0 {
            "available"
        } else {
            "no_coverage"
        }
    }

    /// Human-readable likely-cause warnings — empty when everything is nominal.
    /// This is what a client prints instead of chasing a false "no memories
    /// found" debugging path when recall comes back empty.
    pub fn warnings(&self) -> Vec<String> {
        let mut w = Vec::new();
        if !self.db_responds {
            w.push(
                "database is not responding — recall and writes will fail until the vault process/DB is healthy"
                    .to_string(),
            );
            // Downstream counts are meaningless when the DB is down.
            return w;
        }
        if self.active_memories == 0 {
            w.push(
                "store has 0 active memories — recall will return empty until memories are written"
                    .to_string(),
            );
        }
        if self.embedding_enabled && self.active_memories > 0 && self.embedded_memories == 0 {
            w.push(
                "no active memories carry embeddings — dense/hybrid recall will fall back to keyword; run reindex/embed to restore semantic recall"
                    .to_string(),
            );
        }
        if !self.embedding_enabled {
            w.push(
                "semantic (dense/hybrid) backend is disabled — recall is keyword-only".to_string(),
            );
        }
        w
    }
}

/// Graph node for entity link visualization.
#[derive(Debug, Clone, Serialize)]
pub struct GraphNode {
    pub id: String,
    pub label: String,
    pub category: String,
}

/// Graph edge for entity link visualization.
#[derive(Debug, Clone, Serialize)]
pub struct GraphEdge {
    pub from: String,
    pub to: String,
    pub relationship: String,
}

/// Parameters for the perseus_vault_ask RAG tool.
#[derive(Debug, Deserialize)]
pub struct AskParams {
    pub query: String,
    #[serde(default = "default_ask_limit")]
    pub top_k: usize,
    /// #472 Temporal RAG: reconstruct the retrieved context as it was believed
    /// at this transaction-time instant (unix ms), so the RAG answer is
    /// reproducible at a past decision point. None = live view. Accepts a number
    /// or numeric string (LLM clients often stringify ints).
    #[serde(default, deserialize_with = "crate::tools::string_or_int_opt")]
    pub as_of_unix_ms: Option<i64>,
    /// #472: reconstruct the retrieved context as it was true in the world at
    /// this valid-time instant (unix ms). Combine with as_of for the full cell.
    #[serde(default, deserialize_with = "crate::tools::string_or_int_opt")]
    pub valid_at_unix_ms: Option<i64>,
    /// #783: MCP session identity stamped by the transport for visibility
    /// enforcement on RAG sources. Never treated as an author filter.
    #[serde(default)]
    pub requesting_agent_id: Option<String>,
    /// #884: stale-observation gate. When true (default), observation
    /// candidates that have newer unconsolidated raw facts are verified
    /// against those facts before citation: consistent facts pass with a
    /// verification note, contradicted observations are refused. Setting
    /// this false opts out of the gate (documented escape hatch).
    #[serde(default = "default_verify_stale_observations")]
    pub verify_stale_observations: bool,
}

fn default_ask_limit() -> usize {
    5
}

fn default_verify_stale_observations() -> bool {
    true
}

/// Result from perseus_vault_ask: a grounded answer with cited sources.
#[derive(Debug, Serialize)]
pub struct AskResult {
    pub answer: String,
    pub sources: Vec<AskSource>,
    /// #884: observation sources the stale gate refused to cite (unverified
    /// against newer raw facts). Present (possibly empty) when the gate ran.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub refused_sources: Vec<RefusedSource>,
}

/// A source the ask stale gate refused, with the deterministic reason.
#[derive(Debug, Serialize)]
pub struct RefusedSource {
    pub key: String,
    pub category: String,
    /// "stale_observation_unverified" — the observation has newer
    /// unconsolidated facts and verification failed (see detail).
    pub reason: String,
    /// The newest unconsolidated raw fact's key (trace-back anchor).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// A cited source entity in an ask result.
#[derive(Debug, Serialize)]
pub struct AskSource {
    pub key: String,
    pub category: String,
    pub score: f64,
    pub snippet: String,
    /// #884: set to "verified_against_raw" when the stale gate verified the
    /// observation against newer unconsolidated facts before citation.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verification: Option<String>,
}

/// Parameters for the perseus_vault_ingest connector sync tool.
#[derive(Debug, Deserialize)]
pub struct IngestParams {
    /// Specific connector to run (None = all enabled connectors).
    pub connector: Option<String>,
    #[serde(default)]
    pub dry_run: bool,
}

/// A raw document from an external connector before it becomes an entity.
#[derive(Debug, Clone)]
pub struct RawDocument {
    pub key: String,
    pub category: String,
    pub body_json: String,
    pub tags: Vec<String>,
}

/// Parameters for the perseus_vault_embed tool — generate and store dense embeddings.
#[derive(Debug, Deserialize)]
pub struct EmbedParams {
    /// Text to embed and store on the entity (uses entity's body_json if omitted).
    pub text: Option<String>,
    /// Entity category (required).
    pub category: Option<String>,
    /// Entity key (required).
    pub key: Option<String>,
    /// Embed all entities matching this category that lack embeddings.
    #[serde(default)]
    pub batch_category: Option<String>,
    /// Max entities to embed in batch mode (default: 100).
    #[serde(default = "default_batch_limit")]
    pub batch_limit: usize,
    /// #885: store-wide reindex — convert ALL stored embeddings (every row,
    /// archived included) from float32 to this format in one transaction:
    /// "int8" or "bit". The pre-quantization float32 column is snapshotted
    /// (once) so `restore_quantized_backup` is lossless. Refused when the
    /// store is already quantized (restore first). float32 target is refused
    /// here — the return path is the snapshot restore.
    #[serde(default)]
    pub quant_mode: Option<String>,
    /// #885: rollback — restore the `embedding` column from the
    /// pre-quantization snapshot and flip the format record back to float32.
    #[serde(default)]
    pub restore_quantized_backup: bool,
    /// #885: drop the pre-quantization snapshot after the operator verified
    /// the quantized store. Irreversible (rollback then requires re-embed).
    #[serde(default)]
    pub drop_quantized_backup: bool,
}

fn default_batch_limit() -> usize {
    100
}

/// #886: parameters for perseus_vault_mental_model_set — the ONLY sanctioned
/// write path for the curated `mental_model` category (auto-generated passes
/// never create these; the consolidate path refuses the category).
#[derive(Debug, Deserialize)]
pub struct MentalModelSetParams {
    /// Stable key of the mental model (e.g. "stack-portal").
    pub key: String,
    /// The curated summary (1..=4096 chars) — what the model answers.
    pub summary: String,
    /// Raw-fact category this model covers ("" = none). Enables the
    /// newer-facts staleness check.
    #[serde(default)]
    pub scope: String,
    /// Provenance: raw fact / observation entity ids it was curated from.
    #[serde(default)]
    pub source_ids: Vec<String>,
    /// recall_when triggers for scheduled re-verification (matched by
    /// perseus_vault_recall_when / prepare).
    #[serde(default)]
    pub recall_when: Vec<String>,
    /// Age-based review interval in days (default 30, 1..=3650).
    #[serde(default = "default_mm_review_interval")]
    pub review_interval_days: i64,
    #[serde(default)]
    pub workspace_hash: String,
    /// Curator identity (stamped by the transport when present; else "operator").
    #[serde(default)]
    pub requesting_agent_id: String,
}

fn default_mm_review_interval() -> i64 {
    crate::mental_model::DEFAULT_REVIEW_INTERVAL_DAYS
}

/// #886: parameters for perseus_vault_mental_model_review — list flagged
/// stale mental models, or stamp an operator review decision.
#[derive(Debug, Deserialize)]
pub struct MentalModelReviewArgs {
    /// "list" (default) | "approve" | "dismiss". approve/dismiss stamp
    /// reviewed_at (resets the age clock) and record the decision; the
    /// summary itself is only changed by a re-assert via
    /// perseus_vault_mental_model_set.
    #[serde(default)]
    pub action: String,
    /// Key of the model to decide on (required for approve/dismiss).
    #[serde(default)]
    pub key: String,
    #[serde(default)]
    pub workspace_hash: String,
    #[serde(default)]
    pub requesting_agent_id: String,
    /// Max flagged models to list (default 50, cap 1000).
    #[serde(default = "default_review_list_limit")]
    pub limit: i64,
}

fn default_review_list_limit() -> i64 {
    50
}

/// Parameters for the perseus_vault_prune tool — bulk archive entities.
#[derive(Debug, Deserialize)]
pub struct PruneParams {
    /// Archive entities in this category.
    pub category: Option<String>,
    /// Archive entities with decay_score below this threshold.
    pub min_decay: Option<f64>,
    /// Archive entities older than this many days.
    pub older_than_days: Option<u32>,
    /// Max entities to prune (default: 100, use 0 for unlimited).
    #[serde(default = "default_prune_limit")]
    pub limit: usize,
    #[serde(default)]
    pub dry_run: bool,
    /// Explicitly archive everything in the category (no threshold required).
    #[serde(default)]
    pub purge_all: bool,
}

fn default_prune_limit() -> usize {
    100
}

/// Report from perseus_vault_prune.
#[derive(Debug, Serialize)]
pub struct PruneReport {
    pub archived: usize,
    pub examined: usize,
    pub dry_run: bool,
    pub reason: String,
}

/// Parameters for the perseus_vault_correct tool — structured correction capture.
/// Stores what went wrong, what the user said, and what to do instead.
#[derive(Debug, Deserialize)]
pub struct CorrectParams {
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
    /// Application-time period (#363): when the corrected fact was actually
    /// true in the world. A correction that says "this was true last week"
    /// sets valid_from in the past. None = transaction time / unbounded.
    #[serde(default)]
    pub valid_from_unix_ms: Option<i64>,
    #[serde(default)]
    pub valid_to_unix_ms: Option<i64>,
    #[serde(default)]
    pub evidence: Option<EvidenceEnvelope>,
    /// Workspace scope for the rejection tombstone. Empty means global.
    #[serde(default)]
    pub workspace_hash: String,
    /// Agent that authored the correction (stamped on the tombstone).
    #[serde(default)]
    pub agent_id: String,
}

/// Result from perseus_vault_correct.
#[derive(Debug, Serialize)]
pub struct CorrectResult {
    pub entity_id: String,
    pub journal_id: String,
    pub category: String,
    pub key: String,
    pub created_at_unix_ms: i64,
    /// #855: the agent attribution persisted on the correction entity and its
    /// journal event (host identity when the transport stamped one, else the
    /// caller-supplied author). Empty when neither was provided.
    pub agent_id: String,
    /// #855: the workspace scope persisted on the correction entity and its
    /// journal event. Empty = global/legacy.
    pub workspace_hash: String,
}

/// Parameters for the perseus_vault_synthesize tool — LLM-driven session synthesis.
/// Reviews session content and extracts structured lessons learned.
#[derive(Debug, Deserialize)]
pub struct SynthesizeParams {
    pub session_content: String,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub visibility: String,
    #[serde(default)]
    pub evidence: Option<EvidenceEnvelope>,
}

/// A single synthesized lesson from session content.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SynthesizedLesson {
    pub lesson_type: String, // "success", "failure", "correction", "dead_end", "decision", "insight"
    pub summary: String,
    pub evidence: String,
    pub confidence: f64,
}

/// Result from perseus_vault_synthesize.
#[derive(Debug, Serialize)]
pub struct SynthesizeResult {
    pub lessons: Vec<SynthesizedLesson>,
    pub entities_created: i64,
    pub journal_id: String,
    pub dry_run: bool,
    pub completed_at_unix_ms: i64,
}

/// Parameters for perseus_vault_bench — performance metrics tracking.
#[derive(Debug, Deserialize)]
pub struct BenchParams {
    pub task_description: String,
    pub turns_taken: i64,
    pub tokens_used: i64,
    pub memory_recall_used: bool,
    pub recall_count: i64,
    #[serde(default)]
    pub task_success: bool,
    #[serde(default)]
    pub session_id: String,
    #[serde(default)]
    pub tags: Vec<String>,
}

/// Result from perseus_vault_bench.
#[derive(Debug, Serialize)]
pub struct BenchResult {
    pub entity_id: String,
    pub created_at_unix_ms: i64,
}

/// Parameters for the perseus_vault_consolidate tool (#steal-2, competitive research:
/// Hindsight's Observation layer). Merges overlapping/duplicative facts within
/// a category into a smaller number of durable, evidence-tracked observations.
#[derive(Debug, Deserialize)]
pub struct ConsolidateParams {
    pub category: String,
    /// Trigram similarity threshold ABOVE which two entities are considered
    /// "overlapping" and eligible to merge (opposite sense from
    /// detect_conflicts, which flags dissimilarity as a conflict).
    #[serde(default = "default_consolidate_threshold")]
    pub similarity_threshold: f64,
    #[serde(default = "default_consolidate_limit")]
    pub limit: i64,
    #[serde(default)]
    pub offset: i64,
    #[serde(default)]
    pub dry_run: bool,
    /// When true, scan the COLDEST entities first (last_accessed ASC) instead
    /// of the most recent — "local dreaming": compress memories that are
    /// fading anyway, before decay archives them individually. Default false
    /// preserves the original recent-window behavior.
    #[serde(default)]
    pub cold_first: bool,
    /// When true, archive the merged source entities after the observation is
    /// created (archive_reason names the observation, so the merge is
    /// traceable and reversible). Verified or importance-floored sources are
    /// never archived — same exemption policy as decay. Default false.
    #[serde(default)]
    pub archive_sources: bool,
    /// #854: workspace scope for this run. `Some(ws)` restricts every source
    /// scan, cluster, evidence link, and archive operation to that workspace,
    /// and derived observations inherit the scope (never silently global).
    /// Requires `global` to be false. Neither `workspace_hash` nor `global`
    /// is an error (fail-closed: ordinary maintenance runs must name a scope).
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// #854: explicit cross-workspace mode for deliberate whole-vault
    /// consolidation. Authorization-gated when the caller carries a host
    /// identity (`requesting_agent_id`): the agent must hold capability
    /// `memory.maintenance.global` in the system scope, or the run is
    /// denied. Mutually exclusive with `workspace_hash`.
    #[serde(default)]
    pub global: bool,
    /// Host identity stamped by the MCP transport (clientInfo.name). Used for
    /// global-mode authorization and stamped on derived records as author
    /// attribution. Never trusted from the model when a host identity exists.
    #[serde(default)]
    pub requesting_agent_id: String,
    /// #884: fold new evidence into existing observations instead of
    /// creating duplicates — near-duplicate clusters/singletons update the
    /// matched observation (proof_count, quotes, updated_at) and
    /// contradictions are reconciled into its journey (history) rather than
    /// blindly overwritten. Default true.
    #[serde(default = "default_refine_existing")]
    pub refine_existing: bool,
    /// #884: cap for exact-quote extraction (chars, 64..=4096, validated
    /// fail-closed by the handler). Quotes are the source `note` verbatim,
    /// truncated at the cap with an ellipsis marker.
    #[serde(default = "default_quote_cap")]
    pub quote_cap_chars: i64,
}

fn default_refine_existing() -> bool {
    true
}

fn default_quote_cap() -> i64 {
    512
}

fn default_consolidate_threshold() -> f64 {
    0.6
}

fn default_consolidate_limit() -> i64 {
    50
}

/// Parameters for the perseus_vault_dream tool (#364) — sleep-time LLM consolidation
/// of episodic memories into higher-order semantic insights. Where
/// `consolidate` mechanically merges near-duplicates, `dream` REASONS over a
/// cluster of related memories via the configured LLM and writes back what
/// they collectively imply (a durable pattern / preference / fact), fully
/// provenance-linked to its sources.
#[derive(Debug, Deserialize)]
pub struct DreamParams {
    /// Category to dream over. When omitted, all categories are scanned
    /// (except derived/output categories: observation, insight, synthesis,
    /// memories) until the entity budget is exhausted.
    #[serde(default)]
    pub category: Option<String>,
    /// Optional topic_path prefix filter applied to the scan.
    #[serde(default)]
    pub topic_path: Option<String>,
    /// Trigram similarity threshold for grouping RELATED (not duplicate)
    /// memories into one cluster. Deliberately lower than consolidate's 0.6:
    /// dreaming wants thematic neighborhoods, not near-copies.
    #[serde(default = "default_dream_threshold")]
    pub similarity_threshold: f64,
    /// Budget cap: maximum entities scanned per run (across categories).
    #[serde(default = "default_dream_max_entities")]
    pub max_entities: i64,
    /// Budget cap: maximum clusters sent to the LLM per run (= max LLM calls).
    #[serde(default = "default_dream_max_clusters")]
    pub max_clusters: i64,
    /// Minimum memories a cluster needs before it is worth dreaming over.
    #[serde(default = "default_dream_min_cluster")]
    pub min_cluster_size: i64,
    /// Report candidate insights without writing anything.
    #[serde(default)]
    pub dry_run: bool,
    /// Scan the COLDEST entities first (default true) — consolidate fading
    /// memories into durable semantic insights before decay claims them.
    #[serde(default = "default_dream_cold_first")]
    pub cold_first: bool,
    /// Archive source entities once an insight citing them is written.
    /// Verified or importance-floored sources are never archived (same
    /// exemption policy as decay and consolidate). Default false.
    #[serde(default)]
    pub archive_sources: bool,
    /// #854: workspace scope for this run. `Some(ws)` restricts every source
    /// scan, cluster, and evidence lookup to that workspace, and derived
    /// insights inherit the scope (never silently global). Requires `global`
    /// to be false. Neither `workspace_hash` nor `global` is an error
    /// (fail-closed: ordinary maintenance runs must name a scope).
    #[serde(default)]
    pub workspace_hash: Option<String>,
    /// #854: explicit cross-workspace mode for deliberate whole-vault
    /// dreaming. Authorization-gated when the caller carries a host identity
    /// (`requesting_agent_id`): the agent must hold capability
    /// `memory.maintenance.global` in the system scope, or the run is
    /// denied. Mutually exclusive with `workspace_hash`.
    #[serde(default)]
    pub global: bool,
    /// Host identity stamped by the MCP transport (clientInfo.name). Used for
    /// global-mode authorization and stamped on derived records as author
    /// attribution. Never trusted from the model when a host identity exists.
    #[serde(default)]
    pub requesting_agent_id: String,
}

fn default_dream_threshold() -> f64 {
    0.3
}

fn default_dream_max_entities() -> i64 {
    100
}

fn default_dream_max_clusters() -> i64 {
    5
}

fn default_dream_min_cluster() -> i64 {
    2
}

fn default_dream_cold_first() -> bool {
    true
}

/// One semantic insight distilled by a dream pass, provenance-linked to the
/// episodic sources that support it.
#[derive(Debug, Serialize, Clone)]
pub struct DreamInsight {
    /// The new insight entity's id (category="insight"), or the EXISTING
    /// entity's id when `deduped` is true.
    pub entity_id: String,
    /// Deterministic key derived from the evidence-set hash — re-running over
    /// an unchanged cluster maps to the same key, so no duplicates spawn.
    pub key: String,
    pub summary: String,
    /// "pattern" | "preference" | "fact" | "habit" | "contradiction".
    pub insight_type: String,
    /// Final certainty: LLM confidence blended with evidence coverage.
    pub confidence: f64,
    /// IDs of the source entities that support this insight (evidence).
    pub source_ids: Vec<String>,
    /// The source category this insight was dreamed from.
    pub category: String,
    /// True when the sources contradict each other — surfaced as a flagged
    /// insight, never a silent merge.
    pub contradiction: bool,
    /// True when an insight with the identical evidence set already existed
    /// (idempotent re-run) — nothing was written.
    pub deduped: bool,
}

/// Result from perseus_vault_dream.
#[derive(Debug, Serialize)]
pub struct DreamReport {
    pub categories_scanned: Vec<String>,
    pub entities_examined: i64,
    /// Clusters actually sent to the LLM this run.
    pub clusters_dreamed: i64,
    pub insights_written: i64,
    /// Insights skipped because the identical evidence set was already dreamed.
    pub insights_deduped: i64,
    pub contradictions_flagged: i64,
    /// Sources archived because archive_sources was set (verified or
    /// importance-floored sources are exempt).
    pub sources_archived: i64,
    pub dry_run: bool,
    pub insights: Vec<DreamInsight>,
    /// #854: effective scope of this run. `Some(ws)` = scoped to that
    /// workspace; `None` with `global=true` = deliberate whole-vault run.
    pub workspace_hash: Option<String>,
    pub global: bool,
}

/// One evidence-tracked observation formed by merging 2+ overlapping entities.
#[derive(Debug, Serialize, Clone)]
pub struct Observation {
    /// The new observation entity's id (category="observation").
    pub entity_id: String,
    pub key: String,
    /// Concatenated/deduplicated summary text of the merged sources.
    pub summary: String,
    /// IDs of the source entities this observation was built from (evidence).
    pub source_ids: Vec<String>,
    /// How many source entities support this observation.
    pub proof_count: i64,
    /// Average certainty across merged sources.
    pub certainty: f64,
    /// #884: last write time (creation or refinement).
    pub updated_at_unix_ms: i64,
    /// #884: staleness snapshot — newer unconsolidated raw facts exist in
    /// the merged-from category (read-time computation wins over storage).
    pub stale: bool,
    /// #884: true when this run folded evidence into an existing
    /// observation instead of creating a fresh one.
    pub refined: bool,
    /// #884: exact-quote evidence refs (source id + verbatim quote).
    pub quotes: Vec<crate::observations::QuoteRef>,
}

/// Result from perseus_vault_consolidate.
#[derive(Debug, Serialize)]
pub struct ConsolidateReport {
    pub category: String,
    pub entities_examined: i64,
    pub observations_created: i64,
    pub source_entities_merged: i64,
    /// Sources archived because archive_sources was set. Always <=
    /// source_entities_merged: verified/importance-floored sources stay live.
    pub sources_archived: i64,
    /// #884: observations updated by folding/reconciling new evidence into
    /// an existing observation (never a blind overwrite; journey preserved).
    pub observations_refined: i64,
    /// #874: folds skipped because the merged body would heavily activate an
    /// entity outside the fold's source set (interference discipline).
    pub interference_skips: i64,
    /// #884: observations whose stored stale flag was recomputed this run.
    pub observations_refreshed: i64,
    /// #884: observations stale after this run (newer unconsolidated facts
    /// exist in their merged-from category).
    pub observations_stale: i64,
    /// #884: exact-quote evidence refs captured this run.
    pub quotes_captured: i64,
    pub dry_run: bool,
    pub observations: Vec<Observation>,
    /// #854: effective scope of this run. `Some(ws)` = scoped to that
    /// workspace; `None` with `global=true` = deliberate whole-vault run.
    pub workspace_hash: Option<String>,
    pub global: bool,
}

// ─── Memory origin + external references (#729/#728) ────────────────────────
// Spec: docs/specs/memory-provenance-and-external-refs.md. Both contracts are
// optional and stored inside body_json under reserved keys ("origin",
// "external_refs") — the same metadata channel recall_when already uses, so
// no schema migration is required and existing entities are valid unchanged.

/// Canonical memory-origin values (spec §1.1).
pub const MEMORY_KINDS: [&str; 5] = ["asserted", "extracted", "inferred", "imported", "observed"];

/// Canonical external-ref relationship values (spec §2.1).
pub const REF_RELATIONSHIPS: [&str; 5] = [
    "about",
    "derived_from",
    "mentions",
    "applies_to",
    "supersedes",
];

/// How a memory came to exist. All fields optional — never guessed when
/// unknown (spec §1.2: absent means unlabeled, not defaulted).
#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct OriginRecord {
    /// asserted | extracted | inferred | imported | observed
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub memory_kind: Option<String>,
    /// e.g. user, capture, slack, confluence, jira, connector:<name>, agent
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_system: Option<String>,
    /// manual | rule_based_extractor | llm_extractor | import | event_feed
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub capture_method: Option<String>,
    /// When the fact was true in the world, if distinct from recorded time.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub observed_at_unix_ms: Option<i64>,
}

/// Write-time evidence retention modes. The mode is required inside every
/// evidence envelope so an absent value has an explicit audit meaning.
pub const EVIDENCE_CAPTURE_MODES: [&str; 6] = [
    "snapshot",
    "hash_only",
    "pointer_only",
    "not_requested",
    "capture_failed",
    "legacy_unknown",
];

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EvidenceEnvelope {
    pub capture_mode: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub resolved_value: Option<serde_json::Value>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub content_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_system: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_ref: Option<String>,
    pub captured_at_unix_ms: i64,
    #[serde(default)]
    pub replayable: bool,
}

/// A first-class pointer from a memory to an external system of record.
/// Canonical ref_value forms per ref_type live in
/// docs/specs/source-anchors-corrections-retention.md §1.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ExternalRef {
    /// ari | url | jira_key | confluence_page | account_id | repo |
    /// pull_request | file | session | custom
    pub ref_type: String,
    pub ref_value: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_system: Option<String>,
    /// about | derived_from | mentions | applies_to | supersedes
    /// (default about when omitted)
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub relationship: Option<String>,
}

/// Canonical retention-policy vocabulary from
/// docs/specs/source-anchors-corrections-retention.md §3.
pub const RETENTION_POLICIES: [&str; 5] = [
    "keep_forever",
    "decay_unless_reinforced",
    "archive_when_superseded",
    "retain_no_autoserve",
    "erase_on",
];

/// Artifact representation semantics (#811). Original bytes are always preserved;
/// derived representations point back to their immutable source artifact.
pub const ARTIFACT_REPRESENTATION_KINDS: [&str; 2] = ["original", "derived"];

#[derive(Debug, Clone, Serialize, Deserialize, Default)]
pub struct ArtifactRepresentation {
    /// original | derived
    #[serde(default = "default_artifact_representation_kind")]
    pub kind: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub derived_from_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub derivation_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub derivation_version: Option<String>,
}

fn default_artifact_representation_kind() -> String {
    "original".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ArtifactBinding {
    pub binding_id: String,
    pub sha256: String,
    pub mime_type: String,
    pub workspace_hash: String,
    pub agent_id: String,
    pub visibility: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub origin: Option<OriginRecord>,
    #[serde(default)]
    pub external_refs: Vec<ExternalRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub retention_policy: Option<String>,
    pub representation: ArtifactRepresentation,
    pub created_at_unix_ms: i64,
    /// #876 governed distillation: set when a bound source entity was
    /// physically erased — serve paths refuse revoked bindings (fail-closed).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revoked_at_unix_ms: Option<i64>,
    /// #876: set when a bound source entity was superseded — the artifact is
    /// still serveable but flagged for operator review / retraining.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub stale_at_unix_ms: Option<i64>,
    /// #876: machine-readable revocation reason (e.g. `source_erased:<digest>`).
    #[serde(default)]
    pub revocation_reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactWhyServed {
    pub reason: String,
    pub workspace_hash: String,
    pub visibility: String,
    pub anchors: Vec<ExternalRef>,
}

/// #879 first-class Hermes profile <-> Vault workspace binding. One row per
/// profile; a workspace may be shared by several profiles (intentional
/// shared memory). `access_mode` drives read-only vs read/write enforcement
/// at the tool boundary; `binding_state` drives lifecycle controls.
#[derive(Debug, Clone, Serialize)]
pub struct WorkspaceBinding {
    pub profile_name: String,
    pub workspace_hash: String,
    pub access_mode: String,
    pub binding_state: String,
    pub quarantine_reason: String,
    pub bound_at_unix_ms: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub rebound_at_unix_ms: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub unbound_at_unix_ms: Option<i64>,
    pub last_seen_unix_ms: i64,
    pub metadata_json: String,
}

impl WorkspaceBinding {
    pub fn is_mutation_allowed(&self) -> bool {
        self.binding_state == "active" && self.access_mode == "read_write"
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactManifestBinding {
    pub binding_id: String,
    pub mime_type: String,
    pub workspace_hash: String,
    pub agent_id: String,
    pub visibility: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub origin: Option<OriginRecord>,
    pub external_refs: Vec<ExternalRef>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub retention_policy: Option<String>,
    pub representation: ArtifactRepresentation,
    pub created_at_unix_ms: i64,
    pub why_served: ArtifactWhyServed,
    /// #876: set when a bound source entity was physically erased — the
    /// artifact is refused by serve paths; exposed here for operators.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub revoked_at_unix_ms: Option<i64>,
    /// #876: set when a bound source entity was superseded — retraining
    /// trigger; the artifact remains serveable.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub stale_at_unix_ms: Option<i64>,
    /// #876: machine-readable revocation reason (e.g. `source_erased:<digest>`).
    #[serde(skip_serializing_if = "String::is_empty")]
    pub revocation_reason: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactStructure {
    pub utf8_text: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_count: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub trailing_newline: Option<bool>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactRetrievalCapabilities {
    pub byte_range: bool,
    pub line_range: bool,
    pub verify_value: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactManifest {
    pub sha256: String,
    pub byte_length: i64,
    pub structure: ArtifactStructure,
    pub significant_signals: Vec<String>,
    pub available_retrievals: ArtifactRetrievalCapabilities,
    pub visible_binding_count: i64,
    pub bindings: Vec<ArtifactManifestBinding>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ArtifactAnchor {
    pub sha256: String,
    pub byte_start: i64,
    pub byte_end: i64,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_start: Option<i64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub line_end: Option<i64>,
}
