//! Database-wide singleton authority, independent of the live serve lifecycle.
//!
//! Call `heartbeat` periodically (the lifecycle layer owns scheduling). Every
//! operation is bounded; failure discards the dedicated session and its fence.
//! Fences must never be cached by consumers beyond the owner's live authority.

#[cfg(unix)]
pub mod admin_api;
mod named_lock;
pub mod standby;

pub use named_lock::{NamedGuard, NamedLock};

use anyhow::{Result, bail};
use deadpool_postgres::ClientWrapper;
use gobby_core::postgres_pool::Pool;
use rand::RngCore;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::watch;
use tokio::time::timeout;
use uuid::Uuid;

pub(crate) const NAMESPACE: &str = "gobby-single-active-daemon-v1";
pub(crate) const APPLICATION_PREFIX: &str = "gobby-lease-v1:";
const OPERATION_TIMEOUT: Duration = Duration::from_secs(5);

/// Why every other acquisition is refused while a lease lifetime is open.
pub(crate) const LIFETIME_OPEN: &str = "this daemon's lease lifetime has not ended";

/// Node mode has no local hub authority, even if database credentials exist.
pub use gobby_core::bootstrap::RunMode as LeaseMode;

/// The lease state families read through their context is the one this authority publishes.
pub use gobby_core::family::LeaseState;

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

/// Publishes the lease as lost without taking its lock, so a monitor that cannot reach the lease
/// still fails closed. Authority published as lost is never reused: the next acquisition
/// discards it and acquires afresh.
#[derive(Clone)]
pub struct LeaseLoss(watch::Sender<LeaseState>);

impl LeaseLoss {
    /// Lost only ever replaces Active, so a publish after the release leaves Standby.
    pub fn publish(&self) {
        self.0.send_if_modified(|state| {
            let held = *state == LeaseState::Active;
            if held {
                *state = LeaseState::Lost;
            }
            held
        });
    }
}

/// One lifetime of the services that run under this lease. While it is open, nothing else
/// acquires. Dropping it without passing it to the next `begin_lifetime` abandons it: that
/// lifetime never ends and acquisition stays refused for the rest of the process.
#[must_use = "a dropped lifetime is abandoned: acquisition stays refused in this process"]
pub struct LeaseLifetime {
    loss: LeaseLoss,
    ended: bool,
}

impl LeaseLifetime {
    /// Publishes Lost if the watch still reads Active.
    pub fn lose(&self) {
        self.loss.publish();
    }
}

impl Drop for LeaseLifetime {
    fn drop(&mut self) {
        if !self.ended {
            self.loss.publish();
        }
    }
}

struct LossOnDrop(Option<LeaseLoss>);

impl Drop for LossOnDrop {
    fn drop(&mut self) {
        if let Some(loss) = &self.0 {
            loss.publish();
        }
    }
}

/// Owns the dedicated lock session. Dropping it closes that session, never
/// returning it to the connection pool. Release invalidates authority first.
pub struct ActiveDaemonLease {
    pool: Arc<Pool>,
    application_name: String,
    deployment_token: String,
    authority: Option<Authority>,
    state: watch::Sender<LeaseState>,
    lifetime_open: bool,
}

impl ActiveDaemonLease {
    pub fn new(
        pool: Arc<Pool>,
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
        let (state, _) = watch::channel(LeaseState::Standby);
        Ok(Self {
            pool,
            application_name,
            deployment_token: deployment_token.into(),
            authority: None,
            state,
            lifetime_open: false,
        })
    }

    /// Whether a lifetime begun by `begin_lifetime` has not yet been ended by the next one.
    pub fn lifetime_open(&self) -> bool {
        self.lifetime_open
    }

    pub fn subscribe(&self) -> watch::Receiver<LeaseState> {
        self.state.subscribe()
    }

    /// The handle that publishes this lease as lost; it needs no lease lock to publish.
    pub fn loss(&self) -> LeaseLoss {
        LeaseLoss(self.state.clone())
    }

    pub fn application_name(&self) -> &str {
        &self.application_name
    }

    /// None unless the session is open and the watch reads Active.
    pub fn fence(&self) -> Option<&Fence> {
        self.authority
            .as_ref()
            .filter(|owner| !owner.client.is_closed())
            .filter(|_| *self.state.borrow() == LeaseState::Active)
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

    /// Acquisition outside the run loop, refused while a lease lifetime is open.
    pub async fn try_acquire(&mut self) -> Result<bool> {
        if self.lifetime_open {
            bail!(LIFETIME_OPEN);
        }
        self.acquire().await
    }

    /// A failed lock attempt never writes deployment_runtime. Cancellation
    /// before authority installation drops the detached connection and lock.
    /// Every `Err` and `Ok(false)` leaves the slot empty.
    async fn acquire(&mut self) -> Result<bool> {
        if self.authority.is_some() {
            if *self.state.borrow() != LeaseState::Lost {
                self.heartbeat().await?;
                return Ok(true);
            }
            // A loss published through `LeaseLoss` leaves the session; its authority is spent,
            // so it is released before a fresh acquisition rotates the fence.
            self.release().await?;
        }
        // One bound covers checkout, connect and the lock attempt, so a waiting pool
        // cannot push an attempt past the operation bound.
        let attempt = timeout(OPERATION_TIMEOUT, async {
            let mut client = self.connect(&self.application_name).await?;
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
            anyhow::Ok(Some((client, fence)))
        })
        .await??;
        let Some((client, fence)) = attempt else {
            return Ok(false);
        };
        self.authority = Some(Authority { client, fence });
        self.state.send_replace(LeaseState::Active);
        Ok(true)
    }

    /// The run loop's only acquisition. Ends `previous` and begins the next lifetime in one
    /// critical section, so the gate is never clear over a spent authority.
    pub async fn begin_lifetime(
        &mut self,
        previous: Option<LeaseLifetime>,
    ) -> Result<Option<LeaseLifetime>> {
        if self.lifetime_open && previous.is_none() {
            bail!(LIFETIME_OPEN);
        }
        // `previous` is still unended here: a cancelled acquisition abandons it. The result is
        // bound, not propagated, so the `Err` path ends `previous` and clears the gate too.
        let acquired = self.acquire().await;
        if let Some(mut previous) = previous {
            previous.ended = true;
        }
        self.lifetime_open = matches!(acquired, Ok(true));
        Ok(acquired?.then(|| LeaseLifetime {
            loss: self.loss(),
            ended: false,
        }))
    }

    /// Removes authority while awaiting I/O, so cancelling this operation also
    /// fails closed instead of retaining an unverified lease session. A lease published
    /// Lost fails without touching its session.
    pub async fn heartbeat(&mut self) -> Result<()> {
        if self.authority.is_some() && *self.state.borrow() == LeaseState::Lost {
            bail!("active-daemon lease was published as lost");
        }
        let Some(owner) = self.authority.take() else {
            bail!("active-daemon lease is not held");
        };
        let mut loss = LossOnDrop(Some(self.loss()));
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
