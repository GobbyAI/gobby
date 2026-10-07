//! Wheel notches over the overlays, routed against frames the real
//! renderers drew.

use crossterm::event::{KeyModifiers, MouseEvent, MouseEventKind};
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::Terminal;

use crate::app::{Backend, Workspace};
use crate::ui::dialogs::{Dialog, OrphanRow, WorktreeChoice};
use crate::ui::render_workspace;
use crate::ui::status::Toast;
use crate::ui::{Chrome, Mode};

use super::super::{route_mouse, MouseOutcome, MOUSE_SCROLL_LINES};

/// Draw the frame the way the run loop does, write the hits back and
/// return the open overlay's popup.
fn rendered(ws: &Workspace, chrome: &mut Chrome) -> Rect {
    let area = Rect::new(0, 0, 80, 24);
    chrome.compute_view(ws, area);
    let mut terminal =
        Terminal::new(TestBackend::new(area.width, area.height)).expect("test backend");
    let mut hits = None;
    terminal
        .draw(|frame| hits = Some(render_workspace(frame, ws, chrome)))
        .expect("draw frame");
    chrome.apply_hits(hits.expect("frame drawn"));
    chrome
        .view
        .dialog_area
        .expect("the overlay's popup is drawn")
}

/// Send `notches` wheel notches of `kind` at `at`, each one handled.
fn wheel(
    ws: &Workspace,
    chrome: &mut Chrome,
    kind: MouseEventKind,
    at: (u16, u16),
    notches: usize,
) {
    let mouse = MouseEvent {
        kind,
        column: at.0,
        row: at.1,
        modifiers: KeyModifiers::NONE,
    };
    for _ in 0..notches {
        assert_eq!(route_mouse(ws, chrome, &mouse), MouseOutcome::Handled);
    }
}

/// A cell inside `popup`, and one on the same row left of it.
fn inside_and_outside(popup: Rect) -> ((u16, u16), (u16, u16)) {
    assert!(popup.x > 0, "the popup leaves a column free: {popup:?}");
    let row = popup.y + 2;
    ((popup.x + 2, row), (popup.x - 1, row))
}

fn alerts_scroll(chrome: &Chrome) -> usize {
    match chrome.dialog {
        Some(Dialog::Alerts { scroll }) => scroll,
        ref other => panic!("the Alerts dialog stays open, got {other:?}"),
    }
}

#[test]
fn a_notch_scrolls_the_keybinding_help_until_its_keys_stop() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    chrome.mode = Mode::KeybindHelp;
    let (inside, outside) = inside_and_outside(rendered(&ws, &mut chrome));
    let last = chrome.view.help_last_scroll;
    assert!(last > MOUSE_SCROLL_LINES, "Help overflows at 80x24: {last}");

    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 1);
    assert_eq!(chrome.keybind_help.scroll, MOUSE_SCROLL_LINES);
    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, last);
    assert_eq!(chrome.keybind_help.scroll, last, "the drawn bottom holds");
    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, inside, 1);
    assert_eq!(chrome.keybind_help.scroll, last - MOUSE_SCROLL_LINES);

    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, outside, 1);
    assert_eq!(chrome.keybind_help.scroll, last - MOUSE_SCROLL_LINES);
    assert_eq!(
        chrome.mode,
        Mode::KeybindHelp,
        "a notch outside closes nothing"
    );
}

#[test]
fn a_notch_scrolls_the_alert_log_until_its_keys_stop() {
    let ws = Workspace::scripted();
    let mut chrome = Chrome::dark();
    chrome.alert_log = (0..10)
        .map(|n| Toast::error(format!("alert {n}")))
        .collect();
    chrome.dialog = Some(Dialog::Alerts { scroll: 0 });
    chrome.mode = Mode::ProjectDialog;
    let (inside, outside) = inside_and_outside(rendered(&ws, &mut chrome));

    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 1);
    assert_eq!(alerts_scroll(&chrome), MOUSE_SCROLL_LINES);
    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 3);
    assert_eq!(alerts_scroll(&chrome), 9, "the oldest alert holds");
    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, inside, 1);
    assert_eq!(alerts_scroll(&chrome), 9 - MOUSE_SCROLL_LINES);

    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, outside, 1);
    assert_eq!(alerts_scroll(&chrome), 9 - MOUSE_SCROLL_LINES);
}

#[test]
fn a_notch_moves_the_navigator_selection_one_row() {
    let mut ws = Workspace::scripted();
    for terminal_id in ["term-alpha", "term-beta", "term-gamma"] {
        ws.open_terminal(terminal_id, "native", "epoch")
            .expect("open terminal");
    }
    let mut chrome = Chrome::dark();
    chrome.mode = Mode::Navigator;
    let (inside, outside) = inside_and_outside(rendered(&ws, &mut chrome));

    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 1);
    assert_eq!(chrome.navigator.selected, 1);
    wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 2);
    assert_eq!(chrome.navigator.selected, 2, "the last row holds");
    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, inside, 1);
    assert_eq!(chrome.navigator.selected, 1);

    wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, outside, 1);
    assert_eq!(chrome.navigator.selected, 1);
    assert_eq!(
        chrome.mode,
        Mode::Navigator,
        "a notch outside closes nothing"
    );
}

fn picked(chrome: &Chrome) -> usize {
    match &chrome.dialog {
        Some(Dialog::OpenWorktree { selected, .. } | Dialog::DestroyOrphans { selected, .. }) => {
            *selected
        }
        other => panic!("not a pick list: {other:?}"),
    }
}

#[test]
fn a_notch_moves_a_pick_list_selection_one_row() {
    let ws = Workspace::scripted();
    let names = ["alpha", "beta", "gamma"];
    let open_worktree = Dialog::OpenWorktree {
        project_id: "proj".to_string(),
        choices: names
            .iter()
            .map(|name| WorktreeChoice {
                worktree_id: name.to_string(),
                branch: name.to_string(),
                path: format!("/src/{name}"),
            })
            .collect(),
        selected: 0,
    };
    let destroy_orphans = Dialog::DestroyOrphans {
        rows: names
            .iter()
            .map(|name| OrphanRow {
                terminal_id: name.to_string(),
                backend: Backend::Tmux,
                name: name.to_string(),
                owner: None,
                last_seen: None,
            })
            .collect(),
        checked: vec![false; names.len()],
        selected: 0,
    };
    for dialog in [open_worktree, destroy_orphans] {
        let mut chrome = Chrome::dark();
        chrome.dialog = Some(dialog);
        chrome.mode = Mode::ProjectDialog;
        let (inside, outside) = inside_and_outside(rendered(&ws, &mut chrome));

        wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 1);
        assert_eq!(picked(&chrome), 1);
        wheel(&ws, &mut chrome, MouseEventKind::ScrollDown, inside, 2);
        assert_eq!(picked(&chrome), 2, "the last row holds");
        wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, inside, 1);
        assert_eq!(picked(&chrome), 1);

        wheel(&ws, &mut chrome, MouseEventKind::ScrollUp, outside, 1);
        assert_eq!(picked(&chrome), 1);
        assert_eq!(
            chrome.mode,
            Mode::ProjectDialog,
            "a notch outside closes nothing"
        );
    }
}
