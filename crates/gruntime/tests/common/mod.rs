//! Isolated PostgreSQL harness for the handshake tests. Each case renders the real
//! gcore baseline into a fresh schema, so issuance runs the SQL the daemon installs.

use std::panic::AssertUnwindSafe;
use std::path::Path;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, ensure};
use axum::Router;
use axum::body::Body;
use axum::http::request::Parts;
use axum::http::{Method, Request, Response};
use futures_util::FutureExt;
use gobby_core::grant::MACHINE_HEADER;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_core::route_family::RouteFamily;
use gobby_core::schema::SchemaRunner;
use gobby_runtime::RuntimeFamily;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tokio_postgres::{Client, NoTls};
use tower_service::Service;
use uuid::Uuid;

pub const HANDSHAKE_PATH: &str = "/api/runtime/handshake";
pub const EPOCH: i64 = 7;
pub const SECRET: &str = "handshake-test-secret";
pub const CONFIG_REVISION: i64 = 41;

const VECTORS: &str = include_str!("../fixtures/capability_vectors.json");

pub struct TestDb {
    admin: Client,
    pub url: String,
    pub schema: String,
    pub auth: String,
}

pub async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL handshake test");
        return Ok(());
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "handshake tests require the protected isolated test hub"
    );
    let schema = format!("test_rust_handshake_{}", Uuid::new_v4().simple());
    let auth = format!("{schema}_agent_auth");
    let migrate_url = url.clone();
    let migrate_schema = schema.clone();
    tokio::task::spawn_blocking(move || -> Result<()> {
        let mut client = postgres::Client::connect(&migrate_url, postgres::NoTls)?;
        SchemaRunner::new(&mut client, migrate_schema)?.apply()?;
        Ok(())
    })
    .await??;
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = TestDb {
        admin,
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        schema,
        auth,
    };
    let outcome = AssertUnwindSafe(case(&db)).catch_unwind().await;
    let cleanup = teardown(&db).await;
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
                eprintln!("handshake test cleanup failed: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

/// Roles are cluster-global, so the interactive roles outlive the schema and are
/// dropped by name after the schema goes.
async fn teardown(db: &TestDb) -> Result<()> {
    let rows = db
        .admin
        .query(
            &format!(
                "SELECT role_name::text FROM \"{}\".principal_bindings",
                db.auth
            ),
            &[],
        )
        .await?;
    let roles: Vec<String> = rows.iter().map(|row| row.get(0)).collect();
    db.admin
        .batch_execute(&format!(
            "DROP SCHEMA \"{}\" CASCADE; DROP SCHEMA \"{}\" CASCADE;",
            db.schema, db.auth
        ))
        .await?;
    for role in roles {
        db.admin
            .batch_execute(&format!("DROP ROLE IF EXISTS \"{role}\""))
            .await?;
    }
    Ok(())
}

impl TestDb {
    pub async fn seed_machine(&self, machine: Uuid) -> Result<()> {
        let owner = Uuid::new_v4();
        self.admin
            .execute(
                &format!(
                    "INSERT INTO \"{}\".users (id, email, name, password_hash) VALUES ($1, $2, 'handshake', 'unused')",
                    self.schema
                ),
                &[&owner, &format!("{owner}@handshake.test")],
            )
            .await?;
        self.admin
            .execute(
                &format!(
                    "INSERT INTO \"{}\".machines (id, owner_user_id) VALUES ($1, $2)",
                    self.schema
                ),
                &[&machine, &owner],
            )
            .await?;
        Ok(())
    }

    pub async fn seed_project(&self, project: Uuid) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO \"{}\".projects (id, name) VALUES ($1, $2)",
                    self.schema
                ),
                &[&project, &project.to_string()],
            )
            .await?;
        Ok(())
    }

    pub async fn retire_project(&self, project: Uuid) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "UPDATE \"{}\".projects SET deleted_at = now() WHERE id = $1",
                    self.schema
                ),
                &[&project],
            )
            .await?;
        Ok(())
    }

    pub async fn seed_deployment(&self, token: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO \"{}\".deployment_runtime \
                     (deployment_token, fencing_epoch, grant_signing_secret) VALUES ($1, $2, $3)",
                    self.schema
                ),
                &[&token, &EPOCH, &SECRET],
            )
            .await?;
        Ok(())
    }

    pub async fn unseed_deployment(&self, token: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "DELETE FROM \"{}\".deployment_runtime WHERE deployment_token = $1",
                    self.schema
                ),
                &[&token],
            )
            .await?;
        Ok(())
    }

    pub async fn publish(&self, token: &str, active_values: &Value) -> Result<()> {
        self.publish_with_bindings(token, active_values, &json!({}))
            .await
    }

    /// Publishes the active state with its bindings, keyed by config key.
    pub async fn publish_with_bindings(
        &self,
        token: &str,
        active_values: &Value,
        active_bindings: &Value,
    ) -> Result<()> {
        let state = json!({
            "desired_values": {},
            "active_values": active_values,
            "desired_overrides": {},
            "pending_restart_keys": [],
            "failed_live_keys": {},
            "desired_bindings": {},
            "active_bindings": active_bindings,
        });
        self.admin
            .execute(
                &format!(
                    "INSERT INTO \"{}\".config_active_state \
                     (deployment_token, revision, state, published_at) \
                     VALUES ($1, $2, $3::text::jsonb, now())",
                    self.schema
                ),
                &[&token, &CONFIG_REVISION, &state.to_string()],
            )
            .await?;
        Ok(())
    }

    pub async fn unpublish(&self, token: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "DELETE FROM \"{}\".config_active_state WHERE deployment_token = $1",
                    self.schema
                ),
                &[&token],
            )
            .await?;
        Ok(())
    }

    /// Removes the writer for fresh credential material, so issuance fails outside the
    /// overlay SQLSTATEs and the handshake reports credential issuance failure.
    pub async fn drop_credential_writer(&self) -> Result<()> {
        self.admin
            .batch_execute(&format!(
                "DROP FUNCTION \"{}\".replace_interactive_credential_material(text, uuid, uuid, integer, text, text)",
                self.auth
            ))
            .await?;
        Ok(())
    }

    /// Reads the sealed password and its AAD identity the issuer stored.
    pub async fn stored_material(
        &self,
        token: &str,
        machine: Uuid,
        project: Uuid,
        generation: i64,
    ) -> Result<(String, String)> {
        let row = self
            .admin
            .query_one(
                &format!(
                    "SELECT ciphertext, aad_identity FROM \"{}\".load_interactive_credential_material(\
                     $1::text, $2::text::uuid, $3::text::uuid, $4::text::integer)",
                    self.auth
                ),
                &[
                    &token,
                    &machine.to_string(),
                    &project.to_string(),
                    &generation.to_string(),
                ],
            )
            .await?;
        Ok((row.try_get(0)?, row.try_get(1)?))
    }
}

/// A daemon home with the machine identity and a bootstrap pointing at the test hub.
pub fn daemon_home(machine: Uuid, database_url: &str) -> Result<tempfile::TempDir> {
    let home = tempfile::tempdir()?;
    std::fs::write(home.path().join("machine_id"), machine.to_string())?;
    std::fs::write(
        home.path().join("bootstrap.yaml"),
        format!("database_url: '{database_url}'\n"),
    )?;
    Ok(home)
}

/// A committed capability vector. Its `values` are the flat input Python's
/// capabilities_from_snapshot ran on, so publishing them makes the state the parity input.
pub fn vector_case(name: &str) -> Result<Value> {
    let document: Value = serde_json::from_str(VECTORS)?;
    document["cases"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|case| case["name"] == name)
        .cloned()
        .ok_or_else(|| anyhow::anyhow!("no capability vector named {name}"))
}

pub fn pool(url: &str) -> Result<Arc<Pool>> {
    Ok(Arc::new(Pool::build(
        url,
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-handshake-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?))
}

pub fn family(url: &str, home: &Path) -> Result<RuntimeFamily> {
    Ok(RuntimeFamily::new(pool(url)?, home.to_owned()))
}

pub fn handshake_request(machine_header: &str, body: &Value) -> Request<Body> {
    Request::builder()
        .method(Method::POST)
        .uri(HANDSHAKE_PATH)
        .header("content-type", "application/json")
        .header("x-gobby-key-id", "operator-key")
        .header(MACHINE_HEADER, machine_header)
        .body(Body::from(body.to_string()))
        .expect("static handshake request is valid")
}

pub fn claims(family: &RuntimeFamily, parts: &Parts) -> bool {
    <RuntimeFamily as RouteFamily<()>>::claims(family, parts)
}

pub async fn call(family: &RuntimeFamily, request: Request<Body>) -> Response<Body> {
    let mut router: Router = <RuntimeFamily as RouteFamily<()>>::router(family);
    match router.call(request).await {
        Ok(response) => response,
        Err(never) => match never {},
    }
}

pub async fn json_body(response: Response<Body>) -> Value {
    match response.into_body().collect().await {
        Ok(collected) => serde_json::from_slice(&collected.to_bytes()).unwrap_or(Value::Null),
        Err(_) => Value::Null,
    }
}

pub fn unix_now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |elapsed| {
            i64::try_from(elapsed.as_secs()).unwrap_or(i64::MAX)
        })
}
