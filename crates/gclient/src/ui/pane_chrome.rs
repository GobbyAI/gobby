// upstream: none (Gobby's pane header ladder and edge metadata)
//! What a pane's chrome says and where it fits: the header title on the top
//! edge, with identity and control on the bottom-left and backend address
//! on the bottom-right. A shared divider moves the upper pane's footer
//! beside its title; a pane without room hands its condition to status.

use crate::app::{ControlState, Pane};
use crate::theme::Palette;
use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::pane_layout::PaneInfo;
use crate::ui::sidebar_rows::TICKER_MIN_WINDOW;
use crate::ui::text::display_width;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::widgets::Borders;

/// Pane headers lead with an agent's task, then a manual pane title, then
/// provisional agent identity. Bare terminals have no header until renamed.
pub fn pane_title<W: WorkspaceView>(ws: &W, pane: &Pane) -> String {
    let agent = ws
        .sidebar()
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    if let Some(task_ref) = agent.and_then(|agent| agent.task_ref.as_deref()) {
        return match agent.and_then(|agent| agent.task_title.as_deref()) {
            Some(title) if !title.trim().is_empty() => format!("Task {task_ref} - {title}"),
            _ => format!("Task {task_ref}"),
        };
    }
    pane.label
        .as_deref()
        .filter(|label| !label.trim().is_empty())
        .map(str::to_owned)
        .or_else(|| {
            agent.map(|agent| {
                agent
                    .session_title
                    .as_deref()
                    .filter(|title| !title.trim().is_empty())
                    .map(str::to_owned)
                    .unwrap_or_else(|| agent.definition_label())
            })
        })
        .unwrap_or_default()
}

/// How pane footer identity reads. The words carry the state; the hue repeats it.
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
pub struct PaneFooter {
    pub left: String,
    pub right: String,
    pub tone: MetadataTone,
    /// An exceptional condition a click resolves by taking control.
    pub actionable: bool,
}

/// The backend, plus the pane's condition when focus or an exception gives
/// it one. Asking for control is the normal focused state (#22573): input
/// queues until the grant lands. Read-only (another viewer took the lease,
/// or the host refused input) and Uncertain override Focused.
pub fn pane_footer<W: WorkspaceView>(ws: &W, pane: &Pane, focused: bool) -> PaneFooter {
    let sidebar = ws.sidebar();
    let agent = sidebar
        .agents
        .iter()
        .find(|agent| agent.terminal_id == pane.terminal_id);
    let identity = agent.map_or_else(
        || pane.display_name().to_owned(),
        |agent| {
            if agent.agent_definition_name.is_none() && agent.provider.trim().is_empty() {
                return pane.display_name().to_owned();
            }
            let project = sidebar
                .projects
                .iter()
                .find(|project| project.project_id == agent.project_id)
                .map_or(agent.project_id.as_str(), |project| project.name.as_str());
            let reference = agent
                .task_ref
                .as_deref()
                .or(agent.session_ref.as_deref())
                .unwrap_or_default();
            let definition = agent.definition_label();
            if reference.is_empty() {
                definition
            } else {
                let reference = if reference.starts_with(&format!("{project}#")) {
                    reference.to_owned()
                } else {
                    format!("{project}{reference}")
                };
                format!("{definition} ({reference})")
            }
        },
    );
    let backend = pane.backend.label();
    let address = if pane.backend.is_native() {
        ws.workspace_model()
            .and_then(|model| model.pane_ref_for_terminal(&pane.terminal_id))
            .or_else(|| pane.address.clone())
    } else {
        pane.address.clone()
    };
    let right = address.map_or_else(
        || backend.to_owned(),
        |address| format!("{backend} {address}"),
    );
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
        Some(condition) => PaneFooter {
            left: format!("{identity} · {condition}"),
            right,
            tone: MetadataTone::Warning,
            actionable: true,
        },
        None if focused => PaneFooter {
            left: format!("{identity} · Focused"),
            right,
            tone: MetadataTone::Focused,
            actionable: false,
        },
        None => PaneFooter {
            left: identity,
            right,
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
pub fn top_reserve(info: &PaneInfo, footer: &PaneFooter) -> usize {
    if !info.borders.contains(Borders::TOP) || has_bottom_edge(info) {
        return 0;
    }
    let reserve = display_width(&footer.left) + display_width(&footer.right) + 5;
    if title_budget(info.rect.width, info.is_focused, reserve) < TICKER_MIN_WINDOW {
        return 0;
    }
    reserve
}

/// Where the padded metadata sits: right-aligned on the bottom edge, one
/// cell short of the corner, or on the top edge when `top_reserve` kept room.
pub fn footer_rects(info: &PaneInfo, footer: &PaneFooter) -> Option<(Rect, Rect)> {
    let rect = info.rect;
    let left_width = u16::try_from(display_width(&footer.left) + 2).ok()?;
    let right_width = u16::try_from(display_width(&footer.right) + 2).ok()?;
    let right_x = rect.right().checked_sub(right_width.checked_add(1)?)?;
    let (left_x, y) = if has_bottom_edge(info) {
        (rect.x.checked_add(1)?, rect.bottom().checked_sub(1)?)
    } else if top_reserve(info, footer) > 0 {
        (right_x.checked_sub(left_width.checked_add(1)?)?, rect.y)
    } else {
        return None;
    };
    if left_x.checked_add(left_width.checked_add(1)?)? > right_x {
        return None;
    }
    Some((
        Rect::new(left_x, y, left_width, 1),
        Rect::new(right_x, y, right_width, 1),
    ))
}

/// How far `pane`'s header title overruns its window, for the one ticker
/// period every scrolling title shares (`ViewState::title_travel`).
pub fn title_travel<W: WorkspaceView>(ws: &W, pane: &Pane, info: &PaneInfo) -> usize {
    if !info.borders.contains(Borders::TOP) || info.rect.width <= 4 {
        return 0;
    }
    let reserve = top_reserve(info, &pane_footer(ws, pane, info.is_focused));
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
    let footer = pane_footer(ws, ws.pane(*tab.slots.get(&info.id)?), true);
    if !footer.actionable {
        return None;
    }
    footer_rects(info, &footer).map(|(left, _)| left)
}

#[cfg(test)]
#[path = "pane_chrome/tests.rs"]
mod tests;
