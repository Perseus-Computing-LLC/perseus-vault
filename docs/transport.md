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
