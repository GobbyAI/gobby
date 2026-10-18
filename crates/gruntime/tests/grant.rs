//! Native `GET /api/runtime/config`: grant claims, the ordered rejections, and the
//! interactive revocation check, against an isolated PostgreSQL schema.

use std::panic::AssertUnwindSafe;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Result, ensure};
use axum::Router;
use axum::body::Body;
use axum::http::header::CACHE_CONTROL;
use axum::http::request::Parts;
use axum::http::{Method, Request, Response, StatusCode};
use futures_util::FutureExt;
use gobby_core::grant::presentation::sign_grant;
use gobby_core::grant::{
    EXPECTED_API_CONTRACT, GRANT_HEADER, GrantBundle, PrincipalKind, deployment_token,
    encode_grant_header, expected_schema_identity,
};
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_core::route_family::RouteFamily;
use gobby_runtime::RuntimeFamily;
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tokio_postgres::{Client, NoTls};
use tower_service::Service;
use uuid::Uuid;

const MIGRATION: &str =
    include_str!("../../gcore/assets/schema/migrations/469_config_active_state.sql");
// The pool checks out as `gobby_daemon_runtime`, and Postgres drops schemas that role
// cannot use from the search path. Mirrors the role setup in `tests/config.rs`.
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
const SECRET: &str = "presentation-test-secret";
const EPOCH: i64 = 7;
const GENERATION: i32 = 3;
const CONFIG_REVISION: i64 = 41;

fn now() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("clock")
        .as_secs() as i64
}

fn golden() -> GrantBundle {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/runtime_grants/golden/direct_datastores.json");
    let raw = std::fs::read(path).expect("golden grant");
    serde_json::from_slice(raw.trim_ascii()).expect("parse golden grant")
}

/// A presentable interactive grant bound to one machine and project, signed with the
/// deployment secret.
fn interactive(token: &str, machine: Uuid, project: Uuid) -> GrantBundle {
    let now = now();
    let mut grant = golden();
    grant.deployment.token = token.into();
    grant.deployment.fencing_epoch = EPOCH;
    grant.config_revision = CONFIG_REVISION;
    grant.schema_identity = expected_schema_identity();
    grant.api_contract = EXPECTED_API_CONTRACT;
    grant.principal.kind = PrincipalKind::Interactive;
    grant.principal.machine_id = machine.to_string();
    grant.principal.project_id = project.to_string();
    grant.issued_at = now - 60;
    grant.expires_at = now + 3_600;
    sign_grant(&grant, SECRET.as_bytes()).expect("sign")
}

/// Unpadded base64url, the grant header's wire encoding.
fn b64url(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::new();
    for chunk in bytes.chunks(3) {
        let n = (u32::from(chunk[0]) << 16)
            | (u32::from(*chunk.get(1).unwrap_or(&0)) << 8)
            | u32::from(*chunk.get(2).unwrap_or(&0));
        for index in 0..=chunk.len() {
            out.push(ALPHABET[((n >> (18 - 6 * index)) & 0x3f) as usize] as char);
        }
    }
    out
}

fn config_request(grant: &GrantBundle) -> Request<Body> {
    let header = encode_grant_header(grant).expect("encode");
    Request::builder()
        .method(Method::GET)
        .uri("/api/runtime/config")
        .header("x-gobby-key-id", "key-1")
        .header(GRANT_HEADER, header)
        .body(Body::empty())
        .expect("request")
}

fn config_parts(header: Option<String>) -> Parts {
    let builder = Request::builder()
        .method(Method::GET)
        .uri("/api/runtime/config")
        .header("x-gobby-key-id", "key-1");
    let builder = match header {
        Some(value) => builder.header(GRANT_HEADER, value),
        None => builder,
    };
    builder.body(Body::empty()).expect("request").into_parts().0
}

fn claims(family: &RuntimeFamily, parts: &Parts) -> bool {
    <RuntimeFamily as RouteFamily<()>>::claims(family, parts)
}

fn document(active_values: Value) -> Value {
    json!({
        "desired_values": {},
        "active_values": active_values,
        "desired_overrides": {},
        "pending_restart_keys": [],
        "failed_live_keys": {},
        "desired_bindings": {},
        "active_bindings": {},
    })
}

fn pool(url: &str) -> Result<Arc<Pool>> {
    Ok(Arc::new(Pool::build(
        url,
        PoolSettings {
            max_size: 2,
            application_name: "gobby-gdaemon-grant-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?))
}

fn unreachable_pool() -> Result<Arc<Pool>> {
    let port = std::net::TcpListener::bind("127.0.0.1:0")?
        .local_addr()?
        .port();
    pool(&format!(
        "postgresql://gobby_test@127.0.0.1:{port}/gobby_test?sslmode=disable"
    ))
}

async fn call(family: &RuntimeFamily, request: Request<Body>) -> Response<Body> {
    let mut router: Router = <RuntimeFamily as RouteFamily<()>>::router(family);
    match router.call(request).await {
        Ok(response) => response,
        Err(never) => match never {},
    }
}

async fn json_body(response: Response<Body>) -> Value {
    let bytes = response
        .into_body()
        .collect()
        .await
        .expect("body")
        .to_bytes();
    serde_json::from_slice(&bytes).expect("json body")
}

async fn assert_rejected(
    family: &RuntimeFamily,
    grant: &GrantBundle,
    status: StatusCode,
    code: &str,
    message: &str,
) {
    let response = call(family, config_request(grant)).await;
    assert_eq!(response.status(), status, "{code}");
    assert_eq!(
        json_body(response).await,
        json!({"code": code, "error": code, "message": message}),
        "{code}"
    );
}

/// The per-test schema holds the production config migration and the grant tables the
/// route reads: the deployment row, and the interactive principal lookup under the auth
/// schema name `auth_schema_for` derives for it.
struct TestDb {
    admin: Client,
    schema: String,
    auth: String,
    url: String,
}

impl TestDb {
    async fn seed(&self, token: &str, epoch: i64, secret: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {}.deployment_runtime \
                     (deployment_token, fencing_epoch, grant_signing_secret) \
                     VALUES ($1, $2, $3)",
                    self.schema
                ),
                &[&token, &epoch, &secret],
            )
            .await?;
        Ok(())
    }

    async fn set_epoch(&self, token: &str, epoch: i64) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "UPDATE {}.deployment_runtime SET fencing_epoch = $2 \
                     WHERE deployment_token = $1",
                    self.schema
                ),
                &[&token, &epoch],
            )
            .await?;
        Ok(())
    }

    async fn unseed(&self, token: &str) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "DELETE FROM {}.deployment_runtime WHERE deployment_token = $1",
                    self.schema
                ),
                &[&token],
            )
            .await?;
        Ok(())
    }

    async fn bind(&self, token: &str, machine: Uuid, project: Uuid) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {}.principal_bindings \
                     (managed_execution_id, role_name, owner_kind, deployment_token, \
                      issuing_machine_id, project_id, credential_generation) \
                     VALUES ($1, 'gobby_interactive_1', 'interactive', $2, $3, $4, $5)",
                    self.auth
                ),
                &[&Uuid::new_v4(), &token, &machine, &project, &GENERATION],
            )
            .await?;
        Ok(())
    }

    async fn revoke(&self, token: &str, machine: Uuid, project: Uuid) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "UPDATE {}.principal_bindings SET revoked_at = now() \
                     WHERE deployment_token = $1 AND issuing_machine_id = $2 \
                     AND project_id = $3 AND credential_generation = $4",
                    self.auth
                ),
                &[&token, &machine, &project, &GENERATION],
            )
            .await?;
        Ok(())
    }

    async fn publish(&self, token: &str, revision: i64, active_values: &Value) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {}.config_active_state (deployment_token, revision, state) \
                     VALUES ($1, $2, $3::text::jsonb)",
                    self.schema
                ),
                &[
                    &token,
                    &revision,
                    &document(active_values.clone()).to_string(),
                ],
            )
            .await?;
        Ok(())
    }
}

async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL grant test");
        return Ok(());
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "grant tests require the protected isolated test hub"
    );
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    let schema = format!("test_rust_grant_{}", Uuid::new_v4().simple());
    let auth = format!("{schema}_agent_auth");
    admin
        .batch_execute(&format!(
            "{DAEMON_ROLE}
CREATE SCHEMA {schema}; CREATE SCHEMA {auth};
CREATE TABLE {auth}.principal_bindings (
    managed_execution_id uuid NOT NULL,
    role_name name NOT NULL,
    owner_kind text NOT NULL,
    deployment_token text NOT NULL,
    issuing_machine_id uuid NOT NULL,
    project_id uuid NOT NULL,
    credential_generation integer NOT NULL,
    revoked_at timestamp with time zone
);
CREATE FUNCTION {auth}.lookup_interactive_principal(requested_deployment_token text, requested_machine_id uuid, requested_project_id uuid, requested_generation integer) RETURNS TABLE(managed_execution_id uuid, role_name name, credential_generation integer, revoked_at timestamp with time zone)
    LANGUAGE sql SECURITY DEFINER SET search_path TO '{auth}', 'pg_temp'
    AS $$
    SELECT managed_execution_id, role_name, credential_generation, revoked_at
      FROM principal_bindings
     WHERE owner_kind = 'interactive'
       AND deployment_token = requested_deployment_token
       AND issuing_machine_id = requested_machine_id
       AND project_id = requested_project_id
       AND (requested_generation IS NULL OR credential_generation = requested_generation)
     ORDER BY credential_generation DESC
     LIMIT 1
$$;
CREATE TABLE {schema}.deployment_runtime (
    deployment_token text NOT NULL,
    fencing_epoch bigint DEFAULT 0 NOT NULL,
    grant_signing_secret text NOT NULL,
    epoch_updated_at timestamp with time zone
);
GRANT USAGE ON SCHEMA {schema}, {auth} TO gobby_daemon_runtime;
GRANT SELECT ON {schema}.deployment_runtime TO gobby_daemon_runtime;
{}",
            MIGRATION.replace("public.", &format!("{schema}."))
        ))
        .await?;
    let separator = if url.contains('?') { '&' } else { '?' };
    let db = TestDb {
        admin,
        url: format!("{url}{separator}options=-csearch_path%3D{schema}"),
        schema,
        auth,
    };
    let outcome = AssertUnwindSafe(case(&db)).catch_unwind().await;
    let cleanup = db
        .admin
        .batch_execute(&format!(
            "DROP SCHEMA {} CASCADE; DROP SCHEMA {} CASCADE;",
            db.schema, db.auth
        ))
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
                eprintln!("grant test cleanup failed: {error}");
            }
            std::panic::resume_unwind(panic)
        }
    }
}

#[tokio::test]
async fn runtime_config_presents_interactive_grants() -> Result<()> {
    database_case(async |db| {
        let home = tempfile::tempdir()?;
        let machine = Uuid::new_v4();
        std::fs::write(home.path().join("machine_id"), machine.to_string())?;
        let token = deployment_token(home.path());
        let project = Uuid::new_v4();
        let grant = interactive(&token, machine, project);
        db.seed(&token, EPOCH, SECRET).await?;
        db.publish(
            &token,
            CONFIG_REVISION,
            &json!({"ai.embeddings.dim": 768, "ai.embeddings.model": "nomic-embed-text"}),
        )
        .await?;
        db.bind(&token, machine, project).await?;
        let family = RuntimeFamily::new(pool(&db.url)?, home.path().to_owned());

        let accepted = call(&family, config_request(&grant)).await;
        assert_eq!(accepted.status(), StatusCode::OK);
        assert_eq!(accepted.headers()[CACHE_CONTROL], "no-store");
        assert_eq!(
            json_body(accepted).await,
            json!({
                "config_revision": CONFIG_REVISION,
                "settings": {
                    "ai.embeddings.dim": "768",
                    "ai.embeddings.model": "nomic-embed-text",
                },
            })
        );

        db.set_epoch(&token, EPOCH + 1).await?;
        let stale = call(&family, config_request(&grant)).await;
        assert_eq!(stale.status(), StatusCode::CONFLICT);
        assert_eq!(
            json_body(stale).await,
            json!({
                "code": "stale_epoch",
                "error": "stale_epoch",
                "message": "grant fencing epoch is stale",
            })
        );
        db.set_epoch(&token, EPOCH).await?;

        db.revoke(&token, machine, project).await?;
        let revoked = call(&family, config_request(&grant)).await;
        assert_eq!(revoked.status(), StatusCode::FORBIDDEN);
        assert_eq!(
            json_body(revoked).await,
            json!({
                "code": "revoked",
                "error": "revoked",
                "message": "grant has been revoked",
            })
        );

        db.unseed(&token).await?;
        let unavailable = call(&family, config_request(&grant)).await;
        assert_eq!(unavailable.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            json_body(unavailable).await,
            json!({"detail": "grant service unavailable"})
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn malformed_and_missing_grant_headers_are_forwarded() -> Result<()> {
    let home = tempfile::tempdir()?;
    let family = RuntimeFamily::new(unreachable_pool()?, home.path().to_owned());
    let token = deployment_token(home.path());
    let grant = interactive(&token, Uuid::new_v4(), Uuid::new_v4());

    let mut envelope = serde_json::to_value(&grant)?;
    envelope["managed_api_token"] = json!("capability-token");
    let mut numeric_envelope = envelope.clone();
    numeric_envelope["managed_api_token"] = json!(5);
    let mut managed = grant.clone();
    managed.principal.kind = PrincipalKind::AgentRun;
    let managed_header = encode_grant_header(&managed)?;

    assert!(
        !claims(&family, &config_parts(None)),
        "a missing header is forwarded"
    );
    assert!(
        !claims(&family, &config_parts(Some("%%%not-base64url%%%".into()))),
        "an undecodable header is forwarded"
    );
    assert!(
        !claims(&family, &config_parts(Some(b64url(b"not json")))),
        "a header that is not JSON is forwarded"
    );
    assert!(
        !claims(&family, &config_parts(Some(b64url(br#"{"version":2}"#)))),
        "a partial bundle is forwarded"
    );
    assert!(
        !claims(
            &family,
            &config_parts(Some(b64url(numeric_envelope.to_string().as_bytes())))
        ),
        "a non-string managed_api_token is malformed and forwarded"
    );
    assert!(
        !claims(&family, &config_parts(Some(managed_header))),
        "managed principals stay with Python until 4.7"
    );
    assert!(
        claims(
            &family,
            &config_parts(Some(b64url(envelope.to_string().as_bytes())))
        ),
        "a string managed_api_token does not make an interactive grant malformed"
    );
    Ok(())
}

#[tokio::test]
async fn presentation_rejections_follow_python_order_without_the_backend() -> Result<()> {
    database_case(async |db| {
        let home = tempfile::tempdir()?;
        let machine = Uuid::new_v4();
        std::fs::write(home.path().join("machine_id"), machine.to_string())?;
        let token = deployment_token(home.path());
        let project = Uuid::new_v4();
        let grant = interactive(&token, machine, project);
        db.seed(&token, EPOCH, SECRET).await?;
        db.bind(&token, machine, project).await?;
        let family = RuntimeFamily::new(pool(&db.url)?, home.path().to_owned());

        let mut bad_checksum = grant.clone();
        bad_checksum.payload_checksum = "00".repeat(32);
        assert_rejected(
            &family,
            &bad_checksum,
            StatusCode::FORBIDDEN,
            "invalid_signature",
            "grant payload checksum is invalid",
        )
        .await;

        let mut other_contract = grant.clone();
        other_contract.api_contract = EXPECTED_API_CONTRACT + 1;
        let other_contract = sign_grant(&other_contract, SECRET.as_bytes())?;
        assert_rejected(
            &family,
            &other_contract,
            StatusCode::FORBIDDEN,
            "wrong_api_contract",
            "grant API contract is not supported",
        )
        .await;

        let mut both = other_contract.clone();
        both.payload_checksum = "00".repeat(32);
        assert_rejected(
            &family,
            &both,
            StatusCode::FORBIDDEN,
            "invalid_signature",
            "grant payload checksum is invalid",
        )
        .await;
        Ok(())
    })
    .await
}
