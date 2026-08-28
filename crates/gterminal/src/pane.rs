//! Pane terminal emulation adapters (vt-engine).

mod cursor;
mod input;
mod kitty_keyboard;
mod osc;
mod runtime;
mod shell;
mod shutdown;
mod state;
mod terminal;
mod xtgettcap;

pub use self::runtime::PaneRuntime;
pub use self::shell::{PaneLaunchEnv, PaneShellConfig, ShellMode};
pub use self::state::PaneState;
pub use self::terminal::{
    InputState, ScrollMetrics, TerminalCursorState, TerminalDirtyPatchOutcome,
};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum WheelRouting {
    HostScroll,
    MouseReport,
    AlternateScroll,
}
pub(crate) use self::terminal::{
    GhosttyPaneTerminal, PaneTerminal, TerminalDirtyPatch, TerminalReadSnapshot, TerminalTextMatch,
    TerminalTextPoint, TerminalWordMotion,
};
