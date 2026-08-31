// #850: encryption-by-default bootstrap contract, exercised end-to-end through
// the real CLI with a private HOME.
//
// These tests MUST spawn the binary as a subprocess with a sandboxed HOME
// rather than mutating the test process's environment: `default_db_path()` is
// eagerly evaluated by clap for every `serve`/`write` parse, so a test that
// calls `std::env::set_var("HOME", …)` in-process races with every other test
// that parses a CLI (clap reads HOME mid-parse and `apply_top_level_db`'s
// `*db == default_db_path()` comparison then sees a different value). A child
// process owns its env, so the parent never touches global state.

use std::path::PathBuf;
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_perseus-vault");

fn sandbox(tag: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("perseus-enc-{tag}-{}", uuid::Uuid::new_v4()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

#[test]
fn fresh_default_database_creates_key_canary_and_encrypts_bodies() {
    let home = sandbox("home");
    let db_path = home.join("data").join("perseus-vault.db");
    std::fs::create_dir_all(db_path.parent().unwrap()).unwrap();

    let out = Command::new(BIN)
        .env("HOME", &home)
        .env_remove("PERSEUS_VAULT_ALLOW_PLAINTEXT")
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "bootstrap-key",
            "--body-json",
            r#"{"note":"bootstrap encrypted body"}"#,
        ])
        .output()
        .expect("spawn perseus-vault write");

    assert!(
        out.status.success(),
        "write must succeed under default encryption\nstdout: {}\nstderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );

    // Standard key path under the sandboxed HOME, owner-only on Unix.
    let key_path = home.join(".perseus-vault").join("secret.key");
    assert!(
        key_path.is_file(),
        "key file must exist at {}",
        key_path.display()
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(&key_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "key file must be 0600 on Unix");
    }
    let key_material = std::fs::read_to_string(&key_path).unwrap();
    let key_material = key_material.trim();
    let decoded = base64::Engine::decode(&base64::engine::general_purpose::STANDARD, key_material)
        .expect("key file must be valid base64");
    assert_eq!(decoded.len(), 32, "key file must hold a 32-byte key");

    // Canary established and the written body is ciphertext at rest.
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let canary: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM encryption_canary WHERE id = 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(canary, 1, "encrypted canary must be established by default");
    let stored: String = conn
        .query_row(
            "SELECT body_json FROM entities WHERE key='bootstrap-key'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_ne!(stored, r#"{"note":"bootstrap encrypted body"}"#);
    assert!(
        !stored.contains("bootstrap encrypted body"),
        "ciphertext must not leak the plaintext: {stored}"
    );

    let _ = std::fs::remove_dir_all(&home);
}

#[test]
fn explicit_plaintext_optout_suppresses_default_key_creation() {
    let home = sandbox("optout");
    let db_path = home.join("perseus-vault.db");

    let out = Command::new(BIN)
        .env("HOME", &home)
        .env("PERSEUS_VAULT_ALLOW_PLAINTEXT", "1")
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "optout-key",
            "--body-json",
            r#"{"note":"explicit plaintext opt-out"}"#,
        ])
        .output()
        .expect("spawn perseus-vault write");

    assert!(
        out.status.success(),
        "write must succeed under explicit plaintext opt-out\nstdout: {}\nstderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );

    let key_path = home.join(".perseus-vault").join("secret.key");
    assert!(
        !key_path.exists(),
        "no key file may be created under explicit opt-out: {}",
        key_path.display()
    );

    // Body is stored as plaintext (no encryption was applied).
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let stored: String = conn
        .query_row(
            "SELECT body_json FROM entities WHERE key='optout-key'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(stored, r#"{"note":"explicit plaintext opt-out"}"#);

    let _ = std::fs::remove_dir_all(&home);
}

#[test]
fn init_rekey_migrates_existing_plaintext_rows_and_is_idempotent() {
    let home = sandbox("rekey-migration");
    let db_path = home.join("legacy-plaintext.db");
    let key_path = home.join("migration-key");
    let plaintext = r#"{"note":"legacy plaintext must be migrated"}"#;

    // Establish the pre-migration state explicitly: a real plaintext body and
    // no encryption canary, using the documented compatibility opt-out.
    let write = Command::new(BIN)
        .env("HOME", &home)
        .env("PERSEUS_VAULT_ALLOW_PLAINTEXT", "1")
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "legacy-plaintext",
            "--body-json",
            plaintext,
        ])
        .output()
        .expect("spawn plaintext seed write");
    assert!(
        write.status.success(),
        "plaintext seed must succeed: {}",
        String::from_utf8_lossy(&write.stderr)
    );
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    assert_eq!(
        conn.query_row::<i64, _, _>(
            "SELECT COUNT(*) FROM encryption_canary WHERE id = 1",
            [],
            |r| r.get(0)
        )
        .unwrap(),
        0,
        "the fixture must begin plaintext"
    );
    assert_eq!(
        conn.query_row::<String, _, _>(
            "SELECT body_json FROM entities WHERE key='legacy-plaintext'",
            [],
            |r| r.get(0)
        )
        .unwrap(),
        plaintext
    );
    drop(conn);

    // The explicit migration establishes the canary and encrypts the existing
    // body under the operator-provided key.
    let migrate = Command::new(BIN)
        .env("HOME", &home)
        .env_remove("PERSEUS_VAULT_ALLOW_PLAINTEXT")
        .args([
            "init",
            "--db",
            db_path.to_str().unwrap(),
            "--key-file",
            key_path.to_str().unwrap(),
            "--rekey",
        ])
        .output()
        .expect("spawn init --rekey");
    let migrate_stdout = String::from_utf8_lossy(&migrate.stdout);
    assert!(
        migrate.status.success(),
        "init --rekey must succeed: {migrate_stdout}\n{}",
        String::from_utf8_lossy(&migrate.stderr)
    );
    assert!(
        migrate_stdout.contains("encrypt: 1 records encrypted, 0 skipped, 0 failed"),
        "migration report must account for the legacy row: {migrate_stdout}"
    );

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    assert_eq!(
        conn.query_row::<i64, _, _>(
            "SELECT COUNT(*) FROM encryption_canary WHERE id = 1",
            [],
            |r| r.get(0)
        )
        .unwrap(),
        1,
        "rekey must establish the encryption canary"
    );
    let stored: String = conn
        .query_row(
            "SELECT body_json FROM entities WHERE key='legacy-plaintext'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_ne!(stored, plaintext, "legacy body must no longer be plaintext");
    assert!(
        !stored.contains("legacy plaintext must be migrated"),
        "ciphertext must not contain the migrated body: {stored}"
    );
    drop(conn);

    // A second explicit migration is safe and must not double-encrypt or
    // replace the key/ciphertext.
    let migrate_again = Command::new(BIN)
        .env("HOME", &home)
        .env_remove("PERSEUS_VAULT_ALLOW_PLAINTEXT")
        .args([
            "init",
            "--db",
            db_path.to_str().unwrap(),
            "--key-file",
            key_path.to_str().unwrap(),
            "--rekey",
        ])
        .output()
        .expect("spawn idempotent init --rekey");
    let again_stdout = String::from_utf8_lossy(&migrate_again.stdout);
    assert!(
        migrate_again.status.success(),
        "idempotent init --rekey must succeed: {again_stdout}\n{}",
        String::from_utf8_lossy(&migrate_again.stderr)
    );
    assert!(
        again_stdout.contains("encrypt: 0 records encrypted, 1 skipped, 0 failed"),
        "second migration must skip the already encrypted row: {again_stdout}"
    );

    let _ = std::fs::remove_dir_all(&home);
}

#[test]
fn init_rekey_migrates_archived_plaintext_rows_and_preserves_archive_state() {
    let home = sandbox("rekey-archived");
    let db_path = home.join("legacy-archived.db");
    let key_path = home.join("migration-key");
    let plaintext = r#"{"note":"archived plaintext must be migrated"}"#;

    let write = Command::new(BIN)
        .env("HOME", &home)
        .env("PERSEUS_VAULT_ALLOW_PLAINTEXT", "1")
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "archived-plaintext",
            "--body-json",
            plaintext,
        ])
        .output()
        .expect("spawn archived plaintext seed write");
    assert!(
        write.status.success(),
        "archived plaintext seed must succeed: {}",
        String::from_utf8_lossy(&write.stderr)
    );

    let forget = Command::new(BIN)
        .env("HOME", &home)
        .env("PERSEUS_VAULT_ALLOW_PLAINTEXT", "1")
        .args([
            "forget",
            "--db",
            db_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "archived-plaintext",
            "--reason",
            "test archive",
        ])
        .output()
        .expect("spawn archive command");
    assert!(
        forget.status.success(),
        "archive command must succeed: {}",
        String::from_utf8_lossy(&forget.stderr)
    );

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let (entity_id, archived_before, stored_before): (String, i64, String) = conn
        .query_row(
            "SELECT id, archived, body_json FROM entities WHERE key='archived-plaintext'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(archived_before, 1, "fixture must archive the entity");
    assert!(
        stored_before == plaintext,
        "fixture must begin with the expected plaintext body"
    );
    let (signature_before, _signature_len_before): (i64, i64) = conn
        .query_row(
            "SELECT body_hash, body_len FROM dedup_signatures WHERE entity_id = ?1",
            rusqlite::params![entity_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    drop(conn);

    let migrate = Command::new(BIN)
        .env("HOME", &home)
        .env_remove("PERSEUS_VAULT_ALLOW_PLAINTEXT")
        .args([
            "init",
            "--db",
            db_path.to_str().unwrap(),
            "--key-file",
            key_path.to_str().unwrap(),
            "--rekey",
        ])
        .output()
        .expect("spawn archived init --rekey");
    let migrate_stdout = String::from_utf8_lossy(&migrate.stdout);
    assert!(
        migrate.status.success(),
        "archived init --rekey must succeed: {}",
        String::from_utf8_lossy(&migrate.stderr)
    );
    assert!(
        migrate_stdout.contains("encrypt: 1 records encrypted, 0 skipped, 0 failed"),
        "archived row must be included in migration counts: {migrate_stdout}"
    );

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let (archived_after, stored_after): (i64, String) = conn
        .query_row(
            "SELECT archived, body_json FROM entities WHERE key='archived-plaintext'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(archived_after, 1, "rekey must preserve archive state");
    assert!(
        stored_after != plaintext,
        "archived body must no longer be plaintext"
    );
    assert!(
        !stored_after.contains("archived plaintext must be migrated"),
        "ciphertext must not contain archived body content"
    );
    let (signature_after, signature_len_after): (i64, i64) = conn
        .query_row(
            "SELECT body_hash, body_len FROM dedup_signatures WHERE entity_id = ?1",
            rusqlite::params![entity_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert!(
        signature_after != signature_before,
        "rekey must refresh the dedup signature"
    );
    assert!(
        signature_len_after == stored_after.len() as i64,
        "dedup signature length must match the stored ciphertext"
    );
    drop(conn);

    let verify = Command::new(BIN)
        .env("HOME", &home)
        .args(["verify", "--db", db_path.to_str().unwrap(), "--json"])
        .output()
        .expect("spawn encrypted-state verification");
    let report: serde_json::Value =
        serde_json::from_slice(&verify.stdout).expect("verify --json must emit JSON");
    let c2 = report["checks"]
        .as_array()
        .and_then(|checks| checks.iter().find(|check| check["id"] == "C2"))
        .expect("verify report must include C2");
    assert_eq!(c2["status"], "PASS", "C2 must pass after archived rekey");
    assert_eq!(c2["findings"], serde_json::json!([]));

    let migrate_again = Command::new(BIN)
        .env("HOME", &home)
        .env_remove("PERSEUS_VAULT_ALLOW_PLAINTEXT")
        .args([
            "init",
            "--db",
            db_path.to_str().unwrap(),
            "--key-file",
            key_path.to_str().unwrap(),
            "--rekey",
        ])
        .output()
        .expect("spawn idempotent archived init --rekey");
    let again_stdout = String::from_utf8_lossy(&migrate_again.stdout);
    assert!(
        migrate_again.status.success(),
        "second archived init --rekey must succeed: {}",
        String::from_utf8_lossy(&migrate_again.stderr)
    );
    assert!(
        again_stdout.contains("encrypt: 0 records encrypted, 1 skipped, 0 failed"),
        "second archived migration must be a no-op: {again_stdout}"
    );

    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let (archived_final, stored_final): (i64, String) = conn
        .query_row(
            "SELECT archived, body_json FROM entities WHERE key='archived-plaintext'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        archived_final, 1,
        "idempotent rekey must preserve archive state"
    );
    assert!(
        stored_final == stored_after,
        "idempotent rekey must preserve ciphertext"
    );
    drop(conn);

    let _ = std::fs::remove_dir_all(&home);
}

// #1018 companion guard: keygen/init previously truncated any key file at the
// resolved path, which would destroy the key of an existing encrypted vault
// (precedence resolution now finds legacy `~/.mimir/secret.key` files, making
// the footgun reachable again). Keygen must fail closed on an existing key;
// init must USE the existing key as-is ("generates a key, if none exists")
// and leave its bytes untouched.
#[test]
fn keygen_refuses_and_init_reuses_an_existing_key_file() {
    let home = sandbox("keyguard");
    let key_path = home.join("secret.key");
    let db_path = home.join("vault.db");

    // Create a real key with keygen (fresh path).
    let keygen = Command::new(BIN)
        .env("HOME", &home)
        .args(["keygen", "--key-file", key_path.to_str().unwrap()])
        .output()
        .expect("spawn perseus-vault keygen");
    assert!(
        keygen.status.success(),
        "keygen on a fresh path must succeed: {}",
        String::from_utf8_lossy(&keygen.stderr)
    );
    let key_before = std::fs::read_to_string(&key_path).unwrap();

    // keygen again on the same path: refused, key untouched.
    let keygen2 = Command::new(BIN)
        .env("HOME", &home)
        .args(["keygen", "--key-file", key_path.to_str().unwrap()])
        .output()
        .expect("spawn perseus-vault keygen");
    assert!(
        !keygen2.status.success(),
        "keygen must refuse to overwrite an existing key file"
    );
    assert!(
        String::from_utf8_lossy(&keygen2.stderr).contains("refusing to overwrite existing key file"),
        "refusal must name the action: {}",
        String::from_utf8_lossy(&keygen2.stderr)
    );
    assert_eq!(std::fs::read_to_string(&key_path).unwrap(), key_before);

    // init on the same key: reuses it, succeeds, bytes untouched.
    let init = Command::new(BIN)
        .env("HOME", &home)
        .args([
            "init",
            "--db",
            db_path.to_str().unwrap(),
            "--key-file",
            key_path.to_str().unwrap(),
        ])
        .output()
        .expect("spawn perseus-vault init");
    assert!(
        init.status.success(),
        "init must reuse an existing key: {}",
        String::from_utf8_lossy(&init.stderr)
    );
    assert_eq!(std::fs::read_to_string(&key_path).unwrap(), key_before);
    // The database was encrypted with that key (canary established).
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let canary: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM encryption_canary WHERE id = 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(canary, 1, "init must establish the canary with the existing key");
    drop(conn);

    let _ = std::fs::remove_dir_all(&home);
}

// #1018: a v2.21-era encrypted vault (canary under the pre-rebrand
// `mimir_internal` AAD, key at the legacy `~/.mimir/secret.key` path) must
// open with the CURRENT binary using that same key — no key regeneration, no
// manual DB repair — and recall must keep working through the MCP server.
// The fixture is synthesized by re-encrypting the canary under the legacy AAD
// with the same AES-256-GCM scheme the binary uses (aes-gcm is a direct
// dependency, so the wire format matches by construction).
#[test]
fn legacy_mimir_vault_opens_and_recalls_with_current_binary() {
    use aes_gcm::aead::{Aead, KeyInit, OsRng};
    use aes_gcm::aead::rand_core::RngCore;
    use aes_gcm::{Aes256Gcm, Key, Nonce};
    use base64::Engine as _;
    use std::io::{BufRead, BufReader, Write};
    use std::process::Stdio;

    let home = sandbox("legacy1018");
    let db_path = home.join("mimir.db");
    let key_path = home.join(".mimir").join("secret.key");
    std::fs::create_dir_all(key_path.parent().unwrap()).unwrap();

    // 1. v2.21-era key at the legacy path.
    let keygen = Command::new(BIN)
        .env("HOME", &home)
        .args(["keygen", "--key-file", key_path.to_str().unwrap()])
        .output()
        .expect("spawn perseus-vault keygen");
    assert!(
        keygen.status.success(),
        "keygen failed: {}",
        String::from_utf8_lossy(&keygen.stderr)
    );

    // 2. Bootstrap the encrypted fixture with the current binary.
    let write1 = Command::new(BIN)
        .env("HOME", &home)
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--encryption-key",
            key_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "legacy-entity",
            "--body-json",
            r#"{"note":"pre-rebrand secret"}"#,
        ])
        .output()
        .expect("spawn perseus-vault write");
    assert!(
        write1.status.success(),
        "fixture write failed: {}",
        String::from_utf8_lossy(&write1.stderr)
    );

    // 3. Downgrade the canary to the v2.21-era AAD (`mimir_internal`).
    let key_b64 = std::fs::read_to_string(&key_path).unwrap();
    let key_bytes = base64::engine::general_purpose::STANDARD
        .decode(key_b64.trim())
        .unwrap();
    let cipher = Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&key_bytes));
    let current_aad = format!(
        "{}:{}:{}",
        "perseus_vault_internal".len(),
        "perseus_vault_internal",
        "encryption_canary"
    );
    let legacy_aad = format!(
        "{}:{}:{}",
        "mimir_internal".len(),
        "mimir_internal",
        "encryption_canary"
    );
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    let stored: String = conn
        .query_row(
            "SELECT ciphertext FROM encryption_canary WHERE id = 1",
            [],
            |r| r.get(0),
        )
        .unwrap();
    let combined = base64::engine::general_purpose::STANDARD
        .decode(&stored)
        .unwrap();
    let (nonce_bytes, ct_body) = combined.split_at(12);
    let plain = cipher
        .decrypt(
            Nonce::from_slice(nonce_bytes),
            aes_gcm::aead::Payload {
                msg: ct_body,
                aad: current_aad.as_bytes(),
            },
        )
        .expect("fixture canary must decrypt under the current AAD");
    let mut new_nonce = [0u8; 12];
    OsRng.fill_bytes(&mut new_nonce);
    let legacy_ct = cipher
        .encrypt(
            Nonce::from_slice(&new_nonce),
            aes_gcm::aead::Payload {
                msg: plain.as_slice(),
                aad: legacy_aad.as_bytes(),
            },
        )
        .expect("re-encrypt canary under the legacy AAD");
    // App format is base64(nonce[12] || ciphertext) — prepend the nonce.
    let mut combined = new_nonce.to_vec();
    combined.extend_from_slice(&legacy_ct);
    let legacy_ct_b64 = base64::engine::general_purpose::STANDARD.encode(combined);
    conn.execute(
        "UPDATE encryption_canary SET ciphertext = ?1 WHERE id = 1",
        rusqlite::params![legacy_ct_b64],
    )
    .unwrap();
    drop(conn);

    // 4. The CURRENT binary must open the v2.21-era vault with its existing
    //    key. Pre-fix this exits 1 with "failed to decrypt encryption canary".
    let write2 = Command::new(BIN)
        .env("HOME", &home)
        .args([
            "write",
            "--db",
            db_path.to_str().unwrap(),
            "--encryption-key",
            key_path.to_str().unwrap(),
            "--category",
            "facts",
            "--key",
            "legacy-entity-2",
            "--body-json",
            r#"{"note":"post-upgrade secret"}"#,
        ])
        .output()
        .expect("spawn perseus-vault write");
    let stderr2 = String::from_utf8_lossy(&write2.stderr);
    assert!(
        write2.status.success(),
        "upgraded binary must open a v2.21-era encrypted vault with its existing key\nstderr: {stderr2}"
    );
    assert!(
        !stderr2.contains("failed to decrypt encryption canary"),
        "the canary regression error must not fire: {stderr2}"
    );

    // 5. Startup + recall through the real MCP stdio server, same key.
    let mut child = Command::new(BIN)
        .env("HOME", &home)
        .args([
            "serve",
            "--db",
            db_path.to_str().unwrap(),
            "--encryption-key",
            key_path.to_str().unwrap(),
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn perseus-vault serve");
    let mut stdin = child.stdin.take().unwrap();
    let stdout_handle = child.stdout.take().unwrap();

    // Reader thread: the stdio server answers on stdout. A channel decouples
    // reading from the bounded wait below, so a silent server can never hang
    // the CI job forever (the pre-fix #1018 crash is one failure mode; a
    // stuck server is the other).
    let (tx, rx) = std::sync::mpsc::channel::<String>();
    let reader = std::thread::spawn(move || {
        let stdout = BufReader::new(stdout_handle);
        for line in stdout.lines() {
            match line {
                Ok(l) => {
                    if tx.send(l).is_err() {
                        break;
                    }
                }
                Err(_) => break,
            }
        }
    });

    // One JSON-RPC exchange: initialize, then recall. Fails fast if the
    // server exits (the #1018 regression) or goes silent (hard 240s
    // deadline per call, then the child is killed).
    fn mcp_call(
        stdin: &mut std::process::ChildStdin,
        child: &mut std::process::Child,
        rx: &std::sync::mpsc::Receiver<String>,
        payload: &str,
        target: u64,
    ) -> String {
        use std::time::{Duration, Instant};
        writeln!(stdin, "{payload}").unwrap();
        let deadline = Duration::from_secs(240);
        let start = Instant::now();
        loop {
            match rx.recv_timeout(Duration::from_secs(5)) {
                Ok(line) => {
                    if let Ok(v) = serde_json::from_str::<serde_json::Value>(&line) {
                        if v.get("id").and_then(|i| i.as_u64()) == Some(target) {
                            return line;
                        }
                    }
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                    if let Ok(Some(status)) = child.try_wait() {
                        panic!("serve exited ({status}) before answering id {target}");
                    }
                    if start.elapsed() >= deadline {
                        let _ = child.kill();
                        panic!(
                            "serve did not answer id {target} within {deadline:?} — killed it"
                        );
                    }
                }
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                    panic!("serve stdout closed before answering id {target}");
                }
            }
        }
    }

    let init = mcp_call(
        &mut stdin,
        &mut child,
        &rx,
        r#"{"jsonrpc":"2.0","id":0,"method":"initialize","params":{}}"#,
        0,
    );
    assert!(init.contains("\"result\""), "initialize failed: {init}");

    // Initialized notification — no response expected.
    stdin
        .write_all(br#"{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}"#)
        .unwrap();
    stdin.write_all(b"\n").unwrap();

    let recall = mcp_call(
        &mut stdin,
        &mut child,
        &rx,
        r#"{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"perseus_vault_recall","arguments":{"query":"pre-rebrand secret"}}}"#,
        1,
    );
    assert!(recall.contains("\"result\""), "recall failed: {recall}");
    assert!(
        recall.contains("legacy-entity"),
        "recall must return the pre-rebrand entity: {recall}"
    );

    // Shutdown: closing stdin makes the stdio server exit (EOF), which also
    // ends the reader thread.
    drop(stdin);
    let _ = child.wait();
    let _ = reader.join();
    let _ = std::fs::remove_dir_all(&home);
}
