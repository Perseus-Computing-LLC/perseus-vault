use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

pub(crate) const SCHEMA_VERSION: i64 = 1;
const DEFAULT_BUDGET_LIMIT: i64 = 100;
const DEFAULT_IMPACT_LIMIT: i64 = 100;
const MAX_POLICY_LIMIT: i64 = 1_000_000;
const MAX_HISTORY: usize = 64;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ActionLineageRequest {
    pub schema_version: i64,
    pub transition: String,
    pub action_class: String,
    pub budget_cost: i64,
    pub impact_units: i64,
    #[serde(default)]
    pub continuation: Option<ContinuationReference>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ContinuationReference {
    pub schema_version: i64,
    pub lineage_id: String,
    pub parent_head_digest: String,
    pub continuation_state_digest: String,
    pub workspace_hash: String,
    pub agent_id: String,
    pub authority_manifest_version: i64,
    pub policy_version: String,
}

pub(crate) fn parse_request(value: &Value) -> Result<ActionLineageRequest, String> {
    let request: ActionLineageRequest = serde_json::from_value(value.clone())
        .map_err(|_| "lineage request malformed".to_string())?;
    if request.schema_version != SCHEMA_VERSION {
        return Err("lineage request malformed".to_string());
    }
    if request.transition != "continue" && request.transition != "new_authorization" {
        return Err("lineage request malformed".to_string());
    }
    if request.action_class.is_empty() || request.action_class.len() > 32 {
        return Err("lineage request malformed".to_string());
    }
    if !matches!(
        request.action_class.as_str(),
        "read" | "external_send" | "write" | "delete" | "other"
    ) {
        return Err("lineage request malformed".to_string());
    }
    if !(0..=1_000_000).contains(&request.budget_cost)
        || !(0..=1_000_000).contains(&request.impact_units)
    {
        return Err("lineage request malformed".to_string());
    }
    match (&request.transition[..], request.continuation.as_ref()) {
        ("continue", Some(reference)) => validate_reference(reference)?,
        ("continue", None) => return Err("lineage request malformed".to_string()),
        ("new_authorization", Some(reference)) => validate_reference(reference)?,
        ("new_authorization", None) => {}
        _ => return Err("lineage request malformed".to_string()),
    }
    Ok(request)
}

fn validate_reference(reference: &ContinuationReference) -> Result<(), String> {
    if reference.schema_version != SCHEMA_VERSION
        || reference.authority_manifest_version < 1
        || reference.lineage_id.is_empty()
        || reference.lineage_id.len() > 128
        || reference.workspace_hash.is_empty()
        || reference.workspace_hash.len() > 256
        || reference.agent_id.is_empty()
        || reference.agent_id.len() > 256
        || !is_sha256(&reference.parent_head_digest)
        || !is_sha256(&reference.continuation_state_digest)
        || !is_sha256(&reference.policy_version)
    {
        return Err("lineage request malformed".to_string());
    }
    Ok(())
}

pub(crate) fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct LineageState {
    pub schema_version: i64,
    pub action_classes: Vec<String>,
    pub automaton_state: String,
    pub budget_spent: i64,
    pub impact_units: i64,
    pub last_action_digest: String,
}

impl LineageState {
    pub(crate) fn initial() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            action_classes: Vec::new(),
            automaton_state: "empty".to_string(),
            budget_spent: 0,
            impact_units: 0,
            last_action_digest: String::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct LineagePolicy {
    pub(crate) budget_limit: i64,
    pub(crate) impact_limit: i64,
    pub(crate) deny_read_then_external_send: bool,
}

pub(crate) fn policy_for_constraints(raw: &str) -> Result<(String, LineagePolicy), String> {
    let value: Value =
        serde_json::from_str(raw).map_err(|_| "lineage policy malformed".to_string())?;
    let object = value
        .as_object()
        .ok_or_else(|| "lineage policy malformed".to_string())?;
    if contains_forbidden_key(&value) {
        return Err("lineage policy contains forbidden fields".to_string());
    }

    let mut policy = LineagePolicy {
        budget_limit: DEFAULT_BUDGET_LIMIT,
        impact_limit: DEFAULT_IMPACT_LIMIT,
        deny_read_then_external_send: true,
    };
    if let Some(config_value) = object.get("__aar_lineage__") {
        let config = config_value
            .as_object()
            .ok_or_else(|| "lineage policy malformed".to_string())?;
        for key in config.keys() {
            if !matches!(
                key.as_str(),
                "budget_limit" | "impact_limit" | "deny_read_then_external_send"
            ) {
                return Err("lineage policy malformed".to_string());
            }
        }
        if let Some(value) = config.get("budget_limit") {
            policy.budget_limit = policy_limit(value)?;
        }
        if let Some(value) = config.get("impact_limit") {
            policy.impact_limit = policy_limit(value)?;
        }
        if let Some(value) = config.get("deny_read_then_external_send") {
            policy.deny_read_then_external_send = value
                .as_bool()
                .ok_or_else(|| "lineage policy malformed".to_string())?;
        }
    }
    let canonical = canonical_json(&value)?;
    let version = digest_text(&format!(
        "perseus-vault/action-lineage-policy/v{SCHEMA_VERSION}|{canonical}"
    ));
    Ok((version, policy))
}

fn policy_limit(value: &Value) -> Result<i64, String> {
    let limit = value
        .as_i64()
        .ok_or_else(|| "lineage policy malformed".to_string())?;
    if !(0..=MAX_POLICY_LIMIT).contains(&limit) {
        return Err("lineage policy malformed".to_string());
    }
    Ok(limit)
}

pub(crate) fn validate_request(request: &ActionLineageRequest) -> Result<(), String> {
    let value =
        serde_json::to_value(request).map_err(|_| "lineage request malformed".to_string())?;
    parse_request(&value).map(|_| ())
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct LineageReceipt {
    pub schema_version: i64,
    pub transition_id: String,
    pub action_id: String,
    pub lineage_id: String,
    pub parent_lineage_id: String,
    pub parent_head_digest: String,
    pub head_digest: String,
    pub continuation_state_digest: String,
    pub workspace_hash: String,
    pub agent_id: String,
    pub authority_manifest_id: String,
    pub authority_manifest_version: i64,
    pub policy_version: String,
    pub outcome: String,
    pub reason_code: String,
    pub budget_spent: i64,
    pub impact_units: i64,
    pub budget_limit: i64,
    pub impact_limit: i64,
    pub expires_at_unix_ms: Option<i64>,
    pub revoked_at_unix_ms: Option<i64>,
}

pub(crate) fn compute_head_digest(
    lineage_id: &str,
    parent_lineage_id: &str,
    parent_head_digest: &str,
    workspace_hash: &str,
    agent_id: &str,
    authority_manifest_id: &str,
    authority_manifest_version: i64,
    policy_version: &str,
    state_digest: &str,
) -> String {
    let value = serde_json::json!({
        "lineage_id": lineage_id,
        "parent_lineage_id": parent_lineage_id,
        "parent_head_digest": parent_head_digest,
        "workspace_hash": workspace_hash,
        "agent_id": agent_id,
        "authority_manifest_id": authority_manifest_id,
        "authority_manifest_version": authority_manifest_version,
        "policy_version": policy_version,
        "state_digest": state_digest,
    });
    let canonical = canonical_json(&value).unwrap_or_else(|_| "{}".to_string());
    digest_text(&format!(
        "perseus-vault/action-lineage-head/v{SCHEMA_VERSION}|{canonical}"
    ))
}

pub(crate) fn validate_receipt(receipt: &LineageReceipt) -> Result<(), String> {
    if receipt.schema_version != SCHEMA_VERSION
        || receipt.action_id.is_empty()
        || receipt.action_id.len() > 128
        || receipt.transition_id.is_empty()
        || receipt.transition_id.len() > 128
        || receipt.lineage_id.is_empty()
        || receipt.lineage_id.len() > 128
        || (!receipt.parent_lineage_id.is_empty() && receipt.parent_lineage_id.len() > 128)
        || (receipt.parent_lineage_id.is_empty() != receipt.parent_head_digest.is_empty())
        || (!receipt.parent_head_digest.is_empty() && !is_sha256(&receipt.parent_head_digest))
        || !is_sha256(&receipt.head_digest)
        || !is_sha256(&receipt.continuation_state_digest)
        || !is_sha256(&receipt.policy_version)
        || receipt.workspace_hash.is_empty()
        || receipt.workspace_hash.len() > 256
        || receipt.agent_id.is_empty()
        || receipt.agent_id.len() > 256
        || receipt.authority_manifest_id.is_empty()
        || receipt.authority_manifest_id.len() > 128
        || receipt.authority_manifest_version < 1
        || !(0..=MAX_POLICY_LIMIT).contains(&receipt.budget_limit)
        || !(0..=MAX_POLICY_LIMIT).contains(&receipt.impact_limit)
        || !(0..=receipt.budget_limit).contains(&receipt.budget_spent)
        || !(0..=receipt.impact_limit).contains(&receipt.impact_units)
        || !matches!(
            receipt.outcome.as_str(),
            "continued" | "new_authorization" | "denied" | "stale" | "revoked" | "abstain"
        )
        || !matches!(
            receipt.reason_code.as_str(),
            "admitted"
                | "new_authorization"
                | "resource_denied"
                | "composition_denied"
                | "budget_denied"
                | "impact_denied"
                | "stale"
                | "revoked"
                | "abstain"
                | "policy_mismatch"
                | "authority_mismatch"
                | "expired"
        )
    {
        return Err("lineage receipt malformed".to_string());
    }
    let reason_matches = match receipt.outcome.as_str() {
        "continued" => receipt.reason_code == "admitted",
        "new_authorization" => receipt.reason_code == "new_authorization",
        "denied" => matches!(
            receipt.reason_code.as_str(),
            "resource_denied" | "composition_denied" | "budget_denied" | "impact_denied"
        ),
        "stale" => matches!(
            receipt.reason_code.as_str(),
            "stale" | "policy_mismatch" | "authority_mismatch" | "expired"
        ),
        "revoked" => receipt.reason_code == "revoked",
        "abstain" => receipt.reason_code == "abstain",
        _ => false,
    };
    if !reason_matches {
        return Err("lineage receipt outcome/reason mismatch".to_string());
    }
    let expected_head = compute_head_digest(
        &receipt.lineage_id,
        &receipt.parent_lineage_id,
        &receipt.parent_head_digest,
        &receipt.workspace_hash,
        &receipt.agent_id,
        &receipt.authority_manifest_id,
        receipt.authority_manifest_version,
        &receipt.policy_version,
        &receipt.continuation_state_digest,
    );
    if expected_head != receipt.head_digest {
        return Err("lineage receipt head digest mismatch".to_string());
    }
    Ok(())
}

pub(crate) fn request_digest(
    action_key: &str,
    capability: &str,
    intent_hash: &str,
    admission_binding: &Value,
    request: &ActionLineageRequest,
) -> Result<String, String> {
    validate_request(request)?;
    let value = serde_json::json!({
        "action_key_digest": digest_text(action_key),
        "capability": capability,
        "intent_hash": intent_hash,
        "admission_binding": admission_binding,
        "lineage": request,
    });
    Ok(digest_text(&format!(
        "perseus-vault/action-lineage-request/v{SCHEMA_VERSION}|{}",
        canonical_json(&value)?
    )))
}

pub(crate) fn state_digest(state: &LineageState) -> Result<String, String> {
    validate_state(state)?;
    let value = serde_json::to_value(state).map_err(|_| "lineage state malformed".to_string())?;
    let canonical = canonical_json(&value)?;
    Ok(digest_text(&format!(
        "perseus-vault/action-lineage-state/v{SCHEMA_VERSION}|{canonical}"
    )))
}

fn validate_state(state: &LineageState) -> Result<(), String> {
    if state.schema_version != SCHEMA_VERSION
        || state.action_classes.len() > MAX_HISTORY
        || state.budget_spent < 0
        || state.impact_units < 0
        || state.budget_spent > MAX_POLICY_LIMIT
        || state.impact_units > MAX_POLICY_LIMIT
        || (!state.last_action_digest.is_empty() && !is_sha256(&state.last_action_digest))
        || !matches!(
            state.automaton_state.as_str(),
            "empty" | "read_seen" | "other_seen" | "external_send_seen"
        )
    {
        return Err("lineage state malformed".to_string());
    }
    if state.action_classes.iter().any(|class| {
        !matches!(
            class.as_str(),
            "read" | "external_send" | "write" | "delete" | "other"
        )
    }) {
        return Err("lineage state malformed".to_string());
    }
    if state.action_classes.is_empty() && state.automaton_state != "empty" {
        return Err("lineage state malformed".to_string());
    }
    if state.action_classes.is_empty() && !state.last_action_digest.is_empty() {
        return Err("lineage state malformed".to_string());
    }
    if !state.action_classes.is_empty() && state.last_action_digest.is_empty() {
        return Err("lineage state malformed".to_string());
    }
    let expected_automaton = if state.action_classes.is_empty() {
        "empty"
    } else if state.action_classes.iter().any(|class| class == "read") {
        "read_seen"
    } else if state
        .action_classes
        .iter()
        .any(|class| class == "external_send")
    {
        "external_send_seen"
    } else {
        "other_seen"
    };
    if state.automaton_state != expected_automaton {
        return Err("lineage state malformed".to_string());
    }
    Ok(())
}

fn contains_forbidden_key(value: &Value) -> bool {
    const FORBIDDEN: [&str; 20] = [
        "raw_arguments",
        "tool_arguments",
        "credentials",
        "prompt",
        "prompts",
        "token",
        "tokens",
        "secret",
        "secrets",
        "provider_response",
        "api_key",
        "access_token",
        "authorization",
        "password",
        "private_key",
        "body",
        "body_json",
        "memory_body",
        "tool_payload",
        "sensitive_output",
    ];
    match value {
        Value::Object(object) => object.iter().any(|(key, child)| {
            FORBIDDEN
                .iter()
                .any(|forbidden| key.eq_ignore_ascii_case(forbidden))
                || contains_forbidden_key(child)
        }),
        Value::Array(values) => values.iter().any(contains_forbidden_key),
        _ => false,
    }
}

fn canonical_json(value: &Value) -> Result<String, String> {
    fn canonical_value(value: &Value) -> Value {
        match value {
            Value::Object(object) => {
                let mut keys = object.keys().cloned().collect::<Vec<_>>();
                keys.sort();
                let mut sorted = Map::new();
                for key in keys {
                    sorted.insert(key.clone(), canonical_value(&object[&key]));
                }
                Value::Object(sorted)
            }
            Value::Array(values) => Value::Array(values.iter().map(canonical_value).collect()),
            other => other.clone(),
        }
    }
    serde_json::to_string(&canonical_value(value))
        .map_err(|_| "lineage state malformed".to_string())
}

fn digest_text(value: &str) -> String {
    format!("{:x}", Sha256::digest(value.as_bytes()))
}

pub(crate) fn idempotency_key_digest(action_key: &str) -> String {
    digest_text(&format!(
        "perseus-vault/action-lineage-idempotency/v{SCHEMA_VERSION}|{action_key}"
    ))
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct TransitionDecision {
    pub(crate) state: LineageState,
    pub(crate) outcome: String,
    pub(crate) reason: String,
}

pub(crate) fn validate_capability_action_class(
    capability: &str,
    action_class: &str,
) -> Result<(), String> {
    if capability.is_empty() {
        return Err("lineage action class does not match capability".to_string());
    }
    let expected = match capability {
        "read" | "memory.read" | "memory_read" => "read",
        "external_send"
        | "external.send"
        | "send"
        | "git_push"
        | "git push"
        | "github_pull_request_creation"
        | "github pull request creation"
        | "github_pull_request_merge"
        | "github pull request merge"
        | "github_issue_closure"
        | "github issue closure"
        | "issue_closure"
        | "pr_merge"
        | "deploy" => "external_send",
        "write" | "memory.write" | "memory_write" => "write",
        "delete" | "memory.delete" | "memory_delete" => "delete",
        _ => {
            return Err("lineage capability is not classified by the trusted taxonomy".to_string())
        }
    };
    if expected == action_class {
        Ok(())
    } else {
        Err("lineage action class does not match capability".to_string())
    }
}

pub(crate) fn apply_action(
    current: &LineageState,
    request: &ActionLineageRequest,
    policy: &LineagePolicy,
    intent_hash: &str,
) -> Result<TransitionDecision, String> {
    validate_state(current)?;
    if request.action_class.is_empty()
        || !matches!(
            request.action_class.as_str(),
            "read" | "external_send" | "write" | "delete" | "other"
        )
        || !(0..=1_000_000).contains(&request.budget_cost)
        || !(0..=1_000_000).contains(&request.impact_units)
        || !is_sha256(intent_hash)
    {
        return Err("lineage transition malformed".to_string());
    }
    if policy.deny_read_then_external_send
        && request.action_class == "external_send"
        && current.action_classes.iter().any(|class| class == "read")
    {
        return Ok(TransitionDecision {
            state: current.clone(),
            outcome: "denied".to_string(),
            reason: "composition_denied".to_string(),
        });
    }
    let Some(budget_spent) = current.budget_spent.checked_add(request.budget_cost) else {
        return Ok(TransitionDecision {
            state: current.clone(),
            outcome: "denied".to_string(),
            reason: "budget_denied".to_string(),
        });
    };
    let Some(impact_units) = current.impact_units.checked_add(request.impact_units) else {
        return Ok(TransitionDecision {
            state: current.clone(),
            outcome: "denied".to_string(),
            reason: "impact_denied".to_string(),
        });
    };
    if budget_spent > policy.budget_limit {
        return Ok(TransitionDecision {
            state: current.clone(),
            outcome: "denied".to_string(),
            reason: "budget_denied".to_string(),
        });
    }
    if impact_units > policy.impact_limit {
        return Ok(TransitionDecision {
            state: current.clone(),
            outcome: "denied".to_string(),
            reason: "impact_denied".to_string(),
        });
    }
    if current.action_classes.len() >= MAX_HISTORY {
        return Err("lineage transition exceeds history bound".to_string());
    }
    let mut next = current.clone();
    next.action_classes.push(request.action_class.clone());
    next.automaton_state = if next.action_classes.iter().any(|class| class == "read") {
        "read_seen".to_string()
    } else if next
        .action_classes
        .iter()
        .any(|class| class == "external_send")
    {
        "external_send_seen".to_string()
    } else {
        "other_seen".to_string()
    };
    next.budget_spent = budget_spent;
    next.impact_units = impact_units;
    next.last_action_digest = intent_hash.to_string();
    Ok(TransitionDecision {
        state: next,
        outcome: "continued".to_string(),
        reason: "admitted".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn rejects_unknown_lineage_payload_fields_before_policy_use() {
        let err = super::parse_request(&json!({
            "schema_version": 1,
            "transition": "continue",
            "action_class": "read",
            "budget_cost": 1,
            "impact_units": 0,
            "continuation": {
                "schema_version": 1,
                "lineage_id": "lin-test",
                "parent_head_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "continuation_state_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "workspace_hash": "ws",
                "agent_id": "agent",
                "authority_manifest_version": 1,
                "policy_version": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
            },
            "raw_prompt_sentinel": "LINEAGE-RAW-PROMPT-SENTINEL"
        }))
        .unwrap_err();
        assert_eq!(err, "lineage request malformed");
    }

    #[test]
    fn versioned_lineage_request_is_explicit_and_policy_is_hash_only() {
        let request = super::parse_request(&json!({
            "schema_version": 1,
            "transition": "new_authorization",
            "action_class": "read",
            "budget_cost": 1,
            "impact_units": 0
        }))
        .expect("versioned request should parse");
        assert_eq!(request.schema_version, 1);

        let sensitive_policy = json!({
            "__aar_lineage__": {
                "api_key": "LINEAGE-API-KEY-SENTINEL",
                "budget_limit": 7
            }
        })
        .to_string();
        let error = super::policy_for_constraints(&sensitive_policy).unwrap_err();
        assert_eq!(error, "lineage policy contains forbidden fields");
    }

    fn valid_receipt_for_validation() -> LineageReceipt {
        let continuation_state_digest = "b".repeat(64);
        let policy_version = "c".repeat(64);
        let head_digest = compute_head_digest(
            "lin-test",
            "",
            "",
            "workspace",
            "agent",
            "manifest",
            1,
            &policy_version,
            &continuation_state_digest,
        );
        LineageReceipt {
            schema_version: SCHEMA_VERSION,
            transition_id: "transition-1".to_string(),
            action_id: "action-1".to_string(),
            lineage_id: "lin-test".to_string(),
            parent_lineage_id: String::new(),
            parent_head_digest: String::new(),
            head_digest,
            continuation_state_digest,
            workspace_hash: "workspace".to_string(),
            agent_id: "agent".to_string(),
            authority_manifest_id: "manifest".to_string(),
            authority_manifest_version: 1,
            policy_version,
            outcome: "continued".to_string(),
            reason_code: "admitted".to_string(),
            budget_spent: 1,
            impact_units: 0,
            budget_limit: 100,
            impact_limit: 100,
            expires_at_unix_ms: None,
            revoked_at_unix_ms: None,
        }
    }

    #[test]
    fn receipts_require_identity_pairing_and_matching_outcome_reason() {
        let mut missing_transition = valid_receipt_for_validation();
        missing_transition.transition_id.clear();
        assert!(validate_receipt(&missing_transition).is_err());

        let mut mismatched_reason = valid_receipt_for_validation();
        mismatched_reason.reason_code = "revoked".to_string();
        assert!(validate_receipt(&mismatched_reason).is_err());

        let mut orphan_parent = valid_receipt_for_validation();
        orphan_parent.parent_lineage_id = "parent".to_string();
        assert!(validate_receipt(&orphan_parent).is_err());
    }

    #[test]
    fn state_validation_requires_consistent_automaton_and_last_action() {
        let mut missing_last_digest = LineageState {
            action_classes: vec!["read".to_string()],
            automaton_state: "read_seen".to_string(),
            ..LineageState::initial()
        };
        assert!(state_digest(&missing_last_digest).is_err());

        missing_last_digest.last_action_digest = "a".repeat(64);
        missing_last_digest.automaton_state = "empty".to_string();
        assert!(state_digest(&missing_last_digest).is_err());
    }

    #[test]
    fn policy_rejects_case_variant_sensitive_keys() {
        for key in ["API_KEY", "Prompt", "Authorization", "access_token"] {
            let mut inner = Map::new();
            inner.insert(key.to_string(), json!("LINEAGE-SENSITIVE-SENTINEL"));
            let value = Value::Object(inner).to_string();
            assert!(
                policy_for_constraints(&value).is_err(),
                "key must be rejected: {key}"
            );
        }
    }

    #[test]
    fn composition_policy_denies_read_then_external_send_without_state_change() {
        let (_, policy) = super::policy_for_constraints("{}").unwrap();
        let read = super::apply_action(
            &super::LineageState::initial(),
            &super::ActionLineageRequest {
                schema_version: super::SCHEMA_VERSION,
                transition: "continue".to_string(),
                action_class: "read".to_string(),
                budget_cost: 1,
                impact_units: 0,
                continuation: None,
            },
            &policy,
            &"a".repeat(64),
        )
        .unwrap();
        assert_eq!(read.outcome, "continued");
        let send = super::apply_action(
            &read.state,
            &super::ActionLineageRequest {
                schema_version: super::SCHEMA_VERSION,
                transition: "continue".to_string(),
                action_class: "external_send".to_string(),
                budget_cost: 1,
                impact_units: 1,
                continuation: None,
            },
            &policy,
            &"b".repeat(64),
        )
        .unwrap();
        assert_eq!(send.outcome, "denied");
        assert_eq!(send.reason, "composition_denied");
        assert_eq!(send.state, read.state);
    }

    #[test]
    fn capability_class_binding_rejects_external_send_downgrade() {
        assert!(super::validate_capability_action_class("read", "read").is_ok());
        assert!(super::validate_capability_action_class("external_send", "external_send").is_ok());
        assert!(super::validate_capability_action_class("git_push", "external_send").is_ok());
        assert!(super::validate_capability_action_class("external_send", "other").is_err());
        assert!(super::validate_capability_action_class("unprofiled.tool", "other").is_err());
        assert!(super::validate_capability_action_class("git_push", "read").is_err());
    }

    #[test]
    fn policy_and_state_digests_are_stable_and_hash_only() {
        let raw = r#"{"__aar_lineage__":{"budget_limit":7,"impact_limit":9,"deny_read_then_external_send":true}}"#;
        let (version_a, policy_a) = super::policy_for_constraints(raw).unwrap();
        let (version_b, policy_b) = super::policy_for_constraints(raw).unwrap();
        assert_eq!(version_a, version_b);
        assert_eq!(policy_a.budget_limit, 7);
        assert_eq!(policy_a.impact_limit, 9);
        assert!(policy_a.deny_read_then_external_send);
        assert_eq!(policy_a.budget_limit, policy_b.budget_limit);

        let state = super::LineageState::initial();
        let digest_a = super::state_digest(&state).unwrap();
        let digest_b = super::state_digest(&state).unwrap();
        assert_eq!(digest_a, digest_b);
        assert_eq!(digest_a.len(), 64);
        assert!(!serde_json::to_string(&state)
            .unwrap()
            .contains("LINEAGE-RAW-PROMPT-SENTINEL"));
    }

    #[test]
    fn reset_is_a_successor_and_retry_is_idempotent_with_cumulative_state() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-reset-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("reset-agent", "Reset Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "reset-agent".to_string(),
                workspace_hash: "reset-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string(), "external_send".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["reset-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["reset-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: json!({
                    "__aar_lineage__": {
                        "budget_limit": 3,
                        "impact_limit": 4,
                        "deny_read_then_external_send": false
                    }
                })
                .to_string(),
            },
            "reset-admin",
        )
        .unwrap();

        let first = db
            .action_intent_with_lineage(
                "reset-agent",
                "reset-workspace",
                "reset-scope",
                "reset-ref",
                "read",
                "reset-1",
                &"1".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 2,
                    impact_units: 3,
                    continuation: None,
                },
            )
            .unwrap();
        assert_eq!(first.lineage_outcome, "new_authorization");
        assert_eq!(first.lineage_receipt.as_ref().unwrap().budget_spent, 2);
        assert_eq!(first.lineage_receipt.as_ref().unwrap().impact_units, 3);
        let first_ref = first.lineage_continuation.clone().unwrap();

        let retry = db
            .action_intent_with_lineage(
                "reset-agent",
                "reset-workspace",
                "reset-scope",
                "reset-ref",
                "read",
                "reset-1",
                &"1".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 2,
                    impact_units: 3,
                    continuation: None,
                },
            )
            .unwrap();
        assert_eq!(retry.id, first.id);
        let transition_count: i64 = db
            .conn()
            .unwrap()
            .query_row("SELECT COUNT(*) FROM action_lineage_transitions", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(transition_count, 1);

        let successor = db
            .action_intent_with_lineage(
                "reset-agent",
                "reset-workspace",
                "reset-scope",
                "reset-ref",
                "read",
                "reset-2",
                &"2".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(first_ref.clone()),
                },
            )
            .unwrap();
        assert_eq!(successor.lineage_outcome, "new_authorization");
        assert_ne!(successor.lineage_id, first.lineage_id);
        assert_eq!(successor.lineage_receipt.as_ref().unwrap().budget_spent, 1);
        assert_eq!(successor.lineage_receipt.as_ref().unwrap().impact_units, 1);
        assert_eq!(
            successor
                .lineage_receipt
                .as_ref()
                .unwrap()
                .parent_lineage_id,
            first.lineage_id
        );

        drop(db);
        let reopened = crate::db::Database::open(&path_text).unwrap();
        let receipt = reopened.action_receipt_get(&first.id).unwrap().unwrap();
        assert_eq!(
            receipt.lineage_receipt.as_ref().unwrap().head_digest,
            first.lineage_receipt.as_ref().unwrap().head_digest
        );
        drop(reopened);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn v55_upgrade_creates_lineage_sidecars_and_new_cumulative_column() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-upgrade-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        {
            let conn = db.conn().unwrap();
            conn.execute_batch("PRAGMA user_version=55; DROP TABLE action_lineage_transitions; DROP TABLE action_lineages;").unwrap();
        }
        drop(db);
        let reopened = crate::db::Database::open(&path_text).unwrap();
        let conn = reopened.conn().unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        let lineages: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type=char(116,97,98,108,101) AND name=char(97,99,116,105,111,110,95,108,105,110,101,97,103,101,115)", [], |r| r.get(0)
        ).unwrap();
        let transitions: i64 = conn.query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type=char(116,97,98,108,101) AND name=char(97,99,116,105,111,110,95,108,105,110,101,97,103,101,95,116,114,97,110,115,105,116,105,111,110,115)", [], |r| r.get(0)
        ).unwrap();
        let impact_spent: i64 = conn.query_row(
            r#"SELECT COUNT(*) FROM pragma_table_info("action_lineage_transitions") WHERE name="impact_spent""#, [], |r| r.get(0)
        ).unwrap();
        assert_eq!(version, crate::schema::SCHEMA_VERSION);
        assert_eq!((lineages, transitions, impact_spent), (1, 1, 1));
        drop(conn);
        drop(reopened);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn concurrent_continuations_have_one_authoritative_winner() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-race-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("race-agent", "Race Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "race-agent".to_string(),
                workspace_hash: "race-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["race-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["race-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: json!({
                    "__aar_lineage__": {"budget_limit": 10, "impact_limit": 10}
                })
                .to_string(),
            },
            "race-admin",
        )
        .unwrap();
        let first = db
            .action_intent_with_lineage(
                "race-agent",
                "race-workspace",
                "race-scope",
                "race-ref",
                "read",
                "race-0",
                &"0".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: None,
                },
            )
            .unwrap();
        let reference = first.lineage_continuation.clone().unwrap();
        drop(db);

        let mut joins = Vec::new();
        for (suffix, byte) in [("a", "a"), ("b", "b")] {
            let path_for_thread = path_text.clone();
            let reference_for_thread = reference.clone();
            joins.push(std::thread::spawn(move || {
                let db = crate::db::Database::open(&path_for_thread).unwrap();
                db.action_intent_with_lineage(
                    "race-agent",
                    "race-workspace",
                    "race-scope",
                    "race-ref",
                    "read",
                    &format!("race-{suffix}"),
                    &byte.repeat(64),
                    Some("{}"),
                    &[],
                    None,
                    &super::ActionLineageRequest {
                        schema_version: super::SCHEMA_VERSION,
                        transition: "continue".to_string(),
                        action_class: "read".to_string(),
                        budget_cost: 4,
                        impact_units: 4,
                        continuation: Some(reference_for_thread),
                    },
                )
                .map_err(|error| error.to_string())
            }));
        }
        let outcomes: Vec<_> = joins.into_iter().map(|join| join.join().unwrap()).collect();
        assert_eq!(outcomes.len(), 2);
        let actions: Vec<_> = outcomes.into_iter().map(|result| result.unwrap()).collect();
        assert_eq!(
            actions
                .iter()
                .filter(|action| action.lineage_outcome == "continued")
                .count(),
            1
        );
        assert_eq!(
            actions
                .iter()
                .filter(|action| action.lineage_outcome == "stale")
                .count(),
            1
        );
        assert!(actions
            .iter()
            .all(|action| action.status == "intent" || action.status == "denied"));

        let db = crate::db::Database::open(&path_text).unwrap();
        let conn = db.conn().unwrap();
        let transitions: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM action_lineage_transitions WHERE lineage_id=?1",
                [&reference.lineage_id],
                |r| r.get(0),
            )
            .unwrap();
        let (budget_spent, impact_spent): (i64, i64) = conn
            .query_row(
                "SELECT budget_spent,impact_units FROM action_lineages WHERE lineage_id=?1",
                [&reference.lineage_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert_eq!(transitions, 3);
        assert_eq!((budget_spent, impact_spent), (5, 5));
        drop(conn);
        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn aar_lineage_requires_explicit_continuation_and_keeps_legacy_scope_independent() {
        let path =
            std::env::temp_dir().join(format!("perseus-vault-lineage-{}.db", uuid::Uuid::new_v4()));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("lineage-agent", "Lineage Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "lineage-agent".to_string(),
                workspace_hash: "lineage-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string(), "external_send".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["lineage-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["lineage-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: "{}".to_string(),
            },
            "lineage-admin",
        )
        .unwrap();

        let first = db
            .action_intent_with_lineage(
                "lineage-agent",
                "lineage-workspace",
                "lineage-scope",
                "lineage-ref",
                "read",
                "lineage-read",
                &"a".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 0,
                    continuation: None,
                },
            )
            .unwrap();
        assert_eq!(first.lineage_outcome, "new_authorization");
        let reference = first
            .lineage_continuation
            .clone()
            .expect("continuation reference");

        let second = db
            .action_intent_with_lineage(
                "lineage-agent",
                "lineage-workspace",
                "lineage-scope",
                "lineage-ref",
                "read",
                "lineage-read-2",
                &"b".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 0,
                    continuation: Some(reference),
                },
            )
            .unwrap();
        assert_eq!(second.lineage_outcome, "continued");
        assert_eq!(second.lineage_id, first.lineage_id);
        assert_ne!(second.lineage_transition_id, first.lineage_transition_id);

        let independent = db
            .action_intent(
                "lineage-agent",
                "lineage-workspace",
                "lineage-scope",
                "lineage-ref",
                "read",
                "independent-read",
                &"c".repeat(64),
                Some("{}"),
                &[],
                None,
            )
            .unwrap();
        assert!(independent.lineage_id.is_empty());
        assert!(independent.lineage_continuation.is_none());

        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn lineage_failures_are_closed_and_historical_receipts_stay_verifiable() {
        let make_fixture = |suffix: &str| {
            let path = std::env::temp_dir().join(format!(
                "perseus-vault-lineage-failure-{suffix}-{}.db",
                uuid::Uuid::new_v4()
            ));
            let path_text = path.to_string_lossy().into_owned();
            let db = crate::db::Database::open(&path_text).unwrap();
            db.agent_upsert("failure-agent", "Failure Agent", 3, "lineage")
                .unwrap();
            db.authority_set(
                &crate::models::AuthorityManifestInput {
                    agent_id: "failure-agent".to_string(),
                    workspace_hash: "failure-workspace".to_string(),
                    allowed_capabilities: vec!["read".to_string()],
                    approval_required_capabilities: Vec::new(),
                    scope_anchors: vec!["failure-scope".to_string()],
                    approver_principals: Vec::new(),
                    allowed_inbound_principals: Vec::new(),
                    permitted_external_ref_prefixes: vec!["failure-ref".to_string()],
                    max_parallel_actions: 1,
                    mode: "enforce".to_string(),
                    expires_at_unix_ms: None,
                    capability_constraints_json: json!({
                        "__aar_lineage__": {"budget_limit": 10, "impact_limit": 10}
                    })
                    .to_string(),
                },
                "failure-admin",
            )
            .unwrap();
            let action = db
                .action_intent_with_lineage(
                    "failure-agent",
                    "failure-workspace",
                    "failure-scope",
                    "failure-ref",
                    "read",
                    &format!("failure-{suffix}-0"),
                    &"d".repeat(64),
                    Some("{}"),
                    &[],
                    None,
                    &super::ActionLineageRequest {
                        schema_version: super::SCHEMA_VERSION,
                        transition: "new_authorization".to_string(),
                        action_class: "read".to_string(),
                        budget_cost: 1,
                        impact_units: 1,
                        continuation: None,
                    },
                )
                .unwrap();
            let reference = action.lineage_continuation.clone().unwrap();
            (db, path_text, action, reference)
        };
        let cleanup = |path: &str| {
            let _ = std::fs::remove_file(path);
            let _ = std::fs::remove_file(format!("{path}-wal"));
            let _ = std::fs::remove_file(format!("{path}-shm"));
        };

        let (db, path, first, reference) = make_fixture("tamper");
        let raw_action = serde_json::to_string(&first).unwrap();
        assert!(!raw_action.contains("LINEAGE-RAW-PROMPT-SENTINEL"));
        {
            let conn = db.conn().unwrap();
            let state_json: String = conn
                .query_row(
                    "SELECT continuation_state_json FROM action_lineages WHERE lineage_id=?1",
                    [&reference.lineage_id],
                    |r| r.get(0),
                )
                .unwrap();
            let policy_version: String = conn
                .query_row(
                    "SELECT policy_version FROM action_lineages WHERE lineage_id=?1",
                    [&reference.lineage_id],
                    |r| r.get(0),
                )
                .unwrap();
            assert!(!state_json.contains("LINEAGE-RAW-PROMPT-SENTINEL"));
            assert!(super::is_sha256(&policy_version));
            assert!(conn
                .execute(
                    "UPDATE action_lineage_transitions SET reason_code=?1 WHERE transition_id=?2",
                    rusqlite::params!["tampered", first.lineage_transition_id]
                )
                .is_err());
            assert!(conn
                .execute(
                    "DELETE FROM action_lineage_transitions WHERE transition_id=?1",
                    rusqlite::params![first.lineage_transition_id]
                )
                .is_err());
        }
        let receipt_before = db.action_receipt_get(&first.id).unwrap().unwrap();
        assert_eq!(
            receipt_before.lineage_receipt.as_ref().unwrap().outcome,
            "new_authorization"
        );
        {
            let conn = db.conn().unwrap();
            conn.execute(
                "UPDATE action_lineages SET continuation_state_json=?1 WHERE lineage_id=?2",
                rusqlite::params!["{}", &reference.lineage_id],
            )
            .unwrap();
        }
        let err = db
            .action_intent_with_lineage(
                "failure-agent",
                "failure-workspace",
                "failure-scope",
                "failure-ref",
                "read",
                "failure-tamper-1",
                &"e".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(reference.clone()),
                },
            )
            .unwrap_err()
            .to_string();
        assert!(err.contains("lineage state"));
        let receipt_after = db.action_receipt_get(&first.id).unwrap().unwrap();
        assert_eq!(
            receipt_after.lineage_receipt.unwrap().head_digest,
            receipt_before.lineage_receipt.unwrap().head_digest
        );
        drop(db);
        let bytes = std::fs::read(&path).unwrap();
        assert!(!String::from_utf8_lossy(&bytes).contains("LINEAGE-RAW-PROMPT-SENTINEL"));
        cleanup(&path);

        let (db, path, _first, reference) = make_fixture("expired");
        db.conn()
            .unwrap()
            .execute(
                "UPDATE action_lineages SET expires_at_unix_ms=?1 WHERE lineage_id=?2",
                rusqlite::params![1_i64, &reference.lineage_id],
            )
            .unwrap();
        let expired = db
            .action_intent_with_lineage(
                "failure-agent",
                "failure-workspace",
                "failure-scope",
                "failure-ref",
                "read",
                "failure-expired-1",
                &"f".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(reference.clone()),
                },
            )
            .unwrap();
        let expired_receipt = expired.lineage_receipt.unwrap();
        assert_eq!(expired_receipt.outcome, "stale");
        assert_eq!(expired_receipt.reason_code, "expired");
        assert_eq!(expired_receipt.head_digest, reference.parent_head_digest);
        assert!(expired.lineage_continuation.is_none());
        drop(db);
        cleanup(&path);

        let (db, path, _first, reference) = make_fixture("revoked");
        db.conn()
            .unwrap()
            .execute(
                "UPDATE action_lineages SET revoked_at_unix_ms=?1 WHERE lineage_id=?2",
                rusqlite::params![2_i64, &reference.lineage_id],
            )
            .unwrap();
        let revoked = db
            .action_intent_with_lineage(
                "failure-agent",
                "failure-workspace",
                "failure-scope",
                "failure-ref",
                "read",
                "failure-revoked-1",
                &"b".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(reference.clone()),
                },
            )
            .unwrap();
        let revoked_receipt = revoked.lineage_receipt.unwrap();
        assert_eq!(revoked_receipt.outcome, "revoked");
        assert_eq!(revoked_receipt.reason_code, "revoked");
        assert_eq!(revoked_receipt.head_digest, reference.parent_head_digest);
        assert!(revoked.lineage_continuation.is_none());
        drop(db);
        cleanup(&path);

        let (db, path, _first, reference) = make_fixture("scope");
        let err = db
            .action_intent_with_lineage(
                "failure-agent",
                "wrong-workspace",
                "failure-scope",
                "failure-ref",
                "read",
                "failure-scope-1",
                &"c".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(reference),
                },
            )
            .unwrap_err()
            .to_string();
        assert!(err.contains("authority"));
        drop(db);
        cleanup(&path);
    }

    #[test]
    fn authority_rotation_emits_a_verifiable_stale_receipt_without_advancing_head() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-authority-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("rotation-agent", "Rotation Agent", 3, "lineage")
            .unwrap();
        let manifest_input = |constraints: &str| crate::models::AuthorityManifestInput {
            agent_id: "rotation-agent".to_string(),
            workspace_hash: "rotation-workspace".to_string(),
            allowed_capabilities: vec!["read".to_string()],
            approval_required_capabilities: Vec::new(),
            scope_anchors: vec!["rotation-scope".to_string()],
            approver_principals: Vec::new(),
            allowed_inbound_principals: Vec::new(),
            permitted_external_ref_prefixes: vec!["rotation-ref".to_string()],
            max_parallel_actions: 1,
            mode: "enforce".to_string(),
            expires_at_unix_ms: None,
            capability_constraints_json: constraints.to_string(),
        };
        let first_manifest = db
            .authority_set(&manifest_input("{}"), "rotation-admin")
            .unwrap();
        let first = db
            .action_intent_with_lineage(
                "rotation-agent",
                "rotation-workspace",
                "rotation-scope",
                "rotation-ref",
                "read",
                "rotation-0",
                &"a".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: None,
                },
            )
            .unwrap();
        let reference = first.lineage_continuation.clone().unwrap();
        let first_head = first.lineage_receipt.as_ref().unwrap().head_digest.clone();
        let _second_manifest = db
            .authority_set(&manifest_input("{}"), "rotation-admin")
            .unwrap();

        let stale = db
            .action_intent_with_lineage(
                "rotation-agent",
                "rotation-workspace",
                "rotation-scope",
                "rotation-ref",
                "read",
                "rotation-1",
                &"b".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(reference),
                },
            )
            .expect("authority rotation should produce a bounded stale receipt");
        assert_eq!(stale.lineage_outcome, "stale");
        assert_eq!(stale.status, "denied");
        assert!(stale.lineage_continuation.is_none());
        let receipt = stale.lineage_receipt.as_ref().expect("stale receipt");
        assert_eq!(receipt.reason_code, "authority_mismatch");
        assert_eq!(receipt.authority_manifest_id, first_manifest.id);
        assert_eq!(receipt.head_digest, first_head);
        let current_head: String = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT head_digest FROM action_lineages WHERE lineage_id=?1",
                [&stale.lineage_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(current_head, first_head);
        let reread = db.action_receipt_get(&stale.id).unwrap().unwrap();
        assert_eq!(
            reread.lineage_receipt.unwrap().reason_code,
            "authority_mismatch"
        );

        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn stale_and_revoked_attempts_emit_bounded_outcomes_without_advancing_head() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-outcomes-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("outcome-agent", "Outcome Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "outcome-agent".to_string(),
                workspace_hash: "outcome-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["outcome-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["outcome-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: json!({
                    "__aar_lineage__": {"budget_limit": 10, "impact_limit": 10}
                })
                .to_string(),
            },
            "outcome-admin",
        )
        .unwrap();
        let first = db
            .action_intent_with_lineage(
                "outcome-agent",
                "outcome-workspace",
                "outcome-scope",
                "outcome-ref",
                "read",
                "outcome-0",
                &"1".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "new_authorization".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: None,
                },
            )
            .unwrap();
        let first_ref = first.lineage_continuation.clone().unwrap();
        let second = db
            .action_intent_with_lineage(
                "outcome-agent",
                "outcome-workspace",
                "outcome-scope",
                "outcome-ref",
                "read",
                "outcome-1",
                &"2".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(first_ref.clone()),
                },
            )
            .unwrap();
        let second_ref = second.lineage_continuation.clone().unwrap();
        let authoritative_head = second_ref.parent_head_digest.clone();

        let stale = db
            .action_intent_with_lineage(
                "outcome-agent",
                "outcome-workspace",
                "outcome-scope",
                "outcome-ref",
                "read",
                "outcome-stale",
                &"3".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 5,
                    impact_units: 5,
                    continuation: Some(first_ref),
                },
            )
            .unwrap();
        assert_eq!(stale.lineage_outcome, "stale");
        assert_eq!(stale.status, "denied");
        assert!(stale.lineage_continuation.is_none());
        let current_head: String = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT head_digest FROM action_lineages WHERE lineage_id=?1",
                [&second_ref.lineage_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(current_head, authoritative_head);
        let stale_id = stale.id.clone();

        db.conn()
            .unwrap()
            .execute(
                "UPDATE action_lineages SET revoked_at_unix_ms=?1 WHERE lineage_id=?2",
                rusqlite::params![2_i64, &second_ref.lineage_id],
            )
            .unwrap();
        let revoked = db
            .action_intent_with_lineage(
                "outcome-agent",
                "outcome-workspace",
                "outcome-scope",
                "outcome-ref",
                "read",
                "outcome-revoked",
                &"4".repeat(64),
                Some("{}"),
                &[],
                None,
                &super::ActionLineageRequest {
                    schema_version: super::SCHEMA_VERSION,
                    transition: "continue".to_string(),
                    action_class: "read".to_string(),
                    budget_cost: 1,
                    impact_units: 1,
                    continuation: Some(second_ref.clone()),
                },
            )
            .unwrap();
        assert_eq!(revoked.lineage_outcome, "revoked");
        assert_eq!(revoked.status, "denied");
        assert!(revoked.lineage_continuation.is_none());
        let stale_receipt = db.action_receipt_get(&stale_id).unwrap().unwrap();
        assert_eq!(stale_receipt.lineage_receipt.unwrap().outcome, "stale");
        let revoked_receipt = db.action_receipt_get(&revoked.id).unwrap().unwrap();
        assert_eq!(revoked_receipt.lineage_receipt.unwrap().outcome, "revoked");
        let final_head: String = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT head_digest FROM action_lineages WHERE lineage_id=?1",
                [&second_ref.lineage_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(final_head, authoritative_head);
        let transition_count: i64 = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM action_lineage_transitions WHERE lineage_id=?1",
                [&second_ref.lineage_id],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(transition_count, 4);
        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn idempotency_binds_external_reference_and_rejects_conflicting_retry() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-idempotency-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("idempotency-agent", "Idempotency Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "idempotency-agent".to_string(),
                workspace_hash: "idempotency-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["idempotency-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["idempotency-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: "{}".to_string(),
            },
            "idempotency-admin",
        )
        .unwrap();
        let request = super::ActionLineageRequest {
            schema_version: super::SCHEMA_VERSION,
            transition: "new_authorization".to_string(),
            action_class: "read".to_string(),
            budget_cost: 1,
            impact_units: 0,
            continuation: None,
        };
        db.action_intent_with_lineage(
            "idempotency-agent",
            "idempotency-workspace",
            "idempotency-scope",
            "idempotency-ref/a",
            "read",
            "same-action-key",
            &"a".repeat(64),
            Some("{}"),
            &[],
            None,
            &request,
        )
        .unwrap();
        let conflict = db
            .action_intent_with_lineage(
                "idempotency-agent",
                "idempotency-workspace",
                "idempotency-scope",
                "idempotency-ref/b",
                "read",
                "same-action-key",
                &"a".repeat(64),
                Some("{}"),
                &[],
                None,
                &request,
            )
            .unwrap_err()
            .to_string();
        assert!(conflict.contains("idempotency"));
        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn public_action_intent_denies_cross_session_send_and_returns_hash_only_receipt() {
        let path = std::env::temp_dir().join(format!(
            "perseus-vault-lineage-public-{}.db",
            uuid::Uuid::new_v4()
        ));
        let path_text = path.to_string_lossy().into_owned();
        let db = crate::db::Database::open(&path_text).unwrap();
        db.agent_upsert("public-agent", "Public Agent", 3, "lineage")
            .unwrap();
        db.authority_set(
            &crate::models::AuthorityManifestInput {
                agent_id: "public-agent".to_string(),
                workspace_hash: "public-workspace".to_string(),
                allowed_capabilities: vec!["read".to_string(), "external_send".to_string()],
                approval_required_capabilities: Vec::new(),
                scope_anchors: vec!["public-scope".to_string()],
                approver_principals: Vec::new(),
                allowed_inbound_principals: Vec::new(),
                permitted_external_ref_prefixes: vec!["public-ref".to_string()],
                max_parallel_actions: 1,
                mode: "enforce".to_string(),
                expires_at_unix_ms: None,
                capability_constraints_json: "{}".to_string(),
            },
            "public-admin",
        )
        .unwrap();

        let first_json = crate::tools::handle_action_intent(
            &db,
            json!({
                "agent_id": "public-agent",
                "workspace_hash": "public-workspace",
                "scope_anchor": "public-scope",
                "external_ref": "public-ref/read",
                "capability": "read",
                "action_key": "public-read",
                "intent_hash": "a".repeat(64),
                "resource_constraints_json": "{}",
                "lineage": {
                    "schema_version": 1,
                    "transition": "new_authorization",
                    "action_class": "read",
                    "budget_cost": 1,
                    "impact_units": 0
                }
            }),
        )
        .unwrap();
        let first: crate::models::AuthorizedAction = serde_json::from_str(&first_json).unwrap();
        assert_eq!(first.lineage_outcome, "new_authorization");
        let continuation =
            serde_json::to_value(first.lineage_continuation.clone().unwrap()).unwrap();

        let second_json = crate::tools::handle_action_intent(
            &db,
            json!({
                "agent_id": "public-agent",
                "workspace_hash": "public-workspace",
                "scope_anchor": "public-scope",
                "external_ref": "public-ref/send",
                "capability": "external_send",
                "action_key": "public-send",
                "intent_hash": "b".repeat(64),
                "resource_constraints_json": "{}",
                "lineage": {
                    "schema_version": 1,
                    "transition": "continue",
                    "action_class": "external_send",
                    "budget_cost": 1,
                    "impact_units": 1,
                    "continuation": continuation
                }
            }),
        )
        .unwrap();
        let second: crate::models::AuthorizedAction = serde_json::from_str(&second_json).unwrap();
        assert_eq!(second.status, "denied");
        assert_eq!(second.lineage_outcome, "denied");
        assert_eq!(
            second.lineage_receipt.as_ref().unwrap().reason_code,
            "composition_denied"
        );
        assert!(!second_json.contains("continuation_state_json"));
        assert!(!second_json.contains("LINEAGE-RAW-PROMPT-SENTINEL"));

        drop(db);
        let _ = std::fs::remove_file(&path_text);
        let _ = std::fs::remove_file(format!("{path_text}-wal"));
        let _ = std::fs::remove_file(format!("{path_text}-shm"));
    }

    #[test]
    fn ledger_composition_fixture_binds_vault_receipt_without_raw_payload() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../docs/specs/fixtures/task-action-lineage-ledger-v1.json"
        ))
        .unwrap();
        let vault = fixture.get("vault").unwrap();
        let binding = fixture
            .get("ledger")
            .and_then(|value| value.get("composition_binding"))
            .unwrap();
        assert_eq!(vault.get("lineage_id"), binding.get("task_lineage_id"));
        assert_eq!(vault.get("action_id"), binding.get("authority_action_id"));
        assert_eq!(vault.get("action_id"), binding.get("action_id"));
        assert_eq!(
            vault.get("authority_manifest_id"),
            binding.get("authority_ref")
        );
        assert_eq!(vault.get("policy_version"), binding.get("policy_version"));
        assert_eq!(
            vault.get("continuation_state_digest"),
            binding.get("state_hash")
        );
        assert_eq!(vault.get("head_digest"), binding.get("context_head_digest"));
        assert_eq!(binding.get("verdict").and_then(Value::as_str), Some("deny"));
        assert_eq!(
            fixture.get("sensitive_payload").and_then(Value::as_str),
            Some("not_captured")
        );
        assert!(!contains_forbidden_key(&fixture));
    }
}
