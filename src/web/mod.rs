pub mod dashboard_html;

use axum::{
    extract::{Path, Query, State},
    http::{header, Request, StatusCode},
    middleware::{self, Next},
    response::{Html, Json, Response},
    routing::get,
    Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use std::sync::Arc;
use tower_http::cors::{AllowOrigin, CorsLayer};

use crate::db::Database;

/// Shared application state for the web dashboard.
///
/// #402: no Mutex — `Database` is Sync (internally r2d2-pooled, see the #210
/// comment in transport.rs), so handlers share it by reference and run in
/// parallel. This is the SAME `Arc<Database>` the MCP transport/stdio server
/// uses (threaded from main.rs), not a second `Database::open` on the same
/// file — one process, one pool. Handlers run their DB work on the blocking
/// thread pool via `spawn_blocking`, mirroring transport.rs (#217).
#[derive(Clone)]
pub struct WebState {
    pub db: Arc<Database>,
    pub auth_token: Option<String>,
}

/// Build the Axum router with all API endpoints and the dashboard HTML.
pub fn build_router(db: Arc<Database>, auth_token: Option<String>) -> Router {
    let state = WebState { db, auth_token };

    // Tighten CORS: if auth token is set, allow specific origins; otherwise disable CORS
    let cors = if state.auth_token.is_some() {
        // With auth, we can safely allow CORS but restrict to known origins
        CorsLayer::new()
            .allow_origin(AllowOrigin::mirror_request())
            .allow_methods([axum::http::Method::GET])
            .allow_headers([header::AUTHORIZATION, header::CONTENT_TYPE])
    } else {
        // No auth: listen only on 127.0.0.1 (caller should ensure this), CORS disabled
        CorsLayer::new()
            .allow_origin(AllowOrigin::mirror_request())
            .allow_methods([axum::http::Method::GET])
            .allow_headers([header::AUTHORIZATION, header::CONTENT_TYPE])
    };

    Router::new()
        .route("/", get(dashboard))
        .route("/api/entities", get(list_entities))
        .route("/api/entities/{id}", get(entity_detail))
        .route("/api/search", get(search))
        .route("/api/stats", get(stats))
        .route("/api/journal", get(journal))
        .route("/api/graph", get(graph))
        .route_layer(middleware::from_fn_with_state(state.clone(), auth_middleware))
        .layer(cors)
        .with_state(state)
}

/// Middleware: require Bearer token if auth_token is set.
async fn auth_middleware(
    State(state): State<WebState>,
    request: Request<axum::body::Body>,
    next: Next,
) -> Result<Response, StatusCode> {
    // If no auth token is configured, allow all requests
    let expected = match &state.auth_token {
        Some(token) => token,
        None => return Ok(next.run(request).await),
    };

    // Check Authorization header
    let auth_header = request
        .headers()
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok());

    if let Some(auth) = auth_header {
        if let Some(token) = auth.strip_prefix("Bearer ") {
            if token == expected {
                return Ok(next.run(request).await);
            }
        }
    }

    // Return 401 with WWW-Authenticate header
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

// ─── Dashboard HTML ──────────────────────────────────────────────────

async fn dashboard() -> Html<&'static str> {
    Html(dashboard_html::HTML)
}

// ─── API Query params ────────────────────────────────────────────────

#[derive(Debug, Deserialize)]
struct EntityListParams {
    #[serde(default)]
    offset: i64,
    #[serde(default = "default_page_limit")]
    limit: i64,
    #[serde(default)]
    category: Option<String>,
    #[serde(default)]
    layer: Option<String>,
    /// Scope the entity list to a single workspace. Without this, a
    /// federated (multi-workspace) vault's dashboard showed every
    /// workspace's memory in one unfiltered list. `?workspace=` (present
    /// but empty) scopes strictly to the global `''` workspace (#408);
    /// omit the param entirely for the unscoped view.
    #[serde(default)]
    workspace: Option<String>,
}

fn default_page_limit() -> i64 {
    50
}

#[derive(Debug, Deserialize)]
struct SearchParams {
    q: String,
    #[serde(default = "default_page_limit")]
    limit: i64,
    #[serde(default)]
    category: Option<String>,
    /// Same workspace scoping as `EntityListParams`.
    #[serde(default)]
    workspace: Option<String>,
}

#[derive(Debug, Deserialize)]
struct JournalParams {
    #[serde(default = "default_page_limit")]
    limit: i64,
    // NOTE: intentionally no `workspace` field here yet — the
    // `journal` table has no workspace_hash column, so there is nothing to
    // scope by. See the doc comment on `Database::get_recent_journal` for
    // why this needs a schema migration rather than a query-param fix.
}

#[derive(Debug, Deserialize)]
struct GraphParams {
    /// Scope the entity graph to a single workspace. Without this, the
    /// dashboard's graph tab rendered nodes and edges from every workspace
    /// in one force-directed layout.
    #[serde(default)]
    workspace: Option<String>,
    /// Max nodes per response (#402). No param → capped default of 500,
    /// NOT unbounded; clamped to [1, 5000]. The response reports
    /// `total_nodes`/`truncated` so the dashboard can indicate truncation.
    #[serde(default = "default_graph_limit")]
    limit: i64,
    #[serde(default)]
    offset: i64,
}

/// Default node cap for `/api/graph` (#402).
const DEFAULT_GRAPH_LIMIT: i64 = 500;
/// Hard ceiling for an explicit `?limit=` on every list-shaped endpoint
/// (#402 for /api/graph; #413 extended the same clamp to /api/entities,
/// /api/search, and /api/journal): honors large explicit requests without
/// reopening the unbounded-dump hole — one `?limit=1000000` request could
/// return ~15MB and pin a shared-pool connection for seconds.
const MAX_API_LIMIT: i64 = 5000;

fn default_graph_limit() -> i64 {
    DEFAULT_GRAPH_LIMIT
}

/// Run a blocking DB closure on tokio's blocking thread pool (#402), exactly
/// like the MCP transport does (#217): the tokio async workers stay free, and
/// concurrent handlers each check out their own pooled connection instead of
/// serializing on a process-global lock. Errors are flattened to 500 — the
/// dashboard API intentionally exposes no internal error detail (#354).
async fn blocking_db<T, F>(db: Arc<Database>, f: F) -> Result<T, StatusCode>
where
    T: Send + 'static,
    F: FnOnce(&Database) -> Result<T, StatusCode> + Send + 'static,
{
    tokio::task::spawn_blocking(move || f(&db))
        .await
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
}

// ─── Handlers ────────────────────────────────────────────────────────

async fn list_entities(
    State(state): State<WebState>,
    Query(params): Query<EntityListParams>,
) -> Result<Json<Value>, StatusCode> {
    // #413: same param hygiene /api/graph got in #402 — default (50) stays,
    // explicit limits are clamped to [1, MAX_API_LIMIT] so a single request
    // can't dump the whole table through a shared-pool connection.
    // (Non-numeric / overflowing `?limit=` is already a 400 via Query<i64>.)
    let limit = params.limit.clamp(1, MAX_API_LIMIT);
    let offset = params.offset.max(0);
    let (items, total) = blocking_db(state.db.clone(), move |db| {
        let entities = db
            .list_entities(
                offset,
                limit,
                params.category.as_deref(),
                params.layer.as_deref(),
                params.workspace.as_deref(),
            )
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        // `total` is the true count of matching rows (via a separate
        // COUNT(*) query with the same filters, no LIMIT/OFFSET), not just
        // "how many rows came back in this page" — the previous `items.len()`
        // made it impossible for a client to tell "there are more pages" from
        // "this is everything".
        //
        // NOTE (#402): list + count are two reads without a shared snapshot.
        // The old Mutex incidentally made them atomic; both reads are cheap
        // and the dashboard only uses `total` for paging hints, so a rare
        // off-by-a-write total is acceptable — not worth a read transaction.
        let total = db
            .count_entities(
                params.category.as_deref(),
                params.layer.as_deref(),
                params.workspace.as_deref(),
            )
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        let items: Vec<Value> = entities.iter().map(|e| e.to_json_expanded()).collect();
        Ok((items, total))
    })
    .await?;

    // `limit`/`offset` echo the clamped effective values (#413), mirroring
    // /api/graph — so callers (and tests) can see what was actually applied.
    Ok(Json(json!({ "items": items, "total": total, "limit": limit, "offset": offset })))
}

async fn entity_detail(
    State(state): State<WebState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, StatusCode> {
    let entity = blocking_db(state.db.clone(), move |db| {
        match db.get_entity_by_id_public(&id) {
            Ok(Some(entity)) => Ok(entity),
            Ok(None) => Err(StatusCode::NOT_FOUND),
            Err(_) => Err(StatusCode::INTERNAL_SERVER_ERROR),
        }
    })
    .await?;
    Ok(Json(entity.to_json_expanded()))
}

async fn search(
    State(state): State<WebState>,
    Query(params): Query<SearchParams>,
) -> Result<Json<Value>, StatusCode> {
    // #413: same clamp as /api/entities — /api/search had the identical
    // unbounded-`?limit=` shape.
    let limit = params.limit.clamp(1, MAX_API_LIMIT);
    let items = blocking_db(state.db.clone(), move |db| {
        let recall_params = crate::models::RecallParams {
            query: params.q.clone(),
            category: params.category.clone(),
            limit,
            // recall() already supports workspace_hash scoping (v1.2.0) —
            // the dashboard just wasn't passing it through, so search leaked
            // cross-workspace results the same way list_entities did.
            workspace_hash: params.workspace.clone(),
            ..Default::default()
        };
        let entities = db
            .recall(&recall_params)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
        Ok(entities.iter().map(|e| e.to_json_expanded()).collect::<Vec<Value>>())
    })
    .await?;
    // Search doesn't paginate today (single-shot recall with a limit), so
    // `total` here remains "count in this response" — unlike list_entities,
    // there's no separate unlimited COUNT(*) query backing FTS5 relevance
    // ranking, and adding one would double the recall cost for a value the
    // UI doesn't currently use for pagination. Documented so it doesn't get
    // silently assumed to mean the same thing as list_entities' `total`.
    Ok(Json(json!({ "items": items, "total": items.len(), "limit": limit })))
}

async fn stats(State(state): State<WebState>) -> Result<Json<Value>, StatusCode> {
    let s = blocking_db(state.db.clone(), move |db| {
        db.stats().map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
    })
    .await?;
    Ok(Json(
        serde_json::to_value(s).unwrap_or(json!({ "error": "serialization failed" })),
    ))
}

async fn journal(
    State(state): State<WebState>,
    Query(params): Query<JournalParams>,
) -> Result<Json<Value>, StatusCode> {
    // #413: /api/journal had the same unbounded-`?limit=` hole as
    // /api/entities and /api/search — `get_recent_journal` passes the value
    // straight into SQL `LIMIT`. Clamp it identically.
    let limit = params.limit.clamp(1, MAX_API_LIMIT);
    let events = blocking_db(state.db.clone(), move |db| {
        db.get_recent_journal(limit)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
    })
    .await?;

    Ok(Json(json!({ "items": events, "total": events.len(), "limit": limit })))
}

async fn graph(
    State(state): State<WebState>,
    Query(params): Query<GraphParams>,
) -> Result<Json<Value>, StatusCode> {
    // #402: capped by default, clamped ceiling for explicit requests.
    let limit = params.limit.clamp(1, MAX_API_LIMIT);
    let offset = params.offset.max(0);
    let (nodes, edges, total_nodes) = blocking_db(state.db.clone(), move |db| {
        db.get_entity_graph(params.workspace.as_deref(), limit, offset)
            .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)
    })
    .await?;

    let truncated = (nodes.len() as i64) < total_nodes;
    Ok(Json(json!({
        "nodes": nodes,
        "edges": edges,
        // #402: totals + truncation flag so the dashboard (or any API
        // consumer) can tell "this is everything" from "this is a page".
        "total_nodes": total_nodes,
        "returned_nodes": nodes.len(),
        "truncated": truncated,
        "limit": limit,
        "offset": offset,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::Request as HttpRequest;
    use tower::ServiceExt;

    fn temp_db() -> (Arc<Database>, String) {
        let dir = std::env::temp_dir();
        let path = dir.join(format!("mimir-web-test-{}.db", uuid::Uuid::new_v4()));
        let path_str = path.to_str().unwrap().to_string();
        let db = Database::open(&path_str).expect("open test db");
        (Arc::new(db), path_str)
    }

    fn make_entity(
        id: &str,
        category: &str,
        key: &str,
        body: &str,
        workspace_hash: &str,
    ) -> crate::models::Entity {
        let mut e: crate::models::Entity = serde_json::from_value(serde_json::json!({
            "id": id,
            "category": category,
            "key": key,
            "body_json": body,
            "created_at_unix_ms": 0,
            "last_accessed_unix_ms": 0,
        }))
        .unwrap();
        e.workspace_hash = workspace_hash.to_string();
        e
    }

    async fn body_json(response: Response) -> Value {
        let bytes = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        serde_json::from_slice(&bytes).unwrap()
    }

    // ── Auth middleware ──────────────────────────────────────────────

    #[tokio::test]
    async fn no_token_configured_allows_request() {
        let (db, path) = temp_db();
        let router = build_router(db, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/stats")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn correct_bearer_token_passes_auth() {
        let (db, path) = temp_db();
        let router = build_router(db, Some("secret-token".to_string()));
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/stats")
                    .header("Authorization", "Bearer secret-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn missing_token_is_rejected() {
        let (db, path) = temp_db();
        let router = build_router(db, Some("secret-token".to_string()));
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/stats")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        assert_eq!(
            resp.headers().get(header::WWW_AUTHENTICATE).unwrap(),
            "Bearer"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn wrong_token_is_rejected() {
        let (db, path) = temp_db();
        let router = build_router(db, Some("secret-token".to_string()));
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/stats")
                    .header("Authorization", "Bearer wrong-token")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
        let _ = std::fs::remove_file(&path);
    }

    // ── list_entities ────────────────────────────────────────────────

    #[tokio::test]
    async fn list_entities_returns_items_and_true_total() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            let bodies = [
                r#"{"note":"alpha aardvark architecture migration plan"}"#,
                r#"{"note":"beta bumblebee billing pipeline rewrite"}"#,
                r#"{"note":"gamma giraffe gateway rate limiting rollout"}"#,
            ];
            for (i, body) in bodies.iter().enumerate() {
                db.remember(&make_entity(
                    &format!("e{i}"),
                    "insight",
                    &format!("k{i}"),
                    body,
                    "",
                ))
                .unwrap();
            }
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?limit=2")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(v["items"].as_array().unwrap().len(), 2, "page size respected");
        assert_eq!(
            v["total"], 3,
            "total must be the true row count, not the page size"
        );
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn list_entities_scopes_to_workspace() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity("e-a", "insight", "k-a", "{}", "alpha"))
                .unwrap();
            db.remember(&make_entity("e-b", "insight", "k-b", "{}", "beta"))
                .unwrap();
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?workspace=alpha")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        let items = v["items"].as_array().unwrap();
        assert_eq!(
            items.len(),
            1,
            "workspace filter must exclude the other workspace's entity, got {:?}",
            items
        );
        assert_eq!(items[0]["key"], "k-a");
        assert_eq!(v["total"], 1);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn list_entities_without_workspace_param_sees_all_workspaces() {
        // Backward-compat: omitting ?workspace= must preserve the original
        // unscoped behavior (single-workspace vaults are the common case).
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity("e-a", "insight", "k-a", "{}", "alpha"))
                .unwrap();
            db.remember(&make_entity("e-b", "insight", "k-b", "{}", "beta"))
                .unwrap();
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["total"], 2);
        let _ = std::fs::remove_file(&path);
    }

    // ── search ────────────────────────────────────────────────────────

    #[tokio::test]
    async fn search_scopes_to_workspace() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity(
                "e-a",
                "insight",
                "k-a",
                r#"{"note":"zephyr marker alpha unique"}"#,
                "alpha",
            ))
            .unwrap();
            db.remember(&make_entity(
                "e-b",
                "insight",
                "k-b",
                r#"{"note":"zephyr marker beta unique"}"#,
                "beta",
            ))
            .unwrap();
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/search?q=zephyr&workspace=alpha")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        let items = v["items"].as_array().unwrap();
        assert!(
            items.iter().all(|i| i["key"] == "k-a"),
            "search must not return the other workspace's entity: {:?}",
            items
        );
        let _ = std::fs::remove_file(&path);
    }

    // ── graph ─────────────────────────────────────────────────────────

    #[tokio::test]
    async fn graph_scopes_nodes_and_drops_cross_workspace_edges() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity("g-a", "insight", "node-a", "{}", "alpha"))
                .unwrap();
            db.remember(&make_entity("g-b", "insight", "node-b", "{}", "beta"))
                .unwrap();
            // Link node-a (alpha) -> node-b (beta): a cross-workspace edge
            // that must be dropped when the graph is scoped to "alpha".
            db.link("insight", "node-a", "g-b", "depends_on").unwrap();
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph?workspace=alpha")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        let nodes = v["nodes"].as_array().unwrap();
        let edges = v["edges"].as_array().unwrap();
        assert_eq!(nodes.len(), 1, "only the alpha-workspace node should appear: {:?}", nodes);
        assert_eq!(
            edges.len(),
            0,
            "edge to a node outside the scope must be dropped, not dangling: {:?}",
            edges
        );
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn graph_without_workspace_param_sees_all_workspaces() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity("g-a", "insight", "node-a", "{}", "alpha"))
                .unwrap();
            db.remember(&make_entity("g-b", "insight", "node-b", "{}", "beta"))
                .unwrap();
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["nodes"].as_array().unwrap().len(), 2);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn graph_default_is_capped_and_reports_totals() {
        // #402: /api/graph must NOT return the entire graph unpaginated.
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            for i in 0..510 {
                db.remember(&make_entity(
                    &format!("cap-{i:04}"),
                    "insight",
                    &format!("cap-key-{i:04}"),
                    // Bodies must be distinct: remember() dedupes similar content.
                    &format!(r#"{{"note":"cap {i} {}"}}"#, uuid::Uuid::new_v4()),
                    "",
                ))
                .unwrap();
            }
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        let nodes = v["nodes"].as_array().unwrap();
        assert_eq!(
            nodes.len(),
            500,
            "no params must mean capped default (500), not unbounded — got {} nodes",
            nodes.len()
        );
        assert_eq!(v["total_nodes"], 510, "response must report the true total");
        assert_eq!(v["truncated"], true, "response must flag truncation");
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn graph_honors_limit_param_and_reports_totals() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            for i in 0..5 {
                db.remember(&make_entity(
                    &format!("pg-{i}"),
                    "insight",
                    &format!("pg-key-{i}"),
                    &format!(r#"{{"note":"pg {i} {}"}}"#, uuid::Uuid::new_v4()),
                    "",
                ))
                .unwrap();
            }
        }
        let router = build_router(db_arc, None);
        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph?limit=2")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(
            v["nodes"].as_array().unwrap().len(),
            2,
            "limit param must cap the node count"
        );
        assert_eq!(v["total_nodes"], 5);
        assert_eq!(v["truncated"], true);

        // offset pages must be disjoint and cover the whole set
        let mut seen = std::collections::HashSet::new();
        for offset in [0, 2, 4] {
            let resp = router
                .clone()
                .oneshot(
                    HttpRequest::builder()
                        .uri(format!("/api/graph?limit=2&offset={offset}"))
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            let v = body_json(resp).await;
            for n in v["nodes"].as_array().unwrap() {
                assert!(
                    seen.insert(n["id"].as_str().unwrap().to_string()),
                    "offset pages must not overlap"
                );
            }
        }
        assert_eq!(seen.len(), 5, "pages must cover every node exactly once");
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn graph_under_cap_is_not_truncated() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            for i in 0..3 {
                db.remember(&make_entity(
                    &format!("sm-{i}"),
                    "insight",
                    &format!("sm-key-{i}"),
                    &format!(r#"{{"note":"sm {i} {}"}}"#, uuid::Uuid::new_v4()),
                    "",
                ))
                .unwrap();
            }
        }
        let router = build_router(db_arc, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["nodes"].as_array().unwrap().len(), 3);
        assert_eq!(v["total_nodes"], 3);
        assert_eq!(v["truncated"], false);
        let _ = std::fs::remove_file(&path);
    }

    // ── limit clamps (#413) ───────────────────────────────────────────

    #[tokio::test]
    async fn entities_limit_is_clamped_not_unbounded() {
        // #413: /api/entities accepted any `?limit=` and passed it straight
        // into SQL — `?limit=1000000` dumped the whole table (14.7MB/1.5s at
        // 20k rows) through a shared-pool connection.
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            for i in 0..3 {
                db.remember(&make_entity(
                    &format!("lim-{i}"),
                    "insight",
                    &format!("lim-key-{i}"),
                    &format!(r#"{{"note":"lim {i} {}"}}"#, uuid::Uuid::new_v4()),
                    "",
                ))
                .unwrap();
            }
        }
        let router = build_router(db_arc, None);

        // An absurd explicit limit is clamped to the ceiling, not honored.
        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?limit=999999")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let v = body_json(resp).await;
        assert_eq!(
            v["limit"], MAX_API_LIMIT,
            "effective limit must be clamped to the {MAX_API_LIMIT} ceiling, got {}",
            v["limit"]
        );
        assert_eq!(v["items"].as_array().unwrap().len(), 3);

        // limit=0 clamps up to 1 — previously it hit SQL as `LIMIT 0` and
        // returned nothing.
        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?limit=0")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(
            v["items"].as_array().unwrap().len(),
            1,
            "limit=0 must clamp to 1 row, not pass LIMIT 0 through to SQL"
        );

        // Negative offset is floored to 0, same as /api/graph.
        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?offset=-5")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["offset"], 0);
        assert_eq!(v["items"].as_array().unwrap().len(), 3);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn non_numeric_or_overflowing_limit_is_400() {
        // #413 param hygiene, matching /api/graph: garbage `?limit=` values
        // are a client error, not a silent default or a 500.
        let (db, path) = temp_db();
        let router = build_router(db, None);
        for uri in [
            "/api/entities?limit=abc",
            "/api/entities?limit=99999999999999999999999",
            "/api/search?q=x&limit=abc",
            "/api/journal?limit=abc",
            "/api/graph?limit=abc",
        ] {
            let resp = router
                .clone()
                .oneshot(HttpRequest::builder().uri(uri).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(
                resp.status(),
                StatusCode::BAD_REQUEST,
                "{uri} must be rejected with 400"
            );
        }
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn search_and_journal_limits_are_clamped() {
        // #413: /api/search and /api/journal had the identical unbounded
        // `?limit=` shape as /api/entities.
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity(
                "sj-1",
                "insight",
                "sj-key-1",
                r#"{"note":"quasar clamp marker"}"#,
                "",
            ))
            .unwrap();
        }
        let router = build_router(db_arc, None);
        for uri in ["/api/search?q=quasar&limit=999999", "/api/journal?limit=999999"] {
            let resp = router
                .clone()
                .oneshot(HttpRequest::builder().uri(uri).body(Body::empty()).unwrap())
                .await
                .unwrap();
            assert_eq!(resp.status(), StatusCode::OK, "{uri}");
            let v = body_json(resp).await;
            assert_eq!(
                v["limit"], MAX_API_LIMIT,
                "{uri}: effective limit must be clamped, got {}",
                v["limit"]
            );
        }
        let _ = std::fs::remove_file(&path);
    }

    // ── strict empty-workspace scoping (#408) ─────────────────────────

    #[tokio::test]
    async fn empty_workspace_param_scopes_to_global_rows_only() {
        // #408: `?workspace=` (present but empty) used to silently mean
        // UNSCOPED on /api/entities and /api/graph while recall treated ""
        // as strict-global. Now every surface is strict: Some("") = only the
        // global '' rows; omit the param for the unscoped view.
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity("ws-g", "insight", "k-global", "{}", ""))
                .unwrap();
            db.remember(&make_entity("ws-a", "insight", "k-alpha", "{}", "alpha"))
                .unwrap();
        }
        let router = build_router(db_arc, None);

        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities?workspace=")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        let items = v["items"].as_array().unwrap();
        assert_eq!(
            items.len(),
            1,
            "?workspace= must scope to global-'' rows only, got {items:?}"
        );
        assert_eq!(items[0]["key"], "k-global");
        assert_eq!(v["total"], 1, "count_entities must use the same strict scope");

        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/graph?workspace=")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(
            v["nodes"].as_array().unwrap().len(),
            1,
            "graph ?workspace= must scope to global-'' nodes only"
        );
        assert_eq!(v["total_nodes"], 1);

        // Omitting the param is still the unscoped view (unchanged).
        let resp = router
            .clone()
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        let v = body_json(resp).await;
        assert_eq!(v["total"], 2);
        let _ = std::fs::remove_file(&path);
    }

    // ── concurrency (#402) ────────────────────────────────────────────

    /// Deterministic overlap proof for the handler DB path: two in-flight
    /// `blocking_db` closures rendezvous on a 2-party barrier INSIDE their DB
    /// closure. That can only complete if both closures are executing
    /// simultaneously — under the old `Arc<Mutex<Database>>` (one closure at a
    /// time, lock held across the whole call) this rendezvous is impossible
    /// and the test would deadlock; the timeout turns that into a failure.
    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn db_closures_overlap_instead_of_serializing() {
        let (db, path) = temp_db();
        let barrier = Arc::new(std::sync::Barrier::new(2));
        let (b1, b2) = (barrier.clone(), barrier.clone());
        let (d1, d2) = (db.clone(), db.clone());

        let fut = async move {
            let a = blocking_db(d1, move |db| {
                // Real DB work on this side of the rendezvous...
                let s = db.stats().map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
                b1.wait(); // ...while the peer is also mid-closure.
                Ok(s.total_entities)
            });
            let b = blocking_db(d2, move |db| {
                let s = db.stats().map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
                b2.wait();
                Ok(s.total_entities)
            });
            tokio::join!(a, b)
        };
        let (ra, rb) = tokio::time::timeout(std::time::Duration::from_secs(10), fut)
            .await
            .expect("concurrent DB closures must overlap, not serialize (deadlock = old global-mutex behavior)");
        assert_eq!(ra.unwrap(), 0);
        assert_eq!(rb.unwrap(), 0);
        let _ = std::fs::remove_file(&path);
    }

    /// Full-stack smoke: a burst of simultaneous requests across every API
    /// endpoint against ONE shared Database (no per-surface pool) all succeed.
    #[tokio::test(flavor = "multi_thread", worker_threads = 4)]
    async fn concurrent_requests_across_endpoints_all_succeed() {
        let (db_arc, path) = temp_db();
        {
            let db = &db_arc;
            db.remember(&make_entity(
                "cc-1",
                "insight",
                "cc-key-1",
                r#"{"note":"zephyr concurrency smoke marker"}"#,
                "",
            ))
            .unwrap();
        }
        let router = build_router(db_arc, None);
        let uris = [
            "/api/entities",
            "/api/search?q=zephyr",
            "/api/stats",
            "/api/journal",
            "/api/graph",
            "/api/entities?limit=1",
            "/api/graph?limit=10",
            "/api/stats",
        ];
        let mut handles = Vec::new();
        for uri in uris {
            let r = router.clone();
            handles.push(tokio::spawn(async move {
                r.oneshot(
                    HttpRequest::builder()
                        .uri(uri)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap()
                .status()
            }));
        }
        for h in handles {
            assert_eq!(h.await.unwrap(), StatusCode::OK);
        }
        let _ = std::fs::remove_file(&path);
    }

    /// Type-level regression guard (#402): the web state must stay lock-free —
    /// a shared Sync Database, not a Mutex. (If someone reintroduces
    /// `Arc<Mutex<Database>>`, this stops compiling at the field type.)
    #[test]
    fn web_state_is_send_sync_and_lock_free() {
        fn assert_send_sync<T: Send + Sync>() {}
        assert_send_sync::<WebState>();
        // Field type pinned: Arc<Database>, not Arc<Mutex<Database>>.
        fn _pin_field_type(s: &WebState) -> &Arc<Database> {
            &s.db
        }
    }

    // ── entity_detail / stats / journal smoke tests ──────────────────

    #[tokio::test]
    async fn entity_detail_returns_404_for_missing_id() {
        let (db, path) = temp_db();
        let router = build_router(db, None);
        let resp = router
            .oneshot(
                HttpRequest::builder()
                    .uri("/api/entities/does-not-exist")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::NOT_FOUND);
        let _ = std::fs::remove_file(&path);
    }

    #[tokio::test]
    async fn stats_and_journal_endpoints_respond_ok() {
        let (db, path) = temp_db();
        let router = build_router(db, None);
        for uri in ["/api/stats", "/api/journal"] {
            let resp = router
                .clone()
                .oneshot(
                    HttpRequest::builder()
                        .uri(uri)
                        .body(Body::empty())
                        .unwrap(),
                )
                .await
                .unwrap();
            assert_eq!(resp.status(), StatusCode::OK, "endpoint {} failed", uri);
        }
        let _ = std::fs::remove_file(&path);
    }
}
