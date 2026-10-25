//! Host upgrade handover (Decision 12): the state file an upgrading image
//! writes before exec and the next image restores from. It names every
//! carried descriptor, so only the socket owner may read it.

use std::fs;
use std::io::{self, Write};
use std::os::fd::RawFd;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use base64::Engine;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::events::HostEvents;
use super::state::Inner;
use crate::pane::{ChildExit, PaneCoreHandover};
use crate::protocol::{ObservationState, PaneLocator};

pub(crate) mod fallback;
pub(crate) mod restore;

pub const FORMAT_VERSION: u32 = 1;
pub const SUPPORTED_FORMAT_VERSIONS: &[u32] = &[FORMAT_VERSION];
/// The state file's name inside the socket directory.
pub const STATE_FILE: &str = "gterm-handover.json";

/// The handover runs between the running release and its successor, so this
/// image must read what the previous release writes. A field added here needs
/// a `serde` default derived from the carried state until that release ships;
/// `tests/fixtures/handover_state/` holds the previous release's file, and
/// `read_state_accepts_the_previous_release_state` fails without the default.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HandoverState {
    pub format_version: u32,
    pub host_epoch: String,
    pub generation: u64,
    pub host_pid: u32,
    /// `CLOCK_MONOTONIC` time, from [`monotonic_now_ns`], after which the
    /// restore must not start.
    pub deadline_monotonic_ns: u64,
    pub attempt: UpgradeAttempt,
    pub previous_image: PathBuf,
    /// The running host's argv without the resume flags.
    pub argv: Vec<String>,
    pub control_listener_fd: RawFd,
    pub frames_listener_fd: RawFd,
    pub next_host_id: u64,
    /// Physical stream IDs cannot be reused while this host epoch survives.
    /// gterm 0.1.15 writes none: no carried pane names an attachment id, and
    /// that release never told a client one, so 1 is past every restored id.
    #[serde(default = "first_attachment")]
    pub next_attachment: u64,
    pub events: CarriedEvents,
    pub panes: Vec<CarriedPane>,
}

/// One past the highest restored attachment id; a host starts at the same id.
fn first_attachment() -> u64 {
    1
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UpgradeAttempt {
    pub attempt_id: String,
    pub candidate_sha256: String,
    pub previous_sha256: String,
}

/// The event cursor and the replay ring, each event with its `seq`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CarriedEvents {
    pub cursor: u64,
    pub ring: Vec<Value>,
}

/// One committed native pane.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CarriedPane {
    pub host_terminal_id: String,
    pub terminal_id: String,
    pub spawn_key: String,
    pub master_fd: RawFd,
    pub pid: u32,
    pub pgid: i32,
    pub start_time: f64,
    pub title: String,
    pub rows: u16,
    pub cols: u16,
    pub pixel_width: u32,
    pub pixel_height: u32,
    pub last_seq: u64,
    pub fingerprint: u64,
    pub observation_state: ObservationState,
    pub observation_generation: u64,
    pub observer_generation: u64,
    pub written_bytes: u64,
    pub dropped_bytes: u64,
    pub total_bytes: u64,
    pub truncated: bool,
    pub input_grant: Option<String>,
    pub locator: Option<PaneLocator>,
    pub reported_cwd: Option<PathBuf>,
    pub reservation_id: String,
    pub reserve_key: String,
    pub reserve_generation: u64,
    pub observer_bind: CarriedObserverBind,
    /// The pane's prepared reservation, when it is still registered.
    pub reservation: Option<CarriedReservation>,
    /// An exit recorded but not yet delivered as `terminal_exited`.
    pub exit: Option<ChildExit>,
    /// The pane terminal's snapshot, from [`encode_snapshot`].
    pub snapshot_b64: String,
    pub core: PaneCoreHandover,
}

/// A slot's observer bind. A bind held by a live attachment is carried as
/// `Entitled`: every attachment closes at exec.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub enum CarriedObserverBind {
    None,
    Reserved {
        reservation_id: String,
        generation: u64,
    },
    Entitled {
        reservation_id: String,
        generation: u64,
    },
}

/// A prepared reservation, carried without the connection that made it.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CarriedReservation {
    pub id: String,
    pub key: String,
    pub generation: u64,
    pub terminal_id: String,
    pub identity: Option<CarriedIdentity>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CarriedIdentity {
    pub terminal_id: String,
    pub spawn_key: String,
}

/// How an upgrade attempt ended in the image that reports it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum UpgradeOutcome {
    Succeeded,
    /// The new image failed its restore and this earlier image resumed.
    Fallback(RestoreFailure),
    /// The candidate failed its resume probe; nothing was quiesced.
    Refused,
    /// The write guard was not free within its retry, or the recheck under
    /// it found the host busy or draining.
    Deferred(UpgradeReason),
    /// A step before exec failed and every pane resumed.
    Aborted(UpgradeReason),
    /// `execve` failed with `errno` and every pane resumed.
    RolledBack {
        errno: i32,
    },
}

/// Why an attempt deferred or aborted, as `ping` and `host_upgrade_failed`
/// name it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum UpgradeReason {
    HostBusy,
    HostDraining,
    QuiesceTimeout,
    SoftDeadline,
    CaptureFailed,
    StateWriteFailed,
    /// The pin no longer hashes to the candidate the probe accepted.
    PinMismatch,
}

impl UpgradeReason {
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            UpgradeReason::HostBusy => "host_busy",
            UpgradeReason::HostDraining => "host_draining",
            UpgradeReason::QuiesceTimeout => "quiesce_timeout",
            UpgradeReason::SoftDeadline => "soft_deadline",
            UpgradeReason::CaptureFailed => "capture_failed",
            UpgradeReason::StateWriteFailed => "state_write_failed",
            UpgradeReason::PinMismatch => "pin_mismatch",
        }
    }
}

/// Why a new image's restore failed, as the fallback's outcome names it. The
/// failing image hands it to the earlier one through [`RESTORE_FAILURE_ENV`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RestoreFailure {
    /// The state file did not parse as this image's handover state.
    StateUnreadable,
    /// The image rejected the argv the earlier image recorded.
    ArgsRejected,
    /// The control token or host config was unusable.
    SetupFailed,
    /// Verifying the state or staging a pane, listener, or pin failed.
    StageFailed,
    Panicked,
    /// The failing image named no reason, as releases before 0.1.17 do.
    Unreported,
}

/// The environment variable carrying a [`RestoreFailure`] across the fallback
/// exec. An argument would not do: the earlier image refuses unknown ones.
pub(crate) const RESTORE_FAILURE_ENV: &str = "GTERM_RESTORE_FAILURE";

impl RestoreFailure {
    const ALL: [RestoreFailure; 6] = [
        RestoreFailure::StateUnreadable,
        RestoreFailure::ArgsRejected,
        RestoreFailure::SetupFailed,
        RestoreFailure::StageFailed,
        RestoreFailure::Panicked,
        RestoreFailure::Unreported,
    ];

    pub(crate) fn as_str(self) -> &'static str {
        match self {
            RestoreFailure::StateUnreadable => "state_unreadable",
            RestoreFailure::ArgsRejected => "args_rejected",
            RestoreFailure::SetupFailed => "setup_failed",
            RestoreFailure::StageFailed => "stage_failed",
            RestoreFailure::Panicked => "restore_panicked",
            RestoreFailure::Unreported => "restore_failed",
        }
    }

    /// The reason a failing image reported, or [`RestoreFailure::Unreported`].
    pub(crate) fn reported(value: Option<&str>) -> Self {
        Self::ALL
            .into_iter()
            .find(|failure| Some(failure.as_str()) == value)
            .unwrap_or(RestoreFailure::Unreported)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct UpgradeRecord {
    pub(crate) attempt: UpgradeAttempt,
    pub(crate) outcome: Option<UpgradeOutcome>,
}

/// Host-wide state rebuilt from a handover, ready for `HostState::restored`.
pub(crate) struct CarriedHost {
    pub(crate) host_epoch: String,
    pub(crate) generation: u64,
    pub(crate) upgrade: UpgradeRecord,
    pub(crate) events: HostEvents,
    pub(crate) inner: Inner,
}

pub fn encode_snapshot(snapshot: &[u8]) -> String {
    base64::engine::general_purpose::STANDARD.encode(snapshot)
}

fn decode_snapshot(snapshot_b64: &str) -> io::Result<Vec<u8>> {
    base64::engine::general_purpose::STANDARD
        .decode(snapshot_b64)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, format!("pane snapshot: {err}")))
}

/// `CLOCK_MONOTONIC` in nanoseconds; it keeps counting across `execve`.
pub fn monotonic_now_ns() -> u64 {
    // SAFETY: timespec is plain data that clock_gettime fills.
    let mut now: libc::timespec = unsafe { std::mem::zeroed() };
    // SAFETY: now is a valid out-pointer and CLOCK_MONOTONIC always exists.
    unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut now) };
    (now.tv_sec as u64)
        .saturating_mul(1_000_000_000)
        .saturating_add(now.tv_nsec as u64)
}

/// Writes the state file durably with mode 0600: `stage_state`, then
/// `commit_state`.
pub fn write_state(path: &Path, state: &HandoverState) -> io::Result<()> {
    commit_state(&stage_state(path, state)?, path)
}

/// Writes `state` to a private temporary file beside `path` and `fsync`s it.
/// Nothing reads the temporary file until `commit_state` puts it in place.
pub fn stage_state(path: &Path, state: &HandoverState) -> io::Result<PathBuf> {
    let temp = staged_path(path)?;
    let _ = fs::remove_file(&temp);
    let written = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)?;
        serde_json::to_writer(&mut file, state)?;
        file.flush()?;
        file.sync_all()
    })();
    match written {
        Ok(()) => Ok(temp),
        Err(err) => {
            let _ = fs::remove_file(&temp);
            Err(err)
        }
    }
}

/// Renames a staged state file over `path`, then `fsync`s the directory.
pub fn commit_state(staged: &Path, path: &Path) -> io::Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no parent"))?;
    let committed = fs::rename(staged, path).and_then(|()| fs::File::open(dir)?.sync_all());
    if committed.is_err() {
        let _ = fs::remove_file(staged);
    }
    committed
}

/// Removes the state file at `path` and any staged temporary file for it.
pub fn remove_state(path: &Path) {
    let _ = fs::remove_file(path);
    if let Ok(temp) = staged_path(path) {
        let _ = fs::remove_file(temp);
    }
}

fn staged_path(path: &Path) -> io::Result<PathBuf> {
    let dir = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no parent"))?;
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no name"))?;
    Ok(dir.join(format!(
        ".{}.{}.tmp",
        name.to_string_lossy(),
        std::process::id()
    )))
}

/// Reads a state file, refusing a format this image does not support.
pub fn read_state(path: &Path) -> io::Result<HandoverState> {
    parse_state(state_value(path)?)
}

/// A state file as JSON, before it is checked against this image's types.
pub(crate) fn state_value(path: &Path) -> io::Result<Value> {
    serde_json::from_slice(&fs::read(path)?)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))
}

pub(crate) fn parse_state(value: Value) -> io::Result<HandoverState> {
    let version = value.get("format_version").and_then(Value::as_u64);
    if !version.is_some_and(|version| {
        SUPPORTED_FORMAT_VERSIONS
            .iter()
            .any(|supported| u64::from(*supported) == version)
    }) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!("unsupported handover format {version:?}"),
        ));
    }
    serde_json::from_value(value).map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))
}

#[cfg(test)]
mod tests;
