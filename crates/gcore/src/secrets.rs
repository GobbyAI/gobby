//! Fernet decryption of Gobby's SecretStore.
//!
//! This matches the Python SecretStore chain:
//! 1. Read `~/.gobby/machine_id`.
//! 2. Read `~/.gobby/.secret_salt`.
//! 3. Derive a Fernet key with PBKDF2-HMAC-SHA256.
//! 4. Decrypt `secrets.encrypted_value`.

use anyhow::{Context as _, bail};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use pbkdf2::pbkdf2_hmac;
use postgres::Client;
use sha2::Sha256;

fn derive_fernet_key(machine_id: &str, salt: &[u8]) -> String {
    let mut key_bytes = [0u8; 32];
    pbkdf2_hmac::<Sha256>(machine_id.as_bytes(), salt, 600_000, &mut key_bytes);
    URL_SAFE.encode(key_bytes)
}

fn decrypt_fernet(key: &str, token: &str) -> anyhow::Result<String> {
    let fernet = fernet::Fernet::new(key).ok_or_else(|| anyhow::anyhow!("invalid Fernet key"))?;
    let plaintext = fernet
        .decrypt(token)
        .map_err(|_| anyhow::anyhow!("Fernet decryption failed (machine ID may have changed)"))?;
    String::from_utf8(plaintext).context("decrypted secret is not valid UTF-8")
}

/// Resolve a secret by name from the PostgreSQL hub `secrets` table.
pub fn resolve_secret(conn: &mut Client, secret_name: &str) -> anyhow::Result<String> {
    let gobby_dir = crate::gobby_home()?;
    let machine_id_path = gobby_dir.join("machine_id");
    let machine_id = std::fs::read_to_string(&machine_id_path)
        .with_context(|| format!("failed to read {}", machine_id_path.display()))?
        .trim()
        .to_string();
    if machine_id.is_empty() {
        bail!("machine_id file is empty");
    }

    let salt_path = gobby_dir.join(".secret_salt");
    let salt = std::fs::read(&salt_path)
        .with_context(|| format!("failed to read {}", salt_path.display()))?;
    let fernet_key = derive_fernet_key(&machine_id, &salt);

    let name = secret_name.trim().to_lowercase();
    let row = conn
        .query_one(
            "SELECT encrypted_value FROM secrets WHERE name = $1",
            &[&name],
        )
        .with_context(|| format!("secret '{name}' not found in secrets table"))?;
    let encrypted: String = row.try_get("encrypted_value")?;

    decrypt_fernet(&fernet_key, &encrypted)
}

/// Resolve `$secret:NAME` and `${VAR}` patterns in a config value.
pub fn resolve_config_value(value: &str, conn: &mut Client) -> anyhow::Result<String> {
    if !value.contains("$secret:") && !value.contains("${") {
        return Ok(value.to_string());
    }

    if let Some(name) = value.strip_prefix("$secret:") {
        return resolve_secret(conn, name);
    }

    if let Some(resolved) = crate::config::resolve_env_pattern(value)? {
        return Ok(resolved);
    }

    Ok(value.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn derive_fernet_key_is_deterministic() {
        let key1 = derive_fernet_key("test-machine-id", b"0123456789abcdef");
        let key2 = derive_fernet_key("test-machine-id", b"0123456789abcdef");

        assert_eq!(key1, key2);
        assert!(!key1.is_empty());
    }

    #[test]
    fn derive_fernet_key_uses_salt() {
        let key1 = derive_fernet_key("test-machine-id", b"0123456789abcdef");
        let key2 = derive_fernet_key("test-machine-id", b"fedcba9876543210");

        assert_ne!(key1, key2);
    }

    #[test]
    fn decrypt_fernet_round_trips() {
        let fernet_key = derive_fernet_key("test-machine-42", b"abcdef0123456789");
        let fernet = fernet::Fernet::new(&fernet_key).expect("valid Fernet key");
        let token = fernet.encrypt(b"my-secret-password");

        let decrypted = decrypt_fernet(&fernet_key, &token).expect("decrypts");

        assert_eq!(decrypted, "my-secret-password");
    }
}
