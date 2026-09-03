# Perseus Vault Threat Model

This document states what Perseus Vault defends against, what it does **not**, and the
residual risks an operator owns. It is deliberately honest about limits — a
threat model that only lists strengths is marketing, not security.

For the precise cryptographic spec, see [ENCRYPTION.md](./ENCRYPTION.md). For
the reporting process and version support, see [../SECURITY.md](../SECURITY.md).

*Scope: Perseus Vault the local MCP memory engine (the `perseus_vault` binary) at v2.2.1.
Out of scope: the calling AI agent/host, the operating system, and any
downstream system (e.g. Perseus) that consumes Perseus Vault's output.*

---

## 1. What Perseus Vault is, in security terms

Perseus Vault is a **single local binary** with an **embedded SQLite database**. It
exposes an MCP (JSON-RPC 2.0) interface, by default over **stdio** (no network
socket). It stores AI-agent memory: entities (content + metadata), an
append-only journal, key/value state, an FTS5 keyword index, and optional dense
embeddings. It does not phone home and emits no telemetry.

The security posture follows from that shape: **the primary trust boundary is
the local machine and its filesystem.** Perseus Vault is designed for single-operator,
local-first deployment, not as a multi-tenant network service.

---

## 2. Assets

| Asset | Sensitivity | Where it lives |
|---|---|---|
| Memory content (`body_json`) | High — may contain secrets, PII, proprietary context | `entities.body_json` (encryptable) + `entities_fts` (plaintext index) |
| Memory metadata (category, key, tags, workspace, agent id) | Medium — reveals structure, topics, tenancy | `entities.*` (plaintext) |
| Embedding vectors | Medium — semantically reconstructable | embedding storage (plaintext) |
| Journal (decision log) | Medium | journal table (plaintext) |
| Encryption key | Critical | key file on disk (operator-managed) |
| Connector credentials (e.g. GitHub token) | High | memory only during a connector run; never persisted to the DB |

---

## 3. Trust boundaries

```
   ┌─────────────────────────── local machine (trusted) ───────────────────────────┐
   │                                                                                 │
   │   AI agent / MCP host  ──stdio JSON-RPC──▶  perseus_vault binary  ──▶  SQLite file      │
   │        (trusted)             (B1)            (trusted)    (B2)   (on disk)       │
   │                                                                                 │
   │                          perseus_vault  ──(opt-in)──▶  connectors (GitHub, file watcher)│
   │                                       (B3)                                      │
   └─────────────────────────────────────────────────────────────────────────────────┘
                                          │ (opt-in, off by default)
                                   (B4) HTTP/SSE transport ──▶ network clients
```

- **B1 — MCP caller → Perseus Vault.** Whoever can speak to the stdio pipe is fully
  trusted; Perseus Vault does not authenticate MCP callers. On a single-user machine the
  OS process boundary is the control.
- **B2 — Perseus Vault → SQLite file.** The database file is a plaintext SQLite file
  unless you enable body encryption *and* OS disk encryption (see §5).
- **B3 — Perseus Vault → connectors.** Opt-in egress to GitHub / the filesystem.
- **B4 — Network transport.** Only exists if you explicitly enable HTTP/SSE.
  This is the one boundary that crosses the machine.

---

## 4. Attacker profiles

| # | Attacker | Capability assumed | In scope? |
|---|---|---|---|
| A1 | **Local unprivileged user / co-tenant** | Read other users' files if perms allow | Yes |
| A2 | **Disk / backup thief** | Offline read of the DB file and key file | Yes |
| A3 | **Malicious/compromised MCP caller** | Sends arbitrary MCP requests over stdio | Partial — caller is trusted by design; we still validate input |
| A4 | **Network attacker** | Reaches an enabled HTTP/SSE port | Only if B4 enabled |
| A5 | **Supply-chain attacker** | Malicious dependency / model file | Partial |
| A6 | **Privileged local attacker (root/admin)** | Full machine control, process memory | **No** — out of scope; can read the key from memory |

---

## 5. Threats and mitigations (STRIDE)

### Information disclosure (the central concern)

| Threat | Mitigation | Residual risk |
|---|---|---|
| Disk/backup theft reads memory **content** (A2) | AES-256-GCM on live/history `body_json` and `hints`, enabled by default for fresh installs; protected FTS5 indexes use keyed blind tokens | Metadata, embeddings, journal/state payloads, and deterministic blind-token relationships remain observable. Body encryption alone does **not** make the file opaque — layer OS disk encryption. |
| Disk/backup theft reads **metadata** (A2) | — | Not mitigated by app-layer encryption: category/key/tags/workspace/timestamps are plaintext by design (needed for indexing/routing). Use full-disk encryption. |
| Co-tenant reads the DB or key file (A1) | Unix: `keygen` sets key file `0o600` | **Windows: key file gets default ACLs — not tightened by Perseus Vault.** Operator must restrict the DB file and key file ACLs. |
| Key recovered from process memory (A6) | — | Out of scope; a static key is held in process for the session. No `zeroize` of key material today. |
| Embedding inversion leaks content | — | Vectors are plaintext and semantically reconstructable; protect the file. |

### Tampering

| Threat | Mitigation | Residual risk |
|---|---|---|
| Swap/replace an encrypted body between entities | **Length-prefixed category + key AAD** binds ciphertext to identity; GCM tag verified on read | Low. Effective only when encryption is enabled. |
| Corrupt/forge a body without the key | GCM authentication tag | Low when encrypted. **Plaintext DBs have no app-layer integrity** — rely on filesystem/OS. |
| Direct SQLite writes by a local attacker | — | A local writer can alter plaintext columns, blind-token FTS rows, and metadata. Out of app scope; an OS/filesystem control. |

### Spoofing / Elevation of privilege

| Threat | Mitigation | Residual risk |
|---|---|---|
| Unauthenticated MCP caller acts as the user (A3) | stdio is local-only; OS process boundary | By design Perseus Vault trusts its MCP caller. Do not expose the stdio server to untrusted local processes. |
| Unauthenticated HTTP caller (A4) | HTTP/SSE is **off by default** | **No built-in auth on the HTTP transport.** If you enable it, put auth + TLS in front (reverse proxy) and bind to localhost. |
| Cross-workspace/agent memory leakage | `workspace_hash` / `agent_id` / `visibility` scoping on entities | Scoping is a **routing/relevance** control, not an enforced security boundary against a trusted local caller. Don't treat it as multi-tenant isolation. |

### Injection (a sub-class worth calling out)

| Threat | Mitigation | Residual risk |
|---|---|---|
| SQL injection | All queries parameterized via `rusqlite` (no string concatenation of inputs) | Low |
| FTS5 query injection | FTS5 `MATCH` uses bound parameters | Low |
| File-watcher path traversal (B3) | Paths canonicalized; only configured directories watched | Medium — operator must scope watched directories |
| Connector token exposure (B3) | Tokens kept in memory during a run; never written to the DB or logs | Medium — depends on host environment hygiene |

### Repudiation

| Threat | Mitigation | Residual risk |
|---|---|---|
| Denying a memory change | Append-only journal | Journal is plaintext and locally mutable by a privileged local attacker; it is an operational audit aid, not tamper-proof. |

### Denial of service

| Threat | Mitigation | Residual risk |
|---|---|---|
| Pathologically large body inflating the FTS prefilter | Term-count cap on the FTS dedup prefilter (#228) | Low |
| Resource exhaustion from a trusted caller | — | Caller is trusted; rate-limit at the host if needed. |

### Supply chain

| Threat | Mitigation | Residual risk |
|---|---|---|
| Malicious crate (A5) | MIT/Apache-only deps; `cargo audit` in CI; [SBOM](./SBOM.md) | Standard ecosystem risk |
| Malicious embedding model | Bundled model is fetched at build time from a pinned source; air-gapped builds honor `PERSEUS_VAULT_BUNDLED_MODEL_DIR` | Verify model provenance for offline/regulated builds |

---

## 6. Security assumptions (must hold for the model above)

1. The **operating system and the local user account are trusted.** Perseus Vault does
   not defend against a privileged local attacker (A6).
2. The **MCP caller is trusted.** stdio is not authenticated; do not expose it
   to untrusted local processes.
3. The **HTTP/SSE transport stays disabled** unless you add auth + TLS in front.
4. The **key file is protected by the operator** (especially on Windows, where
   Perseus Vault does not set ACLs), and the key is backed up — there is no recovery.
5. For "the database file reveals nothing," **OS-level disk encryption is in
   use**, because metadata, embeddings, journal/state payloads, and blind-index
   relationships are not covered by the body encryption profile.

---

## 7. Hardening checklist (operator)

- [ ] Confirm encryption is active on fresh installs: `perseus-vault doctor --db <path>` reports `[ENCRYPTED]` (default posture; back up `~/.perseus-vault/secret.key`).
- [ ] Enable OS full-disk/filesystem encryption (LUKS / FileVault / BitLocker) — this is what protects metadata, embeddings, journal/state payloads, and blind-index relationships.
- [ ] Restrict the DB file and key file permissions/ACLs (mandatory on Windows).
- [ ] Keep the HTTP/SSE transport off, or front it with auth + TLS bound to localhost.
- [ ] Leave connectors off unless needed; scope file-watcher directories tightly.
- [ ] Back up the encryption key separately from the database; losing it is unrecoverable.

---

*Verified against `src/encryption.rs`, `src/db.rs`, `src/main.rs`, and
`src/transport.rs` at v2.2.1. Keep this document in sync with the code in the
same PR that changes behavior.*
