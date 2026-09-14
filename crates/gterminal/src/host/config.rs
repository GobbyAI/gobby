//! Validated host resource bounds. Values outside a range refuse startup.

use std::io;
use std::time::Duration;

use crate::protocol::{
    CONTROL_DELIVERY_DEADLINE_MS, CONTROL_QUEUE_ENTRIES, DEFAULT_MAX_ATTACHED_TERMINALS,
    DEFAULT_MAX_ATTACHMENTS_PER_TERMINAL, DEFAULT_MAX_ATTACHMENTS_TOTAL,
    DEFAULT_NATIVE_SCROLLBACK_MAX_BYTES, DEFAULT_NATIVE_SCROLLBACK_MAX_LINES,
    DEFAULT_TMUX_ATTACH_HISTORY_LINES, DEFAULT_TMUX_ATTACH_HISTORY_MAX_BYTES,
    DEFAULT_TMUX_POLL_BACKOFF_CEILING_MS, DEFAULT_TMUX_POLL_INTERVAL_MS, DELTA_LAG_TIMEOUT_MS,
    DELTA_QUEUE_BYTES, EVENT_QUEUE_BYTES, MAX_CONTROL_DEADLINE_MS, MAX_CONTROL_QUEUE_ENTRIES,
    MAX_DELTA_QUEUE_BYTES, MAX_EVENT_QUEUE_BYTES, MAX_LAG_TIMEOUT_MS, MAX_MAX_ATTACHED_TERMINALS,
    MAX_MAX_ATTACHMENTS_PER_TERMINAL, MAX_MAX_ATTACHMENTS_TOTAL, MAX_NATIVE_SCROLLBACK_MAX_BYTES,
    MAX_NATIVE_SCROLLBACK_MAX_LINES, MAX_TMUX_ATTACH_HISTORY_LINES,
    MAX_TMUX_ATTACH_HISTORY_MAX_BYTES, MAX_TMUX_POLL_BACKOFF_CEILING_MS, MAX_TMUX_POLL_INTERVAL_MS,
    MIN_CONTROL_DEADLINE_MS, MIN_CONTROL_QUEUE_ENTRIES, MIN_DELTA_QUEUE_BYTES,
    MIN_EVENT_QUEUE_BYTES, MIN_LAG_TIMEOUT_MS, MIN_MAX_ATTACHED_TERMINALS,
    MIN_MAX_ATTACHMENTS_PER_TERMINAL, MIN_MAX_ATTACHMENTS_TOTAL, MIN_NATIVE_SCROLLBACK_MAX_BYTES,
    MIN_NATIVE_SCROLLBACK_MAX_LINES, MIN_TMUX_ATTACH_HISTORY_LINES,
    MIN_TMUX_ATTACH_HISTORY_MAX_BYTES, MIN_TMUX_POLL_BACKOFF_CEILING_MS, MIN_TMUX_POLL_INTERVAL_MS,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct HostConfig {
    pub max_attachments_per_terminal: u32,
    pub max_attachments_total: u32,
    pub max_attached_terminals: u32,
    pub native_scrollback_max_lines: u32,
    pub native_scrollback_max_bytes: u32,
    pub tmux_attach_history_lines: u32,
    pub tmux_attach_history_max_bytes: u32,
    pub tmux_poll_interval_ms: u32,
    pub tmux_poll_backoff_ceiling_ms: u32,
    pub delta_queue_bytes: u32,
    pub lag_timeout_ms: u32,
    pub control_deadline_ms: u32,
    pub control_queue_entries: u32,
    pub event_queue_bytes: u32,
}

impl Default for HostConfig {
    fn default() -> Self {
        Self {
            max_attachments_per_terminal: DEFAULT_MAX_ATTACHMENTS_PER_TERMINAL,
            max_attachments_total: DEFAULT_MAX_ATTACHMENTS_TOTAL,
            max_attached_terminals: DEFAULT_MAX_ATTACHED_TERMINALS,
            native_scrollback_max_lines: DEFAULT_NATIVE_SCROLLBACK_MAX_LINES,
            native_scrollback_max_bytes: DEFAULT_NATIVE_SCROLLBACK_MAX_BYTES,
            tmux_attach_history_lines: DEFAULT_TMUX_ATTACH_HISTORY_LINES,
            tmux_attach_history_max_bytes: DEFAULT_TMUX_ATTACH_HISTORY_MAX_BYTES,
            tmux_poll_interval_ms: DEFAULT_TMUX_POLL_INTERVAL_MS,
            tmux_poll_backoff_ceiling_ms: DEFAULT_TMUX_POLL_BACKOFF_CEILING_MS,
            delta_queue_bytes: DELTA_QUEUE_BYTES as u32,
            lag_timeout_ms: DELTA_LAG_TIMEOUT_MS as u32,
            control_deadline_ms: CONTROL_DELIVERY_DEADLINE_MS as u32,
            control_queue_entries: CONTROL_QUEUE_ENTRIES as u32,
            event_queue_bytes: EVENT_QUEUE_BYTES as u32,
        }
    }
}

impl HostConfig {
    pub fn validate(self) -> io::Result<Self> {
        check(
            "max_attachments_per_terminal",
            self.max_attachments_per_terminal,
            MIN_MAX_ATTACHMENTS_PER_TERMINAL,
            MAX_MAX_ATTACHMENTS_PER_TERMINAL,
        )?;
        check(
            "max_attachments_total",
            self.max_attachments_total,
            MIN_MAX_ATTACHMENTS_TOTAL,
            MAX_MAX_ATTACHMENTS_TOTAL,
        )?;
        check(
            "max_attached_terminals",
            self.max_attached_terminals,
            MIN_MAX_ATTACHED_TERMINALS,
            MAX_MAX_ATTACHED_TERMINALS,
        )?;
        check(
            "native_scrollback_max_lines",
            self.native_scrollback_max_lines,
            MIN_NATIVE_SCROLLBACK_MAX_LINES,
            MAX_NATIVE_SCROLLBACK_MAX_LINES,
        )?;
        check(
            "native_scrollback_max_bytes",
            self.native_scrollback_max_bytes,
            MIN_NATIVE_SCROLLBACK_MAX_BYTES,
            MAX_NATIVE_SCROLLBACK_MAX_BYTES,
        )?;
        check(
            "tmux_attach_history_lines",
            self.tmux_attach_history_lines,
            MIN_TMUX_ATTACH_HISTORY_LINES,
            MAX_TMUX_ATTACH_HISTORY_LINES,
        )?;
        check(
            "tmux_attach_history_max_bytes",
            self.tmux_attach_history_max_bytes,
            MIN_TMUX_ATTACH_HISTORY_MAX_BYTES,
            MAX_TMUX_ATTACH_HISTORY_MAX_BYTES,
        )?;
        check(
            "tmux_poll_interval_ms",
            self.tmux_poll_interval_ms,
            MIN_TMUX_POLL_INTERVAL_MS,
            MAX_TMUX_POLL_INTERVAL_MS,
        )?;
        check(
            "tmux_poll_backoff_ceiling_ms",
            self.tmux_poll_backoff_ceiling_ms,
            MIN_TMUX_POLL_BACKOFF_CEILING_MS,
            MAX_TMUX_POLL_BACKOFF_CEILING_MS,
        )?;
        check(
            "delta_queue_bytes",
            self.delta_queue_bytes,
            MIN_DELTA_QUEUE_BYTES,
            MAX_DELTA_QUEUE_BYTES,
        )?;
        check(
            "lag_timeout_ms",
            self.lag_timeout_ms,
            MIN_LAG_TIMEOUT_MS,
            MAX_LAG_TIMEOUT_MS,
        )?;
        check(
            "control_deadline_ms",
            self.control_deadline_ms,
            MIN_CONTROL_DEADLINE_MS,
            MAX_CONTROL_DEADLINE_MS,
        )?;
        check(
            "control_queue_entries",
            self.control_queue_entries,
            MIN_CONTROL_QUEUE_ENTRIES,
            MAX_CONTROL_QUEUE_ENTRIES,
        )?;
        check(
            "event_queue_bytes",
            self.event_queue_bytes,
            MIN_EVENT_QUEUE_BYTES,
            MAX_EVENT_QUEUE_BYTES,
        )?;
        Ok(self)
    }

    pub fn native_entitlement_ceiling(self) -> u32 {
        self.max_attachments_total.saturating_sub(4)
    }

    pub fn lag_timeout(self) -> Duration {
        Duration::from_millis(u64::from(self.lag_timeout_ms))
    }

    pub fn control_deadline(self) -> Duration {
        Duration::from_millis(u64::from(self.control_deadline_ms))
    }
}

fn check(name: &str, value: u32, min: u32, max: u32) -> io::Result<()> {
    if value < min || value > max {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("{name}={value} outside {min}..{max}"),
        ));
    }
    Ok(())
}
