use aes_gcm::aead::{Aead, KeyInit, OsRng};
use aes_gcm::{Aes256Gcm, Key, Nonce};
use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use rand::RngCore;

/// Search-index format used when the body-encryption key is loaded.
///
/// This is deliberately a separate, explicit storage mode: the FTS5 table
/// contains keyed blind terms, never the body itself. It is not SQLite page
/// encryption; metadata, schema, and other non-body tables remain plaintext.
pub(crate) const BLIND_TOKEN_SEARCH_MODE: &str = "hmac-sha256-blind-token-v1";

const SEARCH_KEY_DOMAIN: &[u8] = b"perseus-vault/search-index/v1\0";
const MIN_BLIND_PREFIX_CHARS: usize = 3;
const MAX_BLIND_PREFIX_CHARS: usize = 32;

/// Normalize text into the word boundaries used by both the blind-index writer
/// and query builder. JSON punctuation is discarded, so bodies and queries use
/// one representation without storing JSON text in FTS5.
pub(crate) fn search_terms(text: &str) -> Vec<String> {
    text.to_lowercase()
        .split(|c: char| !c.is_alphanumeric())
        .filter(|term| !term.is_empty())
        .map(str::to_string)
        .collect()
}

/// Manages AES-256-GCM encryption for entity body_json and the keyed blind
/// search index derived from the same operator key.
pub struct EncryptionManager {
    cipher: Aes256Gcm,
    /// Subkey derived from the raw encryption key, used to key the journal audit
    /// chain's HMAC (see docs/audit-chain-keyed-mac-design.md). Domain-separated
    /// from the AEAD key so the two uses can never collide.
    audit_key: [u8; 32],
    /// Domain-separated HMAC key for FTS blind terms. The raw operator key is
    /// never used directly as an FTS token key and is never persisted.
    search_key: [u8; 32],
}

/// Derive the audit-chain MAC subkey from the raw 32-byte encryption key.
/// `SHA256("perseus-vault/audit-chain/v1\0" || key)` — domain-separated.
fn derive_audit_key(key_bytes: &[u8]) -> [u8; 32] {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(b"perseus-vault/audit-chain/v1\0");
    h.update(key_bytes);
    h.finalize().into()
}

/// Result of attempting to decrypt a stored `body_json` in a possibly-mixed DB
/// (one where encryption was enabled after some rows were already written plain).
pub enum BodyDecrypt {
    /// Ciphertext that authenticated and decrypted successfully.
    Plaintext(String),
    /// The stored value is not Perseus Vault ciphertext at all (a legacy plaintext row);
    /// it is safe to use as-is. JSON bodies always start with `{`, which is not in
    /// the base64 alphabet, so real plaintext is reliably classified here.
    LegacyPlaintext(String),
    /// The value WAS well-formed ciphertext but failed authentication — wrong key
    /// or tampered / AAD-mismatched data. The raw bytes MUST NOT be returned to the
    /// caller; doing so would silently defeat the AES-256-GCM integrity guarantee.
    AuthFailed(String),
}

impl EncryptionManager {
    /// Load an encryption key from a base64-encoded key file.
    /// Supports `~` expansion for home directory paths.
    pub fn from_key_file(path: &str) -> Result<Self, String> {
        let expanded = if path.starts_with("~/") {
            let home = std::env::var("HOME")
                .or_else(|_| std::env::var("USERPROFILE"))
                .unwrap_or_else(|_| "/root".to_string());
            path.replacen("~", &home, 1)
        } else {
            path.to_string()
        };

        let key_b64 = std::fs::read_to_string(&expanded)
            .map_err(|e| format!("Cannot read key file {}: {}", expanded, e))?
            .trim()
            .to_string();

        let key_bytes = B64
            .decode(&key_b64)
            .map_err(|e| format!("Invalid base64 key in {}: {}", expanded, e))?;

        if key_bytes.len() != 32 {
            return Err(format!(
                "Invalid key length: expected 32 bytes (256-bit), got {}",
                key_bytes.len()
            ));
        }

        let key = Key::<Aes256Gcm>::from_slice(&key_bytes);
        let cipher = Aes256Gcm::new(key);
        let audit_key = derive_audit_key(&key_bytes);
        let search_key = crate::db::hmac_sha256(&key_bytes, SEARCH_KEY_DOMAIN);

        Ok(Self {
            cipher,
            audit_key,
            search_key,
        })
    }

    /// The audit-chain HMAC subkey derived from this manager's encryption key.
    /// Used to key the journal chain (docs/audit-chain-keyed-mac-design.md).
    pub fn audit_key(&self) -> &[u8; 32] {
        &self.audit_key
    }

    /// HMAC-SHA256 blind token for one normalized search term. The output is
    /// lowercase hexadecimal so SQLite's default FTS tokenizer sees exactly
    /// one safe alphanumeric token.
    pub(crate) fn blind_token(&self, term: &str) -> String {
        let mac = crate::db::hmac_sha256(&self.search_key, term.as_bytes());
        let mut out = String::with_capacity(mac.len() * 2);
        for byte in mac {
            use std::fmt::Write as _;
            let _ = write!(out, "{byte:02x}");
        }
        out
    }

    /// Encode plaintext into the FTS5 representation for an encrypted store.
    /// Each body token contributes its full blind token and bounded prefixes;
    /// this preserves exact keyword recall and the common >=3-character prefix
    /// behavior without putting a recoverable body or query string on disk.
    pub(crate) fn blind_index_text(&self, plaintext: &str) -> String {
        let mut terms = Vec::new();
        for term in search_terms(plaintext) {
            let chars: Vec<char> = term.chars().collect();
            if chars.len() < MIN_BLIND_PREFIX_CHARS {
                terms.push(self.blind_token(&term));
                continue;
            }
            for length in MIN_BLIND_PREFIX_CHARS..=chars.len().min(MAX_BLIND_PREFIX_CHARS) {
                let prefix: String = chars[..length].iter().collect();
                terms.push(self.blind_token(&prefix));
            }
            if chars.len() > MAX_BLIND_PREFIX_CHARS {
                terms.push(self.blind_token(&term));
            }
        }
        terms.join(" ")
    }

    /// Build a bound-safe FTS5 MATCH expression from raw query fragments.
    /// Callers may pre-filter stopwords or enforce path-specific minimum
    /// lengths; punctuation within each fragment is normalized identically to
    /// the index writer.
    pub(crate) fn blind_query_from_terms(&self, fragments: &[String]) -> String {
        let mut seen = std::collections::HashSet::new();
        let mut out = Vec::new();
        for fragment in fragments {
            for term in search_terms(fragment) {
                let token = self.blind_token(&term);
                if seen.insert(token.clone()) {
                    out.push(format!("\"{token}\""));
                }
            }
        }
        out.join(" OR ")
    }

    /// Generate a new 256-bit key and return it as a base64 string.
    pub fn generate_key() -> String {
        let mut key = [0u8; 32];
        OsRng.fill_bytes(&mut key);
        B64.encode(key)
    }

    /// Encrypt plaintext with AAD (additional authenticated data) and return
    /// base64-encoded ciphertext (nonce prepended).
    /// AAD binds the ciphertext to the provided context (e.g. category + key) so
    /// that swapping encrypted payloads between entities is detected on decryption.
    pub fn encrypt(&self, plaintext: &str, aad: &[u8]) -> Result<String, String> {
        let mut nonce_bytes = [0u8; 12];
        OsRng.fill_bytes(&mut nonce_bytes);
        let nonce = Nonce::from_slice(&nonce_bytes);

        let payload = aes_gcm::aead::Payload {
            msg: plaintext.as_bytes(),
            aad: if aad.is_empty() { b"" } else { aad },
        };
        let ciphertext = self
            .cipher
            .encrypt(nonce, payload)
            .map_err(|e| format!("Encryption failed: {}", e))?;

        // Prepend nonce for decryption
        let mut combined = nonce_bytes.to_vec();
        combined.extend(&ciphertext);
        Ok(B64.encode(&combined))
    }

    /// Mixed-DB-aware decrypt for stored bodies. Distinguishes a legacy plaintext
    /// row (not ciphertext at all -> safe to pass through) from authentic-looking
    /// ciphertext that fails GCM authentication (wrong key or tampering -> the raw
    /// value must NOT be used). This is the variant read paths should use: the old
    /// `decrypt(...).unwrap_or(raw)` pattern silently returned ciphertext on an
    /// auth failure, nullifying the AAD tamper-detection guarantee.
    pub fn decrypt_body(&self, encoded: &str, aad: &[u8]) -> BodyDecrypt {
        let combined = match B64.decode(encoded) {
            Ok(c) => c,
            // Not base64 -> cannot be our ciphertext -> legacy plaintext row.
            Err(_) => return BodyDecrypt::LegacyPlaintext(encoded.to_string()),
        };
        // Perseus Vault ciphertext is nonce(12) + GCM tag(16) + body(>=0) = >= 28 bytes.
        // Anything shorter is not our ciphertext.
        if combined.len() < 12 + 16 {
            return BodyDecrypt::LegacyPlaintext(encoded.to_string());
        }
        let (nonce_bytes, ciphertext) = combined.split_at(12);
        let nonce = Nonce::from_slice(nonce_bytes);
        let payload = aes_gcm::aead::Payload {
            msg: ciphertext,
            aad: if aad.is_empty() { b"" } else { aad },
        };
        match self.cipher.decrypt(nonce, payload) {
            Ok(pt) => match String::from_utf8(pt) {
                Ok(s) => BodyDecrypt::Plaintext(s),
                Err(e) => BodyDecrypt::AuthFailed(format!("decrypted bytes not UTF-8: {}", e)),
            },
            Err(e) => BodyDecrypt::AuthFailed(format!("authentication failed: {}", e)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mgr() -> EncryptionManager {
        let key = Key::<Aes256Gcm>::from_slice(&[7u8; 32]);
        EncryptionManager {
            cipher: Aes256Gcm::new(key),
            audit_key: derive_audit_key(&[7u8; 32]),
            search_key: crate::db::hmac_sha256(&[7u8; 32], SEARCH_KEY_DOMAIN),
        }
    }

    #[test]
    fn decrypt_body_roundtrip_is_plaintext() {
        let m = mgr();
        let ct = m.encrypt("{\"note\":\"hello\"}", b"cat:key").unwrap();
        match m.decrypt_body(&ct, b"cat:key") {
            BodyDecrypt::Plaintext(s) => assert_eq!(s, "{\"note\":\"hello\"}"),
            _ => panic!("expected Plaintext"),
        }
    }

    #[test]
    fn legacy_plaintext_passes_through() {
        // A real JSON body starts with '{' (not base64) -> classified legacy plaintext.
        let m = mgr();
        match m.decrypt_body("{\"note\":\"legacy unencrypted row\"}", b"cat:key") {
            BodyDecrypt::LegacyPlaintext(s) => assert!(s.contains("legacy")),
            _ => panic!("expected LegacyPlaintext"),
        }
    }

    #[test]
    fn tampered_ciphertext_is_authfailed_not_returned() {
        let m = mgr();
        let ct = m.encrypt("{\"secret\":\"x\"}", b"cat:key").unwrap();
        // Flip a byte in the base64 ciphertext body (after the nonce region).
        let mut bytes = ct.into_bytes();
        let i = bytes.len() - 4;
        bytes[i] = if bytes[i] == b'A' { b'B' } else { b'A' };
        let tampered = String::from_utf8(bytes).unwrap();
        match m.decrypt_body(&tampered, b"cat:key") {
            BodyDecrypt::AuthFailed(_) => {}
            BodyDecrypt::Plaintext(_) => panic!("tampered ciphertext authenticated (GCM broken?)"),
            BodyDecrypt::LegacyPlaintext(s) => {
                panic!("tampered ciphertext returned as plaintext: {}", s)
            }
        }
    }

    #[test]
    fn wrong_aad_is_authfailed() {
        let m = mgr();
        let ct = m.encrypt("{\"a\":1}", b"cat:key").unwrap();
        match m.decrypt_body(&ct, b"different:aad") {
            BodyDecrypt::AuthFailed(_) => {}
            _ => panic!("AAD mismatch must fail authentication"),
        }
    }

    #[test]
    fn wrong_key_is_authfailed() {
        let m = mgr();
        let ct = m.encrypt("{\"a\":1}", b"cat:key").unwrap();
        let other = EncryptionManager {
            cipher: Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&[9u8; 32])),
            audit_key: derive_audit_key(&[9u8; 32]),
            search_key: crate::db::hmac_sha256(&[9u8; 32], SEARCH_KEY_DOMAIN),
        };
        match other.decrypt_body(&ct, b"cat:key") {
            BodyDecrypt::AuthFailed(_) => {}
            _ => panic!("wrong key must fail authentication"),
        }
    }

    #[test]
    fn blind_index_contains_only_keyed_terms_and_queries_match_prefixes() {
        let m = mgr();
        let indexed = m.blind_index_text("{\"note\":\"authentication marker\"}");
        assert!(!indexed.contains("authentication"));
        assert!(indexed
            .split_whitespace()
            .all(|term| term.len() == 64 && term.bytes().all(|b| b.is_ascii_hexdigit())));
        let query = m.blind_query_from_terms(&["auth".to_string()]);
        assert!(indexed.contains(query.trim_matches('"')));
        assert!(!query.contains("auth"));
    }

    #[test]
    fn blind_tokens_are_keyed_and_distinct_between_keys() {
        let first = mgr();
        let second = EncryptionManager {
            cipher: Aes256Gcm::new(Key::<Aes256Gcm>::from_slice(&[9u8; 32])),
            audit_key: derive_audit_key(&[9u8; 32]),
            search_key: crate::db::hmac_sha256(&[9u8; 32], SEARCH_KEY_DOMAIN),
        };
        assert_ne!(first.blind_token("marker"), second.blind_token("marker"));
    }
}
