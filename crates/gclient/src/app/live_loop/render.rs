//! Frame drawing for the interactive loop.

use ratatui::backend::Backend;
use ratatui::Terminal;

use crate::daemon::LiveDaemon;
use crate::frame_source::FrameError;
use crate::ui::Chrome;

use super::Workspace;

/// Draw the whole workspace once, rebuilding the sidebar model the frame reads
/// first. The loop calls this on the render tick, on stale hit maps, and after
/// events that change what is drawn.
pub(super) fn render_live_workspace<B: Backend>(
    terminal: &mut Terminal<B>,
    workspace: &mut Workspace<LiveDaemon>,
    chrome: &mut Chrome,
) -> Result<(), FrameError> {
    workspace.rebuild_sidebar();
    let workspace = &*workspace;
    terminal
        .draw(|frame| {
            chrome.compute_view(workspace, frame.area());
            // Read focus out before the closure exists: capturing `chrome`
            // inside it would borrow across the `apply_hits` below.
            let focused = chrome.cursor_pane();
            let mut content = |frame: &mut ratatui::Frame<'_>, area, pane| {
                crate::views::grid::render(
                    frame,
                    area,
                    workspace.pane(pane),
                    focused == Some(pane),
                );
            };
            let hits = crate::ui::render_workspace_with(frame, workspace, chrome, &mut content);
            chrome.apply_hits(hits);
        })
        .map(|_| ())
        .map_err(|error| FrameError::Other(error.to_string()))
}

/// The click and key routing read the drawn hit map, so a projection that
/// changed the pane layout while input was queued must redraw before the next
/// event uses it, independent of the render tick.
pub(super) fn pane_hit_map_stale(chrome: &Chrome) -> bool {
    let Some(tab) = chrome.active_tab() else {
        return !chrome.view.pane_infos.is_empty();
    };
    let (expected, _) = crate::ui::pane_layout::pane_geometry(
        tab,
        chrome.tab_focus(tab),
        chrome.is_zoomed(),
        chrome.view.terminal_area,
        &chrome.prefs,
    );
    expected.len() != chrome.view.pane_infos.len()
        || expected
            .iter()
            .zip(&chrome.view.pane_infos)
            .any(|(current, drawn)| current.id != drawn.id || current.rect != drawn.rect)
}
