//! Fernet decryption of gobby's SecretStore.
//!
//! Replicates the Python chain:
//! 1. Read ~/.gobby/machine_id (plain text)
//! 2. Read ~/.gobby/.secret_salt (16 raw bytes)
//! 3. PBKDF2-HMAC-SHA256(password=machine_id, salt=salt, iterations=600_000, length=32)
//! 4. base64url_encode(key_bytes) → Fernet key
//! 5. Fernet(key).decrypt(encrypted_value) → plaintext
//!
//! Source: src/gobby/storage/secrets.py, src/gobby/utils/machine_id.py

use std::path::Path;

use anyhow::{Context as _, bail};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use pbkdf2::pbkdf2_hmac;
use sha2::Sha256;

/// Derive a Fernet key from machine_id + salt using PBKDF2-HMAC-SHA256.
/// Matches Python: _derive_fernet_key() in storage/secrets.py
fn derive_fernet_key(machine_id: &str, salt: &[u8]) -> String {
    let mut key_bytes = [0u8; 32];
    pbkdf2_hmac::<Sha256>(machine_id.as_bytes(), salt, 600_000, &mut key_bytes);
    URL_SAFE.encode(key_bytes)
}

/// Decrypt a Fernet-encrypted token string.
fn decrypt_fernet(key: &str, token: &str) -> anyhow::Result<String> {
    let fernet = fernet::Fernet::new(key).ok_or_else(|| anyhow::anyhow!("invalid Fernet key"))?;
    let plaintext = fernet
        .decrypt(token)
        .map_err(|_| anyhow::anyhow!("Fernet decryption failed (machine ID may have changed)"))?;
    String::from_utf8(plaintext).context("decrypted secret is not valid UTF-8")
}

/// Resolve a secret by name from the secrets table in gobby-hub.db.
///
/// Secret names are normalized to lowercase (matching Python SecretStore._normalize_name).
pub fn resolve_secret(db_path: &Path, secret_name: &str) -> anyhow::Result<String> {
    let gobby_dir = dirs::home_dir()
        .context("cannot determine home directory")?
        .join(".gobby");

    // Read machine_id
    let machine_id_path = gobby_dir.join("machine_id");
    let machine_id = std::fs::read_to_string(&machine_id_path)
        .with_context(|| format!("failed to read {}", machine_id_path.display()))?
        .trim()
        .to_string();
    if machine_id.is_empty() {
        bail!("machine_id file is empty");
    }

    // Read salt (16 raw bytes)
    let salt_path = gobby_dir.join(".secret_salt");
    let salt = std::fs::read(&salt_path)
        .with_context(|| format!("failed to read {}", salt_path.display()))?;

    // Derive Fernet key
    let fernet_key = derive_fernet_key(&machine_id, &salt);

    // Read encrypted value from DB
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )
    .with_context(|| {
        format!(
            "failed to open DB for secret resolution: {}",
            db_path.display()
        )
    })?;
    conn.busy_timeout(std::time::Duration::from_millis(5000))?;

    let name = secret_name.trim().to_lowercase();
    let encrypted: String = conn
        .query_row(
            "SELECT encrypted_value FROM secrets WHERE name = ?1",
            rusqlite::params![name],
            |row| row.get(0),
        )
        .with_context(|| format!("secret '{name}' not found in secrets table"))?;

    decrypt_fernet(&fernet_key, &encrypted)
}

/// Resolve `$secret:NAME` and `${VAR}` patterns in a config value.
///
/// - `$secret:NAME` → decrypt from secrets table
/// - `${VAR}` → environment variable
/// - `${VAR:-default}` → environment variable with default
/// - plain text → returned unchanged
pub fn resolve_config_value(value: &str, db_path: &Path) -> anyhow::Result<String> {
    // Fast path: no patterns
    if !value.contains("$secret:") && !value.contains("${") {
        return Ok(value.to_string());
    }

    // $secret:NAME pattern
    if let Some(name) = value.strip_prefix("$secret:") {
        return resolve_secret(db_path, name);
    }

    // ${VAR} or ${VAR:-default} pattern
    if value.starts_with("${") && value.ends_with('}') {
        let var_name = &value[2..value.len() - 1];
        if let Some((var, default)) = var_name.split_once(":-") {
            return Ok(std::env::var(var).unwrap_or_else(|_| default.to_string()));
        }
        return std::env::var(var_name)
            .with_context(|| format!("environment variable {var_name} not set"));
    }

    Ok(value.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_derive_fernet_key_deterministic() {
        let key1 = derive_fernet_key("test-machine-id", b"0123456789abcdef");
        let key2 = derive_fernet_key("test-machine-id", b"0123456789abcdef");
        assert_eq!(key1, key2);
        assert!(!key1.is_empty());
    }

    #[test]
    fn test_derive_fernet_key_different_salt() {
        let key1 = derive_fernet_key("test-machine-id", b"0123456789abcdef");
        let key2 = derive_fernet_key("test-machine-id", b"fedcba9876543210");
        assert_ne!(key1, key2);
    }

    #[test]
    fn test_decrypt_roundtrip() {
        let machine_id = "test-machine-42";
        let salt = b"abcdef0123456789";
        let fernet_key = derive_fernet_key(machine_id, salt);

        // Encrypt with the same key
        let fernet = fernet::Fernet::new(&fernet_key).unwrap();
        let token = fernet.encrypt(b"my-secret-password");

        // Decrypt
        let decrypted = decrypt_fernet(&fernet_key, &token).unwrap();
        assert_eq!(decrypted, "my-secret-password");
    }

    #[test]
    fn test_resolve_config_value_passthrough() {
        let result =
            resolve_config_value("http://localhost:8474", Path::new("/nonexistent")).unwrap();
        assert_eq!(result, "http://localhost:8474");
    }

    #[test]
    fn test_resolve_config_value_env_var() {
        unsafe { std::env::set_var("GCODE_TEST_VAR_123", "hello") };
        let result =
            resolve_config_value("${GCODE_TEST_VAR_123}", Path::new("/nonexistent")).unwrap();
        assert_eq!(result, "hello");
        unsafe { std::env::remove_var("GCODE_TEST_VAR_123") };
    }

    #[test]
    fn test_resolve_config_value_env_default() {
        unsafe { std::env::remove_var("GCODE_NONEXISTENT_VAR_999") };
        let result = resolve_config_value(
            "${GCODE_NONEXISTENT_VAR_999:-fallback}",
            Path::new("/nonexistent"),
        )
        .unwrap();
        assert_eq!(result, "fallback");
    }
}
