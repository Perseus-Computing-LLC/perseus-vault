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
