//! #871: CLI integration tests for the durable operation-state surface.
//!
//! Exercises the real binary end-to-end: the op-producing CLI verbs
//! (`decay`, `reindex`) create durable runs, and `op-runs list|show|retry|prune`
//! expose them. Lifecycle actions (begin/start/...) are MCP-tool surface and
//! are exercised by the in-process unit tests instead.

use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_perseus-vault");

fn scratch_db(name: &str) -> std::path::PathBuf {
    let path = std::env::temp_dir().join(format!("{name}-{}.db", std::process::id()));
    let _ = std::fs::remove_file(&path);
    path
}

fn op_runs(db: &std::path::Path, args: &[&str]) -> (bool, String) {
    let out = Command::new(BIN)
        .arg("op-runs")
        .arg("--db")
        .arg(db.to_str().unwrap())
        .args(args)
        .output()
        .expect("spawn perseus-vault");
    (
        out.status.success(),
        String::from_utf8_lossy(&out.stdout).to_string(),
    )
}

#[test]
fn op_runs_cli_observability_retry_and_prune() {
    let db = scratch_db("opruns-cli");

    // Initialize the vault so the DB exists with the v35 schema.
    let init = Command::new(BIN)
        .arg("init")
        .arg("--db")
        .arg(db.to_str().unwrap())
        .output()
        .expect("init");
    assert!(init.status.success(), "init failed");

    // 1) Fresh DB: list is empty and well-formed.
    let (ok, list) = op_runs(&db, &[]);
    assert!(ok, "list failed");
    assert!(list.contains("\"count\": 0"), "fresh list empty: {list}");

    // 2) `decay` is an op-producing verb: it creates and completes a run.
    let out = Command::new(BIN)
        .arg("decay")
        .arg("--db")
        .arg(db.to_str().unwrap())
        .output()
        .expect("decay");
    assert!(
        out.status.success(),
        "decay failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );

    let (ok, list) = op_runs(&db, &[]);
    assert!(ok, "list 2 failed");
    assert!(list.contains("\"count\": 1"), "decay run recorded: {list}");
    assert!(
        list.contains("\"op_type\": \"decay\""),
        "op_type decay: {list}"
    );
    assert!(
        list.contains("\"state\": \"completed\""),
        "decay completed: {list}"
    );
    assert!(list.contains("opr-"), "run id present: {list}");

    // 3) `show` returns the run with its receipt.
    let id: String = {
        let v: serde_json::Value = serde_json::from_str(&list).expect("list json");
        v["runs"][0]["id"].as_str().expect("run id").to_string()
    };
    let (ok, show) = op_runs(&db, &["--action", "show", "--run-id", &id]);
    assert!(ok, "show failed");
    assert!(
        show.contains("\"state\": \"completed\""),
        "show completed: {show}"
    );
    assert!(
        show.contains("entities_checked="),
        "receipt visible: {show}"
    );

    // 4) Retry of a fully-completed run with nothing recoverable is refused.
    let out = Command::new(BIN)
        .arg("op-runs")
        .arg("--db")
        .arg(db.to_str().unwrap())
        .args(["--action", "retry", "--run-id", &id])
        .output()
        .expect("retry");
    assert!(!out.status.success(), "completed run retry must be refused");
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    assert!(stderr.contains("nothing"), "refusal explains: {stderr}");

    // 5) State filter works.
    let (ok, filtered) = op_runs(&db, &["--state", "completed"]);
    assert!(ok, "filtered list failed");
    assert!(
        filtered.contains("\"count\": 1"),
        "state filter: {filtered}"
    );
    let (ok, none) = op_runs(&db, &["--state", "failed"]);
    assert!(ok, "failed filter ok");
    assert!(none.contains("\"count\": 0"), "failed filter empty: {none}");

    // 6) Retention prune runs and reports a count.
    let (ok, prune) = op_runs(&db, &["--action", "prune", "--retention-days", "30"]);
    assert!(ok, "prune failed");
    assert!(prune.contains("\"pruned\": 0"), "recent run kept: {prune}");
    assert!(
        prune.contains("\"retention_days\": 30"),
        "prune report: {prune}"
    );

    // 7) Unknown run id is a clean CLI error.
    let out = Command::new(BIN)
        .arg("op-runs")
        .arg("--db")
        .arg(db.to_str().unwrap())
        .args(["--action", "show", "--run-id", "opr-does-not-exist"])
        .output()
        .expect("show unknown");
    assert!(!out.status.success(), "unknown run must fail");
    assert!(String::from_utf8_lossy(&out.stderr).contains("unknown op run"));

    let _ = std::fs::remove_file(&db);
}
