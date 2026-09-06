//! 3.1.3: `gclient` links `gobby_terminal::{raw_input, input, selection}`
//! through real consumers. Host bytes parsed by `raw_input` drive the chrome
//! keymap and the pane encoding; a `selection` paints the pane it belongs to.

use crossterm::event::{KeyCode, KeyModifiers};
use gobby_client::key_input::{key_input, resolve_chord, text_bytes, KeyInput, Resolution};
use gobby_client::theme::{Theme, ThemeKind};
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::{render_workspace, Action, Chrome, Keymap};
use gobby_client::Workspace;
use gobby_terminal::input::{KeyboardProtocol, TextCommit};
use gobby_terminal::layout::PaneId as SlotId;
use gobby_terminal::raw_input::{parse_raw_input_bytes, RawInputEvent};
use gobby_terminal::selection::Selection;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use ratatui::style::Color;
use ratatui::Terminal;
use serde_json::json;

fn single_key(bytes: &[u8]) -> KeyInput {
    let events = parse_raw_input_bytes(bytes);
    assert_eq!(events.len(), 1, "{bytes:?} parsed to {events:?}");
    key_input(&events[0], KeyboardProtocol::Legacy).expect("a key event")
}

#[test]
fn raw_input_events_drive_the_keymap_and_pane_bytes() {
    let keymap = Keymap::defaults();

    let prefix = single_key(b"\x02");
    assert_eq!(prefix.key.code, KeyCode::Char('b'));
    assert_eq!(prefix.key.modifiers, KeyModifiers::CONTROL);
    assert_eq!(
        resolve_chord(&keymap, Mode::Terminal, &prefix.key, false),
        Resolution::Prefix
    );

    let help = single_key(b"?");
    assert_eq!(
        resolve_chord(&keymap, Mode::Terminal, &help.key, true),
        Resolution::Action(Action::Help)
    );

    let plain = single_key(b"x");
    assert_eq!(
        resolve_chord(&keymap, Mode::Terminal, &plain.key, false),
        Resolution::Unbound
    );
    assert_eq!(plain.bytes, b"x".to_vec());

    // The same host bytes resolve differently by mode: a focused terminal owns
    // the arrow, the navigation modes bind it.
    let up = single_key(b"\x1b[A");
    assert_eq!(
        resolve_chord(&keymap, Mode::Terminal, &up.key, false),
        Resolution::Unbound
    );
    assert_eq!(
        resolve_chord(&keymap, Mode::Navigate, &up.key, false),
        Resolution::Action(Action::NavigateUp)
    );

    assert_eq!(
        text_bytes(&RawInputEvent::Paste("hi".into())),
        Some(b"hi".to_vec())
    );
    assert_eq!(
        text_bytes(&RawInputEvent::Text(TextCommit::new("é"))),
        Some("é".as_bytes().to_vec())
    );
    assert!(key_input(&RawInputEvent::OuterFocusLost, KeyboardProtocol::Legacy).is_none());
    assert!(text_bytes(&RawInputEvent::OuterFocusLost).is_none());
}

fn scripted_workspace() -> Workspace {
    let mut ws = Workspace::scripted();
    ws.daemon_mut().set_roster(json!({
        "epoch": "e1",
        "seq": 1,
        "entries": [{"entry_id": "run:term-alpha", "kind": "blocked"}]
    }));
    ws.reconcile_subscribe_first().unwrap();
    ws.open_terminal("term-alpha", "native", "epoch").unwrap();
    ws.open_terminal("term-beta", "native", "epoch").unwrap();
    ws
}

fn chrome_for(ws: &Workspace, kind: ThemeKind) -> Chrome {
    let mut chrome = Chrome::new(Theme::new(kind));
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    chrome.open_pane(alpha, "alpha");
    chrome.open_pane(beta, "alpha");
    chrome.open_tab(alpha, "second");
    chrome.active_tab = 0;
    // Splitting focuses the new pane; the assertions below name term-alpha.
    assert!(chrome.focus_pane(alpha));
    chrome
}

fn inner_rect(chrome: &Chrome, slot: SlotId) -> Rect {
    chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .map(|info| info.inner_rect)
        .expect("slot has a pane rect")
}

/// Drag from the top-left inner cell three columns to the right.
fn selection_across(slot: SlotId, inner: Rect) -> Selection {
    let mut selection = Selection::anchor(slot, 0, 0, None);
    selection.drag(inner.x + 3, inner.y, inner, None);
    assert!(selection.finish(), "a drag finishes as a visible selection");
    selection
}

fn background(terminal: &Terminal<TestBackend>, x: u16, y: u16) -> Option<Color> {
    terminal.backend().buffer()[(x, y)].style().bg
}

fn assert_untouched(terminal: &Terminal<TestBackend>, inner: Rect, highlight: Color, pane: &str) {
    for y in inner.y..inner.y + inner.height {
        for x in inner.x..inner.x + inner.width {
            assert_ne!(
                background(terminal, x, y),
                Some(highlight),
                "{pane} cell ({x}, {y}) carries the selection background"
            );
        }
    }
}

fn assert_highlighted_row(terminal: &Terminal<TestBackend>, inner: Rect, highlight: Color) {
    for x in inner.x..=inner.x + 3 {
        assert_eq!(
            background(terminal, x, inner.y),
            Some(highlight),
            "cell ({x}, {}) is inside the selection",
            inner.y
        );
    }
    assert_ne!(background(terminal, inner.x + 4, inner.y), Some(highlight));
    assert_ne!(background(terminal, inner.x, inner.y + 1), Some(highlight));
}

#[test]
fn selection_highlights_only_its_pane() {
    let ws = scripted_workspace();
    let mut chrome = chrome_for(&ws, ThemeKind::Dark);
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let alpha = ws.pane_for_terminal("term-alpha").unwrap();
    let beta = ws.pane_for_terminal("term-beta").unwrap();
    let tab = chrome.active_tab().unwrap();
    let alpha_slot = tab.slot_for(alpha).unwrap();
    let beta_slot = tab.slot_for(beta).unwrap();
    let alpha_inner = inner_rect(&chrome, alpha_slot);
    let beta_inner = inner_rect(&chrome, beta_slot);
    assert!(alpha_inner.width > 5 && beta_inner.width > 5);
    let highlight = chrome.palette.surface1;

    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    chrome.selection = Some(selection_across(alpha_slot, alpha_inner));
    terminal
        .draw(|frame| render_workspace(frame, &ws, &chrome))
        .unwrap();
    assert_highlighted_row(&terminal, alpha_inner, highlight);
    assert_untouched(&terminal, beta_inner, highlight, "term-beta");

    chrome.selection = Some(selection_across(beta_slot, beta_inner));
    terminal
        .draw(|frame| render_workspace(frame, &ws, &chrome))
        .unwrap();
    assert_highlighted_row(&terminal, beta_inner, highlight);
    assert_untouched(&terminal, alpha_inner, highlight, "term-alpha");

    chrome.selection = Some(Selection::anchor(alpha_slot, 0, 0, None));
    terminal
        .draw(|frame| render_workspace(frame, &ws, &chrome))
        .unwrap();
    assert_untouched(&terminal, alpha_inner, highlight, "anchored term-alpha");
}
