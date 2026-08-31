use std::time::{Duration, Instant};

use anyhow::Context as _;
use postgres::Client;
use sha2::{Digest, Sha256};

use crate::config::Context;
use crate::db;

const MIN_LOCK_POLL: Duration = Duration::from_millis(1);
const ADVISORY_LOCK_DELAY_WARNING_MS_ENV: &str = "GCODE_ADVISORY_LOCK_DELAY_WARNING_MS";
const DEFAULT_ADVISORY_LOCK_DELAY_WARNING_MS: u64 = 30_000;

/// Upper bound for the blocking [`IndexLockPolicy::Wait`] acquisition. Normal
/// index/codewiki lock handoffs complete in seconds; only a genuinely hung
/// holder (e.g. a projection sync wedged on FalkorDB or Qdrant) reaches this
/// cap. Exceeding it fails loudly rather than parking indefinitely in
/// `pg_advisory_lock`, which — exactly like the flush path in #17701 — would
/// otherwise leave an un-reclaimable waiter behind if the process is killed.
const DEFAULT_WAIT_LOCK_TIMEOUT: Duration = Duration::from_secs(1800);
/// Poll cadence for the bounded [`IndexLockPolicy::Wait`] acquisition.
const WAIT_LOCK_POLL: Duration = Duration::from_millis(250);

/// Separates a connection's own application name from the epoch second at which
/// it took the gcode index lock. See [`stamp_lock_acquisition`].
const LOCK_ACQUIRED_TAG: &str = " gcode-index-lock@";
/// PostgreSQL truncates `application_name` at `NAMEDATALEN - 1` bytes.
const APPLICATION_NAME_MAX: usize = 63;
/// Digits held in reserve for the stamped epoch second; ten carry it past 2286.
const LOCK_ACQUIRED_EPOCH_DIGITS: usize = 11;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum IndexLockPolicy {
    Wait {
        max_wait: Duration,
    },
    BriefTry {
        total_wait: Duration,
        poll: Duration,
    },
}

impl IndexLockPolicy {
    pub(crate) fn brief_freshness_try() -> Self {
        Self::maintenance_try()
    }

    pub(crate) fn maintenance_try() -> Self {
        Self::BriefTry {
            total_wait: Duration::from_millis(150),
            poll: Duration::from_millis(25),
        }
    }

    /// Skip-if-busy policy for daemon-triggered per-file index flushes.
    ///
    /// A flush that observes a held index lock (typically a full reindex, which
    /// re-indexes the same files anyway) must yield rather than block on
    /// `pg_advisory_lock`: a blocking waiter that is later SIGKILLed by the
    /// flush timeout leaves a lingering advisory-lock waiter (Postgres does not
    /// notice the client disconnect while a backend is parked in the lock wait),
    /// which is how a single stuck reindex produced a 40+ deep waiter pileup
    /// (#17701). `BriefTry` polls `pg_try_advisory_lock` instead, so a killed
    /// flush is idle between polls and its backend is reclaimed promptly.
    pub(crate) fn brief_index_flush_try() -> Self {
        Self::BriefTry {
            total_wait: Duration::from_secs(3),
            poll: Duration::from_millis(200),
        }
    }

    /// Blocking-until-available policy with a generous safety cap, for the full
    /// index, `init`, and codewiki runs. Poll-based (never a raw blocking
    /// `pg_advisory_lock`) so a killed waiter is reclaimed promptly, and bounded
    /// so a hung holder surfaces as a loud error instead of an indefinite hang.
    pub(crate) fn wait() -> Self {
        Self::Wait {
            max_wait: DEFAULT_WAIT_LOCK_TIMEOUT,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum IndexLockResult<T> {
    Acquired(T),
    /// Contended, carrying the holder description when one was looked up.
    Busy(Option<String>),
}

/// Whether a contended acquisition should pay for holder diagnostics.
///
/// Naming the holder costs one catalog query on the acquiring connection, so
/// callers that would discard the answer ask to stay silent.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum LockDiagnostics {
    /// Identify the holder while a blocking wait drags on, and again when an
    /// acquisition ends busy, so the caller can name who holds the lock instead
    /// of reporting bare contention.
    Report,
    /// Skip the lookup: `--quiet` runs, and per-item maintenance loops where one
    /// query per contended item would cost more than it explains.
    Silent,
}

impl LockDiagnostics {
    pub(crate) fn for_context(quiet: bool) -> Self {
        if quiet { Self::Silent } else { Self::Report }
    }

    fn describe(self, conn: &mut Client, key: i64) -> Option<String> {
        match self {
            Self::Report => describe_lock_holder(conn, key),
            Self::Silent => None,
        }
    }
}

pub(crate) fn with_project_lock<T>(
    ctx: &Context,
    policy: IndexLockPolicy,
    f: impl FnOnce() -> anyhow::Result<T>,
) -> anyhow::Result<IndexLockResult<T>> {
    match acquire_project_lock(ctx, policy)? {
        IndexLockResult::Acquired(_guard) => f().map(IndexLockResult::Acquired),
        IndexLockResult::Busy(holder) => Ok(IndexLockResult::Busy(holder)),
    }
}

pub(crate) fn lock_project_by_id(
    database_url: &str,
    project_id: &str,
    policy: IndexLockPolicy,
    diagnostics: LockDiagnostics,
) -> anyhow::Result<IndexLockResult<ProjectIndexLock>> {
    let key = project_lock_key(project_id);
    let mut conn = db::connect_readwrite(database_url)
        .with_context(|| "failed to connect PostgreSQL hub for gcode index lock")?;

    // The guard is built only once the lock is held. An eagerly built guard was
    // dropped on the busy path, and its `Drop` sent `pg_advisory_unlock` for a
    // lock this session never owned (#21053).
    match try_acquire_project_key(&mut conn, project_id, key, policy, diagnostics)? {
        IndexLockResult::Acquired(()) => Ok(IndexLockResult::Acquired(ProjectIndexLock {
            conn,
            key,
            quiet: true,
        })),
        IndexLockResult::Busy(holder) => Ok(IndexLockResult::Busy(holder)),
    }
}

/// Lease the project lock on a caller-owned connection; released on drop.
///
/// A maintenance loop that locks the same project once per item keeps one
/// hub connection for the whole run instead of opening a fresh TLS session per
/// item: that per-item reconnect parked mid-connect and hung a content GC run
/// (#21085).
pub(crate) fn lease_project_lock<'a>(
    conn: &'a mut Client,
    project_id: &str,
    policy: IndexLockPolicy,
) -> anyhow::Result<Option<ProjectIndexLease<'a>>> {
    let key = project_lock_key(project_id);
    // Silent: content GC leases once per candidate version, so a holder lookup
    // per contended project would cost more than it explains, and the caller
    // only counts busy projects.
    let acquired = matches!(
        try_acquire_project_key(conn, project_id, key, policy, LockDiagnostics::Silent)?,
        IndexLockResult::Acquired(()),
    );
    Ok(acquired.then(|| ProjectIndexLease { conn, key }))
}

fn try_acquire_project_key(
    conn: &mut Client,
    project_id: &str,
    key: i64,
    policy: IndexLockPolicy,
    diagnostics: LockDiagnostics,
) -> anyhow::Result<IndexLockResult<()>> {
    match policy {
        IndexLockPolicy::Wait { max_wait } => {
            // Poll rather than block in `pg_advisory_lock`: a parked blocking
            // waiter is not reclaimed when its client dies (#17701), and an
            // unbounded wait lets one hung holder starve every other index and
            // codewiki run indefinitely. Bounded + poll-based means a killed
            // waiter is idle between polls, and a genuinely hung holder trips
            // the cap and fails loudly below instead of hanging forever.
            let notify_every = match diagnostics {
                LockDiagnostics::Report => Some(advisory_lock_delay_warning()),
                LockDiagnostics::Silent => None,
            };
            if !try_advisory_lock_until(conn, key, max_wait, WAIT_LOCK_POLL, notify_every)? {
                // Name the holder even under `--quiet`: quiet suppresses
                // warnings, and giving up on the lock is an error.
                anyhow::bail!(
                    "gave up acquiring gcode index lock for project {} after {}s; {}",
                    project_id,
                    max_wait.as_secs(),
                    holder_detail(conn, key),
                );
            }
        }
        IndexLockPolicy::BriefTry { total_wait, poll } => {
            if !try_advisory_lock_until(conn, key, total_wait, poll, None)? {
                return Ok(IndexLockResult::Busy(diagnostics.describe(conn, key)));
            }
        }
    }
    stamp_lock_acquisition(conn);
    Ok(IndexLockResult::Acquired(()))
}

fn acquire_project_lock(
    ctx: &Context,
    policy: IndexLockPolicy,
) -> anyhow::Result<IndexLockResult<Box<ProjectIndexLock>>> {
    let started = Instant::now();

    match lock_project_by_id(
        &ctx.database_url,
        &ctx.project_id,
        policy,
        LockDiagnostics::for_context(ctx.quiet),
    )? {
        IndexLockResult::Acquired(mut guard) => {
            guard.quiet = ctx.quiet;
            let elapsed = started.elapsed();
            if !ctx.quiet && elapsed >= advisory_lock_delay_warning() {
                eprintln!(
                    "warning: waited {}ms to acquire gcode index lock",
                    elapsed.as_millis()
                );
            }
            Ok(IndexLockResult::Acquired(Box::new(guard)))
        }
        IndexLockResult::Busy(holder) => Ok(IndexLockResult::Busy(holder)),
    }
}

fn try_advisory_lock_until(
    conn: &mut Client,
    key: i64,
    total_wait: Duration,
    poll: Duration,
    notify_every: Option<Duration>,
) -> anyhow::Result<bool> {
    let started = Instant::now();
    let mut next_notice = notify_every;
    loop {
        if try_advisory_lock(conn, key)? {
            return Ok(true);
        }

        let elapsed = started.elapsed();
        if elapsed >= total_wait {
            return Ok(false);
        }

        // Name the holder while still blocked. The wait cap is generous enough
        // that silence is indistinguishable from a hang, which is what cost a
        // session 20 minutes and a daemon restart (#21233).
        if let Some(due) = next_notice.filter(|due| elapsed >= *due) {
            let notice = format!(
                "waiting for the gcode index lock ({}s elapsed, giving up after {}s); {}",
                elapsed.as_secs(),
                total_wait.as_secs(),
                holder_detail(conn, key),
            );
            log::warn!("{notice}");
            eprintln!("warning: {notice}");
            next_notice = notify_every.map(|interval| due + interval);
        }

        let remaining = total_wait - elapsed;
        let sleep_for = if poll.is_zero() {
            Duration::ZERO
        } else {
            poll.max(MIN_LOCK_POLL).min(remaining)
        };
        if sleep_for.is_zero() {
            // A zero poll interval intentionally means aggressive retry with a
            // scheduler yield only; callers use it only for very short windows.
            std::thread::yield_now();
        } else {
            std::thread::sleep(sleep_for);
        }
    }
}

fn try_advisory_lock(conn: &mut Client, key: i64) -> anyhow::Result<bool> {
    let row = conn
        .query_one("SELECT pg_try_advisory_lock($1)", &[&key])
        .with_context(|| "failed to try gcode index lock")?;
    row.try_get(0).map_err(Into::into)
}

/// Publish, on this connection, the epoch second at which it took the lock.
///
/// `pg_locks` records no grant timestamp and no backend can read another's
/// session state, so `application_name` — the one per-backend field
/// `pg_stat_activity` exposes to every other session — is where a holder leaves
/// the fact contention diagnostics actually need. The instant comes from the
/// server clock, so a reader comparing it against its own `now()` measures one
/// timeline rather than two.
///
/// Advertising a hold time must never fail an acquisition that already
/// succeeded, so failures here are logged and dropped.
fn stamp_lock_acquisition(conn: &mut Client) {
    // `floor`, not a bare `::bigint` cast: casting numeric to bigint rounds to
    // nearest, so an acquisition in the second half of a second would stamp
    // itself in the future and read back as a negative hold.
    //
    // Strip any prior stamp before appending: `lease_project_lock` reuses one
    // caller-owned connection across a backlog of projects, and stamps must not
    // stack. The base name is capped so PostgreSQL's truncation can only eat
    // the caller's own name and never the digits after it — a half-eaten epoch
    // would read as a hold of decades.
    let base_max = i32::try_from(
        APPLICATION_NAME_MAX
            .saturating_sub(LOCK_ACQUIRED_TAG.len())
            .saturating_sub(LOCK_ACQUIRED_EPOCH_DIGITS),
    )
    .unwrap_or(i32::MAX);
    if let Err(error) = conn.execute(
        "SELECT set_config(
                    'application_name',
                    left(split_part(current_setting('application_name'), $1, 1), $2)
                        || $1 || floor(extract(epoch FROM now()))::bigint,
                    false)",
        &[&LOCK_ACQUIRED_TAG, &base_max],
    ) {
        log::debug!("failed to publish the gcode index lock acquisition time: {error}");
    }
}

/// Remove the stamp [`stamp_lock_acquisition`] left, restoring the base name.
fn clear_lock_acquisition_stamp(conn: &mut Client) {
    if let Err(error) = conn.execute(
        "SELECT set_config(
                    'application_name',
                    split_part(current_setting('application_name'), $1, 1),
                    false)",
        &[&LOCK_ACQUIRED_TAG],
    ) {
        log::debug!("failed to clear the gcode index lock acquisition stamp: {error}");
    }
}

/// Best-effort one-line identity of the backend holding `key`, and how long it
/// has held it.
///
/// The hold time is the holder's own stamp ([`stamp_lock_acquisition`]). A
/// holder that never stamped — anything taking the advisory key outside this
/// module — leaves only its connection age, an upper bound the text labels as
/// such rather than passing off as a hold time.
///
/// Diagnostics must never mask the contention they describe, so every failure
/// here returns `None` and the caller falls back to reporting bare contention.
fn describe_lock_holder(conn: &mut Client, key: i64) -> Option<String> {
    // `pg_advisory_lock(bigint)` splits its key across `classid` (high 32 bits)
    // and `objid` (low 32 bits) with `objsubid = 1`. Comparing in that direction
    // keeps the reassembled key out of SQL, where shifting the high word of a
    // negative key back overflows `bigint`.
    let classid = (key >> 32) & 0xFFFF_FFFF;
    let objid = key & 0xFFFF_FFFF;
    let row = conn
        .query_opt(
            "SELECT activity.pid,
                    coalesce(nullif(split_part(activity.application_name, $3, 1), ''), '<unnamed>'),
                    coalesce(activity.state, '<unknown>'),
                    extract(epoch FROM (now() - activity.backend_start))::float8,
                    nullif(split_part(activity.application_name, $3, 2), ''),
                    extract(epoch FROM now())::float8
               FROM pg_locks locks
               JOIN pg_stat_activity activity ON activity.pid = locks.pid
              WHERE locks.locktype = 'advisory'
                AND locks.granted
                AND locks.classid::bigint = $1
                AND locks.objid::bigint = $2
                AND locks.objsubid = 1
                AND activity.datname = current_database()
              LIMIT 1",
            &[&classid, &objid, &LOCK_ACQUIRED_TAG],
        )
        .ok()??;
    let pid: i32 = row.try_get(0).ok()?;
    let application_name: String = row.try_get(1).ok()?;
    let state: String = row.try_get(2).ok()?;
    let connection_age: f64 = row.try_get(3).ok()?;
    let stamp: Option<String> = row.try_get(4).ok()?;
    let server_now: f64 = row.try_get(5).ok()?;
    // Parse the stamp in Rust rather than casting it in SQL: a malformed value
    // would abort the whole query and cost the caller every other field too.
    let held_for = stamp
        .and_then(|stamp| stamp.parse::<i64>().ok())
        .map(|acquired| server_now - acquired as f64)
        .filter(|held| *held >= 0.0);
    let age = match held_for {
        Some(held) => format!("held for {held:.0}s"),
        None => format!("connection age {connection_age:.0}s (acquisition time not recorded)"),
    };
    Some(format!(
        "holder: backend pid {pid}, application_name {application_name:?}, state {state:?}, {age}"
    ))
}

/// The holder description, or a plain statement that none could be read.
fn holder_detail(conn: &mut Client, key: i64) -> String {
    describe_lock_holder(conn, key)
        .unwrap_or_else(|| "the lock holder could not be identified".to_string())
}

pub(crate) fn project_lock_key(project_id: &str) -> i64 {
    // PostgreSQL advisory locks are 64-bit; this truncates SHA-256 and accepts
    // the residual collision risk in exchange for deterministic project locks.
    let mut hasher = Sha256::new();
    hasher.update(b"gcode:index:");
    hasher.update(project_id.as_bytes());
    let digest = hasher.finalize();
    i64::from_be_bytes(
        digest[0..8]
            .try_into()
            .expect("SHA-256 digest has at least 8 bytes"),
    )
}

fn advisory_lock_delay_warning() -> Duration {
    std::env::var(ADVISORY_LOCK_DELAY_WARNING_MS_ENV)
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .map(Duration::from_millis)
        .unwrap_or_else(|| Duration::from_millis(DEFAULT_ADVISORY_LOCK_DELAY_WARNING_MS))
}

pub(crate) struct ProjectIndexLock {
    conn: Client,
    key: i64,
    quiet: bool,
}

/// A project lock held on a borrowed connection; see [`lease_project_lock`].
pub(crate) struct ProjectIndexLease<'a> {
    conn: &'a mut Client,
    key: i64,
}

impl Drop for ProjectIndexLease<'_> {
    fn drop(&mut self) {
        match self
            .conn
            .query_one("SELECT pg_advisory_unlock($1)", &[&self.key])
        {
            Ok(row) if row.try_get::<_, bool>(0).unwrap_or(false) => {}
            Ok(_) => log::debug!("leased gcode index lock was not held during unlock"),
            Err(error) => log::debug!("failed to release leased gcode index lock: {error}"),
        }
        // The borrowed connection outlives this lease, so a stale stamp on it
        // would age into a fictitious hold on whatever it locks next.
        clear_lock_acquisition_stamp(self.conn);
    }
}

impl Drop for ProjectIndexLock {
    fn drop(&mut self) {
        // No stamp to clear, unlike `ProjectIndexLease`: this guard owns its
        // `Client` and the connection closes with it, taking the session
        // setting along. Untagging first would only buy a round trip.
        let result = self
            .conn
            .query_one("SELECT pg_advisory_unlock($1)", &[&self.key]);
        match result {
            Ok(row) => match row.try_get::<_, bool>(0) {
                Ok(true) => {}
                Ok(false) => {
                    log::debug!("gcode index lock was not held during unlock");
                    if !self.quiet {
                        eprintln!("warning: gcode index lock was not held during unlock");
                    }
                }
                Err(error) => {
                    log::debug!("failed to read gcode index unlock result: {error}");
                    if !self.quiet {
                        eprintln!("warning: failed to read gcode index unlock result: {error}");
                    }
                }
            },
            Err(error) => {
                log::debug!("failed to release gcode index lock: {error}");
                if !self.quiet {
                    eprintln!("warning: failed to release gcode index lock: {error}");
                }
            }
        }
    }
}

#[cfg(test)]
#[path = "index_lock/tests.rs"]
mod tests;
