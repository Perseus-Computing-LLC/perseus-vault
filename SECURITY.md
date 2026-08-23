# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 2.23.x (latest) | ✅ Active |
| 2.22.x | ✅ Security fixes only |
| < 2.22.0 | ❌ Unsupported |

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

Perseus Vault is a **local-first MCP server** that stores AI agent memory. It processes:

- Entity CRUD (remember, recall, search, forget)
- Journaling (append-only decision logs)
- State management (key-value with TTL)
- Optional embeddings (Ollama / ONNX Runtime)
- Optional connectors (GitHub issues, file watcher)

### Encryption

Perseus Vault supports **AES-256-GCM encryption at rest** for entity bodies. It
is **enabled by default for fresh installations**: the first write to a new
database generates an owner-only standard key (`~/.perseus-vault/secret.key`)
and establishes the encrypted canary. Existing plaintext databases fail closed
with an actionable `init --rekey` migration path (or an explicit
`PERSEUS_VAULT_ALLOW_PLAINTEXT=1` opt-out). See the full
[Encryption Specification](./docs/ENCRYPTION.md)
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

**Encryption is on by default for fresh installs** — the standard key is
auto-generated at `~/.perseus-vault/secret.key` (0o600 on Unix) on first write.
Explicit key management:

```bash
perseus-vault keygen                              # optional; fresh installs auto-create the standard key
perseus-vault --encryption-key ~/.perseus-vault/secret.key   # explicit key path
```

Existing plaintext databases fail closed with an `init --rekey` migration path
(or explicit `PERSEUS_VAULT_ALLOW_PLAINTEXT=1`); `doctor` reports the actual
on-disk state.

> ⚠️ **Body encryption does not make the database file opaque.** For keyword
> search to work, the FTS5 index (`entities_fts`) stores the body in **plaintext**,
> and metadata columns (category, key, tags, workspace, timestamps) are plaintext
> by design. To keep content unreadable from the file itself, **also** enable
> OS-level disk encryption (LUKS / FileVault / BitLocker). On Windows, Perseus Vault does
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

- **Perseus Vault runs on your machine.** It does not phone home. No telemetry.
- **MCP transport is local stdio by default.** No network exposure unless you enable HTTP transport.
- **Connectors are opt-in.** GitHub and file watcher connectors are disabled by default.
- **Encryption keys are your responsibility.** Perseus Vault does not store, transmit, or escrow keys.

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

### RustSec triage

`cargo audit --deny warnings` runs on every push, every PR, and weekly against
the live advisory database (`.github/workflows/audit.yml`). As of 2026-08-16 the
scan reports **0 vulnerabilities** across 438 locked dependencies. Two
*unmaintained* advisories (informational severity — not exploitable
vulnerabilities) are explicitly accepted, with justification, in that workflow:

| Advisory | Crate | Why it is accepted |
|---|---|---|
| RUSTSEC-2024-0436 | `paste` 1.0.15 | Unmaintained, but a proc-macro: it executes only at compile time and adds no runtime attack surface. Reachable only through `tokenizers`, whose newest release still pins it — no patched version exists. |
| RUSTSEC-2026-0192 | `ttf-parser` 0.25.1 | Unmaintained. Compiled only in the opt-in `multimodal` build (local PDF text extraction via `pdf-extract`/`lopdf`); the default binary does not contain it. The newest `lopdf` release still pins it — no patched version exists. |

Both chains were re-verified against crates.io on 2026-08-16: every crate in
each chain is at its newest release and the newest releases still carry these
crates, so there is currently no upgrade path that removes them. Any *new*
advisory fails the CI gate immediately; if an upstream chain ever drops one of
these crates, the corresponding ignore in `.github/workflows/audit.yml` is
removed.

---

## Verifying releases

Release binaries carry **signed SLSA build provenance** (Sigstore-signed, via
GitHub Artifact Attestations). After downloading a release archive you can
verify it was built by our release workflow from this repository:

```bash
gh attestation verify perseus-vault-lite-x86_64-unknown-linux-musl.tar.gz \
  --repo Perseus-Computing-LLC/perseus-vault
```

A successful verification confirms the artifact's provenance (repo, workflow,
commit) and that it has not been tampered with since it was built.

---

## Contact

Security: **perseus@perseus.observer**

**PGP** — encrypt sensitive reports to our security key:

```
Fingerprint: 92C8 E815 1A60 DB38 46DB  420B 029A 35A6 A22B 287E
```

Fetch it from [keys.openpgp.org](https://keys.openpgp.org/search?q=perseus@perseus.observer)
(`gpg --keyserver hkps://keys.openpgp.org --recv-keys 92C8E8151A60DB3846DB420B029A35A6A22B287E`)
and verify the fingerprint above before use.
