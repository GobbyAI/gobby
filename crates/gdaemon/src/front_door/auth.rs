//! Operator API-key identity at the public HTTP and WebSocket boundary.

use std::collections::HashMap;
use std::future::Future;
use std::path::PathBuf;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use anyhow::Result;
use axum::body::Body;
use axum::http::{HeaderMap, HeaderValue, Response, StatusCode};
use gobby_core::api_key_format;
use gobby_core::postgres_pool::{Pool, PoolError, PoolSettings};

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct KeyIdentity {
    pub user_id: String,
    pub machine_id: String,
    pub key_id: String,
}

pub type ResolveFuture<'a> = Pin<Box<dyn Future<Output = Result<Option<KeyIdentity>>> + Send + 'a>>;

/// The hub lookup, separated from the transport so forwarding tests own no database.
pub trait KeyResolver: Send + Sync {
    fn resolve<'a>(&'a self, key_hash: &'a str) -> ResolveFuture<'a>;
}

pub struct PostgresKeyResolver {
    pool: Result<Pool, PoolError>,
    last_touch: Mutex<HashMap<String, Instant>>,
}

impl PostgresKeyResolver {
    pub fn new(database_url: &str) -> Self {
        Self {
            pool: Pool::build(
                database_url,
                PoolSettings {
                    max_size: 2,
                    application_name: "gobby-gdaemon-key-resolver".into(),
                    acquire_timeout: Duration::from_secs(5),
                },
            ),
            last_touch: Mutex::new(HashMap::new()),
        }
    }
}

impl KeyResolver for PostgresKeyResolver {
    fn resolve<'a>(&'a self, key_hash: &'a str) -> ResolveFuture<'a> {
        Box::pin(async move {
            let pool = self
                .pool
                .as_ref()
                .map_err(|error| anyhow::anyhow!("{error}"))?;
            let client = pool.get().await?;
            let row = tokio::time::timeout(
                Duration::from_secs(5),
                client.query_opt(
                    "SELECT k.user_id::text, m.id::text, k.id::text FROM api_keys k \
                 JOIN machines m ON m.id = k.machine_id \
                 WHERE k.key_hash = $1 AND k.revoked_at IS NULL",
                    &[&key_hash],
                ),
            )
            .await??;
            let Some(row) = row else { return Ok(None) };
            let identity = KeyIdentity {
                user_id: row.try_get(0)?,
                machine_id: row.try_get(1)?,
                key_id: row.try_get(2)?,
            };
            // Reserve the attempt before awaiting so concurrent requests cannot
            // touch the same key twice. Failed or cancelled attempts also throttle.
            let touch = {
                let mut last_touch = self
                    .last_touch
                    .lock()
                    .map_err(|_| anyhow::anyhow!("key usage lock poisoned"))?;
                let now = Instant::now();
                if last_touch
                    .get(&identity.key_id)
                    .is_some_and(|last| now.duration_since(*last) < Duration::from_secs(60))
                {
                    false
                } else {
                    last_touch.insert(identity.key_id.clone(), now);
                    true
                }
            };
            if touch {
                tokio::time::timeout(Duration::from_secs(5), client.execute(
                    "UPDATE api_keys SET last_used_at = CURRENT_TIMESTAMP WHERE id = $1::text::uuid AND revoked_at IS NULL",
                    &[&identity.key_id],
                )).await??;
            }
            Ok(Some(identity))
        })
    }
}

pub struct AuthState {
    secret: String,
    resolver: Option<Arc<dyn KeyResolver>>,
    pub(super) bootstrap_path: PathBuf,
}

impl AuthState {
    pub fn new(
        secret: String,
        resolver: Option<Arc<dyn KeyResolver>>,
        bootstrap_path: PathBuf,
    ) -> Result<Self> {
        if secret.is_empty() || HeaderValue::from_str(&secret).is_err() {
            anyhow::bail!("GOBBY_FRONT_DOOR_SECRET must be nonempty and a valid header value");
        }
        Ok(Self {
            secret,
            resolver,
            bootstrap_path,
        })
    }

    pub async fn authenticate(&self, headers: &mut HeaderMap) -> Result<(), Response<Body>> {
        for name in IDENTITY_HEADERS {
            headers.remove(name);
        }
        let Some(authorization) = headers.get("authorization") else {
            return Ok(());
        };
        let authorization = authorization.to_str().map_err(|_| missing_auth())?;
        let Some((scheme, bearer)) = authorization.split_once(' ') else {
            return Err(missing_auth());
        };
        if !scheme.eq_ignore_ascii_case("bearer") {
            return Ok(());
        }
        let bearer = bearer.trim();
        if bearer.starts_with("gobby-agent-v1.") {
            return Ok(());
        }
        let key = api_key_format::parse(bearer).ok_or_else(missing_auth)?;
        let resolver = self.resolver.as_ref().ok_or_else(resolver_unavailable)?;
        let identity = resolver
            .resolve(&api_key_format::hash(key))
            .await
            .map_err(|_| resolver_unavailable())?
            .ok_or_else(missing_auth)?;
        for (name, value) in IDENTITY_HEADERS.into_iter().zip([
            identity.user_id.as_str(),
            identity.machine_id.as_str(),
            identity.key_id.as_str(),
            self.secret.as_str(),
        ]) {
            if value.is_empty() {
                return Err(resolver_unavailable());
            }
            let value = HeaderValue::from_str(value).map_err(|_| resolver_unavailable())?;
            headers.insert(name, value);
        }
        Ok(())
    }
}

const IDENTITY_HEADERS: [&str; 4] = [
    "x-gobby-user-id",
    "x-gobby-machine-id",
    "x-gobby-key-id",
    "x-gobby-front-door",
];

fn missing_auth() -> Response<Body> {
    rejection(
        StatusCode::UNAUTHORIZED,
        "missing_auth",
        "Authentication required",
    )
}

fn resolver_unavailable() -> Response<Body> {
    rejection(
        StatusCode::SERVICE_UNAVAILABLE,
        "key_resolver_unavailable",
        "API key resolver unavailable",
    )
}

pub(super) fn rejection(status: StatusCode, code: &str, message: &str) -> Response<Body> {
    let mut response = Response::new(Body::from(
        serde_json::json!({"error": message, "code": code}).to_string(),
    ));
    *response.status_mut() = status;
    response
        .headers_mut()
        .insert("content-type", HeaderValue::from_static("application/json"));
    response
}
