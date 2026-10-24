//! Dragging a sidebar section divider against the frame the renderers drew.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;

use super::super::{route_mouse, MouseGesture, MouseOutcome};
use crate::app::{ContextMenuKind, Workspace, MOUSE_SCROLL_LINES};
use crate::daemon::{Checkout, ProjectRow, SidebarRows};
use crate::ui::hit::SidebarSection;
use crate::ui::render_workspace;
use crate::ui::sidebar::section_metrics;
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

/// Eighteen project cards, one line each, overflow the Projects section.
fn overflowing_projects() -> Workspace {
    let mut ws = Workspace::scripted();
    ws.set_local_machine("local");
    ws.daemon_mut().set_sidebar_rows(SidebarRows {
        projects: (0..18)
            .map(|index| ProjectRow {
                id: format!("proj-{index}"),
                name: format!("project-{index}"),
                display_name: format!("project-{index}"),
                checkout: Some(Checkout {
                    machine_id: "local".to_string(),
                    root_path: format!("/repos/project-{index}"),
                }),
                ..ProjectRow::default()
            })
            .collect(),
        ..SidebarRows::default()
    });
    ws.select_project("proj-0");
    ws.reconcile_subscribe_first().unwrap();
    ws
}

/// A wheel notch on the divider under Projects scrolls the Projects section,
/// as it would on the row above the divider.
#[test]
fn a_wheel_notch_on_a_section_divider_scrolls_the_section_above_it() {
    let ws = overflowing_projects();
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    chrome.sidebar.section_heights = [0, 4, 0, 0];
    drawn(&ws, &mut chrome);
    let projects = section_metrics(&ws, &chrome, SidebarSection::Projects);
    assert!(
        projects.max_offset_from_bottom >= MOUSE_SCROLL_LINES,
        "the Projects cards overflow by more than a notch: max {} cards {} rects {:?}",
        projects.max_offset_from_bottom,
        chrome.view.project_hit_areas.len(),
        chrome.view.sidebar_section_rects
    );
    let divider = chrome.view.sidebar_section_divider_hit_areas[1]
        .expect("the divider under Projects is drawn");
    let notch = event(MouseEventKind::ScrollDown, divider.x + 1, divider.y);
    assert_eq!(route_mouse(&ws, &mut chrome, &notch), MouseOutcome::Handled);
    assert_eq!(
        chrome.sidebar.scroll(SidebarSection::Projects),
        MOUSE_SCROLL_LINES,
        "the notch scrolls the section above the divider"
    );
}

/// A right-click on the divider under Projects opens the Global menu, as a
/// right-click on the blank sidebar around it does.
#[test]
fn a_right_click_on_a_section_divider_opens_the_global_menu() {
    let ws = overflowing_projects();
    let mut chrome = Chrome::dark();
    chrome.sidebar.pinned = true;
    chrome.sidebar.section_heights = [0, 4, 0, 0];
    drawn(&ws, &mut chrome);
    let divider = chrome.view.sidebar_section_divider_hit_areas[1]
        .expect("the divider under Projects is drawn");
    let press = event(
        MouseEventKind::Down(MouseButton::Right),
        divider.x + 1,
        divider.y,
    );
    assert_eq!(route_mouse(&ws, &mut chrome, &press), MouseOutcome::Handled);
    assert!(
        matches!(
            chrome.menu.as_ref().map(|menu| &menu.kind),
            Some(ContextMenuKind::Global)
        ),
        "the Global menu opens over the divider"
    );
}
