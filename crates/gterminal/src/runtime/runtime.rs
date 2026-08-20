//! Live terminal runtime handle.
//!
//! Spawn, PTY ownership, and the de-agent-ified `PaneRuntime` land in plan 1.3.
//! This leaf keeps the public type and registry so later host code has a stable
//! module path.

pub struct TerminalRuntime {
    _private: (),
}

impl TerminalRuntime {
    pub fn shutdown(self) {}

    #[cfg(unix)]
    pub fn set_handoff_reader_paused(&self, _paused: bool) {}

    #[cfg(unix)]
    pub fn assume_handoff_ownership(&mut self) {}

    #[cfg(unix)]
    pub fn nudge_child_redraw_after_handoff(&self) {}
}
