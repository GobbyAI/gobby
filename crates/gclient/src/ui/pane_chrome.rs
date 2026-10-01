// upstream: none (Gobby's pane corner ladder)
//! What a pane's corners say and where they fit: who it is on the top-left
//! edge, where it is on the bottom-right (V18), led by its sandbox mark. The
//! bottom-left stays empty.
//! A shared divider moves the upper pane's address beside its title; a pane
//! with no top edge hands its title to status.

use crate::app::sidebar_model::{pane_state, AgentEntry, SandboxState};
use crate::app::{ControlState, Pane};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, RowState, WorkspaceView};
use crate::ui::pane_layout::PaneInfo;
use crate::ui::sidebar::agents::{agent_reference, agent_state};
use crate::ui::sidebar_rows::TICKER_MIN_WINDOW;
use crate::ui::status::state_dot;
use crate::ui::text::display_width;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::widgets::Borders;

/// How a pane's corners read. The words carry the state; the hue repeats it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetadataTone {
    Ordinary,
    Focused,
    /// The seat needs you.
    Attention,
    /// Focused, but another client or the host holds input.
    Held,
}

impl MetadataTone {
    pub fn color(self, p: &Palette) -> Color {
        match self {
            Self::Ordinary => p.overlay0,
            Self::Focused => p.accent,
            Self::Attention => p.yellow,
            Self::Held => p.subtext0,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaneCorners {
    /// Top left: state glyph, ref, definition, and the focus word.
    pub title: String,
    /// Bottom right: the pane address (`0:0:1:2`, tmux's `%16`), or the
    /// backend alone until it is known.
    pub address: String,
    /// A pane on a foreign backend names it between the mark and the address.
    pub backend: Option<&'static str>,
    /// Drawn just before the address: whether an OS sandbox wraps the pane.
    pub sandbox: SandboxState,
    /// The mark `sandbox` draws as, from the glyph preference.
    pub sandbox_mark: Option<&'static str>,
    pub tone: MetadataTone,
    /// An exceptional condition a click resolves by taking control.
    pub actionable: bool,
}

impl PaneCorners {
    /// The address corner's text: the sandbox mark, the backend on a tmux
    /// pane, then the address (`<lock> · tmux · %16`).
    pub fn address_label(&self) -> String {
        [self.sandbox_mark, self.backend, Some(self.address.as_str())]
            .into_iter()
            .flatten()
            .collect::<Vec<_>>()
            .join(" · ")
    }
}

/// The mark for a sandbox state: only an SRT pane draws one (#23096). The
/// Nerd Font lock (U+F023) is one cell and ships in Ghostty's default font;
/// `sbx` stands in for fonts without it. An unrestricted pane draws nothing,
/// since even an open padlock reads as locked in one cell, so presence
/// carries the state, never hue alone.
pub fn sandbox_mark(state: SandboxState, nerd_glyphs: bool) -> Option<&'static str> {
    match (state, nerd_glyphs) {
        (SandboxState::Sandboxed, true) => Some("\u{f023}"),
        (SandboxState::Sandboxed, false) => Some("sbx"),
        (SandboxState::Unrestricted, _) => None,
    }
}

/// `<glyph> <ref>: <definition> · <focus word>` for a seat, `<glyph> <name>`
/// for a bare shell. The project leads the ref only where rows from every
/// project mix, as on the Agents row. The focus word is Focused, or
/// Read-only (another viewer took the lease, or the host refused input) or
/// Uncertain, and is absent on unfocused panes. Asking for control is the
/// normal focused state (#22573): input queues until the grant lands.
pub fn pane_corners<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    pane: &Pane,
    focused: bool,
) -> PaneCorners {
    // The row's state holds even when it names no seat, as the frame's does.
    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    let state = if exited(pane, agent) {
        RowState::Orphaned
    } else {
        agent.map_or_else(|| pane_state(pane), |agent| agent_state(ws, agent))
    };
    let identity = agent
        .filter(|agent| {
            agent.manual_title.is_some()
                || agent.agent_definition_name.is_some()
                || !agent.provider.trim().is_empty()
        })
        .map_or_else(
            || pane.display_name().to_owned(),
            |agent| {
                let reference = agent_reference(ws, chrome, agent);
                let definition = agent.definition_label();
                if reference.is_empty() {
                    definition
                } else {
                    format!("{reference}: {definition}")
                }
            },
        );
    let (glyph, _) = state_dot(state, &chrome.palette);
    let condition = if pane.is_acquiring() {
        None
    } else if pane.take_back || pane.control == ControlState::LeaseLost {
        Some("Read-only")
    } else if pane.control == ControlState::UncertainReadOnly {
        Some("Uncertain")
    } else {
        None
    };
    let (title, tone) = match (focused, condition) {
        (true, Some(word)) => (format!("{glyph} {identity} · {word}"), MetadataTone::Held),
        (true, None) => (
            format!("{glyph} {identity} · Focused"),
            MetadataTone::Focused,
        ),
        (false, _) if state == RowState::Attention => {
            (format!("{glyph} {identity}"), MetadataTone::Attention)
        }
        (false, _) => (format!("{glyph} {identity}"), MetadataTone::Ordinary),
    };
    // A pane with no agent row (a bare shell) has no SRT launch record.
    let sandbox = agent.map_or(SandboxState::Unrestricted, |agent| agent.sandbox);
    // A foreign backend with no address yet names only itself.
    let (backend, address) = match (pane.backend.is_native(), own_address(ws, pane)) {
        (true, address) => (
            None,
            address.unwrap_or_else(|| pane.backend.label().to_owned()),
        ),
        (false, Some(address)) => (Some(pane.backend.label()), address),
        (false, None) => (None, pane.backend.label().to_owned()),
    };
    PaneCorners {
        title,
        address,
        backend,
        sandbox,
        sandbox_mark: sandbox_mark(sandbox, chrome.prefs.nerd_glyphs),
        tone,
        actionable: focused && condition.is_some(),
    }
}

/// Whether the pane's terminal exited, by its own state or its agent row's.
pub(crate) fn exited(pane: &Pane, agent: Option<&AgentEntry>) -> bool {
    pane.terminal_state.as_deref() == Some("exited")
        || agent.is_some_and(|agent| agent.terminal_state.as_deref() == Some("exited"))
}

/// The pane's address: machine:workspace:tab:pane wherever the workspace
/// holds the terminal, a tmux pane included (`tmux 0:0:1:2`); a tmux pane
/// the workspace does not hold keeps its own id (`tmux %16`).
pub fn pane_address<W: WorkspaceView>(ws: &W, pane: &Pane) -> String {
    let backend = pane.backend.label();
    let address = own_address(ws, pane);
    if pane.backend.is_native() {
        address.unwrap_or_else(|| backend.to_owned())
    } else {
        address.map_or_else(
            || backend.to_owned(),
            |address| format!("{backend} {address}"),
        )
    }
}

/// The gclient ref of the workspace pane holding the terminal, whatever its
/// backend, else the pane's own id; `None` when it has neither.
fn own_address<W: WorkspaceView>(ws: &W, pane: &Pane) -> Option<String> {
    ws.workspace_model()
        .and_then(|model| model.pane_ref_for_terminal(&pane.terminal_id))
        .or_else(|| pane.address.clone())
}

/// Cells the top-left title may fill: the edge less its corners and padding,
/// and less what the address keeps beside it.
pub fn title_budget(width: u16, reserve: usize) -> usize {
    usize::from(width.saturating_sub(4)).saturating_sub(reserve)
}

/// Whether the pane owns a bottom edge distinct from its top edge.
fn has_bottom_edge(info: &PaneInfo) -> bool {
    info.borders.contains(Borders::BOTTOM) && info.rect.height >= 2
}

/// Top-edge cells kept for the address on a pane without its own bottom edge
/// (with gaps off, a pane above another shares that pane's top line): the
/// padded address and one rule cell before it. None when the title would
/// drop under a readable window, since the title comes first.
pub fn top_reserve(info: &PaneInfo, corners: &PaneCorners) -> usize {
    if !info.borders.contains(Borders::TOP) || has_bottom_edge(info) {
        return 0;
    }
    let reserve = display_width(&corners.address_label()) + 3;
    if title_budget(info.rect.width, reserve) < TICKER_MIN_WINDOW {
        return 0;
    }
    reserve
}

/// Where the address draws: the bottom-right corner, or the top edge's
/// right end on a pane without its own bottom edge. None without room.
pub fn address_rect(info: &PaneInfo, corners: &PaneCorners) -> Option<Rect> {
    let rect = info.rect;
    let width = u16::try_from(display_width(&corners.address_label()) + 2).ok()?;
    let x = rect.right().checked_sub(width.checked_add(1)?)?;
    let y = if has_bottom_edge(info) {
        rect.bottom().checked_sub(1)?
    } else if top_reserve(info, corners) > 0 {
        rect.y
    } else {
        return None;
    };
    (x > rect.x).then(|| Rect::new(x, y, width, 1))
}

/// Where the title draws on the top edge, padded. None without a top edge,
/// which hands the title to the status line.
pub fn title_rect(info: &PaneInfo, corners: &PaneCorners) -> Option<Rect> {
    if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
        return None;
    }
    let budget = title_budget(info.rect.width, top_reserve(info, corners));
    let width = display_width(corners.title.trim()).min(budget) + 2;
    Some(Rect::new(
        info.rect.x.saturating_add(1),
        info.rect.y,
        u16::try_from(width).ok()?,
        1,
    ))
}

/// How far the title outruns its window, for the shared ticker period.
pub fn title_travel<W: WorkspaceView>(
    ws: &W,
    chrome: &Chrome,
    pane: &Pane,
    info: &PaneInfo,
) -> usize {
    if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
        return 0;
    }
    let corners = pane_corners(ws, chrome, pane, info.is_focused);
    let budget = title_budget(info.rect.width, top_reserve(info, &corners));
    if budget < TICKER_MIN_WINDOW {
        return 0;
    }
    display_width(corners.title.trim()).saturating_sub(budget)
}

/// The focused pane's Read-only or Uncertain title, which a click resolves.
pub fn control_indicator_hit_area<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Option<Rect> {
    let tab = chrome.active_tab()?;
    let info = chrome.view.pane_infos.iter().find(|info| info.is_focused)?;
    let corners = pane_corners(ws, chrome, ws.pane(*tab.slots.get(&info.id)?), true);
    if !corners.actionable {
        return None;
    }
    title_rect(info, &corners)
}

#[cfg(test)]
#[path = "pane_chrome/tests.rs"]
mod tests;
