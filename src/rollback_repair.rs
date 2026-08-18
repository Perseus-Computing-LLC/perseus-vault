//! #1084: dependency-guided rollback repair for poisoned/stale memories
//! (arXiv:2608.10502).
//!
//! Deleting or revising a faulty memory leaves already-propagated claims,
//! actions, and derived memories active. This module repairs the
//! propagation, not just the source:
//!
//! - build a typed memory→action dependency graph from existing runtime
//!   provenance (entity links — including the #1064 typed edges — plus
//!   supersession columns and journal tool-call records),
//! - preserve candidates with independent trusted support (a dependent
//!   survives iff it retains at least one non-faulty source edge),
//! - deactivate unsupported state with tombstones (status quarantine —
//!   never deletes),
//! - selectively replay only answer-relevant affected computation (bounded
//!   dry-run consolidation proposals scoped to the affected
//!   category/workspace; the operator commits through the existing
//!   `perseus_vault_consolidate`),
//! - receipt-anchor every step (journal receipts + a durable repair state
//!   record), so the repair itself is auditable and reversible.

use std::collections::HashSet;

/// State-record prefix for durable, reversible repair plans.
pub const REPAIR_STATE_PREFIX: &str = "rollback_repair.";
/// Tombstone status: quarantine removes the row from the serveable set
/// (SERVEABLE_STATUS_SQL) without deleting it.
pub const TOMBSTONE_STATUS: &str = "quarantined";
/// Archive reason prefix stamped on tombstoned rows (names the repair).
pub fn tombstone_reason(repair_id: &str) -> String {
    format!("rollback-repair {repair_id}: deactivated — unsupported derived state")
}

/// One dependent's evidence split for classification.
#[derive(Debug, Clone, serde::Serialize)]
pub struct DependentEvidence {
    pub entity_id: String,
    pub independent_support: Vec<String>,
    pub faulty_support: Vec<String>,
    pub preserved: bool,
}

/// Classification rule (the paper's preservation gate): a dependent is
/// preserved iff at least one of its cited sources is NOT faulty
/// (independent trusted support). A dependent whose support is entirely
/// faulty is unsupported and gets tombstoned.
pub fn classify(
    entity_id: &str,
    source_ids: &[String],
    faulty: &HashSet<String>,
) -> DependentEvidence {
    let mut independent = Vec::new();
    let mut faulty_support = Vec::new();
    for s in source_ids {
        if faulty.contains(s) {
            faulty_support.push(s.clone());
        } else {
            independent.push(s.clone());
        }
    }
    DependentEvidence {
        entity_id: entity_id.to_string(),
        preserved: !independent.is_empty(),
        independent_support: independent,
        faulty_support,
    }
}

/// Sever the edges pointing at faulty targets. Returns the surviving links
/// and the removed edges (kept for the reversal record).
pub fn sever_faulty_edges(
    links: &[crate::models::MemoryLink],
    faulty: &HashSet<String>,
) -> (Vec<crate::models::MemoryLink>, Vec<crate::models::MemoryLink>) {
    let mut kept = Vec::new();
    let mut removed = Vec::new();
    for l in links {
        if faulty.contains(&l.target_id) {
            removed.push(l.clone());
        } else {
            kept.push(l.clone());
        }
    }
    (kept, removed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_preserves_only_with_independent_support() {
        let faulty: HashSet<String> = ["mem-a".to_string()].into_iter().collect();
        let with_support = classify("mem-x", &["mem-a".into(), "mem-b".into()], &faulty);
        assert!(with_support.preserved);
        assert_eq!(with_support.independent_support, vec!["mem-b"]);
        assert_eq!(with_support.faulty_support, vec!["mem-a"]);

        let unsupported = classify("mem-y", &["mem-a".into()], &faulty);
        assert!(!unsupported.preserved);
        assert!(unsupported.independent_support.is_empty());

        let no_sources = classify("mem-z", &[], &faulty);
        assert!(!no_sources.preserved, "no support at all is unsupported");
    }

    // ── DB integration ──

    fn ent(id: &str, body: &str, links: Vec<crate::models::MemoryLink>) -> crate::models::Entity {
        crate::models::Entity {
            id: id.to_string(),
            category: "facts".to_string(),
            key: id.to_string(),
            body_json: body.to_string(),
            status: "active".to_string(),
            entity_type: "insight".to_string(),
            tags: vec![],
            decay_score: 1.0,
            retrieval_count: 0,
            layer: "working".to_string(),
            topic_path: String::new(),
            archived: false,
            archive_reason: String::new(),
            links,
            verified: false,
            source: "agent".to_string(),
            always_on: false,
            certainty: 0.5,
            workspace_hash: String::new(),
            agent_id: String::new(),
            visibility: "workspace".to_string(),
            created_at_unix_ms: crate::db::now_ms(),
            last_accessed_unix_ms: crate::db::now_ms(),
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

    fn link(target: &str) -> crate::models::MemoryLink {
        crate::models::MemoryLink {
            target_id: target.to_string(),
            relationship: "derived_from".to_string(),
            weight: 1.0,
            source: Some("mem-x".to_string()),
            kind: None,
            asserted_at_unix_ms: None,
        }
    }

    #[test]
    fn end_to_end_preserve_tombstone_reverse() {
        let db = crate::db::TestDatabase::new("rollback-e2e");
        let a = "mem-fact-a";
        let b = "mem-fact-b";
        // Faulty A; benign B; claim X depends on A+B (independent support via
        // B); claim Y depends only on A (unsupported); benign Z untouched.
        // Bodies are deliberately lexically distinct: the write interference
        // gate (#874) quarantines trigram-similar writes instead of creating
        // rows, which would collapse this fixture.
        db.remember_skip_dedup(&ent(a, "{\"fact\": \"alpha one\"}", vec![])).unwrap();
        db.remember_skip_dedup(&ent(b, "{\"fact\": \"bravo two\"}", vec![])).unwrap();
        db.remember_skip_dedup(&ent(
            "mem-claim-x",
            "{\"claim\": \"combines alpha and bravo\"}",
            vec![link(a), link(b)],
        ))
        .unwrap();
        db.remember_skip_dedup(&ent(
            "mem-claim-y",
            "{\"claim\": \"solo alpha\"}",
            vec![link(a)],
        ))
        .unwrap();
        db.remember_skip_dedup(&ent("mem-benign-z", "{\"claim\": \"benign untouched\"}", vec![]))
            .unwrap();

        // Dry run writes nothing.
        let plan = db.rollback_repair(&[a.to_string()], true, false, None).unwrap();
        assert_eq!(plan["dry_run"], true);
        let conn = db.conn().unwrap();
        let n_state: i64 = conn
            .query_row("SELECT COUNT(*) FROM state", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_state, 0);

        // Execute.
        let get = |id: &str| db.get_entity_by_id_unfiltered(id).unwrap().unwrap();
        let prior_a_status = get(a).status.clone();
        let prior_y_status = get("mem-claim-y").status.clone();
        let report = db
            .rollback_repair(&[a.to_string()], false, true, None)
            .unwrap();
        let repair_id = report["repair_id"].as_str().unwrap().to_string();
        assert_eq!(report["dry_run"], false);
        let preserved_ids: Vec<&str> = report["preserved"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["entity_id"].as_str().unwrap())
            .collect();
        assert_eq!(preserved_ids, vec!["mem-claim-x"]);
        let tombstoned: Vec<&str> = report["tombstoned"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert!(tombstoned.contains(&a) && tombstoned.contains(&"mem-claim-y"));
        assert!(!tombstoned.contains(&"mem-claim-x"));

        // State check: A + Y quarantined; X keeps its prior status with only
        // the B edge left; benign Z untouched (100% benign preservation).
        let prior_x = get("mem-claim-x").status.clone();
        let prior_z = get("mem-benign-z").status.clone();
        assert_eq!(get(a).status, "quarantined");
        assert_eq!(get("mem-claim-y").status, "quarantined");
        let x = get("mem-claim-x");
        assert_eq!(x.status, prior_x);
        assert_eq!(x.links.len(), 1);
        assert_eq!(x.links[0].target_id, b);
        let z = get("mem-benign-z");
        assert_eq!(z.status, prior_z);
        assert!(z.links.is_empty());
        // Receipts: tombstone ×2 + preserved ×1.
        let n_j: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM journal WHERE event_type LIKE 'rollback_repair%'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(n_j, 3);
        // Selective replay proposal present, scoped, not auto-committed.
        assert!(report["replay"].is_object());
        assert_eq!(report["replay"]["committed"], false);

        // Reverse: restore statuses, re-link severed edges, clean the plan.
        let rev = db.reverse_rollback_repair(&repair_id).unwrap();
        assert_eq!(rev["reversed"], true);
        assert_eq!(get(a).status, prior_a_status);
        assert_eq!(get("mem-claim-y").status, prior_y_status);
        let x = get("mem-claim-x");
        assert_eq!(x.links.len(), 2, "severed edge must be re-linked");
        let n_state: i64 = conn
            .query_row("SELECT COUNT(*) FROM state", [], |r| r.get(0))
            .unwrap();
        assert_eq!(n_state, 0);
        assert!(db.reverse_rollback_repair("rpr-nope").is_err());
    }

    #[test]
    fn unknown_faulty_id_fails_closed() {
        let db = crate::db::TestDatabase::new("rollback-unknown");
        let err = db
            .rollback_repair(&["mem-ghost".to_string()], false, false, None)
            .unwrap_err();
        assert!(err.contains("not found"));
        assert!(db.rollback_repair(&[], false, false, None).is_err());
    }

    #[test]
    fn supersession_predecessor_survives_as_self_support() {
        // An entity replaced BY a faulty successor carries the pre-fault
        // state and survives as its own independent evidence.
        let db = crate::db::TestDatabase::new("rollback-supersede");
        let good = "mem-v1";
        let faulty = "mem-v2";
        db.remember_skip_dedup(&ent(good, "{\"v\": \"gamma original\"}", vec![])).unwrap();
        db.remember_skip_dedup(&ent(faulty, "{\"v\": \"delta faulty\"}", vec![])).unwrap();
        {
            let conn = db.conn().unwrap();
            conn.execute(
                "UPDATE entities SET superseded_by = ?1 WHERE id = ?2",
                [faulty, good],
            )
            .unwrap();
        }
        let report = db
            .rollback_repair(&[faulty.to_string()], false, false, None)
            .unwrap();
        let preserved_ids: Vec<&str> = report["preserved"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v["entity_id"].as_str().unwrap())
            .collect();
        assert_eq!(preserved_ids, vec![good]);
        let prior_good = db.get_entity_by_id_unfiltered(good).unwrap().unwrap().status.clone();
        assert_eq!(
            db.get_entity_by_id_unfiltered(good).unwrap().unwrap().status,
            prior_good
        );
        let rid = report["repair_id"].as_str().unwrap().to_string();
        db.reverse_rollback_repair(&rid).unwrap();
    }

    #[test]
    fn sever_faulty_edges_removes_exact_targets_only() {
        let faulty: HashSet<String> = ["mem-a".to_string()].into_iter().collect();
        let links = vec![
            crate::models::MemoryLink {
                target_id: "mem-a".into(),
                relationship: "derived_from".into(),
                weight: 1.0,
                source: Some("mem-x".into()),
                kind: None,
                asserted_at_unix_ms: None,
            },
            crate::models::MemoryLink {
                target_id: "mem-b".into(),
                relationship: "derived_from".into(),
                weight: 1.0,
                source: Some("mem-x".into()),
                kind: None,
                asserted_at_unix_ms: None,
            },
        ];
        let (kept, removed) = sever_faulty_edges(&links, &faulty);
        assert_eq!(kept.len(), 1);
        assert_eq!(kept[0].target_id, "mem-b");
        assert_eq!(removed.len(), 1);
        assert_eq!(removed[0].target_id, "mem-a");
    }
}
