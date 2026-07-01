//! Fernet decryption of Gobby's shared SecretStore envelope.
//!
//! This matches the Python SecretStore envelope:
//! 1. Read `secret_key_material.default`.
//! 2. Load the KEK from `~/.gobby/.secret_kek` or `GOBBY_SECRET_KEK_PASSPHRASE`.
//! 3. Unwrap the Fernet DEK from `secret_key_material.wrapped_dek`.
//! 4. Decrypt `secrets.encrypted_value` with the DEK.

use anyhow::{Context as _, bail};
use base64::Engine as _;
use base64::engine::general_purpose::URL_SAFE;
use postgres::{Client, Row};
use scrypt::{Params as ScryptParams, scrypt};

const SECRET_KEY_ID: &str = "default";
const SECRET_NAME_MAX_LEN: usize = 256;
const POSTURE_KEY_FILE: &str = "key_file";
const POSTURE_SCRYPT_PASSPHRASE: &str = "scrypt_passphrase";
const SECRET_KEK_PASSPHRASE_ENV: &str = "GOBBY_SECRET_KEK_PASSPHRASE";
const SCRYPT_N: i32 = 1 << 14;
const SCRYPT_R: i32 = 8;
const SCRYPT_P: i32 = 1;
const FERNET_KEY_LEN: usize = 32;

#[derive(Debug)]
struct SecretKeyMaterial {
    wrapped_dek: String,
    kek_posture: String,
    kek_salt: Option<String>,
    kek_kdf_n: Option<i32>,
    kek_kdf_r: Option<i32>,
    kek_kdf_p: Option<i32>,
}

impl SecretKeyMaterial {
    fn from_row(row: Row) -> anyhow::Result<Self> {
        Ok(Self {
            wrapped_dek: row.try_get("wrapped_dek")?,
            kek_posture: row.try_get("kek_posture")?,
            kek_salt: row.try_get("kek_salt")?,
            kek_kdf_n: row.try_get("kek_kdf_n")?,
            kek_kdf_r: row.try_get("kek_kdf_r")?,
            kek_kdf_p: row.try_get("kek_kdf_p")?,
        })
    }
}

fn load_key_material(conn: &mut Client) -> anyhow::Result<SecretKeyMaterial> {
    let row = conn
        .query_one(
            "SELECT wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p \
             FROM secret_key_material WHERE id = $1",
            &[&SECRET_KEY_ID],
        )
        .context("secret key material not found; run the Gobby daemon secret migration")?;
    SecretKeyMaterial::from_row(row)
}

fn normalize_posture(posture: &str) -> anyhow::Result<String> {
    let normalized = posture.trim().to_ascii_lowercase().replace('-', "_");
    match normalized.as_str() {
        POSTURE_KEY_FILE | POSTURE_SCRYPT_PASSPHRASE => Ok(normalized),
        _ => bail!(
            "invalid secret KEK posture '{posture}'; expected '{POSTURE_KEY_FILE}' or '{POSTURE_SCRYPT_PASSPHRASE}'"
        ),
    }
}

fn validate_fernet_key(key: &str, label: &str) -> anyhow::Result<()> {
    fernet::Fernet::new(key)
        .map(|_| ())
        .ok_or_else(|| anyhow::anyhow!("invalid {label} Fernet key"))
}

fn decrypt_fernet_bytes(key: &str, token: &str) -> anyhow::Result<Vec<u8>> {
    let fernet = fernet::Fernet::new(key).ok_or_else(|| anyhow::anyhow!("invalid Fernet key"))?;
    let plaintext = fernet
        .decrypt(token)
        .map_err(|_| anyhow::anyhow!("Fernet decryption failed"))?;
    Ok(plaintext)
}

fn decrypt_fernet_string(key: &str, token: &str) -> anyhow::Result<String> {
    let plaintext = decrypt_fernet_bytes(key, token)
        .context("failed to decrypt secret value with envelope DEK")?;
    String::from_utf8(plaintext).context("decrypted secret is not valid UTF-8")
}

fn load_key_file_kek() -> anyhow::Result<String> {
    let gobby_dir = crate::gobby_home()?;
    let path = gobby_dir.join(".secret_kek");
    let key = std::fs::read_to_string(&path)
        .with_context(|| format!("failed to read secret KEK file {}", path.display()))?;
    let key = key.trim().to_string();
    if key.is_empty() {
        bail!("secret KEK file is empty: {}", path.display());
    }
    validate_fernet_key(&key, "secret KEK file")?;
    Ok(key)
}

fn positive_scrypt_param(name: &str, value: i32) -> anyhow::Result<u32> {
    if value <= 0 {
        bail!("scrypt parameter {name} must be positive");
    }
    u32::try_from(value).with_context(|| format!("scrypt parameter {name} is out of range"))
}

fn derive_scrypt_fernet_key(
    passphrase: &str,
    salt: &[u8],
    n: i32,
    r: i32,
    p: i32,
) -> anyhow::Result<String> {
    let n = positive_scrypt_param("n", n)?;
    let r = positive_scrypt_param("r", r)?;
    let p = positive_scrypt_param("p", p)?;
    if n < 2 || !n.is_power_of_two() {
        bail!("scrypt parameter n must be a power of two greater than 1");
    }
    let log_n = u8::try_from(n.trailing_zeros()).context("scrypt parameter n is too large")?;
    let params =
        ScryptParams::new(log_n, r, p, FERNET_KEY_LEN).context("invalid scrypt KEK parameters")?;
    let mut key_bytes = [0u8; FERNET_KEY_LEN];
    scrypt(passphrase.as_bytes(), salt, &params, &mut key_bytes)
        .context("failed to derive scrypt KEK")?;
    Ok(URL_SAFE.encode(key_bytes))
}

fn scrypt_kek(material: &SecretKeyMaterial) -> anyhow::Result<String> {
    let passphrase = std::env::var(SECRET_KEK_PASSPHRASE_ENV)
        .ok()
        .filter(|value| !value.is_empty())
        .with_context(|| {
            format!("Secret KEK passphrase posture requires {SECRET_KEK_PASSPHRASE_ENV}")
        })?;
    let salt_text = material
        .kek_salt
        .as_deref()
        .context("scrypt_passphrase KEK material is missing kek_salt")?;
    let salt = URL_SAFE
        .decode(salt_text)
        .context("scrypt_passphrase KEK material has invalid kek_salt")?;
    let n = material
        .kek_kdf_n
        .filter(|value| *value != 0)
        .unwrap_or(SCRYPT_N);
    let r = material
        .kek_kdf_r
        .filter(|value| *value != 0)
        .unwrap_or(SCRYPT_R);
    let p = material
        .kek_kdf_p
        .filter(|value| *value != 0)
        .unwrap_or(SCRYPT_P);
    derive_scrypt_fernet_key(&passphrase, &salt, n, r, p)
}

fn unwrap_dek(material: &SecretKeyMaterial) -> anyhow::Result<String> {
    let posture = normalize_posture(&material.kek_posture)?;
    let kek = match posture.as_str() {
        POSTURE_KEY_FILE => load_key_file_kek()?,
        POSTURE_SCRYPT_PASSPHRASE => scrypt_kek(material)?,
        _ => unreachable!("normalize_posture validates supported postures"),
    };
    let dek = decrypt_fernet_bytes(&kek, &material.wrapped_dek)
        .context("Secret DEK cannot be unwrapped with configured KEK")?;
    let dek = String::from_utf8(dek).context("unwrapped secret DEK is not valid UTF-8")?;
    validate_fernet_key(&dek, "secret DEK")?;
    Ok(dek)
}

fn decrypt_secret_value(
    material: &SecretKeyMaterial,
    encrypted_value: &str,
) -> anyhow::Result<String> {
    let dek = unwrap_dek(material)?;
    decrypt_fernet_string(&dek, encrypted_value)
}

/// Resolve a secret by name from the PostgreSQL hub `secrets` table.
pub fn resolve_secret(conn: &mut Client, secret_name: &str) -> anyhow::Result<String> {
    let name = secret_name.trim();
    validate_secret_name(name)?;

    // Secrets are resolved case-insensitively; case-only duplicates collide on this key.
    let name = name.to_lowercase();
    let row = conn
        .query_one(
            "SELECT encrypted_value FROM secrets WHERE name = $1",
            &[&name],
        )
        .with_context(|| format!("secret '{name}' not found in secrets table"))?;
    let encrypted: String = row.try_get("encrypted_value")?;
    let material = load_key_material(conn)?;

    decrypt_secret_value(&material, &encrypted)
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
            .take_while(|byte| secret_name_byte(*byte))
            .count();
        if name_len == 0 {
            bail!("empty secret reference in `{value}`");
        }
        let name = &after_prefix[..name_len];
        validate_secret_name(name)?;
        validate_secret_reference_boundary(value, name, &after_prefix[name_len..])?;
        output.push_str(&resolve(name)?);
        rest = &after_prefix[name_len..];
    }
    output.push_str(rest);

    if let Some(resolved) = crate::config::resolve_env_pattern(&output)? {
        return Ok(resolved);
    }

    bail!("unresolved environment pattern in config value")
}

fn validate_secret_name(name: &str) -> anyhow::Result<()> {
    if name.is_empty() {
        bail!("secret name must not be empty");
    }
    if name.len() > SECRET_NAME_MAX_LEN {
        bail!("secret name exceeds {SECRET_NAME_MAX_LEN} characters");
    }
    if !name.bytes().all(secret_name_byte) {
        bail!("secret name contains unsupported characters");
    }
    Ok(())
}

fn validate_secret_reference_boundary(
    value: &str,
    name: &str,
    remainder: &str,
) -> anyhow::Result<()> {
    if remainder.is_empty() || remainder.starts_with("$secret:") || remainder.starts_with("${") {
        return Ok(());
    }
    let Some(next) = remainder.chars().next() else {
        return Ok(());
    };
    if secret_boundary_char(next) {
        return Ok(());
    }
    bail!("invalid secret reference boundary after `{name}` in `{value}`");
}

fn secret_name_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.')
}

fn secret_boundary_char(value: char) -> bool {
    value.is_ascii_whitespace()
        || matches!(
            value,
            '/' | '\\'
                | '@'
                | ':'
                | '?'
                | '&'
                | '='
                | '#'
                | '%'
                | ','
                | ';'
                | '+'
                | '!'
                | '~'
                | '('
                | ')'
                | '['
                | ']'
                | '{'
                | '}'
                | '<'
                | '>'
                | '|'
                | '"'
                | '\''
        )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::OsString;
    use std::path::Path;
    use std::sync::MutexGuard;

    const PYTHON_KEY_FILE_KEK: &str = "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=";
    const PYTHON_WRAPPED_DEK_KEY_FILE: &str = "gAAAAABJlgLSAAAAAAAAAAAAAAAAAAAAAC9b8RrAMGFhE3wxSkKBFwLhxkJrL8D_3Yz5NsOSpTGXwSHaKKYAAv7LiH3nK3KaCIs4vSSbctmyv763hbGrXx2-xFonOlEINxCtTMFY15-9";
    const PYTHON_SECRET_TOKEN: &str = "gAAAAABJlgLTAQEBAQEBAQEBAQEBAQEBARaUwRex_DmfFRE7ZvGLxsGAlqNaJoaqTlXAUOJNq7X7jeLdyzVD6zFZQceOWjduRQ==";
    const PYTHON_PASSPHRASE: &str = "correct horse battery staple";
    const PYTHON_SCRYPT_SALT: &str = "MDEyMzQ1Njc4OWFiY2RlZg==";
    const PYTHON_SCRYPT_KEK: &str = "tjK03tRvEjqCcPwmgtddMkgjlXrk8U_b9rIvfeBMKCc=";
    const PYTHON_WRAPPED_DEK_SCRYPT: &str = "gAAAAABJlgLUAgICAgICAgICAgICAgICAl0B0DJd9T7R9n92mSisMTeX4Vj42VnDpzkOXFJUDd6VWoPnzdjUwgxl9kPwERHWhTnsvnPDI9uxHC6T8MIDrcf0JdRVhu-OA3FeBELxYx4g";

    struct EnvGuard {
        _lock: MutexGuard<'static, ()>,
        previous_gobby_home: Option<OsString>,
        previous_passphrase: Option<OsString>,
    }

    impl EnvGuard {
        fn new() -> Self {
            let guard = Self {
                _lock: crate::config::TEST_ENV_LOCK
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner()),
                previous_gobby_home: std::env::var_os("GOBBY_HOME"),
                previous_passphrase: std::env::var_os(SECRET_KEK_PASSPHRASE_ENV),
            };
            guard.clear();
            guard
        }

        fn clear(&self) {
            // SAFETY: EnvGuard holds TEST_ENV_LOCK while mutating process environment.
            unsafe {
                std::env::remove_var("GOBBY_HOME");
                std::env::remove_var(SECRET_KEK_PASSPHRASE_ENV);
            }
        }

        fn set_gobby_home(&self, path: &Path) {
            // SAFETY: EnvGuard holds TEST_ENV_LOCK while mutating process environment.
            unsafe { std::env::set_var("GOBBY_HOME", path) };
        }

        fn set_passphrase(&self, value: &str) {
            // SAFETY: EnvGuard holds TEST_ENV_LOCK while mutating process environment.
            unsafe { std::env::set_var(SECRET_KEK_PASSPHRASE_ENV, value) };
        }
    }

    impl Drop for EnvGuard {
        fn drop(&mut self) {
            // SAFETY: EnvGuard holds TEST_ENV_LOCK while restoring process environment.
            unsafe {
                match &self.previous_gobby_home {
                    Some(value) => std::env::set_var("GOBBY_HOME", value),
                    None => std::env::remove_var("GOBBY_HOME"),
                }
                match &self.previous_passphrase {
                    Some(value) => std::env::set_var(SECRET_KEK_PASSPHRASE_ENV, value),
                    None => std::env::remove_var(SECRET_KEK_PASSPHRASE_ENV),
                }
            }
        }
    }

    fn key_file_material() -> SecretKeyMaterial {
        SecretKeyMaterial {
            wrapped_dek: PYTHON_WRAPPED_DEK_KEY_FILE.to_string(),
            kek_posture: POSTURE_KEY_FILE.to_string(),
            kek_salt: None,
            kek_kdf_n: None,
            kek_kdf_r: None,
            kek_kdf_p: None,
        }
    }

    fn scrypt_material() -> SecretKeyMaterial {
        SecretKeyMaterial {
            wrapped_dek: PYTHON_WRAPPED_DEK_SCRYPT.to_string(),
            kek_posture: POSTURE_SCRYPT_PASSPHRASE.to_string(),
            kek_salt: Some(PYTHON_SCRYPT_SALT.to_string()),
            kek_kdf_n: Some(SCRYPT_N),
            kek_kdf_r: Some(SCRYPT_R),
            kek_kdf_p: Some(SCRYPT_P),
        }
    }

    #[test]
    fn python_key_file_fixture_decrypts_wrapped_dek_and_secret_value() {
        let guard = EnvGuard::new();
        let home = tempfile::tempdir().expect("temp home");
        guard.set_gobby_home(home.path());
        std::fs::write(
            home.path().join(".secret_kek"),
            format!("{PYTHON_KEY_FILE_KEK}\n"),
        )
        .expect("write secret KEK file");

        let decrypted = decrypt_secret_value(&key_file_material(), PYTHON_SECRET_TOKEN)
            .expect("python key-file fixture decrypts");

        assert_eq!(decrypted, "postgres-secret");
    }

    #[test]
    fn python_scrypt_fixture_decrypts_wrapped_dek_and_secret_value() {
        let guard = EnvGuard::new();
        guard.set_passphrase(PYTHON_PASSPHRASE);
        let salt = URL_SAFE
            .decode(PYTHON_SCRYPT_SALT)
            .expect("fixture salt decodes");

        let derived =
            derive_scrypt_fernet_key(PYTHON_PASSPHRASE, &salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
                .expect("derive scrypt KEK");
        let decrypted = decrypt_secret_value(&scrypt_material(), PYTHON_SECRET_TOKEN)
            .expect("python scrypt fixture decrypts");

        assert_eq!(derived, PYTHON_SCRYPT_KEK);
        assert_eq!(decrypted, "postgres-secret");
    }

    #[test]
    fn scrypt_passphrase_requires_env() {
        let _guard = EnvGuard::new();

        let error = decrypt_secret_value(&scrypt_material(), PYTHON_SECRET_TOKEN)
            .expect_err("missing passphrase is rejected");

        assert!(error.to_string().contains(SECRET_KEK_PASSPHRASE_ENV));
    }

    #[test]
    fn key_file_kek_requires_existing_file() {
        let guard = EnvGuard::new();
        let home = tempfile::tempdir().expect("temp home");
        guard.set_gobby_home(home.path());

        let error = decrypt_secret_value(&key_file_material(), PYTHON_SECRET_TOKEN)
            .expect_err("missing key file is rejected");

        assert!(error.to_string().contains("failed to read secret KEK file"));
    }

    #[test]
    fn posture_normalization_matches_python_contract() {
        assert_eq!(
            normalize_posture(" SCRYPT-PASSPHRASE ").expect("valid posture"),
            POSTURE_SCRYPT_PASSPHRASE
        );

        let error = normalize_posture("unsupported").expect_err("invalid posture");
        assert!(error.to_string().contains("invalid secret KEK posture"));
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
    fn resolve_config_value_expands_leading_whole_value_secret() {
        // The canonical config_store pattern: the entire value is a single
        // `$secret:NAME` reference (e.g. databases.falkordb.password).
        let resolved = resolve_config_value_with("$secret:DB_PASS", test_secret_resolver)
            .expect("leading whole-value secret resolves");

        assert_eq!(resolved, "secret-db-pass");
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
    #[serial_test::serial]
    fn resolve_config_value_expands_secret_then_environment() {
        // SAFETY: serial_test keeps this process-wide environment mutation isolated.
        unsafe { std::env::set_var("GOBBY_SECRET_TEST_HOST", "example.test") };

        let resolved = resolve_config_value_with(
            "https://${GOBBY_SECRET_TEST_HOST}/token/$secret:API_KEY",
            test_secret_resolver,
        )
        .expect("config value resolves");

        // SAFETY: serial_test keeps this process-wide environment mutation isolated.
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
    fn resolve_config_value_rejects_invalid_secret_names() {
        let invalid_char =
            resolve_config_value_with("$secret:API_KEY/ok/$secret:BAD*NAME", test_secret_resolver)
                .expect_err("invalid character");
        assert!(
            invalid_char
                .to_string()
                .contains("invalid secret reference boundary")
        );

        let overlong = format!("$secret:{}", "A".repeat(SECRET_NAME_MAX_LEN + 1));
        let error =
            resolve_config_value_with(&overlong, test_secret_resolver).expect_err("overlong name");
        assert!(error.to_string().contains("secret name exceeds"));
    }

    #[test]
    fn validate_secret_name_rejects_empty_and_unsupported_names() {
        assert!(validate_secret_name("").is_err());
        assert!(validate_secret_name("bad/name").is_err());
        assert!(validate_secret_name("bad name").is_err());
        assert!(validate_secret_name("good.NAME-1").is_ok());
    }

    #[test]
    fn resolve_config_value_rejects_unresolved_environment() {
        let error = resolve_config_value_with("${GOBBY_SECRET_TEST_MISSING}", test_secret_resolver)
            .expect_err("error");

        assert!(error.to_string().contains("unresolved environment pattern"));
    }

    #[test]
    fn unresolved_environment_omits_expanded_secret_output() {
        let error = resolve_config_value_with(
            "https://$secret:USER@${GOBBY_SECRET_TEST_MISSING}",
            test_secret_resolver,
        )
        .expect_err("error");
        let message = error.to_string();

        assert!(message.contains("unresolved environment pattern in config value"));
        assert!(!message.contains("secret-user"));
        assert!(!message.contains("$secret:USER"));
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
