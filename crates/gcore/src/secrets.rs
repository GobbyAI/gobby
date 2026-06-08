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
    resolve_config_value_with(value, |name| resolve_secret(conn, name))
}

fn resolve_config_value_with(
    value: &str,
    mut resolve: impl FnMut(&str) -> anyhow::Result<String>,
) -> anyhow::Result<String> {
    if !value.contains("$secret:") && !value.contains("${") {
        return Ok(value.to_string());
    }

    let mut output = String::with_capacity(value.len());
    let mut rest = value;
    while let Some(start) = rest.find("$secret:") {
        output.push_str(&rest[..start]);
        let after_prefix = &rest[start + "$secret:".len()..];
        let name_len = after_prefix
            .bytes()
            .take_while(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'_' | b'-' | b'.'))
            .count();
        if name_len == 0 {
            bail!("empty secret reference in `{value}`");
        }
        let name = &after_prefix[..name_len];
        output.push_str(&resolve(name)?);
        rest = &after_prefix[name_len..];
    }
    output.push_str(rest);

    if let Some(resolved) = crate::config::resolve_env_pattern(&output)? {
        return Ok(resolved);
    }

    bail!("unresolved environment pattern in `{value}`")
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

    #[test]
    fn resolve_config_value_expands_embedded_secret() {
        let resolved = resolve_config_value_with(
            "postgres://user:$secret:DB_PASS@localhost/db",
            test_secret_resolver,
        )
        .expect("config value resolves");

        assert_eq!(resolved, "postgres://user:secret-db-pass@localhost/db");
    }

    #[test]
    fn resolve_config_value_expands_multiple_secrets() {
        let resolved = resolve_config_value_with(
            "$secret:USER:$secret:PASSWORD@localhost",
            test_secret_resolver,
        )
        .expect("config value resolves");

        assert_eq!(resolved, "secret-user:secret-password@localhost");
    }

    #[test]
    fn resolve_config_value_expands_secret_then_environment() {
        static ENV_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
        let _guard = ENV_LOCK
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        unsafe { std::env::set_var("GOBBY_SECRET_TEST_HOST", "example.test") };

        let resolved = resolve_config_value_with(
            "https://${GOBBY_SECRET_TEST_HOST}/token/$secret:API_KEY",
            test_secret_resolver,
        )
        .expect("config value resolves");

        unsafe { std::env::remove_var("GOBBY_SECRET_TEST_HOST") };
        assert_eq!(resolved, "https://example.test/token/secret-api-key");
    }

    #[test]
    fn resolve_config_value_rejects_unresolved_secret() {
        let error =
            resolve_config_value_with("$secret:MISSING", test_secret_resolver).expect_err("error");

        assert!(error.to_string().contains("missing test secret MISSING"));
    }

    #[test]
    fn resolve_config_value_rejects_unresolved_environment() {
        let error = resolve_config_value_with("${GOBBY_SECRET_TEST_MISSING}", test_secret_resolver)
            .expect_err("error");

        assert!(error.to_string().contains("unresolved environment pattern"));
    }

    #[test]
    fn resolve_config_value_plain_value_uses_fast_path() {
        let resolved = resolve_config_value_with("plain-value", |_| {
            bail!("secret resolver should not be called")
        })
        .expect("plain value resolves");

        assert_eq!(resolved, "plain-value");
    }

    fn test_secret_resolver(name: &str) -> anyhow::Result<String> {
        match name {
            "API_KEY" => Ok("secret-api-key".to_string()),
            "DB_PASS" => Ok("secret-db-pass".to_string()),
            "PASSWORD" => Ok("secret-password".to_string()),
            "USER" => Ok("secret-user".to_string()),
            _ => bail!("missing test secret {name}"),
        }
    }
}
