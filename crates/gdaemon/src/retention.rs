//! Hub-global token-event retention. Raw details expire; lifetime usage does not.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use anyhow::{Context, Result};
use gobby_core::postgres_pool::{DedicatedSession, Pool};
use tokio::sync::watch;

const OWNER_LOCK: &str = "hub-retention:token-events";
const BATCH_SIZE: u64 = 10_000;
const MAX_BATCHES: usize = 20;
const BATCH_YIELD: Duration = Duration::from_millis(100);
const DAY_SECONDS: u64 = 24 * 60 * 60;
const DAILY_RUN_SECONDS: u64 = 3 * 60 * 60 + 30 * 60;
const PRUNE_SQL: &str = include_str!("retention/prune.sql");

#[derive(Debug, Default, PartialEq, Eq)]
struct CycleReport {
    owner: bool,
    deleted: u64,
    batches: usize,
}

/// Startup jitter spreads connection attempts; the advisory lock elects the owner.
pub(crate) async fn run(pool: Pool, machine_id: uuid::Uuid, mut stop: watch::Receiver<bool>) {
    let mut delay = Duration::from_secs((machine_id.as_u128() % 901) as u64);
    loop {
        if wait_or_stop(&mut stop, delay).await {
            return;
        }
        match run_cycle(&pool, &mut stop).await {
            Ok(report) => eprintln!(
                "gdaemon token_events retention owner={} deleted={} batches={}",
                report.owner, report.deleted, report.batches
            ),
            Err(error) => eprintln!("gdaemon token_events retention failed: {error:#}"),
        }
        delay = until_daily_run(SystemTime::now());
    }
}

async fn run_cycle(pool: &Pool, stop: &mut watch::Receiver<bool>) -> Result<CycleReport> {
    let session = pool.dedicated_session().await?;
    session
        .simple_query("SET statement_timeout = '30s'; SET lock_timeout = '2s'")
        .await?;
    let rows = session
        .query(
            "SELECT (clock_timestamp() - INTERVAL '180 days')::text",
            &[],
        )
        .await?;
    let cutoff: String = rows[0].try_get(0)?;
    cycle_at(&session, &cutoff, stop).await
}

async fn cycle_at(
    session: &DedicatedSession,
    cutoff: &str,
    stop: &mut watch::Receiver<bool>,
) -> Result<CycleReport> {
    if stopping(stop) || !session.try_session_lock(OWNER_LOCK).await? {
        return Ok(CycleReport::default());
    }
    // DedicatedSession discards its connection on drop, releasing the cycle lock
    // on success, error, or cancellation without lending it to another borrower.
    let mut report = CycleReport {
        owner: true,
        ..CycleReport::default()
    };
    for index in 0..MAX_BATCHES {
        if stopping(stop) {
            break;
        }
        let deleted = prune_batch(session, cutoff).await?;
        if deleted == 0 {
            break;
        }
        report.deleted += deleted;
        report.batches += 1;
        eprintln!(
            "gdaemon token_events retention batch={} deleted={deleted}",
            report.batches
        );
        if deleted < BATCH_SIZE || index + 1 == MAX_BATCHES {
            break;
        }
        // Shutdown is checked between transactions, never by cancelling a batch.
        if wait_or_stop(stop, BATCH_YIELD).await {
            break;
        }
    }
    Ok(report)
}

async fn prune_batch(session: &DedicatedSession, cutoff: &str) -> Result<u64> {
    let rows = session
        .query(PRUNE_SQL, &[&cutoff])
        .await
        .context("archive and delete expired token-event batch")?;
    let count: i64 = rows[0].try_get(0)?;
    u64::try_from(count).context("invalid token-event deletion count")
}

fn stopping(stop: &watch::Receiver<bool>) -> bool {
    *stop.borrow() || stop.has_changed().is_err()
}

async fn wait_or_stop(stop: &mut watch::Receiver<bool>, delay: Duration) -> bool {
    if stopping(stop) {
        return true;
    }
    tokio::select! {
        _ = stop.changed() => true,
        () = tokio::time::sleep(delay) => false,
    }
}

fn until_daily_run(now: SystemTime) -> Duration {
    let seconds = now.duration_since(UNIX_EPOCH).unwrap_or_default().as_secs();
    let today = seconds / DAY_SECONDS * DAY_SECONDS + DAILY_RUN_SECONDS;
    let next = if today > seconds {
        today
    } else {
        today + DAY_SECONDS
    };
    Duration::from_secs(next - seconds)
}

#[cfg(test)]
#[path = "retention/tests.rs"]
mod tests;
