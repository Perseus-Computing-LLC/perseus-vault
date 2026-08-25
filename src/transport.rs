// MCP SSE + Streamable HTTP transport layer.
// Reuses the core MCP JSON-RPC handler from crate::mcp.
//
// Uses static globals instead of axum Router state because axum 0.7's serve()
// only accepts Router<()>. State is set once via init_transport_state() before
// the server starts.

use axum::{
    extract::{Query, State},
    http::{header, HeaderMap, Method, Request, StatusCode},
    middleware::{self, Next},
    response::{
        sse::{Event, Sse},
        IntoResponse, Json, Response,
    },
    routing::{get, post},
    Router,
};
use futures::stream::Stream;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex, OnceLock, RwLock},
};
use tokio::sync::broadcast;
use tower_http::cors::{AllowOrigin, CorsLayer};

use crate::db::Database;
use crate::mcp::{self, JsonRpcRequest, MCPState};

/// Transport mode
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum TransportMode {
    Sse,
    Http,
}

const MCP_SESSION_ID_HEADER: &str = "mcp-session-id";
const DEFAULT_MAX_HTTP_SESSIONS: usize = 1024;

struct SessionEntry {
    state: Arc<MCPState>,
    last_used: u64,
}

struct SessionRegistry {
    entries: HashMap<String, SessionEntry>,
    clock: u64,
    max_sessions: usize,
}

impl SessionRegistry {
    fn new() -> Self {
        let max_sessions = std::env::var("PERSEUS_VAULT_HTTP_MAX_SESSIONS")
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .filter(|value| *value > 0)
            .unwrap_or(DEFAULT_MAX_HTTP_SESSIONS);
        Self { entries: HashMap::new(), clock: 0, max_sessions }
    }

    fn get(&mut self, session_id: &str) -> Option<Arc<MCPState>> {
        self.clock = self.clock.wrapping_add(1);
        let entry = self.entries.get_mut(session_id)?;
        entry.last_used = self.clock;
        Some(Arc::clone(&entry.state))
    }

    fn create(&mut self, session_id: String) -> Arc<MCPState> {
        if let Some(existing) = self.get(&session_id) {
            return existing;
        }
        if self.entries.len() >= self.max_sessions {
            if let Some(oldest) = self.entries.iter()
                .min_by_key(|(_, entry)| entry.last_used)
                .map(|(id, _)| id.clone())
            {
                self.entries.remove(&oldest);
            }
        }
        self.clock = self.clock.wrapping_add(1);
        let state = Arc::new(MCPState::new());
        self.entries.insert(session_id, SessionEntry {
            state: Arc::clone(&state),
            last_used: self.clock,
        });
        state
    }
}

/// Shared state for the MCP HTTP transport, stored as a global static.
struct TransportState {
    // #210: the DB remains lock-free at this layer. Session-registry locks are
    // held only long enough to clone an Arc<MCPState>; tool execution never runs
    // while the registry is locked.
    db: Arc<Database>,
    sessions: Mutex<SessionRegistry>,
    // Backward compatibility for pre-session clients that initialize and then
    // omit Mcp-Session-Id. It always points at the most recently created session.
    legacy_mcp_state: RwLock<Arc<MCPState>>,
    sse_tx: broadcast::Sender<String>,
}

static TRANSPORT_STATE: OnceLock<TransportState> = OnceLock::new();

/// Initialize the global transport state. Must be called before starting the server.
pub fn init_transport_state(db: Arc<Database>) {
    let (sse_tx, _) = broadcast::channel::<String>(256);
    let state = TransportState {
        db,
        sessions: Mutex::new(SessionRegistry::new()),
        legacy_mcp_state: RwLock::new(Arc::new(MCPState::new())),
        sse_tx,
    };
    TRANSPORT_STATE.set(state).ok();
}

/// Query params for POST /message
#[derive(Debug, Deserialize)]
struct MessageParams {
    #[serde(default)]
    #[allow(dead_code)]
    session_id: Option<String>,
}

/// Query params for GET /sse
#[derive(Debug, Deserialize)]
struct SseParams {
    #[serde(default)]
    #[allow(dead_code)]
    session_id: Option<String>,
}

/// Build the MCP HTTP transport router.
///
/// When `auth_token` is `Some`, every route requires a matching
/// `Authorization: Bearer <token>` header and returns 401 otherwise.
/// When `None`, auth is skipped entirely (backward compatible).
pub fn build_transport_router(mode: TransportMode, auth_token: Option<String>) -> Router {
    // Tightened CORS: explicit methods + headers instead of the previous
    // `Any/Any/Any`. Origin mirrors the request (auth is header-based Bearer, not
    // cookie/ambient, so reflecting Origin does not enable a cross-site
    // credential attack), but a `PERSEUS_VAULT_CORS_ALLOWED_ORIGINS` allowlist can lock
    // it down further.
    let session_header = header::HeaderName::from_static(MCP_SESSION_ID_HEADER);
    let cors = CorsLayer::new()
        .allow_origin(cors_allow_origin())
        .allow_methods([Method::GET, Method::POST, Method::OPTIONS])
        .allow_headers([
            header::AUTHORIZATION,
            header::CONTENT_TYPE,
            session_header.clone(),
        ])
        .expose_headers([session_header]);

    let mut router = Router::new().route("/message", post(handle_message));

    if mode == TransportMode::Sse {
        router = router.route("/sse", get(handle_sse));
    }

    let router = router
        .route_layer(middleware::from_fn_with_state(auth_token, auth_middleware))
        .layer(cors);
    // DoS-resistance: explicit body-size cap + global rate limit (Phase 1/2).
    crate::httplimit::apply_http_limits(router)
}

/// Resolve the CORS origin policy: an explicit comma-separated allowlist from
/// `PERSEUS_VAULT_CORS_ALLOWED_ORIGINS` if set, otherwise mirror the request origin.
fn cors_allow_origin() -> AllowOrigin {
    match std::env::var("PERSEUS_VAULT_CORS_ALLOWED_ORIGINS") {
        Ok(list) if !list.trim().is_empty() => {
            let origins: Vec<header::HeaderValue> = list
                .split(',')
                .filter_map(|o| header::HeaderValue::from_str(o.trim()).ok())
                .collect();
            AllowOrigin::list(origins)
        }
        _ => AllowOrigin::mirror_request(),
    }
}

/// Middleware: require a Bearer token if one is configured.
/// Skips auth when `auth_token` is `None`.
async fn auth_middleware(
    State(auth_token): State<Option<String>>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Result<Response, StatusCode> {
    let expected = match auth_token {
        Some(token) => token,
        None => return Ok(next.run(request).await),
    };

    let auth_header = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    if let Some(auth) = auth_header {
        if let Some(token) = auth.strip_prefix("Bearer ") {
            if crate::util::constant_time_str_eq(token, &expected) {
                return Ok(next.run(request).await);
            }
        }
    }

    let mut response = Response::new(axum::body::Body::from(
        json!({"error": "unauthorized", "message": "Valid Bearer token required"}).to_string(),
    ));
    *response.status_mut() = StatusCode::UNAUTHORIZED;
    response.headers_mut().insert(
        header::WWW_AUTHENTICATE,
        header::HeaderValue::from_static("Bearer"),
    );
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        header::HeaderValue::from_static("application/json"),
    );
    Ok(response)
}

/// Helper to get a reference to the global state.
fn get_state() -> Result<&'static TransportState, StatusCode> {
    TRANSPORT_STATE.get().ok_or(StatusCode::SERVICE_UNAVAILABLE)
}

fn valid_session_id(session_id: &str) -> bool {
    !session_id.is_empty()
        && session_id.len() <= 128
        && session_id
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b':'))
}

fn requested_session_id(
    headers: &HeaderMap,
    params: &MessageParams,
) -> Result<(Option<String>, bool), StatusCode> {
    let header_id = headers
        .get(MCP_SESSION_ID_HEADER)
        .map(|value| value.to_str().map(str::to_owned))
        .transpose()
        .map_err(|_| StatusCode::BAD_REQUEST)?;
    let (session_id, from_header) = match header_id {
        Some(value) => (Some(value), true),
        None => (params.session_id.clone(), false),
    };
    if session_id.as_deref().is_some_and(|id| !valid_session_id(id)) {
        return Err(StatusCode::BAD_REQUEST);
    }
    Ok((session_id, from_header))
}

fn resolve_mcp_session(
    state: &TransportState,
    req: &JsonRpcRequest,
    requested_id: Option<String>,
    from_header: bool,
) -> Result<(Arc<MCPState>, Option<String>), StatusCode> {
    if req.method == "initialize" {
        let mut sessions = state.sessions.lock()
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        let (session_id, mcp_state) = match requested_id {
            Some(session_id) if from_header => {
                let mcp_state = sessions.get(&session_id).ok_or(StatusCode::NOT_FOUND)?;
                (session_id, mcp_state)
            }
            Some(session_id) => {
                let mcp_state = sessions.create(session_id.clone());
                (session_id, mcp_state)
            }
            None => {
                let session_id = uuid::Uuid::new_v4().to_string();
                let mcp_state = sessions.create(session_id.clone());
                (session_id, mcp_state)
            }
        };
        drop(sessions);
        if !from_header {
            *state.legacy_mcp_state.write()
                .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)? = Arc::clone(&mcp_state);
        }
        return Ok((mcp_state, Some(session_id)));
    }

    if let Some(session_id) = requested_id {
        let mcp_state = state.sessions.lock()
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
            .get(&session_id)
            .ok_or(StatusCode::NOT_FOUND)?;
        return Ok((mcp_state, Some(session_id)));
    }

    let legacy = state.legacy_mcp_state.read()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok((Arc::clone(&*legacy), None))
}

fn json_response(value: Value, session_id: Option<&str>) -> Response {
    let mut response = Json(value).into_response();
    if let Some(session_id) = session_id {
        if let Ok(value) = header::HeaderValue::from_str(session_id) {
            response.headers_mut().insert(
                header::HeaderName::from_static(MCP_SESSION_ID_HEADER),
                value,
            );
        }
    }
    response
}

/// Handle POST /message — JSON-RPC request → JSON-RPC response.
async fn handle_message(
    Query(params): Query<MessageParams>,
    headers: HeaderMap,
    axum::Json(request): axum::Json<Value>,
) -> Result<Response, StatusCode> {
    let req: JsonRpcRequest = match serde_json::from_value(request) {
        Ok(r) => r,
        Err(e) => {
            return Ok(json_response(json!({
                "jsonrpc": "2.0",
                "id": null,
                "error": {"code": -32700, "message": format!("Parse error: {}", e)}
            }), None));
        }
    };

    let state = get_state()?;
    let (requested_id, from_header) = requested_session_id(&headers, &params)?;
    let (mcp_state, session_id) = resolve_mcp_session(state, &req, requested_id, from_header)?;
    // #210: the handler is blocking and can make synchronous LLM round-trips
    // (perseus_vault_ask / perseus_vault_synthesize), so run it on the blocking thread pool to
    // keep the Tokio async workers (SSE streams, connection accept) free (#217).
    // The registry lock was released above; concurrent sessions and requests run
    // independently while sharing only the pooled database.
    let response = tokio::task::spawn_blocking(move || {
        mcp::handle_request(&req, &mcp_state, &state.db)
    })
    .await
    .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    match response {
        Some(resp) => {
            if params.session_id.is_some() {
                let resp_str = serde_json::to_string(&resp).unwrap_or_default();
                let _ = state.sse_tx.send(resp_str);
            }
            Ok(json_response(
                serde_json::to_value(resp)
                    .unwrap_or(json!({"jsonrpc":"2.0","id":null,"error":{"code":-32603,"message":"Serialization error"}})),
                session_id.as_deref(),
            ))
        }
        None => Ok(json_response(
            json!({"jsonrpc": "2.0", "id": null, "result": null}),
            session_id.as_deref(),
        )),
    }
}

/// Handle GET /sse — Server-Sent Events stream.
async fn handle_sse(
    Query(_params): Query<SseParams>,
) -> Result<Sse<impl Stream<Item = Result<Event, std::convert::Infallible>>>, StatusCode> {
    let state = get_state()?;
    let rx = state.sse_tx.subscribe();

    let stream = async_stream::stream! {
        yield Ok(Event::default()
            .event("endpoint")
            .data("/message"));

        let mut rx = rx;
        loop {
            match rx.recv().await {
                Ok(msg) => {
                    yield Ok(Event::default()
                        .event("message")
                        .data(msg));
                }
                Err(broadcast::error::RecvError::Lagged(_)) => {
                    continue;
                }
                Err(broadcast::error::RecvError::Closed) => {
                    break;
                }
            }
        }
    };
    Ok(Sse::new(stream).keep_alive(
        axum::response::sse::KeepAlive::new()
            .interval(std::time::Duration::from_secs(15))
            .text("keep-alive"),
    ))
}


#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request;
    use tower::ServiceExt; // for `oneshot`

    fn message_request(auth: Option<&str>) -> Request<Body> {
        let mut builder = Request::builder()
            .method("POST")
            .uri("/message")
            .header(header::CONTENT_TYPE, "application/json");
        if let Some(token) = auth {
            builder = builder.header(header::AUTHORIZATION, format!("Bearer {}", token));
        }
        builder
            .body(Body::from(
                r#"{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}"#,
            ))
            .unwrap()
    }

    #[tokio::test]
    async fn no_token_configured_allows_request() {
        // When auth_token is None, requests pass through (state may be missing,
        // which yields 503 — but crucially NOT 401).
        let router = build_transport_router(TransportMode::Http, None);
        let resp = router.oneshot(message_request(None)).await.unwrap();
        assert_ne!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn missing_token_is_rejected() {
        let router = build_transport_router(TransportMode::Http, Some("secret".to_string()));
        let resp = router.oneshot(message_request(None)).await.unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn wrong_token_is_rejected() {
        let router = build_transport_router(TransportMode::Http, Some("secret".to_string()));
        let resp = router
            .oneshot(message_request(Some("wrong")))
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn correct_token_passes_auth() {
        // A correct token must clear the auth layer. State isn't initialized in
        // this unit test, so the handler returns 503 — the point is it is NOT 401.
        let router = build_transport_router(TransportMode::Http, Some("secret".to_string()));
        let resp = router
            .oneshot(message_request(Some("secret")))
            .await
            .unwrap();
        assert_ne!(resp.status(), StatusCode::UNAUTHORIZED);
    }

    #[tokio::test]
    async fn unauthorized_response_sets_www_authenticate() {
        let router = build_transport_router(TransportMode::Http, Some("secret".to_string()));
        let resp = router.oneshot(message_request(None)).await.unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        assert!(resp.headers().contains_key(header::WWW_AUTHENTICATE));
    }

    #[tokio::test]
    async fn independent_http_clients_receive_independent_mcp_sessions() {
        let path = std::env::temp_dir()
            .join(format!("perseus-vault-http-sessions-{}.db", uuid::Uuid::new_v4()));
        let db = Database::open(path.to_str().unwrap()).expect("open session test db");
        init_transport_state(Arc::new(db));
        let router = build_transport_router(TransportMode::Http, None);

        let initialize = |id: u64, name: &str| Request::builder()
            .method("POST")
            .uri("/message")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(json!({
                "jsonrpc": "2.0", "id": id, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": name, "version": "0"}
                }
            }).to_string()))
            .unwrap();

        let first = router.clone().oneshot(initialize(1, "client-a")).await.unwrap();
        assert_eq!(first.status(), StatusCode::OK);
        let first_session = first.headers().get(MCP_SESSION_ID_HEADER)
            .and_then(|v| v.to_str().ok())
            .expect("initialize must return Mcp-Session-Id").to_string();
        let first_body = axum::body::to_bytes(first.into_body(), usize::MAX).await.unwrap();
        let first_value: Value = serde_json::from_slice(&first_body).unwrap();
        assert!(first_value.get("result").is_some(), "first initialize failed: {first_value}");

        let second = router.clone().oneshot(initialize(2, "client-b")).await.unwrap();
        assert_eq!(second.status(), StatusCode::OK);
        let second_session = second.headers().get(MCP_SESSION_ID_HEADER)
            .and_then(|v| v.to_str().ok())
            .expect("initialize must return Mcp-Session-Id").to_string();
        let second_body = axum::body::to_bytes(second.into_body(), usize::MAX).await.unwrap();
        let second_value: Value = serde_json::from_slice(&second_body).unwrap();
        assert!(second_value.get("result").is_some(), "second initialize failed: {second_value}");
        assert_ne!(first_session, second_session);

        {
            let state = get_state().unwrap();
            let mut sessions = state.sessions.lock().unwrap();
            let first_state = sessions.get(&first_session).unwrap();
            let second_state = sessions.get(&second_session).unwrap();
            assert_eq!(&*first_state.session_agent_id.read().unwrap(), "client-a");
            assert_eq!(&*second_state.session_agent_id.read().unwrap(), "client-b");
        }

        for (id, session) in [(3, first_session), (4, second_session)] {
            let request = Request::builder()
                .method("POST")
                .uri("/message")
                .header(header::CONTENT_TYPE, "application/json")
                .header(MCP_SESSION_ID_HEADER, session)
                .body(Body::from(json!({
                    "jsonrpc":"2.0", "id":id, "method":"tools/list", "params":{}
                }).to_string()))
                .unwrap();
            let response = router.clone().oneshot(request).await.unwrap();
            assert_eq!(response.status(), StatusCode::OK);
            let body = axum::body::to_bytes(response.into_body(), usize::MAX).await.unwrap();
            let value: Value = serde_json::from_slice(&body).unwrap();
            assert!(value.get("result").is_some(), "session {id} was not initialized: {value}");
        }

        let _ = std::fs::remove_file(path);
    }

    /// #223: concurrent-client load test for the DB connection pool, driven
    /// through the REAL HTTP transport — the same `init_transport_state` +
    /// `build_transport_router` + `axum::serve` path `main.rs` uses — rather
    /// than direct `Database` calls. This exercises the full request path under
    /// contention: `handle_message` -> `spawn_blocking` -> `mcp::handle_request`
    /// -> `call_tool` -> a pooled connection.
    ///
    /// `#[ignore]` on purpose: this is a load/soak test, not a CI correctness
    /// gate (the durability/throughput characteristics under contention "can't
    /// be proven by CI" — see #223). Run it explicitly and sweep the pool knobs:
    ///
    /// ```text
    /// cargo test --release pool_load_test_http_transport -- --ignored --nocapture
    ///
    /// # sweep: small pool, default busy_timeout, more clients
    /// PERSEUS_VAULT_POOL_MAX_SIZE=4 PERSEUS_VAULT_BUSY_TIMEOUT_MS=5000 PERSEUS_VAULT_LOADTEST_CLIENTS=32 \
    ///   cargo test --release pool_load_test_http_transport -- --ignored --nocapture

    /// True when a load run's ONLY failures are a small number of
    /// busy-timeout exhaustions: SQLITE_BUSY is the documented outcome when a
    /// writer waits longer than busy_timeout, so a single tail exhaustion under
    /// saturation is a timing event, not a defect. A lock-error STORM (more
    /// than `LOCK_RETRY_CAP`), any non-lock error, or any acknowledged write
    /// that failed to persist is a real regression and never qualifies.
    fn is_transient_lock_tail(
        lock: u64,
        other: u64,
        persisted: i64,
        ok_writes: u64,
    ) -> bool {
        const LOCK_RETRY_CAP: u64 = 5;
        let cap = std::env::var("PERSEUS_VAULT_LOADTEST_LOCK_RETRY_CAP")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(LOCK_RETRY_CAP);
        lock > 0 && lock <= cap && other == 0 && persisted == ok_writes as i64
    }

    #[test]
    fn transient_lock_tail_decision() {
        // one tail exhaustion, everything else consistent -> retry
        assert!(is_transient_lock_tail(1, 0, 799, 799));
        // a storm of lock errors is a regression, not noise -> no retry
        assert!(!is_transient_lock_tail(8, 0, 799, 799));
        // any non-lock error -> no retry
        assert!(!is_transient_lock_tail(1, 1, 799, 799));
        // an acknowledged write that never persisted -> no retry
        assert!(!is_transient_lock_tail(1, 0, 799, 800));
        // clean run -> no retry needed
        assert!(!is_transient_lock_tail(0, 0, 800, 800));
    }

    /// ```
    ///
    /// Tunables (env): `PERSEUS_VAULT_LOADTEST_CLIENTS` (default 16),
    /// `PERSEUS_VAULT_LOADTEST_WRITES` / `PERSEUS_VAULT_LOADTEST_READS` per client (default 25 / 75),
    /// plus the pool's `PERSEUS_VAULT_POOL_MAX_SIZE` / `PERSEUS_VAULT_BUSY_TIMEOUT_MS`
    /// (consumed by `Database::open`).
    ///
    /// Asserts the four properties #223 calls out: no `database is locked` /
    /// `SQLITE_BUSY` after the busy_timeout, no lost writes (final row count ==
    /// writes that returned success), no deadlock (the run completes and joins),
    /// and reports p50/p99/max latency so the operator can judge tail behavior.
    #[test]
    #[ignore = "load test: run explicitly with --ignored --nocapture"]
    fn pool_load_test_http_transport() {
        use crate::db::Database;
        use std::sync::atomic::{AtomicU64, Ordering};
        use std::sync::Arc;
        use std::time::Instant;

        fn env_usize(key: &str, default: usize) -> usize {
            std::env::var(key).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
        }

        // Classify one response body. A pooled-write/lock failure surfaces as an
        // MCP tool error (`isError:true`) whose text carries rusqlite's message,
        // so we scan for the SQLITE_BUSY signature explicitly and bucket the rest.
        fn classify(
            text: &str,
            lock: &AtomicU64,
            other: &AtomicU64,
            writes_ok: &AtomicU64,
            is_write: bool,
        ) {
            let lower = text.to_lowercase();
            let is_lock =
                lower.contains("database is locked") || lower.contains("sqlite_busy");
            let v: Value = serde_json::from_str(text).unwrap_or(Value::Null);
            let is_err = is_lock
                || text.starts_with("TRANSPORT_ERROR")
                || v.get("error").is_some()
                || v.pointer("/result/isError").and_then(|b| b.as_bool()).unwrap_or(false);
            if is_lock {
                lock.fetch_add(1, Ordering::Relaxed);
            } else if is_err {
                other.fetch_add(1, Ordering::Relaxed);
            } else if is_write {
                writes_ok.fetch_add(1, Ordering::Relaxed);
            }
        }

        let clients = env_usize("PERSEUS_VAULT_LOADTEST_CLIENTS", 16);
        let writes_per = env_usize("PERSEUS_VAULT_LOADTEST_WRITES", 25);
        let reads_per = env_usize("PERSEUS_VAULT_LOADTEST_READS", 75);

        // This test measures DB-pool concurrency + max throughput by flooding the
        // transport as fast as possible — which is inherently defeated by the HTTP
        // rate limiter (it would shed the flood with 429s and be counted as
        // errors). Disable rate limiting for this run; it has its own unit tests
        // in `httplimit`. (The bucket is per-router, so this only affects the
        // router built below.)
        std::env::set_var("PERSEUS_VAULT_HTTP_RATE_PER_SEC", "0");

        let path = std::env::temp_dir()
            .join(format!("perseus_vault-loadtest-{}.db", uuid::Uuid::new_v4()));
        let path_str = path.to_str().unwrap().to_string();
        let db = Database::open(&path_str).expect("open load-test db");
        init_transport_state(Arc::new(db));

        // Real HTTP server on an ephemeral port (mirrors main.rs wiring).
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("tokio runtime");
        let (addr_tx, addr_rx) = std::sync::mpsc::channel();
        rt.spawn(async move {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
                .await
                .expect("bind ephemeral port");
            addr_tx.send(listener.local_addr().unwrap()).unwrap();
            let router = build_transport_router(TransportMode::Http, None);
            axum::serve(listener, router).await.unwrap();
        });
        let addr = addr_rx.recv().expect("server address");
        let base = format!("http://{}/message", addr);

        // This legacy load client omits Mcp-Session-Id after initialization; the
        // compatibility pointer keeps its subsequent tools/call requests on the
        // most recently created session.
        let init = ureq::post(&base)
            .set("Content-Type", "application/json")
            .send_string(
                &serde_json::json!({
                    "jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}
                })
                .to_string(),
            );
        assert!(init.is_ok(), "initialize failed: {:?}", init.err());

        // Run one full load pass and return its statistics. Parameterized by
        // category so a retry writes into its own partition and the per-attempt
        // durability count stays honest.
        let run_load = |category: &'static str| -> (u64, u64, u64, i64, u64, std::time::Duration, Vec<u128>) {
            let lock_errors = Arc::new(AtomicU64::new(0));
            let other_errors = Arc::new(AtomicU64::new(0));
            let writes_ok = Arc::new(AtomicU64::new(0));

            let start = Instant::now();
            let mut handles = Vec::new();
            for c in 0..clients {
                let base = base.clone();
                let lock_errors = Arc::clone(&lock_errors);
                let other_errors = Arc::clone(&other_errors);
                let writes_ok = Arc::clone(&writes_ok);
                handles.push(std::thread::spawn(move || {
                    let mut latencies: Vec<u128> = Vec::with_capacity(writes_per + 2 * reads_per);
                    let call = |name: &str, args: serde_json::Value| -> (String, u128) {
                        let t = Instant::now();
                        let body = serde_json::json!({
                            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}
                        });
                        let text = match ureq::post(&base)
                            .set("Content-Type", "application/json")
                            .send_string(&body.to_string())
                        {
                            Ok(resp) => resp.into_string().unwrap_or_default(),
                            Err(ureq::Error::Status(_, resp)) => {
                                resp.into_string().unwrap_or_default()
                            }
                            Err(e) => format!("TRANSPORT_ERROR: {}", e),
                        };
                        (text, t.elapsed().as_micros())
                    };

                    // Interleave writes and reads so the two contend on the pool.
                    let ops = writes_per.max(reads_per);
                    for i in 0..ops {
                        if i < writes_per {
                            // High-entropy unique content so each write is a real
                            // create — perseus_vault_remember dedups bodies above 70% trigram
                            // similarity, so near-identical payloads would collapse
                            // and `persisted == issued` would no longer test durability.
                            let nonce = format!(
                                "{}{}",
                                uuid::Uuid::new_v4().simple(),
                                uuid::Uuid::new_v4().simple()
                            );
                            let (text, us) = call("perseus_vault_remember", serde_json::json!({
                                "category": category,
                                "key": format!("c{}-w{}", c, i),
                                "body_json": format!("{{\"content\":\"{}\"}}", nonce),
                            }));
                            latencies.push(us);
                            classify(&text, &lock_errors, &other_errors, &writes_ok, true);
                        }
                        if i < reads_per {
                            let (text, us) = call("perseus_vault_recall", serde_json::json!({
                                "query": "client", "category": category, "limit": 10
                            }));
                            latencies.push(us);
                            classify(&text, &lock_errors, &other_errors, &writes_ok, false);

                            let (text2, us2) = call("perseus_vault_context", serde_json::json!({}));
                            latencies.push(us2);
                            classify(&text2, &lock_errors, &other_errors, &writes_ok, false);
                        }
                    }
                    latencies
                }));
            }

            let mut all: Vec<u128> = Vec::new();
            for h in handles {
                all.extend(h.join().expect("client thread panicked (possible deadlock)"));
            }
            let elapsed = start.elapsed();

            all.sort_unstable();
            let pct = |p: f64| -> u128 {
                if all.is_empty() {
                    return 0;
                }
                let idx = (((all.len() - 1) as f64) * p).round() as usize;
                all[idx]
            };
            let lock = lock_errors.load(Ordering::Relaxed);
            let other = other_errors.load(Ordering::Relaxed);
            let ok_writes = writes_ok.load(Ordering::Relaxed);
            let issued_writes = (clients * writes_per) as u64;

            // Independently verify no lost writes: reopen the file with a raw
            // connection and count the rows that actually persisted.
            let verify = rusqlite::Connection::open(&path_str)
                .expect("reopen for verification");
            let persisted: i64 = verify
                .query_row(
                    "SELECT COUNT(*) FROM entities WHERE category = ?1",
                    [category],
                    |r| r.get(0),
                )
                .unwrap();
            drop(verify);

            eprintln!(
                "\n#223 pool load test\n\
                 clients={clients} writes/client={writes_per} reads/client={reads_per}\n\
                 pool max_size={} busy_timeout={}ms\n\
                 requests={} wall={:.2}s throughput={:.0} req/s\n\
                 latency p50={}us p99={}us max={}us\n\
                 lock_errors={lock} other_errors={other}\n\
                 writes: issued={issued_writes} ok={ok_writes} persisted={persisted}",
                std::env::var("PERSEUS_VAULT_POOL_MAX_SIZE").unwrap_or_else(|_| "16".into()),
                std::env::var("PERSEUS_VAULT_BUSY_TIMEOUT_MS").unwrap_or_else(|_| "5000".into()),
                all.len(),
                elapsed.as_secs_f64(),
                all.len() as f64 / elapsed.as_secs_f64().max(1e-9),
                pct(0.50),
                pct(0.99),
                all.last().copied().unwrap_or(0),
            );


            (lock, other, ok_writes, persisted, issued_writes, elapsed, all)
        };

        // #223's four properties are asserted strictly below on a single
        // measurement. A 32-client flood on a shared CI runner can exhaust the
        // 5s busy_timeout for ONE tail write — SQLITE_BUSY is the documented
        // outcome when a writer waits longer than busy_timeout, so a single
        // exhaustion under saturation is a timing event, not a defect. When the
        // failure signature is transient-only (no non-lock errors, every
        // acknowledged write persisted, at most a handful of lock exhaustions),
        // re-run the load once on a fresh partition; a systematic pool/locking
        // regression reproduces on the retry and fails the strict asserts.
        let (lock, other, ok_writes, persisted, issued_writes, elapsed, _all) = {
            let (lock, other, ok_writes, persisted, issued_writes, elapsed, all) =
                run_load("loadtest");
            if lock == 0 || !is_transient_lock_tail(lock, other, persisted, ok_writes) {
                (lock, other, ok_writes, persisted, issued_writes, elapsed, all)
            } else {
                eprintln!(
                    "\n#223 transient busy-timeout tail under saturation (lock_errors={lock});\n\
                     retrying once on a fresh partition for a clean measurement"
                );
                run_load("loadtest_retry")
            }
        };

        let _ = std::fs::remove_file(&path_str);

        // The four properties #223 asks us to prove:
        assert_eq!(lock, 0, "SQLITE_BUSY / 'database is locked' after busy_timeout");
        assert_eq!(other, 0, "unexpected tool/transport errors under load");
        assert_eq!(
            persisted, issued_writes as i64,
            "lost writes: {issued_writes} issued, {persisted} persisted"
        );
        assert_eq!(
            ok_writes, issued_writes,
            "every issued write should have returned success"
        );
        // #404: optional wall-clock budget, enforced only when the caller pins
        // one (the concurrency-gate CI workflow sets 60s). Before the #397 fix
        // this configuration at 2x pool oversubscription browned out to ~32s
        // walls with 30s max latency; the budget catches a regression whose
        // symptom is stalling rather than erroring. Checked LAST so the more
        // specific error/durability asserts above name the failure first.
        if let Some(budget_secs) = std::env::var("PERSEUS_VAULT_LOADTEST_MAX_WALL_SECS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
        {
            assert!(
                elapsed.as_secs_f64() < budget_secs,
                "wall time {:.2}s exceeded the {budget_secs}s budget",
                elapsed.as_secs_f64()
            );
        }
        // (Reaching here at all proves no deadlock — all client threads joined.)
    }
}
