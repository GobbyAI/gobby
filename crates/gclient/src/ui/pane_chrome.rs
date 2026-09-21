// upstream: none (Gobby's pane header ladder and edge metadata)
//! What a pane's chrome says and where it fits: the header title on the top
//! edge, and the backend with its control condition on the bottom-right
//! edge. A pane without that edge carries its metadata top-right beside the
//! title; a pane with no border at all hands both to the status line.

use crate::app::{ControlState, Pane};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::pane_layout::PaneInfo;
use crate::ui::sidebar_rows::TICKER_MIN_WINDOW;
use crate::ui::text::display_width;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::widgets::Borders;

/// Pane headers name the running session when one supplied a title, then the
/// pane label, then the terminal's own display-name ladder.
pub fn pane_title<W: WorkspaceView>(ws: &W, pane: &Pane) -> String {
    ws.sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id)
        .and_then(|agent| agent.session_title.as_ref())
        .filter(|title| !title.trim().is_empty())
        .cloned()
        .or_else(|| {
            pane.label
                .as_ref()
                .filter(|label| !label.trim().is_empty())
                .cloned()
        })
        .unwrap_or_else(|| pane.display_name().to_owned())
}

/// How pane metadata reads. The words carry the state; the hue repeats it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MetadataTone {
    Ordinary,
    Focused,
    Warning,
}

impl MetadataTone {
    pub fn color(self, p: &Palette) -> Color {
        match self {
            Self::Ordinary => p.overlay0,
            Self::Focused => p.accent,
            Self::Warning => p.yellow,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaneMetadata {
    pub text: String,
    pub tone: MetadataTone,
    /// An exceptional condition a click resolves by taking control.
    pub actionable: bool,
}

/// The backend, plus the pane's condition when focus or an exception gives
/// it one. Asking for control is the normal focused state (#22573): input
/// queues until the grant lands. Read-only (another viewer took the lease,
/// or the host refused input) and Uncertain override Focused.
pub fn pane_metadata(pane: &Pane, focused: bool) -> PaneMetadata {
    let backend = pane.backend.label();
    let exception = if pane.is_acquiring() {
        None
    } else if pane.take_back || pane.control == ControlState::LeaseLost {
        Some("Read-only")
    } else if pane.control == ControlState::UncertainReadOnly {
        Some("Uncertain")
    } else {
        None
    };
    match exception {
        Some(condition) => PaneMetadata {
            text: format!("{backend} · {condition}"),
            tone: MetadataTone::Warning,
            actionable: true,
        },
        None if focused => PaneMetadata {
            text: format!("{backend} · Focused"),
            tone: MetadataTone::Focused,
            actionable: false,
        },
        None => PaneMetadata {
            text: backend.to_owned(),
            tone: MetadataTone::Ordinary,
            actionable: false,
        },
    }
}

/// Cells a header title may use on a `width`-cell top edge: less the corners
/// and padding, the focus marker, and `reserve` cells kept for metadata.
pub fn title_budget(width: u16, focused: bool, reserve: usize) -> usize {
    usize::from(width.saturating_sub(4))
        .saturating_sub(if focused { 2 } else { 0 })
        .saturating_sub(reserve)
}

/// Whether the pane owns a bottom edge distinct from its top edge.
fn has_bottom_edge(info: &PaneInfo) -> bool {
    info.borders.contains(Borders::BOTTOM) && info.rect.height >= 2
}

/// Top-edge cells kept for metadata on a pane without its own bottom edge
/// (with gaps off, a pane above another shares that pane's top line): the
/// padded text and one rule cell before it. None when the title would drop
/// under a readable window, since the title comes first.
pub fn top_reserve(info: &PaneInfo, meta: &PaneMetadata) -> usize {
    if !info.borders.contains(Borders::TOP) || has_bottom_edge(info) {
        return 0;
    }
    let reserve = display_width(&meta.text) + 3;
    if title_budget(info.rect.width, info.is_focused, reserve) < TICKER_MIN_WINDOW {
        return 0;
    }
    reserve
}

/// Where the padded metadata sits: right-aligned on the bottom edge, one
/// cell short of the corner, or on the top edge when `top_reserve` kept room.
pub fn metadata_rect(info: &PaneInfo, meta: &PaneMetadata) -> Option<Rect> {
    let rect = info.rect;
    let y = if has_bottom_edge(info) {
        rect.bottom() - 1
    } else if top_reserve(info, meta) > 0 {
        rect.y
    } else {
        return None;
    };
    let width = display_width(&meta.text) + 2;
    if width > usize::from(rect.width.saturating_sub(2)) {
        return None;
    }
    let width = u16::try_from(width).ok()?;
    let corner = rect.right().saturating_sub(1);
    Some(Rect::new(corner.saturating_sub(width), y, width, 1))
}

/// How far `pane`'s header title overruns its window, for the one ticker
/// period every scrolling title shares (`ViewState::title_travel`).
pub fn title_travel<W: WorkspaceView>(ws: &W, pane: &Pane, info: &PaneInfo) -> usize {
    if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
        return 0;
    }
    let reserve = top_reserve(info, &pane_metadata(pane, info.is_focused));
    let budget = title_budget(info.rect.width, info.is_focused, reserve);
    if budget < TICKER_MIN_WINDOW {
        return 0;
    }
    display_width(pane_title(ws, pane).trim()).saturating_sub(budget)
}

/// The focused pane's metadata cells while they offer an action. Focus is a
/// condition, not a button; a borderless pane's lives in the status line.
pub fn control_indicator_hit_area<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Option<Rect> {
    let tab = chrome.active_tab()?;
    let info = chrome.view.pane_infos.iter().find(|info| info.is_focused)?;
    let meta = pane_metadata(ws.pane(*tab.slots.get(&info.id)?), true);
    if !meta.actionable {
        return None;
    }
    metadata_rect(info, &meta)
}

#[cfg(test)]
#[path = "pane_chrome/tests.rs"]
mod tests;
