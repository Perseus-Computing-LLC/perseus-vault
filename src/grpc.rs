// gRPC server — maps all Perseus Vault (formerly Mneme/Mimir) MCP tools to
// protobuf RPCs. NOTE: the underlying "mneme.v1" proto package/service names
// and generated Rust types (MnemeGrpcServer, etc.) are left unchanged — this
// is a wire contract external gRPC clients depend on by that literal name,
// same category as the "mimir_*" MCP tool names and the MCP Registry LABEL.
// Renaming those is a breaking API change to schedule separately, not a
// branding fix.
// Enabled via "grpc" feature flag. Compiles the proto in build.rs.

#[cfg(feature = "grpc")]
pub mod grpc {
    tonic::include_proto!("mneme.v1");

    use std::sync::Arc;
    use tonic::{Request, Response, Status};

    use crate::db::Database;
    use crate::models;

    // #402: no Mutex — `Database` is Sync (internally r2d2-pooled, see the
    // #210 comment in transport.rs), and this is the SAME `Arc<Database>` the
    // other surfaces use: one process, one pool. Concurrent RPCs each check
    // out their own pooled connection instead of serializing on a global lock.
    pub struct MnemeGrpcServer {
        db: Arc<Database>,
    }

    impl MnemeGrpcServer {
        pub fn new(db: Arc<Database>) -> Self {
            Self { db }
        }
    }

    // Helper to run DB operations on the blocking thread pool.
    //
    // #402: DB work is synchronous rusqlite, so it must not run inline in an
    // async fn — that stalls a tonic/tokio runtime worker for the duration of
    // the query. Mirror the MCP HTTP transport (#217): `spawn_blocking` keeps
    // the async workers free, and with no mutex the closures run in parallel.
    //
    // Error hygiene (#354): this module is a documented external wire contract,
    // so internal error text (rusqlite constraint/column names, file paths)
    // must not reach remote clients. Match the HTTP surface (src/web/mod.rs,
    // which returns a bare 500 with no detail): log the detail server-side,
    // return a generic INTERNAL to the client. Handlers that raise a *typed*
    // Status inside the closure (e.g. get_entity's not_found) get it passed
    // through unchanged instead of being flattened into INTERNAL.
    // (sanitize_error runs INSIDE the blocking closure: `Box<dyn Error>` is
    // not Send, so it must be mapped to a `Status` before crossing back.)
    async fn with_db<T>(
        server: &MnemeGrpcServer,
        f: impl FnOnce(&Database) -> Result<T, Box<dyn std::error::Error>> + Send + 'static,
    ) -> Result<T, Status>
    where
        T: Send + 'static,
    {
        let db = Arc::clone(&server.db);
        tokio::task::spawn_blocking(move || f(&db).map_err(sanitize_error))
            .await
            .map_err(|e| {
                eprintln!("mimir grpc: blocking task join error: {e}");
                Status::internal("internal error")
            })?
    }

    /// Map a handler error to the client-facing Status: intentional `Status`
    /// values pass through; everything else is logged and genericized.
    fn sanitize_error(e: Box<dyn std::error::Error>) -> Status {
        match e.downcast::<Status>() {
            Ok(status) => *status,
            Err(e) => {
                eprintln!("mimir grpc: internal error: {e}");
                Status::internal("internal error")
            }
        }
    }

    #[tonic::async_trait]
    impl mneme_server::Mneme for MnemeGrpcServer {
        // ── CRUD ──
        async fn remember(&self, req: Request<RememberRequest>) -> Result<Response<RememberResponse>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                // Same id convention as the MCP surface (handle_remember):
                // db.remember does NOT generate ids — an empty id here would be
                // inserted verbatim, producing an entity unreachable by id.
                let raw_id = uuid::Uuid::new_v4().to_string().replace('-', "");
                let id = format!("mem-{}", &raw_id[..12.min(raw_id.len())]);
                let entity = models::Entity {
                    id,
                    category: r.category,
                    key: r.key,
                    body_json: r.body_json,
                    status: r.status,
                    entity_type: r.r#type,
                    tags: r.tags,
                    decay_score: r.importance,
                    retrieval_count: 0,
                    layer: "buffer".to_string(),
                    topic_path: r.topic_path,
                    archived: false,
                    archive_reason: String::new(),
                    links: vec![],
                    verified: false,
                    source: "grpc".to_string(),
                    always_on: r.always_on,
                    certainty: r.certainty,
                    workspace_hash: r.workspace_hash,
                    agent_id: r.agent_id,
                    visibility: r.visibility,
                    created_at_unix_ms: crate::db::now_ms(),
                    last_accessed_unix_ms: crate::db::now_ms(),
                    follow_count: 0,
                    miss_count: 0,
                    follow_rate: 0.0,
                    efficacy_status: "unverified".to_string(),
                    embedding: None,
                };
                let (id, action) = db.remember(&entity)?;
                Ok(Response::new(RememberResponse { id, action, category: entity.category, key: entity.key }))
            })
            .await
        }

        async fn recall(&self, req: Request<RecallRequest>) -> Result<Response<RecallResponse>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                let params = models::RecallParams {
                    query: r.query,
                    category: r.category,
                    entity_type: r.r#type,
                    limit: r.limit,
                    offset: r.offset,
                    min_decay: r.min_decay,
                    topic_path: r.topic_path,
                    include_archived: r.include_archived,
                    skip_side_effects: true,
                    mode: crate::models::SearchMode::Fts5,
                    embedding: None,
                    preview_cap: r.preview_cap,
                    always_on: r.always_on,
                    content_weight: r.content_weight,
                    trust_weight: 0.0,
                    diversity_halving: r.diversity_halving,
                    diversity_per_query_share: 0.0,
                    recency_half_life_secs: None,
                    workspace_hash: r.workspace_hash,
                    agent_id: r.agent_id,
                    visibility: r.visibility,
                    layer: None,
                    reinforce: false,
                };
                let entities = db.recall(&params)?;
                let items: Vec<EntityMessage> =
                    entities.iter().map(entity_to_proto).collect();
                let total = items.len() as i64;
                Ok(Response::new(RecallResponse { items, total }))
            })
            .await
        }

        async fn get_entity(&self, req: Request<GetEntityRequest>) -> Result<Response<EntityMessage>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                let entity = db.get_entity_by_id_public(&r.id)
                    .map_err(|_| Status::not_found("entity not found"))?
                    .ok_or_else(|| Status::not_found("entity not found"))?;
                Ok(Response::new(entity_to_proto(&entity)))
            })
            .await
        }

        async fn forget(&self, req: Request<ForgetRequest>) -> Result<Response<ForgetResponse>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                db.forget(&r.category, &r.key, &r.reason)?;
                Ok(Response::new(ForgetResponse { ok: true }))
            })
            .await
        }

        // ── Graph ──
        async fn link(&self, _req: Request<LinkRequest>) -> Result<Response<LinkResponse>, Status> {
            Err(Status::unimplemented("link"))
        }
        async fn unlink(&self, _req: Request<UnlinkRequest>) -> Result<Response<UnlinkResponse>, Status> {
            Err(Status::unimplemented("unlink"))
        }
        async fn traverse(&self, _req: Request<TraverseRequest>) -> Result<Response<TraverseResponse>, Status> {
            Err(Status::unimplemented("traverse"))
        }

        // ── Journal ──
        async fn journal(&self, req: Request<JournalRequest>) -> Result<Response<JournalEvent>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                let event = models::JournalEvent {
                    id: format!("jrn-{}", uuid::Uuid::new_v4().to_string().replace('-', "").chars().take(12).collect::<String>()),
                    event_type: r.event_type,
                    evaluated_json: r.evaluated_json,
                    acted_json: r.acted_json,
                    forward_json: r.forward_json,
                    category: r.category,
                    key: r.key,
                    entity_id: r.entity_id,
                    agent_id: r.agent_id,
                    created_at_unix_ms: crate::db::now_ms(),
                };
                db.journal(&event)?;
                Ok(Response::new(journal_event_to_proto(&event)))
            })
            .await
        }

        async fn timeline(&self, _req: Request<TimelineRequest>) -> Result<Response<TimelineResponse>, Status> {
            Err(Status::unimplemented("timeline"))
        }

        // ── State ──
        async fn state_set(&self, req: Request<StateSetRequest>) -> Result<Response<StateSetResponse>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                let now = crate::db::now_ms();
                let entry = models::StateEntry {
                    key: r.key,
                    value_json: r.value_json,
                    // Same TTL convention as the MCP surface (handle_state_set).
                    expires_at_unix_ms: r.ttl_seconds.map(|ttl| now + (ttl as i64) * 1000),
                    created_at_unix_ms: now,
                };
                db.state_set(&entry)?;
                Ok(Response::new(StateSetResponse { ok: true }))
            })
            .await
        }
        async fn state_get(&self, _req: Request<StateGetRequest>) -> Result<Response<StateEntry>, Status> {
            Err(Status::unimplemented("state_get"))
        }
        async fn state_delete(&self, _req: Request<StateDeleteRequest>) -> Result<Response<StateDeleteResponse>, Status> {
            Err(Status::unimplemented("state_delete"))
        }
        async fn state_list(&self, _req: Request<StateListRequest>) -> Result<Response<StateListResponse>, Status> {
            Err(Status::unimplemented("state_list"))
        }

        // ── Ops ──
        async fn health(&self, _req: Request<HealthRequest>) -> Result<Response<HealthResponse>, Status> {
            with_db(self, move |db| {
                Ok(Response::new(HealthResponse { healthy: db.health_check() }))
            })
            .await
        }
        async fn stats(&self, _req: Request<StatsRequest>) -> Result<Response<StatsResponse>, Status> {
            with_db(self, move |db| {
                let s = db.stats()?;
                Ok(Response::new(StatsResponse {
                    total_entities: s.total_entities,
                    total_journal: s.total_journal_events,
                    total_state: s.total_state_entries,
                    db_size_bytes: s.db_file_size_bytes as i64,
                }))
            })
            .await
        }
        async fn context(&self, req: Request<ContextRequest>) -> Result<Response<ContextResponse>, Status> {
            let r = req.into_inner();
            with_db(self, move |db| {
                let ctx = db.context(&r.categories, r.limit, r.workspace_hash.as_deref())?;
                Ok(Response::new(ContextResponse { context: ctx }))
            })
            .await
        }
        async fn workspace_list(&self, _req: Request<WorkspaceListRequest>) -> Result<Response<WorkspaceListResponse>, Status> {
            with_db(self, move |db| {
                let cats = db.workspace_list_categories()?;
                Ok(Response::new(WorkspaceListResponse { categories: cats }))
            })
            .await
        }

        // ── AI ──
        async fn ask(&self, _req: Request<AskRequest>) -> Result<Response<AskResponse>, Status> { Err(Status::unimplemented("ask")) }
        async fn embed(&self, _req: Request<EmbedRequest>) -> Result<Response<EmbedResponse>, Status> { Err(Status::unimplemented("embed")) }
        async fn cohere(&self, _req: Request<CohereRequest>) -> Result<Response<CohereResponse>, Status> { Err(Status::unimplemented("cohere")) }

        // ── Lifecycle ──
        async fn decay(&self, _req: Request<DecayRequest>) -> Result<Response<DecayResponse>, Status> { Err(Status::unimplemented("decay")) }
        async fn prune(&self, _req: Request<PruneRequest>) -> Result<Response<PruneResponse>, Status> { Err(Status::unimplemented("prune")) }
        async fn compact(&self, _req: Request<CompactRequest>) -> Result<Response<CompactResponse>, Status> { Err(Status::unimplemented("compact")) }
        async fn score(&self, _req: Request<ScoreRequest>) -> Result<Response<ScoreResponse>, Status> { Err(Status::unimplemented("score")) }

        // ── Quality ──
        async fn conflicts(&self, _req: Request<ConflictsRequest>) -> Result<Response<ConflictsResponse>, Status> { Err(Status::unimplemented("conflicts")) }

        // ── Vault ──
        async fn vault_export(&self, _req: Request<VaultExportRequest>) -> Result<Response<VaultExportResponse>, Status> { Err(Status::unimplemented("vault_export")) }
        async fn vault_import(&self, _req: Request<VaultImportRequest>) -> Result<Response<VaultImportResponse>, Status> { Err(Status::unimplemented("vault_import")) }

        // ── Federation ──
        async fn federate(&self, _req: Request<FederateRequest>) -> Result<Response<FederateResponse>, Status> { Err(Status::unimplemented("federate")) }
        async fn share(&self, _req: Request<ShareRequest>) -> Result<Response<ShareResponse>, Status> { Err(Status::unimplemented("share")) }

        // ── Streaming ──
        type WatchJournalStream = tokio_stream::wrappers::ReceiverStream<Result<JournalEvent, Status>>;
        async fn watch_journal(&self, _req: Request<WatchJournalRequest>) -> Result<Response<Self::WatchJournalStream>, Status> {
            Err(Status::unimplemented("watch_journal"))
        }
        type StreamContextStream = tokio_stream::wrappers::ReceiverStream<Result<ContextChunk, Status>>;
        async fn stream_context(&self, _req: Request<StreamContextRequest>) -> Result<Response<Self::StreamContextStream>, Status> {
            Err(Status::unimplemented("stream_context"))
        }
    }

    // ── Helpers ──
    fn entity_to_proto(e: &models::Entity) -> EntityMessage {
        EntityMessage {
            id: e.id.clone(), category: e.category.clone(), key: e.key.clone(),
            body_json: e.body_json.clone(), status: e.status.clone(), r#type: e.entity_type.clone(),
            tags: e.tags.clone(), decay_score: e.decay_score, retrieval_count: e.retrieval_count,
            layer: e.layer.clone(), topic_path: e.topic_path.clone(),
            archived: e.archived, archive_reason: e.archive_reason.clone(),
            verified: e.verified, source: e.source.clone(), always_on: e.always_on,
            certainty: e.certainty, workspace_hash: e.workspace_hash.clone(),
            agent_id: e.agent_id.clone(), visibility: e.visibility.clone(),
            created_at_unix_ms: e.created_at_unix_ms, last_accessed_unix_ms: e.last_accessed_unix_ms,
        }
    }

    fn journal_event_to_proto(e: &models::JournalEvent) -> JournalEvent {
        JournalEvent {
            id: e.id.clone(), event_type: e.event_type.clone(),
            evaluated_json: e.evaluated_json.clone(), acted_json: e.acted_json.clone(),
            forward_json: e.forward_json.clone(), category: e.category.clone(),
            key: e.key.clone(), entity_id: e.entity_id.clone(),
            agent_id: e.agent_id.clone(), created_at_unix_ms: e.created_at_unix_ms,
        }
    }

    /// Start the gRPC server on the given address. Runs in the current thread
    /// and blocks until shutdown. For background usage, spawn via std::thread::spawn.
    pub async fn serve(
        db: Arc<Database>,
        addr: std::net::SocketAddr,
    ) -> Result<(), Box<dyn std::error::Error>> {
        use tonic::transport::Server;
        let svc = MnemeGrpcServer::new(db);
        Server::builder()
            .add_service(mneme_server::MnemeServer::new(svc))
            .serve(addr)
            .await?;
        Ok(())
    }

    #[cfg(test)]
    mod tests {
        use super::mneme_server::Mneme;
        use super::*;
        use crate::db::Database;

        fn test_server() -> (MnemeGrpcServer, String) {
            let path = std::env::temp_dir()
                .join(format!("mimir-test-grpc-{}.db", uuid::Uuid::new_v4()));
            let path_str = path.to_str().unwrap().to_string();
            let db = Database::open(&path_str).expect("open test db");
            (MnemeGrpcServer::new(Arc::new(db)), path_str)
        }

        fn remember_req(key: &str) -> RememberRequest {
            RememberRequest {
                category: "note".to_string(),
                key: key.to_string(),
                body_json: "{\"content\":\"hello grpc\"}".to_string(),
                status: "active".to_string(),
                r#type: "insight".to_string(),
                tags: vec!["t1".to_string()],
                importance: 1.0,
                topic_path: String::new(),
                recall_when: vec![],
                always_on: false,
                certainty: 0.5,
                workspace_hash: String::new(),
                agent_id: String::new(),
                visibility: "workspace".to_string(),
            }
        }

        #[test]
        fn sanitize_error_hides_internal_detail_from_clients() {
            // #354: raw internal error text (constraint/column names) must not
            // reach gRPC clients — generic message out, detail logged only.
            let e: Box<dyn std::error::Error> =
                "UNIQUE constraint failed: entities.category, entities.key".into();
            let status = sanitize_error(e);
            assert_eq!(status.code(), tonic::Code::Internal);
            assert_eq!(status.message(), "internal error");
        }

        #[test]
        fn sanitize_error_passes_through_typed_statuses() {
            let e: Box<dyn std::error::Error> = Box::new(Status::not_found("entity not found"));
            let status = sanitize_error(e);
            assert_eq!(status.code(), tonic::Code::NotFound);
            assert_eq!(status.message(), "entity not found");
        }

        #[test]
        fn entity_to_proto_maps_fields() {
            let e = models::Entity {
                id: "ent-1".to_string(),
                category: "note".to_string(),
                key: "k".to_string(),
                body_json: "{}".to_string(),
                status: "active".to_string(),
                entity_type: "insight".to_string(),
                tags: vec!["a".to_string()],
                decay_score: 0.7,
                retrieval_count: 3,
                layer: "working".to_string(),
                topic_path: "x/y".to_string(),
                archived: false,
                archive_reason: String::new(),
                links: vec![],
                verified: true,
                source: "grpc".to_string(),
                always_on: true,
                certainty: 0.9,
                workspace_hash: "ws".to_string(),
                agent_id: "agent".to_string(),
                visibility: "workspace".to_string(),
                created_at_unix_ms: 1,
                last_accessed_unix_ms: 2,
                follow_count: 0,
                miss_count: 0,
                follow_rate: 0.0,
                efficacy_status: "unverified".to_string(),
                embedding: None,
            };
            let p = entity_to_proto(&e);
            assert_eq!(p.id, "ent-1");
            assert_eq!(p.category, "note");
            assert_eq!(p.key, "k");
            assert_eq!(p.r#type, "insight");
            assert_eq!(p.tags, vec!["a".to_string()]);
            assert_eq!(p.decay_score, 0.7);
            assert_eq!(p.retrieval_count, 3);
            assert!(p.verified);
            assert!(p.always_on);
            assert_eq!(p.workspace_hash, "ws");
            assert_eq!(p.created_at_unix_ms, 1);
            assert_eq!(p.last_accessed_unix_ms, 2);
        }

        #[tokio::test]
        async fn remember_then_get_entity_roundtrip() {
            let (server, path) = test_server();
            let resp = server
                .remember(Request::new(remember_req("k1")))
                .await
                .expect("remember");
            let r = resp.into_inner();
            assert!(!r.id.is_empty());
            assert_eq!(r.category, "note");
            assert_eq!(r.key, "k1");

            let got = server
                .get_entity(Request::new(GetEntityRequest { id: r.id.clone() }))
                .await
                .expect("get_entity")
                .into_inner();
            assert_eq!(got.id, r.id);
            assert_eq!(got.category, "note");
            assert_eq!(got.key, "k1");
            let _ = std::fs::remove_file(&path);
        }

        #[tokio::test]
        async fn get_entity_missing_returns_not_found_not_internal() {
            // The typed not_found raised inside the with_db closure must
            // survive sanitize_error instead of being flattened to INTERNAL.
            let (server, path) = test_server();
            let err = server
                .get_entity(Request::new(GetEntityRequest { id: "does-not-exist".to_string() }))
                .await
                .expect_err("missing entity should error");
            assert_eq!(err.code(), tonic::Code::NotFound);
            let _ = std::fs::remove_file(&path);
        }

        /// Deterministic overlap proof (#402): two in-flight `with_db`
        /// closures rendezvous on a 2-party barrier INSIDE their DB closure —
        /// only possible if both run simultaneously. Under the old
        /// `Arc<Mutex<Database>>` (closure executed synchronously while
        /// holding the global lock) this rendezvous would deadlock; the
        /// timeout turns that into a failure.
        #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
        async fn rpc_db_closures_overlap_instead_of_serializing() {
            let (server, path) = test_server();
            let barrier = Arc::new(std::sync::Barrier::new(2));
            let (b1, b2) = (barrier.clone(), barrier.clone());
            let fut = async {
                let a = with_db(&server, move |db| {
                    let healthy = db.health_check();
                    b1.wait();
                    Ok(healthy)
                });
                let b = with_db(&server, move |db| {
                    let healthy = db.health_check();
                    b2.wait();
                    Ok(healthy)
                });
                tokio::join!(a, b)
            };
            let (ra, rb) = tokio::time::timeout(std::time::Duration::from_secs(10), fut)
                .await
                .expect("concurrent RPC DB closures must overlap, not serialize");
            assert!(ra.unwrap());
            assert!(rb.unwrap());
            let _ = std::fs::remove_file(&path);
        }

        #[tokio::test]
        async fn health_and_stats_respond() {
            let (server, path) = test_server();
            let h = server
                .health(Request::new(HealthRequest {}))
                .await
                .expect("health")
                .into_inner();
            assert!(h.healthy);
            let s = server
                .stats(Request::new(StatsRequest {}))
                .await
                .expect("stats")
                .into_inner();
            assert_eq!(s.total_entities, 0);
            let _ = std::fs::remove_file(&path);
        }
    }
}

// Non-grpc fallback
#[cfg(not(feature = "grpc"))]
pub mod grpc {
    use std::sync::Arc;
    use crate::db::Database;

    /// Stub module — gRPC is compiled out.
    // No in-crate caller in the default (non-grpc) build; kept so callers behind
    // `--features grpc` get a clear error instead of a missing symbol.
    // (#402: signature tracks the real serve() — shared Arc<Database>, no Mutex.)
    #[allow(dead_code)]
    pub async fn serve(
        _db: Arc<Database>,
        _addr: std::net::SocketAddr,
    ) -> Result<(), Box<dyn std::error::Error>> {
        Err("gRPC transport not compiled in. Rebuild with: cargo build --features grpc".into())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[tokio::test]
        async fn stub_serve_returns_actionable_error() {
            let path = std::env::temp_dir()
                .join(format!("mimir-test-grpc-stub-{}.db", uuid::Uuid::new_v4()));
            let path_str = path.to_str().unwrap().to_string();
            let db = Database::open(&path_str).expect("open test db");
            let err = serve(
                Arc::new(db),
                "127.0.0.1:0".parse().unwrap(),
            )
            .await
            .expect_err("stub must refuse to serve");
            assert!(err.to_string().contains("--features grpc"));
            let _ = std::fs::remove_file(&path_str);
        }
    }
}
