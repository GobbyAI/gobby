//! Durable, machine-scoped host observations. Epochs have no ordering.
use gobby_core::postgres_pool::Pool;
use uuid::Uuid;

use super::{EpochSnapshot, HostFailure, authority_failure};

#[cfg(test)]
mod tests;

pub(super) struct ObservedHost {
    pub info: super::HostInfo,
    exited: bool,
}

impl From<super::HostInfo> for ObservedHost {
    fn from(info: super::HostInfo) -> Self {
        Self {
            info,
            exited: false,
        }
    }
}

pub(super) async fn record_confirmed_exit(
    inner: &super::Inner,
    pid: u32,
) -> Result<(), HostFailure> {
    let known = inner
        .last_host
        .lock()
        .expect("host observation lock poisoned")
        .clone();
    if let Some(info) = known.filter(|info| info.pid == pid) {
        let machine = Uuid::parse_str(&inner.options.machine_id).map_err(authority_failure)?;
        inner.authority.observe_exit(machine, &info.epoch).await?;
        let mut latest = inner
            .last_host
            .lock()
            .expect("host observation lock poisoned");
        if latest.as_ref() == Some(&info) {
            *latest = None;
        }
    }
    Ok(())
}

pub(super) async fn observe_process_exit(
    inner: &super::Inner,
    known: &mut Option<ObservedHost>,
    child: &mut Option<tokio::process::Child>,
) -> Result<(), HostFailure> {
    let owned_pid = child.as_ref().and_then(tokio::process::Child::id);
    let owned_exited = match child.as_mut() {
        Some(child) => child.try_wait().map_err(super::pre_io)?.is_some(),
        None => false,
    };
    if let Some(observed) = known.as_mut() {
        let info = &observed.info;
        observed.exited = observed.exited
            || (owned_exited && owned_pid == Some(info.pid))
            || process_exited(info.pid)?;
        if observed.exited {
            let machine = Uuid::parse_str(&inner.options.machine_id).map_err(authority_failure)?;
            // Keep the observation until commit succeeds, so storage errors retry.
            inner.authority.observe_exit(machine, &info.epoch).await?;
            *known = None;
            *inner
                .last_host
                .lock()
                .expect("host observation lock poisoned") = None;
        }
    }
    if owned_exited {
        *child = None;
    }
    Ok(())
}

/// EPERM is an existing process; only ESRCH proves absence.
pub(super) fn process_exited(pid: u32) -> Result<bool, HostFailure> {
    let pid = rustix::process::Pid::from_raw(i32::try_from(pid).map_err(authority_failure)?)
        .ok_or_else(|| authority_failure("invalid observed host pid"))?;
    match rustix::process::test_kill_process(pid) {
        Ok(()) | Err(rustix::io::Errno::PERM) => Ok(false),
        Err(rustix::io::Errno::SRCH) => Ok(true),
        Err(error) => Err(super::HostFailure::PostAdoptionIo(error.to_string())),
    }
}

pub(super) async fn observe(
    pool: &Pool,
    machine: Uuid,
    epoch: &str,
    exited: bool,
) -> Result<(), HostFailure> {
    pool.transaction(None, async |tx| {
        // Serialize even the first observation, when no row exists to lock.
        tx.query_one(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 24210))",
            &[&machine.to_string()],
        )
        .await?;
        if exited {
            tx.execute(
                "INSERT INTO terminal_host_epochs(machine_id, epoch, is_current, exited_at)
                 VALUES ($1, $2, false, clock_timestamp())
                 ON CONFLICT (machine_id, epoch) DO UPDATE
                 SET is_current = false,
                     exited_at = COALESCE(terminal_host_epochs.exited_at, EXCLUDED.exited_at)",
                &[&machine, &epoch],
            )
            .await?;
        } else {
            let dead = tx
                .query_opt(
                    "SELECT 1 FROM terminal_host_epochs
                 WHERE machine_id = $1 AND epoch = $2 AND exited_at IS NOT NULL",
                    &[&machine, &epoch],
                )
                .await?;
            anyhow::ensure!(
                dead.is_none(),
                "cannot revive an observed exited host epoch"
            );
            tx.execute(
                "UPDATE terminal_host_epochs SET is_current = false
                 WHERE machine_id = $1 AND is_current AND epoch <> $2",
                &[&machine, &epoch],
            )
            .await?;
            tx.execute(
                "INSERT INTO terminal_host_epochs(machine_id, epoch, is_current)
                 VALUES ($1, $2, true)
                 ON CONFLICT (machine_id, epoch) DO UPDATE SET is_current = true",
                &[&machine, &epoch],
            )
            .await?;
        }
        anyhow::Ok(())
    })
    .await
    .map_err(authority_failure)
}

pub(super) async fn snapshot(pool: &Pool, machine: Uuid) -> Result<EpochSnapshot, HostFailure> {
    let client = pool.get().await.map_err(authority_failure)?;
    // One statement supplies a coherent current/exited view.
    let rows = client
        .query(
            "SELECT epoch, is_current, exited_at IS NOT NULL FROM terminal_host_epochs
         WHERE machine_id = $1 AND (is_current OR exited_at IS NOT NULL)",
            &[&machine],
        )
        .await
        .map_err(authority_failure)?;
    let mut snapshot = EpochSnapshot {
        machine_id: machine,
        current_epoch: None,
        exited_epochs: Default::default(),
    };
    for row in rows {
        let epoch: String = row.try_get(0).map_err(authority_failure)?;
        if row.try_get::<_, bool>(1).map_err(authority_failure)? {
            snapshot.current_epoch = Some(epoch);
        } else if row.try_get::<_, bool>(2).map_err(authority_failure)? {
            snapshot.exited_epochs.insert(epoch);
        }
    }
    Ok(snapshot)
}
