use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::sync::OnceLock;

use crate::db::Database;
use crate::tools;
use crate::beliefs;
use crate::claim_card;

/// The parent PID observed once at process start, before any reparenting can
/// occur. `is_orphaned_by_ppid()` compares the live ppid against this baseline
/// so we detect *reparenting* (parent died → we were re-adopted) rather than
/// the mere fact that our ppid is 1.
///
/// This distinction matters in containers: when the vault is spawned directly
/// by a PID-1 entrypoint (e.g. a Python `demo_server_local.py` running as the
/// container's init, or any `docker run <binary>` where the binary's launcher
/// is PID 1), a perfectly healthy child legitimately has `getppid() == 1` from
/// birth. The original `getppid() == 1` guard (#547) false-positived on exactly
/// that topology and self-terminated a live server on its first request. See
/// the demo-container regression: parent is PID 1, so every start tripped the
/// orphan guard and crash-looped.
static INITIAL_PPID: OnceLock<i32> = OnceLock::new();

/// Windows only: the parent process's creation timestamp, captured alongside
/// INITIAL_PPID. OpenProcess-liveness alone is vulnerable to PID reuse — a
/// dead parent's PID can be recycled by an unrelated process, which would look
/// "alive". Comparing creation times makes the liveness check exact.
#[cfg(windows)]
static INITIAL_PPID_CREATE_TIME: OnceLock<u64> = OnceLock::new();

/// Record the current parent PID as the baseline. Call once, as early as
/// possible in `run_server`, before entering the request loop. Idempotent:
/// only the first call sets the baseline.
pub fn record_initial_ppid() {
    // getppid() has identical reparent-to-PID-1 semantics on every Unix
    // (Linux: init; macOS: launchd), so the baseline/orphan check is not
    // Linux-specific — widened from cfg(linux) to cfg(unix) for #748.
    #[cfg(unix)]
    {
        let _ = INITIAL_PPID.set(unsafe { libc::getppid() });
    }
    // Windows has no getppid(): recover the parent PID from a Toolhelp
    // snapshot and stamp its creation time for the PID-reuse-safe liveness
    // check (#751).
    #[cfg(windows)]
    {
        let ppid = windows_parent_pid().unwrap_or(0);
        let _ = INITIAL_PPID.set(ppid);
        if ppid > 0 {
            if let Some(t) = windows_process_creation_time(ppid) {
                let _ = INITIAL_PPID_CREATE_TIME.set(t);
            }
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = INITIAL_PPID.set(0);
    }
}

/// Current parent PID via a Toolhelp process snapshot (Windows has no
/// getppid). Returns None if the snapshot fails — callers treat that as
/// "unknown" and never false-fire the orphan guard.
#[cfg(windows)]
fn windows_parent_pid() -> Option<i32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32First, Process32Next, PROCESSENTRY32,
        TH32CS_SNAPPROCESS,
    };
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if snap == INVALID_HANDLE_VALUE {
            return None;
        }
        let mut entry: PROCESSENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<PROCESSENTRY32>() as u32;
        let self_pid = std::process::id();
        let mut found = None;
        if Process32First(snap, &mut entry) != 0 {
            loop {
                if entry.th32ProcessID == self_pid {
                    found = Some(entry.th32ParentProcessID as i32);
                    break;
                }
                if Process32Next(snap, &mut entry) == 0 {
                    break;
                }
            }
        }
        CloseHandle(snap);
        found
    }
}

/// Creation time of `pid` as a u64 FILETIME, or None if the process cannot be
/// opened (i.e. it is dead or inaccessible). Doubles as the liveness probe.
#[cfg(windows)]
fn windows_process_creation_time(pid: i32) -> Option<u64> {
    use windows_sys::Win32::Foundation::{CloseHandle, FILETIME};
    use windows_sys::Win32::System::Threading::{
        GetProcessTimes, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid as u32);
        if h == 0 {
            return None;
        }
        let mut ct: FILETIME = std::mem::zeroed();
        let mut et: FILETIME = std::mem::zeroed();
        let mut kt: FILETIME = std::mem::zeroed();
        let mut ut: FILETIME = std::mem::zeroed();
        let ok = GetProcessTimes(h, &mut ct, &mut et, &mut kt, &mut ut);
        CloseHandle(h);
        if ok == 0 {
            return None;
        }
        Some(((ct.dwHighDateTime as u64) << 32) | ct.dwLowDateTime as u64)
    }
}

/// Returns `true` if this process has been reparented since start, which is the
/// definitive indicator that the spawning parent has died.
///
/// Orphaning is detected as: the live ppid differs from the baseline captured
/// at start AND the live ppid is now 1 (reparented to init). A process that was
/// *born* with ppid == 1 (its launcher is the container's PID-1 init) is NOT an
/// orphan — its baseline is 1 and stays 1, so this correctly returns `false`.
///
/// On Windows (#751) there is no reparenting: the check instead probes the
/// recorded parent PID with `OpenProcess` and compares creation timestamps, so
/// a dead parent — or a PID recycled by an unrelated process — is detected.
/// Unknown parent (snapshot failed at startup) conservatively returns `false`.
///
/// Exposed as `pub` so the orphan case can be unit-tested without needing to
/// actually kill a parent process.
pub fn is_orphaned_by_ppid() -> bool {
    // #858: a handoff child is deliberately reparented when the spawning
    // server exits — the handoff protocol is its liveness proof, and stdin
    // EOF remains the real client-death signal.
    if crate::live_update::handoff_child() {
        return false;
    }
    // Safety: getppid() is always safe — no undefined behaviour, no allocation.
    // All Unix platforms (Linux AND macOS) reparent orphans to PID 1, so this
    // check is the primary parent-death signal on macOS too (#748) — without
    // it, macOS/Windows had no orphan signal at all and the flat idle timer
    // was the only guard, killing healthy-but-quiet hosts.
    #[cfg(unix)]
    {
        let current = unsafe { libc::getppid() };
        // Baseline should have been recorded at startup; if it wasn't (defensive),
        // fall back to comparing against the current value so we never false-fire.
        let baseline = *INITIAL_PPID.get_or_init(|| current);
        // Orphaned only if we were reparented to init: born under a real parent
        // (baseline != 1) and now adopted by init (current == 1). A process born
        // directly under PID 1 has baseline == 1 and is never treated as orphaned.
        current == 1 && baseline != 1
    }
    // Windows (#751): no reparenting concept — instead probe the recorded
    // parent PID with OpenProcess and compare creation times (PID-reuse safe).
    #[cfg(windows)]
    {
        let baseline = *INITIAL_PPID.get_or_init(|| 0);
        if baseline <= 0 {
            // Parent unknown (snapshot failed at start): never false-fire.
            return false;
        }
        match windows_process_creation_time(baseline) {
            // OpenProcess/GetProcessTimes failed -> parent is gone.
            None => true,
            // Handle opened: alive only if it is the SAME process (creation
            // time matches the startup stamp). A recycled PID means the
            // original parent died.
            Some(t) => match INITIAL_PPID_CREATE_TIME.get() {
                Some(recorded) => t != *recorded,
                // No stamp recorded: liveness alone is the best signal we have.
                None => false,
            },
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        false
    }
}

#[derive(Debug, Deserialize)]
pub struct JsonRpcRequest {
    pub jsonrpc: String,
    #[serde(default)]
    pub id: Option<Value>,
    pub method: String,
    #[serde(default)]
    pub params: Option<Value>,
}

#[derive(Debug, Serialize)]
pub struct JsonRpcResponse {
    pub jsonrpc: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub id: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<JsonRpcError>,
}

#[derive(Debug, Serialize)]
pub struct JsonRpcError {
    pub code: i64,
    pub message: String,
}

pub struct MCPState {
    // #210: AtomicBool so the HTTP/SSE transport can share &MCPState across
    // concurrent requests without a Mutex (which would re-serialize them now
    // that the DB pool removed the other lock). handle_request takes &MCPState.
    pub initialized: std::sync::atomic::AtomicBool,
    // #684: agent identity captured from the `initialize` handshake's
    // clientInfo.name. Threaded into tool calls as `requesting_agent_id` so
    // visibility enforcement knows who is asking. Empty when the client sent no
    // clientInfo (single-agent / legacy) → unscoped, preserving old behavior.
    // RwLock: set once at initialize, read per tools/call across the shared &state.
    pub session_agent_id: std::sync::RwLock<String>,
}

impl MCPState {
    pub fn new() -> Self {
        MCPState {
            initialized: std::sync::atomic::AtomicBool::new(false),
            session_agent_id: std::sync::RwLock::new(String::new()),
        }
    }
}

/// Parse the `PERSEUS_VAULT_IDLE_TIMEOUT_SECS` env value into an idle-watchdog duration.
///
/// - unset / "0" / unparseable  -> disabled (None). DEFAULT IS OFF since #748:
///   inactivity is NOT proof of abandonment — a quiet-but-alive host (Claude
///   Desktop routinely goes many minutes between tool calls and never respawns
///   a dead server) must never be reaped. Parent death is detected
///   deterministically by the orphan watcher (PDEATHSIG on Linux, ppid poll
///   everywhere else), so the flat timer is no longer the orphan guard.
/// - "N" (N > 0)                -> Some(N seconds): OPT-IN aggressive reaping,
///   for the one topology parent-death detection cannot see: a host that leaks
///   the child's stdin write-end while STAYING ALIVE (the original #57228
///   Hermes-worker reconnect leak). Hosts with that lifecycle should set this
///   when spawning the server.
///
/// Factored out of `run_server` so the watchdog policy is unit-tested.
pub fn parse_idle_timeout(raw: Option<&str>) -> Option<std::time::Duration> {
    match raw {
        Some(v) => match v.trim().parse::<u64>() {
            Ok(0) => None,
            Ok(secs) => Some(std::time::Duration::from_secs(secs)),
            Err(_) => {
                eprintln!(
                    "perseus-vault: ignoring unparseable PERSEUS_VAULT_IDLE_TIMEOUT_SECS value {:?} — idle watchdog disabled",
                    v
                );
                None
            }
        },
        None => None,
    }
}

/// Run the MCP server loop: read JSON-RPC from stdin, write responses to stdout.
///
/// Takes `Arc<Database>` (#402) so main.rs can hand the SAME pooled Database
/// to the web dashboard / gRPC surfaces instead of each opening a second
/// `Database` (a second 16-conn pool) on the same file.
pub fn run_server(db: std::sync::Arc<Database>) {
    // Capture the baseline parent PID immediately, before anything can reparent
    // us. is_orphaned_by_ppid() compares against this so a process legitimately
    // born under a PID-1 container entrypoint is not mistaken for an orphan (#547
    // follow-up: fixes the demo-container crash loop).
    record_initial_ppid();

    let mut stdout = std::io::stdout();
    let state = MCPState::new();

    // Idle watchdog — OPT-IN since #748 (PERSEUS_VAULT_IDLE_TIMEOUT_SECS, default off).
    //
    // The original #57228 guard treated 600s of silence as proof of orphanhood.
    // That proxy is wrong on every platform without a real parent-death signal
    // (macOS/Windows had none): Claude Desktop goes quiet for long stretches in
    // normal use and — critically — never respawns a server that exits, so the
    // timer was silently killing healthy sessions and forcing a full app
    // restart. True orphans are now caught deterministically by parent-death
    // detection (PR_SET_PDEATHSIG on Linux; the ppid watcher thread below on
    // all Unix), so the flat timer remains only as an opt-in for hosts that
    // leak a child's stdin write-end while STAYING ALIVE (the actual #57228
    // Hermes-worker topology) — those hosts set PERSEUS_VAULT_IDLE_TIMEOUT_SECS when
    // spawning. EOF on stdin (well-behaved host shutdown) exits regardless.
    let idle_timeout: Option<std::time::Duration> =
        parse_idle_timeout(std::env::var("PERSEUS_VAULT_IDLE_TIMEOUT_SECS").ok().as_deref());

    // Read stdin on a dedicated thread so the main loop can time out on silence.
    let (tx, rx) = std::sync::mpsc::channel::<std::io::Result<String>>();
    std::thread::spawn(move || {
        let stdin = std::io::stdin();
        let reader = BufReader::new(stdin.lock());
        for line in reader.lines() {
            // If the main loop has exited (idle timeout), the receiver is dropped
            // and send() errors — stop reading and let this thread end.
            if tx.send(line).is_err() {
                break;
            }
        }
        // EOF: closing tx makes the main loop's recv return Disconnected.
    });

    eprintln!("perseus-vault: MCP server ready");

    // --- Deterministic parent-death detection (Linux, fixes #547) ---
    //
    // PR_SET_PDEATHSIG makes the kernel send SIGTERM to this process the
    // instant its parent dies, regardless of pipe/traffic state. This closes
    // the race that defeats the idle watchdog: a leaked write-end of stdin
    // held by a still-live sibling keeps recv_timeout() marginally fed so
    // the idle timer never elapses, yet the spawning parent is already dead.
    //
    // After setting the signal we re-check is_orphaned_by_ppid() immediately:
    // if the parent died in the window between fork() and prctl() we exit now
    // rather than blocking forever (the signal delivery already happened
    // before the prctl so we would never receive it). This compares the live
    // ppid against the baseline captured at start, so a server born directly
    // under a PID-1 container entrypoint is NOT treated as orphaned.
    #[cfg(target_os = "linux")]
    {
        if !crate::live_update::handoff_child() {
            unsafe {
                // PR_SET_PDEATHSIG = 1; SIGTERM = 15.  Using the raw constants
                // avoids pulling in the full `nix` crate just for this call.
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM, 0, 0, 0);
            }
            if is_orphaned_by_ppid() {
                eprintln!("perseus-vault: parent already dead at server start — exiting (orphan-reap race guard, #547)");
                return;
            }

        }
    }

    // --- Parent-death watcher thread (Unix + Windows; primary orphan signal) ---
    //
    // The per-request ppid poll in the loop below can only run WHEN TRAFFIC
    // ARRIVES — an orphaned server sitting idle in recv() would never notice
    // its parent died. This thread polls the same orphan check on a 5s timer
    // and exits promptly, so abandonment detection works with zero traffic.
    // On Linux it backs up PR_SET_PDEATHSIG (which seccomp/kernels can filter);
    // on macOS it is the ONLY parent-death signal (reparent-to-launchd poll,
    // #748); on Windows it polls OpenProcess + creation-time on the recorded
    // parent PID (#751). It is what lets the idle watchdog default to OFF: the
    // server dies iff its host actually died, never merely because the host
    // went quiet (Claude Desktop neither pings nor respawns).
    #[cfg(any(unix, windows))]
    {
        std::thread::spawn(|| loop {
            std::thread::sleep(std::time::Duration::from_secs(5));
            if is_orphaned_by_ppid() {
                eprintln!(
                    "perseus-vault: parent process died — exiting orphaned stdio server (orphan watcher, #547/#748)"
                );
                // process::exit from the watcher: the main loop is blocked in
                // recv() and cannot be woken without traffic. SQLite is in WAL
                // mode, so skipping destructor-driven pool shutdown is safe.
                std::process::exit(0);
            }
        });
    }

    loop {
        let line = match idle_timeout {
            Some(timeout) => match rx.recv_timeout(timeout) {
                Ok(l) => l,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    eprintln!(
                        "perseus-vault: no client activity for {}s — exiting idle stdio server (orphan-leak guard, #57228)",
                        timeout.as_secs()
                    );
                    break;
                }
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
            },
            None => match rx.recv() {
                Ok(l) => l,
                Err(_) => break,
            },
        };

        let line = match line {
            Ok(l) => l,
            Err(e) => {
                eprintln!("perseus-vault: stdin read error: {}", e);
                break;
            }
        };

        if line.trim().is_empty() {
            continue;
        }

        // Ppid poll: if we have been reparented to init our spawning parent is
        // gone. PR_SET_PDEATHSIG above handles the common case, but on Linux
        // kernels that ignore the signal or on non-Linux platforms this is the
        // deterministic fallback. One getppid() syscall per request is negligible.
        if is_orphaned_by_ppid() {
            eprintln!("perseus-vault: ppid == 1 detected — parent died, exiting (orphan-reap, #547)");
            break;
        }

        let request: JsonRpcRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("perseus-vault: JSON parse error: {} in line: {}", e, line);
                let error_response = json!({
                    "jsonrpc": "2.0",
                    "id": Value::Null,
                    "error": {"code": -32700, "message": format!("Parse error: {}", e)}
                });
                let _ = writeln!(stdout, "{}", error_response);
                let _ = stdout.flush();
                continue;
            }
        };

        let response = handle_request(&request, &state, &db);

        if let Some(resp) = response {
            let resp_str = serde_json::to_string(&resp).unwrap_or_else(|_| {
                json!({
                    "jsonrpc": "2.0",
                    "id": request.id,
                    "error": {"code": -32603, "message": "Internal error: serialization failed"}
                })
                .to_string()
            });
            let _ = writeln!(stdout, "{}", resp_str);
            let _ = stdout.flush();

            // #858: fd-preserving live-update handoff. The response above is
            // flushed FIRST so the client receives the report before the
            // process image switches; the child inherits the same stdin/stdout
            // pipes, so the MCP session continues uninterrupted.
            if crate::live_update::handoff_pending() {
                if let Err(e) = crate::live_update::perform_handoff() {
                    eprintln!("perseus-vault: handoff spawn failed: {e}");
                }
                // Exit regardless: on success the child owns the session; on
                // failure the client sees EOF and can restart cleanly (the
                // stale gate keeps serving loud errors until then).
                std::process::exit(0);
            }
        }
    }
}

pub fn handle_request(
    req: &JsonRpcRequest,
    state: &MCPState,
    db: &Database,
) -> Option<JsonRpcResponse> {
    let id = req.id.clone();

    if req.jsonrpc != "2.0" {
        return Some(error_response(
            id,
            -32600,
            "Invalid Request: jsonrpc must be \"2.0\"",
        ));
    }

    match req.method.as_str() {
        "initialize" => {
            let response = JsonRpcResponse {
                jsonrpc: "2.0".to_string(),
                id,
                result: Some(json!({
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {
                        // Tracks Cargo.toml's package name automatically, so a
                        // future rename doesn't leave this handshake reporting
                        // stale branding (it was hardcoded through the earlier
                        // product renames).
                        "name": env!("CARGO_PKG_NAME"),
                        "version": env!("CARGO_PKG_VERSION")
                    },
                    "capabilities": {
                        "tools": {
                            "listChanged": false
                        }
                    }
                })),
                error: None,
            };
            // #684: capture the client's identity from the handshake so
            // subsequent tool calls can be attributed/visibility-scoped. MCP
            // clientInfo.name (e.g. the agent's name); sanitized to a bounded
            // token. Absent clientInfo → stays empty → unscoped.
            if let Some(name) = req
                .params
                .as_ref()
                .and_then(|p| p.get("clientInfo"))
                .and_then(|c| c.get("name"))
                .and_then(|n| n.as_str())
            {
                let sanitized: String = name
                    .trim()
                    .chars()
                    .filter(|c| c.is_alphanumeric() || matches!(c, '-' | '_' | '.' | ':'))
                    .take(128)
                    .collect();
                if let Ok(mut slot) = state.session_agent_id.write() {
                    *slot = sanitized;
                }
            }
            state.initialized.store(true, std::sync::atomic::Ordering::Relaxed);
            Some(response)
        }

        "notifications/initialized" => {
            // Notification — no response
            None
        }

        "tools/list" => {
            if !state.initialized.load(std::sync::atomic::Ordering::Relaxed) {
                return Some(error_response(id, -32002, "Not initialized"));
            }
            Some(list_tools(id))
        }

        "tools/call" => {
            if !state.initialized.load(std::sync::atomic::Ordering::Relaxed) {
                return Some(error_response(id, -32002, "Not initialized"));
            }

            let params = match &req.params {
                Some(p) => p,
                None => return Some(error_response(id, -32602, "Missing params")),
            };

            let tool_name = match params.get("name").and_then(|v| v.as_str()) {
                Some(n) => n,
                None => return Some(error_response(id, -32602, "Missing tool name")),
            };

            let mut tool_args = params.get("arguments").cloned().unwrap_or(json!({}));

            // #684: stamp the captured session identity so tools that enforce
            // visibility (recall) know who is asking, without the caller having
            // to pass it. #855: the transport-captured host identity is
            // AUTHORITATIVE — a caller-supplied `requesting_agent_id`
            // (model-forged or empty) is overwritten, never trusted, so no
            // model can claim another agent's identity.
            if let Ok(sid) = state.session_agent_id.read() {
                if !sid.is_empty() {
                    if let Some(obj) = tool_args.as_object_mut() {
                        obj.insert("requesting_agent_id".to_string(), json!(*sid));
                    }
                }
            }

            // #879: enforce profile <-> workspace bindings at the tool
            // boundary. The transport-stamped requesting_agent_id is the
            // Hermes profile name; the binding registry is vault-authoritative.
            // Mutations on the scoped surface deny cross-workspace targets,
            // read_only bindings, and quarantined/unbound bindings. Reads
            // deny cross-workspace targets when the caller names a workspace.
            // Unbound profiles keep the legacy unscoped behavior (binding is
            // an opt-in governance surface).
            {
                const SCOPE_MUTATION_TOOLS: &[&str] = &[
                    "perseus_vault_remember",
                    "perseus_vault_reject_value",
                    "perseus_vault_forget",
                    "perseus_vault_link",
                    "perseus_vault_unlink",
                    "perseus_vault_supersede",
                    "perseus_vault_state_set",
                    "perseus_vault_embed",
                    "perseus_vault_artifact_register",
                    "perseus_vault_learned_artifact_register",
                    "perseus_vault_expire",
                    "perseus_vault_redact",
                    "perseus_vault_erase",
                    "perseus_vault_correct",
                    "perseus_vault_follow",
                ];
                const SCOPE_READ_TOOLS: &[&str] = &[
                    "perseus_vault_recall",
                    "perseus_vault_recall_batch",
                    "perseus_vault_recall_layer",
                    "perseus_vault_scan",
                    "perseus_vault_context",
                    "perseus_vault_ask",
                    "perseus_vault_artifact_manifest",
                    "perseus_vault_artifact_excerpt",
                    "perseus_vault_artifact_verify_value",
                ];
                let profile = tool_args
                    .get("requesting_agent_id")
                    .and_then(|v| v.as_str());
                let ws = tool_args.get("workspace_hash").and_then(|v| v.as_str());
                let denied = if SCOPE_MUTATION_TOOLS.contains(&tool_name) {
                    db.enforce_workspace_binding(profile, ws, true)
                        .err()
                        .map(|e| e.to_string())
                } else if SCOPE_READ_TOOLS.contains(&tool_name) {
                    db.enforce_workspace_binding(profile, ws, false)
                        .err()
                        .map(|e| e.to_string())
                } else {
                    None
                };
                if let Some(msg) = denied {
                    return Some(JsonRpcResponse {
                        jsonrpc: "2.0".to_string(),
                        id,
                        result: Some(json!({
                            "content": [{ "type": "text", "text": msg }],
                            "isError": true,
                        })),
                        error: None,
                    });
                }
            }

            // v23 (Chancery cross-ref, #6): extract the chancery writ ID from
            // `_meta.chancery/lease` on the tools/call params envelope. When
            // Chancery wraps an MCP server it stamps every request with this so
            // the vault can record the writ in its journal audit trail. Set on a
            // thread-local so `db.journal()` picks it up without threading it
            // through every handler.
            let chancery_writ_id = params
                .get("_meta")
                .and_then(|m| m.get("chancery/lease"))
                .and_then(|v| v.as_str())
                .unwrap_or("");
            crate::db::set_chancery_writ_id(chancery_writ_id);

            let result_text = call_tool(tool_name, db, tool_args, id.clone());

            // Try to parse the result as JSON for structuredContent
            let structured: Option<serde_json::Value> = serde_json::from_str(&result_text).ok();
            let mut result = json!({
                "content": [{
                    "type": "text",
                    "text": result_text
                }]
            });
            // Copy isError through, then move the parsed value into
            // structuredContent rather than deep-cloning the whole result (#208).
            if let Some(parsed) = structured {
                if let Some(is_err) = parsed.get("isError") {
                    result["isError"] = is_err.clone();
                }
                result["structuredContent"] = parsed;
            }
            Some(JsonRpcResponse {
                jsonrpc: "2.0".to_string(),
                id,
                result: Some(result),
                error: None,
            })
        }

        _ => Some(error_response(
            id,
            -32601,
            &format!("Method not found: {}", req.method),
        )),
    }
}

/// Parse-once cache of the canonical Perseus Vault tool registry. The embedded
/// literal is the single source of truth for tool schemas; no legacy aliases
/// are synthesized.
fn tool_registry_base() -> &'static Vec<serde_json::Value> {
    static BASE: OnceLock<Vec<serde_json::Value>> = OnceLock::new();
    BASE.get_or_init(|| {
        let registry = serde_json::from_str::<serde_json::Value>(
        r###"[
  {
    "name": "perseus_vault_remember",
    "description": "Store or update an entity by (category, key). Idempotent — call as often as you want, same key returns an update. NEAR-DUPLICATE MERGING (#531): a NEW key whose body is >=70% trigram-similar to an existing entity in the same category+workspace does NOT create a new entity — the write is folded into the existing one (result: action='deduped', deduped=true, merged_into=<id>). Right for conversational memory; wrong for bulk ingest of templated records, which are similar by construction and will silently collapse to a handful of rows. For bulk ingest pass skip_dedup=true (or use perseus_vault_ingest_file), and check the returned action. Prefer recall_when triggers (retrieve when relevant) over always_on=true (inject unconditionally): the recall-first perseus_vault_context hard-caps the always-on set and warns when it overflows, so reserve always_on for genuinely identity-critical facts. Optional certainty (0.0-1.0) is used by perseus_vault_conflicts for typed-entity conflict detection. Pass derived_from (ids or {category,key} pairs of the memories you recalled) to auto-mark those sources useful — cited memories rank higher and decay slower. Use this for saving facts, decisions, architecture notes, and conventions. When encryption is enabled, body_json is encrypted at rest with AES-256-GCM.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category: 'decision', 'architecture', 'convention', 'insight', or custom"
        },
        "key": {
          "type": "string",
          "description": "Unique key within the category, e.g. 'use-postgres-16' or 'deployment-strategy'"
        },
        "body_json": {
          "type": "string",
          "description": "JSON object with the entity body — store content, summary, and any custom fields here"
        },
        "status": {
          "type": "string",
          "default": "active",
          "description": "Entity status: 'active', 'draft', 'deprecated'"
        },
        "type": {
          "type": "string",
          "default": "insight",
          "description": "Entity type: 'insight', 'architecture', 'decision', 'reference', 'convention'"
        },
        "tags": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Tags for categorization and cross-referencing"
        },
        "importance": {
          "type": "number",
          "default": 0.5,
          "description": "Initial importance 0.0–1.0 — sets the starting decay score"
        },
        "topic_path": {
          "type": "string",
          "default": "",
          "description": "Hierarchical topic path, e.g. 'architecture/database/postgres'"
        },
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Workspace scope identifier (v1.2.0). Empty = global. Entities with a workspace_hash are invisible to recall queries scoped to a different workspace."
        },
        "agent_id": {
          "type": "string",
          "default": "",
          "description": "Agent identity (v1.2.0). Tracks which agent wrote this entity. Used for agent attribution and context filtering."
        },
        "actor_kind": {
          "type": "string",
          "default": "assistant",
          "description": "Actor basis for the write (for example assistant, user, connector, or system). Missing admission stays reviewable."
        },
        "admission": {
          "type": "object",
          "description": "Hash-only admission envelope. Authoritative admission requires a validated source_event_id and matching workspace; missing or unverified evidence is stored as proposed/requires_review.",
          "properties": {
            "record_digest": {"type": "string"},
            "source_identity": {"type": "string"},
            "authorization_scope": {"type": "string"},
            "ingestion_channel": {"type": "string"},
            "workspace_hash": {"type": "string"},
            "source_trust": {"type": "string", "enum": ["untrusted", "trusted", "authoritative"]},
            "source_event_id": {"type": "string"},
            "actor_kind": {"type": "string"},
            "actor_identity": {"type": "string"},
            "validated": {"type": "boolean"},
            "valid_from_unix_ms": {"type": "integer"},
            "recorded_at_unix_ms": {"type": "integer"},
            "task_relevance_bps": {"type": "integer"},
            "instruction_bearing": {"type": "boolean"},
            "contradicts_authoritative": {"type": "boolean"}
          }
        },
        "valid_from_unix_ms": {
          "type": "integer",
          "description": "Application-time period start (#363): when the fact became TRUE IN THE WORLD, independent of when it was recorded. Set in the past for retroactive facts ('this was true last week, we just learned it') without rewriting transaction history. Default: transaction time (now). Query with perseus_vault_valid_at / perseus_vault_bitemporal / recall's valid_at filter."
        },
        "valid_to_unix_ms": {
          "type": "integer",
          "description": "Application-time period end (#363, exclusive): when the fact STOPPED being true in the world. Omit for 'still true' (unbounded). Must be greater than valid_from_unix_ms."
        },
        "skip_dedup": {
          "type": "boolean",
          "default": false,
          "description": "Opt out of near-duplicate merging for this write (#531). Set true for bulk/API ingest of templated records so every acknowledged write actually creates its key; leave false for conversational memory."
        },
        "allow_rejected": {
          "type": "boolean",
          "default": false,
          "description": "#849: deliberate trusted override of a rejected-value tombstone. Journaled as an audited override; never set automatically."
        },
        "derived_from": {
          "type": "array",
          "items": {
            "oneOf": [
              {
                "type": "string",
                "description": "Entity id of a cited source, e.g. 'mem-a1b2c3d4e5f6' (as returned by recall/remember)"
              },
              {
                "type": "object",
                "properties": {
                  "category": { "type": "string" },
                  "key": { "type": "string" }
                },
                "required": ["category", "key"],
                "description": "A cited source addressed by (category, key)"
              }
            ]
          },
          "description": "#487: the memories this write was built on (max 64). Each cited source is automatically marked useful — usefulness_count bumped, last_useful/last_accessed refreshed — so memories that actually inform later writes rank higher in recall and decay slower. Cite the entities you recalled before composing this write. Unknown citations are reported in the result, not fatal; self-citations are ignored."
        },
        "origin": {
          "type": "object",
          "properties": {
            "memory_kind": { "type": "string", "enum": ["asserted", "extracted", "inferred", "imported", "observed"] },
            "source_system": { "type": "string" },
            "capture_method": { "type": "string" },
            "observed_at_unix_ms": { "type": "integer" }
          },
          "description": "#729: optional memory-origin/provenance metadata (spec: docs/specs/memory-provenance-and-external-refs.md). Stored inside body_json under the reserved 'origin' key — surfaced by recall/get_entity via body expansion. All fields optional; unknown values are left absent, never guessed."
        },
        "external_refs": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "ref_type": { "type": "string" },
              "ref_value": { "type": "string" },
              "source_system": { "type": "string" },
              "relationship": { "type": "string", "enum": ["about", "derived_from", "mentions", "applies_to", "supersedes"] }
            },
            "required": ["ref_type", "ref_value"]
          },
          "description": "#728: optional first-class pointers to external systems of record (max 32). Stored inside body_json under the reserved 'external_refs' key; filter recall with ref_type/ref_value."
        },
        "evidence": {
          "type": "object",
          "description": "Write-time audit envelope for captures and decisions. capture_mode distinguishes snapshot, hash_only, pointer_only, not_requested, capture_failed, and legacy_unknown; a missing value is never interpreted implicitly.",
          "properties": {
            "capture_mode": { "type": "string", "enum": ["snapshot", "hash_only", "pointer_only", "not_requested", "capture_failed", "legacy_unknown"] },
            "resolved_value": { "description": "Resolved source value retained at write time when capture_mode=snapshot" },
            "content_sha256": { "type": "string", "description": "64-hex SHA-256 of the resolved value or source bytes" },
            "source_system": { "type": "string" },
            "source_ref": { "type": "string" },
            "captured_at_unix_ms": { "type": "integer" },
            "replayable": { "type": "boolean" }
          },
          "required": ["capture_mode", "captured_at_unix_ms", "replayable"]
        }
      },
      "required": [
        "category",
        "key",
        "body_json"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "description": "Entity ID, e.g. 'mem-a1b2c3d4e5f6'"
        },
        "action": {
          "type": "string",
          "description": "'created' for new entities, 'updated' for existing ones"
        },
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        },
        "derived_from": {
          "type": "object",
          "description": "Present when derived_from citations were passed: {reinforced: n, not_found: [labels]}"
        },
        "proposed": {
          "type": "boolean",
          "description": "True when the write lacks authoritative admission and must remain reviewable."
        },
        "requires_review": {
          "type": "boolean",
          "description": "Whether the stored write must be reviewed before promotion or authoritative use."
        },
        "provenance": {
          "type": "object",
          "description": "Hash-only admission/provenance state; raw prompts, bodies, credentials, and tool arguments are excluded."
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Remember Entity"
  },
  {
    "name": "perseus_vault_recall",
    "description": "Search entities with FTS5 keyword search. Words are OR'd together. Returns entities sorted by relevance with expanded content/summary fields at top level. Use this to find previously stored facts, decisions, or architecture notes. When encryption is enabled, body_json is decrypted transparently.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Search query — words are OR'd together for broad recall. An EMPTY string (\"\") is the match-all / enumeration path: it drops the keyword predicate and returns every entity in scope (respecting category/type/limit/offset), so it is the way to 'list all' a category. Wildcards are NOT globs: \"*\" is a literal FTS5 term and matches nothing — pass \"\" to enumerate, not \"*\"."
        },
        "category": {
          "type": "string",
          "description": "Filter by category, e.g. 'decision' or 'architecture'"
        },
        "type": {
          "type": "string",
          "description": "Filter by entity type, e.g. 'insight' or 'reference'"
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum number of results to return (max 1000)"
        },
        "offset": {
          "type": "integer",
          "default": 0,
          "description": "Number of results to skip for pagination"
        },
        "min_decay": {
          "type": "number",
          "default": 0.0,
          "description": "Minimum decay score threshold 0.0–1.0 — higher values return fresher results"
        },
        "topic_path": {
          "type": "string",
          "description": "Filter by topic path prefix, e.g. 'architecture/'"
        },
        "mode": {
          "type": "string",
          "default": "fts5",
          "description": "Search mode: 'fts5' (keyword), 'dense' (vector), 'hybrid' (fused via RRF), or 'fused' (TEMPR-style multi-strategy: fts5 + dense + graph + temporal with weighted RRF, token-budget truncation, and a full fused_trace, #883)",
          "enum": [
            "fts5",
            "dense",
            "hybrid",
            "fused"
          ]
        },
        "strategies": {
          "type": "array",
          "items": { "type": "string", "enum": ["fts5", "dense", "graph", "temporal"] },
          "description": "Fused mode only: strategies to engage (2-4). Omit = all four. Unknown names are rejected."
        },
        "max_tokens": {
          "type": "integer",
          "default": 0,
          "description": "Fused mode only: token-budget truncation (estimated tokens = chars/4 per body). 0 = derive from depth_budget (mid = 4096)."
        },
        "depth_budget": {
          "type": "string",
          "enum": ["low", "mid", "high"],
          "description": "Fused mode only: depth budget -> default token caps 1024 / 4096 / 16384 when max_tokens is unset."
        },
        "strategy_weights": {
          "type": "object",
          "description": "Fused mode only: per-strategy RRF weight multipliers (default 1.0 each). Arms that find nothing contribute nothing."
        },
        "rerank": {
          "type": "boolean",
          "default": false,
          "description": "Fused mode only: optional rerank stage over the fused pool (rank-calibrated dense + BM25 agreement signals; default off, latency-preserving)."
        },
        "query_time_unix_ms": {
          "type": "integer",
          "description": "Fused mode only: anchor instant for the temporal strategy (unix ms; default now). Accepts a number or numeric string."
        },
        "include_archived": {
          "type": "boolean",
          "default": false,
          "description": "Include archived (soft-deleted) entities in results"
        },
        "include_confidence": {
          "type": "boolean",
          "default": false,
          "description": "Add a normalized confidence score (0.0-1.0) to each result, rolled up from rank, trust (verified/certainty), and decay. Presentation-only; does not change ranking."
        },
        "reinforce": {
          "type": "boolean",
          "default": false,
          "description": "Opt-in reinforcement for mode='dense'/'hybrid': bump retrieval_count/last_accessed/decay on the returned hits so semantically-used memories resist decay and promote through layers. Default false keeps semantic recall side-effect-free and byte-deterministic over a frozen DB. No effect on mode='fts5', which already reinforces."
        },
        "expansion": {
          "type": "object",
          "properties": {
            "enabled": {
              "type": "boolean",
              "default": false,
              "description": "Enable stemming-based query expansion"
            },
            "n_variants": {
              "type": "integer",
              "default": 1,
              "description": "Number of stemmed token variants to generate"
            }
          },
          "description": "Configuration for FTS5 query expansion using Porter stemming"
        },
        "preview_cap": {
          "type": "integer",
          "description": "If set, truncate body_json at N chars and append drill-down footer. Use perseus_vault_get_entity to read full body."
        },
        "content_weight": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 0,
          "description": "Additive boost for content witness — rewards entities whose body text literally contains query terms. Damped by body length. Never penalizes."
        },
        "trust_weight": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 0.15,
          "description": "Additive boost for provenance/trust (default 0.15, on by default) — verified sources rank above unverified AI drafts on the same topic. Verified entities get the full boost; unverified ones are scaled by certainty. Set 0 to disable. Never penalizes."
        },
        "diversity_halving": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "default": 1,
          "description": "Per-keyword diversity quota factor (1.0=disabled). Each distinct matched keyword gets ceil(N x halving^n) slots — first keyword N, second N/2, etc."
        },
        "recency_half_life_secs": {
          "type": "number",
          "minimum": 0,
          "description": "Time-aware ranking for mode='hybrid' (default off). When set, each fused result's score is multiplied by 0.5^(age / this), where age is seconds since the memory was created — so a memory this many seconds old keeps half its weight and recent context outranks older but similar hits. Omit for relevance-only ranking."
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are returned. Omit for no workspace filtering."
        },
        "scope_weight": {
          "type": "number",
          "minimum": 0,
          "maximum": 1,
          "description": "#485: scope as a ranking multiplier instead of a hard filter. Requires workspace_hash. Widens the workspace filter to also include GLOBAL (workspace_hash='') memories, weighted by this factor in the ranking (hybrid/dense scores multiplied; keyword mode returns current-scope hits first) — current-workspace memories outrank equally-relevant global ones, but a strong global memory still surfaces. Never exposes other workspaces' memories. Omit for the strict filter (unchanged default)."
        },
        "agent_id": {
          "type": "string",
          "description": "Agent identity filter (v1.2.0). When set, only entities with a matching agent_id are returned. Omit for no agent filtering."
        },
        "epistemic_state": {
          "type": "string",
          "enum": ["candidate", "verified", "corroborated", "rejected", "defensively_recalled"],
          "description": "#880: epistemic trust-axis filter. When set, only entities in the requested trust state are returned — 'candidate' surfaces useful-but-unverified records, 'verified'/'corroborated' restrict to established fact, 'rejected' shows reviewed-and-refused records. Omit for no trust filtering (default)."
        },
        "retrieval_profile": {
          "type": "string",
          "enum": ["personal", "agent", "shared"],
          "description": "#784 serving posture. personal returns preference/personal classes; agent returns convention/correction/keystone classes; shared (default) returns non-personal memory in the requested workspace. Applied after visibility filtering."
        },
        "layer": {
            "type": "string",
            "description": "Filter by memory layer (world, episodic, semantic)."
        },
        "ref_type": {
          "type": "string",
          "description": "#728: post-filter hits to entities whose body external_refs carry this ref_type (exact match, e.g. 'repo', 'pull_request', 'jira_key')."
        },
        "ref_value": {
          "type": "string",
          "description": "#728: post-filter hits to entities whose body external_refs carry this ref_value. Matches exactly or as a hierarchical '/' prefix ('github:Org' matches 'github:Org/repo')."
        },
        "deadline_ms": {
          "type": "integer",
          "description": "#864: bounded recall. When set, the recall is timed; if it exceeds this many ms the response outcome.status is 'timeout' so callers know the result set may be incomplete. Results are still returned in full."
        },
        "include_outcome": {
          "type": "boolean",
          "default": false,
          "description": "#864/#873/#887: always attach the explicit 'outcome' block (status, backend health, abstention, reason). By default it is attached only when recall was degraded/partial/timeout/empty/unavailable/stale, so nominal responses stay byte-identical."
        },
        "as_of_unix_ms": {
          "type": "integer",
          "description": "#472 Temporal RAG: transaction-time instant (unix ms). Reconstruct semantic recall AS BELIEVED at this past instant — each hit's body is the version that was live at as_of_unix_ms; corrections recorded later do not leak in. Combine with valid_at for the full bi-temporal cell. Hits are stamped with is_live_version / recorded_at_unix_ms / valid_from_unix_ms / valid_to_unix_ms. Omit for today's live view. (v1: candidate generation is over the live index, so a fact fully deleted since that instant will not surface.)"
        },
        "valid_at": {
          "type": "integer",
          "description": "Valid-time instant (#363/#472, unix ms): reconstruct recall to the world-version whose application-time period [valid_from, valid_to) contains this instant — 'what was true at time T', per current (or as_of) knowledge. Rebuilds the point-in-time body from history (not just a live-row narrow) and returns hits stamped with is_live_version / recorded_at_unix_ms / valid_from/to. Combine with as_of_unix_ms for the full bi-temporal cell."
        },
        "valid_from_unix_ms": {
          "type": "integer",
          "description": "Valid-time period filter start (#363, unix ms). Pair with valid_to_unix_ms and valid_op; ignored when valid_at is set. Omit for unbounded start."
        },
        "valid_to_unix_ms": {
          "type": "integer",
          "description": "Valid-time period filter end (#363, unix ms, exclusive). Omit for unbounded end."
        },
        "valid_op": {
          "type": "string",
          "default": "overlaps",
          "enum": ["overlaps", "contains"],
          "description": "SQL:2011 period predicate for the valid-time period filter (#363): 'overlaps' (fact's valid period shares at least one instant with the queried period) or 'contains' (fact's valid period contains the whole queried period)."
        }
      },
      "required": [
        "query"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Matching entities with expanded body_json fields at top level"
        },
        "total": {
          "type": "integer",
          "description": "Number of results returned"
        },
        "variants": {
          "type": "integer",
          "description": "Number of query variants used when expansion is enabled"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Recall Entities"
  },
  {
    "name": "perseus_vault_recall_batch",
    "description": "Recall entities across a batch of queries, fusing their results server-side using reciprocal rank fusion (RRF) to merge, deduplicate, and surface the most globally relevant memories first.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "queries": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "query": {
                "type": "string",
                "description": "Search query — words are OR'd together for broad recall. An EMPTY string (\"\") is the match-all / enumeration path."
              },
              "category": {
                "type": "string",
                "description": "Filter by category, e.g. 'decision' or 'architecture'"
              },
              "type": {
                "type": "string",
                "description": "Filter by entity type, e.g. 'insight' or 'reference'"
              },
              "limit": {
                "type": "integer",
                "default": 10,
                "description": "Maximum number of results to return (max 1000)"
              },
              "offset": {
                "type": "integer",
                "default": 0,
                "description": "Number of results to skip for pagination"
              },
              "min_decay": {
                "type": "number",
                "default": 0.0,
                "description": "Minimum decay score threshold 0.0–1.0 — higher values return fresher results"
              },
              "topic_path": {
                "type": "string",
                "description": "Filter by topic path prefix, e.g. 'architecture/'"
              },
              "mode": {
                "type": "string",
                "default": "fts5",
                "description": "Search mode: 'fts5' (keyword), 'dense' (vector), or 'hybrid' (fused via RRF)",
                "enum": [
                  "fts5",
                  "dense",
                  "hybrid"
                ]
              },
              "include_archived": {
                "type": "boolean",
                "default": false,
                "description": "Include archived (soft-deleted) entities in results"
              },
              "include_confidence": {
                "type": "boolean",
                "default": false,
                "description": "Add a normalized confidence score (0.0-1.0) to each result, rolled up from rank, trust (verified/certainty), and decay. Presentation-only; does not change ranking."
              },
              "reinforce": {
                "type": "boolean",
                "default": false,
                "description": "Opt-in reinforcement for mode='dense'/'hybrid': bump retrieval_count/last_accessed/decay on the returned hits so semantically-used memories resist decay."
              },
              "preview_cap": {
                "type": "integer",
                "description": "If set, truncate body_json at N chars and append drill-down footer."
              },
              "content_weight": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0,
                "description": "Additive boost for content witness — rewards entities whose body text literally contains query terms."
              },
              "trust_weight": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 0.15,
                "description": "Additive boost for provenance/trust (default 0.15, on by default)."
              },
              "diversity_halving": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "default": 1,
                "description": "Per-keyword diversity quota factor (1.0=disabled)."
              },
              "recency_half_life_secs": {
                "type": "number",
                "minimum": 0,
                "description": "Time-aware ranking for mode='hybrid' (default off)."
              },
              "workspace_hash": {
                "type": "string",
                "description": "Workspace scope filter."
              },
              "scope_weight": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "#485: scope as a ranking multiplier instead of a hard filter."
              },
              "agent_id": {
                "type": "string",
                "description": "Agent identity filter."
              },
              "layer": {
                "type": "string",
                "description": "Filter by memory layer (world, episodic, semantic)."
              },
              "as_of_unix_ms": {
                "type": "integer",
                "description": "Temporal RAG transaction-time."
              },
              "valid_at": {
                "type": "integer",
                "description": "Valid-time instant."
              },
              "valid_from_unix_ms": {
                "type": "integer",
                "description": "Valid-time period filter start."
              },
              "valid_to_unix_ms": {
                "type": "integer",
                "description": "Valid-time period filter end."
              },
              "valid_op": {
                "type": "string",
                "default": "overlaps",
                "enum": ["overlaps", "contains"],
                "description": "SQL:2011 period predicate for valid-time period filter."
              }
            },
            "required": [
              "query"
            ]
          }
        }
      },
      "required": [
        "queries"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Matching entities fused from batch queries with expanded body_json fields at top level"
        },
        "total": {
          "type": "integer",
          "description": "Number of results returned"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Recall Entities Batch"
  },
  {
    "name": "perseus_vault_recall_layer",
    "description": "Recall entities from a specific biomimetic memory layer (world, episodic, semantic).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "layer": {
          "type": "string",
          "description": "The memory layer to recall from.",
          "enum": ["world", "episodic", "semantic"]
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum number of results to return (max 1000)."
        }
      },
      "required": ["layer"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": { "type": "object" },
          "description": "Matching entities with expanded body_json fields at top level."
        },
        "total": {
          "type": "integer",
          "description": "Number of results returned."
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    }
  },
  {
    "name": "perseus_vault_scan",
    "description": "Enumerate every entity in a category (or the whole store) deterministically, page by page (#562). This is the first-class 'list all / export / sync / reset' path: pages are keyed by immutable entity id (ascending) with a continuation cursor, so repeated calls walk the full set exactly once — unlike recall(query=\"\") pagination, whose relevance ordering mutates as recalls reinforce entities (pages can skip or repeat rows) and whose offset is capped. Call with no cursor for the first page, then pass back next_cursor until has_more is false. Read-only: scanning does not bump retrieval counts or decay. Note the recall query contract this complements: recall's query=\"\" is match-all enumeration; \"*\" is a literal FTS5 term (NOT a glob) and matches nothing.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Category to enumerate, e.g. 'decision'. Omit or pass \"\" to scan every category (no category is excluded — unlike recall, which hides high-volume categories such as 'conversation' unless explicitly requested)."
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter. When set, only entities with exactly this workspace_hash are returned (\"\" targets only global entities). Omit for unscoped."
        },
        "include_archived": {
          "type": "boolean",
          "default": false,
          "description": "Include archived (soft-deleted) entities in the scan."
        },
        "cursor": {
          "type": "string",
          "description": "Continuation cursor: the next_cursor value from the previous page. Omit for the first page."
        },
        "limit": {
          "type": "integer",
          "default": 100,
          "description": "Page size (1–1000)."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": { "type": "object" },
          "description": "Entities in this page, ordered by id ascending, with expanded body_json fields at top level."
        },
        "total": {
          "type": "integer",
          "description": "Number of entities in this page."
        },
        "has_more": {
          "type": "boolean",
          "description": "True when another page exists."
        },
        "next_cursor": {
          "type": ["string", "null"],
          "description": "Pass this as `cursor` to fetch the next page. Null on the final page."
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Scan / Enumerate Entities"
  },
  {
    "name": "perseus_vault_hygiene",
    "description": "Read-only hygiene report: surface likely low-signal memories so a startup-memory block stays dense without manual forensics. Scores every active memory by startup 'actionability' (the same signal as recall's startup mode) — concrete anchors like issue/ticket keys, #refs, paths, URLs, named systems, and decision/escalation language score high; vague, date-only titles (e.g. '2026-07-13') and very short bodies score low — and returns the worst offenders (below `threshold`) with the reasons they were flagged. Keyset-scans in pages; never bumps retrieval counts or decay. Use it to find archive/consolidate candidates before curating startup recall.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Restrict the scan to one category, e.g. 'memories'. Omit to scan every active category."
        },
        "threshold": {
          "type": "number",
          "default": 0.35,
          "description": "Actionability score (0.0–1.0) below which a memory is flagged low-signal. Lower = stricter (fewer flags)."
        },
        "scan_limit": {
          "type": "integer",
          "default": 1000,
          "description": "Maximum active memories to scan (1–10000)."
        },
        "limit": {
          "type": "integer",
          "default": 50,
          "description": "Maximum flagged rows to return, worst first (1–1000)."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "scanned": {
          "type": "integer",
          "description": "Number of active memories inspected."
        },
        "flagged_count": {
          "type": "integer",
          "description": "Total memories below the threshold (may exceed the returned rows)."
        },
        "returned": {
          "type": "integer",
          "description": "Number of flagged rows in this response."
        },
        "threshold": {
          "type": "number",
          "description": "The actionability threshold applied."
        },
        "flagged": {
          "type": "array",
          "items": { "type": "object" },
          "description": "Worst-first: {id, category, key, actionability, reasons[], retrieval_count}. reasons ∈ date_only_title | short_body | no_concrete_entities | low_actionability."
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Startup-Memory Hygiene Report"
  },
  {
    "name": "perseus_vault_promote",
    "description": "Promote a memory across the class ladder (to_category) and/or the scope ladder (to_workspace_hash) per the shared-memory promotion ladder (perseus docs/shared-memory-promotion-ladder.md §4). Creates a new entity that carries a promoted_from provenance record (source category/key/id/scope, reason, timestamp) and links the source to it with relationship='promoted_to'. The source entity is never edited or hidden — raw evidence stays reachable. Uses skip_dedup internally so the promoted copy always creates its own key even when near-identical to the source.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_category": {
          "type": "string",
          "description": "Category of the source entity to promote"
        },
        "from_key": {
          "type": "string",
          "description": "Key of the source entity to promote"
        },
        "to_category": {
          "type": "string",
          "description": "Target class/category. Omit to keep the source category."
        },
        "to_workspace_hash": {
          "type": "string",
          "description": "Target scope (workspace_hash; empty string = global). Omit to keep the source scope."
        },
        "to_key": {
          "type": "string",
          "description": "Target key. Omit to keep the source key."
        },
        "reason": {
          "type": "string",
          "description": "Why this promotion is happening (recorded in promoted_from)."
        }
      },
      "required": ["from_category", "from_key"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "promoted": { "type": "boolean" },
        "action": { "type": "string", "description": "'created' or 'updated' for the target entity" },
        "from_id": { "type": "string" },
        "to_id": { "type": "string" },
        "to_workspace_hash": { "type": "string" }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Promote Memory"
  },
  {
    "name": "perseus_vault_demote",
    "description": "Demote a governed memory exactly one rung down the durable-memory ladder. Writes a provenance-preserving copy, a demoted_to link, and an append-only demotion journal event.",
    "inputSchema": {"type":"object","properties":{
      "from_category":{"type":"string"},"from_key":{"type":"string"},"to_category":{"type":"string"},"to_key":{"type":"string"},"reason":{"type":"string"}
    },"required":["from_category","from_key","to_category"]},
    "outputSchema": {"type":"object","properties":{"demoted":{"type":"boolean"},"to_id":{"type":"string"}}},
    "annotations": {"destructiveHint": true},
    "title": "Demote Memory"
  },
  {
    "name": "perseus_vault_beliefs",
    "description": "Derived-belief overlay (#717, spec: docs/specs/belief-overlay.md): compute the current effective belief for a topic from the live entity store, with fresh local corrections always outranking stale global beliefs regardless of semantic similarity (precedence tiers are absolute, never blended).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "topic": {
          "type": "string",
          "description": "Topic or question to resolve the current effective belief for"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Optional workspace scope for the local-correction tier"
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum belief candidates to return"
        }
      },
      "required": ["topic"]
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Derived Beliefs Overlay"
  },
  {
    "name": "perseus_vault_claim_card",
    "description": "Evidence-backed claim card (#852, spec: docs/specs/claim-cards.md): a deterministic, versioned projection of one entity's claim, provenance class (source_human/fact_extracted/fact_derived/inference_agent), valid vs recorded time, confidence/support, supersession/contradiction/stale state, evidence references, a sanitized agent_projection hash-bound to the selected evidence and policy, and machine-readable reason codes (serveable / archived / scope_mismatch / revoked_access + flags). Read-only view over existing entities and links — never a second source of truth.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "entity_id": {
          "type": "string",
          "description": "ID of the entity to project as a claim card"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Caller's workspace scope for visibility enforcement (workspace-scoped entities mismatch → withheld with scope_mismatch)"
        },
        "agent_id": {
          "type": "string",
          "description": "Caller identity for visibility enforcement (private/fleet entities require the author's agent_id → else revoked_access)"
        },
        "include_evidence": {
          "type": "boolean",
          "default": true,
          "description": "Include evidence references (metadata only; raw bodies never cross)"
        },
        "include_agent_projection": {
          "type": "boolean",
          "default": true,
          "description": "Include the sanitized agent_projection block"
        }
      },
      "required": ["entity_id"]
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Evidence-Backed Claim Card"
  },
  {
    "name": "perseus_vault_semantic_search",
    "description": "Dense-only semantic search: find entities by meaning, ranked purely by embedding similarity (no keyword fallback). On by default via the bundled in-process ONNX model — zero config, zero network. A one-tool shortcut for 'find things like this'. For fused keyword+vector results use perseus_vault_recall.",
      "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Natural-language text to semantically match against stored memories"
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum number of results to return"
        },
        "category": {
          "type": "string",
          "description": "Filter by category, e.g. 'decision' or 'architecture'"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter. When set, only entities with a matching workspace_hash are returned."
        },
        "agent_id": {
          "type": "string",
          "description": "Agent identity filter. When set, only entities with a matching agent_id are returned."
        }
      },
      "required": [
        "query"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Matching entities ranked by dense embedding similarity, with expanded body_json fields at top level"
        },
        "total": {
          "type": "integer",
          "description": "Number of results returned"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Semantic Search Entities"
  },
  {
    "name": "perseus_vault_ask",
    "description": "Ask a natural language question and get a grounded answer from stored memories via RAG. Internally recalls top-k entities, assembles context, and queries the configured LLM (Ollama) for an answer with cited sources. Requires --llm-endpoint to be set. LLM request timeout defaults to 30s; set PERSEUS_VAULT_LLM_TIMEOUT_SECS for large/cold models that need longer to load (#528).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "Natural language question to answer from stored memories"
        },
        "top_k": {
          "type": "integer",
          "default": 5,
          "description": "Number of top entities to use as context (max 20)"
        },
        "as_of_unix_ms": {
          "type": "integer",
          "description": "#472 Temporal RAG: answer from the memory context AS IT WAS BELIEVED at this transaction-time instant (unix ms) — the retrieved bodies are reconstructed to the versions live at that instant, so a corrected-later fact does not leak into the past answer. Combine with valid_at_unix_ms for the full bi-temporal cell. Omit for the live view."
        },
        "valid_at_unix_ms": {
          "type": "integer",
          "description": "#472 Temporal RAG: answer from the context that was TRUE IN THE WORLD at this valid-time instant (unix ms), per current (or as_of) knowledge. Omit for the live view."
        }
      },
      "required": [
        "query"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "answer": {
          "type": "string",
          "description": "Grounded answer with cited sources"
        },
        "sources": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "key": {
                "type": "string"
              },
              "category": {
                "type": "string"
              },
              "score": {
                "type": "number"
              },
              "snippet": {
                "type": "string"
              }
            }
          },
          "description": "Cited source entities used in the answer"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true,
      "destructiveHint": false
    },
    "title": "Ask Question from Memories"
  },
  {
    "name": "perseus_vault_get_entity",
    "description": "Get an entity by ID with its full body_json content. Use after perseus_vault_recall with preview_cap to read the complete body of a truncated result. The drill-down footer embedded in preview-capped results references this tool with the entity ID to use.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "description": "Entity ID to retrieve (from recall result id field or preview cap footer)"
        }
      },
      "required": [
        "id"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "body_json": {
          "type": "string",
          "description": "Full entity body content"
        },
        "status": {
          "type": "string"
        },
        "entity_type": {
          "type": "string"
        },
        "decay_score": {
          "type": "number"
        },
        "retrieval_count": {
          "type": "integer"
        },
        "layer": {
          "type": "string"
        },
        "always_on": {
          "type": "boolean"
        },
        "certainty": {
          "type": "number"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Get Entity by ID"
  },
  {
    "name": "perseus_vault_history",
    "description": "List superseded (historical) versions of a fact (category + key), newest first. Each entry was the live fact for an interval before it was overwritten. The companion to perseus_vault_as_of: as_of returns the single version live at one instant; history returns the version trail. Paginated: returns the `limit` newest versions (default 20) starting at `offset`; `total` in the response is the FULL trail size, so total > returned means there are more pages. Returns an empty list if the fact has never been overwritten (its only version is the current live one in recall).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key within the category"
        },
        "limit": {
          "type": "integer",
          "default": 20,
          "description": "Maximum versions to return (newest first), 0-1000. Defaults to 20. 0 is count-only: returns no version bodies while `total` still reports the full trail size."
        },
        "offset": {
          "type": "integer",
          "default": 0,
          "description": "Number of newest versions to skip, for paging through a long trail."
        }
      },
      "required": [
        "category",
        "key"
      ]
    }
  },
  {
    "name": "perseus_vault_as_of",
    "description": "Transaction-time time-travel: return the version of a fact (category + key) that Perseus Vault believed at a given past instant. When a fact is overwritten, the prior version is kept in history; this returns whichever version was live at as_of_unix_ms. Use to answer 'what did we believe about X back then?' or to audit how a fact changed. For the orthogonal valid-time axis ('what was actually TRUE in the world at time T') use perseus_vault_valid_at; for both axes at once use perseus_vault_bitemporal. Returns found=false if the fact had not been recorded yet at that time. If the instant falls inside a window compacted by history retention (#398), returns an explicit marker (compacted=true, versions_compacted, digest) instead of the original — now unrecoverable — versions.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key within the category"
        },
        "as_of_unix_ms": {
          "type": "integer",
          "description": "Transaction-time instant (unix ms) to travel to"
        }
      },
      "required": [
        "category",
        "key",
        "as_of_unix_ms"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "False if the fact had not been recorded by as_of_unix_ms"
        },
        "id": {
          "type": "string"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "body_json": {
          "type": "string",
          "description": "The fact's content as it was at as_of_unix_ms"
        },
        "status": {
          "type": "string"
        },
        "entity_type": {
          "type": "string"
        },
        "as_of_unix_ms": {
          "type": "integer"
        },
        "compacted": {
          "type": "boolean",
          "description": "Present and true when the instant falls inside a retention-compacted window: the result is a tombstone marker, not a real version (#398)"
        },
        "versions_compacted": {
          "type": "integer",
          "description": "How many original versions the compacted window rolled up (#398)"
        },
        "digest": {
          "type": "string",
          "description": "Hash-chain digest folded over the evicted versions (#398)"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Time-Travel Entity Lookup"
  },
  {
    "name": "perseus_vault_valid_at",
    "description": "Valid-time (application-time) lookup: return the version of a fact (category + key) that — per CURRENT knowledge — was actually true in the world at a given instant. Orthogonal to perseus_vault_as_of: as_of answers 'what did we BELIEVE at time T' (transaction time); valid_at answers 'what WAS TRUE at time T, as we understand it now'. Facts carry a valid period [valid_from, valid_to) settable on perseus_vault_remember; a later-recorded version's claim supersedes earlier claims for the instants it covers. Returns found=false if no version's valid period contains the instant.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key within the category"
        },
        "valid_at_unix_ms": {
          "type": "integer",
          "description": "World-instant (unix ms) to evaluate: which version was actually true then"
        }
      },
      "required": [
        "category",
        "key",
        "valid_at_unix_ms"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "False if no version's valid period contains the instant"
        },
        "id": {
          "type": "string"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "body_json": {
          "type": "string",
          "description": "The fact's content as it was true at the instant"
        },
        "status": {
          "type": "string"
        },
        "entity_type": {
          "type": "string"
        },
        "valid_from_unix_ms": {
          "type": "integer",
          "description": "Start of the matched version's valid period"
        },
        "valid_to_unix_ms": {
          "type": "integer",
          "description": "End of the matched version's valid period (absent = still true)"
        },
        "recorded_at_unix_ms": {
          "type": "integer",
          "description": "Transaction time the matched version was recorded"
        },
        "is_live_version": {
          "type": "boolean",
          "description": "True when the matched version is the current live row (not superseded)"
        },
        "valid_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Valid-Time Lookup (What Was True)"
  },
  {
    "name": "perseus_vault_bitemporal",
    "description": "Full bi-temporal query (SQL:2011 SYSTEM_TIME + APPLICATION_TIME): 'as of transaction time tx_at, which version did we believe was true in the world at valid time valid_at?' Returns the exact cell of the bi-temporal rectangle — the audit-grade 'who knew what, as-of-when' question. Combines both axes: perseus_vault_as_of is this with valid_at pinned to tx_at; perseus_vault_valid_at is this with tx_at pinned to now. Retroactive and proactive updates land in the correct rectangle cell. Returns found=false if nothing recorded by tx_at was valid at valid_at.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key within the category"
        },
        "tx_at_unix_ms": {
          "type": "integer",
          "description": "Transaction-time instant (unix ms): reconstruct knowledge as of this moment"
        },
        "valid_at_unix_ms": {
          "type": "integer",
          "description": "Valid-time instant (unix ms): the world-moment being asked about"
        }
      },
      "required": [
        "category",
        "key",
        "tx_at_unix_ms",
        "valid_at_unix_ms"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "False if nothing recorded by tx_at was valid at valid_at"
        },
        "id": {
          "type": "string"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "body_json": {
          "type": "string",
          "description": "The version occupying that bi-temporal rectangle cell"
        },
        "status": {
          "type": "string"
        },
        "entity_type": {
          "type": "string"
        },
        "valid_from_unix_ms": {
          "type": "integer"
        },
        "valid_to_unix_ms": {
          "type": "integer"
        },
        "recorded_at_unix_ms": {
          "type": "integer"
        },
        "invalidated_at_unix_ms": {
          "type": "integer",
          "description": "Transaction time this version was retired (absent = live)"
        },
        "is_live_version": {
          "type": "boolean"
        },
        "tx_at_unix_ms": {
          "type": "integer"
        },
        "valid_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Bi-Temporal Rectangle Query"
  },
  {
    "name": "perseus_vault_forget",
    "description": "Soft-delete an entity by setting archived=1. The entity is hidden from queries but recoverable. Use this to clean up stale or incorrect facts without permanent data loss.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category to archive"
        },
        "key": {
          "type": "string",
          "description": "Entity key to archive"
        },
        "reason": {
          "type": "string",
          "default": "",
          "description": "Reason for archiving, logged for audit trail"
        }
      },
      "required": [
        "category",
        "key"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether the entity was found and archived"
        },
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Forget Entity (Soft-Delete)"
  },
  {
    "name": "perseus_vault_ingest",
    "description": "Sync external data connectors (GitHub issues, file watcher) into Perseus Vault. Call with no arguments to run all enabled connectors, or specify a connector name to run only that one. Use dry_run=true to preview without storing.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "connector": {
          "type": "string",
          "description": "Specific connector to run (omit for all enabled)"
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "Preview documents without storing them"
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "ingested": {
          "type": "integer",
          "description": "Number of documents ingested (or would be ingested in dry run)"
        },
        "dry_run": {
          "type": "boolean",
          "description": "Whether this was a dry run"
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Error messages from connectors that failed"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Ingest External Data"
  },
  {
    "name": "perseus_vault_ingest_file",
    "description": "Ingest a document file into memory by extracting its text LOCALLY (no cloud, no network). Plaintext/markdown/structured-text work in any build; DOCX and PDF require a binary built with --features multimodal (otherwise a clear error is returned). The extracted text is stored as a normal entity (recallable via perseus_vault_recall). category defaults to 'document', key defaults to the file name.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "path": {
          "type": "string",
          "description": "Path to the document file to ingest"
        },
        "category": {
          "type": "string",
          "description": "Entity category (default 'document')"
        },
        "key": {
          "type": "string",
          "description": "Entity key (default: the file name)"
        },
        "tags": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Optional tags"
        }
      },
      "required": [
        "path"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "description": "Stored entity id"
        },
        "action": {
          "type": "string",
          "description": "created or updated"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "chars": {
          "type": "integer",
          "description": "Characters of text extracted"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Ingest Document File"
  },
  {
    "name": "perseus_vault_artifact_register",
    "description": "Register an immutable artifact by reading a local file, hashing its exact bytes with full SHA-256, and storing a scope-bound metadata binding plus the preserved original bytes. Returns the compact deterministic manifest by default. This first slice accepts only uncompressed source bytes so retrieval anchors stay exact to the original artifact.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "path": { "type": "string", "description": "Local file path to register" },
        "mime_type": { "type": "string", "description": "Optional MIME type override; otherwise inferred from the file extension" },
        "workspace_hash": { "type": "string", "default": "", "description": "Workspace scope for the metadata binding. Omit/empty = global." },
        "agent_id": { "type": "string", "default": "", "description": "Owning agent id for visibility checks." },
        "visibility": { "type": "string", "default": "workspace", "description": "private | fleet | workspace | tenant | public" },
        "origin": { "type": "object", "description": "Optional origin/provenance metadata using the existing memory-origin contract." },
        "external_refs": { "type": "array", "items": { "type": "object" }, "description": "Optional external source anchors; pointers only, never access grants." },
        "retention_policy": { "type": "string", "description": "Optional retention policy from the existing vocabulary." },
        "representation": { "type": "object", "description": "original or derived representation metadata; derived artifacts must point at a full parent SHA-256." }
      },
      "required": ["path"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string" },
        "artifact_action": { "type": "string" },
        "binding_action": { "type": "string" },
        "manifest": { "type": "object" }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Register Immutable Artifact"
  },
  {
    "name": "perseus_vault_learned_artifact_register",
    "description": "#876 governed distillation: register a learned-memory artifact (trained weights / distilled cartridge) bound to its source entities with hash-only evidence, gated fail-closed on a COMPLETED 'learned_memory' action receipt (no receipt, no registration). Every source (category, key) in the workspace is snapshotted (entity id + normalized body digest + recorded_at) into learned_artifact_sources; physically erasing or purging a source revokes the binding (serve paths refuse revoked artifacts), superseding a source flags it stale (retraining trigger). Returns the artifact sha256, source-bindings count, and receipt-replay evidence.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "path": { "type": "string", "description": "Local file path to register (trained artifact / cartridge)" },
        "mime_type": { "type": "string", "description": "Optional MIME type override; otherwise inferred from the file extension" },
        "workspace_hash": { "type": "string", "default": "", "description": "Workspace scope for the metadata binding. Omit/empty = global." },
        "agent_id": { "type": "string", "default": "", "description": "Owning agent id for visibility checks." },
        "visibility": { "type": "string", "default": "workspace", "description": "private | fleet | workspace | tenant | public" },
        "action_id": { "type": "string", "description": "Action id of a COMPLETED 'learned_memory' action receipt (intent -> lease -> complete); the gate refuses registration without it." },
        "source_entities": { "type": "array", "items": { "type": "array", "items": { "type": "string" }, "minItems": 2, "maxItems": 2 }, "description": "(category, key) pairs the artifact was distilled from; snapshotted hash-only at registration." },
        "external_refs": { "type": "array", "items": { "type": "object" }, "description": "Optional external source anchors; pointers only, never access grants." },
        "retention_policy": { "type": "string", "description": "Optional retention policy from the existing vocabulary." },
        "derivation_version": { "type": "string", "description": "Optional distillation pipeline version tag." }
      },
      "required": ["path", "action_id", "source_entities"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string" },
        "artifact_action": { "type": "string" },
        "binding_action": { "type": "string" },
        "source_bindings_count": { "type": "integer" },
        "action_id": { "type": "string" },
        "evidence": { "type": "object" },
        "manifest": { "type": "object" }
      }
    },
    "annotations": {
      "destructiveHint": false
    },
    "title": "Register Governed Learned Artifact"
  },
  {
    "name": "perseus_vault_workspace_bind",
    "description": "#879: bind a Hermes profile to a Vault workspace (one profile <-> one workspace; re-binding switches workspace and resets lifecycle state). access_mode read_write | read_only; read_only bindings deny mutations at the tool boundary. Journaled (workspace_bound / workspace_rebound).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "profile_name": { "type": "string", "description": "Hermes profile name (must match the MCP clientInfo.name used at handshake)" },
        "workspace_hash": { "type": "string", "default": "", "description": "Workspace to bind the profile to" },
        "access_mode": { "type": "string", "default": "read_write", "enum": ["read_write", "read_only"], "description": "read_write or read_only" },
        "metadata": { "type": "object", "description": "Optional metadata (host, hermes version, actor, ...)" }
      },
      "required": ["profile_name", "workspace_hash"]
    },
    "outputSchema": { "type": "object" },
    "title": "Bind Hermes Profile to Workspace"
  },
  {
    "name": "perseus_vault_workspace_unbind",
    "description": "#879: unbind a Hermes profile from its workspace (lifecycle: active/quarantined -> unbound; row retained for audit). Journaled (workspace_unbound).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "profile_name": { "type": "string", "description": "Hermes profile name to unbind" },
        "reason": { "type": "string", "description": "Unbind reason (journaled)" }
      },
      "required": ["profile_name"]
    },
    "outputSchema": { "type": "object" },
    "title": "Unbind Hermes Profile"
  },
  {
    "name": "perseus_vault_workspace_quarantine",
    "description": "#879: operator lifecycle control — quarantine an active binding (stops all access until reactivated) or reactivate a quarantined/unbound binding. Journaled (workspace_quarantined / workspace_reactivated).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "profile_name": { "type": "string", "description": "Hermes profile name" },
        "action": { "type": "string", "default": "quarantine", "enum": ["quarantine", "reactivate"], "description": "quarantine or reactivate" },
        "reason": { "type": "string", "description": "Reason (required for quarantine, journaled)" }
      },
      "required": ["profile_name"]
    },
    "outputSchema": { "type": "object" },
    "title": "Quarantine or Reactivate Profile Binding"
  },
  {
    "name": "perseus_vault_workspace_status",
    "description": "#879: diagnostics — all profile <-> workspace bindings with lifecycle state, access mode, heartbeat, and staleness signal; distinguishes live, stale, quarantined, and unbound bindings.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "bindings": { "type": "array", "items": { "type": "object" } },
        "count": { "type": "integer" }
      }
    },
    "title": "Workspace Binding Status"
  },
  {
    "name": "perseus_vault_artifact_manifest",
    "description": "Serve the compact deterministic manifest for one artifact identity after scope + visibility filtering. When workspace_hash is omitted, only global bindings are considered — an artifact hash alone is a pointer, not an access grant.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string", "description": "Full 64-hex SHA-256 content identity" },
        "workspace_hash": { "type": "string", "description": "Exact workspace scope to read; omit for global-only." },
        "requesting_agent_id": { "type": "string", "description": "Optional requesting agent id for visibility filtering." }
      },
      "required": ["sha256"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string" },
        "byte_length": { "type": "integer" },
        "structure": { "type": "object" },
        "significant_signals": { "type": "array", "items": { "type": "string" } },
        "available_retrievals": { "type": "object" },
        "visible_binding_count": { "type": "integer" },
        "bindings": { "type": "array", "items": { "type": "object" } }
      }
    },
    "title": "Serve Artifact Manifest"
  },
  {
    "name": "perseus_vault_artifact_excerpt",
    "description": "Retrieve an exact bounded excerpt from the preserved original artifact bytes by either a half-open byte range [start,end) or an inclusive 1-indexed line range. Returns exact source anchors plus base64 bytes, and UTF-8 text when the slice decodes cleanly.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string", "description": "Full 64-hex SHA-256 content identity" },
        "workspace_hash": { "type": "string", "description": "Exact workspace scope to read; omit for global-only." },
        "requesting_agent_id": { "type": "string", "description": "Optional requesting agent id for visibility filtering." },
        "byte_start": { "type": "integer", "description": "Byte-range start offset (inclusive)" },
        "byte_end": { "type": "integer", "description": "Byte-range end offset (exclusive)" },
        "line_start": { "type": "integer", "description": "Line-range start (1-indexed, inclusive)" },
        "line_end": { "type": "integer", "description": "Line-range end (1-indexed, inclusive)" }
      },
      "required": ["sha256"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string" },
        "range": { "type": "object" },
        "content_b64": { "type": "string" },
        "content_utf8": { "type": ["string", "null"] },
        "anchors": { "type": "array", "items": { "type": "object" } },
        "why_served": { "type": "object" }
      }
    },
    "title": "Retrieve Exact Artifact Excerpt"
  },
  {
    "name": "perseus_vault_artifact_log_digest",
    "description": "Build a deterministic, evidence-preserving navigation digest over a visible UTF-8 log artifact. Repeated non-protected templates are collapsed with exact counts and first/last source anchors. Lines containing error, warn, exception, fatal, panic, denied, refused, timeout, assertion, or traceback remain verbatim. This is never an LLM summary or replacement for original bytes.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string", "description": "Full 64-hex SHA-256 content identity" },
        "workspace_hash": { "type": "string", "description": "Exact workspace scope to read; omit for global-only." },
        "requesting_agent_id": { "type": "string", "description": "Optional requesting agent id for visibility filtering." }
      },
      "required": ["sha256"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "format": { "type": "string" },
        "source_sha256": { "type": "string" },
        "config_version": { "type": "string" },
        "input_line_count": { "type": "integer" },
        "omitted_line_count": { "type": "integer" },
        "protected_line_count": { "type": "integer" },
        "sections": { "type": "array", "items": { "type": "object" } },
        "protected_lines": { "type": "array", "items": { "type": "array" } },
        "retrieval": { "type": "string" }
      }
    },
    "title": "Build Deterministic Evidence-Preserving Log Digest"
  },
  {
    "name": "perseus_vault_artifact_verify_value",
    "description": "Verify that a candidate value occurs verbatim in the preserved original artifact bytes, with bounded exact-match search only (no regex). Returns exact source anchors for each match found.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string", "description": "Full 64-hex SHA-256 content identity" },
        "workspace_hash": { "type": "string", "description": "Exact workspace scope to read; omit for global-only." },
        "requesting_agent_id": { "type": "string", "description": "Optional requesting agent id for visibility filtering." },
        "candidate": { "type": "string", "description": "Candidate value to verify: UTF-8 text by default, or base64 when encoding='base64'." },
        "encoding": { "type": "string", "default": "utf8", "description": "utf8 | base64" },
        "max_matches": { "type": "integer", "default": 5, "description": "Maximum exact-match anchors to return (bounded)." }
      },
      "required": ["sha256", "candidate"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "sha256": { "type": "string" },
        "candidate_encoding": { "type": "string" },
        "candidate_byte_length": { "type": "integer" },
        "match_count": { "type": "integer" },
        "truncated": { "type": "boolean" },
        "matches": { "type": "array", "items": { "type": "object" } },
        "why_served": { "type": "object" }
      }
    },
    "title": "Verify Candidate Against Original Artifact Bytes"
  },
  {
    "name": "perseus_vault_embed",
    "description": "Generate and store dense vector embeddings for entities via Ollama /api/embed. Supports single entity (category+key) or batch mode (batch_category). Requires --llm-endpoint to be set.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {
          "type": "string",
          "description": "Text to embed (omit to use entity body_json)"
        },
        "category": {
          "type": "string",
          "description": "Entity category for single mode"
        },
        "key": {
          "type": "string",
          "description": "Entity key for single mode"
        },
        "batch_category": {
          "type": "string",
          "description": "Embed all entities in this category lacking embeddings"
        },
        "batch_limit": {
          "type": "integer",
          "default": 100,
          "description": "Max entities in batch mode"
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "embedded": {
          "type": "integer",
          "description": "Number of entities embedded"
        },
        "dimensions": {
          "type": "integer",
          "description": "Vector dimensions"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Generate Entity Embeddings"
  },
  {
    "name": "perseus_vault_prune",
    "description": "Bulk archive entities by category, decay threshold, or age. Use dry_run=true to preview without archiving. Useful for cleaning stale or low-quality memories. With scope='history' (#398) it instead evicts old superseded versions from entity_history under the given (or env-configured PERSEUS_VAULT_HISTORY_*) bounds, rolling each evicted run into a compaction tombstone; dry_run reports the rows and bytes that would be evicted.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Archive entities in this category"
        },
        "min_decay": {
          "type": "number",
          "description": "Archive entities with decay_score below this threshold"
        },
        "older_than_days": {
          "type": "integer",
          "description": "Archive entities older than this many days"
        },
        "limit": {
          "type": "integer",
          "default": 100,
          "description": "Max entities to prune (0 = unlimited)"
        },
        "scope": {
          "type": "string",
          "enum": ["entities", "history"],
          "description": "'history' prunes superseded versions from entity_history under retention bounds instead of archiving live entities (#398)"
        },
        "max_age_days": {
          "type": "integer",
          "description": "scope='history': evict versions invalidated more than this many days ago (overrides PERSEUS_VAULT_HISTORY_MAX_AGE_DAYS)"
        },
        "max_versions_per_key": {
          "type": "integer",
          "description": "scope='history': keep at most this many stored versions per key, oldest evicted first (overrides PERSEUS_VAULT_HISTORY_MAX_VERSIONS_PER_KEY)"
        },
        "max_bytes": {
          "type": "integer",
          "description": "scope='history': global stored-history byte budget, globally-oldest evicted first (overrides PERSEUS_VAULT_HISTORY_MAX_BYTES)"
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "Preview without archiving/evicting"
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "archived": {
          "type": "integer"
        },
        "examined": {
          "type": "integer"
        },
        "dry_run": {
          "type": "boolean"
        },
        "reason": {
          "type": "string"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Prune Stale Entities"
  },
  {
    "name": "perseus_vault_link",
    "description": "Create a relationship link from one entity to another. Builds a knowledge graph that perseus_vault_traverse can walk. Use 'depends_on', 'implements', 'extends', 'references', or custom relationships.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_category": {
          "type": "string",
          "description": "Source entity category"
        },
        "from_key": {
          "type": "string",
          "description": "Source entity key"
        },
        "to_id": {
          "type": "string",
          "description": "Target entity ID (from perseus_vault_remember return value)"
        },
        "relationship": {
          "type": "string",
          "default": "related",
          "description": "Relationship type: 'depends_on', 'implements', 'extends', 'references', or custom"
        }
      },
      "required": [
        "from_category",
        "from_key",
        "to_id"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "success": {
          "type": "boolean"
        },
        "from": {
          "type": "string",
          "description": "Source as 'category/key'"
        },
        "to": {
          "type": "string",
          "description": "Target entity ID"
        },
        "relationship": {
          "type": "string",
          "description": "Relationship type set"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Link Entities"
  },
  {
    "name": "perseus_vault_unlink",
    "description": "Remove a relationship link from one entity to another. Use this to correct outdated or incorrect links in the knowledge graph.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_category": {
          "type": "string",
          "description": "Source entity category"
        },
        "from_key": {
          "type": "string",
          "description": "Source entity key"
        },
        "to_id": {
          "type": "string",
          "description": "Target entity ID to unlink"
        }
      },
      "required": [
        "from_category",
        "from_key",
        "to_id"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "success": {
          "type": "boolean"
        },
        "from": {
          "type": "string",
          "description": "Source as 'category/key'"
        },
        "to": {
          "type": "string",
          "description": "Target entity ID"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Unlink Entities"
  },
  {
    "name": "perseus_vault_journal",
    "description": "Append a structured decision/observation log entry. Uses evaluated/acted/forward pattern: what was considered, what was done, and what happens next. Essential for audit trails and timeline reconstruction.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "event_type": {
          "type": "string",
          "default": "decision",
          "description": "Event type: 'decision', 'observation', 'action', 'error'"
        },
        "evaluated": {
          "type": "object",
          "description": "What was evaluated: options considered, context, constraints"
        },
        "acted": {
          "type": "object",
          "description": "What action was taken and why"
        },
        "forward": {
          "type": "object",
          "description": "What the plan is going forward"
        },
        "category": {
          "type": "string",
          "description": "Related entity category for linking"
        },
        "key": {
          "type": "string",
          "description": "Related entity key for linking"
        },
        "entity_id": {
          "type": "string",
          "description": "Related entity ID for linking"
        },
        "agent_id": {
          "type": "string",
          "default": "",
          "description": "Agent identity (v1.2.0). Records which agent created this journal event."
        },
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Explicit workspace attribution for the journal event; empty string denotes the global partition."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "id": {
          "type": "string",
          "description": "Journal event ID"
        },
        "event_type": {
          "type": "string",
          "description": "Event type recorded"
        },
        "created_at_unix_ms": {
          "type": "integer",
          "description": "Creation timestamp in unix milliseconds"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Append Journal Entry"
  },
  {
    "name": "perseus_vault_check_failure_pattern",
    "description": "Deja-vu guard (#521): call BEFORE retrying a failed command or committing to an approach. Checks the action against workspace-scoped prior failures in both the journal (error events and failure-marked acted/forward payloads) and the entity store (failure/pitfall/root-cause memories), ranked by similarity, recency, and trust. Returns matching prior failures with the recorded cause and resolution, a deja_vu flag, and a one-line warning when the action was already tried and failed. Read-only: never bumps retrieval counts or decay. Record failures via perseus_vault_journal (event_type 'error') or perseus_vault_remember so the guard can find them.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "description": "The command line or approach description you are about to (re)try, e.g. 'cargo build --no-default-features' or 'parse the changelog with a regex'"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Required workspace scope. Use an empty string only for the explicit global partition; other workspaces are never searched."
        },
        "limit": {
          "type": "integer",
          "default": 5,
          "description": "Maximum number of matches to return (1-50)"
        }
      },
      "required": [
        "action",
        "workspace_hash"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "matches": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "workspace_hash": {
                "type": "string",
                "description": "Stored workspace scope of the matched failure; empty means the global partition."
              }
            }
          },
          "description": "Prior failures matching the action, best first. Each includes source, ref, workspace_hash, when (unix ms), what_failed, cause, resolution, and score."
        },
        "deja_vu": {
          "type": "boolean",
          "description": "True when at least one prior recorded failure matches the action"
        },
        "warning": {
          "type": "string",
          "description": "One-line agent-actionable deja-vu warning (present only when matches exist)"
        },
        "message": {
          "type": "string",
          "description": "Unambiguous empty state ('no prior failures recorded matching this action') when nothing matches"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Check Failure Pattern (Deja-Vu Guard)"
  },
  {
    "name": "perseus_vault_timeline",
    "description": "Query workspace-scoped journal events by time range with optional filters for event type, category, or entity. Use this to reconstruct the decision history and understand what happened when.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "workspace_hash": {
          "type": "string",
          "description": "Required workspace scope. Use an empty string only for the explicit global partition."
        },
        "from_ms": {
          "type": "integer",
          "description": "Start time boundary in unix milliseconds"
        },
        "to_ms": {
          "type": "integer",
          "description": "End time boundary in unix milliseconds"
        },
        "event_type": {
          "type": "string",
          "description": "Filter by event type: 'decision', 'observation', 'action', 'error'"
        },
        "category": {
          "type": "string",
          "description": "Filter by related entity category"
        },
        "entity_id": {
          "type": "string",
          "description": "Filter by related entity ID"
        },
        "limit": {
          "type": "integer",
          "default": 50,
          "description": "Maximum number of events to return (max 1000)"
        },
        "offset": {
          "type": "integer",
          "default": 0,
          "description": "Number of events to skip for pagination"
        }
      },
      "required": [
        "workspace_hash"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": {
                "type": "string"
              },
              "event_type": {
                "type": "string"
              },
              "workspace_hash": {
                "type": "string",
                "description": "Stored workspace attribution; empty string denotes the global partition."
              }
            }
          },
          "description": "Journal events matching the query"
        },
        "total": {
          "type": "integer",
          "description": "Number of events returned"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Query Journal Timeline"
  },
  {
    "name": "perseus_vault_state_set",
    "description": "Set a key-value state entry with optional TTL for auto-expiration. Use this for session state, temporary flags, or configuration values that should expire after a set time.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "key": {
          "type": "string",
          "description": "State key — unique identifier for this state entry"
        },
        "value_json": {
          "type": "string",
          "description": "JSON value to store"
        },
        "ttl_seconds": {
          "type": "integer",
          "description": "Time-to-live in seconds. Entry auto-expires and returns null after this duration. Omit for permanent state."
        }
      },
      "required": [
        "key",
        "value_json"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "key": {
          "type": "string",
          "description": "State key set"
        },
        "ttl_seconds": {
          "type": "integer",
          "description": "TTL that was set, if any"
        },
        "expires_at_unix_ms": {
          "type": "integer",
          "description": "Expiration timestamp in unix milliseconds, if TTL was set"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Set State Entry"
  },
  {
    "name": "perseus_vault_state_get",
    "description": "Get a state value by key. Returns null if the key has expired or doesn't exist. Use this instead of perseus_vault_recall for transient session state that doesn't need FTS5 search.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "key": {
          "type": "string",
          "description": "State key to retrieve"
        }
      },
      "required": [
        "key"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether the key exists and hasn't expired"
        },
        "key": {
          "type": "string",
          "description": "State key requested"
        },
        "value": {
          "type": "string",
          "description": "JSON value if found"
        },
        "expires_at_unix_ms": {
          "type": "integer",
          "description": "Expiration timestamp if TTL was set"
        },
        "created_at_unix_ms": {
          "type": "integer",
          "description": "Creation timestamp"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Get State Entry"
  },
  {
    "name": "perseus_vault_state_delete",
    "description": "Delete a state entry by key. Permanent removal — unlike perseus_vault_forget which is a soft-delete. Use this to clean up expired or unused state entries.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "key": {
          "type": "string",
          "description": "State key to permanently delete"
        }
      },
      "required": [
        "key"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether the key existed and was deleted"
        },
        "key": {
          "type": "string",
          "description": "Key that was deleted"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Delete State Entry"
  },
  {
    "name": "perseus_vault_state_list",
    "description": "List all state keys, optionally filtered by a key prefix. Use this to discover what state entries exist without knowing exact keys ahead of time.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "prefix": {
          "type": "string",
          "default": "",
          "description": "Only return keys that start with this prefix"
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "keys": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Matching state keys"
        },
        "total": {
          "type": "integer",
          "description": "Number of keys returned"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "List State Entries"
  },
  {
    "name": "perseus_vault_health",
    "description": "Cheap readiness probe for the vault server and its SQLite database. Returns healthy/unhealthy plus a readiness snapshot: `ready` (DB answers AND at least one active memory), `active_memories`, `embedded_memories`, `semantic_recall` (available|no_coverage|disabled), `db_path`, and `warnings[]` with likely causes. Call this before a recall-heavy workflow, or when recall unexpectedly returns empty, to tell an empty/degraded store apart from a broken MCP child. Use perseus_vault_stats for detailed statistics.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "status": {
          "type": "string",
          "enum": [
            "healthy",
            "unhealthy"
          ],
          "description": "Server health status (healthy iff the DB responds)"
        },
        "db_path": {
          "type": "string",
          "description": "Absolute path of the SQLite file this server is bound to (#671)"
        },
        "ready": {
          "type": "boolean",
          "description": "True when the DB responds AND the store has at least one active memory — i.e. recall can return non-empty results"
        },
        "active_memories": {
          "type": "integer",
          "description": "Count of non-archived memories (the set recall reads)"
        },
        "embedded_memories": {
          "type": "integer",
          "description": "Count of active memories carrying a dense embedding"
        },
        "semantic_recall": {
          "type": "string",
          "enum": [
            "available",
            "no_coverage",
            "disabled"
          ],
          "description": "Dense/hybrid posture: available (backend on, coverage present), no_coverage (backend on, nothing embedded), or disabled (keyword-only build/config)"
        },
        "warnings": {
          "type": "array",
          "items": { "type": "string" },
          "description": "Likely-cause messages for degraded/empty states; empty when nominal"
        },
        "binary_stale": {
          "type": "boolean",
          "description": "True when the running binary was replaced on disk since this process started (#858): results come from a stale image — call perseus_vault_handoff_restart or restart the session"
        },
        "binary_path": {
          "type": "string",
          "description": "Absolute path of the running binary (empty when undeterminable)"
        },
        "pid": {
          "type": "integer",
          "description": "PID of the running server process"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Check Health"
  },
  {
    "name": "perseus_vault_handoff_restart",
    "description": "Live-update / reconnect for long-lived stdio sessions (#858). When the perseus-vault binary was rebuilt or replaced on disk mid-session, the running process image is stale: every other tool refuses loudly (isError) until the session is restarted — or this tool hot-swaps the process on the SAME stdio connection. States: binary unchanged -> no_handoff_needed (identity report); stale + dry_run -> dry_run (what would happen); stale without confirm -> confirm_required; stale + confirm:true -> the replacement binary is spawned on this session's stdio and the old process exits immediately after this response — the MCP session continues uninterrupted in the new process image. Do not pipeline requests during the handoff.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dry_run": {
          "type": "boolean",
          "description": "Report what would happen without performing the handoff (default false)"
        },
        "confirm": {
          "type": "boolean",
          "description": "Required to actually perform the hot-swap when the binary is stale (default false)"
        }
      }
    }
  },
  {
    "name": "perseus_vault_quality_telemetry",

    "description": "Machine-readable memory-quality telemetry: contradiction rate, supersession lag, class/layer distribution, and promotion-flow proxy.",
    "inputSchema": {
      "type": "object",
      "properties": {"category": {"type": "string", "description": "Category for contradiction scan (default general)."}}
    }
  },
  {
    "name": "perseus_vault_retrieval_telemetry",

    "description": "Read-only retrieval telemetry: concentration (top slot/token shares, Herfindahl), repeated-serving rate over a turn/second window, diversity (sources, source classes, Simpson), cross-arm contamination (per-arm audits, delivered-set validation, optional arm-level probe), low-trust query-class fan-out, and diversity/cooldown displacement. Reports include denominators, scope, retrieval profile, source class, and the versioned artifact hash; empty/degraded/unavailable states are separated from zero concentration.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "window_turns": {"type": "integer", "description": "Window in serving batches (distinct recalls). Default: none (window_secs wins)."},
        "window_secs": {"type": "integer", "description": "Window in seconds (default 86400)."},
        "profile": {"type": "string", "description": "Scope: only events recorded under this profile."},
        "workspace_hash": {"type": "string", "description": "Scope: only events from this workspace."},
        "probe_query": {"type": "string", "description": "Optional contamination probe: run arm-level SQL deltas for this query and report blocked re-entry per arm."},
        "probe_mode": {"type": "string", "description": "Probe mode: lexical|dense|hybrid|fused|graph|proactive (default lexical)."}
      }
    }
  },
  {
    "name": "perseus_vault_stats",
    "description": "Return comprehensive database statistics: entity counts by category, type, and decay layer; journal event count; state entry count; database file size; date range of stored data; and history growth (stored version rows, bytes, and the top-10 keys by version count — #398).",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "total_entities": {
          "type": "integer",
          "description": "Total entities in the database"
        },
        "by_category": {
          "type": "object",
          "description": "Entity counts grouped by category"
        },
        "by_type": {
          "type": "object",
          "description": "Entity counts grouped by type"
        },
        "by_layer": {
          "type": "object",
          "description": "Entity counts grouped by decay layer (buffer/working/core)"
        },
        "total_journal_events": {
          "type": "integer",
          "description": "Total journal events recorded"
        },
        "total_state_entries": {
          "type": "integer",
          "description": "Total state entries (including expired)"
        },
        "db_file_size_bytes": {
          "type": "integer",
          "description": "Database file size on disk in bytes"
        },
        "oldest_unix_ms": {
          "type": ["integer", "null"],
          "description": "Oldest entity creation timestamp, or null when the database has no entities"
        },
        "newest_unix_ms": {
          "type": ["integer", "null"],
          "description": "Newest entity creation timestamp, or null when the database has no entities"
        },
        "total_history_rows": {
          "type": "integer",
          "description": "Superseded versions stored in entity_history, incl. compaction tombstones (#398)"
        },
        "history_bytes": {
          "type": "integer",
          "description": "Stored history body bytes — SUM(LENGTH(body_json)); row/index overhead excluded (#398)"
        },
        "top_history_keys": {
          "type": "array",
          "description": "Top-10 (category, key) pairs by stored version count: [{category, key, versions, bytes}] (#398)"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Get Database Statistics"
  },
  {
    "name": "perseus_vault_compact",
    "description": "Archive entities whose decay score has fallen below a threshold. Supports dry-run mode to preview without making changes. Run periodically or threshold-triggered to keep the database focused on active, high-value memories.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "min_decay": {
          "type": "number",
          "default": 0.1,
          "description": "Decay threshold — entities with decay score below this are archived"
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "If true, report what would be archived without making changes"
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entities_archived": {
          "type": "integer",
          "description": "Number of entities actually archived (0 in dry-run mode)"
        },
        "entities_examined": {
          "type": "integer",
          "description": "Number of entities checked"
        },
        "dry_run": {
          "type": "boolean",
          "description": "Whether this was a dry run"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Compact Low-Decay Entities"
  },
  {
    "name": "perseus_vault_purge",
    "description": "Permanently delete all archived entities and run VACUUM to reclaim disk space. This is the only operation that actually removes entities — prune/forget only soft-archive. Erasure is complete (#398): every superseded version of a purged entity is deleted from entity_history, and journal rows referencing it are redacted in place (payloads scrubbed; rows kept so the audit hash chain stays verifiable). Purged data is DELETED and NOT RECOVERABLE — this forget-then-purge path is the GDPR-style erasure mechanism. Supports dry_run=true to preview first.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "If true, report what would be deleted without making changes"
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entities_deleted": {
          "type": "integer",
          "description": "Number of archived entities permanently deleted"
        },
        "history_rows_deleted": {
          "type": "integer",
          "description": "Superseded versions of the purged entities deleted from entity_history (#398)"
        },
        "journal_rows_redacted": {
          "type": "integer",
          "description": "Journal rows referencing purged entities scrubbed in place; the audit hash chain stays valid (#398)"
        },
        "artifact_bindings_revoked": {
          "type": "integer",
          "description": "Learned-artifact bindings revoked because their source entity was physically removed; serve paths refuse revoked bindings (#876)"
        },
        "bytes_freed": {
          "type": "integer",
          "description": "Bytes reclaimed after VACUUM (0 in dry-run mode)"
        },
        "dry_run": {
          "type": "boolean",
          "description": "Whether this was a dry run"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Purge Archived Entities"
  },
  {
    "name": "perseus_vault_expire",
    "description": "Time-based lifecycle sweep (#868): transition entities whose expires_at_unix_ms has passed to status='expired'. Content, history, and searchability are RETAINED — expiry is not erasure, and recall already excludes expired rows; the sweep makes the lifecycle state explicit and observable. Idempotent and re-runnable; use dry_run=true to preview with identical predicates. Contract: docs/specs/data-boundaries-retention-lifecycle.md.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "If true, report what would be expired without making changes"
        },
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Restrict the sweep to one workspace (empty = global sweep)"
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entities_expired": {
          "type": "integer",
          "description": "Entities transitioned to status='expired'"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace the sweep was restricted to ('' = global)"
        },
        "dry_run": {
          "type": "boolean",
          "description": "Whether this was a dry run"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Expire Due Entities"
  },
  {
    "name": "perseus_vault_redact",
    "description": "Content redaction (#868): scrub the body of a workspace-scoped entity to a hash-only marker, delete its history snapshots and FTS text, and append a hash-only 'redacted' journal event. Metadata (id, key, links, provenance) is RETAINED; re-ingest of the same value stays allowed (redaction ≠ erasure). Requires an explicit workspace_hash (fail-closed, #854). Contract: docs/specs/data-boundaries-retention-lifecycle.md.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope of the entity (required — a bare category/key is ambiguous)"
        },
        "agent_id": {
          "type": "string",
          "default": "",
          "description": "Acting agent for attribution (overridden by the transport-stamped requesting_agent_id when present)"
        },
        "requesting_agent_id": {
          "type": "string",
          "description": "MCP session identity stamped by the transport; overrides agent_id"
        }
      },
      "required": [
        "category",
        "key",
        "workspace_hash"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether a matching entity was found and redacted"
        },
        "entity_id": {
          "type": "string",
          "description": "Id of the first redacted row"
        },
        "value_sha256": {
          "type": "string",
          "description": "Hash-only audit evidence: sha256 of the scrubbed body"
        },
        "history_deleted": {
          "type": "integer",
          "description": "History snapshot rows deleted (content-bearing)"
        },
        "fts_cleaned": {
          "type": "integer",
          "description": "FTS index rows removed"
        },
        "journal_event_id": {
          "type": "string",
          "description": "Id of the hash-only 'redacted' journal event"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace the redaction was scoped to"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Redact Entity Content"
  },
  {
    "name": "perseus_vault_erase",
    "description": "Physical erasure (#868/#866): permanently remove a workspace-scoped entity from the primary store AND all derived layers (FTS, history, history-FTS, community membership, inbound links, journal payloads), quarantine derived entities that cited it via evidence links, install a permanent rejection tombstone + governance mandate (re-ingest fails closed and survives primary-DB rollback), and append a hash-only 'erased' journal event. ERASED DATA IS NOT RECOVERABLE. Requires an explicit workspace_hash (fail-closed, #854). Use dry_run=true to preview exact counts. Contract: docs/specs/data-boundaries-retention-lifecycle.md.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope of the entity (required — a bare category/key is ambiguous)"
        },
        "agent_id": {
          "type": "string",
          "default": "",
          "description": "Acting agent for attribution (overridden by the transport-stamped requesting_agent_id when present)"
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "If true, report exactly what would be erased without making changes"
        },
        "requesting_agent_id": {
          "type": "string",
          "description": "MCP session identity stamped by the transport; overrides agent_id"
        }
      },
      "required": [
        "category",
        "key",
        "workspace_hash"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entities_erased": {
          "type": "integer",
          "description": "Primary rows removed"
        },
        "history_deleted": {
          "type": "integer",
          "description": "History snapshot rows removed"
        },
        "fts_cleaned": {
          "type": "integer",
          "description": "FTS index rows removed"
        },
        "community_memberships_cleaned": {
          "type": "integer",
          "description": "Community member_ids entries removed"
        },
        "community_rows_deleted": {
          "type": "integer",
          "description": "Communities deleted because the erased entity was their last member"
        },
        "inbound_links_cleaned": {
          "type": "integer",
          "description": "Inbound link edges removed from other rows"
        },
        "derived_quarantined": {
          "type": "integer",
          "description": "Derived entities citing the erased source, now quarantined pending operator review"
        },
        "journal_rows_redacted": {
          "type": "integer",
          "description": "Journal payloads scrubbed in place (audit chain preserved)"
        },
        "journal_event_id": {
          "type": "string",
          "description": "Id of the hash-only 'erased' journal event"
        },
        "value_sha256": {
          "type": "string",
          "description": "Hash-only evidence: sha256 of the erased body"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace the erasure was scoped to"
        },
        "dry_run": {
          "type": "boolean",
          "description": "Whether this was a dry run"
        },
        "governance_mandate_ok": {
          "type": "boolean",
          "description": "False if the permanent re-ingest mandate could not be installed (content is gone; guard needs operator attention)"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Erase Entity Permanently"
  },
  {
    "name": "perseus_vault_memories",

    "description": "Anthropic memory-tool compatible file interface over the vault: view / create / str_replace / insert / delete / rename on paths under /memories. Files are stored as vault entities (category 'memories', FTS-indexed, encrypted at rest, edits versioned via history), so clients built against Claude's native memory directory convention can use the vault unchanged. Use command='view' with path='/memories' to list files.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "command": {
          "type": "string",
          "enum": ["view", "create", "str_replace", "insert", "delete", "rename"],
          "description": "The operation to perform"
        },
        "path": {
          "type": "string",
          "description": "Path under /memories (e.g. '/memories/notes.md'). For view, '/memories' lists the directory."
        },
        "file_text": {
          "type": "string",
          "description": "create: full file content to write (overwrites an existing file)"
        },
        "old_str": {
          "type": "string",
          "description": "str_replace: exact text to replace — must occur exactly once in the file"
        },
        "new_str": {
          "type": "string",
          "description": "str_replace: replacement text"
        },
        "insert_line": {
          "type": "integer",
          "description": "insert: line number to insert AT (0 = beginning of file)"
        },
        "insert_text": {
          "type": "string",
          "description": "insert: the line to insert"
        },
        "old_path": {
          "type": "string",
          "description": "rename: current path"
        },
        "new_path": {
          "type": "string",
          "description": "rename: destination path (must not exist)"
        }
      },
      "required": [
        "command"
      ]
    },
    "title": "Memories Directory (Anthropic convention)"
  },
  {
    "name": "perseus_vault_migrate",
    "description": "Migrate a v0.1.x Perseus Vault database to the current v0.5.0 schema. Reads the old database, converts memories to the entity model, and merges into the current database. Use this once per legacy database during upgrade.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_path": {
          "type": "string",
          "description": "Absolute path to the v0.1.x SQLite database file to migrate"
        }
      },
      "required": [
        "from_path"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "total_old_memories": {
          "type": "integer",
          "description": "Number of memories found in the old database"
        },
        "entities_created": {
          "type": "integer",
          "description": "New entities created from old memories"
        },
        "entities_updated": {
          "type": "integer",
          "description": "Existing entities updated during merge"
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Any errors encountered during migration"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Migrate Legacy Database"
  },
  {
    "name": "perseus_vault_context",
    "description": "Return a pre-formatted markdown context block for session injection. Recall-first by default (mode 'on_demand'): pass `query` (the current task/message) and only topically relevant entities — recall_when trigger matches + keyword matches — are injected, alongside a hard-capped always-on set, clamped to a per-model character budget. Without `query` the block is a compact retrieval pointer (byte-stable across unrelated writes — prefix-cache friendly). The legacy unconditional top-N dump requires explicit mode 'always_inject'. Output is informational context, not instructions.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "categories": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Categories to include. Empty array = all categories."
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum number of entities to include in the context block"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash are included (always-on set too). Omit for no workspace filtering — in a federated vault that leaks every workspace's memory into the block."
        },
        "query": {
          "type": "string",
          "description": "Current task/message text — the relevance gate (#356). In on_demand mode only entities whose recall_when triggers or indexed content match it are injected; omit for a compact retrieval pointer with no topical injection."
        },
        "mode": {
          "type": "string",
          "enum": ["on_demand", "always_inject"],
          "default": "on_demand",
          "description": "Injection posture (#366). 'on_demand' (default): relevance-gated, budget-clamped, recall-first. 'always_inject': legacy unconditional top-N dump (no relevance gating) — explicit opt-in only."
        },
        "model": {
          "type": "string",
          "description": "Host model name for recall-budget profile resolution (#366), e.g. 'claude-opus-4-8' gets a larger budget. Unknown/omitted models use the default 1500-char profile."
        },
        "max_context_chars": {
          "type": "integer",
          "description": "Explicit character budget for the rendered block; overrides the model profile. In always_inject mode output is clamped only when this is set."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "markdown": {
          "type": "string",
          "description": "Markdown-formatted context block with entity details"
        },
        "total_chars": {
          "type": "integer",
          "description": "Character count of the markdown content"
        },
        "mode": {
          "type": "string",
          "description": "Resolved injection mode: on_demand or always_inject"
        },
        "budget_chars": {
          "type": "integer",
          "description": "Resolved character budget (0 = unclamped legacy output)"
        },
        "entities_injected": {
          "type": "integer",
          "description": "Number of entities actually injected (always-on + topical)"
        },
        "warnings": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Soft warnings: always-on cap overflow, budget truncation"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Get Context Block"
  },
  {
    "name": "perseus_vault_extract",
    "description": "Extract structured knowledge — facts, preferences, temporal events, episodes — from raw text or a stored entity, using a fully local, deterministic rule-based extractor (no cloud LLM, no embedding/API call, no network). Read-only: never writes to the store. Provide `text`, or `category` + `key` to extract from a stored entity.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {
          "type": "string",
          "description": "Raw text to extract from. If omitted, category + key of a stored entity are used."
        },
        "category": {
          "type": "string",
          "description": "Category of a stored entity to extract from (requires key)."
        },
        "key": {
          "type": "string",
          "description": "Key of a stored entity to extract from (requires category)."
        },
        "strategy": {
          "type": "string",
          "default": "rule_based",
          "enum": [
            "rule_based",
            "none"
          ],
          "description": "Extractor strategy: 'rule_based' (local heuristics) or 'none' (no-op)."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Extracted items, each an object with `kind` and `text`."
        },
        "total": {
          "type": "integer",
          "description": "Number of items extracted"
        },
        "strategy": {
          "type": "string",
          "description": "Extractor strategy used"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Extract Structured Knowledge"
  },
  {
    "name": "perseus_vault_capture",
    "description": "Opt-in in-session memory capture (#520): distill a session transcript or insight payload into durable memory entities the moment a problem is solved, instead of waiting for a scheduled harvest. Splits the payload into candidate notes (headed sections, paragraphs, or JSONL records — auto-detected), classifies each by cheap local signals into root-cause / pitfall / decision / pattern / takeaway, and writes each through the normal remember path with source='capture' (layer buffer, moderate importance). Fully local and deterministic by default — no LLM, no network; pass llm=true to distill via the configured --llm-endpoint instead (falls back to the rule-based path on any LLM failure or timeout). Anti-flood by design: near-duplicate merging stays ON (a re-captured solved problem merges into the existing memory), same-headline notes update in place, and writes are capped per invocation with dropped notes reported. Nothing runs automatically — capture happens only when this tool (or the `perseus-vault capture` CLI verb) is explicitly invoked, e.g. from an on_insight or SessionEnd lifecycle hook (run `maintain` after end-of-session capture).",
    "inputSchema": {
      "type": "object",
      "properties": {
        "text": {
          "type": "string",
          "description": "The transcript / insight payload to distill. Plain text, markdown (headed sections become separate notes), or JSONL (one note per record, using its content/text/insight/lesson/summary/message field)."
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace hash to scope the captured entities to. Omit for unscoped (global) capture."
        },
        "agent_id": {
          "type": "string",
          "description": "Agent ID recorded on the captured entities."
        },
        "max_entities": {
          "type": "integer",
          "default": 20,
          "description": "Anti-flood cap: max entities written by this invocation (1-20; callers can lower the cap, not raise it). Notes beyond the cap are dropped and counted in the result."
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "Distill and return the would-be notes without writing anything."
        },
        "llm": {
          "type": "boolean",
          "default": false,
          "description": "Distill via the configured LLM endpoint instead of the local rule-based distiller. Requires --llm-endpoint; falls back to the rule-based path on any LLM failure (the result's llm_fallback field says why)."
        },
        "consume": {
          "type": "boolean",
          "default": false,
          "description": "#563: after a SUCCESSFUL non-dry-run capture, atomically remove exactly the captured regions from source_file (temp file + rename, leaving a <source_file>.bak). Scoped to captured records only — surrounding headers/rules/pointers are left untouched. No-op under dry_run, when nothing was captured, or when source_file is unset, so it can never delete content that was not durably stored. Use it to keep a host-inlined write-buffer (e.g. an AGENTS.local.md the agent loads every turn) from accumulating already-stored blocks forever. The result reports 'consumed' (regions removed) and 'source_backup'."
        },
        "source_file": {
          "type": "string",
          "description": "#563: path to the file the payload came from. Required for consume to have anything to prune; ignored when consume is false."
        },
        "evidence": {
          "type": "object",
          "description": "Write-time evidence envelope for captured notes. Omit only for legacy_unknown compatibility.",
          "properties": {
            "capture_mode": { "type": "string", "enum": ["snapshot", "hash_only", "pointer_only", "not_requested", "capture_failed", "legacy_unknown"] },
            "resolved_value": { "description": "Resolved source value retained at capture time" },
            "content_sha256": { "type": "string" },
            "source_system": { "type": "string" },
            "source_ref": { "type": "string" },
            "captured_at_unix_ms": { "type": "integer" },
            "replayable": { "type": "boolean" }
          },
          "required": ["capture_mode", "captured_at_unix_ms", "replayable"]
        }
      },
      "required": [
        "text"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "captured": {
          "type": "integer",
          "description": "Number of notes distilled (and written, unless dry_run)"
        },
        "created": {
          "type": "integer",
          "description": "Notes that created a new entity"
        },
        "updated": {
          "type": "integer",
          "description": "Notes that updated an existing entity in place (same category+key)"
        },
        "merged": {
          "type": "integer",
          "description": "Notes merged into an existing near-duplicate entity by the trigram dedup (the capture flood control)"
        },
        "candidates": {
          "type": "integer",
          "description": "Candidate notes found in the payload before capping"
        },
        "dropped": {
          "type": "integer",
          "description": "Candidate notes dropped by the per-invocation cap"
        },
        "dry_run": {
          "type": "boolean",
          "description": "True when nothing was written"
        },
        "distiller": {
          "type": "string",
          "description": "'rule_based' or 'llm' — which distiller produced the notes"
        },
        "llm_fallback": {
          "type": "string",
          "description": "Present when llm=true was requested but the rule-based path was used; says why"
        },
        "notes": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Per-note report: {id, key, type, summary, action}"
        },
        "message": {
          "type": "string",
          "description": "Unambiguous empty state when the payload contained nothing durable"
        },
        "consumed": {
          "type": "integer",
          "description": "#563: number of captured regions removed from source_file (0 unless consume=true and the prune ran). See source_backup / consume_skipped / consume_error."
        },
        "source_backup": {
          "type": "string",
          "description": "#563: path to the pre-prune backup (<source_file>.bak) written when consumed > 0"
        }
      }
    },
    "title": "Capture Session Insights"
  },
  {
    "name": "perseus_vault_traverse",
    "description": "Walk the entity link graph starting from a given entity up to a configurable depth. Returns a chain of linked entities — useful for exploring dependencies, decision trees, and relationship graphs built via perseus_vault_link.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Starting entity category"
        },
        "key": {
          "type": "string",
          "description": "Starting entity key"
        },
        "max_depth": {
          "type": "integer",
          "default": 3,
          "description": "Maximum traversal depth from the starting entity"
        },
        "max_nodes": {
          "type": "integer",
          "default": 100,
          "description": "Maximum total nodes to traverse before stopping"
        }
      },
      "required": [
        "category",
        "key"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entity": {
          "type": "object",
          "description": "Root entity with its links"
        },
        "traversed": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Linked entities traversed from root"
        }
      },
      "required": [
        "entity",
        "traversed"
      ]
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Traverse Entity Graph"
  },
  {
    "name": "perseus_vault_score",
    "description": "Assign a quality score (0.0–1.0) to an entity. The score persists as an importance floor: decay_tick/cohere never recompute decay_score below it, so an explicitly scored memory survives idle time indefinitely (fidelity beats recency). Scores >= 0.7 also mark the entity verified. Re-score with 0.0 to clear the floor. Use this to mark entities as accurate, verified, or deprecated.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category to score"
        },
        "key": {
          "type": "string",
          "description": "Entity key to score"
        },
        "score": {
          "type": "number",
          "description": "Quality score 0.0–1.0. 1.0 = verified, 0.5 = neutral, 0.0 = low quality"
        }
      },
      "required": [
        "category",
        "key",
        "score"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether the entity was found"
        },
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        },
        "score": {
          "type": "number",
          "description": "Quality score assigned"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Score Entity Quality"
  },
  {
    "name": "perseus_vault_follow",
    "description": "Record whether an entity (typically a convention/insight/lesson) was actually FOLLOWED or MISSED by the agent — the honest follow-rate signal. Unlike retrieval_count (how often a memory is recalled), this tracks whether recall changed behavior. After enough attempts, efficacy_status flips to 'useful' or 'dead' and feeds into decay scoring so ignored rules decay out of recall while followed ones resist decay.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category"
        },
        "key": {
          "type": "string",
          "description": "Entity key"
        },
        "followed": {
          "type": "boolean",
          "description": "true if the agent's action followed/honored this entity's guidance, false if it was ignored/missed"
        },
        "context": {
          "type": "string",
          "description": "Optional description of the action/context this observation relates to"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter. When set, the stamped row is resolved with strict workspace equality — the same semantics as a workspace-scoped recall — so the signal lands on the row the agent actually saw (no global fallback). Omit to keep the unscoped deterministic pick (global '' row first, then lexicographically-first workspace)."
        }
      },
      "required": [
        "category",
        "key",
        "followed"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": {
          "type": "boolean",
          "description": "Whether the entity was found"
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "follow_count": {
          "type": "integer"
        },
        "miss_count": {
          "type": "integer"
        },
        "follow_rate": {
          "type": "number"
        },
        "efficacy_status": {
          "type": "string",
          "description": "'unverified' | 'useful' | 'dead'"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Record Follow/Miss Efficacy Signal"
  },
  {
    "name": "perseus_vault_operator_review",
    "description": "Read-only operator review queue for contradictions, stale/low-actionability facts, and deprecated supersession lag. Does not resolve or hide findings.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {"type": "string", "description": "Category to review (default general)."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "stale_threshold": {"type": "number", "minimum": 0, "maximum": 1}
      }
    }
  },
  {
    "name": "perseus_vault_conflicts",
    "description": "Detect conflicting entities in the same category — pairs with low trigram similarity in their body_json. Flags potential contradictions, duplicate-but-divergent entries, and stale-overwritten facts. Read-only by default. Opt in with resolve=true to actively invalidate the lower-certainty side of clear conflicts (superseding it into history, reversible + time-travelable via perseus_vault_as_of); that path defaults to dry_run=true so you preview first, and never resolves pairs whose certainties are within certainty_margin.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "default": "general",
          "description": "Category to scan for conflicts"
        },
        "threshold": {
          "type": "number",
          "default": 0.4,
          "description": "Similarity threshold — pairs below this are flagged as conflicts"
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Maximum number of conflicts to return / resolve"
        },
        "offset": {
          "type": "integer",
          "default": 0,
          "description": "Number of entities to skip for pagination"
        },
        "resolve": {
          "type": "boolean",
          "default": false,
          "description": "Opt-in: invalidate the lower-certainty side of clear conflicts instead of only reporting them"
        },
        "dry_run": {
          "type": "boolean",
          "default": true,
          "description": "When resolve=true, only report what would be invalidated unless set false"
        },
        "certainty_margin": {
          "type": "number",
          "default": 0.2,
          "description": "Minimum certainty gap to auto-resolve; closer pairs are skipped as ambiguous"
        }
      },
      "required": [
        "category"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "conflicts": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Conflict pairs with similarity scores (detection mode)"
        },
        "invalidations": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Winner/loser pairs invalidated or previewed (resolve mode)"
        }
      }
    },
    "annotations": {
      "readOnlyHint": false
    },
    "title": "Detect Conflicting Entities"
  },
  {
    "name": "perseus_vault_consolidate",
    "description": "Merge overlapping/duplicative entities in the same category into durable, evidence-tracked 'observations' — the mirror image of perseus_vault_conflicts, which flags dissimilar (contradictory) pairs. Groups entities whose pairwise trigram similarity meets similarity_threshold, then creates one new entity per group (category='observation') whose body carries a summary (the highest-certainty source's content), the full list of source entity ids as evidence, and a proof_count. The observation links back to each source (relationship='evidence_for') for full audit. By default sources stay live; set archive_sources=true to retire merged sources ('local dreaming' — verified or importance-floored sources are never archived), and cold_first=true to target the memories decay is about to claim. perseus_vault_autocohere runs a bounded cold_first+archive_sources pass automatically. Read-only preview with dry_run=true.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Category to scan for overlapping/duplicative entities to consolidate"
        },
        "similarity_threshold": {
          "type": "number",
          "default": 0.6,
          "description": "Trigram similarity threshold at or above which two entities are considered overlapping enough to merge"
        },
        "limit": {
          "type": "integer",
          "default": 50,
          "description": "Maximum number of observations to create"
        },
        "offset": {
          "type": "integer",
          "default": 0,
          "description": "Number of entities to skip for pagination"
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "Preview which observations would be created without writing anything"
        },
        "cold_first": {
          "type": "boolean",
          "default": false,
          "description": "Scan the COLDEST entities first (longest since last access) instead of the most recent — compress memories that are fading anyway, before decay archives them individually"
        },
        "archive_sources": {
          "type": "boolean",
          "default": false,
          "description": "Archive merged source entities after the observation is created (archive_reason names the observation; reversible). Verified or importance-floored sources are never archived."
        },
        "workspace_hash": {
          "type": "string",
          "description": "#854 workspace scope for this run. Scans, clusters, evidence links, and archive operations are strictly restricted to this workspace, and derived observations inherit it. Mutually exclusive with global=true. One of workspace_hash or global is required."
        },
        "global": {
          "type": "boolean",
          "default": false,
          "description": "#854 explicit cross-workspace mode for deliberate whole-vault consolidation. Capability-gated (memory.maintenance.global) when the caller carries a host identity. Mutually exclusive with workspace_hash."
        },
        "requesting_agent_id": {
          "type": "string",
          "default": "",
          "description": "Host identity stamped by the MCP transport. Used for global-mode authorization and stamped as author on derived observations."
        }
      },
      "required": [
        "category"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string"
        },
        "entities_examined": {
          "type": "integer",
          "description": "Number of entities scanned in this category"
        },
        "observations_created": {
          "type": "integer",
          "description": "Number of new observation entities created (or would be, in dry-run)"
        },
        "source_entities_merged": {
          "type": "integer",
          "description": "Total count of source entities folded into the created observations"
        },
        "sources_archived": {
          "type": "integer",
          "description": "Sources archived because archive_sources was set (verified/importance-floored sources are exempt)"
        },
        "dry_run": {
          "type": "boolean"
        },
        "workspace_hash": {
          "type": ["string", "null"],
          "description": "#854 effective scope: the workspace this run operated in (null when global=true)"
        },
        "global": {
          "type": "boolean",
          "description": "#854 true when this run deliberately crossed all workspaces"
        },
        "observations": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "The observations created (or previewed), each with entity_id, key, summary, source_ids, proof_count, certainty"
        }
      }
    },
    "annotations": {
      "readOnlyHint": false
    },
    "title": "Consolidate Overlapping Facts into Observations"
  },
  {
    "name": "perseus_vault_dream",
    "description": "Sleep-time LLM consolidation: batch clusters of related cold/episodic memories, reflect over each cluster via the configured LLM endpoint, and write back durable higher-order SEMANTIC insights (category='insight', semantic layer) — 'given these N memories, what stable pattern/preference/fact do they collectively imply?'. Each written insight carries evidence_for links to every source entity (full provenance), a certainty blended from LLM confidence and evidence coverage, and derivation='dream' so it is auditable and reversible. Idempotent: insights are keyed by an evidence-set hash, so re-dreaming an unchanged cluster never spawns duplicates. Contradictory sources surface as a flagged 'contradiction' insight, never a silent merge. Never fabricates: clusters that support no durable generalization are a no-op. Requires --llm-endpoint (fully local via Ollama); returns a clean error without it unless fallback_consolidate=true, which runs the non-LLM perseus_vault_consolidate pass instead. Bounded by max_entities/max_clusters budgets. Preview with dry_run=true.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Category to dream over. Omit to scan all categories (derived categories — insight, observation, synthesis, memories — are always skipped) until the entity budget is exhausted."
        },
        "topic_path": {
          "type": "string",
          "description": "Optional topic_path prefix filter applied to the scan."
        },
        "similarity_threshold": {
          "type": "number",
          "default": 0.3,
          "description": "Trigram similarity threshold for grouping RELATED memories into one cluster. Lower than consolidate's 0.6 on purpose: dreaming wants thematic neighborhoods, not near-duplicates."
        },
        "max_entities": {
          "type": "integer",
          "default": 100,
          "description": "Budget cap: maximum entities scanned per run (across categories)."
        },
        "max_clusters": {
          "type": "integer",
          "default": 5,
          "description": "Budget cap: maximum clusters sent to the LLM per run (= max LLM calls)."
        },
        "min_cluster_size": {
          "type": "integer",
          "default": 2,
          "description": "Minimum memories a cluster needs before it is worth dreaming over."
        },
        "dry_run": {
          "type": "boolean",
          "default": false,
          "description": "Report candidate insights and their evidence sets without writing anything."
        },
        "cold_first": {
          "type": "boolean",
          "default": true,
          "description": "Scan the COLDEST entities first (longest since last access) — consolidate fading memories into durable semantic insights before decay claims them."
        },
        "archive_sources": {
          "type": "boolean",
          "default": false,
          "description": "Archive source entities once an insight citing them is written (archive_reason names the insight; reversible). Verified or importance-floored sources are never archived; contradiction sources always stay live."
        },
        "fallback_consolidate": {
          "type": "boolean",
          "default": false,
          "description": "When no --llm-endpoint is configured, run the mechanical (non-LLM) perseus_vault_consolidate cold_first pass instead of returning an error."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "categories_scanned": {
          "type": "array",
          "items": {
            "type": "string"
          }
        },
        "entities_examined": {
          "type": "integer",
          "description": "Number of entities scanned across all categories this run"
        },
        "clusters_dreamed": {
          "type": "integer",
          "description": "Clusters actually sent to the LLM this run"
        },
        "insights_written": {
          "type": "integer",
          "description": "Semantic insights written (or that would be, in dry-run)"
        },
        "insights_deduped": {
          "type": "integer",
          "description": "Insights skipped because the identical evidence set was already dreamed"
        },
        "contradictions_flagged": {
          "type": "integer",
          "description": "Insights flagged as contradictions among their sources"
        },
        "sources_archived": {
          "type": "integer",
          "description": "Sources archived because archive_sources was set (verified/importance-floored sources are exempt)"
        },
        "dry_run": {
          "type": "boolean"
        },
        "workspace_hash": {
          "type": ["string", "null"],
          "description": "#854 effective scope: the workspace this run operated in (null when global=true)"
        },
        "global": {
          "type": "boolean",
          "description": "#854 true when this run deliberately crossed all workspaces"
        },
        "insights": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "The insights written (or previewed), each with entity_id, key, summary, insight_type, confidence, source_ids, category, contradiction, deduped"
        },
        "fallback": {
          "type": "string",
          "description": "Present only when fallback_consolidate ran (no LLM endpoint): always \"consolidate\". The report then has this union shape — categories_scanned, entities_examined, observations_created, sources_archived, dry_run — instead of the LLM dream counters."
        },
        "note": {
          "type": "string",
          "description": "Fallback-only explanation of why the mechanical pass ran"
        },
        "observations_created": {
          "type": "integer",
          "description": "Fallback-only: observations created by the mechanical consolidate pass"
        }
      }
    },
    "annotations": {
      "readOnlyHint": false
    },
    "title": "Dream: LLM Consolidation of Episodic Memory into Semantic Insights"
  },
  {
    "name": "perseus_vault_vault_export",
    "description": "Export all non-archived entities to .md files with YAML frontmatter in a vault directory. Files are human-readable, git-trackable, and Obsidian-compatible. Use this for backup, transfer between workspaces, or offline review.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "vault_dir": {
          "type": "string",
          "default": "~/.perseus-vault/vault",
          "description": "Directory path to write .md files. Created if it doesn't exist. Use ~ for home directory."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "files_created": {
          "type": "integer",
          "description": "Number of new .md files created"
        },
        "files_updated": {
          "type": "integer",
          "description": "Number of existing .md files updated"
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Any errors encountered during export"
        },
        "vault_dir": {
          "type": "string",
          "description": "Absolute path to the vault directory"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Export Vault to Files"
  },
  {
    "name": "perseus_vault_derived_export",
    "description": "Compile durable knowledge into a deterministic, provenance-rich Markdown surface. The export is derived and read-only; SQLite remains the source of truth.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "output_path": {"type": "string", "description": "Markdown file path to write."},
        "workspace_hash": {"type": "string", "description": "Optional exact workspace scope."}
      },
      "required": ["output_path"]
    }
  },
  {
    "name": "perseus_vault_markdown_import",
    "description": "Import one Markdown file as explicitly non-authoritative, provenance-labeled draft evidence. Duplicate source content is idempotently detected.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "path": {"type": "string", "description": "Markdown file path to import."},
        "workspace_hash": {"type": "string"},
        "source_system": {"type": "string", "description": "Provenance source label; defaults to markdown."}
      },
      "required": ["path"]
    }
  },
  {
    "name": "perseus_vault_structured_index_anchor",
    "description": "Represent an upstream structured-index record as a refetchable anchor, or import it explicitly as low-confidence non-authoritative draft evidence.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "index_type": {"type": "string", "description": "Structured index kind, e.g. ide_symbol or domain_fact_map."},
        "index_uri": {"type": "string", "description": "Stable index locator for later refetch."},
        "record_id": {"type": "string", "description": "Stable record identity inside the index."},
        "mode": {"type": "string", "enum": ["reference", "import"], "default": "reference"},
        "content": {"type": "string", "description": "Required only for mode=import."},
        "workspace_hash": {"type": "string"},
        "source_system": {"type": "string"},
        "observed_at_unix_ms": {"type": "integer"},
        "revision": {"type": "string", "description": "Optional upstream revision/ETag for refetch verification."}
      },
      "required": ["index_type", "index_uri", "record_id"]
    }
  },
  {
    "name": "perseus_vault_vault_import",
    "description": "Import .md files from a vault directory into the database. Reads YAML frontmatter for metadata and markdown body for content. Idempotent — re-running on the same vault won't duplicate entities. Pair with perseus_vault_vault_export for transfer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "vault_dir": {
          "type": "string",
          "default": "~/.perseus-vault/vault",
          "description": "Directory path to read .md files from. Use ~ for home directory."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "files_created": {
          "type": "integer",
          "description": "Number of new entities created from files"
        },
        "files_updated": {
          "type": "integer",
          "description": "Number of existing entities updated"
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Any errors encountered during import"
        },
        "vault_dir": {
          "type": "string",
          "description": "Absolute path of the vault directory read"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Import Vault from Files"
  },
  {
    "name": "perseus_vault_decay",
    "description": "Recalculate Ebbinghaus decay scores for all entities based on time since last access. Auto-archives entities that have fully decayed (score < 0.05). Run periodically to keep memory fresh — decayed entities surface less often in recall results.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entities_checked": {
          "type": "integer",
          "description": "Total entities evaluated"
        },
        "entities_updated": {
          "type": "integer",
          "description": "Entities whose stored decay score was actually rewritten (rows whose recomputed score changed). A steady-state tick reports ~0: unchanged rows are evaluated but not written."
        },
        "auto_archived": {
          "type": "integer",
          "description": "Entities auto-archived because decay fell below 0.05"
        },
        "completed_at_unix_ms": {
          "type": "integer",
          "description": "Completion timestamp"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Recalculate Decay Scores"
  },
  {
    "name": "perseus_vault_reindex",
    "description": "Rebuild the FTS5 search index from the entities table. Repairs index drift — e.g. after a direct SQLite write, an interrupted archive, or a legacy database written before the atomic prune/forget fixes — so archived entities stop surfacing in recall/search. Returns the number of entities reindexed.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "reindexed": {
          "type": "integer",
          "description": "Number of non-archived entities indexed into FTS5"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Rebuild Search Index"
  },
  {
    "name": "perseus_vault_workspace_list",
    "description": "List all distinct entity categories present in the database. Use this to discover what knowledge domains exist before querying with perseus_vault_recall or perseus_vault_context.",
    "inputSchema": {
      "type": "object",
      "properties": {}
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "categories": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "All distinct categories in the database"
        },
        "total": {
          "type": "integer",
          "description": "Number of categories"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "List Workspace Categories"
  },
  {
    "name": "perseus_vault_recall_when",
    "description": "Search entities whose recall_when triggers match a given context. Use this for proactive just-in-time memory injection — before writing code, before plans, at session start. Pass the current task description as context and get back memories that declared they should be recalled in similar situations.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "context": {
          "type": "string",
          "description": "The current task or context description to match against recall_when triggers"
        },
        "limit": {
          "type": "integer",
          "description": "Maximum entities to return (default 10, max 100)",
          "default": 10
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter (v1.2.0). When set, only entities with a matching workspace_hash can fire. Omit for no workspace filtering — in a federated vault that lets one workspace's triggers inject into another's turns."
        }
      },
      "required": [
        "context"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "items": {
          "type": "array",
          "items": {
            "type": "object"
          }
        },
        "total": {
          "type": "integer"
        },
        "context": {
          "type": "string"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Proactive Recall by Context"
  },
  {
    "name": "perseus_vault_cohere",
    "description": "Run an autonomous coherence grooming pass over the memory. Promotes buffer entities to working layer, applies decay, auto-links related entities, and archives stale ones below the decay threshold. Use dry_run=true to preview without making changes.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dry_run": {
          "type": "boolean",
          "description": "If true, count what would be done without making changes",
          "default": false
        },
        "max_links": {
          "type": "integer",
          "description": "Maximum auto-links to create (default 20, max 100)",
          "default": 20
        },
        "promote_threshold": {
          "type": "integer",
          "description": "Retrieval count threshold for buffer to working promotion (default 3)",
          "default": 3
        },
        "archive_threshold": {
          "type": "number",
          "description": "Decay score below which entities are auto-archived (default 0.05)",
          "default": 0.05
        },
        "cross_scope_promote": {
          "type": "boolean",
          "description": "#486: also run cross-scope promotion — a fact independently observed in >= cross_scope_k distinct workspaces is promoted to one global-scope entity with promoted_from links back to the per-scope evidence. Off by default; re-runs are idempotent (the global scope's dedup absorbs them); undo by forgetting the promoted entity.",
          "default": false
        },
        "cross_scope_k": {
          "type": "integer",
          "description": "Minimum distinct workspaces before a recurring fact is promoted (default 3, minimum 2)",
          "default": 3
        },
        "cross_scope_similarity": {
          "type": "number",
          "description": "Trigram similarity treating two bodies as the same fact across scopes (default 0.7, matching write-time dedup)",
          "default": 0.7
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "promoted": {
          "type": "integer",
          "description": "Number of entities promoted from buffer to working"
        },
        "cross_scope_clusters": {
          "type": "integer",
          "description": "#486: clusters found spanning >= cross_scope_k workspaces (0 unless cross_scope_promote)"
        },
        "cross_scope_promoted": {
          "type": "integer",
          "description": "#486: new global-scope entities created by cross-scope promotion"
        },
        "cross_scope_skipped_existing": {
          "type": "integer",
          "description": "#486: qualifying clusters already represented at the global scope (idempotent re-run)"
        },
        "decayed": {
          "type": "integer",
          "description": "Number of entities whose decay score was reduced"
        },
        "linked": {
          "type": "integer",
          "description": "Number of auto-links created"
        },
        "archived": {
          "type": "integer",
          "description": "Number of entities archived due to low decay"
        },
        "entities_examined": {
          "type": "integer",
          "description": "Total non-archived entities examined"
        },
        "dry_run": {
          "type": "boolean"
        },
        "completed_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Run Coherence Grooming"
  },
  {
    "name": "perseus_vault_share",
    "description": "Share an entity to another workspace. Copies the entity (by category + key) from its current workspace into the target workspace, preserving content and metadata while generating a new ID. The original entity is unchanged. Use this for controlled cross-workspace knowledge transfer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "category": {
          "type": "string",
          "description": "Entity category to share"
        },
        "key": {
          "type": "string",
          "description": "Entity key to share"
        },
        "to_workspace": {
          "type": "string",
          "description": "Target workspace hash to copy the entity into"
        }
      },
      "required": [
        "category",
        "key",
        "to_workspace"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "shared_id": {
          "type": "string",
          "description": "ID of the new shared copy"
        },
        "action": {
          "type": "string",
          "description": "'created' or 'updated'"
        },
        "from_workspace": {
          "type": "string",
          "description": "Source workspace the entity was copied from"
        },
        "to_workspace": {
          "type": "string",
          "description": "Target workspace the entity was copied to"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Share Entity to Workspace"
  },
  {
    "name": "perseus_vault_federate",
    "description": "Federate entities from one workspace to another. Exports entities scoped to from_workspace, remaps their workspace_hash to to_workspace, and imports them — effectively copying or moving knowledge between workspaces. Use this for cross-agent or cross-project knowledge sharing without manual file transfer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_workspace": {
          "type": "string",
          "description": "Source workspace hash to export entities from"
        },
        "to_workspace": {
          "type": "string",
          "description": "Target workspace hash to import entities into"
        },
        "vault_dir": {
          "type": "string",
          "default": "/tmp/perseus-vault-federate",
          "description": "Temporary vault directory for the intermediate .md export files"
        }
      },
      "required": [
        "from_workspace",
        "to_workspace"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "exported": {
          "type": "integer",
          "description": "Number of entities exported from the source workspace"
        },
        "remapped": {
          "type": "integer",
          "description": "Number of entities whose workspace_hash was remapped"
        },
        "imported": {
          "type": "integer",
          "description": "Number of entities imported into the target workspace"
        },
        "import_errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Any errors encountered during import"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Federate Entities Between Workspaces"
  },
  {
    "name": "perseus_vault_correct",
    "description": "Capture a user correction to the agent. Stores what went wrong, what the user said, and the lesson learned — as both a 'correction' entity and a journal entry. Use this every time the user corrects your approach. Enables the self-improving feedback loop: the agent learns from mistakes across sessions.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "wrong_approach": {
          "type": "string",
          "description": "What the agent did that was wrong (the mistaken approach)"
        },
        "user_correction": {
          "type": "string",
          "description": "What the user said to correct the agent (the right way)"
        },
        "task_context": {
          "type": "string",
          "description": "What task was being attempted when the correction occurred"
        },
        "evidence": {
          "type": "object",
          "description": "Write-time audit envelope for the correction's source evidence. capture_mode distinguishes snapshot, hash_only, pointer_only, not_requested, capture_failed, and legacy_unknown; a missing value is never interpreted implicitly.",
          "properties": {
            "capture_mode": { "type": "string", "enum": ["snapshot", "hash_only", "pointer_only", "not_requested", "capture_failed", "legacy_unknown"] },
            "resolved_value": { "description": "Resolved source value retained at write time when capture_mode=snapshot" },
            "content_sha256": { "type": "string", "description": "64-hex SHA-256 of the resolved value or source bytes" },
            "source_system": { "type": "string" },
            "source_ref": { "type": "string" },
            "captured_at_unix_ms": { "type": "integer" },
            "replayable": { "type": "boolean" }
          },
          "required": ["capture_mode", "captured_at_unix_ms", "replayable"]
        },
        "session_id": {
          "type": "string",
          "default": "",
          "description": "Session identifier for traceability"
        },
        "tags": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Tags for categorization"
        },
        "category": {
          "type": "string",
          "default": "correction",
          "description": "Entity category (default: 'correction')"
        },
        "visibility": {
          "type": "string",
          "default": "workspace",
          "description": "Visibility: 'private', 'workspace', or 'public'"
        },
        "valid_from_unix_ms": {
          "type": "integer",
          "description": "Application-time period start (#363): when the corrected fact was actually true in the world. Set in the past for retroactive corrections. Default: transaction time."
        },
        "valid_to_unix_ms": {
          "type": "integer",
          "description": "Application-time period end (#363, exclusive). Omit for 'still true'."
        },
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Workspace scope for the rejection tombstone (#849). Empty means global."
        },
        "agent_id": {
          "type": "string",
          "default": "",
          "description": "Agent that authored the correction (stamped on the tombstone)."
        },
        "requesting_agent_id": {
          "type": "string",
          "default": "",
          "description": "#855 host identity (stamped by the MCP transport). When present, it is authoritative: the correction entity, journal event, and tombstone attribute the host, not any model-supplied agent_id."
        }
      },
      "required": [
        "wrong_approach",
        "user_correction",
        "task_context"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entity_id": {
          "type": "string",
          "description": "Created correction entity ID"
        },
        "journal_id": {
          "type": "string",
          "description": "Created journal entry ID"
        },
        "agent_id": {
          "type": "string",
          "description": "#855 agent attribution persisted on the entity and journal event (host identity when the transport stamped one)"
        },
        "workspace_hash": {
          "type": "string",
          "description": "#855 workspace scope persisted on the entity and journal event. Empty = global/legacy."
        },
        "category": {
          "type": "string"
        },
        "key": {
          "type": "string"
        },
        "created_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Capture Agent Correction"
  },
  {
    "name": "perseus_vault_synthesize",
    "description": "LLM-driven session synthesis. Reviews a session transcript and extracts structured lessons: what worked (success), what failed (failure), what was corrected (correction), what was abandoned (dead_end), and key decisions made (decision). Each lesson becomes an entity linked to a synthesis journal entry. Requires --llm-endpoint to be configured. This is the Perplexity-Brain-style overnight synthesis loop for agent self-improvement.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "session_content": {
          "type": "string",
          "description": "Full session transcript to synthesize lessons from"
        },
        "session_id": {
          "type": "string",
          "default": "",
          "description": "Session identifier for traceability"
        },
        "tags": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Tags applied to all synthesized entities"
        },
        "visibility": {
          "type": "string",
          "default": "workspace",
          "description": "Visibility for synthesized entities"
        }
      },
      "required": [
        "session_content"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "lessons": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "lesson_type": {
                "type": "string"
              },
              "summary": {
                "type": "string"
              },
              "evidence": {
                "type": "string"
              },
              "confidence": {
                "type": "number"
              }
            }
          },
          "description": "Extracted lessons with type, summary, evidence, and confidence"
        },
        "entities_created": {
          "type": "integer",
          "description": "Number of lesson entities created"
        },
        "journal_id": {
          "type": "string"
        },
        "dry_run": {
          "type": "boolean"
        },
        "completed_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Synthesize Session Lessons"
  },
  {
    "name": "perseus_vault_bench",
    "description": "Record a performance benchmark data point. Tracks task metrics (turns taken, tokens used, success) alongside whether memory recall was used — enabling measurement of Perseus Vault's impact on agent performance. Aggregate with perseus_vault_recall to analyze trends.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "task_description": {
          "type": "string",
          "description": "Description of the task being measured"
        },
        "turns_taken": {
          "type": "integer",
          "description": "Number of conversation turns the task took"
        },
        "tokens_used": {
          "type": "integer",
          "description": "Total tokens consumed by the task"
        },
        "memory_recall_used": {
          "type": "boolean",
          "description": "Whether memory recall (perseus_vault_recall) was used during this task"
        },
        "recall_count": {
          "type": "integer",
          "default": 0,
          "description": "How many times memory was recalled during this task"
        },
        "task_success": {
          "type": "boolean",
          "default": false,
          "description": "Whether the task completed successfully"
        },
        "session_id": {
          "type": "string",
          "default": "",
          "description": "Session identifier for traceability"
        },
        "tags": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Tags for categorization"
        }
      },
      "required": [
        "task_description",
        "turns_taken",
        "tokens_used",
        "memory_recall_used"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "entity_id": {
          "type": "string",
          "description": "Created benchmark entity ID"
        },
        "created_at_unix_ms": {
          "type": "integer"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Record Benchmark"
  },
  {
    "name": "perseus_vault_autocohere",
    "description": "Run a full atomic grooming pass. When capture_text is supplied, capture runs first and must succeed before cohere, decay, compact, consolidation, or retention can compress source context. Returns a summary report. Use dry_run=true to preview without writing.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dry_run": {
          "type": "boolean",
          "description": "If true, preview changes without writing",
          "default": false
        },
        "capture_text": {
          "type": "string",
          "description": "Optional raw transcript/insight payload persisted before every compaction-like stage. Capture failure aborts the pass."
        },
        "capture_workspace_hash": {
          "type": "string",
          "description": "Workspace scope for pre-compaction captured facts"
        },
        "capture_agent_id": {
          "type": "string",
          "description": "Agent attribution for pre-compaction captured facts"
        },
        "capture_max_entities": {
          "type": "integer",
          "description": "Maximum durable notes extracted from capture_text (1-20)"
        },
        "workspace_hash": {
          "type": "string",
          "description": "#854 workspace scope for the consolidation step. When set, only that workspace's entities are consolidated and the observations inherit the scope. Omit for the whole-vault pass."
        },
        "global": {
          "type": "boolean",
          "default": false,
          "description": "#854 explicit whole-vault consolidation mode (capability-gated with a host identity). Mutually exclusive with workspace_hash."
        },
        "requesting_agent_id": {
          "type": "string",
          "default": "",
          "description": "Host identity stamped by the MCP transport. Used for global-mode authorization and consolidation author attribution."
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "precompact_capture": {
          "type": "object",
          "description": "Capture barrier report. stage=completed means capture persisted before all lifecycle compression stages; stage=skipped means no capture_text was supplied."
        },
        "promoted_entities": {
          "type": "integer",
          "description": "Entities promoted during cohere"
        },
        "links_created": {
          "type": "integer",
          "description": "Auto-links created during cohere"
        },
        "archived_entities": {
          "type": "integer",
          "description": "Entities archived (cohere + compact)"
        },
        "decay_updates": {
          "type": "integer",
          "description": "Entities whose decay score was updated"
        },
        "compact_archived_count": {
          "type": "integer",
          "description": "Entities archived during compact step"
        },
        "history_rows_evicted": {
          "type": "integer",
          "description": "entity_history rows evicted by the retention policy (#398; 0 while no PERSEUS_VAULT_HISTORY_* knob is set)"
        },
        "history_bytes_evicted": {
          "type": "integer",
          "description": "Stored history body bytes evicted (#398)"
        },
        "history_tombstones_written": {
          "type": "integer",
          "description": "Compaction tombstones written (#398)"
        },
        "db_size_delta_bytes": {
          "type": "integer",
          "description": "Change in SQLite file size in bytes"
        },
        "decay_auto_archived": {
          "type": "integer",
          "description": "Entities decay auto-archived during this pass (#490; 0 under dry_run)"
        },
        "observations_created": {
          "type": "integer",
          "description": "Observations created by the consolidation step"
        },
        "consolidate_sources_archived": {
          "type": "integer",
          "description": "Sources archived by the consolidation step (verified/importance-floored exempt)"
        },
        "workspace_hash": {
          "type": ["string", "null"],
          "description": "#854 effective consolidation scope: the workspace the consolidate step operated in (null = whole-vault pass)"
        },
        "global": {
          "type": "boolean",
          "description": "#854 true when the consolidation step deliberately crossed all workspaces"
        },
        "dry_run": {
          "type": "boolean"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Atomic Coherence Pass"
  },
  {
    "name": "perseus_vault_supersede",
    "description": "Create a 'supersedes' relationship from a new fact to an old one, setting the old entity's status to 'deprecated'. Use this when a newer entity makes an older one obsolete.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "from_category": {
          "type": "string",
          "description": "Category of the OLD entity being superseded"
        },
        "from_key": {
          "type": "string",
          "description": "Key of the OLD entity being superseded"
        },
        "to_category": {
          "type": "string",
          "description": "Category of the NEW entity that supersedes"
        },
        "to_key": {
          "type": "string",
          "description": "Key of the NEW entity that supersedes"
        },
        "reason": {
          "type": "string",
          "description": "Reason for superseding (recorded in archive_reason)",
          "default": ""
        },
        "relationship": {
          "type": "string",
          "description": "Link relationship type (default: 'supersedes')",
          "default": "supersedes"
        },
        "valid_to_unix_ms": {
          "type": "integer",
          "description": "When the OLD fact stopped being true in the world (#363, unix ms). Defaults to transaction time (now). Closes the old entity's application-time period so perseus_vault_valid_at stops returning it from that instant on. Must be after the fact's valid_from, and may only TIGHTEN an already-closed period (a fact that ended cannot be retroactively extended); violations are rejected before any mutation."
        }
      },
      "required": [
        "from_category",
        "from_key",
        "to_category",
        "to_key"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "from_entity_id": {
          "type": "string",
          "description": "ID of the old (superseded) entity"
        },
        "from_entity_category": {
          "type": "string"
        },
        "from_entity_key": {
          "type": "string"
        },
        "from_valid_to_unix_ms": {
          "type": "integer",
          "description": "The instant the old fact's validity was closed at (#363)"
        },
        "to_entity_id": {
          "type": "string",
          "description": "ID of the new (superseding) entity"
        },
        "to_entity_category": {
          "type": "string"
        },
        "to_entity_key": {
          "type": "string"
        },
        "relationship": {
          "type": "string"
        },
        "status_updated": {
          "type": "string",
          "description": "New status of the old entity (always 'deprecated')"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Supersede Entity"
  },
  {
    "name": "perseus_vault_maintenance",
    "description": "Database maintenance operations: deduplicate entities with identical (category, key), detect orphan journal entries and links, vacuum (reclaim disk space), reindex FTS5, and enforce the entity_history retention policy (#398 — no-op unless PERSEUS_VAULT_HISTORY_* env knobs are set). Set dry_run=true to preview. Use 'all' to run everything.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "dedup": {
          "type": "boolean",
          "description": "Find duplicate (category, key) entities and archive the oldest",
          "default": false
        },
        "orphans": {
          "type": "boolean",
          "description": "Detect journal entries and links pointing to non-existent entities",
          "default": false
        },
        "vacuum": {
          "type": "boolean",
          "description": "Run SQLite VACUUM to reclaim disk space",
          "default": false
        },
        "reindex": {
          "type": "boolean",
          "description": "Rebuild the FTS5 search index from entities table",
          "default": false
        },
        "history": {
          "type": "boolean",
          "description": "Enforce the entity_history retention policy from PERSEUS_VAULT_HISTORY_* env knobs (#398; no-op while none are set)",
          "default": false
        },
        "all": {
          "type": "boolean",
          "description": "Run all maintenance operations (dedup, orphans, vacuum, reindex, history retention)",
          "default": false
        },
        "dry_run": {
          "type": "boolean",
          "description": "If true, preview changes without writing",
          "default": false
        }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "dedup_archived": {
          "type": "integer",
          "description": "Number of duplicate entities archived"
        },
        "orphan_journal_entries_found": {
          "type": "integer",
          "description": "Orphan journal entries detected"
        },
        "orphan_links_found": {
          "type": "integer",
          "description": "Orphan links detected"
        },
        "vacuum_reclaimed_bytes": {
          "type": "integer",
          "description": "Disk space reclaimed by VACUUM"
        },
        "reindex_rows_affected": {
          "type": "integer",
          "description": "Rows reindexed into FTS5"
        },
        "history_rows_evicted": {
          "type": "integer",
          "description": "entity_history rows evicted by the retention policy (#398)"
        },
        "history_bytes_evicted": {
          "type": "integer",
          "description": "Stored history body bytes evicted (#398)"
        },
        "history_tombstones_written": {
          "type": "integer",
          "description": "Compaction tombstones written for evicted runs (#398)"
        },
        "dry_run": {
          "type": "boolean"
        },
        "errors": {
          "type": "array",
          "items": {
            "type": "string"
          },
          "description": "Errors encountered during maintenance"
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Run Database Maintenance"
  },
  {
    "name": "perseus_vault_communities",
    "description": "GraphRAG community detection: partition the entity link graph (built via perseus_vault_link) into communities using deterministic label propagation or greedy modularity ('louvain'). Persists the result with an extractive summary per community; community ids are derived from the member set, so re-detection after membership changes yields new ids. Local-first — no LLM or network required.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Workspace scope for the graph. Empty = global/unscoped entities."
        },
        "algorithm": {
          "type": "string",
          "default": "label_prop",
          "enum": ["label_prop", "louvain"],
          "description": "Detection algorithm: 'label_prop' (deterministic label propagation, default) or 'louvain' (greedy one-level modularity optimization)."
        },
        "min_size": {
          "type": "integer",
          "default": 2,
          "description": "Minimum member count for a community to be kept (minimum 2 — isolated entities never form communities)."
        }
      },
      "required": []
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "workspace_hash": { "type": "string" },
        "algorithm": { "type": "string" },
        "node_count": { "type": "integer", "description": "Entities considered as graph nodes" },
        "edge_count": { "type": "integer", "description": "Undirected edges in the graph" },
        "modularity": { "type": "number", "description": "Newman modularity of the detected partition" },
        "communities": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": { "type": "string", "description": "Community id ('com-' + member-set digest)" },
              "size": { "type": "integer" },
              "member_ids": { "type": "array", "items": { "type": "string" } },
              "summary": { "type": "string", "description": "Extractive summary (top members by in-community degree), capped in size" }
            }
          }
        },
        "stale_summaries_archived": { "type": "integer", "description": "Stale community_summary entities archived because membership changed" },
        "generated_at_unix_ms": { "type": "integer" }
      }
    },
    "annotations": {
      "idempotentHint": true
    },
    "title": "Detect Link-Graph Communities"
  },
  {
    "name": "perseus_vault_community_summary",
    "description": "Return (and materialize) the summary of one detected community. Default is the extractive summary (top representative members); set use_llm=true for an optional LLM polish that degrades back to extractive when no LLM endpoint is configured. The summary is stored as a 'community_summary' entity carrying evidence_for links to its members, and cached while membership is unchanged.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "community_id": {
          "type": "string",
          "description": "Community id from perseus_vault_communities, e.g. 'com-1a2b3c4d5e6f7a8b'"
        },
        "use_llm": {
          "type": "boolean",
          "default": false,
          "description": "Polish the summary with the configured LLM (--llm-endpoint). Never required: falls back to the extractive summary on error or when disabled."
        },
        "refresh": {
          "type": "boolean",
          "default": false,
          "description": "Force regeneration even when a cached summary entity exists."
        }
      },
      "required": ["community_id"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "community_id": { "type": "string" },
        "summary": { "type": "string" },
        "summary_entity_id": { "type": "string", "description": "entities.id of the materialized community_summary entity" },
        "member_count": { "type": "integer" },
        "cached": { "type": "boolean", "description": "True when an existing summary entity was reused (membership unchanged)" },
        "llm_used": { "type": "boolean" }
      }
    },
    "annotations": {
      "idempotentHint": true
    },
    "title": "Get Community Summary"
  },
  {
    "name": "perseus_vault_global_recall",
    "description": "GraphRAG global search: answer a broad 'what does the vault know about X, holistically' query by scoring it against community summaries first (breadth), then drilling into the best communities' member entities (depth). Cites entities across multiple communities instead of returning only the single nearest cluster like flat recall. Detects communities automatically on first use. Local-first and deterministic; optional use_llm synthesizes the final answer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "query": {
          "type": "string",
          "description": "The global question to answer across the whole memory graph"
        },
        "workspace_hash": {
          "type": "string",
          "default": "",
          "description": "Workspace scope. Empty = global/unscoped entities."
        },
        "top_communities": {
          "type": "integer",
          "default": 3,
          "description": "How many best-matching communities to drill into"
        },
        "limit": {
          "type": "integer",
          "default": 10,
          "description": "Max member entities cited across all communities (round-robined so every matched community is represented)"
        },
        "auto_detect": {
          "type": "boolean",
          "default": true,
          "description": "Run community detection automatically when none are persisted yet"
        },
        "use_llm": {
          "type": "boolean",
          "default": false,
          "description": "Synthesize the final answer with the configured LLM; degrades to the extractive answer on error or when disabled."
        }
      },
      "required": ["query"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "query": { "type": "string" },
        "workspace_hash": { "type": "string" },
        "communities_considered": { "type": "integer", "description": "Persisted communities scored in the breadth pass" },
        "communities": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": { "type": "string" },
              "score": { "type": "number", "description": "Distinct query-token hits in the community summary" },
              "size": { "type": "integer" },
              "summary": { "type": "string" },
              "members": {
                "type": "array",
                "items": {
                  "type": "object",
                  "properties": {
                    "id": { "type": "string" },
                    "category": { "type": "string" },
                    "key": { "type": "string" },
                    "score": { "type": "number" },
                    "snippet": { "type": "string" }
                  }
                }
              }
            }
          }
        },
        "answer": { "type": "string", "description": "Extractive (or LLM-synthesized) holistic answer citing entities across communities" },
        "llm_used": { "type": "boolean" }
      }
    },
    "title": "Global Recall (GraphRAG)"
  },
  {
    "name": "perseus_vault_keystone_set",
    "description": "Author a Keystone — a mandatory policy rule that survives context compaction (#683). Unlike ordinary memories (retrieved when relevant), keystones are fetched deterministically at session start via perseus_vault_keystone_get, merged across scope, and are meant to be obeyed over any conflicting instruction (e.g. 'Every memory write MUST carry a retention class', 'Customer PII MUST NOT cross agent boundaries'). Higher weight wins on contradiction. Re-setting the same (scope, scope_id, content) updates it in place. Every mutation is appended to the cryptographic audit chain. Authoring is gated on trust tier: pass author_trust_tier (>= trust_tier_required, default 2). NOTE: until multi-agent trust tiers land (#684), author_trust_tier is caller-asserted; when omitted the write is allowed and the response flags that enforcement is pending.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "content": { "type": "string", "description": "The policy rule text. Imperative, testable directives work best." },
        "scope": { "type": "string", "default": "tenant", "description": "Merge scope: 'tenant' (org-wide), 'fleet' (a team), or 'agent' (an individual). Narrower scopes are layered on top of broader ones at get time." },
        "scope_id": { "type": "string", "description": "Identifier the keystone applies to within a non-tenant scope: the fleet_id ('fleet') or agent_id ('agent'). Omit/empty for tenant scope or 'all in scope'." },
        "weight": { "type": "number", "default": 1.0, "description": "Conflict-resolution weight; on contradiction the higher-weight keystone wins. Also the merge/sort order returned by keystone_get." },
        "trust_tier_required": { "type": "integer", "default": 2, "description": "Minimum author trust tier permitted to set/modify this keystone. Defaults to 2 (per #684's tier model: tier 2 = write keystones)." },
        "author_trust_tier": { "type": "integer", "description": "The authoring agent's trust tier, checked against trust_tier_required. Caller-asserted until #684 wires per-agent trust + session identity." },
        "agent_id": { "type": "string", "description": "Identity of the authoring agent, stamped on the keystone and its audit-chain event for provenance." },
        "workspace_hash": { "type": "string", "description": "Optional workspace scope. Keystones with an empty workspace_hash are global (apply everywhere)." }
      },
      "required": ["content"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "id": { "type": "string" },
        "created": { "type": "boolean", "description": "true if a new keystone was created, false if an existing one was updated" },
        "trust_enforced": { "type": "boolean", "description": "false when author_trust_tier was omitted (enforcement pending #684)" }
      }
    },
    "title": "Set Keystone"
  },
  {
    "name": "perseus_vault_keystone_get",
    "description": "Fetch the merged Keystones (mandatory policy rules, #683) that apply at session start — the deterministic counterpart to recall. Returns rules ordered by weight (highest first, then scope tenant<fleet<agent, then id) so a renderer can inject them ahead of all other context and resolve contradictions by weight. Filter by scope/scope_id/workspace to get exactly the set an agent must obey. Read-only.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "scope": { "type": "string", "description": "Optional: restrict to a single scope ('tenant' | 'fleet' | 'agent'). Omit to merge all scopes." },
        "scope_id": { "type": "string", "description": "Optional: with a non-tenant scope, restrict to this fleet_id/agent_id. Rules with an empty scope_id (scope-wide) are always included." },
        "workspace_hash": { "type": "string", "description": "Optional workspace scope. Global keystones (empty workspace_hash) are always included." }
      }
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "keystones": {
          "type": "array",
          "items": {
            "type": "object",
            "properties": {
              "id": { "type": "string" },
              "content": { "type": "string" },
              "scope": { "type": "string" },
              "scope_id": { "type": "string" },
              "weight": { "type": "number" }
            }
          }
        },
        "count": { "type": "integer" }
      }
    },
    "title": "Get Keystones"
  },
  {
    "name": "perseus_vault_agent",
    "description": "Register/update or look up an agent in the multi-agent registry (#684). Agents carry a trust tier (0-3) that gates sensitive ops (e.g. authoring keystones needs tier >= 2) and drives visibility enforcement on recall: tier 0 = read own only, 1 = fleet, 2 = read all + write keystones, 3 = admin. Pass trust_tier (and optionally name/fleet_id) to upsert; omit trust_tier to just look up. entities/journal already stamp agent_id (v1.2.0); this adds the identity + tier metadata. NOTE: an empty/unknown agent has no registry row — unknown identified agents resolve to tier 0, and a caller with no session identity is unscoped.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "agent_id": { "type": "string", "description": "The agent's stable identifier (e.g. the MCP clientInfo name)." },
        "name": { "type": "string", "description": "Human-readable name (upsert only)." },
        "trust_tier": { "type": "integer", "description": "Trust tier 0-3. Provide to upsert; omit to look up. Clamped to [0,3]." },
        "fleet_id": { "type": "string", "description": "Fleet/team the agent belongs to (used for 'fleet' visibility). Upsert only." }
      },
      "required": ["agent_id"]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "found": { "type": "boolean" },
        "created": { "type": "boolean", "description": "true if an upsert created a new registry row" },
        "agent": {
          "type": "object",
          "properties": {
            "agent_id": { "type": "string" },
            "name": { "type": "string" },
            "trust_tier": { "type": "integer" },
            "fleet_id": { "type": "string" }
          }
        }
      }
    },
    "title": "Agent Registry"
  },
  {
    "name":"perseus_vault_authority_set", "description":"Create a versioned authority manifest for a registered agent.", "inputSchema":{"type":"object","properties":{"agent_id":{"type":"string"},"workspace_hash":{"type":"string"},"allowed_capabilities":{"type":"array","items":{"type":"string"}},"scope_anchors":{"type":"array","items":{"type":"string"}},"approval_required_capabilities":{"type":"array","items":{"type":"string"}},"approver_principals":{"type":"array","items":{"type":"string"}},"allowed_inbound_principals":{"type":"array","items":{"type":"string"}},"permitted_external_ref_prefixes":{"type":"array","items":{"type":"string"}},"max_parallel_actions":{"type":"integer","default":1},"mode":{"type":"string","default":"shadow"},"expires_at_unix_ms":{"type":"integer"},"author_agent_id":{"type":"string"},"capability_constraints_json":{"type":"string","default":"{}"}},"required":["agent_id","workspace_hash","allowed_capabilities","scope_anchors"]}, "title":"Set Action Authority"},
  {"name":"perseus_vault_authority_get", "description":"Get the active authority manifest for an agent and workspace.", "inputSchema":{"type":"object","properties":{"agent_id":{"type":"string"},"workspace_hash":{"type":"string"},"include_revoked":{"type":"boolean","default":false}},"required":["agent_id","workspace_hash"]}, "title":"Get Action Authority"},
  {"name":"perseus_vault_authority_revoke", "description":"Revoke an authority manifest.", "inputSchema":{"type":"object","properties":{"manifest_id":{"type":"string"},"actor_agent_id":{"type":"string"},"reason":{"type":"string"}},"required":["manifest_id"]}, "title":"Revoke Action Authority"},
  {"name":"perseus_vault_authority_set_signed", "description":"Load a signed, distributable policy/authority profile (Ed25519 sigstore-style attestation); verification failure grants no authority (fail closed) and the verification result lands in the ledger journal.", "inputSchema":{"type":"object","properties":{"profile_json":{"type":"string"},"trusted_public_key_b64":{"type":"string"},"author_agent_id":{"type":"string"}},"required":["profile_json","trusted_public_key_b64","author_agent_id"]}, "title":"Load Signed Authority Profile"},
  {"name":"perseus_vault_action_intent", "description":"Record a fail-closed authorized action intent.", "inputSchema":{"type":"object","properties":{"agent_id":{"type":"string"},"workspace_hash":{"type":"string"},"scope_anchor":{"type":"string"},"external_ref":{"type":"string"},"capability":{"type":"string"},"action_key":{"type":"string"},"intent_hash":{"type":"string"},"resource_constraints_json":{"type":"string","default":"{}"}},"required":["agent_id","workspace_hash","scope_anchor","external_ref","capability","action_key","intent_hash"]}, "title":"Record Action Intent"},
  {"name":"perseus_vault_action_approve", "description":"Grant or deny an approval-requested action.", "inputSchema":{"type":"object","properties":{"action_id":{"type":"string"},"approver_principal":{"type":"string"},"decision":{"type":"string","enum":["granted","denied"]}},"required":["action_id","approver_principal","decision"]}, "title":"Decide Action Approval"},
  {"name":"perseus_vault_action_complete", "description":"Record an executed, failed, cancelled, or denied action outcome by hash.", "inputSchema":{"type":"object","properties":{"action_id":{"type":"string"},"actor_agent_id":{"type":"string"},"outcome":{"type":"string","enum":["executed","failed","cancelled","denied"]},"outcome_hash":{"type":"string"}},"required":["action_id","actor_agent_id","outcome","outcome_hash"]}, "title":"Complete Authorized Action"},
  {"name":"perseus_vault_action_resolve_timeout", "description":"Resolve a pending approval to deny once its window has expired (timeout defaults to deny).", "inputSchema":{"type":"object","properties":{"action_id":{"type":"string"},"approval_timeout_ms":{"type":"integer"}},"required":["action_id","approval_timeout_ms"]}, "title":"Resolve Approval Timeout"},
  {"name":"perseus_vault_action_receipt_get", "description":"Get durable action receipt metadata and hashes.", "inputSchema":{"type":"object","properties":{"action_id":{"type":"string"}},"required":["action_id"]}, "title":"Get Action Receipt"},
  {"name":"perseus_vault_action_lease_acquire", "description":"Acquire the single active lease for an action key.", "inputSchema":{"type":"object","properties":{"action_id":{"type":"string"},"holder_id":{"type":"string"},"ttl_seconds":{"type":"integer","default":1}},"required":["action_id","holder_id"]}, "title":"Acquire Action Lease"},
  {"name":"perseus_vault_action_lease_release", "description":"Release an action lease held by its owner.", "inputSchema":{"type":"object","properties":{"lease_id":{"type":"string"},"holder_id":{"type":"string"}},"required":["lease_id","holder_id"]}, "title":"Release Action Lease"},
  {"name":"perseus_vault_stage_trace_validate", "description":"Validate a versioned hash-only runtime stage trace and optionally compare replay semantics. Raw prompts, memory bodies, credentials, and tool payloads are not accepted.", "inputSchema":{"type":"object","properties":{"trace":{"type":"object","description":"perseus-vault-stage-trace/v1 structured trace"},"replay_of":{"type":"object","description":"Optional second trace to compare by replay fingerprint"}},"required":["trace"]}, "title":"Validate Runtime Stage Trace"},
  {"name":"perseus_vault_reject_value", "description":"Record a scoped digest-only rejected-value tombstone. Equivalent values remain rejected across new entity keys and writer paths until the tombstone expires or is explicitly superseded.", "inputSchema":{"type":"object","properties":{"workspace_hash":{"type":"string","description":"Workspace scope; empty means global."},"subject":{"type":"string"},"predicate":{"type":"string"},"value":{"type":"string","description":"Normalized only for matching; the value is not stored."},"reason":{"type":"string"},"evidence_ref":{"type":"string"},"author_agent_id":{"type":"string"},"expires_at_unix_ms":{"type":"integer"}},"required":["workspace_hash","subject","predicate","value"]}, "title":"Reject Value"}
]"###,
        )
        .expect("tools JSON must be valid");
        registry
            .as_array()
            .expect("tools registry must be a JSON array")
            .clone()
    })
}

/// Build the tools/list response from the canonical registry (parsed once by
/// `tool_registry_base`; cached there so repeated tools/list calls don't
/// re-parse the embedded literal — perf review #208).
fn list_tools(id: Option<Value>) -> JsonRpcResponse {
    JsonRpcResponse {
        jsonrpc: "2.0".to_string(),
        id,
        result: Some(json!({
            "tools": tool_registry_base()
        })),
        error: None,
    }
}
fn call_tool(name: &str, db: &Database, args: Value, _id: Option<Value>) -> String {
    // Keep the caller's original name for error messages — a
    // "perseus_vault_bogus" call should say so, not report a rewritten name.
    let original_name = name;

    // #858: fail loud when the running binary was replaced on disk — never
    // silently serve results from a stale process image. The handoff tool and
    // health stay callable (health reports the staleness in its payload).
    if let Some(stale_msg) = crate::live_update::stale_error_message(name) {
        return serde_json::to_string(&json!({
            "content": [{"type": "text", "text": stale_msg}],
            "isError": true
        }))
        .unwrap_or_else(|_| {
            format!(
                r#"{{"content":[{{"type":"text","text":"{}"}}],"isError":true}}"#,
                stale_msg
            )
        });
    }

    let handler_result: Result<String, String> = match name {
        "perseus_vault_remember" => tools::handle_remember(db, args).map_err(|e| e.to_string()),

        "perseus_vault_reject_value" => tools::handle_reject_value(db, args).map_err(|e| e.to_string()),

        "perseus_vault_recall" => tools::handle_recall(db, args).map_err(|e| e.to_string()),

        "perseus_vault_recall_batch" => tools::handle_recall_batch(db, args).map_err(|e| e.to_string()),

        "perseus_vault_recall_layer" => tools::handle_recall_layer(db, args).map_err(|e| e.to_string()),

        "perseus_vault_scan" => tools::handle_scan(db, args).map_err(|e| e.to_string()),

        "perseus_vault_hygiene" => tools::handle_hygiene(db, args).map_err(|e| e.to_string()),

        "perseus_vault_semantic_search" => {
            tools::handle_semantic_search(db, args).map_err(|e| e.to_string())
        }

        "perseus_vault_ask" => tools::handle_ask(db, args).map_err(|e| e.to_string()),

        "perseus_vault_get_entity" => tools::handle_get_entity(db, args).map_err(|e| e.to_string()),
        "perseus_vault_history" => tools::handle_history(db, args).map_err(|e| e.to_string()),
        "perseus_vault_as_of" => tools::handle_as_of(db, args).map_err(|e| e.to_string()),
        "perseus_vault_valid_at" => tools::handle_valid_at(db, args).map_err(|e| e.to_string()),
        "perseus_vault_bitemporal" => tools::handle_bitemporal(db, args).map_err(|e| e.to_string()),
        "perseus_vault_forget" => tools::handle_forget(db, args).map_err(|e| e.to_string()),

        "perseus_vault_ingest" => tools::handle_ingest(db, args).map_err(|e| e.to_string()),

        "perseus_vault_ingest_file" => tools::handle_ingest_file(db, args).map_err(|e| e.to_string()),

        "perseus_vault_artifact_register" => {
            tools::handle_artifact_register(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_artifact_manifest" => {
            tools::handle_artifact_manifest(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_artifact_excerpt" => {
            tools::handle_artifact_excerpt(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_artifact_log_digest" => {
            tools::handle_artifact_log_digest(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_artifact_verify_value" => {
            tools::handle_artifact_verify_value(db, args).map_err(|e| e.to_string())
        }

        "perseus_vault_embed" => tools::handle_embed(db, args).map_err(|e| e.to_string()),

        "perseus_vault_prune" => tools::handle_prune(db, args).map_err(|e| e.to_string()),

        "perseus_vault_link" => tools::handle_link(db, args).map_err(|e| e.to_string()),

        "perseus_vault_unlink" => tools::handle_unlink(db, args).map_err(|e| e.to_string()),

        "perseus_vault_journal" => tools::handle_journal(db, args).map_err(|e| e.to_string()),

        "perseus_vault_check_failure_pattern" => {
            tools::handle_check_failure_pattern(db, args).map_err(|e| e.to_string())
        }

        "perseus_vault_timeline" => tools::handle_timeline(db, args).map_err(|e| e.to_string()),

        "perseus_vault_state_set" => tools::handle_state_set(db, args).map_err(|e| e.to_string()),

        "perseus_vault_state_get" => tools::handle_state_get(db, args).map_err(|e| e.to_string()),

        "perseus_vault_state_delete" => tools::handle_state_delete(db, args).map_err(|e| e.to_string()),

        "perseus_vault_state_list" => tools::handle_state_list(db, args).map_err(|e| e.to_string()),

        "perseus_vault_health" => Ok(tools::handle_health(db)),
        "perseus_vault_handoff_restart" => crate::live_update::handle_handoff_restart(args),
        "perseus_vault_quality_telemetry" => tools::handle_quality_telemetry(db, args),
        "perseus_vault_retrieval_telemetry" => tools::handle_retrieval_telemetry(db, args),

        "perseus_vault_stats" => Ok(tools::handle_stats(db)),

        "perseus_vault_compact" => Ok(tools::handle_compact(db, args)),

        "perseus_vault_purge" => tools::handle_purge(db, args).map_err(|e| e.to_string()),
        "perseus_vault_expire" => tools::handle_expire(db, args).map_err(|e| e.to_string()),
        "perseus_vault_redact" => tools::handle_redact(db, args).map_err(|e| e.to_string()),
        "perseus_vault_erase" => tools::handle_erase(db, args).map_err(|e| e.to_string()),
        "perseus_vault_learned_artifact_register" => {
            tools::handle_learned_artifact_register(db, args).map_err(|e| e.to_string())
        }

        "perseus_vault_workspace_bind" => {
            tools::handle_workspace_bind(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_workspace_unbind" => {
            tools::handle_workspace_unbind(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_workspace_quarantine" => {
            tools::handle_workspace_quarantine(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_workspace_status" => {
            tools::handle_workspace_status(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_memories" => tools::handle_memories(db, args).map_err(|e| e.to_string()),

        "perseus_vault_migrate" => Ok(tools::handle_migrate(db, args)),

        "perseus_vault_context" => Ok(tools::handle_context(db, args)),

        "perseus_vault_extract" => tools::handle_extract(db, args).map_err(|e| e.to_string()),

        "perseus_vault_capture" => tools::handle_capture(db, args).map_err(|e| e.to_string()),

        "perseus_vault_traverse" => Ok(tools::handle_traverse(db, args)),
        "perseus_vault_score" => Ok(tools::handle_score(db, args)),
        "perseus_vault_follow" => tools::handle_follow(db, args).map_err(|e| e.to_string()),
        "perseus_vault_keystone_set" => tools::handle_keystone_set(db, args),
        "perseus_vault_keystone_get" => tools::handle_keystone_get(db, args),
        "perseus_vault_agent" => tools::handle_agent(db, args),
        "perseus_vault_authority_set" => tools::handle_authority_set(db, args),
        "perseus_vault_authority_set_signed" => tools::handle_authority_set_signed(db, args),
        "perseus_vault_authority_get" => tools::handle_authority_get(db, args),
        "perseus_vault_authority_revoke" => tools::handle_authority_revoke(db, args),
        "perseus_vault_action_intent" => tools::handle_action_intent(db, args),
        "perseus_vault_action_approve" => tools::handle_action_approve(db, args),
        "perseus_vault_action_complete" => tools::handle_action_complete(db, args),
        "perseus_vault_action_resolve_timeout" => tools::handle_action_resolve_timeout(db, args),
        "perseus_vault_action_receipt_get" => tools::handle_action_receipt_get(db, args),
        "perseus_vault_action_lease_acquire" => tools::handle_action_lease_acquire(db, args),
        "perseus_vault_action_lease_release" => tools::handle_action_lease_release(db, args),
        "perseus_vault_stage_trace_validate" => (|| -> Result<String, String> {
            let trace_value = args
                .get("trace")
                .cloned()
                .ok_or_else(|| "stage_trace_validate requires trace".to_string())?;
            let trace: crate::stage_trace::StageTrace = serde_json::from_value(trace_value)
                .map_err(|e| format!("invalid stage trace: {e}"))?;
            trace.validate()?;
            let replay_fingerprint = trace.replay_fingerprint()?;
            let replay_match = if let Some(replay_value) = args.get("replay_of").cloned() {
                let replay: crate::stage_trace::StageTrace =
                    serde_json::from_value(replay_value)
                        .map_err(|e| format!("invalid replay trace: {e}"))?;
                Some(crate::stage_trace::StageTrace::validate_replay(&trace, &replay).is_ok())
            } else {
                None
            };
            serde_json::to_string(&json!({
                "valid": true,
                "trace_digest": trace.digest()?,
                "replay_fingerprint": replay_fingerprint,
                "replay_match": replay_match,
                "schema_version": crate::stage_trace::STAGE_TRACE_SCHEMA_VERSION,
                "stage_count": trace.stages.len(),
            }))
            .map_err(|e| e.to_string())
        })(),
        "perseus_vault_promote" => tools::handle_promote(db, args),
        "perseus_vault_demote" => tools::handle_demote(db, args),
        "perseus_vault_beliefs" => beliefs::handle_beliefs(db, args),
        "perseus_vault_claim_card" => claim_card::handle_claim_card(db, args),
        "perseus_vault_operator_review" => tools::handle_operator_review(db, args),
        "perseus_vault_conflicts" => Ok(tools::handle_conflicts(db, args)),
        "perseus_vault_consolidate" => Ok(tools::handle_consolidate(db, args)),
        "perseus_vault_dream" => tools::handle_dream(db, args),
        "perseus_vault_vault_export" => Ok(tools::handle_vault_export(db, args)),
        "perseus_vault_derived_export" => tools::handle_derived_export(db, args),
        "perseus_vault_markdown_import" => tools::handle_markdown_import(db, args),
        "perseus_vault_structured_index_anchor" => tools::handle_structured_index_anchor(db, args),
        "perseus_vault_vault_import" => Ok(tools::handle_vault_import(db, args)),
        "perseus_vault_decay" => Ok(tools::handle_decay(db, args)),
        "perseus_vault_reindex" => Ok(tools::handle_reindex(db, args)),
        "perseus_vault_share" => tools::handle_share(db, args).map_err(|e| e.to_string()),
        "perseus_vault_federate" => tools::handle_federate(db, args).map_err(|e| e.to_string()),
        "perseus_vault_workspace_list" => Ok(tools::handle_workspace_list(db)),
        "perseus_vault_recall_when" => tools::handle_recall_when(db, args).map_err(|e| e.to_string()),
        "perseus_vault_cohere" => tools::handle_cohere(db, args).map_err(|e| e.to_string()),
        "perseus_vault_correct" => tools::handle_correct(db, args).map_err(|e| e.to_string()),
        "perseus_vault_synthesize" => tools::handle_synthesize(db, args).map_err(|e| e.to_string()),
        "perseus_vault_bench" => tools::handle_bench(db, args).map_err(|e| e.to_string()),

        "perseus_vault_communities" => tools::handle_communities(db, args).map_err(|e| e.to_string()),
        "perseus_vault_community_summary" => {
            tools::handle_community_summary(db, args).map_err(|e| e.to_string())
        }
        "perseus_vault_global_recall" => tools::handle_global_recall(db, args).map_err(|e| e.to_string()),

        "perseus_vault_autocohere" => tools::handle_autocohere(db, args).map_err(|e| e.to_string()),
        "perseus_vault_supersede" => tools::handle_supersede(db, args).map_err(|e| e.to_string()),
        "perseus_vault_maintenance" => tools::handle_maintenance(db, args).map_err(|e| e.to_string()),

        _ => Err(format!("Unknown tool: {}", original_name)),
    };

    // MCP spec §3.3: tool failures must return isError:true in the result,
    // NOT a JSON-RPC protocol error (which is reserved for transport/protocol faults).
    match handler_result {
        Ok(text) => text,
        Err(err_msg) => serde_json::to_string(&json!({
            "content": [{"type": "text", "text": err_msg}],
            "isError": true
        }))
        .unwrap_or_else(|_| {
            format!(
                r#"{{"content":[{{"type":"text","text":"{}"}}],"isError":true}}"#,
                err_msg
            )
        }),
    }
}

fn error_response(id: Option<Value>, code: i64, message: &str) -> JsonRpcResponse {
    JsonRpcResponse {
        jsonrpc: "2.0".to_string(),
        id,
        result: None,
        error: Some(JsonRpcError {
            code,
            message: message.to_string(),
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    /// Tool names advertised by tools/list (the canonical registry).
    fn advertised_names() -> Vec<String> {
        tool_registry_base()
            .iter()
            .map(|t| t["name"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn registry_and_advertised_manifest_are_unique_and_in_sync() {
        let base = tool_registry_base();
        let registry_names: Vec<&str> = base
            .iter()
            .map(|tool| tool["name"].as_str().expect("registry tool name"))
            .collect();
        let registry_set: std::collections::HashSet<&str> =
            registry_names.iter().copied().collect();
        assert_eq!(
            registry_set.len(),
            registry_names.len(),
            "registry names must be unique"
        );
        assert_eq!(
            registry_names.len(),
            100,
            "update public metadata when adding a tool"
        );

        let canonical = advertised_names();
        let canonical_set: std::collections::HashSet<&str> =
            canonical.iter().map(String::as_str).collect();
        assert_eq!(canonical.len(), registry_names.len());
        assert_eq!(
            canonical_set.len(),
            canonical.len(),
            "canonical names must be unique"
        );
        assert!(canonical
            .iter()
            .all(|name| name.starts_with("perseus_vault_")));
    }

    #[test]
    fn journal_scope_attribution_is_advertised_in_schemas() {
        let canonical = tool_registry_base();
        for name in ["perseus_vault_journal", "perseus_vault_timeline"] {
            let tool = canonical
                .iter()
                .find(|tool| tool["name"] == name)
                .unwrap_or_else(|| panic!("missing {name}"));
            if name.ends_with("journal") {
                assert_eq!(tool["inputSchema"]["properties"]["workspace_hash"]["type"], "string");
            } else {
                assert_eq!(
                    tool["outputSchema"]["properties"]["items"]["items"]["properties"]["workspace_hash"]["type"],
                    "string"
                );
            }
        }
    }

    #[test]
    fn stats_schema_allows_null_timestamps_for_an_empty_database() {
        let stats = tool_registry_base()
            .iter()
            .find(|tool| tool["name"] == "perseus_vault_stats")
            .expect("stats tool must be registered");

        for field in ["oldest_unix_ms", "newest_unix_ms"] {
            assert_eq!(
                stats["outputSchema"]["properties"][field]["type"],
                json!(["integer", "null"]),
                "{field} must accept the null value returned for an empty database"
            );
        }
    }

    #[test]
    fn dream_is_registered_and_errors_cleanly_without_llm() {
        assert!(advertised_names().contains(&"perseus_vault_dream".to_string()));

        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-dream-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // No --llm-endpoint configured: the tool must answer with a clean MCP
        // tool error (isError, spec §3.3) — never a crash or protocol error —
        // and the message must name the flag and the non-LLM alternative.
        let r = call_tool("perseus_vault_dream", &db, json!({"category": "episodes"}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true), "got: {r}");
        let msg = v["content"][0]["text"].as_str().unwrap();
        assert!(msg.contains("--llm-endpoint"), "got: {msg}");
        assert!(msg.contains("perseus_vault_consolidate"), "got: {msg}");

        // Opt-in graceful degradation: fallback_consolidate runs the non-LLM
        // consolidate pass instead of erroring, and says so.
        let r = call_tool(
            "perseus_vault_dream",
            &db,
            json!({"fallback_consolidate": true, "dry_run": true}),
            None,
        );
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["fallback"], json!("consolidate"), "got: {r}");
        assert_eq!(v["dry_run"], json!(true));

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn check_failure_pattern_is_registered_and_dispatches() {
        // #521: tools/list must expose the deja-vu guard under the canonical name.
        assert!(advertised_names().contains(&"perseus_vault_check_failure_pattern".to_string()));

        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-fpguard-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Alias prefixes normalize into the same handler; empty store answers
        // with the unambiguous empty state.
        let r = call_tool(
            "perseus_vault_check_failure_pattern",
            &db,
            json!({"action": "cargo build --release", "workspace_hash": ""}),
            None,
        );
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["deja_vu"], json!(false), "got: {r}");
        assert!(
            v["message"]
                .as_str()
                .unwrap()
                .contains("no prior failures recorded matching this action"),
            "got: {r}"
        );

        // Missing required `action` → clean MCP tool error (isError, §3.3).
        let r = call_tool("perseus_vault_check_failure_pattern", &db, json!({}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true), "got: {r}");

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn capture_is_registered_and_dispatches() {
        // #520: tools/list must expose the capture pipeline under the
        // canonical name.
        assert!(advertised_names().contains(&"perseus_vault_capture".to_string()));

        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-capture-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // A real payload distills and writes through the remember path.
        let r = call_tool(
            "perseus_vault_capture",
            &db,
            json!({"text": "The deploy failed because the schema version was never bumped."}),
            None,
        );
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["captured"], json!(1), "got: {r}");
        assert_eq!(v["created"], json!(1), "got: {r}");
        assert_eq!(v["notes"][0]["type"], json!("root-cause"), "got: {r}");

        // Empty payload → clean MCP tool error (isError, spec §3.3).
        let r = call_tool("perseus_vault_capture", &db, json!({"text": "  "}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true), "got: {r}");

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn memories_adapter_full_lifecycle_roundtrip() {
        // The Anthropic /memories directory convention over vault entities:
        // create, list, view (numbered), str_replace (unique-match), insert,
        // rename, delete, and recreate-after-delete (revival must also
        // restore the FTS row so the file is searchable again).
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-memories-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let call = |args: Value| -> String {
            call_tool("perseus_vault_memories", &db, args, None)
        };

        // create
        let r = call(json!({"command": "create", "path": "/memories/notes.md",
                            "file_text": "alpha\nbeta\ngamma"}));
        assert!(r.contains("created"), "create failed: {r}");

        // view directory
        let r = call(json!({"command": "view", "path": "/memories"}));
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["files"], json!(["notes.md"]), "dir listing: {r}");

        // view file — numbered content
        let r = call(json!({"command": "view", "path": "/memories/notes.md"}));
        assert!(r.contains("beta"), "view content missing: {r}");
        let v: Value = serde_json::from_str(&r).unwrap();
        assert!(
            v["content"].as_str().unwrap().contains("     2\tbeta"),
            "expected cat -n numbering: {r}"
        );

        // str_replace — must reject ambiguous and missing matches
        let r = call(json!({"command": "str_replace", "path": "/memories/notes.md",
                            "old_str": "beta", "new_str": "BETA"}));
        assert!(r.contains("replaced"), "str_replace failed: {r}");
        let r = call(json!({"command": "str_replace", "path": "/memories/notes.md",
                            "old_str": "missing", "new_str": "x"}));
        assert!(r.contains("not found"), "missing old_str must error: {r}");

        // insert at line 0
        let r = call(json!({"command": "insert", "path": "/memories/notes.md",
                            "insert_line": 0, "insert_text": "header"}));
        assert!(r.contains("inserted"), "insert failed: {r}");
        let r = call(json!({"command": "view", "path": "/memories/notes.md"}));
        let v: Value = serde_json::from_str(&r).unwrap();
        assert!(
            v["content"].as_str().unwrap().starts_with("     1\theader"),
            "insert at 0 must lead the file: {r}"
        );

        // rename
        let r = call(json!({"command": "rename", "old_path": "/memories/notes.md",
                            "new_path": "/memories/archive/notes.md"}));
        assert!(r.contains("renamed"), "rename failed: {r}");
        let r = call(json!({"command": "view", "path": "/memories"}));
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["files"], json!(["archive/notes.md"]), "post-rename listing: {r}");

        // path traversal is rejected
        let r = call(json!({"command": "view", "path": "/memories/../etc/passwd"}));
        assert!(r.contains("invalid path") || r.contains("error"), "traversal must be rejected: {r}");

        // delete, then recreate: revival must restore searchability (the FTS
        // row is deleted by forget; the remember update path must re-insert it).
        let r = call(json!({"command": "delete", "path": "/memories/archive/notes.md"}));
        assert!(r.contains("deleted"), "delete failed: {r}");
        let r = call(json!({"command": "create", "path": "/memories/archive/notes.md",
                            "file_text": "reborn searchable zanzibar"}));
        assert!(r.contains("created"), "recreate failed: {r}");
        let hits = db
            .recall(&crate::models::RecallParams {
                query: "zanzibar".to_string(),
                skip_side_effects: true,
                ..crate::models::RecallParams::default()
            })
            .unwrap();
        assert!(
            hits.iter().any(|e| e.key == "archive/notes.md"),
            "revived file must be FTS-searchable again"
        );

        drop(db);
        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn bitemporal_tools_are_registered_and_dispatch() {
        // #363: perseus_vault_valid_at / perseus_vault_bitemporal exist in the
        // registry and dispatch through call_tool.
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-bitemporal-tools-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        let names = advertised_names();
        for expect in [
            "perseus_vault_valid_at",
            "perseus_vault_bitemporal",
        ] {
            assert!(names.contains(&expect.to_string()), "missing tool {expect}");
        }

        // Round-trip through call_tool.
        let stored = call_tool(
            "perseus_vault_remember",
            &db,
            json!({"category": "f", "key": "k", "body_json": "{\"note\":\"x\"}",
                   "valid_from_unix_ms": 1000}),
            None,
        );
        assert!(stored.contains("created"), "{stored}");
        for prefix in ["perseus_vault"] {
            let r = call_tool(
                &format!("{prefix}_valid_at"),
                &db,
                json!({"category": "f", "key": "k", "valid_at_unix_ms": 2000}),
                None,
            );
            assert!(r.contains("\"found\":true"), "{prefix}_valid_at: {r}");
            let b = call_tool(
                &format!("{prefix}_bitemporal"),
                &db,
                json!({"category": "f", "key": "k",
                       "tx_at_unix_ms": now_ms_for_test(), "valid_at_unix_ms": 2000}),
                None,
            );
            assert!(b.contains("\"found\":true"), "{prefix}_bitemporal: {b}");
        }

        let _ = fs::remove_file(&db_path);
    }

    fn now_ms_for_test() -> i64 {
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis() as i64
    }

    #[test]
    fn keystone_tools_register_dispatch_order_and_gate() {
        // #683: keystones are registered, round-trip through
        // call_tool, merge by weight, are updated in place on re-set, gate on
        // trust tier, and every mutation lands on the audit chain.
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-keystones-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Registered under the canonical prefix.
        for expect in [
            "perseus_vault_keystone_set",
            "perseus_vault_keystone_get",
        ] {
            assert!(advertised_names().contains(&expect.to_string()), "missing tool {expect}");
        }

        // Author two keystones with different weights (author tier satisfies).
        let low = call_tool(
            "perseus_vault_keystone_set",
            &db,
            json!({"content": "cite source memory IDs", "scope": "tenant",
                   "weight": 1.0, "author_trust_tier": 2}),
            None,
        );
        assert!(low.contains("\"created\":true"), "{low}");
        // #684: caller-asserted (no registered agent) → checked but not
        // registry-enforced. trust_enforced reflects registry backing only.
        assert!(low.contains("\"trust_enforced\":false"), "{low}");
        let _ = call_tool(
            "perseus_vault_keystone_set",
            &db,
            json!({"content": "PII MUST NOT cross agent boundaries", "scope": "fleet",
                   "scope_id": "sec", "weight": 9.0, "author_trust_tier": 3}),
            None,
        );

        // get merges both, highest weight first.
        let got = call_tool("perseus_vault_keystone_get", &db, json!({}), None);
        let v: Value = serde_json::from_str(&got).unwrap();
        assert_eq!(v["count"], json!(2), "{got}");
        assert_eq!(v["keystones"][0]["content"], json!("PII MUST NOT cross agent boundaries"));
        assert_eq!(v["keystones"][1]["content"], json!("cite source memory IDs"));

        // Re-setting the same (scope, scope_id, content) updates in place.
        let again = call_tool(
            "perseus_vault_keystone_set",
            &db,
            json!({"content": "cite source memory IDs", "scope": "tenant",
                   "weight": 5.0, "author_trust_tier": 2}),
            None,
        );
        assert!(again.contains("\"created\":false"), "re-set must update: {again}");
        let got2 = call_tool("perseus_vault_keystone_get", &db, json!({}), None);
        let v2: Value = serde_json::from_str(&got2).unwrap();
        assert_eq!(v2["count"], json!(2), "no duplicate row on re-set: {got2}");

        // Trust gate: asserting tier below required is rejected.
        let denied = call_tool(
            "perseus_vault_keystone_set",
            &db,
            json!({"content": "denied rule", "author_trust_tier": 1}),
            None,
        );
        assert!(denied.contains("insufficient trust tier"), "{denied}");
        // Omitting the tier is allowed but flagged as unenforced.
        let unenforced = call_tool(
            "perseus_vault_keystone_set",
            &db,
            json!({"content": "unenforced rule", "scope": "agent", "scope_id": "a1"}),
            None,
        );
        assert!(unenforced.contains("\"trust_enforced\":false"), "{unenforced}");

        // Every keystone_set is crypto-chained (event_type keystone_set) and the
        // chain still verifies.
        let chained: i64 = db
            .conn()
            .unwrap()
            .query_row(
                "SELECT COUNT(*) FROM journal WHERE event_type = 'keystone_set'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(chained, 4, "one audit event per successful set (3 create + 1 update); the trust-denied set emits none");
        assert!(
            crate::db::verify_audit_chain(&db).is_ok(),
            "audit chain must verify after keystone mutations"
        );

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn stage_trace_tool_validates_hash_only_replay_contract() {
        let db_path = std::env::temp_dir()
            .join(format!("perseus-stage-trace-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let trace = crate::stage_trace::StageTrace::new("trace-mcp", "workspace-a")
            .seal()
            .expect("empty trace is a valid fixture");
        let response = call_tool(
            "perseus_vault_stage_trace_validate",
            &db,
            json!({
                "trace": serde_json::to_value(&trace).unwrap(),
                "replay_of": serde_json::to_value(&trace).unwrap()
            }),
            None,
        );
        let value: Value = serde_json::from_str(&response).expect("structured response");
        assert_eq!(value["valid"], true, "{response}");
        assert_eq!(value["replay_match"], true, "{response}");
        assert!(advertised_names().contains(&"perseus_vault_stage_trace_validate".to_string()));
        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn rejected_value_tombstones_block_laundering_and_support_audited_override() {
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-rejected-value-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Reject the value explicitly with the new tool. The value is the
        // canonical body string the writer would store (digest matching is
        // case/whitespace-insensitive, so any equivalent spelling matches).
        let reject = call_tool(
            "perseus_vault_reject_value",
            &db,
            json!({
                "workspace_hash": "ws-a",
                "subject": "never-use-x",
                "predicate": "convention",
                "value": "{\"note\": \"Always use X for everything\"}",
                "reason": "user correction",
                "author_agent_id": "test-agent"
            }),
            None,
        );
        assert!(reject.contains("\"rejected\":true"), "reject must succeed: {reject}");

        // A normal remember with the same body is blocked, even under a
        // brand-new key and different (subject, predicate) spelling: that is
        // the laundering prevention (the gate matches on value digest within
        // the workspace scope).
        let blocked = call_tool(
            "perseus_vault_remember",
            &db,
            json!({
                "category": "convention",
                "key": "totally-different-key",
                "workspace_hash": "ws-a",
                "body_json": "{\"note\": \"Always use X for everything\"}"
            }),
            None,
        );
        assert!(blocked.contains("rejected"), "laundered write must be blocked: {blocked}");

        // Case/whitespace variants of the rejected value are equivalent.
        let blocked_variant = call_tool(
            "perseus_vault_remember",
            &db,
            json!({
                "category": "convention",
                "key": "another-key",
                "workspace_hash": "ws-a",
                "body_json": "{ \"note\":  \"always  use x  for everything\" }"
            }),
            None,
        );
        assert!(
            blocked_variant.contains("rejected"),
            "equivalent value variant must be blocked: {blocked_variant}"
        );

        // A different value for the same key is NOT blocked.
        let fine = call_tool(
            "perseus_vault_remember",
            &db,
            json!({
                "category": "convention",
                "key": "totally-different-key",
                "workspace_hash": "ws-a",
                "body_json": "{\"note\": \"Use Y instead\"}"
            }),
            None,
        );
        assert!(fine.contains("\"action\":\"created\""), "unrejected value must write: {fine}");

        // Scope isolation: the same value re-ingested in a DIFFERENT workspace
        // is not poisoned by the ws-a tombstone.
        let other_ws = call_tool(
            "perseus_vault_remember",
            &db,
            json!({
                "category": "convention",
                "key": "ws-b-key",
                "workspace_hash": "ws-b",
                "body_json": "{\"note\": \"Always use X for everything\"}"
            }),
            None,
        );
        assert!(
            other_ws.contains("\"action\":\"created\""),
            "different workspace must not be poisoned: {other_ws}"
        );

        // The deliberate override writes through and is journaled.
        let override_ok = call_tool(
            "perseus_vault_remember",
            &db,
            json!({
                "category": "convention",
                "key": "never-use-x",
                "workspace_hash": "ws-a",
                "body_json": "{\"note\": \"Always use X for everything\"}",
                "allow_rejected": true
            }),
            None,
        );
        assert!(override_ok.contains("\"action\":\"created\""), "override must write: {override_ok}");

        let rejected_events: i64 = {
            let conn = db.conn().expect("db connection");
            conn.query_row(
                "SELECT COUNT(*) FROM journal WHERE event_type = 'rejected_write'",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0)
        };
        assert!(rejected_events >= 1, "rejected write must be journaled");

        let override_events: i64 = {
            let conn = db.conn().expect("db connection");
            conn.query_row(
                "SELECT COUNT(*) FROM journal WHERE event_type = 'rejected_write_override'",
                [],
                |r| r.get(0),
            )
            .unwrap_or(0)
        };
        assert!(
            override_events >= 1,
            "trusted override must be journaled for audit"
        );

        // Correcting a wrong approach records a tombstone and the correction
        // itself still writes.
        let corrected = call_tool(
            "perseus_vault_correct",
            &db,
            json!({
                "workspace_hash": "ws-a",
                "wrong_approach": "Used X everywhere",
                "user_correction": "Prefer Y",
                "task_context": "choose strategy",
                "agent_id": "test-agent"
            }),
            None,
        );
        assert!(corrected.contains("entity_id"), "correction must write: {corrected}");
        let corr_val: Value = serde_json::from_str(&corrected).expect("correction response JSON");
        let corr_id = corr_val["entity_id"].as_str().expect("entity_id").to_string();
        let corr_key = format!("correction-{}", &corr_id[4..16]);
        assert!(
            db.is_value_rejected("ws-a", &corr_key, "correction", "Used X everywhere")
                .unwrap(),
            "correction must leave a scoped tombstone on its own key"
        );

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn unknown_tool_error_reports_the_caller_name() {
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-unknown-tool-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // An unknown tool name is reported verbatim — no prefix normalization.
        let result = call_tool("perseus_vault_bogus", &db, json!({}), None);
        assert!(result.contains("Unknown tool: perseus_vault_bogus"), "got: {result}");

        let other = call_tool("custom_bogus", &db, json!({}), None);
        assert!(other.contains("Unknown tool: custom_bogus"), "got: {other}");

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn rejects_non_json_rpc_2_requests() {
        let db_path =
            std::env::temp_dir().join(format!("perseus_vault-jsonrpc-version-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let req = JsonRpcRequest {
            jsonrpc: "1.0".to_string(),
            id: Some(json!(1)),
            method: "initialize".to_string(),
            params: None,
        };
        let state = MCPState::new();

        let resp = handle_request(&req, &state, &db).expect("error response");
        assert_eq!(resp.error.expect("json-rpc error").code, -32600);
        assert!(!state.initialized.load(std::sync::atomic::Ordering::Relaxed));

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn initialize_reports_the_current_crate_name_not_a_hardcoded_one() {
        // Regression: serverInfo.name was a hardcoded literal that went stale
        // across the earlier product renames. It must track Cargo.toml's
        // package name instead.
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-initialize-name-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let req = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(1)),
            method: "initialize".to_string(),
            params: None,
        };
        let state = MCPState::new();

        let resp = handle_request(&req, &state, &db).expect("initialize response");
        let result = resp.result.expect("initialize result");
        assert_eq!(
            result["serverInfo"]["name"],
            json!(env!("CARGO_PKG_NAME")),
        );

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn initialize_captures_client_identity_and_scopes_recall() {
        // #684: the initialize handshake's clientInfo.name is captured and
        // stamped onto tool calls as requesting_agent_id, so a private entity is
        // transparently hidden from a different client — no explicit arg needed.
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-clientinfo-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        db.agent_upsert("alice", "Alice", 0, "eng").unwrap();
        db.agent_upsert("bob", "Bob", 0, "eng").unwrap();
        tools::handle_remember(
            &db,
            json!({"category": "notes", "key": "secret",
                   "body_json": "{\"note\":\"quantum blueprint\"}",
                   "visibility": "private", "agent_id": "alice"}),
        )
        .expect("private note");

        let state = MCPState::new();
        // Handshake as client "bob".
        let init = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(1)),
            method: "initialize".to_string(),
            params: Some(json!({"clientInfo": {"name": "bob", "version": "1.0"}})),
        };
        handle_request(&init, &state, &db).expect("initialize");
        assert_eq!(
            *state.session_agent_id.read().unwrap(),
            "bob",
            "clientInfo.name must be captured"
        );

        // A plain recall (no requesting_agent_id arg) is transparently scoped to bob.
        let call = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(2)),
            method: "tools/call".to_string(),
            params: Some(json!({
                "name": "perseus_vault_recall",
                "arguments": {"query": "quantum", "mode": "fts5"}
            })),
        };
        let resp = handle_request(&call, &state, &db).expect("recall response");
        let structured = resp.result.expect("result")["structuredContent"].clone();
        assert_eq!(
            structured["total"],
            json!(0),
            "bob must not see alice's private note via the captured identity: {structured}"
        );
        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn transport_host_identity_overrides_forged_requesting_agent_id() {
        // #855 review: even when the caller forges a requesting_agent_id
        // (or passes an empty one), the transport overwrites it with the
        // captured clientInfo.name — a model cannot claim another identity.
        let db_path = std::env::temp_dir()
            .join(format!("perseus_vault-forged-id-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        let state = MCPState::new();
        let init = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(1)),
            method: "initialize".to_string(),
            params: Some(json!({"clientInfo": {"name": "host-bob", "version": "1.0"}})),
        };
        handle_request(&init, &state, &db).expect("initialize");

        // Forge: the model claims to be "mallory" in both author and host
        // fields. The transport must replace requesting_agent_id with host-bob.
        let call = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(2)),
            method: "tools/call".to_string(),
            params: Some(json!({
                "name": "perseus_vault_correct",
                "arguments": {
                    "wrong_approach": "assistant guessed the state",
                    "user_correction": "user corrected",
                    "task_context": "review",
                    "agent_id": "mallory",
                    "requesting_agent_id": "mallory"
                }
            })),
        };
        let resp = handle_request(&call, &state, &db).expect("correct response");
        let structured = resp.result.expect("result")["structuredContent"].clone();
        assert_eq!(
            structured["agent_id"],
            json!("host-bob"),
            "host identity must override the forged id: {structured}"
        );
        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn recall_confidence_is_opt_in_and_normalized() {
        let db_path =
            std::env::temp_dir().join(format!("perseus_vault-confidence-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        tools::handle_remember(
            &db,
            json!({"category": "demo", "key": "k1", "body_json": "{\"content\":\"alpha bravo\"}"}),
        )
        .expect("remember");

        // Default: confidence is absent (opt-in, non-breaking).
        let plain = tools::handle_recall(&db, json!({"query": "alpha"})).expect("recall");
        let plain_v: Value = serde_json::from_str(&plain).unwrap();
        assert!(
            plain_v["items"][0].get("confidence").is_none(),
            "confidence must be opt-in"
        );

        // Opt-in: confidence present and normalized to [0,1].
        let withc =
            tools::handle_recall(&db, json!({"query": "alpha", "include_confidence": true}))
                .expect("recall");
        let withc_v: Value = serde_json::from_str(&withc).unwrap();
        let c = withc_v["items"][0]["confidence"]
            .as_f64()
            .expect("confidence number");
        assert!((0.0..=1.0).contains(&c), "confidence {} out of range", c);

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn history_tool_lists_superseded_versions() {
        let db_path =
            std::env::temp_dir().join(format!("perseus_vault-history-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        tools::handle_remember(
            &db,
            json!({"category":"facts","key":"color","body_json":"{\"content\":\"blue\"}"}),
        )
        .expect("v1");
        // A content change snapshots the prior version into history.
        tools::handle_remember(
            &db,
            json!({"category":"facts","key":"color","body_json":"{\"content\":\"green\"}"}),
        )
        .expect("v2");

        let resp =
            tools::handle_history(&db, json!({"category":"facts","key":"color"})).expect("history");
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["total"].as_i64().unwrap(), 1, "one superseded version: {}", resp);
        let body = v["versions"][0]["content"]
            .as_str()
            .or_else(|| v["versions"][0]["body_json"].as_str())
            .unwrap_or("");
        assert!(body.contains("blue"), "history should hold the old 'blue' value: {}", resp);

        // Unknown key -> empty trail.
        let empty =
            tools::handle_history(&db, json!({"category":"facts","key":"nope"})).expect("history");
        let ev: Value = serde_json::from_str(&empty).unwrap();
        assert_eq!(ev["total"].as_i64().unwrap(), 0);

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn graphrag_tools_dispatch_including_aliases() {
        // #365: the three GraphRAG tools must be dispatchable under the
        // canonical perseus_vault_* name and both rename aliases, and must appear in
        // tools/list.
        let db_path =
            std::env::temp_dir().join(format!("perseus_vault-graphrag-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Two linked entities so detection has a community to find.
        tools::handle_remember(
            &db,
            json!({"category":"g","key":"n1","body_json":"{\"content\":\"quasar telescope\"}"}),
        )
        .expect("remember n1");
        tools::handle_remember(
            &db,
            json!({"category":"g","key":"n2","body_json":"{\"content\":\"nebula filter rig\"}"}),
        )
        .expect("remember n2");
        let n2 = db.get_entity("g", "n2").unwrap().expect("n2 exists");
        db.link("g", "n1", &n2.id, "related").expect("link");

        let detect = call_tool("perseus_vault_communities", &db, json!({}), None);
        let v: Value = serde_json::from_str(&detect).expect("valid JSON");
        assert_eq!(v["communities"].as_array().unwrap().len(), 1, "got: {detect}");
        let cid = v["communities"][0]["id"].as_str().unwrap().to_string();

        // Dispatch via the canonical names.
        let summary = call_tool(
            "perseus_vault_community_summary",
            &db,
            json!({"community_id": cid}),
            None,
        );
        let sv: Value = serde_json::from_str(&summary).expect("valid JSON");
        assert_eq!(sv["community_id"].as_str().unwrap(), cid, "got: {summary}");
        assert!(sv.get("isError").is_none(), "got: {summary}");

        let recall = call_tool("perseus_vault_global_recall", &db, json!({"query": "quasar"}), None);
        let rv: Value = serde_json::from_str(&recall).expect("valid JSON");
        assert!(rv.get("isError").is_none(), "got: {recall}");
        assert_eq!(rv["communities"].as_array().unwrap().len(), 1, "got: {recall}");

        // tools/list advertises the graph tools under the canonical prefix.
        let names = advertised_names();
        for tool in [
            "perseus_vault_communities",
            "perseus_vault_community_summary",
            "perseus_vault_global_recall",
        ] {
            assert!(names.contains(&tool.to_string()), "must advertise {tool}");
        }

        drop(db);
        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn recall_layer_filter_scopes_by_canonical_and_alias() {
        let db_path =
            std::env::temp_dir().join(format!("perseus_vault-layerfilter-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        tools::handle_remember(
            &db,
            json!({"category":"demo","key":"a","body_json":"{\"content\":\"alpha core fact\"}","layer":"core"}),
        )
        .expect("remember a");
        tools::handle_remember(
            &db,
            json!({"category":"demo","key":"b","body_json":"{\"content\":\"alpha working fact\"}","layer":"working"}),
        )
        .expect("remember b");

        let keys = |resp: &str| -> Vec<String> {
            let v: Value = serde_json::from_str(resp).unwrap();
            v["items"]
                .as_array()
                .unwrap()
                .iter()
                .map(|i| i["key"].as_str().unwrap().to_string())
                .collect()
        };

        // Canonical "core" -> only entity a.
        let core =
            tools::handle_recall(&db, json!({"query":"alpha","layer":"core"})).expect("recall");
        let ck = keys(&core);
        assert!(
            ck.contains(&"a".to_string()) && !ck.contains(&"b".to_string()),
            "core filter returned {:?}",
            ck
        );

        // Alias "semantic" -> "working" -> only entity b.
        let sem =
            tools::handle_recall(&db, json!({"query":"alpha","layer":"semantic"})).expect("recall");
        let sk = keys(&sem);
        assert!(
            sk.contains(&"b".to_string()) && !sk.contains(&"a".to_string()),
            "semantic->working filter returned {:?}",
            sk
        );

        // No layer filter -> both.
        let all = tools::handle_recall(&db, json!({"query":"alpha"})).expect("recall");
        assert_eq!(keys(&all).len(), 2, "no filter should return both");

        let _ = fs::remove_file(db_path);
    }

    #[test]
    fn artifact_tools_advertise_canonical_names_and_dispatch() {
        let names = advertised_names();
        for name in [
            "perseus_vault_artifact_register",
            "perseus_vault_artifact_manifest",
            "perseus_vault_artifact_excerpt",
            "perseus_vault_artifact_log_digest",
            "perseus_vault_artifact_verify_value",
        ] {
            assert!(names.contains(&name.to_string()), "canonical list must advertise {name}");
        }

        let db_path = std::env::temp_dir().join(format!("perseus_vault-artifact-tools-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let source = std::env::temp_dir().join(format!("artifact-mcp-{}.txt", uuid::Uuid::new_v4()));
        std::fs::write(&source, "artifact via MCP\n").unwrap();

        let registered = call_tool(
            "perseus_vault_artifact_register",
            &db,
            json!({"path": source.to_string_lossy(), "workspace_hash": "ws-mcp"}),
            None,
        );
        let rv: Value = serde_json::from_str(&registered).unwrap();
        let sha = rv["sha256"].as_str().unwrap();

        let manifest = call_tool(
            "perseus_vault_artifact_manifest",
            &db,
            json!({"sha256": sha, "workspace_hash": "ws-mcp"}),
            None,
        );
        let mv: Value = serde_json::from_str(&manifest).unwrap();
        assert_eq!(mv["sha256"], json!(sha));

        let _ = fs::remove_file(db_path);
        let _ = std::fs::remove_file(source);
    }

    #[test]
    fn idle_timeout_is_opt_in_since_748() {
        use std::time::Duration;
        // Unset -> DISABLED (default off): inactivity is not abandonment; a
        // quiet-but-alive host (Claude Desktop) must never be reaped. Orphans
        // are caught by parent-death detection, not a flat timer.
        assert_eq!(parse_idle_timeout(None), None);
        // Explicit "0" -> disabled (same as unset).
        assert_eq!(parse_idle_timeout(Some("0")), None);
        // Explicit value -> honored (opt-in aggressive reaping for hosts that
        // leak the stdin write-end while staying alive, the #57228 topology).
        assert_eq!(parse_idle_timeout(Some("30")), Some(Duration::from_secs(30)));
        // Whitespace tolerated.
        assert_eq!(
            parse_idle_timeout(Some(" 120 ")),
            Some(Duration::from_secs(120))
        );
        // Garbage -> disabled (never silently re-enables the flat timer),
        // never panics.
        assert_eq!(parse_idle_timeout(Some("banana")), None);
    }

    #[test]
    fn is_orphaned_by_ppid_returns_false_in_test_process() {
        // The test runner's parent is not init (ppid 1), so this must be false.
        // This is a baseline sanity check; it also confirms the function does not
        // panic and returns the correct type on the current platform.
        assert!(
            !super::is_orphaned_by_ppid(),
            "test process should not have ppid==1"
        );
    }

    /// Verify that `is_orphaned_by_ppid` distinguishes a reparented orphan from
    /// a process legitimately born under a PID-1 init.
    ///
    /// We can't kill the real parent in a unit test, so we model the decision
    /// directly against the documented contract:
    ///   orphaned  <=>  current_ppid == 1  AND  baseline_ppid != 1
    ///
    /// This is the exact logic that fixes the demo-container crash loop, where a
    /// server born under a PID-1 entrypoint (baseline == 1) was falsely reaped by
    /// the old `getppid() == 1` guard. Full end-to-end orphan detection (spawn a
    /// child, kill the parent, observe reparenting) is left to manual/integration
    /// verification since a unit test cannot reparent itself.
    #[test]
    fn is_orphaned_by_ppid_contract() {
        // Pure decision function mirroring is_orphaned_by_ppid's Linux branch.
        fn decide(current_ppid: i32, baseline_ppid: i32) -> bool {
            current_ppid == 1 && baseline_ppid != 1
        }

        // Born under a real parent, later reparented to init => orphaned.
        assert!(decide(1, 4242), "reparented-to-init must be treated as orphaned");

        // Born directly under PID 1 (container entrypoint) and still there =>
        // NOT an orphan. This is the demo-container regression case.
        assert!(
            !decide(1, 1),
            "process born under PID-1 init must NOT be treated as orphaned"
        );

        // Normal case: real, unchanged parent => not orphaned.
        assert!(!decide(4242, 4242), "live parent must not be treated as orphaned");

        // Sanity: the live function never fires in a normal test environment
        // (the test runner's parent is never init).
        assert!(
            !super::is_orphaned_by_ppid(),
            "ppid should not be 1 in a normal test environment"
        );
    }

    /// The baseline recorder must be idempotent and safe to call, and after
    /// recording, a normal test process (real parent, not init) must not be
    /// considered orphaned.
    #[test]
    fn record_initial_ppid_is_idempotent_and_safe() {
        super::record_initial_ppid();
        super::record_initial_ppid(); // second call must not panic
        assert!(
            !super::is_orphaned_by_ppid(),
            "after recording baseline, a process with a live parent is not orphaned"
        );
    }

    #[test]
    fn call_boundary_enforces_profile_workspace_bindings() {
        // #879 end-to-end: the transport-captured clientInfo.name is the
        // profile identity; a read_only binding denies mutations and a bound
        // profile cannot touch another workspace — the denial surfaces as an
        // isError at the tools/call boundary.
        let db_path = std::env::temp_dir()
            .join(format!("perseus-vault-binding-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        db.workspace_bind("profile-ro", "ws-own", "read_only", "{}", "operator")
            .unwrap();

        let state = MCPState::new();
        // Handshake as the bound read_only profile.
        let init = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(1)),
            method: "initialize".to_string(),
            params: Some(json!({"clientInfo": {"name": "profile-ro", "version": "1.0"}})),
        };
        handle_request(&init, &state, &db).expect("initialize");
        assert_eq!(*state.session_agent_id.read().unwrap(), "profile-ro");

        // Mutation in the bound (read_only) workspace -> denied.
        let call = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(2)),
            method: "tools/call".to_string(),
            params: Some(json!({
                "name": "perseus_vault_remember",
                "arguments": {"category": "decision", "key": "k",
                              "body_json": "{\"v\":1}", "workspace_hash": "ws-own"}
            })),
        };
        let resp = handle_request(&call, &state, &db).expect("remember response");
        let result = resp.result.expect("result");
        assert_eq!(result["isError"], json!(true), "{result}");
        assert!(
            result["content"][0]["text"]
                .as_str()
                .unwrap()
                .contains("read_only"),
            "{result}"
        );

        // Mutation in a DIFFERENT workspace -> denied (cross-workspace).
        let call = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(3)),
            method: "tools/call".to_string(),
            params: Some(json!({
                "name": "perseus_vault_remember",
                "arguments": {"category": "decision", "key": "k2",
                              "body_json": "{\"v\":2}", "workspace_hash": "ws-other"}
            })),
        };
        let resp = handle_request(&call, &state, &db).expect("remember response");
        let result = resp.result.expect("result");
        assert_eq!(result["isError"], json!(true), "{result}");
        assert!(
            result["content"][0]["text"]
                .as_str()
                .unwrap()
                .contains("cross-workspace"),
            "{result}"
        );

        // Reads within the bound workspace stay allowed.
        let call = JsonRpcRequest {
            jsonrpc: "2.0".to_string(),
            id: Some(json!(4)),
            method: "tools/call".to_string(),
            params: Some(json!({
                "name": "perseus_vault_recall",
                "arguments": {"query": "anything", "mode": "fts5", "workspace_hash": "ws-own"}
            })),
        };
        let resp = handle_request(&call, &state, &db).expect("recall response");
        assert!(resp.result.expect("result").get("isError").is_none(), "read must pass");

        let _ = std::fs::remove_file(db_path);
    }
}
