//! Dragging a sidebar section divider against the frame the renderers drew.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;

use super::super::{route_mouse, MouseGesture, MouseOutcome};
use crate::app::Workspace;
use crate::ui::render_workspace;
use crate::ui::Chrome;

/// Draw the frame the way the run loop does and write the hits back.
fn drawn(ws: &Workspace, chrome: &mut Chrome) {
    let area = Rect::new(0, 0, 100, 40);
    chrome.compute_view(ws, area);
    let mut terminal =
        Terminal::new(TestBackend::new(area.width, area.height)).expect("test backend");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, ws, chrome)))
        .expect("draw frame");
    chrome.apply_hits(hits.expect("frame drawn"));
}

fn event(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    }
}

/// Pressing the divider under Machines, dragging it two rows down and
/// releasing moves two rows from Projects to Machines, and the release
/// saves both pins so a restart reopens with the dragged heights.
#[test]
fn a_section_divider_drag_moves_rows_between_its_pair_and_release_saves_them() {
    let home = tempfile::tempdir().expect("temp gobby home");
    let mut ws = Workspace::scripted();
    ws.set_gobby_home(home.path().to_path_buf());
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    chrome.sidebar.section_heights = [8, 6, 12, 0];
    drawn(&ws, &mut chrome);
    let divider = chrome.view.sidebar_section_divider_hit_areas[0]
        .expect("the divider under Machines is drawn");
    let (column, row) = (divider.x + 1, divider.y);

    let press = event(MouseEventKind::Down(MouseButton::Left), column, row);
    assert_eq!(route_mouse(&ws, &mut chrome, &press), MouseOutcome::Handled);
    assert!(
        matches!(
            chrome.gesture,
            Some(MouseGesture::SidebarSectionDrag { index: 0, .. })
        ),
        "the press records the divider: {:?}",
        chrome.gesture
    );

    let drag = event(MouseEventKind::Drag(MouseButton::Left), column, row + 2);
    assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Handled);
    let release = event(MouseEventKind::Up(MouseButton::Left), column, row + 2);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &release),
        MouseOutcome::Handled
    );

    assert_eq!(chrome.prefs.sidebar_section_heights[..2], [10, 4]);
    let saved = crate::prefs::load_prefs(home.path()).expect("load prefs");
    assert_eq!(
        saved.sidebar_section_heights[..2],
        [10, 4],
        "a restart reopens with the dragged heights"
    );
}
