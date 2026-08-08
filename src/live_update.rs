//! #858 — live-update / reconnect workflow.
//!
//! A stdio MCP client (e.g. Rovo Dev) spawns the `perseus-vault` child once
//! per session. If the binary is rebuilt/replaced on disk mid-session, the
//! running process image is stale but the client keeps talking to it — and the
//! failure mode was subtle: calls degrading into empty results until a full
//! session restart.
//!
//! This module makes the situation explicit and recoverable:
//!
//! 1. **Detection** — capture the running binary's identity (dev/ino + len +
//!    mtime) once at startup; compare against the current on-disk file on
//!    every tool call (one `stat`).
//! 2. **Fail loud** — when stale, every tool except the handoff tool itself
//!    and `mimir_health` (which reports the staleness in its payload) returns
//!    an explicit `isError` result instead of silently serving stale results.
//!    Override for one-off diagnostics: `PERSEUS_VAULT_IGNORE_STALE_BINARY=1`.
//! 3. **Hot-swap handoff** — `perseus_vault_handoff_restart` spawns the
//!    replacement binary on the SAME stdio fds (Rust's default `Command`
//!    inherits them) and exits; the client's pipes never close and the MCP
//!    session continues in the new process image. The child is tagged
//!    `PERSEUS_VAULT_HANDOFF_CHILD=1` so the orphan guards (PDEATHSIG, ppid
//!    watcher, per-request poll) do not reap it when its spawning parent exits
//!    — the handoff protocol is the liveness proof, and stdin EOF remains the
//!    real client-death signal.
//!
//! Safety notes on the handoff:
//! - The exec happens in the server loop AFTER the response is written and
//!   flushed (via [`handoff_pending`]), so the client receives the report
//!   before the process image switches.
//! - Do not pipeline requests during handoff: the old process's stdin
//!   `BufReader` may hold read-ahead bytes that die with the process. MCP
//!   clients are strictly request/response, so this is a documented
//!   constraint, not a live hazard.
//! - SQLite is in WAL mode: the child's fresh open recovers any unfinished
//!   state from the old process exactly like the orphan-watcher exit path
//!   (#547) already does.

use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// Identity of a binary file: path + (dev, ino) + (len, mtime_ns).
///
/// dev/ino uniquely identify the file *object* — a rename-replace (the normal
/// `cargo build` / `install` pattern) changes the inode. len/mtime catch
/// in-place rewrites that reuse the same inode (toolchains that patch the file
/// rather than replace it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BinaryIdentity {
    pub path: PathBuf,
    pub dev: u64,
    pub ino: u64,
    pub len: u64,
    pub mtime_ns: i64,
}

impl BinaryIdentity {
    pub fn capture(path: &Path) -> Option<BinaryIdentity> {
        let md = std::fs::metadata(path).ok()?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            // MetadataExt::mtime() is i64 seconds since epoch; mtime_nsec() is
            // the timespec nanosecond component — combine for ns resolution.
            let mtime_ns = md
                .mtime()
                .saturating_mul(1_000_000_000)
                .saturating_add(md.mtime_nsec());
            Some(BinaryIdentity {
                path: path.to_path_buf(),
                dev: md.dev(),
                ino: md.ino(),
                len: md.len(),
                mtime_ns,
            })
        }
        #[cfg(not(unix))]
        {
            let mtime_ns = md
                .modified()
                .ok()
                .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                .map(|d| d.as_nanos() as i64)
                .unwrap_or(0);
            Some(BinaryIdentity {
                path: path.to_path_buf(),
                dev: 0,
                ino: 0,
                len: md.len(),
                mtime_ns,
            })
        }
    }

    /// True when the file this identity was captured from no longer matches
    /// what is on disk at the same path (replaced, rewritten, or deleted).
    pub fn replaced(&self) -> bool {
        match BinaryIdentity::capture(&self.path) {
            None => true,
            Some(cur) => {
                cur.dev != self.dev
                    || cur.ino != self.ino
                    || cur.len != self.len
                    || cur.mtime_ns != self.mtime_ns
            }
        }
    }
}

/// The identity of the binary this process was launched from — captured once
/// (the process image is immutable; only the on-disk file can change).
pub fn running_identity() -> Option<&'static BinaryIdentity> {
    static RUNNING: OnceLock<Option<BinaryIdentity>> = OnceLock::new();
    RUNNING
        .get_or_init(|| {
            std::env::current_exe()
                .ok()
                .and_then(|p| BinaryIdentity::capture(&p))
        })
        .as_ref()
}

/// True when this process was spawned by a `handoff_restart` from an older
/// server instance (env `PERSEUS_VAULT_HANDOFF_CHILD=1`).
pub fn handoff_child() -> bool {
    std::env::var_os("PERSEUS_VAULT_HANDOFF_CHILD").is_some()
}

/// Whether the running binary has been replaced on disk since this process
/// started.
pub fn running_stale() -> bool {
    running_identity().map(|i| i.replaced()).unwrap_or(false)
}

/// Pure gate logic: the message to return (or None when the call may proceed).
fn stale_message_for(stale: bool, tool: &str, ignore: bool) -> Option<String> {
    if !stale || ignore {
        return None;
    }
    if tool == "mimir_handoff_restart" || tool == "mimir_health" {
        return None;
    }
    let pid = std::process::id();
    let path = running_identity()
        .map(|i| i.path.display().to_string())
        .unwrap_or_default();
    Some(format!(
        "perseus-vault: the running binary was replaced on disk (pid {pid}, {path}); \
         refusing to serve results from a stale process image. Run \
         perseus_vault_handoff_restart with {{\"confirm\": true}} to hot-swap this \
         session on the same stdio connection, or restart the client session. \
         To override for diagnostics: PERSEUS_VAULT_IGNORE_STALE_BINARY=1"
    ))
}

/// Fail-loud gate for the MCP dispatch: when the binary was replaced
/// mid-session, every tool except the handoff tool itself (and `mimir_health`,
/// which reports the staleness in its payload) refuses with an explicit error
/// — never a silent empty result (#858).
pub fn stale_error_message(tool: &str) -> Option<String> {
    stale_message_for(
        running_stale(),
        tool,
        std::env::var_os("PERSEUS_VAULT_IGNORE_STALE_BINARY").is_some(),
    )
}

/// Set by `handle_handoff_restart` when the exec branch is taken; consumed by
/// the MCP server loop after the response is flushed.
static HANDOFF_PENDING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

pub fn handoff_pending() -> bool {
    HANDOFF_PENDING.load(std::sync::atomic::Ordering::SeqCst)
}

fn set_handoff_pending() {
    HANDOFF_PENDING.store(true, std::sync::atomic::Ordering::SeqCst);
}

/// Spawn the replacement binary on the same stdio fds. Rust's `Command`
/// inherits stdin/stdout/stderr by default, so the client's pipes never close
/// and the MCP session continues in the new process image. The child is tagged
/// `PERSEUS_VAULT_HANDOFF_CHILD=1` so the orphan guards do not reap it when
/// this process exits.
pub fn perform_handoff() -> Result<(), String> {
    let path = std::env::current_exe().map_err(|e| format!("current_exe: {e}"))?;
    let args: Vec<String> = std::env::args().skip(1).collect();
    let child = std::process::Command::new(&path)
        .args(&args)
        .env("PERSEUS_VAULT_HANDOFF_CHILD", "1")
        .spawn()
        .map_err(|e| format!("spawn {}: {e}", path.display()))?;
    eprintln!(
        "mimir: handoff spawned child pid {} ({}) — old process exiting",
        child.id(),
        path.display()
    );
    Ok(())
}

/// Pure report builder — the four handoff states, testable without touching
/// the running test binary.
fn handoff_report_for(stale: bool, dry_run: bool, confirm: bool) -> Value {
    let ident = running_identity();
    fn identity_json(i: &BinaryIdentity) -> Value {
        json!({"dev": i.dev, "ino": i.ino, "len": i.len, "mtime_ns": i.mtime_ns})
    }
    let mut report = json!({
        "pid": std::process::id(),
        "binary_path": ident.map(|i| i.path.display().to_string()).unwrap_or_default(),
        "running_identity": ident.map(identity_json).unwrap_or(Value::Null),
        "disk_identity": ident
            .and_then(|i| BinaryIdentity::capture(&i.path))
            .map(|b| identity_json(&b))
            .unwrap_or(Value::Null),
        "binary_stale": stale,
    });
    if !stale {
        report["status"] = json!("no_handoff_needed");
        report["note"] = json!(
            "The running binary matches the file on disk; no handoff required. \
             Rebuild the binary and call again to hot-swap."
        );
    } else if dry_run {
        report["status"] = json!("dry_run");
        report["would_handoff"] = json!(true);
        report["note"] = json!(
            "Binary replaced; a confirm:true call would spawn the new binary on \
             this same stdio session and exit this process."
        );
    } else if !confirm {
        report["status"] = json!("confirm_required");
        report["note"] = json!(
            "Binary replaced; pass {\"confirm\": true} to perform the hot-swap."
        );
    } else {
        report["status"] = json!("handoff_performed");
        report["note"] = json!(
            "Hot-swap scheduled: the new binary starts on this session immediately \
             after this response. If the session goes quiet, the spawn failed and \
             the client session should be restarted."
        );
    }
    report
}

/// #858: `mimir_handoff_restart` — explicit session-local reconnect.
///
/// - binary unchanged → `no_handoff_needed` (identity report included)
/// - stale + `dry_run` → `dry_run` report of what would happen
/// - stale, no `confirm` → `confirm_required` (clear feedback, no exec)
/// - stale + `confirm` → schedules the hot-swap; the server loop performs the
///   spawn AFTER flushing this very response, then exits.
pub fn handle_handoff_restart(args: Value) -> Result<String, String> {
    let dry_run = args.get("dry_run").and_then(Value::as_bool).unwrap_or(false);
    let confirm = args.get("confirm").and_then(Value::as_bool).unwrap_or(false);
    let stale = running_stale();
    let report = handoff_report_for(stale, dry_run, confirm);
    if stale && confirm && !dry_run {
        set_handoff_pending();
    }
    Ok(report.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::Duration;

    fn tmp_file(name: &str, content: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("live-update-test-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let p = dir.join(name);
        fs::write(&p, content).unwrap();
        p
    }

    #[test]
    fn untouched_file_is_not_replaced() {
        let p = tmp_file("same.txt", "hello");
        let id = BinaryIdentity::capture(&p).expect("capture");
        assert!(!id.replaced(), "untouched file must not look replaced");
        fs::remove_file(&p).ok();
    }

    #[test]
    fn in_place_rewrite_is_detected() {
        let p = tmp_file("rewrite.txt", "hello");
        let id = BinaryIdentity::capture(&p).expect("capture");
        std::thread::sleep(Duration::from_millis(20)); // distinct mtime
        fs::write(&p, "hello world — a longer rewrite").unwrap();
        assert!(id.replaced(), "in-place rewrite must be detected");
        fs::remove_file(&p).ok();
    }

    #[test]
    fn rename_replace_is_detected() {
        let p = tmp_file("rename-target.txt", "old image");
        let id = BinaryIdentity::capture(&p).expect("capture");
        let replacement = tmp_file("rename-source.txt", "new image");
        std::thread::sleep(Duration::from_millis(20));
        fs::rename(&replacement, &p).unwrap();
        assert!(id.replaced(), "rename-replace (new inode) must be detected");
        fs::remove_file(&p).ok();
    }

    #[test]
    fn deletion_is_detected() {
        let p = tmp_file("deleted.txt", "gone soon");
        let id = BinaryIdentity::capture(&p).expect("capture");
        fs::remove_file(&p).unwrap();
        assert!(id.replaced(), "deletion must be detected");
    }

    #[test]
    fn stale_gate_blocks_all_but_handoff_and_health() {
        assert!(stale_message_for(true, "mimir_recall", false).is_some());
        assert!(stale_message_for(true, "perseus_vault_remember", false).is_some());
        // The recovery tools themselves stay callable.
        assert!(stale_message_for(true, "mimir_handoff_restart", false).is_none());
        assert!(stale_message_for(true, "mimir_health", false).is_none());
        // Not stale → no gate; ignore override → no gate.
        assert!(stale_message_for(false, "mimir_recall", false).is_none());
        assert!(stale_message_for(true, "mimir_recall", true).is_none());
    }

    #[test]
    fn handoff_report_covers_all_four_states() {
        let ok = handoff_report_for(false, false, false);
        assert_eq!(ok["status"], "no_handoff_needed");
        assert_eq!(ok["binary_stale"], false);

        let dry = handoff_report_for(true, true, false);
        assert_eq!(dry["status"], "dry_run");
        assert_eq!(dry["would_handoff"], true);

        let need_confirm = handoff_report_for(true, false, false);
        assert_eq!(need_confirm["status"], "confirm_required");

        let go = handoff_report_for(true, false, true);
        assert_eq!(go["status"], "handoff_performed");
        assert!(go["binary_path"].as_str().map(|s| !s.is_empty()).unwrap_or(false));
    }

    #[test]
    fn handoff_pending_flag_roundtrip() {
        assert!(!handoff_pending());
        set_handoff_pending();
        assert!(handoff_pending());
        // Reset so parallel tests / later dispatch smoke tests are unaffected.
        HANDOFF_PENDING.store(false, std::sync::atomic::Ordering::SeqCst);
        assert!(!handoff_pending());
    }

    #[test]
    fn handler_no_handoff_needed_when_binary_untouched() {
        // In the test process the running binary (the test harness) is not
        // replaced, so the handler must report no_handoff_needed and must NOT
        // schedule a handoff.
        let out = handle_handoff_restart(json!({})).expect("handler");
        let v: Value = serde_json::from_str(&out).expect("json");
        assert_eq!(v["status"], "no_handoff_needed");
        assert!(!handoff_pending());
    }
}
