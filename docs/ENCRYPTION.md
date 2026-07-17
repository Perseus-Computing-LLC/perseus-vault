# Mimir Encryption Specification

This document specifies exactly how Mimir encrypts data at rest: the algorithm,
key format, what is and is **not** encrypted, and the security properties (and
limits) you can rely on. It is intentionally precise so you can reason about
Mimir in a threat model — see [THREAT-MODEL.md](./THREAT-MODEL.md).

Encryption is **opt-in** and **off by default**. A fresh database is plaintext
SQLite until you start Mimir with a key.

---

## 1. Algorithm

| Property | Value |
|---|---|
| Cipher | AES-256-GCM (AEAD) |
| Key size | 256 bits (32 bytes) |
| Nonce | 96-bit (12-byte), random per message, from the OS CSPRNG (`OsRng`) |
| Authentication tag | 128-bit (GCM default), verified on every decrypt |
| AAD (additional authenticated data) | `"{category}:{key}"` of the entity |
| Implementation | [`aes-gcm`](https://crates.io/crates/aes-gcm) crate (RustCrypto) |

Each ciphertext record is stored as `base64( nonce_12_bytes || ciphertext || tag )`.
The nonce is generated fresh for every encryption and prepended to the output;
decryption splits it back off.

### Why AAD matters

The entity's `category:key` is bound into the ciphertext as AAD. Decryption fails
if the AAD does not match — so an attacker who can write to the database **cannot
move a valid ciphertext from one entity to another** (a copy/replace attack)
without detection. The tag covers both the body and the identity it belongs to.

---

## 2. Key format and management

Mimir uses a **raw 256-bit key**, not a passphrase. There is **no
password-based key derivation** (no Argon2/PBKDF/scrypt). The key is read
verbatim from a key file.

### Key file

- Content: a single base64-encoded 32-byte key (trailing whitespace trimmed).
- Default path: `~/.mimir/secret.key` (`%USERPROFILE%\.mimir\secret.key` on Windows).
- A key of the wrong length is rejected at startup.

### Generating a key

```bash
mimir keygen                          # writes ~/.mimir/secret.key
mimir keygen --key-file /path/to.key  # custom location
```

`keygen` draws 32 bytes from the OS CSPRNG and base64-encodes them.

> **Filesystem permissions caveat.** On **Unix**, `keygen` sets the key file to
> `0o600` (owner read/write only). On **Windows**, the file is created with the
> directory's default ACL — Mimir does **not** tighten Windows ACLs. If you run
> on Windows, restrict the key file's ACL yourself.

### Using a key

```bash
mimir --encryption-key ~/.mimir/secret.key
```

Mimir never stores, transmits, escrows, or logs the key. Key custody,
rotation, and backup are entirely the operator's responsibility.

### Rotation and recovery

- **No automatic rotation.** There is no built-in scheduled rotation. Rotating a
  key means decrypting with the old key and re-writing with the new one
  (e.g. `perseus-vault init --rekey --key-file /path/to/new-key`, see below).
- **Key recovery is manual.** If the key is lost, encrypted `body_json` is
  unrecoverable — back up the key file immediately after `init` or `keygen`.
  See [Key recovery procedure](#key-recovery-procedure) below.
- **Wrong-key startup fails closed.** When a key is loaded, the canary is
  verified before any read/write. If authentication fails, Vault refuses to
  start with a fatal error — it no longer silently returns ciphertext as
  plaintext.
- **Keys are never stored in SQLite** or printed in diagnostics. The key file
  is read once at startup and kept only in process memory. The `doctor` command
  reads the canary table but never reveals the key material.

### Key recovery procedure

1. **Back up the key file immediately after generating it:**
   ```bash
   cp ~/.perseus-vault/secret.key ~/.perseus-vault/secret.key.backup-$(date +%F)
   ```
   Store a second copy off-site (e.g. encrypted vault, password manager).

2. **Back up the database** before any rekey or migration:
   ```bash
   cp ~/.perseus-vault/data/perseus-vault.db ~/.perseus-vault/data/perseus-vault.db.backup-$(date +%F)
   ```

3. **Test your backup** by restoring it on a different machine:
   ```bash
   perseus-vault doctor --db /tmp/restored.db  # confirms encryption state
   perseus-vault serve --db /tmp/restored.db --encryption-key /path/to/secret.key
   ```

4. **If the key is lost** and the database is encrypted:
   - `body_json` content is **permanently unrecoverable**.
   - Metadata (categories, keys, timestamps, FTS index) is still readable.
   - Run `perseus-vault init --rekey` with a new key to encrypt plaintext-only
     write targets (new writes only; existing ciphertext stays unrecoverable).
   - Filesystem-level recovery tools (extundelete, PhotoRec) on the key file's
     directory may help if the key was recently deleted.

### Plaintext, encrypted, and mixed databases

Perseus Vault categorises a database into one of three storage states,
reported by `perseus-vault doctor` without requiring an encryption key:

| State | `doctor` output | Meaning |
|-------|----------------|---------|
| **Plaintext** | `plaintext (not encrypted ...)` | No `encryption_canary` table; all `body_json` values are raw JSON. A key has never been provided, or was removed after creation. Safe to read without a key. |
| **Encrypted** | `[ENCRYPTED] AES-256-GCM canary present` | The canary exists; `body_json` values are ciphertext. A key is required for reads and writes. The canary is verified on every startup — a wrong key is rejected with a fatal error. |
| **Mixed legacy** | `[WARN] mixed — some bodies appear encrypted` | The canary is **absent** but some `body_json` values match the ciphertext format. This happens when encryption was enabled and later the canary was lost (e.g. a partial restore from backup). Run `perseus-vault init --rekey` to establish a canary and normalise the state. |

The `perseus-vault init` command always produces an **encrypted** database.
A fresh database started without a key is **plaintext**.

---

## 3. Encryption scope — what is and is NOT encrypted

Encryption covers **only the `body_json` column of the `entities` table** — the
free-form content of a memory. Everything Mimir needs in cleartext to index,
search, and route memories is stored **unencrypted**.

### Encrypted

| Data | Where |
|---|---|
| Entity body (the memory content) | `entities.body_json` |

### NOT encrypted (plaintext on disk)

| Data | Where | Why |
|---|---|---|
| **Full-text search index** | `entities_fts` (FTS5) | FTS5 indexes the **plaintext** body so keyword search works. **This is the most important caveat — see below.** |
| Category, key | `entities.category`, `entities.key` | Lookup keys; also used as AAD |
| Tags, topic path, type, source | `entities.*` | Filtering / routing |
| Status, layer, decay score, counts, timestamps | `entities.*` | Ranking / lifecycle |
| Workspace hash, agent id, visibility | `entities.*` | Multi-tenant scoping |
| Embedding vectors | embedding storage | Derived from body content; stored as raw floats |
| Journal entries, state key/value, links | their tables | Not in scope of body encryption |

### ⚠️ The FTS5 index holds plaintext

When encryption is enabled, `entities.body_json` is ciphertext, **but the
`entities_fts` full-text index still stores the body in plaintext** (this is
required for FTS5 keyword search to function, and is asserted directly in the
code). An attacker who can read the SQLite file can therefore recover memory
**content** from the FTS5 shadow tables (`entities_fts_content` and friends) —
**encryption of `body_json` alone does not make the database opaque.**

If your threat model requires the *content* to be unreadable from the database
file, you must **also** protect the file itself — e.g. full-disk / filesystem
encryption (LUKS, FileVault, BitLocker) or an encrypted volume. Today, treat
Mimir's at-rest encryption as **defense-in-depth for the `body_json` column**,
layered under OS-level disk encryption — not as a standalone guarantee that the
database file reveals nothing.

> A future option to index ciphertext (or to disable FTS under encryption, or a
> blind-index scheme) would close this gap; it is not implemented today. Stating
> the current limit honestly is the point of this document.

---

## 4. Encryption in transit

At-rest encryption is independent of transport. Mimir's default transport is
**MCP over local stdio** (no network). If you enable an HTTP/SSE transport,
secure it with TLS and authentication at the deployment layer — see
[transport.md](./transport.md). The encryption key is **not** involved in
transport security.

---

## 5. Properties you can rely on (and cannot)

**You can rely on:**
- `entities.body_json` is confidential at rest under AES-256-GCM, given a secret key.
- Body integrity/authenticity is verified on read (GCM tag), bound to the
  entity's `category:key` via AAD.
- Keys never leave the machine; no telemetry, no escrow.

**You cannot rely on (today):**
- The database file being opaque — **metadata and the FTS plaintext index are
  readable without the key**.
- Passphrase strength — the key is a raw 32-byte value; protect the key file.
- Forward secrecy or per-record keys — one static key encrypts all bodies.
- Automatic rotation or key recovery.

---

*Verified against `src/encryption.rs` and `src/db.rs` at v2.2.1. If the
implementation changes, update this spec in the same PR.*
