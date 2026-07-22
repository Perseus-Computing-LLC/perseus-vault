# Remote Transport: SSE & Streamable HTTP

Perseus Vault is an MCP **stdio** server by default. For remote access — Claude Desktop,
the Anthropic MCP Connector API, or any HTTP MCP client — it also ships a full
**SSE** and **Streamable HTTP** transport, with optional Bearer-token auth.

## Transport modes

| Flag | Endpoints | Use case |
|---|---|---|
| *(default)* | stdio | Local clients (Hermes, Claude Code, Cursor) |
| `--transport sse` | `GET /sse` + `POST /message` | Claude Desktop, MCP Connector API |
| `--transport http` | `POST /message` only | Stateless Streamable HTTP |

## stdio process lifecycle & orphan detection

Each MCP client spawns **one** `perseus-vault` stdio process per connection and
runs all its tool calls over that persistent pipe — it does **not** spawn a
process per tool call. A well-behaved client closes stdin when the session ends,
the server sees EOF, and it exits, freeing its DB handle.

**The server exits when its host dies — never merely because the host went
quiet.** Abandonment is detected by parent death, not inactivity (#748):

- **Linux:** the kernel delivers SIGTERM the instant the parent dies
  (`PR_SET_PDEATHSIG`), backed by a reparent-to-init poll.
- **macOS / other Unix:** a background watcher polls `getppid()` every 5s and
  exits promptly when the process is reparented to PID 1 (launchd) — i.e. the
  spawning host is gone. This works with zero traffic, so an orphaned server
  blocked in `recv()` still notices.
- A quiet-but-alive host (e.g. Claude Desktop, which can go many minutes
  between tool calls and does **not** respawn a server that exits) is left
  running indefinitely. Idleness is not abandonment.

`MIMIR_IDLE_TIMEOUT_SECS=<seconds>` remains as an **opt-in** flat idle watchdog
(default: **off** since v2.21.0; previously 600s) for the one topology
parent-death detection cannot see: a host that leaks the child's stdin
write-end while *staying alive* (the Hermes-worker reconnect leak,
NousResearch/hermes-agent#57228). Hosts with that lifecycle should set the env
var when spawning the server. Unparseable values are ignored with a warning.

> ⚠️ **Do not run an external process-count reaper** (e.g. a cron that kills the
> oldest `perseus-vault` processes when more than N exist). These subprocesses
> are the normal stdio transport for **live** tool calls, so a count-based reap
> races with active operations and kills them mid-call — clients then see
> `Unknown tool` errors and silent dispatch failures ([#450]). The built-in
> parent-death watcher already reclaims true orphans. If you must add external
> cleanup,
> key it on **age + orphaned parent** (PPID reparented to init), never on raw
> process count.

[#450]: https://github.com/Perseus-Computing-LLC/perseus-vault/issues/450

## Quick start

```bash
perseus-vault --db /path/to/perseus-vault.db \
  --transport sse \
  --web-bind 0.0.0.0 \
  --port 8765 \
  --mcp-token "$(openssl rand -hex 32)"
```

Output:

```
perseus-vault: MCP over sse transport on http://0.0.0.0:8765
perseus-vault: POST http://0.0.0.0:8765/message
perseus-vault: GET  http://0.0.0.0:8765/sse
```

> ⚠️ Binding to a non-loopback address (`--web-bind 0.0.0.0`) **without** an auth
> token is refused by default — the server exits with an error rather than come
> up wide open. Set `--mcp-token` (as above), keep the default `127.0.0.1` bind,
> or, only if the network is genuinely trusted (e.g. an auth-terminating reverse
> proxy), set `MIMIR_ALLOW_INSECURE_BIND=1` to override.

## Authentication (`--mcp-token`)

When `--mcp-token` is set, **every** transport route requires a matching
`Authorization: Bearer <token>` header. Requests without it — or with the wrong
token — get `401 Unauthorized` with a `WWW-Authenticate: Bearer` header. Has no
effect on stdio transport.

```bash
perseus-vault --db /path/to/perseus-vault.db \
  --transport http \
  --web-bind 0.0.0.0 \
  --port 8765 \
  --mcp-token "$(openssl rand -hex 32)"
```

Client request:

```bash
curl -s -X POST http://localhost:8765/message \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

| Auth state | Response |
|---|---|
| No `Authorization` header | `401 Unauthorized` |
| Wrong token | `401 Unauthorized` |
| Correct `Bearer <token>` | `200 OK` (request processed) |

When `--mcp-token` is **not** set, auth is skipped entirely (backward
compatible) — appropriate only for loopback (`127.0.0.1`) deployments. Token
comparison is constant-time, so a wrong token leaks no timing signal about the
secret.

## Hardening

The HTTP surfaces (MCP transport + web dashboard) apply these protections by
default; all are env-tunable:

| Control | Default | Env knob |
|---|---|---|
| Secure-bind guard | refuse non-loopback bind with no token | `MIMIR_ALLOW_INSECURE_BIND=1` to override |
| Request-body cap | 8 MiB | `MIMIR_MAX_HTTP_BODY_BYTES` |
| Rate limit (global token bucket) | 50 req/s, burst 100 → `429` | `MIMIR_HTTP_RATE_PER_SEC` (0 disables), `MIMIR_HTTP_RATE_BURST` |
| CORS | tightened methods/headers; origin mirrors request | `MIMIR_CORS_ALLOWED_ORIGINS` (comma-separated allowlist) |

The rate limit is **global**, not per-client — the vault is a single-tenant,
local-first service, so per-IP fairness is a fronting reverse proxy's job. TLS is
likewise expected to be terminated by a proxy for the HTTP transport; the gRPC
server can terminate TLS/mTLS itself (see [GRPC-SECURITY.md](./GRPC-SECURITY.md)).

## Verify the endpoint

```python
import json, urllib.request

TOKEN = "YOUR_TOKEN"  # omit the header entirely if running without --mcp-token

def jsonrpc(method, params=None, id=1):
    body = {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    req = urllib.request.Request(
        "http://localhost:8765/message",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {TOKEN}",
        },
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())

init = jsonrpc("initialize", {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1.0"},
})
print("Server:", init["result"]["serverInfo"])

tools = jsonrpc("tools/list", id=2)
print("Tools:", len(tools["result"]["tools"]))
```

## Connecting from the Anthropic MCP Connector API

The SSE endpoint must be publicly reachable (e.g. behind a Cloudflare tunnel).
Pass the Bearer token via the connector's `authorization_token` field:

```python
client.beta.messages.create(
    model="claude-opus-4-8",
    mcp_servers=[{
        "type": "url",
        "url": "https://perseus-vault-mcp.example.com/sse",
        "name": "perseus-vault",
        "authorization_token": "YOUR_TOKEN",  # matches --mcp-token
    }],
    tools=[{"type": "mcp_toolset", "mcp_server_name": "perseus-vault"}],
    betas=["mcp-client-2025-11-20"],
)
```

## Docker

```bash
docker run -p 8765:8765 \
  -v ~/.mimir/data:/data \
  ghcr.io/perseus-computing-llc/perseus-vault:latest \
  --db /data/perseus-vault.db \
  --transport sse \
  --web-bind 0.0.0.0 \
  --port 8765 \
  --mcp-token YOUR_TOKEN
```
