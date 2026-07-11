use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::sync::OnceLock;

use crate::db::Database;
use crate::tools;

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

/// Record the current parent PID as the baseline. Call once, as early as
/// possible in `run_server`, before entering the request loop. Idempotent:
/// only the first call sets the baseline.
pub fn record_initial_ppid() {
    #[cfg(target_os = "linux")]
    {
        let _ = INITIAL_PPID.set(unsafe { libc::getppid() });
    }
    #[cfg(not(target_os = "linux"))]
    {
        let _ = INITIAL_PPID.set(0);
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
/// Exposed as `pub` so the orphan case can be unit-tested without needing to
/// actually kill a parent process.
pub fn is_orphaned_by_ppid() -> bool {
    // Safety: getppid() is always safe — no undefined behaviour, no allocation.
    #[cfg(target_os = "linux")]
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
    #[cfg(not(target_os = "linux"))]
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
}

impl MCPState {
    pub fn new() -> Self {
        MCPState {
            initialized: std::sync::atomic::AtomicBool::new(false),
        }
    }
}

/// Parse the `MIMIR_IDLE_TIMEOUT_SECS` env value into an idle-watchdog duration.
///
/// - unset / unparseable  -> default 600s (Some)
/// - "0"                  -> disabled (None)
/// - "N"                  -> Some(N seconds)
///
/// Factored out of `run_server` so the orphan-leak guard (#57228) is unit-tested.
pub fn parse_idle_timeout(raw: Option<&str>) -> Option<std::time::Duration> {
    match raw {
        Some(v) => match v.trim().parse::<u64>() {
            Ok(0) => None,
            Ok(secs) => Some(std::time::Duration::from_secs(secs)),
            Err(_) => Some(std::time::Duration::from_secs(600)),
        },
        None => Some(std::time::Duration::from_secs(600)),
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

    // Idle watchdog (fixes NousResearch/hermes-agent#57228 from the server side).
    //
    // A stdio MCP server that receives ZERO traffic for `idle_timeout` is, by
    // definition, an abandoned/orphaned child: its client (a long-lived Hermes
    // worker) reconnected and leaked the write-end of this pipe, so we will never
    // see EOF and would otherwise block in the read forever — accumulating one
    // orphan per reconnect until SQLite handle contention makes the vault appear
    // "down". An ACTIVE client always issues a tools/call (or at least a ping)
    // well within the window, so it is never affected; an orphan self-terminates
    // and frees its DB handle. Override with MIMIR_IDLE_TIMEOUT_SECS (0 disables).
    let idle_timeout: Option<std::time::Duration> =
        parse_idle_timeout(std::env::var("MIMIR_IDLE_TIMEOUT_SECS").ok().as_deref());

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

    eprintln!("mimir: MCP server ready");

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
        unsafe {
            // PR_SET_PDEATHSIG = 1; SIGTERM = 15.  Using the raw constants
            // avoids pulling in the full `nix` crate just for this call.
            libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM, 0, 0, 0);
        }
        if is_orphaned_by_ppid() {
            eprintln!("mimir: parent already dead at server start — exiting (orphan-reap race guard, #547)");
            return;
        }
    }

    loop {
        let line = match idle_timeout {
            Some(timeout) => match rx.recv_timeout(timeout) {
                Ok(l) => l,
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    eprintln!(
                        "mimir: no client activity for {}s — exiting idle stdio server (orphan-leak guard, #57228)",
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
                eprintln!("mimir: stdin read error: {}", e);
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
            eprintln!("mimir: ppid == 1 detected — parent died, exiting (orphan-reap, #547)");
            break;
        }

        let request: JsonRpcRequest = match serde_json::from_str(&line) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("mimir: JSON parse error: {} in line: {}", e, line);
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
                        // stale branding like it did across Mimir -> Mneme ->
                        // Perseus Vault (this was hardcoded to "mimir" the
                        // whole time).
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

            let tool_args = params.get("arguments").cloned().unwrap_or(json!({}));

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

/// Given a `mimir_*` tool definition from the static registry, return a clone
/// advertised under the equivalent `mneme_*` name (Mneme rename, transition
/// release — both names dispatch to the same handler via `call_tool`).
/// Returns `None` for entries that, unexpectedly, aren't named `mimir_*`.
fn mneme_alias_tool(tool: &serde_json::Value) -> Option<serde_json::Value> {
    let name = tool.get("name")?.as_str()?;
    let suffix = name.strip_prefix("mimir_")?;
    let mut alias = tool.clone();
    alias["name"] = serde_json::Value::String(format!("mneme_{}", suffix));
    Some(alias)
}

/// Given a `mimir_*` tool definition from the static registry, return a clone
/// advertised under the equivalent `perseus_vault_*` name (Perseus Vault
/// rename, transition release — all three names dispatch to the same handler
/// via `call_tool`). Returns `None` for entries that aren't named `mimir_*`.
fn perseus_vault_alias_tool(tool: &serde_json::Value) -> Option<serde_json::Value> {
    let name = tool.get("name")?.as_str()?;
    let suffix = name.strip_prefix("mimir_")?;
    let mut alias = tool.clone();
    alias["name"] = serde_json::Value::String(format!("perseus_vault_{}", suffix));
    Some(alias)
}

/// Parse-once cache of the canonical tool registry. Every tool is declared
/// under its original `mimir_*` name; rename-transition aliases (`mneme_*`,
/// `perseus_vault_*`) are synthesized on top of this at advertise time by
/// `build_tools_array`. The registry is a compile-time constant, parsed exactly
/// once per process instead of re-parsing ~3.5k lines of JSON on every
/// tools/list request (perf review #208).
fn tool_registry_base() -> &'static Vec<serde_json::Value> {
    static BASE: OnceLock<Vec<serde_json::Value>> = OnceLock::new();
    BASE.get_or_init(|| {
        serde_json::from_str::<serde_json::Value>(
        r###"[
  {
    "name": "mimir_remember",
    "description": "Store or update an entity by (category, key). Idempotent — call as often as you want, same key returns an update. NEAR-DUPLICATE MERGING (#531): a NEW key whose body is >=70% trigram-similar to an existing entity in the same category+workspace does NOT create a new entity — the write is folded into the existing one (result: action='deduped', deduped=true, merged_into=<id>). Right for conversational memory; wrong for bulk ingest of templated records, which are similar by construction and will silently collapse to a handful of rows. For bulk ingest pass skip_dedup=true (or use mimir_ingest_file), and check the returned action. Prefer recall_when triggers (retrieve when relevant) over always_on=true (inject unconditionally): the recall-first mimir_context hard-caps the always-on set and warns when it overflows, so reserve always_on for genuinely identity-critical facts. Optional certainty (0.0-1.0) is used by mimir_conflicts for typed-entity conflict detection. Pass derived_from (ids or {category,key} pairs of the memories you recalled) to auto-mark those sources useful — cited memories rank higher and decay slower. Use this for saving facts, decisions, architecture notes, and conventions. When encryption is enabled, body_json is encrypted at rest with AES-256-GCM.",
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
        "valid_from_unix_ms": {
          "type": "integer",
          "description": "Application-time period start (#363): when the fact became TRUE IN THE WORLD, independent of when it was recorded. Set in the past for retroactive facts ('this was true last week, we just learned it') without rewriting transaction history. Default: transaction time (now). Query with mimir_valid_at / mimir_bitemporal / recall's valid_at filter."
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
        }
      }
    },
    "annotations": {
      "destructiveHint": true
    },
    "title": "Remember Entity"
  },
  {
    "name": "mimir_recall",
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
          "description": "If set, truncate body_json at N chars and append drill-down footer. Use mimir_get_entity to read full body."
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
        "layer": {
            "type": "string",
            "description": "Filter by memory layer (world, episodic, semantic)."
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
    "name": "mimir_recall_layer",
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
    "name": "mimir_scan",
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
    "name": "mimir_semantic_search",
    "description": "Dense-only semantic search: find entities by meaning, ranked purely by embedding similarity (no keyword fallback). On by default via the bundled in-process ONNX model — zero config, zero network. A one-tool shortcut for 'find things like this'. For fused keyword+vector results use mimir_recall.",
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
    "name": "mimir_ask",
    "description": "Ask a natural language question and get a grounded answer from stored memories via RAG. Internally recalls top-k entities, assembles context, and queries the configured LLM (Ollama) for an answer with cited sources. Requires --llm-endpoint to be set. LLM request timeout defaults to 30s; set MIMIR_LLM_TIMEOUT_SECS for large/cold models that need longer to load (#528).",
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
    "name": "mimir_get_entity",
    "description": "Get an entity by ID with its full body_json content. Use after mimir_recall with preview_cap to read the complete body of a truncated result. The drill-down footer embedded in preview-capped results references this tool with the entity ID to use.",
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
    "name": "mimir_history",
    "description": "List superseded (historical) versions of a fact (category + key), newest first. Each entry was the live fact for an interval before it was overwritten. The companion to mimir_as_of: as_of returns the single version live at one instant; history returns the version trail. Paginated: returns the `limit` newest versions (default 20) starting at `offset`; `total` in the response is the FULL trail size, so total > returned means there are more pages. Returns an empty list if the fact has never been overwritten (its only version is the current live one in recall).",
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
    "name": "mimir_as_of",
    "description": "Transaction-time time-travel: return the version of a fact (category + key) that Mneme believed at a given past instant. When a fact is overwritten, the prior version is kept in history; this returns whichever version was live at as_of_unix_ms. Use to answer 'what did we believe about X back then?' or to audit how a fact changed. For the orthogonal valid-time axis ('what was actually TRUE in the world at time T') use mimir_valid_at; for both axes at once use mimir_bitemporal. Returns found=false if the fact had not been recorded yet at that time. If the instant falls inside a window compacted by history retention (#398), returns an explicit marker (compacted=true, versions_compacted, digest) instead of the original — now unrecoverable — versions.",
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
    "name": "mimir_valid_at",
    "description": "Valid-time (application-time) lookup: return the version of a fact (category + key) that — per CURRENT knowledge — was actually true in the world at a given instant. Orthogonal to mimir_as_of: as_of answers 'what did we BELIEVE at time T' (transaction time); valid_at answers 'what WAS TRUE at time T, as we understand it now'. Facts carry a valid period [valid_from, valid_to) settable on mimir_remember; a later-recorded version's claim supersedes earlier claims for the instants it covers. Returns found=false if no version's valid period contains the instant.",
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
    "name": "mimir_bitemporal",
    "description": "Full bi-temporal query (SQL:2011 SYSTEM_TIME + APPLICATION_TIME): 'as of transaction time tx_at, which version did we believe was true in the world at valid time valid_at?' Returns the exact cell of the bi-temporal rectangle — the audit-grade 'who knew what, as-of-when' question. Combines both axes: mimir_as_of is this with valid_at pinned to tx_at; mimir_valid_at is this with tx_at pinned to now. Retroactive and proactive updates land in the correct rectangle cell. Returns found=false if nothing recorded by tx_at was valid at valid_at.",
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
    "name": "mimir_forget",
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
    "name": "mimir_ingest",
    "description": "Sync external data connectors (GitHub issues, file watcher) into Mneme. Call with no arguments to run all enabled connectors, or specify a connector name to run only that one. Use dry_run=true to preview without storing.",
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
    "name": "mimir_ingest_file",
    "description": "Ingest a document file into memory by extracting its text LOCALLY (no cloud, no network). Plaintext/markdown/structured-text work in any build; DOCX and PDF require a binary built with --features multimodal (otherwise a clear error is returned). The extracted text is stored as a normal entity (recallable via mimir_recall). category defaults to 'document', key defaults to the file name.",
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
    "name": "mimir_embed",
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
    "name": "mimir_prune",
    "description": "Bulk archive entities by category, decay threshold, or age. Use dry_run=true to preview without archiving. Useful for cleaning stale or low-quality memories. With scope='history' (#398) it instead evicts old superseded versions from entity_history under the given (or env-configured MIMIR_HISTORY_*) bounds, rolling each evicted run into a compaction tombstone; dry_run reports the rows and bytes that would be evicted.",
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
          "description": "scope='history': evict versions invalidated more than this many days ago (overrides MIMIR_HISTORY_MAX_AGE_DAYS)"
        },
        "max_versions_per_key": {
          "type": "integer",
          "description": "scope='history': keep at most this many stored versions per key, oldest evicted first (overrides MIMIR_HISTORY_MAX_VERSIONS_PER_KEY)"
        },
        "max_bytes": {
          "type": "integer",
          "description": "scope='history': global stored-history byte budget, globally-oldest evicted first (overrides MIMIR_HISTORY_MAX_BYTES)"
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
    "name": "mimir_link",
    "description": "Create a relationship link from one entity to another. Builds a knowledge graph that mimir_traverse can walk. Use 'depends_on', 'implements', 'extends', 'references', or custom relationships.",
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
          "description": "Target entity ID (from mimir_remember return value)"
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
    "name": "mimir_unlink",
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
    "name": "mimir_journal",
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
    "name": "mimir_check_failure_pattern",
    "description": "Deja-vu guard (#521): call BEFORE retrying a failed command or committing to an approach. Checks the action against previously recorded failures in both the journal (error events and failure-marked acted/forward payloads) and the entity store (failure/pitfall/root-cause memories), ranked by similarity, recency, and trust. Returns matching prior failures with the recorded cause and resolution, a deja_vu flag, and a one-line warning when the action was already tried and failed. Read-only: never bumps retrieval counts or decay. Record failures via mimir_journal (event_type 'error') or mimir_remember so the guard can find them.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "action": {
          "type": "string",
          "description": "The command line or approach description you are about to (re)try, e.g. 'cargo build --no-default-features' or 'parse the changelog with a regex'"
        },
        "workspace_hash": {
          "type": "string",
          "description": "Workspace scope filter. When set, only failures recorded in this workspace (plus global, unscoped ones) are matched; other workspaces' failures never leak. Omit for no workspace filtering."
        },
        "limit": {
          "type": "integer",
          "default": 5,
          "description": "Maximum number of matches to return (1-50)"
        }
      },
      "required": [
        "action"
      ]
    },
    "outputSchema": {
      "type": "object",
      "properties": {
        "matches": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "Prior failures matching the action, best first. Each: {source: 'journal'|'entity', ref, when (unix ms), what_failed, cause, resolution, score}"
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
    "name": "mimir_timeline",
    "description": "Query journal events by time range with optional filters for event type, category, or entity. Use this to reconstruct the decision history and understand what happened when.",
    "inputSchema": {
      "type": "object",
      "properties": {
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
    "name": "mimir_state_set",
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
    "name": "mimir_state_get",
    "description": "Get a state value by key. Returns null if the key has expired or doesn't exist. Use this instead of mimir_recall for transient session state that doesn't need FTS5 search.",
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
    "name": "mimir_state_delete",
    "description": "Delete a state entry by key. Permanent removal — unlike mimir_forget which is a soft-delete. Use this to clean up expired or unused state entries.",
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
    "name": "mimir_state_list",
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
    "name": "mimir_health",
    "description": "Check whether the Mneme server and its SQLite database are healthy. Returns a simple healthy/unhealthy status. Use this for health checks and monitoring, not for detailed stats (use mimir_stats).",
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
          "description": "Server health status"
        }
      }
    },
    "annotations": {
      "readOnlyHint": true
    },
    "title": "Check Health"
  },
  {
    "name": "mimir_stats",
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
          "type": "integer",
          "description": "Oldest entity creation timestamp"
        },
        "newest_unix_ms": {
          "type": "integer",
          "description": "Newest entity creation timestamp"
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
    "name": "mimir_compact",
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
    "name": "mimir_purge",
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
    "name": "mimir_memories",
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
    "name": "mimir_migrate",
    "description": "Migrate a v0.1.x Mneme database to the current v0.5.0 schema. Reads the old database, converts memories to the entity model, and merges into the current database. Use this once per legacy database during upgrade.",
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
    "name": "mimir_context",
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
    "name": "mimir_extract",
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
    "name": "mimir_capture",
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
    "name": "mimir_traverse",
    "description": "Walk the entity link graph starting from a given entity up to a configurable depth. Returns a chain of linked entities — useful for exploring dependencies, decision trees, and relationship graphs built via mimir_link.",
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
    "name": "mimir_score",
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
    "name": "mimir_follow",
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
    "name": "mimir_conflicts",
    "description": "Detect conflicting entities in the same category — pairs with low trigram similarity in their body_json. Flags potential contradictions, duplicate-but-divergent entries, and stale-overwritten facts. Read-only by default. Opt in with resolve=true to actively invalidate the lower-certainty side of clear conflicts (superseding it into history, reversible + time-travelable via mimir_as_of); that path defaults to dry_run=true so you preview first, and never resolves pairs whose certainties are within certainty_margin.",
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
    "name": "mimir_consolidate",
    "description": "Merge overlapping/duplicative entities in the same category into durable, evidence-tracked 'observations' — the mirror image of mimir_conflicts, which flags dissimilar (contradictory) pairs. Groups entities whose pairwise trigram similarity meets similarity_threshold, then creates one new entity per group (category='observation') whose body carries a summary (the highest-certainty source's content), the full list of source entity ids as evidence, and a proof_count. The observation links back to each source (relationship='evidence_for') for full audit. By default sources stay live; set archive_sources=true to retire merged sources ('local dreaming' — verified or importance-floored sources are never archived), and cold_first=true to target the memories decay is about to claim. mimir_autocohere runs a bounded cold_first+archive_sources pass automatically. Read-only preview with dry_run=true.",
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
    "name": "mimir_dream",
    "description": "Sleep-time LLM consolidation: batch clusters of related cold/episodic memories, reflect over each cluster via the configured LLM endpoint, and write back durable higher-order SEMANTIC insights (category='insight', semantic layer) — 'given these N memories, what stable pattern/preference/fact do they collectively imply?'. Each written insight carries evidence_for links to every source entity (full provenance), a certainty blended from LLM confidence and evidence coverage, and derivation='dream' so it is auditable and reversible. Idempotent: insights are keyed by an evidence-set hash, so re-dreaming an unchanged cluster never spawns duplicates. Contradictory sources surface as a flagged 'contradiction' insight, never a silent merge. Never fabricates: clusters that support no durable generalization are a no-op. Requires --llm-endpoint (fully local via Ollama); returns a clean error without it unless fallback_consolidate=true, which runs the non-LLM mimir_consolidate pass instead. Bounded by max_entities/max_clusters budgets. Preview with dry_run=true.",
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
          "description": "When no --llm-endpoint is configured, run the mechanical (non-LLM) mimir_consolidate cold_first pass instead of returning an error."
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
        "insights": {
          "type": "array",
          "items": {
            "type": "object"
          },
          "description": "The insights written (or previewed), each with entity_id, key, summary, insight_type, confidence, source_ids, category, contradiction, deduped"
        }
      }
    },
    "annotations": {
      "readOnlyHint": false
    },
    "title": "Dream: LLM Consolidation of Episodic Memory into Semantic Insights"
  },
  {
    "name": "mimir_vault_export",
    "description": "Export all non-archived entities to .md files with YAML frontmatter in a vault directory. Files are human-readable, git-trackable, and Obsidian-compatible. Use this for backup, transfer between workspaces, or offline review.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "vault_dir": {
          "type": "string",
          "default": "~/.mimir/vault",
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
    "name": "mimir_vault_import",
    "description": "Import .md files from a vault directory into the database. Reads YAML frontmatter for metadata and markdown body for content. Idempotent — re-running on the same vault won't duplicate entities. Pair with mimir_vault_export for transfer.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "vault_dir": {
          "type": "string",
          "default": "~/.mimir/vault",
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
    "name": "mimir_decay",
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
    "name": "mimir_reindex",
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
    "name": "mimir_workspace_list",
    "description": "List all distinct entity categories present in the database. Use this to discover what knowledge domains exist before querying with mimir_recall or mimir_context.",
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
    "name": "mimir_recall_when",
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
    "name": "mimir_cohere",
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
    "name": "mimir_share",
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
    "name": "mimir_federate",
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
          "default": "/tmp/mimir-federate",
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
    "name": "mimir_correct",
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
    "name": "mimir_synthesize",
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
    "name": "mimir_bench",
    "description": "Record a performance benchmark data point. Tracks task metrics (turns taken, tokens used, success) alongside whether memory recall was used — enabling measurement of Mneme's impact on agent performance. Aggregate with mimir_recall to analyze trends.",
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
          "description": "Whether memory recall (mimir_recall) was used during this task"
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
    "name": "mimir_autocohere",
    "description": "Run a full atomic grooming pass: cohere (promote, link, archive), then decay (recalculate Ebbinghaus decay), then compact (archive below threshold), then consolidate, then enforce the entity_history retention policy (#398 — no-op unless MIMIR_HISTORY_* env knobs are set). Returns a summary report. Use dry_run=true to preview without changes.",
    "inputSchema": {
      "type": "object",
      "properties": {
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
          "description": "entity_history rows evicted by the retention policy (#398; 0 while no MIMIR_HISTORY_* knob is set)"
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
    "name": "mimir_supersede",
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
          "description": "When the OLD fact stopped being true in the world (#363, unix ms). Defaults to transaction time (now). Closes the old entity's application-time period so mimir_valid_at stops returning it from that instant on. Must be after the fact's valid_from, and may only TIGHTEN an already-closed period (a fact that ended cannot be retroactively extended); violations are rejected before any mutation."
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
    "name": "mimir_maintenance",
    "description": "Database maintenance operations: deduplicate entities with identical (category, key), detect orphan journal entries and links, vacuum (reclaim disk space), reindex FTS5, and enforce the entity_history retention policy (#398 — no-op unless MIMIR_HISTORY_* env knobs are set). Set dry_run=true to preview. Use 'all' to run everything.",
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
          "description": "Enforce the entity_history retention policy from MIMIR_HISTORY_* env knobs (#398; no-op while none are set)",
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
    "name": "mimir_communities",
    "description": "GraphRAG community detection: partition the entity link graph (built via mimir_link) into communities using deterministic label propagation or greedy modularity ('louvain'). Persists the result with an extractive summary per community; community ids are derived from the member set, so re-detection after membership changes yields new ids. Local-first — no LLM or network required.",
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
    "name": "mimir_community_summary",
    "description": "Return (and materialize) the summary of one detected community. Default is the extractive summary (top representative members); set use_llm=true for an optional LLM polish that degrades back to extractive when no LLM endpoint is configured. The summary is stored as a 'community_summary' entity carrying evidence_for links to its members, and cached while membership is unchanged.",
    "inputSchema": {
      "type": "object",
      "properties": {
        "community_id": {
          "type": "string",
          "description": "Community id from mimir_communities, e.g. 'com-1a2b3c4d5e6f7a8b'"
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
    "name": "mimir_global_recall",
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
  }
]"###,
        )
        .expect("tools JSON must be valid")
        .as_array()
        .expect("tools registry must be a JSON array")
        .clone()
    })
}

/// Whether tools/list advertises all three rename-transition prefixes
/// (`mimir_`/`mneme_`/`perseus_vault_`) or only the canonical
/// `perseus_vault_*` set. Legacy names stay dispatchable via `call_tool`
/// regardless — this controls only what is *advertised*, so a client sees one
/// copy of each tool instead of three (the 3× manifest was tripling the
/// tool-schema payload on every request for every connected client).
///
/// Default (unset or "canonical"): canonical-only. Opt back into the historical
/// 3× manifest with `PERSEUS_VAULT_TOOL_ALIASES=all` (the legacy env
/// `MIMIR_TOOL_ALIASES` is also honored, with `PERSEUS_VAULT_` taking
/// precedence).
fn advertise_all_aliases() -> bool {
    let mode = std::env::var("PERSEUS_VAULT_TOOL_ALIASES")
        .or_else(|_| std::env::var("MIMIR_TOOL_ALIASES"))
        .unwrap_or_default();
    matches!(
        mode.trim().to_ascii_lowercase().as_str(),
        "all" | "legacy" | "1" | "true"
    )
}

/// Build the advertised tool array from the canonical registry. When
/// `advertise_all` is false, only the canonical `perseus_vault_*` name is
/// emitted for each tool; when true, all three rename-transition prefixes are
/// emitted (the historical behavior).
fn build_tools_array(base_array: &[serde_json::Value], advertise_all: bool) -> serde_json::Value {
    let mut aliased: Vec<serde_json::Value> =
        Vec::with_capacity(base_array.len() * if advertise_all { 3 } else { 1 });
    for tool in base_array {
        if advertise_all {
            aliased.push(tool.clone());
            if let Some(mneme_alias) = mneme_alias_tool(tool) {
                aliased.push(mneme_alias);
            }
            if let Some(vault_alias) = perseus_vault_alias_tool(tool) {
                aliased.push(vault_alias);
            }
        } else {
            // Canonical-only: advertise the perseus_vault_* name. A tool that
            // (unexpectedly) isn't mimir_*-prefixed passes through unchanged.
            aliased.push(perseus_vault_alias_tool(tool).unwrap_or_else(|| tool.clone()));
        }
    }
    serde_json::Value::Array(aliased)
}

/// Build the tools/list response. The canonical registry is parsed once
/// (`tool_registry_base`); the advertised array is cached per advertise-mode so
/// repeated tools/list calls don't re-synthesize it (perf review #208).
fn list_tools(id: Option<Value>) -> JsonRpcResponse {
    static TOOLS_ALL: OnceLock<serde_json::Value> = OnceLock::new();
    static TOOLS_CANONICAL: OnceLock<serde_json::Value> = OnceLock::new();
    let tools_json = if advertise_all_aliases() {
        TOOLS_ALL.get_or_init(|| build_tools_array(tool_registry_base(), true))
    } else {
        TOOLS_CANONICAL.get_or_init(|| build_tools_array(tool_registry_base(), false))
    };

    JsonRpcResponse {
        jsonrpc: "2.0".to_string(),
        id,
        result: Some(json!({
            "tools": tools_json.clone()
        })),
        error: None,
    }
}
fn call_tool(name: &str, db: &Database, args: Value, _id: Option<Value>) -> String {
    // Keep the caller's original (un-normalized) name for error messages —
    // a "mneme_bogus"/"perseus_vault_bogus" call should say so, not report
    // back the normalized "mimir_bogus" it was rewritten to below.
    let original_name = name;
    // Mneme/Perseus Vault rename (transition release): "mneme_*" and
    // "perseus_vault_*" are back-compat aliases for "mimir_*" — normalize
    // whichever prefix is present once here so every match arm below keeps
    // dispatching on the original name without needing its own alias arm.
    let owned_name = name
        .strip_prefix("perseus_vault_")
        .or_else(|| name.strip_prefix("mneme_"))
        .map(|suffix| format!("mimir_{}", suffix));
    let name: &str = owned_name.as_deref().unwrap_or(name);

    let handler_result: Result<String, String> = match name {
        "mimir_remember" => tools::handle_remember(db, args).map_err(|e| e.to_string()),

        "mimir_recall" => tools::handle_recall(db, args).map_err(|e| e.to_string()),

        "mimir_recall_layer" => tools::handle_recall_layer(db, args).map_err(|e| e.to_string()),

        "mimir_scan" => tools::handle_scan(db, args).map_err(|e| e.to_string()),

        "mimir_semantic_search" => {
            tools::handle_semantic_search(db, args).map_err(|e| e.to_string())
        }

        "mimir_ask" => tools::handle_ask(db, args).map_err(|e| e.to_string()),

        "mimir_get_entity" => tools::handle_get_entity(db, args).map_err(|e| e.to_string()),
        "mimir_history" => tools::handle_history(db, args).map_err(|e| e.to_string()),
        "mimir_as_of" => tools::handle_as_of(db, args).map_err(|e| e.to_string()),
        "mimir_valid_at" => tools::handle_valid_at(db, args).map_err(|e| e.to_string()),
        "mimir_bitemporal" => tools::handle_bitemporal(db, args).map_err(|e| e.to_string()),
        "mimir_forget" => tools::handle_forget(db, args).map_err(|e| e.to_string()),

        "mimir_ingest" => tools::handle_ingest(db, args).map_err(|e| e.to_string()),

        "mimir_ingest_file" => tools::handle_ingest_file(db, args).map_err(|e| e.to_string()),

        "mimir_embed" => tools::handle_embed(db, args).map_err(|e| e.to_string()),

        "mimir_prune" => tools::handle_prune(db, args).map_err(|e| e.to_string()),

        "mimir_link" => tools::handle_link(db, args).map_err(|e| e.to_string()),

        "mimir_unlink" => tools::handle_unlink(db, args).map_err(|e| e.to_string()),

        "mimir_journal" => tools::handle_journal(db, args).map_err(|e| e.to_string()),

        "mimir_check_failure_pattern" => {
            tools::handle_check_failure_pattern(db, args).map_err(|e| e.to_string())
        }

        "mimir_timeline" => tools::handle_timeline(db, args).map_err(|e| e.to_string()),

        "mimir_state_set" => tools::handle_state_set(db, args).map_err(|e| e.to_string()),

        "mimir_state_get" => tools::handle_state_get(db, args).map_err(|e| e.to_string()),

        "mimir_state_delete" => tools::handle_state_delete(db, args).map_err(|e| e.to_string()),

        "mimir_state_list" => tools::handle_state_list(db, args).map_err(|e| e.to_string()),

        "mimir_health" => Ok(tools::handle_health(db)),

        "mimir_stats" => Ok(tools::handle_stats(db)),

        "mimir_compact" => Ok(tools::handle_compact(db, args)),

        "mimir_purge" => tools::handle_purge(db, args).map_err(|e| e.to_string()),
        "mimir_memories" => tools::handle_memories(db, args).map_err(|e| e.to_string()),

        "mimir_migrate" => Ok(tools::handle_migrate(db, args)),

        "mimir_context" => Ok(tools::handle_context(db, args)),

        "mimir_extract" => tools::handle_extract(db, args).map_err(|e| e.to_string()),

        "mimir_capture" => tools::handle_capture(db, args).map_err(|e| e.to_string()),

        "mimir_traverse" => Ok(tools::handle_traverse(db, args)),
        "mimir_score" => Ok(tools::handle_score(db, args)),
        "mimir_follow" => tools::handle_follow(db, args).map_err(|e| e.to_string()),
        "mimir_conflicts" => Ok(tools::handle_conflicts(db, args)),
        "mimir_consolidate" => Ok(tools::handle_consolidate(db, args)),
        "mimir_dream" => tools::handle_dream(db, args),
        "mimir_vault_export" => Ok(tools::handle_vault_export(db, args)),
        "mimir_vault_import" => Ok(tools::handle_vault_import(db, args)),
        "mimir_decay" => Ok(tools::handle_decay(db, args)),
        "mimir_reindex" => Ok(tools::handle_reindex(db, args)),
        "mimir_share" => tools::handle_share(db, args).map_err(|e| e.to_string()),
        "mimir_federate" => tools::handle_federate(db, args).map_err(|e| e.to_string()),
        "mimir_workspace_list" => Ok(tools::handle_workspace_list(db)),
        "mimir_recall_when" => tools::handle_recall_when(db, args).map_err(|e| e.to_string()),
        "mimir_cohere" => tools::handle_cohere(db, args).map_err(|e| e.to_string()),
        "mimir_correct" => tools::handle_correct(db, args).map_err(|e| e.to_string()),
        "mimir_synthesize" => tools::handle_synthesize(db, args).map_err(|e| e.to_string()),
        "mimir_bench" => tools::handle_bench(db, args).map_err(|e| e.to_string()),

        "mimir_communities" => tools::handle_communities(db, args).map_err(|e| e.to_string()),
        "mimir_community_summary" => {
            tools::handle_community_summary(db, args).map_err(|e| e.to_string())
        }
        "mimir_global_recall" => tools::handle_global_recall(db, args).map_err(|e| e.to_string()),

        "mimir_autocohere" => tools::handle_autocohere(db, args).map_err(|e| e.to_string()),
        "mimir_supersede" => tools::handle_supersede(db, args).map_err(|e| e.to_string()),
        "mimir_maintenance" => tools::handle_maintenance(db, args).map_err(|e| e.to_string()),

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

    /// Tool names advertised by tools/list for a given advertise-mode. Bypasses
    /// the env var + OnceLock caching in `list_tools` so the two modes can be
    /// asserted deterministically in the same process (no cross-test races).
    fn advertised_names(advertise_all: bool) -> Vec<String> {
        build_tools_array(tool_registry_base(), advertise_all)
            .as_array()
            .unwrap()
            .iter()
            .map(|t| t["name"].as_str().unwrap().to_string())
            .collect()
    }

    #[test]
    fn tool_aliases_default_to_canonical_only() {
        // The regression this guards (#tool-alias-triple): every connected
        // client was loading each tool three times (mimir_/mneme_/perseus_vault_),
        // tripling the tool-schema payload on every request. Default advertise
        // mode must emit exactly one canonical `perseus_vault_*` copy per tool.
        let canonical = advertised_names(false);
        let all = advertised_names(true);
        assert_eq!(
            all.len(),
            canonical.len() * 3,
            "all-aliases must advertise 3× the canonical set"
        );
        assert!(
            canonical.iter().all(|n| n.starts_with("perseus_vault_")),
            "canonical mode must advertise only perseus_vault_* names"
        );
        assert!(
            !canonical.iter().any(|n| n.starts_with("mimir_") || n.starts_with("mneme_")),
            "canonical mode must not advertise legacy mimir_/mneme_ names"
        );
    }

    #[test]
    fn dream_is_registered_with_aliases_and_errors_cleanly_without_llm() {
        // Default advertises only the canonical name; the legacy prefixes stay
        // dispatchable (asserted via call_tool below) but unadvertised. Opt-in
        // `all` restores every rename-transition alias.
        assert!(advertised_names(false).contains(&"perseus_vault_dream".to_string()));
        assert!(!advertised_names(false).contains(&"mimir_dream".to_string()));
        for name in ["mimir_dream", "mneme_dream", "perseus_vault_dream"] {
            assert!(
                advertised_names(true).contains(&name.to_string()),
                "all-aliases missing {name}"
            );
        }

        let db_path = std::env::temp_dir()
            .join(format!("mimir-dream-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // No --llm-endpoint configured: the tool must answer with a clean MCP
        // tool error (isError, spec §3.3) — never a crash or protocol error —
        // and the message must name the flag and the non-LLM alternative.
        let r = call_tool("mimir_dream", &db, json!({"category": "episodes"}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true), "got: {r}");
        let msg = v["content"][0]["text"].as_str().unwrap();
        assert!(msg.contains("--llm-endpoint"), "got: {msg}");
        assert!(msg.contains("mimir_consolidate"), "got: {msg}");

        // Alias prefixes normalize into the same handler.
        let r = call_tool("perseus_vault_dream", &db, json!({}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true));

        // Opt-in graceful degradation: fallback_consolidate runs the non-LLM
        // consolidate pass instead of erroring, and says so.
        let r = call_tool(
            "mimir_dream",
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
    fn check_failure_pattern_is_registered_and_dispatches_under_aliases() {
        // #521: tools/list must expose the deja-vu guard under the canonical
        // name AND the rename-transition aliases (which come from the shared
        // alias synthesis, not hand-duplicated entries).
        assert!(advertised_names(false).contains(&"perseus_vault_check_failure_pattern".to_string()));
        for name in [
            "mimir_check_failure_pattern",
            "mneme_check_failure_pattern",
            "perseus_vault_check_failure_pattern",
        ] {
            assert!(
                advertised_names(true).contains(&name.to_string()),
                "all-aliases missing {name}"
            );
        }

        let db_path = std::env::temp_dir()
            .join(format!("mimir-fpguard-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Alias prefixes normalize into the same handler; empty store answers
        // with the unambiguous empty state.
        let r = call_tool(
            "perseus_vault_check_failure_pattern",
            &db,
            json!({"action": "cargo build --release"}),
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
        let r = call_tool("mimir_check_failure_pattern", &db, json!({}), None);
        let v: Value = serde_json::from_str(&r).unwrap();
        assert_eq!(v["isError"], json!(true), "got: {r}");

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn capture_is_registered_and_dispatches_under_aliases() {
        // #520: tools/list must expose the capture pipeline under the
        // canonical name AND the rename-transition aliases (which come from
        // the shared alias synthesis, not hand-duplicated entries).
        assert!(advertised_names(false).contains(&"perseus_vault_capture".to_string()));
        for name in ["mimir_capture", "mneme_capture", "perseus_vault_capture"] {
            assert!(
                advertised_names(true).contains(&name.to_string()),
                "all-aliases missing {name}"
            );
        }

        let db_path = std::env::temp_dir()
            .join(format!("mimir-capture-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // Alias prefixes normalize into the same handler; a real payload
        // distills and writes through the remember path.
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
        let r = call_tool("mimir_capture", &db, json!({"text": "  "}), None);
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
            .join(format!("mimir-memories-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");
        let call = |args: Value| -> String {
            call_tool("mimir_memories", &db, args, None)
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
    fn bitemporal_tools_are_registered_and_dispatch_under_all_aliases() {
        // #363: mimir_valid_at / mimir_bitemporal exist in the registry (with
        // mneme_/perseus_vault_ aliases synthesized like every other tool) and
        // dispatch through call_tool under each prefix.
        let db_path = std::env::temp_dir()
            .join(format!("mimir-bitemporal-tools-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        let names = advertised_names(true);
        for expect in [
            "mimir_valid_at",
            "mneme_valid_at",
            "perseus_vault_valid_at",
            "mimir_bitemporal",
            "mneme_bitemporal",
            "perseus_vault_bitemporal",
        ] {
            assert!(names.contains(&expect.to_string()), "missing tool {expect}");
        }
        // Canonical default advertises exactly the perseus_vault_* variants.
        let canonical = advertised_names(false);
        assert!(canonical.contains(&"perseus_vault_valid_at".to_string()));
        assert!(canonical.contains(&"perseus_vault_bitemporal".to_string()));
        assert!(!canonical.contains(&"mimir_valid_at".to_string()));

        // Round-trip through call_tool under every prefix.
        let stored = call_tool(
            "mimir_remember",
            &db,
            json!({"category": "f", "key": "k", "body_json": "{\"note\":\"x\"}",
                   "valid_from_unix_ms": 1000}),
            None,
        );
        assert!(stored.contains("created"), "{stored}");
        for prefix in ["mimir", "mneme", "perseus_vault"] {
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
    fn unknown_tool_error_reports_original_unnormalized_name() {
        let db_path = std::env::temp_dir()
            .join(format!("mimir-unknown-tool-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(db_path.to_str().expect("temp db path")).expect("open temp db");

        // A caller using either back-compat prefix should see ITS OWN name in
        // the error, not the "mimir_*" name it gets normalized to internally.
        let mneme_result = call_tool("mneme_bogus", &db, json!({}), None);
        assert!(mneme_result.contains("Unknown tool: mneme_bogus"), "got: {mneme_result}");
        assert!(!mneme_result.contains("mimir_bogus"), "got: {mneme_result}");

        let vault_result = call_tool("perseus_vault_bogus", &db, json!({}), None);
        assert!(
            vault_result.contains("Unknown tool: perseus_vault_bogus"),
            "got: {vault_result}"
        );
        assert!(!vault_result.contains("mimir_bogus"), "got: {vault_result}");

        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn rejects_non_json_rpc_2_requests() {
        let db_path =
            std::env::temp_dir().join(format!("mimir-jsonrpc-version-{}.db", uuid::Uuid::new_v4()));
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
        // Regression: serverInfo.name was a hardcoded "mimir" literal,
        // reporting stale branding through the Mimir -> Mneme -> Perseus
        // Vault renames. It must track Cargo.toml's package name instead.
        let db_path = std::env::temp_dir()
            .join(format!("mimir-initialize-name-{}.db", uuid::Uuid::new_v4()));
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
    fn recall_confidence_is_opt_in_and_normalized() {
        let db_path =
            std::env::temp_dir().join(format!("mimir-confidence-{}.db", uuid::Uuid::new_v4()));
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
            std::env::temp_dir().join(format!("mimir-history-{}.db", uuid::Uuid::new_v4()));
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
        // canonical mimir_* name and both rename aliases, and must appear in
        // tools/list.
        let db_path =
            std::env::temp_dir().join(format!("mimir-graphrag-{}.db", uuid::Uuid::new_v4()));
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

        let detect = call_tool("mimir_communities", &db, json!({}), None);
        let v: Value = serde_json::from_str(&detect).expect("valid JSON");
        assert_eq!(v["communities"].as_array().unwrap().len(), 1, "got: {detect}");
        let cid = v["communities"][0]["id"].as_str().unwrap().to_string();

        // Alias dispatch: perseus_vault_* and mneme_* normalize to mimir_*.
        let summary = call_tool(
            "perseus_vault_community_summary",
            &db,
            json!({"community_id": cid}),
            None,
        );
        let sv: Value = serde_json::from_str(&summary).expect("valid JSON");
        assert_eq!(sv["community_id"].as_str().unwrap(), cid, "got: {summary}");
        assert!(sv.get("isError").is_none(), "got: {summary}");

        let recall = call_tool("mneme_global_recall", &db, json!({"query": "quasar"}), None);
        let rv: Value = serde_json::from_str(&recall).expect("valid JSON");
        assert!(rv.get("isError").is_none(), "got: {recall}");
        assert_eq!(rv["communities"].as_array().unwrap().len(), 1, "got: {recall}");

        // In `all` mode tools/list advertises every prefix (x3 with aliases).
        let all = advertised_names(true);
        for tool in [
            "mimir_communities",
            "mimir_community_summary",
            "mimir_global_recall",
            "mneme_global_recall",
            "perseus_vault_communities",
        ] {
            assert!(all.contains(&tool.to_string()), "all-aliases must advertise {tool}");
        }
        // Canonical default advertises only the perseus_vault_* variants.
        let canonical = advertised_names(false);
        assert!(canonical.contains(&"perseus_vault_global_recall".to_string()));
        assert!(!canonical.contains(&"mimir_global_recall".to_string()));

        drop(db);
        let _ = fs::remove_file(&db_path);
    }

    #[test]
    fn recall_layer_filter_scopes_by_canonical_and_alias() {
        let db_path =
            std::env::temp_dir().join(format!("mimir-layerfilter-{}.db", uuid::Uuid::new_v4()));
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
    fn idle_timeout_parsing_covers_orphan_guard_cases() {
        use std::time::Duration;
        // Unset -> 10-minute default (guard ON).
        assert_eq!(parse_idle_timeout(None), Some(Duration::from_secs(600)));
        // Explicit "0" -> disabled (guard OFF, for interactive/debug use).
        assert_eq!(parse_idle_timeout(Some("0")), None);
        // Explicit value -> honored.
        assert_eq!(parse_idle_timeout(Some("30")), Some(Duration::from_secs(30)));
        // Whitespace tolerated.
        assert_eq!(
            parse_idle_timeout(Some(" 120 ")),
            Some(Duration::from_secs(120))
        );
        // Garbage -> safe default, never panics.
        assert_eq!(
            parse_idle_timeout(Some("banana")),
            Some(Duration::from_secs(600))
        );
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
}
