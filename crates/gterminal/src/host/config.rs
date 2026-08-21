//! Validated host resource bounds. Values outside a range refuse startup.

use std::io;

use crate::protocol::{
    DEFAULT_MAX_ATTACHED_TERMINALS, DEFAULT_MAX_ATTACHMENTS_PER_TERMINAL,
    DEFAULT_MAX_ATTACHMENTS_TOTAL, DEFAULT_NATIVE_SCROLLBACK_MAX_BYTES,
    DEFAULT_NATIVE_SCROLLBACK_MAX_LINES, DEFAULT_TMUX_ATTACH_HISTORY_LINES,
    DEFAULT_TMUX_ATTACH_HISTORY_MAX_BYTES, MAX_MAX_ATTACHED_TERMINALS,
    MAX_MAX_ATTACHMENTS_PER_TERMINAL, MAX_MAX_ATTACHMENTS_TOTAL, MAX_NATIVE_SCROLLBACK_MAX_BYTES,
    MAX_NATIVE_SCROLLBACK_MAX_LINES, MAX_TMUX_ATTACH_HISTORY_LINES,
    MAX_TMUX_ATTACH_HISTORY_MAX_BYTES, MIN_MAX_ATTACHED_TERMINALS,
    MIN_MAX_ATTACHMENTS_PER_TERMINAL, MIN_MAX_ATTACHMENTS_TOTAL, MIN_NATIVE_SCROLLBACK_MAX_BYTES,
    MIN_NATIVE_SCROLLBACK_MAX_LINES, MIN_TMUX_ATTACH_HISTORY_LINES,
    MIN_TMUX_ATTACH_HISTORY_MAX_BYTES,
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
        Ok(self)
    }

    pub fn native_entitlement_ceiling(self) -> u32 {
        self.max_attachments_total.saturating_sub(4)
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
