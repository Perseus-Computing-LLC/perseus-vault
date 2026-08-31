//! Bounded, hash-only per-candidate selection decisions (#1140).
//!
//! The projection explains how an already-materialized fused candidate set was
//! governed and selected. It never stores queries, memory bodies, prompts, or
//! provider payloads. Replay fingerprints intentionally exclude wall-clock data.

use serde::Serialize;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};

pub const SELECTION_DECISIONS_SCHEMA_VERSION: &str = "perseus-vault-selection-decisions/v1";
pub(crate) const MAX_DECISIONS: usize = 4096;
const DISPOSITIONS: [&str; 11] = [
    "selected",
    "dropped_budget",
    "dropped_type_cap",
    "dropped_caller_limit",
    "dropped_coverage",
    "filtered_lifecycle",
    "filtered_scope",
    "filtered_policy",
    "abstained",
    "unavailable",
    "not_in_candidate_pool",
];
const ARM_STATUSES: [&str; 5] = ["ok", "empty", "degraded", "skipped", "unavailable"];

/// The configuration that changes candidate membership, ordering, or delivery.
/// It is hashed into the public trace rather than serialized into it because
/// filters can contain sensitive scope/category values.
#[derive(Debug, Clone, Serialize)]
pub struct SelectionPolicy {
    pub mode: String,
    pub query_sha256: String,
    pub limit: usize,
    pub token_budget: i64,
    pub budget_profile: Option<String>,
    pub rerank: bool,
    pub multihop: bool,
    pub strategies: Vec<String>,
    pub strategy_weights: BTreeMap<String, f64>,
    pub filters: BTreeMap<String, String>,
}

impl SelectionPolicy {
    pub fn digest(&self) -> Result<String, String> {
        if self.mode.is_empty() || self.mode.len() > 64 {
            return Err("selection policy mode must be bounded and non-empty".to_string());
        }
        validate_sha256(&self.query_sha256, "selection policy query_sha256")?;
        if self.token_budget <= 0 {
            return Err("selection policy token_budget must be positive".to_string());
        }
        if self.limit > MAX_DECISIONS {
            return Err("selection policy limit exceeds selection decision bound".to_string());
        }
        let mut canonical = self.clone();
        canonical.strategies.sort();
        canonical.strategies.dedup();
        canonical_digest(&canonical)
    }
}

/// One bounded decision for an opaque entity/candidate identifier.
#[derive(Debug, Clone, Serialize)]
pub struct SelectionDecision {
    pub candidate_id: String,
    /// Hash-only lineage commitment; absent only for legacy producers.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_chain_commitment: Option<String>,
    /// `known`, `unknown`, or `malformed` lineage state.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub source_chain_status: String,
    pub source_arm_ranks: BTreeMap<String, u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fused_rank: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub fused_score: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rerank_score: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub validity_multiplier: Option<f64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_estimate: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub token_estimator: Option<String>,
    pub eligible: bool,
    pub selected: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub final_rank: Option<u32>,
    pub disposition: String,
}

/// A source-arm capability state. Empty/degraded/unavailable arms are explicit
/// here rather than represented by fabricated candidate records.
#[derive(Debug, Clone, Serialize)]
pub struct SelectionArmState {
    pub arm: String,
    pub status: String,
    pub candidate_count: usize,
}

/// A versioned, bounded, hash-only selection projection attached to fused_trace.
#[derive(Debug, Clone, Serialize)]
pub struct SelectionDecisionTrace {
    pub schema_version: String,
    pub policy_digest: String,
    pub arms: Vec<SelectionArmState>,
    pub candidate_count: usize,
    pub eligible_count: usize,
    /// Candidates admitted to the pre-token-budget delivery order.
    pub retained_count: usize,
    pub delivered_count: usize,
    pub abstained: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub abstention_reason: Option<String>,
    pub token_budget: i64,
    pub estimated_tokens_used: i64,
    pub candidates: Vec<SelectionDecision>,
    pub delivered_order: Vec<String>,
    pub replay_fingerprint_sha256: String,
}

#[derive(Serialize)]
struct ReplayMaterial<'a> {
    schema_version: &'a str,
    policy_digest: &'a str,
    arms: &'a [SelectionArmState],
    candidate_count: usize,
    eligible_count: usize,
    retained_count: usize,
    delivered_count: usize,
    abstained: bool,
    abstention_reason: &'a Option<String>,
    token_budget: i64,
    estimated_tokens_used: i64,
    candidates: &'a [SelectionDecision],
    delivered_order: &'a [String],
}

impl SelectionDecisionTrace {
    /// Build and seal a trace. Candidate records are sorted by opaque ID so
    /// arm discovery order cannot change the report or replay fingerprint.
    pub fn build(
        policy: SelectionPolicy,
        mut candidates: Vec<SelectionDecision>,
        retained_count: usize,
        estimated_tokens_used: i64,
        mut arms: Vec<SelectionArmState>,
        delivered_order: Vec<String>,
        abstention_reason: Option<String>,
    ) -> Result<Self, String> {
        candidates.sort_by(|left, right| left.candidate_id.cmp(&right.candidate_id));
        arms.sort_by(|left, right| left.arm.cmp(&right.arm));
        let policy_digest = policy.digest()?;
        let trace = Self {
            schema_version: SELECTION_DECISIONS_SCHEMA_VERSION.to_string(),
            policy_digest,
            arms,
            candidate_count: candidates.len(),
            eligible_count: candidates.iter().filter(|item| item.eligible).count(),
            retained_count,
            delivered_count: delivered_order.len(),
            abstained: abstention_reason.is_some(),
            abstention_reason,
            token_budget: policy.token_budget,
            estimated_tokens_used,
            candidates,
            delivered_order,
            replay_fingerprint_sha256: String::new(),
        };
        trace.validate_without_fingerprint()?;
        let fingerprint = trace.compute_replay_fingerprint()?;
        let mut sealed = trace;
        sealed.replay_fingerprint_sha256 = fingerprint;
        sealed.validate()?;
        Ok(sealed)
    }

    pub fn validate(&self) -> Result<(), String> {
        self.validate_without_fingerprint()?;
        validate_sha256(&self.replay_fingerprint_sha256, "replay_fingerprint_sha256")?;
        let actual = self.compute_replay_fingerprint()?;
        if actual != self.replay_fingerprint_sha256 {
            return Err("replay_fingerprint_sha256 does not match selection decisions".to_string());
        }
        Ok(())
    }

    pub fn replay_fingerprint(&self) -> Result<String, String> {
        self.validate()?;
        Ok(self.replay_fingerprint_sha256.clone())
    }

    /// Re-seal the trace after a later governed serving stage changes the
    /// delivered set or order. The caller must update candidate dispositions
    /// before invoking this method; all invariants and the hash binding are
    /// rechecked.
    pub(crate) fn reseal(&mut self) -> Result<(), String> {
        self.validate_without_fingerprint()?;
        self.replay_fingerprint_sha256 = self.compute_replay_fingerprint()?;
        self.validate()
    }

    fn validate_without_fingerprint(&self) -> Result<(), String> {
        if self.schema_version != SELECTION_DECISIONS_SCHEMA_VERSION {
            return Err(format!(
                "unsupported selection decision schema_version: {}",
                self.schema_version
            ));
        }
        validate_sha256(&self.policy_digest, "policy_digest")?;
        if self.candidate_count != self.candidates.len() {
            return Err("candidate_count does not match candidates".to_string());
        }
        if self.candidate_count > MAX_DECISIONS {
            return Err("selection decision candidate bound exceeded".to_string());
        }
        if self.token_budget <= 0 {
            return Err("token_budget must be positive".to_string());
        }
        if self.estimated_tokens_used < 0 {
            return Err("estimated_tokens_used must be non-negative".to_string());
        }
        let mut arm_names = BTreeSet::new();
        for arm in &self.arms {
            validate_identifier("selection arm", &arm.arm)?;
            if !ARM_STATUSES.contains(&arm.status.as_str()) {
                return Err(format!("unsupported selection arm status: {}", arm.status));
            }
            if !arm_names.insert(arm.arm.as_str()) {
                return Err("selection arm names must be unique".to_string());
            }
            if arm.candidate_count > MAX_DECISIONS {
                return Err("selection arm candidate bound exceeded".to_string());
            }
        }
        if self.retained_count > self.eligible_count {
            return Err("retained_count cannot exceed eligible_count".to_string());
        }
        if self.delivered_count != self.delivered_order.len() {
            return Err("delivered_count does not match delivered_order".to_string());
        }
        if self.delivered_count > self.retained_count {
            return Err("delivered_count cannot exceed retained_count".to_string());
        }
        if self.abstained != self.abstention_reason.is_some() {
            return Err("abstained and abstention_reason must agree".to_string());
        }
        if self.abstained && self.delivered_count != 0 {
            return Err("an abstained trace cannot deliver candidates".to_string());
        }
        if let Some(reason) = &self.abstention_reason {
            validate_identifier("abstention_reason", reason)?;
        }
        let mut ids = BTreeSet::new();
        let mut eligible_count = 0usize;
        let mut selected = BTreeMap::new();
        for item in &self.candidates {
            validate_identifier("candidate_id", &item.candidate_id)?;
            if let Some(commitment) = &item.source_chain_commitment {
                validate_sha256(commitment, "source_chain_commitment")?;
            }
            if !["known", "unknown", "malformed"].contains(&item.source_chain_status.as_str()) {
                return Err("source_chain_status must be known, unknown, or malformed".to_string());
            }
            if !ids.insert(item.candidate_id.as_str()) {
                return Err("candidate_id must be unique".to_string());
            }
            if item.source_arm_ranks.is_empty() {
                return Err(format!(
                    "candidate {} must identify at least one source arm",
                    item.candidate_id
                ));
            }
            for (arm, rank) in &item.source_arm_ranks {
                validate_identifier("source arm", arm)?;
                if *rank == 0 {
                    return Err("source arm ranks are one-based".to_string());
                }
            }
            for (label, score) in [
                ("fused_score", item.fused_score),
                ("rerank_score", item.rerank_score),
                ("validity_multiplier", item.validity_multiplier),
            ] {
                if score.is_some_and(|value| !value.is_finite()) {
                    return Err(format!("{label} must be finite when present"));
                }
            }
            for (label, rank) in [
                ("fused_rank", item.fused_rank),
                ("final_rank", item.final_rank),
            ] {
                if rank == Some(0) {
                    return Err(format!("{label} is one-based when present"));
                }
            }
            match (&item.token_estimate, &item.token_estimator) {
                (Some(tokens), Some(estimator)) => {
                    if *tokens < 1 {
                        return Err("token_estimate must be positive".to_string());
                    }
                    validate_identifier("token_estimator", estimator)?;
                }
                (None, None) => {}
                _ => return Err("token_estimate and token_estimator must agree".to_string()),
            }
            if !DISPOSITIONS.contains(&item.disposition.as_str()) {
                return Err(format!("unsupported disposition: {}", item.disposition));
            }
            if item.selected != (item.disposition == "selected") {
                return Err(format!(
                    "candidate {} selected/disposition mismatch",
                    item.candidate_id
                ));
            }
            if item.selected {
                if !item.eligible || item.final_rank.is_none() {
                    return Err(format!(
                        "selected candidate {} must be eligible and ranked",
                        item.candidate_id
                    ));
                }
                selected.insert(item.candidate_id.clone(), item.final_rank.unwrap());
            } else if item.final_rank.is_some() {
                return Err(format!(
                    "unselected candidate {} cannot have a final rank",
                    item.candidate_id
                ));
            }
            if item.eligible {
                eligible_count += 1;
            }
        }
        if eligible_count != self.eligible_count {
            return Err("eligible_count does not match candidate decisions".to_string());
        }
        let mut delivered_ids = BTreeSet::new();
        for (index, id) in self.delivered_order.iter().enumerate() {
            validate_identifier("delivered candidate_id", id)?;
            if !delivered_ids.insert(id.as_str()) {
                return Err("delivered_order must not contain duplicates".to_string());
            }
            let Some(rank) = selected.get(id) else {
                return Err(format!("delivered candidate {id} is not selected"));
            };
            if *rank as usize != index + 1 {
                return Err(format!(
                    "delivered candidate {id} has inconsistent final rank"
                ));
            }
        }
        if delivered_ids.len() != selected.len() {
            return Err("every selected candidate must appear in delivered_order".to_string());
        }
        Ok(())
    }

    fn compute_replay_fingerprint(&self) -> Result<String, String> {
        canonical_digest(&ReplayMaterial {
            schema_version: &self.schema_version,
            policy_digest: &self.policy_digest,
            arms: &self.arms,
            candidate_count: self.candidate_count,
            eligible_count: self.eligible_count,
            retained_count: self.retained_count,
            delivered_count: self.delivered_count,
            abstained: self.abstained,
            abstention_reason: &self.abstention_reason,
            token_budget: self.token_budget,
            estimated_tokens_used: self.estimated_tokens_used,
            candidates: &self.candidates,
            delivered_order: &self.delivered_order,
        })
    }
}

fn canonical_digest<T: Serialize>(value: &T) -> Result<String, String> {
    let bytes = serde_json::to_vec(value)
        .map_err(|error| format!("selection decision serialization failed: {error}"))?;
    Ok(format!("{:x}", Sha256::digest(bytes)))
}

fn validate_sha256(value: &str, label: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(format!("{label} must be a lowercase SHA-256 digest"));
    }
    Ok(())
}

fn validate_identifier(label: &str, value: &str) -> Result<(), String> {
    if value.is_empty()
        || value.len() > 256
        || value.chars().any(|c| c.is_control() || c.is_whitespace())
    {
        return Err(format!(
            "{label} must be a bounded non-whitespace identifier"
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn policy() -> SelectionPolicy {
        SelectionPolicy {
            mode: "fused".to_string(),
            query_sha256: "a".repeat(64),
            limit: 2,
            token_budget: 20,
            budget_profile: None,
            rerank: false,
            multihop: false,
            strategies: vec!["fts5".to_string(), "dense".to_string()],
            strategy_weights: BTreeMap::new(),
            filters: BTreeMap::new(),
        }
    }

    fn decision(
        id: &str,
        disposition: &str,
        eligible: bool,
        selected: bool,
        final_rank: Option<u32>,
    ) -> SelectionDecision {
        SelectionDecision {
            candidate_id: id.to_string(),
            source_chain_commitment: None,
            source_chain_status: "unknown".to_string(),
            source_arm_ranks: BTreeMap::from([("fts5".to_string(), 1)]),
            fused_rank: Some(1),
            fused_score: Some(0.5),
            rerank_score: None,
            validity_multiplier: None,
            token_estimate: Some(7),
            token_estimator: Some("chars-div-4-v1".to_string()),
            eligible,
            selected,
            final_rank,
            disposition: disposition.to_string(),
        }
    }

    #[test]
    fn trace_is_sorted_and_replay_stable_across_discovery_order() {
        let first = SelectionDecisionTrace::build(
            policy(),
            vec![
                decision("candidate-b", "dropped_budget", true, false, None),
                decision("candidate-a", "selected", true, true, Some(1)),
            ],
            2,
            7,
            Vec::new(),
            vec!["candidate-a".to_string()],
            None,
        )
        .unwrap();
        let second = SelectionDecisionTrace::build(
            policy(),
            vec![
                decision("candidate-a", "selected", true, true, Some(1)),
                decision("candidate-b", "dropped_budget", true, false, None),
            ],
            2,
            7,
            Vec::new(),
            vec!["candidate-a".to_string()],
            None,
        )
        .unwrap();

        assert_eq!(
            first
                .candidates
                .iter()
                .map(|item| item.candidate_id.as_str())
                .collect::<Vec<_>>(),
            vec!["candidate-a", "candidate-b"]
        );
        assert_eq!(
            first.replay_fingerprint_sha256,
            second.replay_fingerprint_sha256
        );
    }

    #[test]
    fn trace_counts_eligibility_and_delivery_separately() {
        let trace = SelectionDecisionTrace::build(
            policy(),
            vec![
                decision("selected", "selected", true, true, Some(1)),
                decision("budget", "dropped_budget", true, false, None),
                decision("scope", "filtered_scope", false, false, None),
            ],
            2,
            7,
            Vec::new(),
            vec!["selected".to_string()],
            None,
        )
        .unwrap();

        assert_eq!(trace.candidate_count, 3);
        assert_eq!(trace.eligible_count, 2);
        assert_eq!(trace.retained_count, 2);
        assert_eq!(trace.delivered_count, 1);
    }

    #[test]
    fn missing_scores_are_explicit_and_raw_content_has_no_serialization_slot() {
        let mut item = decision("candidate", "unavailable", true, false, None);
        item.fused_score = None;
        item.token_estimate = None;
        item.token_estimator = None;
        let trace =
            SelectionDecisionTrace::build(policy(), vec![item], 0, 0, Vec::new(), Vec::new(), None)
                .unwrap();
        let serialized = serde_json::to_string(&trace).unwrap();

        assert!(!serialized.contains("body"));
        assert!(!serialized.contains("prompt"));
        assert!(!serialized.contains("raw"));
        assert!(!serialized.contains("candidate text"));
        assert!(!serialized.contains("fused_score"));
        assert!(!serialized.contains("token_estimate"));
    }

    #[test]
    fn tampering_with_a_decision_invalidates_the_replay_fingerprint() {
        let trace = SelectionDecisionTrace::build(
            policy(),
            vec![decision("candidate", "selected", true, true, Some(1))],
            1,
            7,
            Vec::new(),
            vec!["candidate".to_string()],
            None,
        )
        .unwrap();
        let mut tampered = trace.clone();
        tampered.candidates[0].disposition = "dropped_budget".to_string();

        assert!(tampered.validate().is_err());

        let mut budget_tampered = trace;
        budget_tampered.token_budget += 1;
        assert!(budget_tampered.validate().is_err());
    }

    #[test]
    fn unavailable_arms_are_explicit_without_fabricating_candidates() {
        let trace = SelectionDecisionTrace::build(
            policy(),
            vec![decision("candidate", "unavailable", true, false, None)],
            0,
            0,
            vec![SelectionArmState {
                arm: "dense".to_string(),
                status: "unavailable".to_string(),
                candidate_count: 0,
            }],
            Vec::new(),
            None,
        )
        .unwrap();

        assert_eq!(trace.arms[0].status, "unavailable");
        assert_eq!(trace.arms[0].candidate_count, 0);
    }

    #[test]
    fn abstention_reason_is_explicit_when_no_candidate_is_eligible() {
        let trace = SelectionDecisionTrace::build(
            policy(),
            vec![decision("filtered", "filtered_policy", false, false, None)],
            0,
            0,
            Vec::new(),
            Vec::new(),
            Some("no_eligible_candidates".to_string()),
        )
        .unwrap();
        assert!(trace.abstained);
        assert_eq!(
            trace.abstention_reason.as_deref(),
            Some("no_eligible_candidates")
        );
    }

    #[test]
    fn invalid_dispositions_and_inconsistent_states_fail_closed() {
        let mut invalid = decision("candidate", "made_up", true, false, None);
        assert!(SelectionDecisionTrace::build(
            policy(),
            vec![invalid.clone()],
            0,
            0,
            Vec::new(),
            Vec::new(),
            None
        )
        .is_err());

        invalid.disposition = "selected".to_string();
        assert!(SelectionDecisionTrace::build(
            policy(),
            vec![invalid],
            0,
            0,
            Vec::new(),
            Vec::new(),
            None
        )
        .is_err());
    }
}
