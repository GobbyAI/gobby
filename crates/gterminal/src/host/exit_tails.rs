//! Final text of retired native terminals.
//!
//! The host drops a native slot when its child exits, and the slot holds the
//! only copy of what the child printed. A caller that learns of the exit a
//! poll late would otherwise read nothing, so the host keeps each retired
//! terminal's last text for a short window, like tmux's remain-on-exit.

use std::collections::VecDeque;
use std::time::{Duration, Instant};

use super::state::TerminalSlot;

/// How long a retired terminal's final text stays readable.
const RETENTION: Duration = Duration::from_secs(60);
/// Retired terminals kept at once; the oldest goes first.
const CAPACITY: usize = 64;

struct Tail {
    host_terminal_id: String,
    retired_at: Instant,
    snapshot: Result<super::snapshot::SnapshotBody, &'static str>,
}

/// Bounded, time-limited final text of retired native terminals.
#[derive(Default)]
pub(crate) struct ExitTails {
    tails: VecDeque<Tail>,
}

impl ExitTails {
    #[cfg(test)]
    pub(crate) fn record(&mut self, host_terminal_id: &str, text: String, now: Instant) {
        let snapshot = super::snapshot::truncate_snapshot(&text, usize::MAX, usize::MAX);
        self.record_snapshot(host_terminal_id, Ok(snapshot), now);
    }

    pub(super) fn record_snapshot(
        &mut self,
        host_terminal_id: &str,
        snapshot: Result<super::snapshot::SnapshotBody, &'static str>,
        now: Instant,
    ) {
        self.tails
            .retain(|tail| now.duration_since(tail.retired_at) < RETENTION);
        if self.tails.len() == CAPACITY {
            self.tails.pop_front();
        }
        self.tails.push_back(Tail {
            host_terminal_id: host_terminal_id.to_owned(),
            retired_at: now,
            snapshot,
        });
    }

    #[cfg(test)]
    pub(crate) fn get(&self, host_terminal_id: &str, now: Instant) -> Option<&str> {
        self.get_snapshot(host_terminal_id, now)
            .and_then(|snapshot| snapshot.as_ref().ok())
            .map(|snapshot| snapshot.text.as_str())
    }

    pub(super) fn get_snapshot(
        &self,
        host_terminal_id: &str,
        now: Instant,
    ) -> Option<&Result<super::snapshot::SnapshotBody, &'static str>> {
        self.tails
            .iter()
            .rev()
            .find(|tail| {
                tail.host_terminal_id == host_terminal_id
                    && now.duration_since(tail.retired_at) < RETENTION
            })
            .map(|tail| &tail.snapshot)
    }
}

/// The text a slot printed last, bounded to the snapshot limits.
#[cfg(feature = "vt-engine")]
pub(super) fn final_text(
    slot: &TerminalSlot,
) -> Result<super::snapshot::SnapshotBody, &'static str> {
    use crate::protocol::SnapshotMode;

    let Some(child) = slot.child.as_ref() else {
        return Ok(super::snapshot::truncate_snapshot("", 0, 0));
    };
    let terminal = child.runtime.terminal_handle();
    match terminal.begin_history_capture().and_then(|capture| {
        capture.read(
            SnapshotMode::Text,
            crate::protocol::SNAPSHOT_DEFAULT_MAX_LINES,
            crate::protocol::SNAPSHOT_DEFAULT_MAX_BYTES,
        )
    }) {
        Ok(snapshot) => Ok(super::snapshot::SnapshotBody {
            text: snapshot.text,
            truncated: snapshot.truncated,
            total_bytes: snapshot.total_bytes,
            dropped_bytes: snapshot.dropped_bytes,
        }),
        Err(code) => Err(code),
    }
}

impl super::state::HostState {
    pub(super) async fn retain_exit_tail(&self, slot: Option<TerminalSlot>) {
        let Some(slot) = slot else {
            return;
        };
        #[cfg(feature = "vt-engine")]
        if let Some(drain) = slot
            .child
            .as_ref()
            .and_then(|child| child.runtime.request_output_drain())
        {
            let _ = tokio::time::timeout(Duration::from_secs(1), drain).await;
        }
        let captured = tokio::task::spawn_blocking(move || {
            let text = final_text(&slot);
            (slot.host_terminal_id.clone(), text)
        })
        .await;
        match captured {
            Ok((host_id, snapshot)) => self.inner.lock().await.exit_tails.record_snapshot(
                &host_id,
                snapshot,
                Instant::now(),
            ),
            Err(error) => tracing::warn!(%error, "final history capture failed"),
        }
    }

    #[cfg(feature = "vt-engine")]
    pub(super) async fn capture_exit_tail(
        &self,
        host_id: &str,
    ) -> Option<Result<super::snapshot::SnapshotBody, &'static str>> {
        let capture = {
            let inner = self.inner.lock().await;
            let slot = inner
                .by_host_id
                .get(host_id)
                .and_then(|id| inner.terminals.get(id))?;
            slot.child
                .as_ref()?
                .runtime
                .terminal_handle()
                .begin_history_capture()
        };
        let capture = match capture {
            Ok(capture) => capture,
            Err(code) => return Some(Err(code)),
        };
        match tokio::task::spawn_blocking(move || {
            capture.read(
                crate::protocol::SnapshotMode::Text,
                crate::protocol::SNAPSHOT_DEFAULT_MAX_LINES,
                crate::protocol::SNAPSHOT_DEFAULT_MAX_BYTES,
            )
        })
        .await
        {
            Ok(Ok(snapshot)) => Some(Ok(super::snapshot::SnapshotBody {
                text: snapshot.text,
                truncated: snapshot.truncated,
                total_bytes: snapshot.total_bytes,
                dropped_bytes: snapshot.dropped_bytes,
            })),
            Ok(Err(code)) => {
                tracing::debug!(code, "exit history capture refused");
                Some(Err(code))
            }
            Err(error) => {
                tracing::warn!(%error, "exit history capture failed");
                Some(Err("capture_failed"))
            }
        }
    }

    #[cfg(all(test, not(feature = "vt-engine")))]
    pub(super) async fn capture_exit_tail(
        &self,
        _host_id: &str,
    ) -> Option<Result<super::snapshot::SnapshotBody, &'static str>> {
        None
    }
}

#[cfg(not(feature = "vt-engine"))]
pub(super) fn final_text(
    _slot: &TerminalSlot,
) -> Result<super::snapshot::SnapshotBody, &'static str> {
    Ok(super::snapshot::truncate_snapshot("", 0, 0))
}

#[cfg(test)]
#[path = "exit_tails/tests.rs"]
mod tests;
