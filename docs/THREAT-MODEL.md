# Mimir Threat Model

This document states what Mimir defends against, what it does **not**, and the
residual risks an operator owns. It is deliberately honest about limits — a
threat model that only lists strengths is marketing, not security.

For the precise cryptographic spec, see [ENCRYPTION.md](./ENCRYPTION.md). For
the reporting process and version support, see [../SECURITY.md](../SECURITY.md).

*Scope: Mimir the local MCP memory engine (the `mimir` binary) at v2.2.1.
Out of scope: the calling AI agent/host, the operating system, and any
downstream system (e.g. Perseus) that consumes Mimir's output.*

---

## 1. What Mimir is, in security terms

Mimir is a **single local binary** with an **embedded SQLite database**. It
exposes an MCP (JSON-RPC 2.0) interface, by default over **stdio** (no network
socket). It stores AI-agent memory: entities (content + metadata), an
append-only journal, key/value state, an FTS5 keyword index, and optional dense
embeddings. It does not phone home and emits no telemetry.

The security posture follows from that shape: **the primary trust boundary is
the local machine and its filesystem.** Mimir is designed for single-operator,
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
   │   AI agent / MCP host  ──stdio JSON-RPC──▶  mimir binary  ──▶  SQLite file      │
   │        (trusted)             (B1)            (trusted)    (B2)   (on disk)       │
   │                                                                                 │
   │                          mimir  ──(opt-in)──▶  connectors (GitHub, file watcher)│
   │                                       (B3)                                      │
   └─────────────────────────────────────────────────────────────────────────────────┘
                                          │ (opt-in, off by default)
                                   (B4) HTTP/SSE transport ──▶ network clients
```

- **B1 — MCP caller → Mimir.** Whoever can speak to the stdio pipe is fully
  trusted; Mimir does not authenticate MCP callers. On a single-user machine the
  OS process boundary is the control.
- **B2 — Mimir → SQLite file.** The database file is a plaintext SQLite file
  unless you enable body encryption *and* OS disk encryption (see §5).
- **B3 — Mimir → connectors.** Opt-in egress to GitHub / the filesystem.
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
| Disk/backup theft reads memory **content** (A2) | Opt-in AES-256-GCM on `body_json` | **The FTS5 index stores plaintext** (see [ENCRYPTION.md §3](./ENCRYPTION.md)); metadata is plaintext. Body encryption alone does **not** make the file opaque — layer OS disk encryption. |
| Disk/backup theft reads **metadata** (A2) | — | Not mitigated by app-layer encryption: category/key/tags/workspace/timestamps are plaintext by design (needed for indexing/routing). Use full-disk encryption. |
| Co-tenant reads the DB or key file (A1) | Unix: `keygen` sets key file `0o600` | **Windows: key file gets default ACLs — not tightened by Mimir.** Operator must restrict the DB file and key file ACLs. |
| Key recovered from process memory (A6) | — | Out of scope; a static key is held in process for the session. No `zeroize` of key material today. |
| Embedding inversion leaks content | — | Vectors are plaintext and semantically reconstructable; protect the file. |

### Tampering

| Threat | Mitigation | Residual risk |
|---|---|---|
| Swap/replace an encrypted body between entities | **AAD = `category:key`** binds ciphertext to identity; GCM tag verified on read | Low. Effective only when encryption is enabled. |
| Corrupt/forge a body without the key | GCM authentication tag | Low when encrypted. **Plaintext DBs have no app-layer integrity** — rely on filesystem/OS. |
| Direct SQLite writes by a local attacker | — | A local writer can alter plaintext columns, the FTS index, and metadata. Out of app scope; an OS/filesystem control. |

### Spoofing / Elevation of privilege

| Threat | Mitigation | Residual risk |
|---|---|---|
| Unauthenticated MCP caller acts as the user (A3) | stdio is local-only; OS process boundary | By design Mimir trusts its MCP caller. Do not expose the stdio server to untrusted local processes. |
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
| Malicious embedding model | Bundled model is fetched at build time from a pinned source; air-gapped builds honor `MIMIR_BUNDLED_MODEL_DIR` | Verify model provenance for offline/regulated builds |

---

## 6. Security assumptions (must hold for the model above)

1. The **operating system and the local user account are trusted.** Mimir does
   not defend against a privileged local attacker (A6).
2. The **MCP caller is trusted.** stdio is not authenticated; do not expose it
   to untrusted local processes.
3. The **HTTP/SSE transport stays disabled** unless you add auth + TLS in front.
4. The **key file is protected by the operator** (especially on Windows, where
   Mimir does not set ACLs), and the key is backed up — there is no recovery.
5. For "the database file reveals nothing," **OS-level disk encryption is in
   use**, because metadata and the FTS plaintext index are not covered by
   `body_json` encryption.

---

## 7. Hardening checklist (operator)

- [ ] Enable body encryption: `mimir keygen` then `mimir --encryption-key ~/.mimir/secret.key`.
- [ ] Enable OS full-disk/filesystem encryption (LUKS / FileVault / BitLocker) — this is what protects metadata and the FTS index.
- [ ] Restrict the DB file and key file permissions/ACLs (mandatory on Windows).
- [ ] Keep the HTTP/SSE transport off, or front it with auth + TLS bound to localhost.
- [ ] Leave connectors off unless needed; scope file-watcher directories tightly.
- [ ] Back up the encryption key separately from the database; losing it is unrecoverable.

---

*Verified against `src/encryption.rs`, `src/db.rs`, `src/main.rs`, and
`src/transport.rs` at v2.2.1. Keep this document in sync with the code in the
same PR that changes behavior.*
