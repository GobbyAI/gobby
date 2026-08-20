//! Pane terminal emulation adapters (vt-engine).

mod cursor;
mod input;
mod kitty_keyboard;
mod osc;
mod state;
mod terminal;
mod xtgettcap;

pub use self::state::PaneState;
pub use self::terminal::{InputState, ScrollMetrics, TerminalCursorState};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WheelRouting {
    HostScroll,
    MouseReport,
    AlternateScroll,
}
pub(crate) use self::terminal::{
    GhosttyPaneTerminal, PaneTerminal, TerminalDirtyPatch, TerminalDirtyPatchOutcome,
    TerminalReadSnapshot, TerminalTextMatch, TerminalTextPoint, TerminalWordMotion,
};
