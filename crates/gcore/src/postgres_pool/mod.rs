//! Async PostgreSQL connection pool for the native daemon (`postgres-pool`).
//!
//! `RecyclingMethod::Fast` resets no session state, so the `post_create` and
//! `post_recycle` hooks establish and re-verify the runtime role, UTC, and the
//! configured application name on every checkout; a connection that fails
//! verification is discarded. `Pool::get` owns the only deadline, because
//! deadpool's own timeouts do not enclose its hooks.

mod config;
mod row;
mod session;
mod transaction;

pub use row::{FromRow, RowError};
pub use session::DedicatedSession;
pub use transaction::{LockTarget, Transaction, TransactionError};

use std::sync::Arc;
#[cfg(test)]
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use deadpool_postgres::{Hook, HookError, ManagerConfig, RecyclingMethod, Runtime};
use tokio_postgres::Client;

const RUNTIME_ROLE: &str = "gobby_daemon_runtime";
const APPLICATION_NAME_PREFIX: &str = "gobby-gdaemon";
const MINIMUM_SHARE: usize = 2;

/// Sizing and identity for one native pool.
#[derive(Debug, Clone)]
pub struct PoolSettings {
    /// The native share of the connection budget; at least 2.
    pub max_size: usize,
    /// Must start with `gobby-gdaemon`.
    pub application_name: String,
    /// Bound on one whole checkout: slot wait, connect, and session hooks.
    pub acquire_timeout: Duration,
}

#[derive(Debug, thiserror::Error)]
pub enum PoolError {
    #[error(
        "the native PostgreSQL pool share is {actual} connections; at least {minimum} are required"
    )]
    NativeShareTooSmall { actual: usize, minimum: usize },
    #[error("pooled application_name `{0}` must be printable ASCII starting with `gobby-gdaemon`")]
    InvalidApplicationName(String),
    #[error("invalid PostgreSQL pool configuration")]
    InvalidConfig(#[source] anyhow::Error),
    #[error("the Gobby PostgreSQL hub at {endpoint} is unavailable")]
    Unavailable {
        endpoint: String,
        #[source]
        source: tokio_postgres::Error,
    },
    #[error(
        "the Gobby PostgreSQL hub at {endpoint} cannot run pooled sessions as gobby_daemon_runtime"
    )]
    RuntimeRoleUnavailable {
        endpoint: String,
        #[source]
        source: HookError,
    },
    #[error("timed out acquiring a pooled PostgreSQL connection")]
    AcquireTimeout,
    #[error("the PostgreSQL pool is closed")]
    Closed,
}

/// A bounded pool whose every checkout runs as `gobby_daemon_runtime` in UTC.
pub struct Pool {
    inner: deadpool_postgres::Pool,
    acquire_timeout: Duration,
    endpoint: String,
    #[cfg(test)]
    create_stall: Arc<AtomicBool>,
    #[cfg(test)]
    recycle_stall: Arc<AtomicBool>,
    /// One-shot gate: the next rollback signals it once its guard is armed,
    /// then suspends before `ROLLBACK`, so a test can cancel it there.
    #[cfg(test)]
    rollback_gate: std::sync::Mutex<Option<tokio::sync::oneshot::Sender<()>>>,
}

impl Pool {
    /// Build a pool without connecting; the first checkout opens the first connection.
    pub fn build(database_url: &str, settings: PoolSettings) -> Result<Self, PoolError> {
        if settings.max_size < MINIMUM_SHARE {
            return Err(PoolError::NativeShareTooSmall {
                actual: settings.max_size,
                minimum: MINIMUM_SHARE,
            });
        }
        // The server rewrites bytes outside printable ASCII, which would fail
        // every verification.
        let name = &settings.application_name;
        if !name.starts_with(APPLICATION_NAME_PREFIX)
            || !name.bytes().all(|b| (b' '..=b'~').contains(&b))
        {
            return Err(PoolError::InvalidApplicationName(settings.application_name));
        }
        let target =
            config::ConnectTarget::parse(database_url, name).map_err(PoolError::InvalidConfig)?;
        let manager = target
            .manager(ManagerConfig {
                recycling_method: RecyclingMethod::Fast,
            })
            .map_err(PoolError::InvalidConfig)?;
        let create = SessionHook::new(name);
        let recycle = SessionHook::new(name);
        #[cfg(test)]
        let (create_stall, recycle_stall) = (create.stall.clone(), recycle.stall.clone());
        let inner = deadpool_postgres::Pool::builder(manager)
            .max_size(settings.max_size)
            .runtime(Runtime::Tokio1)
            .post_create(Hook::async_fn(move |client, _| {
                let hook = create.clone();
                Box::pin(async move { hook.establish(client).await })
            }))
            .post_recycle(Hook::async_fn(move |client, _| {
                let hook = recycle.clone();
                Box::pin(async move { hook.restore(client).await })
            }))
            .build()
            .map_err(|error| PoolError::InvalidConfig(error.into()))?;
        Ok(Self {
            inner,
            acquire_timeout: settings.acquire_timeout,
            endpoint: target.endpoint,
            #[cfg(test)]
            create_stall,
            #[cfg(test)]
            recycle_stall,
            #[cfg(test)]
            rollback_gate: Default::default(),
        })
    }

    /// Pool size and idle connections, for observation.
    pub fn status(&self) -> deadpool_postgres::Status {
        self.inner.status()
    }

    /// Check out a verified connection within `acquire_timeout`. Expiry, or
    /// dropping this future, releases any slot a stalled hook held and closes
    /// that connection.
    pub async fn get(&self) -> Result<deadpool_postgres::Client, PoolError> {
        let checkout = tokio::time::timeout(self.acquire_timeout, self.inner.get()).await;
        match checkout.map_err(|_| PoolError::AcquireTimeout)? {
            Ok(client) => Ok(client),
            Err(deadpool_postgres::PoolError::Backend(source)) => Err(PoolError::Unavailable {
                endpoint: self.endpoint.clone(),
                source,
            }),
            Err(deadpool_postgres::PoolError::PostCreateHook(source)) => {
                Err(PoolError::RuntimeRoleUnavailable {
                    endpoint: self.endpoint.clone(),
                    source,
                })
            }
            // Unreachable with deadpool's timeouts unset and a runtime set.
            Err(deadpool_postgres::PoolError::Timeout(_)) => Err(PoolError::AcquireTimeout),
            Err(
                deadpool_postgres::PoolError::Closed
                | deadpool_postgres::PoolError::NoRuntimeSpecified,
            ) => Err(PoolError::Closed),
        }
    }
}

#[derive(Debug, thiserror::Error)]
#[error("invalid SQL identifier: {0:?}")]
pub struct IdentifierError(pub String);

/// Double-quote `name` for SQL text; only `^[A-Za-z_][A-Za-z0-9_]*$` is
/// accepted, as the Python daemon's `validate_identifier` does.
pub fn quote_identifier(name: &str) -> Result<String, IdentifierError> {
    let mut bytes = name.bytes();
    let valid = matches!(bytes.next(), Some(byte) if byte.is_ascii_alphabetic() || byte == b'_')
        && bytes.all(|byte| byte.is_ascii_alphanumeric() || byte == b'_');
    if valid {
        Ok(format!("\"{name}\""))
    } else {
        Err(IdentifierError(name.to_owned()))
    }
}

/// One hook's session invariants.
#[derive(Clone, Debug)]
struct SessionHook {
    application_name: Arc<str>,
    #[cfg(test)]
    stall: Arc<AtomicBool>,
}

impl SessionHook {
    fn new(application_name: &str) -> Self {
        Self {
            application_name: application_name.into(),
            #[cfg(test)]
            stall: Arc::default(),
        }
    }

    /// `post_create`: take the runtime role and UTC, then verify.
    async fn establish(&self, client: &Client) -> Result<(), HookError> {
        #[cfg(test)]
        self.stall_if_set(client).await?;
        client
            .batch_execute("SET ROLE gobby_daemon_runtime; SET TIME ZONE 'UTC'")
            .await
            .map_err(HookError::Backend)?;
        self.verify(client).await
    }

    /// `post_recycle`: end a borrower's transaction, undo its time zone or
    /// name change, then verify.
    async fn restore(&self, client: &Client) -> Result<(), HookError> {
        #[cfg(test)]
        self.stall_if_set(client).await?;
        // BEGIN; ROLLBACK: silent when idle, ends an open transaction, discards an aborted one.
        client
            .batch_execute("BEGIN; ROLLBACK; SET TIME ZONE 'UTC'")
            .await
            .map_err(HookError::Backend)?;
        client
            .execute(
                "SELECT set_config('application_name', $1, false)",
                &[&&*self.application_name],
            )
            .await
            .map_err(HookError::Backend)?;
        self.verify(client).await
    }

    /// Autocommit check of role, time zone, and application name.
    async fn verify(&self, client: &Client) -> Result<(), HookError> {
        let row = client
            .query_one(
                "SELECT current_user, current_setting('TimeZone'), \
                 current_setting('application_name')",
                &[],
            )
            .await
            .map_err(HookError::Backend)?;
        let session: (String, String, String) = (
            row.try_get(0).map_err(HookError::Backend)?,
            row.try_get(1).map_err(HookError::Backend)?,
            row.try_get(2).map_err(HookError::Backend)?,
        );
        if (session.0.as_str(), session.1.as_str(), session.2.as_str())
            == (RUNTIME_ROLE, "UTC", &*self.application_name)
        {
            return Ok(());
        }
        Err(HookError::message(format!(
            "pooled session runs as {} in {} named {}",
            session.0, session.1, session.2
        )))
    }

    /// Test probe: hold the hook in `pg_sleep(30)` while the flag is set.
    #[cfg(test)]
    async fn stall_if_set(&self, client: &Client) -> Result<(), HookError> {
        if self.stall.load(Ordering::SeqCst) {
            client
                .batch_execute("SELECT pg_sleep(30)")
                .await
                .map_err(HookError::Backend)?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests;
