// upstream: herdr v0.8.0 src/client/shell/agent_sidebar.rs
//! Bare terminals that are not named by roster entries.

use super::agents::{machine_admits, TERMINAL_ROW};
use super::{render_band, render_section_rows, SidebarHits};
use crate::app::sidebar_model::pane_state;
use crate::ui::chrome::{Chrome, WorkspaceView};
use crate::ui::hit::SidebarSection;
use crate::ui::pane_chrome::pane_address;
use crate::ui::sidebar_rows::{RowKind, SidebarRow};
use ratatui::layout::Rect;
use ratatui::Frame;

/// The focused project's panes no roster entry names, when the machine
/// filter admits this machine: the foreground job and the pane's address,
/// over the working directory once the daemon has reported one.
pub fn terminal_rows<W: WorkspaceView>(ws: &W, chrome: &Chrome) -> Vec<SidebarRow> {
    let model = ws.sidebar();
    if !machine_admits(ws, chrome, &model.local_machine) {
        return Vec::new();
    }
    let focused = chrome.focused_pane();
    let home = std::env::var("HOME").ok();
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
            Some(SidebarRow {
                id: format!("{TERMINAL_ROW}{terminal_id}"),
                label: pane.display_name().to_string(),
                kind: RowKind::Terminal,
                state: pane_state(pane),
                detail: pane
                    .cwd
                    .as_deref()
                    .map(|cwd| home_relative(cwd, home.as_deref()))
                    .unwrap_or_default(),
                address: pane_address(ws, pane),
                active: focused == Some(pane_id),
                ..SidebarRow::default()
            })
        })
        .collect()
}

/// `path` with the home directory written `~`, as a shell prompt shows it.
/// Only a whole leading component matches: `/Users/joshua` stays whole
/// under a home of `/Users/josh`.
fn home_relative(path: &str, home: Option<&str>) -> String {
    let home = home.map(|home| home.trim_end_matches('/'));
    match home
        .filter(|home| !home.is_empty())
        .and_then(|home| path.strip_prefix(home))
    {
        Some("") => "~".to_owned(),
        Some(rest) if rest.starts_with('/') => format!("~{rest}"),
        _ => path.to_owned(),
    }
}

pub(super) fn render_terminals(
    frame: &mut Frame,
    area: Rect,
    rows: &[SidebarRow],
    chrome: &Chrome,
    hits: &mut SidebarHits,
) {
    let section = SidebarSection::Terminals;
    render_band(frame, area, section.title(), &chrome.palette);
    render_section_rows(frame, area, section, rows, chrome, hits);
}

#[cfg(test)]
mod tests {
    use super::home_relative;

    #[test]
    fn the_home_directory_reads_as_a_tilde_only_as_a_whole_component() {
        let home = Some("/Users/josh");
        assert_eq!(home_relative("/Users/josh", home), "~");
        assert_eq!(
            home_relative("/Users/josh/Projects/gobby", home),
            "~/Projects/gobby"
        );
        assert_eq!(
            home_relative("/Users/josh/src", Some("/Users/josh/")),
            "~/src"
        );
        assert_eq!(
            home_relative("/Users/joshua/src", home),
            "/Users/joshua/src"
        );
        assert_eq!(home_relative("/srv/app", home), "/srv/app");
        assert_eq!(home_relative("/srv/app", None), "/srv/app");
        assert_eq!(home_relative("/srv/app", Some("/")), "/srv/app");
    }
}
