//! Hash-only trust admission and poisoning acceptance gates (#821).
//!
//! This is deliberately a policy evaluator over metadata and digests. Raw
//! prompts, documents, email bodies, tool output, and secrets never enter the
//! durable decision evidence represented here.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

pub const ADMISSION_SCHEMA_VERSION: &str = "perseus-vault-memory-admission/v1";

const OUTCOMES: [&str; 7] = [
    "admitted",
    "proposed",
    "quarantined",
    "suppressed",
    "escalated",
    "abstained",
    "revoked",
];

/// Stable public admission classes composed over the existing trust-admission
/// outcomes. These are a reporting/evidence taxonomy, not a second lifecycle.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum AdmissionOutcomeClass {
    Save,
    Drop,
    Block,
    PendingApproval,
}

impl AdmissionOutcomeClass {
    pub fn from_outcome(outcome: &str) -> Result<Self, String> {
        match outcome {
            "admitted" => Ok(Self::Save),
            "suppressed" => Ok(Self::Drop),
            "abstained" | "revoked" => Ok(Self::Block),
            "proposed" | "quarantined" | "escalated" => Ok(Self::PendingApproval),
            other => Err(format!("unsupported admission outcome: {other}")),
        }
    }
}

impl std::fmt::Display for AdmissionOutcomeClass {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let value = match self {
            Self::Save => "save",
            Self::Drop => "drop",
            Self::Block => "block",
            Self::PendingApproval => "pending_approval",
        };
        f.write_str(value)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AdmissionRequest {
    pub record_digest: String,
    pub source_identity: String,
    pub authorization_scope: String,
    pub ingestion_channel: String,
    pub workspace_hash: String,
    pub source_trust: String,
    pub valid_from_unix_ms: i64,
    pub recorded_at_unix_ms: i64,
    pub task_relevance_bps: u16,
    #[serde(default)]
    pub instruction_bearing: bool,
    #[serde(default)]
    pub contradicts_authoritative: bool,
    /// Optional immutable source event needed before an authoritative claim
    /// can become durable authority. Missing validation keeps the record as a
    /// reviewable proposal rather than silently treating the claim as fact.
    #[serde(default)]
    pub source_event_id: Option<String>,
    #[serde(default)]
    pub actor_kind: Option<String>,
    #[serde(default)]
    pub actor_identity: Option<String>,
    #[serde(default)]
    pub validated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct AdmissionEvidence {
    pub schema_version: String,
    pub outcome: String,
    /// Hash-covered stable reporting class. `None` is accepted only for
    /// pre-classification evidence; readers derive it from `outcome`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub outcome_class: Option<AdmissionOutcomeClass>,
    pub reason_codes: Vec<String>,
    pub record_digest: String,
    pub source_identity: String,
    pub authorization_scope: String,
    pub ingestion_channel: String,
    pub workspace_hash: String,
    pub authoritative: bool,
    pub durable: bool,
    pub decision_digest: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub revocation_digest: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub source_event_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor_kind: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor_identity: Option<String>,
}

impl AdmissionEvidence {
    pub fn validate(&self) -> Result<(), String> {
        if self.schema_version != ADMISSION_SCHEMA_VERSION {
            return Err(format!(
                "unsupported schema_version: {}",
                self.schema_version
            ));
        }
        if !OUTCOMES.contains(&self.outcome.as_str()) {
            return Err(format!("unsupported outcome: {}", self.outcome));
        }
        self.outcome_class()?;
        for (label, value) in [
            ("record_digest", self.record_digest.as_str()),
            ("decision_digest", self.decision_digest.as_str()),
        ] {
            validate_sha256(label, value)?;
        }
        for (label, value) in [
            ("source_identity", self.source_identity.as_str()),
            ("authorization_scope", self.authorization_scope.as_str()),
            ("ingestion_channel", self.ingestion_channel.as_str()),
            ("workspace_hash", self.workspace_hash.as_str()),
        ] {
            validate_identifier(label, value)?;
        }
        if self.reason_codes.is_empty() || self.reason_codes.len() > 16 {
            return Err("reason_codes must contain between 1 and 16 entries".to_string());
        }
        for reason in &self.reason_codes {
            validate_identifier("reason_code", reason)?;
        }
        if let Some(digest) = &self.revocation_digest {
            validate_sha256("revocation_digest", digest)?;
        }
        if self.authoritative && (!self.durable || self.outcome != "admitted") {
            return Err("only durable admitted evidence may be authoritative".to_string());
        }
        let mut unsigned = self.clone();
        unsigned.decision_digest.clear();
        let expected = canonical_digest(&unsigned)?;
        if self.decision_digest != expected {
            return Err("decision_digest does not match admission evidence".to_string());
        }
        if self.outcome == "proposed" && self.authoritative {
            return Err("proposed evidence cannot be authoritative".to_string());
        }
        if matches!(
            self.outcome.as_str(),
            "quarantined" | "suppressed" | "escalated" | "abstained" | "revoked"
        ) && self.authoritative
        {
            return Err("non-admitted evidence cannot be authoritative".to_string());
        }
        Ok(())
    }

    /// Return the stable class, validating an explicitly stored class against
    /// the canonical mapping. Legacy evidence without the field is readable.
    pub fn outcome_class(&self) -> Result<AdmissionOutcomeClass, String> {
        let expected = AdmissionOutcomeClass::from_outcome(&self.outcome)?;
        if let Some(actual) = self.outcome_class {
            if actual != expected {
                return Err(format!(
                    "outcome_class {} does not match outcome {}",
                    actual, self.outcome
                ));
            }
            Ok(actual)
        } else {
            Ok(expected)
        }
    }

    /// Resolve a pending candidate through an explicit human approval. The
    /// transition is hash-covered by the new decision digest; the reviewer's
    /// audit identity is recorded by the caller's journal event rather than
    /// copied into candidate content.
    pub fn approve(&mut self, reason: &str) -> Result<(), String> {
        validate_review_reason("approval_reason", reason)?;
        if self.outcome_class()? != AdmissionOutcomeClass::PendingApproval {
            return Err("only pending_approval evidence may be approved".to_string());
        }
        self.outcome = "admitted".to_string();
        self.outcome_class = Some(AdmissionOutcomeClass::Save);
        self.authoritative = true;
        self.durable = true;
        self.reason_codes.push("human_approved".to_string());
        self.reason_codes
            .push(format!("review_reason_{}", digest_text(reason)));
        self.reason_codes.sort();
        self.reason_codes.dedup();
        self.decision_digest.clear();
        self.decision_digest = canonical_digest(self)?;
        self.validate()
    }

    /// Resolve a pending candidate through an explicit human rejection. DROP
    /// maps to the existing relevance-suppression outcome; BLOCK maps to the
    /// existing fail-closed scope/policy outcome. Neither can become active.
    pub fn reject(&mut self, class: AdmissionOutcomeClass, reason: &str) -> Result<(), String> {
        validate_review_reason("rejection_reason", reason)?;
        if self.outcome_class()? != AdmissionOutcomeClass::PendingApproval {
            return Err("only pending_approval evidence may be rejected".to_string());
        }
        let (outcome, marker) = match class {
            AdmissionOutcomeClass::Drop => ("suppressed", "human_rejected_drop"),
            AdmissionOutcomeClass::Block => ("abstained", "human_rejected_block"),
            AdmissionOutcomeClass::Save | AdmissionOutcomeClass::PendingApproval => {
                return Err("rejection class must be drop or block".to_string())
            }
        };
        self.outcome = outcome.to_string();
        self.outcome_class = Some(class);
        self.authoritative = false;
        self.durable = false;
        self.reason_codes.push(marker.to_string());
        self.reason_codes
            .push(format!("review_reason_{}", digest_text(reason)));
        self.reason_codes.sort();
        self.reason_codes.dedup();
        self.decision_digest.clear();
        self.decision_digest = canonical_digest(self)?;
        self.validate()
    }

    pub fn downgrade_to_proposed(&mut self, reason: &str) -> Result<(), String> {
        validate_identifier("downgrade_reason", reason)?;
        self.outcome = "proposed".to_string();
        self.outcome_class = Some(AdmissionOutcomeClass::PendingApproval);
        self.authoritative = false;
        self.durable = true;
        self.reason_codes.push(reason.to_string());
        self.reason_codes.sort();
        self.reason_codes.dedup();
        self.decision_digest.clear();
        self.decision_digest = canonical_digest(self)?;
        self.validate()
    }

    pub fn can_activate(&self, query_workspace: &str, query_relevance_bps: u16) -> bool {
        self.outcome == "admitted"
            && self.authoritative
            && self.durable
            && self.workspace_hash == query_workspace
            && query_relevance_bps >= 5000
    }

    pub fn revoke(&mut self, actor_digest: &str, reason: &str) -> Result<(), String> {
        validate_sha256("actor_digest", actor_digest)?;
        validate_identifier("revocation_reason", reason)?;
        let material = (
            &self.schema_version,
            &self.decision_digest,
            actor_digest,
            reason,
            "revoked",
        );
        self.revocation_digest = Some(canonical_digest(&material)?);
        self.outcome = "revoked".to_string();
        self.outcome_class = Some(AdmissionOutcomeClass::Block);
        self.authoritative = false;
        self.durable = true;
        self.reason_codes.push("operator_revoked".to_string());
        self.decision_digest.clear();
        self.decision_digest = canonical_digest(self)?;
        self.validate()
    }
}

pub fn evaluate(request: &AdmissionRequest) -> Result<AdmissionEvidence, String> {
    validate_request(request)?;
    let (outcome, reasons, authoritative, durable) =
        if request.authorization_scope != request.workspace_hash {
            ("abstained", vec!["workspace_scope_mismatch"], false, false)
        } else if request.task_relevance_bps < 5000 {
            ("suppressed", vec!["task_irrelevant"], false, false)
        } else if request.instruction_bearing && request.source_trust == "untrusted" {
            (
                "quarantined",
                vec!["untrusted_instruction_bearing"],
                false,
                true,
            )
        } else if request.contradicts_authoritative {
            (
                "escalated",
                vec!["contradicts_authoritative_record"],
                false,
                true,
            )
        } else if request.source_trust == "authoritative"
            && (!request.validated || request.source_event_id.is_none())
        {
            ("proposed", vec!["source_validation_required"], false, true)
        } else if request.source_trust == "untrusted" {
            ("quarantined", vec!["untrusted_source"], false, true)
        } else if request.source_trust == "authoritative" {
            (
                "admitted",
                vec!["authorized_authoritative_source"],
                true,
                true,
            )
        } else {
            (
                "proposed",
                vec!["trusted_source_requires_authority"],
                false,
                true,
            )
        };
    let mut evidence = AdmissionEvidence {
        schema_version: ADMISSION_SCHEMA_VERSION.to_string(),
        outcome: outcome.to_string(),
        outcome_class: Some(AdmissionOutcomeClass::from_outcome(outcome)?),
        reason_codes: reasons.into_iter().map(str::to_string).collect(),
        record_digest: request.record_digest.clone(),
        source_identity: request.source_identity.clone(),
        authorization_scope: request.authorization_scope.clone(),
        ingestion_channel: request.ingestion_channel.clone(),
        workspace_hash: request.workspace_hash.clone(),
        authoritative,
        durable,
        decision_digest: String::new(),
        revocation_digest: None,
        source_event_id: request.source_event_id.clone(),
        actor_kind: request.actor_kind.clone(),
        actor_identity: request.actor_identity.clone(),
    };
    evidence.decision_digest = canonical_digest(&evidence)?;
    evidence.validate()?;
    Ok(evidence)
}

fn validate_request(request: &AdmissionRequest) -> Result<(), String> {
    validate_sha256("record_digest", &request.record_digest)?;
    for (label, value) in [
        ("source_identity", request.source_identity.as_str()),
        ("authorization_scope", request.authorization_scope.as_str()),
        ("ingestion_channel", request.ingestion_channel.as_str()),
        ("workspace_hash", request.workspace_hash.as_str()),
        ("source_trust", request.source_trust.as_str()),
    ] {
        validate_identifier(label, value)?;
    }
    if !["untrusted", "trusted", "authoritative"].contains(&request.source_trust.as_str()) {
        return Err(format!(
            "unsupported source_trust: {}",
            request.source_trust
        ));
    }
    for (label, value) in [
        ("source_event_id", request.source_event_id.as_deref()),
        ("actor_kind", request.actor_kind.as_deref()),
        ("actor_identity", request.actor_identity.as_deref()),
    ] {
        if let Some(value) = value {
            validate_identifier(label, value)?;
        }
    }
    if request.valid_from_unix_ms < 0 || request.recorded_at_unix_ms < request.valid_from_unix_ms {
        return Err("invalid valid/recorded time interval".to_string());
    }
    if request.task_relevance_bps > 10_000 {
        return Err("task_relevance_bps must be between 0 and 10000".to_string());
    }
    Ok(())
}

pub fn digest_text(value: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(value.as_bytes());
    format!("{:x}", hasher.finalize())
}

/// Environment-held key used to prove that an admission source was created by
/// the authenticated transport boundary. Only a digest of the resulting HMAC
/// is persisted in the journal; the raw attestation is never durable.
pub(crate) const ADMISSION_SOURCE_HMAC_KEY_ENV: &str = "PERSEUS_VAULT_ADMISSION_SOURCE_HMAC_KEY";

pub(crate) fn admission_source_attestation_payload(
    evaluated: &Value,
    requesting_agent_id: &str,
) -> Result<String, String> {
    let object = evaluated
        .as_object()
        .ok_or("admission_source evaluated must be an object")?;
    let field = |name: &str| {
        object
            .get(name)
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty())
            .ok_or_else(|| format!("admission_source attestation requires evaluated.{name}"))
    };
    if requesting_agent_id.trim().is_empty() {
        return Err("admission_source attestation requires requesting_agent_id".to_string());
    }
    Ok(json!({
        "record_digest": field("record_digest")?,
        "source_identity": field("source_identity")?,
        "workspace_hash": field("workspace_hash")?,
        "actor_kind": field("actor_kind")?,
        "actor_identity": field("actor_identity")?,
        "requesting_agent_id": requesting_agent_id,
    })
    .to_string())
}

pub(crate) fn admission_source_hmac_hex(key: &str, payload: &str) -> String {
    const BLOCK_SIZE: usize = 64;
    let mut key_block = [0u8; BLOCK_SIZE];
    if key.as_bytes().len() > BLOCK_SIZE {
        let digest = Sha256::digest(key.as_bytes());
        key_block[..digest.len()].copy_from_slice(&digest);
    } else {
        key_block[..key.len()].copy_from_slice(key.as_bytes());
    }
    let mut inner_pad = [0x36u8; BLOCK_SIZE];
    let mut outer_pad = [0x5cu8; BLOCK_SIZE];
    for index in 0..BLOCK_SIZE {
        inner_pad[index] ^= key_block[index];
        outer_pad[index] ^= key_block[index];
    }
    let mut inner = Sha256::new();
    inner.update(inner_pad);
    inner.update(payload.as_bytes());
    let inner_digest = inner.finalize();
    let mut outer = Sha256::new();
    outer.update(outer_pad);
    outer.update(inner_digest);
    let digest = outer.finalize();
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

pub(crate) fn admission_source_attestation_digest(
    evaluated: &Value,
    requesting_agent_id: &str,
) -> Result<String, String> {
    let key = match std::env::var(ADMISSION_SOURCE_HMAC_KEY_ENV) {
        Ok(value) => value,
        Err(_) => {
            #[cfg(test)]
            {
                "test-admission-source-key".to_string()
            }
            #[cfg(not(test))]
            {
                return Err(format!("{ADMISSION_SOURCE_HMAC_KEY_ENV} is not configured"));
            }
        }
    };
    let key = key.trim();
    if key.is_empty() {
        return Err(format!("{ADMISSION_SOURCE_HMAC_KEY_ENV} is not configured"));
    }
    let payload = admission_source_attestation_payload(evaluated, requesting_agent_id)?;
    Ok(digest_text(&admission_source_hmac_hex(key, &payload)))
}

fn canonical_digest<T: Serialize>(value: &T) -> Result<String, String> {
    let bytes =
        serde_json::to_vec(value).map_err(|e| format!("admission serialization failed: {e}"))?;
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    Ok(format!("{:x}", hasher.finalize()))
}

fn validate_sha256(label: &str, value: &str) -> Result<(), String> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
    {
        return Err(format!("{label} must be a lowercase SHA-256 value"));
    }
    Ok(())
}

fn validate_review_reason(label: &str, value: &str) -> Result<(), String> {
    if value.trim().is_empty() || value.len() > 256 || value.chars().any(|c| c.is_control()) {
        return Err(format!(
            "{label} must be non-empty, bounded, and free of control characters"
        ));
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

    fn digest(seed: &str) -> String {
        let mut hasher = Sha256::new();
        hasher.update(seed.as_bytes());
        format!("{:x}", hasher.finalize())
    }

    fn request(trust: &str) -> AdmissionRequest {
        AdmissionRequest {
            record_digest: digest("record"),
            source_identity: "email-42".to_string(),
            authorization_scope: "workspace-a".to_string(),
            ingestion_channel: "connector-email".to_string(),
            workspace_hash: "workspace-a".to_string(),
            source_trust: trust.to_string(),
            valid_from_unix_ms: 100,
            recorded_at_unix_ms: 110,
            task_relevance_bps: 9000,
            instruction_bearing: false,
            contradicts_authoritative: false,
            source_event_id: (trust == "authoritative").then(|| "event-42".to_string()),
            actor_kind: Some("connector".to_string()),
            actor_identity: Some("email-42".to_string()),
            validated: trust == "authoritative",
        }
    }

    #[test]
    fn untrusted_instruction_is_quarantined_and_cannot_activate_later() {
        let mut input = request("untrusted");
        input.instruction_bearing = true;
        let evidence = evaluate(&input).unwrap();
        assert_eq!(evidence.outcome, "quarantined");
        assert!(!evidence.authoritative);
        assert!(!evidence.can_activate("workspace-a", 9000));
    }

    #[test]
    fn trusted_authoritative_source_is_admitted_only_in_scope() {
        let evidence = evaluate(&request("authoritative")).unwrap();
        assert!(evidence.authoritative);
        assert!(evidence.durable);
        assert!(evidence.can_activate("workspace-a", 9000));
        assert!(!evidence.can_activate("workspace-b", 9000));
        assert!(!evidence.can_activate("workspace-a", 4999));
    }

    #[test]
    fn contradiction_escalates_without_deleting_history() {
        let mut input = request("trusted");
        input.contradicts_authoritative = true;
        let evidence = evaluate(&input).unwrap();
        assert_eq!(evidence.outcome, "escalated");
        assert!(evidence.durable);
        assert!(!evidence.authoritative);
    }

    #[test]
    fn missing_scope_abstains_fail_closed() {
        let mut input = request("authoritative");
        input.authorization_scope = "workspace-b".to_string();
        let evidence = evaluate(&input).unwrap();
        assert_eq!(evidence.outcome, "abstained");
        assert!(!evidence.durable);
        assert!(!evidence.authoritative);
    }

    #[test]
    fn revocation_is_hash_only_and_preserves_record_identity() {
        let mut evidence = evaluate(&request("authoritative")).unwrap();
        let original = evidence.record_digest.clone();
        evidence
            .revoke(&digest("operator"), "poisoning_confirmed")
            .unwrap();
        assert_eq!(evidence.outcome, "revoked");
        assert_eq!(evidence.record_digest, original);
        assert!(evidence.revocation_digest.is_some());
        assert!(!evidence.can_activate("workspace-a", 9000));
        assert!(evidence.validate().is_ok());
    }

    #[test]
    fn decision_digest_rejects_post_issue_evidence_mutation() {
        let mut evidence = evaluate(&request("trusted")).unwrap();
        evidence.reason_codes.push("forged_reason".to_string());
        assert!(evidence.validate().is_err());
    }

    #[test]
    fn four_outcome_classes_are_stable_and_hash_covered() {
        let mut drop_request = request("trusted");
        drop_request.task_relevance_bps = 100;
        let mut block_request = request("trusted");
        block_request.authorization_scope = "workspace-b".to_string();
        let cases = [
            (
                evaluate(&request("authoritative")).unwrap(),
                AdmissionOutcomeClass::Save,
            ),
            (
                evaluate(&drop_request).unwrap(),
                AdmissionOutcomeClass::Drop,
            ),
            (
                evaluate(&block_request).unwrap(),
                AdmissionOutcomeClass::Block,
            ),
            (
                evaluate(&request("trusted")).unwrap(),
                AdmissionOutcomeClass::PendingApproval,
            ),
        ];

        for (evidence, expected) in cases {
            assert_eq!(evidence.outcome_class().unwrap(), expected);
            let encoded = serde_json::to_string(&evidence).unwrap();
            assert!(encoded.contains(&format!("\"outcome_class\":\"{expected}\"")));

            let mut forged = evidence;
            forged.outcome_class = Some(if expected == AdmissionOutcomeClass::Save {
                AdmissionOutcomeClass::Drop
            } else {
                AdmissionOutcomeClass::Save
            });
            assert!(forged.validate().is_err());
        }
    }

    #[test]
    fn raw_payload_fields_are_not_part_of_evidence_shape() {
        let evidence = evaluate(&request("trusted")).unwrap();
        let encoded = serde_json::to_string(&evidence).unwrap();
        assert!(!encoded.contains("body"));
        assert!(!encoded.contains("prompt"));
        assert!(!encoded.contains("secret"));
    }
}
