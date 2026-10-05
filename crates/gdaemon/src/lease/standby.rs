//! Read-only inspection and explicitly requested verified stale recovery.
//! Native HTTP admission and mode integration belong to the lifecycle cutover.

use super::{APPLICATION_PREFIX, ActiveDaemonLease, resolve_keys};
use anyhow::{Result, anyhow, bail};
use deadpool_postgres::ClientWrapper;
use std::time::Duration;
use tokio::time::{sleep, timeout};

const RECOVERY_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Debug, Clone, PartialEq)]
pub struct LeaseOwner {
    pub pid: i32,
    pub application_name: String,
    pub heartbeat_age_seconds: f64,
}

impl ActiveDaemonLease {
    pub async fn owner(&self) -> Result<Option<LeaseOwner>> {
        timeout(RECOVERY_TIMEOUT, async {
            let client = self.connect("gobby-lease-probe").await?;
            read_owner(&client, resolve_keys(&client).await?).await
        })
        .await?
    }

    /// Never promotes. Termination rechecks PID, application identity,
    /// freshness and the exact database-wide lock in a single statement.
    pub async fn recover_stale_owner(&self, stale_after: Duration) -> Result<LeaseOwner> {
        let (client, keys, owner) = timeout(RECOVERY_TIMEOUT, async {
            let client = self.connect("gobby-lease-probe").await?;
            let keys = resolve_keys(&client).await?;
            let Some(owner) = read_owner(&client, keys).await? else {
                bail!("active-daemon lease has no owner");
            };
            if !owner.application_name.starts_with(APPLICATION_PREFIX) {
                bail!("active-daemon lease holder has an unrecognized application identity");
            }
            let seconds = stale_after.as_secs_f64();
            if owner.heartbeat_age_seconds < seconds {
                bail!("active-daemon lease owner is still fresh");
            }
            let class_id = i64::from(keys.0 as u32);
            let object_id = i64::from(keys.1 as u32);
            let rows = client
                .query(
                    "SELECT pg_terminate_backend(activity.pid) \
                     FROM pg_stat_activity AS activity \
                     WHERE activity.pid = $1 AND activity.application_name = $2 \
                     AND clock_timestamp() - activity.state_change >= make_interval(secs => $3) \
                     AND EXISTS (SELECT 1 FROM pg_locks AS locks \
                       WHERE locks.pid = activity.pid AND locks.database = \
                       (SELECT oid FROM pg_database WHERE datname = current_database()) \
                       AND locks.locktype = 'advisory' AND locks.granted AND locks.objsubid = 2 \
                       AND locks.classid::bigint = $4 AND locks.objid::bigint = $5)",
                    &[
                        &owner.pid,
                        &owner.application_name,
                        &seconds,
                        &class_id,
                        &object_id,
                    ],
                )
                .await?;
            if rows
                .first()
                .map(|row| row.try_get::<_, bool>(0))
                .transpose()?
                != Some(true)
            {
                bail!("lease owner changed or refreshed during stale recovery verification");
            }
            anyhow::Ok((client, keys, owner))
        })
        .await??;
        // Give release its full budget after termination, independently of
        // the bounded verification/termination phase above.
        timeout(RECOVERY_TIMEOUT, async {
            loop {
                if read_owner(&client, keys).await?.is_none() {
                    return Ok(owner);
                }
                sleep(Duration::from_millis(50)).await;
            }
        })
        .await
        .map_err(|_| anyhow!("stale lease owner did not release within recovery timeout"))?
    }
}

async fn read_owner(client: &ClientWrapper, keys: (i32, i32)) -> Result<Option<LeaseOwner>> {
    let class_id = i64::from(keys.0 as u32);
    let object_id = i64::from(keys.1 as u32);
    let row = client
        .query_opt(
            "SELECT activity.pid, activity.application_name, \
             EXTRACT(EPOCH FROM (clock_timestamp() - activity.state_change))::double precision \
             FROM pg_locks AS locks JOIN pg_stat_activity AS activity ON activity.pid = locks.pid \
             WHERE locks.database = (SELECT oid FROM pg_database WHERE datname = current_database()) \
             AND locks.locktype = 'advisory' AND locks.granted AND locks.objsubid = 2 \
             AND locks.classid::bigint = $1 AND locks.objid::bigint = $2",
            &[&class_id, &object_id],
        )
        .await?;
    row.map(|row| {
        Ok(LeaseOwner {
            pid: row.try_get(0)?,
            application_name: row.try_get(1)?,
            heartbeat_age_seconds: row.try_get(2)?,
        })
    })
    .transpose()
}
