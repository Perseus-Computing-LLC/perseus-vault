# Perseus Vault — MCP JSON-RPC Response Fixtures

> Generated 2026-07-15. Verified against `src/tools.rs` response shapes.
> These fixtures represent the stable byte-exact output that Noisegate should
> preserve verbatim. They are the JSON-RPC `result` objects returned by the
> MCP server — the exact bytes agents consume.

## perseus_vault_context

Returns a pre-formatted markdown context block for session injection.
Recall-first by default, clamped to a per-model character budget.

```json
{
    "markdown": "## Project Context\n\n### Architecture Decisions\n- **Database layer**: PostgreSQL 16 with row-level security, no ORM. Migrations are numbered and never edited after merge. (decided 2026-06-15, confidence: 0.95)\n- **API framework**: Axum 0.8 + SQLx, structured error types in `src/errors/`. All handlers return `Result<Response, AppError>`. (decided 2026-05-20, confidence: 0.90)\n\n### Recent Fixes\n- **N+1 query in user endpoint**: Added `sqlx::query_as_with` with eager loading. PR #234 merged 2026-07-10.\n- **Race condition in auth middleware**: Token refresh was not atomic. Fixed with `compare_exchange` loop. PR #228 merged 2026-07-08.\n\n### Conventions\n- Error types live in `src/errors/`, one file per domain\n- Migrations are numbered sequentially, never edited after merge\n- All public API endpoints require `Accept: application/json` header\n\n### Current State\n- 3 open bugs, 8 roadmap items tracked in Linear\n- Last release: v0.12.3 (2026-07-14)\n- CI passing on main, staging deploy in progress\n",
    "total_chars": 1124,
    "mode": "on_demand",
    "budget_chars": 4000,
    "entities_injected": 8,
    "warnings": []
}
```

## perseus_vault_recall

Targeted recall with relevance gating. Returns entities matching the query,
ordered by relevance. Supports temporal filtering via `as_of_unix_ms`,
`valid_from_unix_ms`, and `valid_to_unix_ms`.

```json
{
    "items": [
        {
            "id": "a1b2c3d4",
            "category": "insight",
            "key": "db-migration-strategy",
            "body_json": "{\"decision\": \"PostgreSQL 16 with row-level security, migrations numbered sequentially, never edited after merge\", \"rationale\": \"auditability and rollback safety\"}",
            "status": "active",
            "entity_type": "insight",
            "tags": ["database", "architecture", "migrations"],
            "decay_score": 0.92,
            "retrieval_count": 17,
            "layer": "core",
            "always_on": false,
            "certainty": 0.95,
            "created_at_unix_ms": 1718400000000,
            "last_accessed_unix_ms": 1721000000000,
            "workspace_hash": "proj_db_migration"
        },
        {
            "id": "e5f6g7h8",
            "category": "insight",
            "key": "api-framework-choice",
            "body_json": "{\"decision\": \"Axum 0.8 + SQLx, no ORM\", \"rationale\": \"compile-time query checking, zero-cost abstractions\"}",
            "status": "active",
            "entity_type": "insight",
            "tags": ["api", "architecture", "rust"],
            "decay_score": 0.88,
            "retrieval_count": 12,
            "layer": "core",
            "always_on": false,
            "certainty": 0.90,
            "created_at_unix_ms": 1717000000000,
            "last_accessed_unix_ms": 1720500000000,
            "workspace_hash": "proj_api_design"
        }
    ],
    "total": 2
}
```

### Empty recall result

```json
{
    "items": [],
    "total": 0,
    "diagnostic": {
        "reason": "no_match",
        "hint": "the store is populated and the backend is healthy — this query simply had no matches; broaden the query or mode before assuming a fault",
        "active_memories": 1427,
        "embedded_memories": 512,
        "semantic_recall": true
    }
}
```

## perseus_vault_scan

Cursor-based paginated scan across entities. Returns items with a `has_more`
flag and `next_cursor` for pagination.

```json
{
    "items": [
        {
            "id": "a1b2c3d4",
            "category": "insight",
            "key": "db-migration-strategy",
            "body_json": "{...}",
            "status": "active",
            "entity_type": "insight",
            "tags": ["database", "architecture"],
            "decay_score": 0.92,
            "retrieval_count": 17,
            "layer": "core",
            "always_on": false,
            "certainty": 0.95,
            "created_at_unix_ms": 1718400000000,
            "last_accessed_unix_ms": 1721000000000
        },
        {
            "id": "e5f6g7h8",
            "category": "insight",
            "key": "api-framework-choice",
            "body_json": "{...}",
            "status": "active",
            "entity_type": "insight",
            "tags": ["api", "architecture"],
            "decay_score": 0.88,
            "retrieval_count": 12,
            "layer": "core",
            "always_on": false,
            "certainty": 0.90,
            "created_at_unix_ms": 1717000000000,
            "last_accessed_unix_ms": 1720500000000
        }
    ],
    "total": 2,
    "has_more": false,
    "next_cursor": null
}
```

## perseus_vault_get_entity

Single entity fetch by ID. Returns full entity metadata including decay score,
retrieval count, and timestamps.

```json
{
    "id": "a1b2c3d4",
    "category": "insight",
    "key": "db-migration-strategy",
    "body_json": "{\"decision\": \"PostgreSQL 16 with row-level security, migrations numbered sequentially, never edited after merge\", \"rationale\": \"auditability and rollback safety\", \"alternatives_considered\": [\"Prisma ORM\", \"SeaORM\"], \"decided_by\": \"architecture-review-2026-06-15\"}",
    "status": "active",
    "entity_type": "insight",
    "tags": ["database", "architecture", "migrations", "postgresql"],
    "decay_score": 0.92,
    "retrieval_count": 17,
    "layer": "core",
    "always_on": false,
    "certainty": 0.95,
    "created_at_unix_ms": 1718400000000,
    "last_accessed_unix_ms": 1721000000000
}
```

## Error response (all tools)

All tools return error objects on failure. Noisegate should preserve these verbatim — they contain actionable diagnostics.

```json
{
    "error": "Entity not found: a1b2c3d4"
}
```

## JSON-RPC envelope

The MCP server wraps every result in a JSON-RPC 2.0 response envelope.
These are the bytes Noisegate sees on stdio:

```json
{
    "jsonrpc": "2.0",
    "id": 1,
    "result": {
        "markdown": "## Project Context\n...",
        "total_chars": 1124,
        "mode": "on_demand",
        "budget_chars": 4000,
        "entities_injected": 8,
        "warnings": []
    }
}
```

Noisegate should preserve the `result` object byte-exactly. The `jsonrpc`
and `id` fields are transport-level and may vary per request.
