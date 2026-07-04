# gRPC Security Model

The gRPC surface (`grpc` feature, `src/grpc.rs`) maps the MCP tools to protobuf
RPCs on the `mneme.v1` service. It is **off by default** and, like the HTTP
transport, is a remote surface reachable by attacker profile **A4** the moment
it binds beyond loopback. This document is the security design; it is
implemented in `serve_with` and enforced whether or not individual RPC handlers
are filled in yet (many are still `unimplemented!`).

## Threat model

Same as [THREAT-MODEL.md](./THREAT-MODEL.md): the local machine is the trust
boundary and stdio is the trusted path. A network-exposed gRPC endpoint moves
the boundary to the network, so it must provide **transport confidentiality**,
**authentication**, and **resource bounds** before it carries real traffic.

## Controls (implemented)

All configured via environment; defaults are "off" so behavior is unchanged
until an operator opts in — with one exception: the **secure-bind guard** is
always on.

| Control | Mechanism | Env |
|---|---|---|
| **Secure-bind guard** | Refuses to serve on a non-loopback address without either an auth token or mTLS. Mirrors the HTTP `guard_bind` policy. | `MIMIR_ALLOW_INSECURE_BIND=1` to override (trusted network only) |
| **Authentication** | `AuthInterceptor` requires `authorization: Bearer <token>` metadata on every RPC; constant-time compared; rejects with `UNAUTHENTICATED`. | `MIMIR_GRPC_AUTH_TOKEN` |
| **TLS** | `ServerTlsConfig` with a PEM server identity. | `MIMIR_GRPC_TLS_CERT`, `MIMIR_GRPC_TLS_KEY` |
| **Mutual TLS** | Adds a client-CA root; clients must present a cert chaining to it. Counts as authentication for the bind guard. | `MIMIR_GRPC_TLS_CLIENT_CA` |
| **Message-size cap** | `max_decoding_message_size` / `max_encoding_message_size` bound per-message memory. | `MIMIR_GRPC_MAX_MSG_BYTES` (default 4 MiB) |
| **Error hygiene** | Internal error text (SQLite constraints, paths) is logged server-side and genericized to `INTERNAL` before reaching clients (`sanitize_error`). | — |

### Example: authenticated + mTLS

```bash
MIMIR_GRPC_AUTH_TOKEN="$(openssl rand -hex 32)" \
MIMIR_GRPC_TLS_CERT=/etc/perseus/server.crt \
MIMIR_GRPC_TLS_KEY=/etc/perseus/server.key \
MIMIR_GRPC_TLS_CLIENT_CA=/etc/perseus/client-ca.crt \
perseus-vault --db /path/to/perseus-vault.db  # (once a --grpc flag is wired)
```

## Deferred / future work

- **Per-method authorization (read vs. write).** Today auth is all-or-nothing.
  A future step is a scope/role model so a token can be granted read-only
  (`recall`, `get_entity`, `stats`) without write (`remember`, `forget`,
  `purge`). The interceptor is the natural enforcement point.
- **Per-client rate limiting.** The HTTP surface has a global token bucket; gRPC
  currently relies on the message-size cap plus a fronting proxy. A tonic layer
  (e.g. `tower` load-shed/concurrency-limit) can be added when the RPCs are
  implemented and traffic patterns are known.
- **Wiring `serve` into the CLI.** `serve`/`serve_with` exist and are secured,
  but there is no `--grpc` flag yet; the endpoint is not startable from the CLI
  until the handlers are implemented. When that lands, add the flag and route it
  through the same `guard_bind`-style checks.

## Rationale for "secure by default, opt-in features"

TLS and auth default to off because the common case is a loopback developer
setup where they add friction with no benefit. The **bind guard** is the
backstop: it makes the one dangerous combination — exposed, unauthenticated,
plaintext — fail loudly instead of silently, so an operator can't accidentally
publish an open endpoint.
