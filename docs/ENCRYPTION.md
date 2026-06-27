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

- **No automatic rotation.** There is no built-in re-encrypt/rekey command.
  Rotating a key means decrypting with the old key and re-writing with the new
  one (e.g. export → re-import under a new key).
- **No recovery.** If the key is lost, encrypted `body_json` is unrecoverable.
- On read, a failed decrypt (wrong key **or** a never-encrypted database) does
  **not** raise an error: Mimir falls back to returning the **stored bytes
  verbatim**. This is what lets a plaintext database be read without a key — but
  it also means that opening an *encrypted* database with the **wrong key**
  silently returns the raw base64 ciphertext as the "body" instead of failing
  loudly. There is no built-in signal that distinguishes "plaintext DB" from
  "encrypted DB, wrong key." Treat a key mismatch as an operator error to catch
  out-of-band; do not rely on the read path to flag it.

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
