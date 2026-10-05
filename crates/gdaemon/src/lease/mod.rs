//! Database-wide singleton authority, independent of the live serve lifecycle.
//!
//! Call `heartbeat` periodically (the lifecycle layer owns scheduling). Every
//! operation is bounded; failure discards the dedicated session and its fence.
//! Fences must never be cached by consumers beyond the owner's live authority.

pub mod standby;

use anyhow::{Result, bail};
use deadpool_postgres::ClientWrapper;
use gobby_core::postgres_pool::{Pool, PoolSettings};
use rand::RngCore;
use std::time::Duration;
use tokio::sync::watch;
use tokio::time::timeout;
use uuid::Uuid;

pub(crate) const NAMESPACE: &str = "gobby-single-active-daemon-v1";
pub(crate) const APPLICATION_PREFIX: &str = "gobby-lease-v1:";
const OPERATION_TIMEOUT: Duration = Duration::from_secs(5);

/// Node mode has no local hub authority, even if database credentials exist.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LeaseMode {
    Standalone,
    Hub,
    Node,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum LeaseState {
    Standby,
    Active,
    Lost,
}

/// The epoch and signing secret rotate together at successful acquisition.
/// Intentionally has no Debug implementation: the signing secret is sensitive.
pub struct Fence {
    pub epoch: i64,
    pub signing_secret: String,
}

struct Authority {
    client: ClientWrapper,
    fence: Fence,
}

struct LossOnDrop(Option<watch::Sender<LeaseState>>);

impl Drop for LossOnDrop {
    fn drop(&mut self) {
        if let Some(state) = &self.0 {
            state.send_replace(LeaseState::Lost);
        }
    }
}

/// Owns the dedicated lock session. Dropping it closes that session, never
/// returning it to the connection pool. Release invalidates authority first.
pub struct ActiveDaemonLease {
    pool: Pool,
    application_name: String,
    deployment_token: String,
    authority: Option<Authority>,
    state: watch::Sender<LeaseState>,
}

impl ActiveDaemonLease {
    pub fn new(
        database_url: &str,
        machine_id: &str,
        deployment_token: &str,
        mode: LeaseMode,
    ) -> Result<Self> {
        if mode == LeaseMode::Node {
            bail!("node mode cannot acquire a hub singleton lease");
        }
        if machine_id.is_empty()
            || !machine_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
            || deployment_token.is_empty()
        {
            bail!("lease requires a valid machine identity and deployment token");
        }
        let instance = Uuid::new_v4().simple().to_string();
        let application_name = format!("{APPLICATION_PREFIX}{machine_id}:{}", &instance[..8]);
        // PostgreSQL truncates application_name at 63 bytes. Refuse ambiguous
        // identities rather than relying on a silently truncated instance ID.
        if application_name.len() > 63 {
            bail!("lease application identity exceeds PostgreSQL's 63-byte limit");
        }
        let pool = Pool::build(
            database_url,
            PoolSettings {
                max_size: 2,
                application_name: "gobby-gdaemon-lease-connect".into(),
                acquire_timeout: OPERATION_TIMEOUT,
            },
        )?;
        let (state, _) = watch::channel(LeaseState::Standby);
        Ok(Self {
            pool,
            application_name,
            deployment_token: deployment_token.into(),
            authority: None,
            state,
        })
    }

    pub fn subscribe(&self) -> watch::Receiver<LeaseState> {
        self.state.subscribe()
    }

    pub fn application_name(&self) -> &str {
        &self.application_name
    }

    pub fn fence(&self) -> Option<&Fence> {
        self.authority
            .as_ref()
            .filter(|owner| !owner.client.is_closed())
            .map(|owner| &owner.fence)
    }

    /// Uses gcore's bounded connection/TLS contract, then detaches the session.
    /// The dedicated lease control plane uses bootstrap credentials, matching
    /// Python's lease connection. RESET ROLE never touches a recyclable session.
    pub(crate) async fn connect(&self, name: &str) -> Result<ClientWrapper> {
        let client = deadpool_postgres::Object::take(self.pool.get().await?);
        timeout(OPERATION_TIMEOUT, async {
            // A disconnected backend waiting on a table lock may not observe
            // socket EOF until its statement completes. Bound work server-side
            // too, so cancellation cannot strand the singleton lock forever.
            client
                .batch_execute("RESET ROLE; SET statement_timeout = '5s'")
                .await?;
            client
                .query_one("SELECT set_config('application_name', $1, false)", &[&name])
                .await?;
            anyhow::Ok(())
        })
        .await??;
        Ok(client)
    }

    /// A failed lock attempt never writes deployment_runtime. Cancellation
    /// before authority installation drops the detached connection and lock.
    pub async fn try_acquire(&mut self) -> Result<bool> {
        if self.authority.is_some() {
            self.heartbeat().await?;
            return Ok(true);
        }
        let mut client = self.connect(&self.application_name).await?;
        let fence = timeout(OPERATION_TIMEOUT, async {
            let keys = resolve_keys(&client).await?;
            let acquired: bool = client
                .query_one("SELECT pg_try_advisory_lock($1, $2)", &[&keys.0, &keys.1])
                .await?
                .try_get(0)?;
            if !acquired {
                return Ok(None);
            }
            let mut bytes = [0u8; 32];
            rand::rngs::OsRng.try_fill_bytes(&mut bytes)?;
            let secret: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
            let transaction = client.transaction().await?;
            let row = transaction
                .query_one(
                    "INSERT INTO deployment_runtime \
                     (deployment_token, fencing_epoch, grant_signing_secret, epoch_updated_at) \
                     VALUES ($1, 1, $2, clock_timestamp()) \
                     ON CONFLICT (deployment_token) DO UPDATE SET \
                     fencing_epoch = deployment_runtime.fencing_epoch + 1, \
                     grant_signing_secret = EXCLUDED.grant_signing_secret, \
                     epoch_updated_at = clock_timestamp() \
                     RETURNING fencing_epoch, grant_signing_secret",
                    &[&self.deployment_token, &secret],
                )
                .await?;
            let fence = Fence {
                epoch: row.try_get(0)?,
                signing_secret: row.try_get(1)?,
            };
            transaction.commit().await?;
            anyhow::Ok(Some(fence))
        })
        .await??;
        let Some(fence) = fence else {
            return Ok(false);
        };
        self.authority = Some(Authority { client, fence });
        self.state.send_replace(LeaseState::Active);
        Ok(true)
    }

    /// Removes authority while awaiting I/O, so cancelling this operation also
    /// fails closed instead of retaining an unverified lease session.
    pub async fn heartbeat(&mut self) -> Result<()> {
        let Some(owner) = self.authority.take() else {
            bail!("active-daemon lease is not held");
        };
        let mut loss = LossOnDrop(Some(self.state.clone()));
        timeout(OPERATION_TIMEOUT, owner.client.simple_query("SELECT 1")).await??;
        self.authority = Some(owner);
        loss.0 = None;
        Ok(())
    }

    pub async fn release(&mut self) -> Result<()> {
        let owner = self.authority.take();
        self.state.send_replace(LeaseState::Standby);
        if let Some(owner) = owner {
            // Dropping the session also releases the lock if explicit unlock
            // fails or this future is cancelled.
            let keys = timeout(OPERATION_TIMEOUT, resolve_keys(&owner.client)).await??;
            timeout(
                OPERATION_TIMEOUT,
                owner
                    .client
                    .query_one("SELECT pg_advisory_unlock($1, $2)", &[&keys.0, &keys.1]),
            )
            .await??;
        }
        Ok(())
    }
}

impl Drop for ActiveDaemonLease {
    fn drop(&mut self) {
        self.authority = None;
        self.state.send_replace(LeaseState::Standby);
    }
}

pub(crate) async fn resolve_keys(client: &ClientWrapper) -> Result<(i32, i32)> {
    let row = client
        .query_one(
            "SELECT hashtext($1), hashtext(current_database())",
            &[&NAMESPACE],
        )
        .await?;
    Ok((row.try_get(0)?, row.try_get(1)?))
}
