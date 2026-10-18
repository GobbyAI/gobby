//! The `runtime` family's route table and native `GET /api/config/effective`.

use std::panic::AssertUnwindSafe;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Result, ensure};
use axum::Router;
use axum::body::Body;
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::request::Parts;
use axum::http::{Method, Request, Response, StatusCode};
use futures_util::FutureExt;
use gobby_core::grant::{GRANT_HEADER, GrantBundle, deployment_token, encode_grant_header};
use gobby_core::postgres_pool::{Pool, PoolSettings};
use gobby_core::route_family::RouteFamily;
use gobby_runtime::{Caller, NATIVE_ROUTES, RuntimeFamily};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tokio_postgres::{Client, NoTls};
use tower_service::Service;
use uuid::Uuid;

/// The production migration, applied to a per-test schema.
const MIGRATION: &str =
    include_str!("../../gcore/assets/schema/migrations/469_config_active_state.sql");

/// The daemon role as `crates/gcore/assets/schema/baseline.sql` creates it: the
/// same advisory lock, idempotent create and attributes. A fresh hub gets this
/// role from the baseline; the isolated test hub needs it before migration 469's
/// grant and the schema grant in `database_case` can run.
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
    async fn publish(&self, token: &str, revision: i64, state: &Value) -> Result<()> {
        self.admin
            .execute(
                &format!(
                    "INSERT INTO {}.config_active_state (deployment_token, revision, state) \
                     VALUES ($1, $2, $3::text::jsonb)",
                    self.schema
                ),
                &[&token, &revision, &state.to_string()],
            )
            .await?;
        Ok(())
    }
}

// Each case owns a fresh schema on the protected test hub, created from the
// production migration and readable by the pool's `gobby_daemon_runtime` role.
async fn database_case(case: impl AsyncFnOnce(&TestDb) -> Result<()>) -> Result<()> {
    let Ok(url) = std::env::var("DATABASE_URL") else {
        eprintln!("DATABASE_URL unset; skipping isolated PostgreSQL runtime-family test");
        return Ok(());
    };
    let config: tokio_postgres::Config = url.parse()?;
    ensure!(
        config.get_dbname() == Some("gobby_test")
            && config.get_hosts() == [tokio_postgres::config::Host::Tcp("127.0.0.1".into())]
            && config.get_ports() == [60892]
            && std::env::var("GOBBY_TEST_PROTECT").as_deref() == Ok("1"),
        "runtime-family tests require the protected isolated test hub"
    );
    let (admin, connection) = config.connect(NoTls).await?;
    let driver = tokio::spawn(connection);
    let schema = format!("test_rust_runtime_{}", Uuid::new_v4().simple());
    admin
        .batch_execute(&format!(
            "{DAEMON_ROLE}
CREATE SCHEMA {schema}; GRANT USAGE ON SCHEMA {schema} TO gobby_daemon_runtime; {}",
            MIGRATION.replace("public.", &format!("{schema}."))
        ))
        .await?;
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
                eprintln!("runtime-family test cleanup failed: {error}");
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
            application_name: "gobby-gdaemon-runtime-config-test".into(),
            acquire_timeout: Duration::from_secs(5),
        },
    )?))
}

/// A pool whose every checkout fails: nothing listens on its port.
fn unreachable_pool() -> Result<Arc<Pool>> {
    let port = std::net::TcpListener::bind("127.0.0.1:0")?
        .local_addr()?
        .port();
    pool(&format!(
        "postgresql://gobby_test@127.0.0.1:{port}/gobby_test?sslmode=disable"
    ))
}

/// A published state document whose only populated part is `active_values`.
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

/// A request as the front door forwards it for `caller`. A gated row is claimed
/// only with a grant; see `granted_parts`.
fn request(method: Method, path: &str, caller: Option<Caller>) -> Request<Body> {
    let builder = Request::builder().method(method).uri(path);
    let builder = match caller {
        None => builder,
        Some(Caller::Operator) => builder.header("x-gobby-key-id", "key-1"),
    };
    builder.body(Body::empty()).expect("request")
}

fn parts(method: Method, path: &str, caller: Option<Caller>) -> Parts {
    request(method, path, caller).into_parts().0
}

/// The golden grant, an interactive principal: the shape the route gate decodes.
fn interactive_grant_header() -> String {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/runtime_grants/golden/direct_datastores.json");
    let raw = std::fs::read(path).expect("golden grant");
    let grant: GrantBundle =
        serde_json::from_slice(raw.trim_ascii()).expect("parse golden grant");
    encode_grant_header(&grant).expect("encode")
}

/// `parts` for `caller`, carrying the interactive grant a gated row requires.
fn granted_parts(method: Method, path: &str, caller: Caller) -> Parts {
    let mut presented = parts(method, path, Some(caller));
    presented
        .headers
        .insert(GRANT_HEADER, interactive_grant_header().parse().expect("header"));
    presented
}

fn claims(family: &RuntimeFamily, parts: &Parts) -> bool {
    <RuntimeFamily as RouteFamily<()>>::claims(family, parts)
}

/// A concrete path for an axum 0.8 route pattern.
fn concrete(pattern: &str) -> String {
    pattern
        .split('/')
        .map(|segment| {
            if segment.starts_with("{*") {
                "rest/of/path"
            } else if segment.starts_with('{') {
                "segment"
            } else {
                segment
            }
        })
        .collect::<Vec<_>>()
        .join("/")
}

async fn call(family: &RuntimeFamily, request: Request<Body>) -> Response<Body> {
    let mut router: Router = <RuntimeFamily as RouteFamily<()>>::router(family);
    match router.call(request).await {
        Ok(response) => response,
        Err(never) => match never {},
    }
}

fn effective() -> Request<Body> {
    request(Method::GET, "/api/config/effective", Some(Caller::Operator))
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

#[tokio::test]
async fn effective_config_matches_python_projection() -> Result<()> {
    database_case(async |db| {
        let home = tempfile::tempdir()?;
        let path = std::env::var("PATH")?;
        let state = json!({
            "desired_values": {"ai.embeddings.dim": 1024, "ai.embeddings.model": "desired-only"},
            "active_values": {
                "ai.embeddings.dim": 768,
                "ai.embeddings.model": "nomic-embed-text",
                "ai.embeddings.query_prefix": "recherche : é",
                "ai.embeddings.routing": "${PATH}",
                "ai.embeddings.timeout_seconds": 2.5,
                "databases.falkordb.host": null,
                "databases.falkordb.port": 16379,
                "databases.qdrant.url": "${GOBBY_RUNTIME_TEST_UNSET:-http://localhost:6333}",
                "indexing.respect_gitignore": false,
                "databases.falkordb.password": "never-exported",
                "test_mode": true,
            },
            "desired_overrides": {"ai.embeddings.dim": 1024},
            "pending_restart_keys": ["ai.embeddings.dim"],
            "failed_live_keys": {
                "indexing.respect_gitignore": {"revision": 6, "subscriber": "indexer"},
            },
            "desired_bindings": {},
            "active_bindings": {
                "databases.falkordb.password": {
                    "reference": "$secret:falkordb_password",
                    "fingerprint": "f1",
                    "sealed": "c2VhbGVk",
                },
            },
        });
        db.publish(&deployment_token(home.path()), 7, &state)
            .await?;
        let family = RuntimeFamily::new(pool(&db.url)?, home.path().to_owned());

        let response = call(&family, effective()).await;

        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(response.headers()[CONTENT_TYPE], "application/json");
        assert_eq!(response.headers()[CACHE_CONTROL], "no-store");
        // Active values only, machine keys only, null skipped, non-strings in
        // `json.dumps` form, strings verbatim, and `${…}` expanded.
        assert_eq!(
            json_body(response).await,
            json!({
                "revision": 7,
                "config": {
                    "ai.embeddings.dim": "768",
                    "ai.embeddings.model": "nomic-embed-text",
                    "ai.embeddings.query_prefix": "recherche : é",
                    "ai.embeddings.routing": path,
                    "ai.embeddings.timeout_seconds": "2.5",
                    "databases.falkordb.port": "16379",
                    "databases.qdrant.url": "http://localhost:6333",
                    "indexing.respect_gitignore": "false",
                },
            })
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn unresolved_values_are_omitted() -> Result<()> {
    database_case(async |db| {
        let home = tempfile::tempdir()?;
        let active_values = json!({
            "databases.qdrant.url": "${GOBBY_RUNTIME_TEST_UNSET}",
            "databases.falkordb.host": "$secret:falkordb_host",
            "databases.falkordb.port": "${GOBBY_RUNTIME_TEST_UNSET:-$secret:falkordb_port}",
            "ai.embeddings.routing": "${not-a-variable}",
            "ai.embeddings.model": "prefix-${GOBBY_RUNTIME_TEST_UNSET:-}",
            "ai.embeddings.query_prefix": "literal ${ never closed",
        });
        db.publish(&deployment_token(home.path()), 2, &document(active_values))
            .await?;
        let family = RuntimeFamily::new(pool(&db.url)?, home.path().to_owned());

        let response = call(&family, effective()).await;

        assert_eq!(response.status(), StatusCode::OK);
        // An empty default resolves; an unclosed `${` is no reference at all.
        assert_eq!(
            json_body(response).await,
            json!({
                "revision": 2,
                "config": {
                    "ai.embeddings.model": "prefix-",
                    "ai.embeddings.query_prefix": "literal ${ never closed",
                },
            })
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn missing_row_is_unavailable_and_database_failure_fails() -> Result<()> {
    database_case(async |db| {
        let home = tempfile::tempdir()?;
        let other = tempfile::tempdir()?;
        let state = document(json!({"ai.embeddings.model": "another deployment"}));
        db.publish(&deployment_token(other.path()), 3, &state)
            .await?;

        let missing = call(
            &RuntimeFamily::new(pool(&db.url)?, home.path().to_owned()),
            effective(),
        )
        .await;

        assert_eq!(missing.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(missing.headers()[CONTENT_TYPE], "application/json");
        assert_eq!(missing.headers().get(CACHE_CONTROL), None);
        assert_eq!(
            json_body(missing).await,
            json!({
                "error": {
                    "code": "runtime_unavailable",
                    "message": "Configuration runtime is still starting",
                    "path": [],
                    "retryable": true,
                },
            })
        );

        let failed = call(
            &RuntimeFamily::new(unreachable_pool()?, home.path().to_owned()),
            effective(),
        )
        .await;

        assert_eq!(failed.status(), StatusCode::INTERNAL_SERVER_ERROR);
        assert_eq!(failed.headers()[CONTENT_TYPE], "application/json");
        assert_eq!(failed.headers().get(CACHE_CONTROL), None);
        assert_eq!(
            json_body(failed).await,
            json!({"detail": "Internal server error"})
        );
        Ok(())
    })
    .await
}

#[tokio::test]
async fn claims_follow_the_route_table() -> Result<()> {
    let home = tempfile::tempdir()?;
    let family = RuntimeFamily::new(unreachable_pool()?, home.path().to_owned());

    for row in NATIVE_ROUTES {
        let path = concrete(row.path);
        let method = &row.method;
        assert!(
            !claims(&family, &parts(method.clone(), &path, None)),
            "{method} {path} without a caller"
        );
        let gated = row.gate.is_some();
        for &caller in Caller::ALL {
            let presented = if gated {
                granted_parts(method.clone(), &path, caller)
            } else {
                parts(method.clone(), &path, Some(caller))
            };
            assert_eq!(
                claims(&family, &presented),
                row.callers.contains(&caller),
                "{method} {path} for {caller:?}"
            );
            if gated {
                // Without a grant the same request is forwarded to Python.
                assert!(
                    !claims(&family, &parts(method.clone(), &path, Some(caller))),
                    "{method} {path} for {caller:?} without a grant"
                );
            }
        }
        // The router registers every row.
        let served = call(
            &family,
            request(method.clone(), &path, Some(row.callers[0])),
        )
        .await;
        assert!(
            !matches!(
                served.status(),
                StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED
            ),
            "{method} {path} answered {}",
            served.status()
        );
    }

    for (method, path) in [
        (Method::DELETE, "/api/runtime/config"),
        (Method::GET, "/api/config/no-such-route"),
    ] {
        for &caller in Caller::ALL {
            assert!(
                !claims(&family, &parts(method.clone(), path, Some(caller))),
                "{method} {path} for {caller:?}"
            );
        }
        // Nothing outside the table is registered.
        let unserved = call(
            &family,
            request(method.clone(), path, Some(Caller::Operator)),
        )
        .await;
        assert!(
            matches!(
                unserved.status(),
                StatusCode::NOT_FOUND | StatusCode::METHOD_NOT_ALLOWED
            ),
            "{method} {path} answered {}",
            unserved.status()
        );
    }
    Ok(())
}
