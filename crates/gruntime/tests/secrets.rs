//! Secret envelope and credential sealing against Python's committed vector and
//! the isolated PostgreSQL test hub.

use std::fs;
use std::os::unix::fs::PermissionsExt;
use std::panic::AssertUnwindSafe;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, Result, bail, ensure};
use base64::Engine as _;
use base64::engine::general_purpose::STANDARD;
use futures_util::FutureExt;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_runtime::secrets::{SecretError, SecretStore, publish_private_file};
use serde_json::Value;
use tempfile::TempDir;
use tokio_postgres::{Client, NoTls};
use uuid::Uuid;

const VECTOR: &str = include_str!("fixtures/secret_seal_vector.json");
const GLOBAL_PROJECT_ID: &str = "00000000-0000-0000-0000-000000000002";
const DAEMON_ROLE: &str =
    "SELECT pg_advisory_xact_lock(hashtextextended('gobby:agent-authorization:v1', 0));
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gobby_daemon_runtime') THEN
        CREATE ROLE gobby_daemon_runtime;
    END IF;
    ALTER ROLE gobby_daemon_runtime
        NOLOGIN NOSUPERUSER INHERIT NOCREATEROLE NOCREATEDB
        NOREPLICATION NOBYPASSRLS;
END
$roles$;";

struct TestDb {
    admin: Client,
    schema: String,
    url: String,
}

impl TestDb {
    fn table(&self, name: &str) -> String {
        format!("{}.{name}", self.schema)
    }

    async fn count(&self, table: &str) -> Result<i64> {
        let row = self
            .admin
            .query_one(&format!("SELECT count(*) FROM {}", self.table(table)), &[])
            .await?;
        Ok(row.try_get(0)?)
    }

    async fn seed_key_file_row(&self, wrapped_dek: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {} (id, wrapped_dek, kek_posture) VALUES ('default', $1, 'key_file')",
                    self.table("secret_key_material")
                ),
                &[&wrapped_dek],
            )
            .await?;
        Ok(())
    }

    async fn seed_scrypt_row(&self, vector: &Value) -> Result<()> {
        let scrypt = &vector["scrypt"];
        let n = i32::try_from(text_i64(scrypt, "kek_kdf_n")?)?;
        let r = i32::try_from(text_i64(scrypt, "kek_kdf_r")?)?;
        let p = i32::try_from(text_i64(scrypt, "kek_kdf_p")?)?;
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {} (id, wrapped_dek, kek_posture, kek_salt, kek_kdf_n, kek_kdf_r, kek_kdf_p) \
                     VALUES ('default', $1, 'scrypt_passphrase', $2, $3, $4, $5)",
                    self.table("secret_key_material")
                ),
                &[&text(scrypt, "wrapped_dek")?, &text(scrypt, "kek_salt")?, &n, &r, &p],
            )
            .await?;
        Ok(())
    }

    async fn seed_secret(&self, name: &str, encrypted_value: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {} (id, name, encrypted_value, project_id) \
                     VALUES (gen_random_uuid(), $1, $2, '{GLOBAL_PROJECT_ID}'::uuid)",
                    self.table("secrets")
                ),
                &[&name, &encrypted_value],
            )
            .await?;
        Ok(())
    }
}

fn schema_sql(schema: &str) -> String {
    format!(
        "{DAEMON_ROLE}
CREATE SCHEMA {schema};
GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime;
CREATE TABLE {schema}.secret_key_material (
    id text NOT NULL PRIMARY KEY,
    wrapped_dek text NOT NULL,
    kek_posture text NOT NULL,
    kek_salt text,
    kek_kdf_n integer,
    kek_kdf_r integer,
    kek_kdf_p integer,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE {schema}.secrets (
    id uuid NOT NULL PRIMARY KEY,
    name text NOT NULL,
    encrypted_value text NOT NULL,
    category text DEFAULT 'general'::text,
    description text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    project_id uuid DEFAULT '{GLOBAL_PROJECT_ID}'::uuid NOT NULL,
    UNIQUE (name, project_id)
);
GRANT SELECT, INSERT, UPDATE, DELETE ON {schema}.secret_key_material, {schema}.secrets TO gobby_daemon_runtime;"
    )
}

async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        bail!("DATABASE_URL must name the isolated test hub for secret envelope tests");
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "secret envelope tests require the protected isolated test hub"
    );
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    let schema = format!("test_rust_secrets_{}", Uuid::new_v4().simple());
    admin.batch_execute(&schema_sql(&schema)).await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = TestDb {
        admin,
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        schema,
    };
    let outcome = AssertUnwindSafe(case(&db)).catch_unwind().await;
    let cleanup = db
        .admin
        .batch_execute(&format!("DROP SCHEMA {} CASCADE", db.schema))
        .await;
    drop(db);
    driver.abort();
    let _ = driver.await;
    match outcome {
        Ok(result) => {
            cleanup?;
            result
        }
        Err(panic) => {
            if let Err(error) = cleanup {
                eprintln!("secret envelope test cleanup failed: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

fn pool(url: &str) -> Result<Arc<Pool>> {
    Ok(Arc::new(Pool::build(
        url,
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-runtime-secrets-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?))
}

fn vector() -> Result<Value> {
    Ok(serde_json::from_str(VECTOR)?)
}

fn text<'a>(value: &'a Value, name: &str) -> Result<&'a str> {
    value[name]
        .as_str()
        .with_context(|| format!("vector field `{name}` must be a string"))
}

fn text_i64(value: &Value, name: &str) -> Result<i64> {
    value[name]
        .as_i64()
        .with_context(|| format!("vector field `{name}` must be an integer"))
}

fn write_kek(home: &Path, kek: &str) -> Result<()> {
    let path = home.join(".secret_kek");
    fs::write(&path, kek)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    Ok(())
}

fn count_temp_files(home: &Path) -> Result<usize> {
    let mut count = 0;
    for entry in fs::read_dir(home)? {
        let name = entry?.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(".secret_kek.") && name.ends_with(".tmp") {
            count += 1;
        }
    }
    Ok(count)
}

#[tokio::test]
async fn opens_python_sealed_vector() -> Result<()> {
    let vector = vector()?;
    database_case(async |db| {
        let home = TempDir::new()?;
        write_kek(home.path(), text(&vector, "kek")?)?;
        db.seed_key_file_row(text(&vector, "wrapped_dek")?).await?;
        db.seed_secret("fixture", text(&vector, "encrypted_secret")?)
            .await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf());

        assert_eq!(
            store.secret("fixture").await?.as_deref(),
            Some(text(&vector, "secret_plaintext")?)
        );
        let opened = store
            .open_sealed(
                text(&vector, "python_token")?,
                text(&vector, "python_aad")?.as_bytes(),
            )
            .await?;
        assert_eq!(opened, text(&vector, "python_plaintext")?.as_bytes());
        Ok(())
    })
    .await
}

#[tokio::test]
async fn unwraps_both_postures() -> Result<()> {
    let vector = vector()?;
    database_case(async |db| {
        let home = TempDir::new()?;
        write_kek(home.path(), text(&vector, "kek")?)?;
        db.seed_key_file_row(text(&vector, "wrapped_dek")?).await?;
        db.seed_secret("fixture", text(&vector, "encrypted_secret")?)
            .await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf());

        store.ensure_ready().await?;
        assert_eq!(
            store.secret("fixture").await?.as_deref(),
            Some(text(&vector, "secret_plaintext")?)
        );
        Ok(())
    })
    .await?;

    database_case(async |db| {
        let home = TempDir::new()?;
        db.seed_scrypt_row(&vector).await?;
        db.seed_secret("fixture", text(&vector, "encrypted_secret")?)
            .await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf())
            .with_kek_passphrase(text(&vector["scrypt"], "passphrase")?);

        store.ensure_ready().await?;
        assert_eq!(
            store.secret("fixture").await?.as_deref(),
            Some(text(&vector, "secret_plaintext")?)
        );
        ensure!(
            !home.path().join(".secret_kek").exists(),
            "the scrypt posture must not create a key file"
        );
        Ok(())
    })
    .await?;

    database_case(async |db| {
        let home = TempDir::new()?;
        db.seed_scrypt_row(&vector).await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf())
            .with_kek_passphrase("wrong-passphrase");

        assert!(matches!(
            store.ensure_ready().await,
            Err(SecretError::Unavailable)
        ));
        Ok(())
    })
    .await
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_envelope_initialization_converges() -> Result<()> {
    database_case(async |db| {
        let home = TempDir::new()?;
        let leftover = home.path().join(".secret_kek.4242.0123456789abcdef.tmp");
        fs::write(&leftover, b"partial")?;
        let pool = pool(&db.url)?;
        let first = SecretStore::new(Arc::clone(&pool), home.path().to_path_buf());
        let second = SecretStore::new(pool, home.path().to_path_buf());

        let (a, b) = tokio::join!(first.ensure_ready(), second.ensure_ready());
        a?;
        b?;

        ensure!(
            db.count("secret_key_material").await? == 1,
            "exactly one key-material row must win the race"
        );
        let kek = home.path().join(".secret_kek");
        ensure!(
            fs::read(&kek)? != b"partial",
            "the reader must not adopt the planted partial temp file"
        );
        ensure!(
            fs::metadata(&kek)?.permissions().mode() & 0o777 == 0o600,
            "the KEK file must be 0600"
        );
        let token = first.seal(b"converged", b"aad").await?;
        assert_eq!(second.open_sealed(&token, b"aad").await?, b"converged");
        let token = second.seal(b"both ways", b"aad").await?;
        assert_eq!(first.open_sealed(&token, b"aad").await?, b"both ways");
        ensure!(
            count_temp_files(home.path())? == 1,
            "publishers must remove their own temp files"
        );

        let existing = fs::read(&kek)?;
        ensure!(
            !publish_private_file(&kek, b"replacement")?,
            "a racing publish must lose to the existing KEK"
        );
        ensure!(
            fs::read(&kek)? == existing,
            "the winning KEK must not be replaced"
        );
        ensure!(
            count_temp_files(home.path())? == 1,
            "a losing publish must remove its temp file"
        );

        let locked = home.path().join("locked");
        fs::create_dir(&locked)?;
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o500))?;
        let outcome = publish_private_file(&locked.join(".secret_kek"), b"new");
        fs::set_permissions(&locked, fs::Permissions::from_mode(0o700))?;
        ensure!(
            outcome.is_err(),
            "an unwritable directory must fail the publish"
        );
        ensure!(
            fs::read_dir(&locked)?.next().is_none(),
            "a publish that fails before the link leaves neither final nor temp file"
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn open_rejects_wrong_aad_and_truncation() -> Result<()> {
    let vector = vector()?;
    database_case(async |db| {
        let home = TempDir::new()?;
        write_kek(home.path(), text(&vector, "kek")?)?;
        db.seed_key_file_row(text(&vector, "wrapped_dek")?).await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf());

        let token = store.seal(b"plaintext", b"right aad").await?;
        assert_eq!(store.open_sealed(&token, b"right aad").await?, b"plaintext");
        assert!(matches!(
            store.open_sealed(&token, b"wrong aad").await,
            Err(SecretError::InvalidToken)
        ));
        let raw = STANDARD.decode(&token)?;
        let short = STANDARD.encode(&raw[..27]);
        assert!(matches!(
            store.open_sealed(&short, b"right aad").await,
            Err(SecretError::InvalidToken)
        ));
        let clipped = STANDARD.encode(&raw[..raw.len() - 1]);
        assert!(matches!(
            store.open_sealed(&clipped, b"right aad").await,
            Err(SecretError::InvalidToken)
        ));
        assert!(matches!(
            store
                .open_sealed(text(&vector, "python_token")?, b"wrong aad")
                .await,
            Err(SecretError::InvalidToken)
        ));
        Ok(())
    })
    .await
}

#[tokio::test]
async fn missing_envelope_with_existing_secrets_fails_closed() -> Result<()> {
    let vector = vector()?;
    database_case(async |db| {
        let home = TempDir::new()?;
        db.seed_secret("fixture", text(&vector, "encrypted_secret")?)
            .await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf());

        assert!(matches!(
            store.ensure_ready().await,
            Err(SecretError::Unavailable)
        ));
        assert!(matches!(
            store.secret("fixture").await,
            Err(SecretError::Unavailable)
        ));
        ensure!(
            !home.path().join(".secret_kek").exists(),
            "fail-closed initialization must not create a KEK file"
        );
        ensure!(
            db.count("secret_key_material").await? == 0,
            "fail-closed initialization must not insert key material"
        );
        ensure!(
            db.count("secrets").await? == 1,
            "the secrets table must be unchanged"
        );
        let stored: String = db
            .admin
            .query_one(
                &format!("SELECT encrypted_value FROM {}", db.table("secrets")),
                &[],
            )
            .await?
            .try_get(0)?;
        ensure!(
            stored == text(&vector, "encrypted_secret")?,
            "the stored secret must be unchanged"
        );
        Ok(())
    })
    .await
}

/// Writes Rust's half of the committed vector. Run once with `--run-ignored only`
/// after the implementation is green; the placeholder guard keeps it single-use.
#[tokio::test]
#[ignore = "regenerates the committed Rust half of secret_seal_vector.json"]
async fn generate_rust_seal_vector() -> Result<()> {
    let vector = vector()?;
    let mut token = String::new();
    database_case(async |db| {
        let home = TempDir::new()?;
        write_kek(home.path(), text(&vector, "kek")?)?;
        db.seed_key_file_row(text(&vector, "wrapped_dek")?).await?;
        let store = SecretStore::new(pool(&db.url)?, home.path().to_path_buf());
        token = store
            .seal(
                text(&vector, "rust_plaintext")?.as_bytes(),
                text(&vector, "rust_aad")?.as_bytes(),
            )
            .await?;
        Ok(())
    })
    .await?;

    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/secret_seal_vector.json"
    );
    let fixture = fs::read_to_string(path)?;
    let pending = r#""rust_token":"pending native seal generation""#;
    ensure!(
        fixture.matches(pending).count() == 1,
        "the fixture must still carry the pending Rust token placeholder exactly once"
    );
    fs::write(
        path,
        fixture.replacen(pending, &format!(r#""rust_token":"{token}""#), 1),
    )?;
    Ok(())
}
