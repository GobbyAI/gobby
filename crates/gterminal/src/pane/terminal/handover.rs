//! Host handover for one pane terminal: the ghostty snapshot carries the C
//! terminal, and `PaneCoreHandover` carries the Rust state `GhosttyPaneCore`
//! keeps around it.

use serde::{Deserialize, Serialize};

use super::super::cursor::DecscusrTracker;
use super::super::kitty_keyboard::KittyKeyboardTracker;
use super::super::osc::{AgentOscStateTracker, DefaultColorEventTracker, DefaultColorOscTracker};
use super::GhosttyPaneTerminal;
use crate::ghostty::{ColorScheme, RgbColor};
use crate::terminal_theme::ThemeDeclaration;

/// Continuation tracking limit for every pane terminal, set before any input
/// so a snapshot can carry an unfinished escape or UTF-8 sequence.
pub const PANE_CONTINUATION_MAX_BYTES: usize = 4096;

/// The `GhosttyPaneCore` wrapper state a handover carries beside the pane's
/// snapshot. The render state is rebuilt from the decoded terminal; the OSC
/// debug tracker, cursor settle timer, and Windows-only fields start fresh.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct PaneCoreHandover {
    host_terminal_theme: ThemeDeclaration,
    host_color_scheme: Option<ColorScheme>,
    initial_default_foreground: Option<RgbColor>,
    initial_default_background: Option<RgbColor>,
    transient_default_color_owner_pgid: Option<u32>,
    child_default_foreground_changed: bool,
    child_default_background_changed: bool,
    default_color_tracker: DefaultColorOscTracker,
    default_color_event_tracker: DefaultColorEventTracker,
    agent_osc_state: AgentOscStateTracker,
    kitty_keyboard: KittyKeyboardTracker,
    decscusr_tracker: DecscusrTracker,
}

fn io_error(err: impl std::fmt::Display) -> std::io::Error {
    std::io::Error::other(err.to_string())
}

impl GhosttyPaneTerminal {
    /// Encodes the pane's terminal snapshot and its carried wrapper state.
    pub fn encode_handover(&self) -> std::io::Result<(Vec<u8>, PaneCoreHandover)> {
        let core = self
            .core
            .lock()
            .map_err(|_| std::io::Error::other("ghostty core lock poisoned"))?;
        let snapshot = core.terminal.encode_snapshot().map_err(io_error)?;
        let carried = PaneCoreHandover {
            host_terminal_theme: ThemeDeclaration::from(&core.host_terminal_theme),
            host_color_scheme: core.terminal.color_scheme(),
            initial_default_foreground: core.initial_default_foreground,
            initial_default_background: core.initial_default_background,
            transient_default_color_owner_pgid: core.transient_default_color_owner_pgid,
            child_default_foreground_changed: core.child_default_foreground_changed,
            child_default_background_changed: core.child_default_background_changed,
            default_color_tracker: core.default_color_tracker.clone(),
            default_color_event_tracker: core.default_color_event_tracker.clone(),
            agent_osc_state: core.agent_osc_state.clone(),
            kitty_keyboard: core.kitty_keyboard.clone(),
            decscusr_tracker: core.decscusr_tracker.clone(),
        };
        Ok((snapshot, carried))
    }

    /// Builds a pane terminal from a handover snapshot and its carried
    /// wrapper state.
    ///
    /// The host theme is restored without writing to the terminal. The
    /// snapshot already holds the palette and default colors the old host
    /// applied, including any child OSC 10/11 override, and a VT write here
    /// would land inside an unfinished sequence the snapshot carried. The
    /// carried child color ownership makes later theme updates skip the
    /// colors the child set.
    pub fn from_handover(
        snapshot: &[u8],
        core: PaneCoreHandover,
        scrollback_limit_bytes: usize,
    ) -> std::io::Result<Self> {
        let mut terminal =
            crate::ghostty::Terminal::decode_snapshot(snapshot, PANE_CONTINUATION_MAX_BYTES)
                .map_err(io_error)?;
        terminal
            .set_scrollback_max_bytes(scrollback_limit_bytes)
            .map_err(io_error)?;
        crate::kitty_graphics::set_enabled(true);
        if crate::kitty_graphics::is_enabled() {
            terminal.enable_kitty_graphics().map_err(io_error)?;
        }
        terminal.set_color_scheme(core.host_color_scheme);
        let pane = Self::from_tracked_terminal(terminal)?;
        {
            let mut pane_core = pane
                .core
                .lock()
                .map_err(|_| std::io::Error::other("ghostty core lock poisoned"))?;
            pane_core.host_terminal_theme = core.host_terminal_theme.terminal_theme();
            pane_core.initial_default_foreground = core.initial_default_foreground;
            pane_core.initial_default_background = core.initial_default_background;
            pane_core.transient_default_color_owner_pgid = core.transient_default_color_owner_pgid;
            pane_core.child_default_foreground_changed = core.child_default_foreground_changed;
            pane_core.child_default_background_changed = core.child_default_background_changed;
            pane_core.default_color_tracker = core.default_color_tracker;
            pane_core.default_color_event_tracker = core.default_color_event_tracker;
            pane_core.agent_osc_state = core.agent_osc_state;
            pane_core.kitty_keyboard = core.kitty_keyboard;
            pane_core.decscusr_tracker = core.decscusr_tracker;
        }
        Ok(pane)
    }
}
