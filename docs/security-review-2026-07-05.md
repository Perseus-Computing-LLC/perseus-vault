# Perseus Vault (memory engine) — Security Review — 2026-07-05

Independent pre-launch audit of the Rust memory engine (v2.17.1), ahead of the
integrated Perseus + Perseus Vault launch. Five parallel review passes fed findings
that were then **re-traced to source and re-verified by hand**, and — critically —
**calibrated against the product's own `docs/THREAT-MODEL.md`**. Several agent-reported
"criticals" are, per the documented threat model, the *intended design*; those are
recorded below under "By design" rather than inflated into vulnerabilities.

Threat model (from `docs/THREAT-MODEL.md`, verbatim): *"the primary trust boundary is
… local-first deployment, not a multi-tenant network service"*; *"Mimir trusts its MCP
caller"*; and — explicitly — *"Cross-workspace/agent … scoping is a **routing/relevance**
control, not an enforced security boundary … Don't treat it as multi-tenant isolation."*
The audit respects that boundary.

## TL;DR
The engine is **well-hardened for its stated model**: no SQL injection (fully
parameterized), no FTS5 operator injection (every term quoted + bound), **no shell/
raw-SQL tool exists at all** (the `@query`/`allow_query_shell` gate is a Python-Mimir
concept absent from this binary), no `unsafe`, request-path panics confined to tests,
strong resource caps (ingest/zip-bomb/scan/traverse), and a genuinely secure-by-default
transport posture (`stdio`, `127.0.0.1`, dashboard off, `guard_bind` **fail-closed** on
any non-loopback bind without a token, constant-time compares, SSE GET authenticated,
gRPC dormant). The AES-256-GCM core is correct (unique random nonce per op, CSPRNG
keygen, `0600` key file, fail-fast canary, no ciphertext-leak decrypt fallbacks).

The material issues are: (1) the **"audit chain" is not tamper-evident** — a
non-cryptographic 64-bit `DefaultHasher`, unkeyed, that **does not hash the entry
payload** and is **never verified anywhere** (`verify_audit_chain` is dead code) — which
undercuts the SECURITY.md/CMMC "audit trail" framing; (2) a **documented `--workspace-token`
auth flag that no code reads**; and (3) **supply-chain posture** — `install.sh` checksum
verification fails *open*, the Docker image runs as root, and SECURITY.md claims a
`cargo audit` CI gate that **does not exist**.

## Findings (verified, ranked)

| # | Sev | Area | What | File |
|---|-----|------|------|------|
| 1 | MED | Audit integrity | The "SHA-256 audit chain" is `std::hash::DefaultHasher` (SipHash-1-3, 64-bit, **unkeyed**); it hashes only `(prev, event_id, created_at, workspace_hash)` — **not the event payload** (`evaluated_json`/`forward_json`), so content edits are invisible; and `verify_audit_chain` is `#[allow(dead_code)]` with **no caller** — nothing ever checks it. Contradicts the "append-only decision log" / CMMC "audit trail" claim | `db.rs:8462-8481,8550`; comment admits "not cryptographic … upgrade" |
| 2 | MED | Migration integrity | v11→v12 `rehash_audit_chain` recomputes every hash from current row data **without verifying the prior chain first** → a tampered pre-v12 journal is laundered into a "valid" v12 chain (moot today since #1 makes the chain non-evidentiary, but blocks any future keyed upgrade) | `db.rs:8490-8520`; `schema.rs:463` |
| 3 | MED | Auth (dead control) | `--workspace-token` is defined on the CLI + `Serve` subcommand and **documented as workspace auth**, but grep shows zero reads — the `Serve` handler destructures it with `..` and never binds it. A "control that looks active and isn't" | `main.rs:99-102,218-222,1750` |
| 4 | MED | Supply chain | `install.sh` checksum verification **fails open**: a missing published `.sha256` (line 109) or an absent `sha256sum`/`shasum` (line 81) both **warn-and-proceed** to install an unverified binary. Also, checksum shares the binary's origin → no MITM protection, only corruption detection | `scripts/install.sh:75-84,101-110` |
| 5 | MED | Container | Final Docker image runs as **root** (no `USER`) — the MCP server parses untrusted input and writes `/data` as UID 0 | `Dockerfile:19-34` |
| 6 | MED | Supply chain (claim) | SECURITY.md states *"`cargo audit` run in CI on every push"* — **no workflow runs it** (verified across all 12 workflows). `cargo audit` locally: 0 vulns, 2 unmaintained-only advisories (`paste`, `ttf-parser`) | `.github/workflows/*`; `SECURITY.md` |
| 7 | LOW | Crypto (defense-in-depth) | AEAD AAD is `category:key` — it does **not** bind `workspace_hash`, though entity identity is the `(category,key,workspace_hash)` triple. Honestly documented ("`category:key` … anti-swap"), so not a claim violation, but a cross-workspace ciphertext swap is possible with DB-file write access | `db.rs:195,2513` |
| 8 | LOW | Container/registry | Base images (`rust:1.96-alpine`, `alpine:3.21`) and the `server.json` OCI ref are pinned by **mutable tag, not digest** | `Dockerfile:11,19`; `server.json:9` |
| 9 | LOW | Build supply chain | Default `bundled-embeddings` build fetches an ONNX model at compile time (SHA-256 pinned — good), but `ort` `features=["download-binaries"]` pulls a prebuilt native lib at build time **not** covered by that pinning | `build.rs`; `Cargo.toml:42,74` |
| 10 | LOW | DoS hardening | `handle_traverse` passes agent-supplied `max_depth`/`max_nodes` with **no upper clamp** (bounded only by graph size + `visited` dedup) | `tools.rs:1566` |
| 11 | LOW | Hardening | Dense recall path casts `params.limit as usize` **without `.max(0)`** (the Hybrid path guards it); harmless today (downstream `pool_target`/`max_scan` caps neutralize it) but inconsistent | `db.rs:3047` |
| 12 | LOW | SSRF-ish | GitHub connector interpolates operator-config `repo` into the API URL **without URL-encoding/validation** (host hardcoded to api.github.com; operator-config trust) | `connectors/github.rs:76-79` |
| 13 | LOW | Windows | On Windows, `keygen` warns-and-continues if `icacls` fails → key file may persist with inherited ACLs (documented in THREAT-MODEL; local multi-user only) | `main.rs:763-789` |

Root-cause groupings: **#1/#2** are one cause — an audit chain built on a non-crypto,
payload-blind, never-verified primitive. **#4/#5/#6/#8/#9** are supply-chain/deploy
posture. **#3** is a dead auth control.

## By design — NOT vulnerabilities (per `docs/THREAT-MODEL.md`)
The following were flagged by an automated pass as "critical cross-tenant" issues but are
the **documented, intended design** for a local-first, trusted-caller engine, and are
internally consistent with the threat model:
- **Client-asserted `workspace_hash` (no caller auth).** THREAT-MODEL §5: scoping is a
  "routing/relevance control, not an enforced security boundary … Don't treat it as
  multi-tenant isolation." Correct and documented.
- **`as_of`/`share`/`recall` spanning workspaces when scope is omitted.** Same rationale;
  the caller is trusted. (Note `as_of` also ignores `archived` — a *functional* hygiene
  inconsistency worth a follow-up, not a security bug.)
- **`forget` is soft-delete only (leaves embeddings/history).** Explicitly documented
  (README: "`mimir_forget` — Soft-delete (archived=1)"; `mimir_purge` is the eraser).

**Recommendation:** keep launch/marketing/CMMC language aligned with this — do **not**
describe workspace scoping as "isolation" or the current chain as "tamper-proof." Finding
#1's fix is the prerequisite for any honest "audit trail" / CMMC claim.

## Confirmed sound (re-verified — no action)
- **No SQLi** (parameterized; only compile-time literals/`?`-lists/int constants
  interpolated). **No FTS5 injection** (every term double-quote-escaped + bound to `MATCH ?`).
  **No shell/raw-SQL tool** (only `Command::new` sites — python3 embedding, Windows
  `icacls` — are argv-separated, no `sh -c`, untrusted text in a distinct argv slot).
- **Crypto core**: single `encrypt()`, fresh `OsRng` 96-bit nonce per call (no reuse);
  `OsRng` keygen; `0600` key file (Unix) + defense-in-depth re-chmod; **no `decrypt().unwrap_or(raw)`
  anywhere** — all read paths honor `AuthFailed`; fail-fast key canary on startup;
  legacy-plaintext rows correctly classified.
- **Transport**: default `stdio`/`127.0.0.1`/dashboard-off; `guard_bind` exits on any
  non-loopback bind without a token (unless `MIMIR_ALLOW_INSECURE_BIND=1`); constant-time
  token compare (`subtle`); SSE GET behind `route_layer` auth; gRPC dormant (no reflection).
- **Resource caps**: `limit` clamp `(0,1000)`; traverse `visited` cycle guard; community
  `MAX_ALGO_ITERS`; ingest `MIMIR_MAX_INGEST_BYTES`; DOCX zip-bomb decompressed-bytes cap;
  dense `max_scan`/pool clamp. **Concurrency**: WAL + `BEGIN IMMEDIATE` + busy-timeout +
  transient-`SQLITE_BUSY` retry; purge is single-transaction (no readable history after delete).
- **Deps**: `Cargo.lock` committed, no git/`[patch]` deps, no known-CVE crate; OCI label
  matches `server.json` (registry ownership check passes); no secrets baked in the image.

## Recommended fix order
- **Now (supply/deploy, no engine risk):** #4 (install.sh hard-fail), #5 (non-root `USER`),
  #6 (add the `cargo audit` CI job the docs already promise), #8 (digest pins).
- **Now (small code):** #3 (wire or remove `--workspace-token`), #10/#11 (clamps), #12 (encode `repo`).
- **Partially addressed (this review's follow-up PR):** #1 — the chain is now a real
  **SHA-256** (was 64-bit SipHash) and `verify_audit_chain` is **wired to a
  `verify-audit-chain` CLI command** (was dead code); ships a v13→v14 rehash migration.
  **Important correction:** the chain's *exclusion of the payload is deliberate*, not an
  oversight — `purge`/redaction must be able to erase payload content (GDPR) while the
  chain over event existence/order/time/workspace stays verifiable. So "cover the
  payload" cannot be done naively; it requires a redaction-safe per-entry payload
  *commitment*.
- **Still open (migration-sensitive — the external-audit item):** the chain remains
  **unkeyed**, so it is not tamper-evident against an attacker who can recompute it. True
  tamper-evidence needs a **keyed MAC** (HMAC off the encryption key; note the
  encryption-set-after-open ordering must be handled) plus the payload commitment above
  for content-integrity. Then #7 (bind `workspace_hash` into the AEAD AAD with a rekey
  migration).

## External audit
The engine internals are strong and the threat model is refreshingly honest. Before any
**government/CMMC** positioning of the "audit trail," an external audit of the
audit-chain redesign (#1/#2) is warranted — that is the one area where a stated
compliance claim currently outruns the implementation.
