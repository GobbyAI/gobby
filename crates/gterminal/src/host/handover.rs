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
    pub events: CarriedEvents,
    pub panes: Vec<CarriedPane>,
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
    Fallback,
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

/// Writes the state file durably with mode 0600: a private temporary file,
/// `fsync`, rename over `path`, then `fsync` of the directory.
pub fn write_state(path: &Path, state: &HandoverState) -> io::Result<()> {
    let dir = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no parent"))?;
    let name = path
        .file_name()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "state path has no name"))?;
    let temp = dir.join(format!(
        ".{}.{}.tmp",
        name.to_string_lossy(),
        std::process::id()
    ));
    let _ = fs::remove_file(&temp);
    let written = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)?;
        serde_json::to_writer(&mut file, state)?;
        file.flush()?;
        file.sync_all()?;
        fs::rename(&temp, path)?;
        fs::File::open(dir)?.sync_all()
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temp);
    }
    written
}

/// Reads a state file, refusing a format this image does not support.
pub fn read_state(path: &Path) -> io::Result<HandoverState> {
    let bytes = fs::read(path)?;
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|err| io::Error::new(io::ErrorKind::InvalidData, err))?;
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
