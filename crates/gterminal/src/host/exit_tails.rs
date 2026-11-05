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
    text: String,
}

/// Bounded, time-limited final text of retired native terminals.
#[derive(Default)]
pub(crate) struct ExitTails {
    tails: VecDeque<Tail>,
}

impl ExitTails {
    pub(crate) fn record(&mut self, host_terminal_id: &str, text: String, now: Instant) {
        self.tails
            .retain(|tail| now.duration_since(tail.retired_at) < RETENTION);
        if self.tails.len() == CAPACITY {
            self.tails.pop_front();
        }
        self.tails.push_back(Tail {
            host_terminal_id: host_terminal_id.to_owned(),
            retired_at: now,
            text,
        });
    }

    pub(crate) fn get(&self, host_terminal_id: &str, now: Instant) -> Option<&str> {
        self.tails
            .iter()
            .rev()
            .find(|tail| {
                tail.host_terminal_id == host_terminal_id
                    && now.duration_since(tail.retired_at) < RETENTION
            })
            .map(|tail| tail.text.as_str())
    }
}

/// The text a slot printed last, bounded to the snapshot limits.
#[cfg(feature = "vt-engine")]
pub(crate) fn final_text(slot: &TerminalSlot) -> String {
    use crate::protocol::SnapshotMode;

    let Some(child) = slot.child.as_ref() else {
        return String::new();
    };
    let text = child
        .runtime
        .snapshot_history(SnapshotMode::Text)
        .unwrap_or_else(|| child.runtime.visible_snapshot(SnapshotMode::Text));
    bounded_tail(&text)
}

#[cfg(not(feature = "vt-engine"))]
pub(crate) fn final_text(_slot: &TerminalSlot) -> String {
    String::new()
}

/// The last `SNAPSHOT_DEFAULT_MAX_LINES` lines within `SNAPSHOT_DEFAULT_MAX_BYTES`.
#[cfg(feature = "vt-engine")]
fn bounded_tail(text: &str) -> String {
    use crate::protocol::{SNAPSHOT_DEFAULT_MAX_BYTES, SNAPSHOT_DEFAULT_MAX_LINES};

    let mut start = text.len().saturating_sub(SNAPSHOT_DEFAULT_MAX_BYTES);
    while !text.is_char_boundary(start) {
        start += 1;
    }
    let lines: Vec<&str> = text[start..].lines().collect();
    lines[lines.len().saturating_sub(SNAPSHOT_DEFAULT_MAX_LINES)..].join("\n")
}

#[cfg(test)]
#[path = "exit_tails/tests.rs"]
mod tests;
