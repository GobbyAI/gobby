// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! Bare terminals that are not named by roster entries.

use super::agents::{machine_admits, TERMINAL_ROW};
use super::{render_band, render_section_rows, BandStyle, SidebarHits};
use crate::app::sidebar_model::pane_state;
use crate::ui::chrome::{terminal_address, Chrome, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::sidebar_rows::{RowKind, SidebarRow};
use ratatui::layout::Rect;
use ratatui::Frame;

/// The focused project's panes no roster entry names, when the machine
/// filter admits this machine: the foreground job over its address and backend.
pub fn terminal_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let model = ws.sidebar();
    if !machine_admits(ws, chrome, &model.local_machine) {
        return Vec::new();
    }
    let focused = chrome.focused_pane();
    ws.roster_terminal_ids()
        .into_iter()
        .filter(|terminal_id| {
            !model
                .agents
                .iter()
                .any(|agent| agent.terminal_id == *terminal_id)
        })
        .filter_map(|terminal_id| {
            let pane_id = ws.pane_for_terminal(&terminal_id)?;
            let pane = ws.pane(pane_id);
            let tokens = [
                terminal_address(ws, &terminal_id),
                Some(pane.backend.label().to_string()),
            ]
            .into_iter()
            .flatten()
            .collect();
            Some(SidebarRow {
                id: format!("{TERMINAL_ROW}{terminal_id}"),
                label: pane.display_name().to_string(),
                kind: RowKind::Agent,
                state: pane_state(pane),
                tokens,
                active: focused == Some(pane_id),
                ..SidebarRow::default()
            })
        })
        .collect()
}

pub(super) fn render_terminals(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Terminals;
    render_band(
        frame,
        area,
        section.title(),
        &[],
        BandStyle::section(&chrome.palette),
    );
    render_section_rows(frame, area, section, rows, chrome, hits);
}
