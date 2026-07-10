# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 2.0.x (latest) | ✅ Active |
| 1.x | ✅ Security fixes only |
| 0.x | ❌ Unsupported |

## Reporting a Vulnerability

**Do not open a public issue.** Email security disclosures to:

**perseus@perseus.observer**

You will receive a response within 48 hours. Perseus Computing LLC is a US-owned
small business and treats security reports as confidential until a fix is published.

### What to include

- Affected version(s) and build target (Linux, macOS, Windows)
- Steps to reproduce
- Impact assessment (what an attacker could do)
- Any suggested mitigations

### Disclosure timeline

1. **Acknowledgment** — within 48 hours
2. **Triage** — severity assessment within 5 business days
3. **Fix development** — timeline depends on severity
4. **Coordinated disclosure** — CVE assigned, fix released, advisory published

We support responsible disclosure and will credit reporters who follow this policy.

> Maintainers: the internal process behind these commitments (handler roles,
> severity rubric, embargo and CVE handling) is documented in
> [`docs/vuln-response.md`](docs/vuln-response.md). For the full map of security
> documents, the access-privileges register, and the milestones that gate when
> we escalate security effort, see [`docs/SECURITY-INDEX.md`](docs/SECURITY-INDEX.md)
> and [`docs/SECURITY-MILESTONES.md`](docs/SECURITY-MILESTONES.md).

---

## Security Model

Mimir is a **local-first MCP server** that stores AI agent memory. It processes:

- Entity CRUD (remember, recall, search, forget)
- Journaling (append-only decision logs)
- State management (key-value with TTL)
- Optional embeddings (Ollama / ONNX Runtime)
- Optional connectors (GitHub issues, file watcher)

### Encryption

Mimir supports **opt-in AES-256-GCM encryption at rest** for entity bodies. It
is **off by default**. See the full [Encryption Specification](./docs/ENCRYPTION.md)
and [Threat Model](./docs/THREAT-MODEL.md) for precise guarantees and limits.

| Property | Detail |
|---|---|
| Algorithm | AES-256-GCM (96-bit random nonce per message; 128-bit tag) |
| Key | Raw 256-bit key from a base64 **key file** — **no passphrase / KDF** |
| AAD | `category:key` binds ciphertext to entity identity (anti-swap) |
| Encryption scope | The `entities.body_json` field **only** |
| Encrypted at rest | ⚠️ Body only. **The FTS5 index and all metadata are plaintext** — see caveat below |
| Encrypted in transit | ⚠️ MCP stdio is local-only; secure the optional HTTP/SSE transport with TLS yourself |
| Key management | Operator responsibility — keys never leave the machine; no escrow, no recovery |

**Enable encryption:**
```bash
mimir keygen                                  # writes ~/.mimir/secret.key (0o600 on Unix)
mimir --encryption-key ~/.mimir/secret.key    # start with encryption on
```

> ⚠️ **Body encryption does not make the database file opaque.** For keyword
> search to work, the FTS5 index (`entities_fts`) stores the body in **plaintext**,
> and metadata columns (category, key, tags, workspace, timestamps) are plaintext
> by design. To keep content unreadable from the file itself, **also** enable
> OS-level disk encryption (LUKS / FileVault / BitLocker). On Windows, Mimir does
> not restrict the key file's ACL — do it yourself. Details in
> [docs/ENCRYPTION.md](./docs/ENCRYPTION.md).

### Attack surface

| Vector | Risk | Mitigation |
|---|---|---|
| SQL injection | None | Parameterized queries via rusqlite — no string concatenation |
| Malicious MCP requests | Low | JSON-RPC 2.0 validation; MCP stdio is local-only by default |
| Entity injection (FTS5) | Low | FTS5 uses parameterized queries; inputs are escaped |
| File watcher path traversal | Medium | Paths are canonicalized before watching; only configured directories |
| GitHub connector token exposure | Medium | Token is never logged or stored in the database; memory-only during connector run |
| Embedding model download | Low | Optional; models are downloaded from Ollama or ONNX Runtime's official CDN |
| HTTP transport (axum) | Medium | CORS configured; no authentication by default (local-only intended use) |

### Trust boundaries

- **Mimir runs on your machine.** It does not phone home. No telemetry.
- **MCP transport is local stdio by default.** No network exposure unless you enable HTTP transport.
- **Connectors are opt-in.** GitHub and file watcher connectors are disabled by default.
- **Encryption keys are your responsibility.** Mimir does not store, transmit, or escrow keys.

---

## Compliance

| Standard | Status |
|---|---|
| NIST SP 800-53 | Mapping in progress |
| NIST AI RMF | Alignment documented |
| EO 14028 (SBOM) | [SBOM published](./docs/SBOM.md) |
| CMMC Level 2 | In progress — encryption, access control, audit trail |
| ITAR | US-owned LLC; all development in US; no foreign nationals on codebase |

---

## Dependency Security

- **17 runtime dependencies** — all MIT or Apache-2.0 licensed
- **Zero copyleft (GPL/AGPL)** — safe for government deployment
- **SQLite bundled** via rusqlite — no system library dependency
- **SBOM published** at [docs/SBOM.md](./docs/SBOM.md)
- We monitor [RustSec Advisory Database](https://rustsec.org) for crate CVEs
- `cargo audit` run in CI on every push

---

## Contact

Security: **perseus@perseus.observer**
PGP key: Available on request
