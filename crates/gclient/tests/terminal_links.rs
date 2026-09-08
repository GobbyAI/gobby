//! 3.1.3: `gclient` links `gobby_terminal::{raw_input, input, selection}`
//! through real consumers. Host bytes parsed by `raw_input` drive the chrome
//! keymap and the pane encoding; a `selection` paints the pane it belongs to.
//! 3.2.1: a ctrl+click resolves the OSC 8 or bare URL under the pointer.

use crossterm::event::{KeyCode, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{route_mouse, MouseOutcome, PaneId};
use gobby_client::frame_source::{PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::key_input::{key_input, resolve_chord, text_bytes, KeyInput, Resolution};
use gobby_client::theme::{Theme, ThemeKind};
use gobby_client::ui::chrome::Mode;
use gobby_client::ui::{render_workspace, Action, Chrome, Keymap};
use gobby_client::Workspace;
use gobby_terminal::input::{KeyboardProtocol, TextCommit};
use gobby_terminal::layout::PaneId as SlotId;
use gobby_terminal::protocol::{CellData, FrameData, PaneModes, ServerMessage};
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
    chrome.tabs_mut().active_tab = 0;
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
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .unwrap();
    assert_highlighted_row(&terminal, alpha_inner, highlight);
    assert_untouched(&terminal, beta_inner, highlight, "term-beta");

    chrome.selection = Some(selection_across(beta_slot, beta_inner));
    terminal
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .unwrap();
    assert_highlighted_row(&terminal, beta_inner, highlight);
    assert_untouched(&terminal, alpha_inner, highlight, "term-alpha");

    chrome.selection = Some(Selection::anchor(alpha_slot, 0, 0, None));
    terminal
        .draw(|frame| {
            render_workspace(frame, &ws, &chrome);
        })
        .unwrap();
    assert_untouched(&terminal, alpha_inner, highlight, "anchored term-alpha");
}

/// A frame showing `rows`, each padded with blanks to the widest row.
fn frame_of(rows: &[&str], modes: PaneModes) -> FrameData {
    let width = rows
        .iter()
        .map(|row| row.chars().count())
        .max()
        .unwrap_or(0);
    let cells = rows
        .iter()
        .flat_map(|row| {
            row.chars()
                .chain(std::iter::repeat(' '))
                .take(width)
                .map(|symbol| CellData {
                    symbol: symbol.to_string(),
                    fg: 0,
                    bg: 0,
                    modifier: 0,
                    skip: false,
                    hyperlink: None,
                })
        })
        .collect();
    FrameData {
        cells,
        width: u16::try_from(width).expect("frame width"),
        height: u16::try_from(rows.len()).expect("frame height"),
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes,
    }
}

/// Column where `needle` starts in `row`.
fn col_of(row: &str, needle: &str) -> u16 {
    let at = row
        .find(needle)
        .unwrap_or_else(|| panic!("{needle:?} in {row:?}"));
    u16::try_from(row[..at].chars().count()).expect("column")
}

/// Give the cells spelling `needle` on `rows[row]` the OSC 8 target `uri`.
fn link_cells(frame: &mut FrameData, rows: &[&str], row: usize, needle: &str, uri: &str) {
    let index = u32::try_from(frame.hyperlinks.len()).expect("link index");
    frame.hyperlinks.push(uri.to_string());
    let start = usize::from(col_of(rows[row], needle));
    let width = usize::from(frame.width);
    for col in start..start + needle.chars().count() {
        frame.cells[row * width + col].hyperlink = Some(index);
    }
}

/// Open `terminal` in `ws` and deliver `frame` as its latest frame.
async fn pane_showing(ws: &mut Workspace, terminal: &str, frame: FrameData) -> PaneId {
    let pane = ws
        .open_terminal(terminal, "native", "epoch-links")
        .expect("open terminal");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(frame));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("replace source");
    ws.recv_pane_frame(pane).await.expect("receive frame");
    pane
}

/// Press and release the left button on inner cell (`col`, `row`) of `inner`
/// holding `modifiers`; what the press meant.
fn click(
    ws: &Workspace,
    chrome: &mut Chrome,
    inner: Rect,
    col: u16,
    row: u16,
    modifiers: KeyModifiers,
) -> MouseOutcome {
    let at = |kind| MouseEvent {
        kind,
        column: inner.x + col,
        row: inner.y + row,
        modifiers,
    };
    let outcome = route_mouse(ws, chrome, &at(MouseEventKind::Down(MouseButton::Left)));
    route_mouse(ws, chrome, &at(MouseEventKind::Up(MouseButton::Left)));
    outcome
}

#[tokio::test]
async fn ctrl_click_resolves_osc8_and_bare_urls() {
    let rows = [
        "see https://example.com/a-b_c?q=x@y. or (https://wiki.example/Foo_(bar))",
        "read the docs and the local file",
        "plain words only",
    ];
    let mut frame = frame_of(&rows, PaneModes::default());
    link_cells(&mut frame, &rows, 1, "docs", "https://docs.example/start");
    link_cells(&mut frame, &rows, 1, "file", "file:///etc/hosts");
    let mut ws = Workspace::scripted();
    let pane = pane_showing(&mut ws, "term-links", frame).await;
    let area = Rect::new(0, 0, 120, 40);
    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "links");
    chrome.compute_view(&ws, area);
    let inner = inner_rect(&chrome, slot);
    let ctrl = KeyModifiers::CONTROL;

    assert_eq!(
        click(
            &ws,
            &mut chrome,
            inner,
            col_of(rows[0], "example.com"),
            0,
            ctrl
        ),
        MouseOutcome::OpenLink("https://example.com/a-b_c?q=x@y".to_string()),
        "a bare URL loses its trailing period"
    );
    assert_eq!(
        click(&ws, &mut chrome, inner, col_of(rows[0], "Foo_"), 0, ctrl),
        MouseOutcome::OpenLink("https://wiki.example/Foo_(bar)".to_string()),
        "the bracket the URL opened stays, the wrapping one goes"
    );
    assert_eq!(
        click(&ws, &mut chrome, inner, col_of(rows[1], "docs"), 1, ctrl),
        MouseOutcome::OpenLink("https://docs.example/start".to_string()),
        "an OSC 8 target wins over the visible text"
    );
    for (row, col, why) in [
        (
            0,
            col_of(rows[0], "y.") + 1,
            "the trimmed period is not part of the link",
        ),
        (
            1,
            col_of(rows[1], "file"),
            "a file: OSC 8 target never opens",
        ),
        (2, col_of(rows[2], "plain"), "plain text is a plain click"),
    ] {
        assert_eq!(
            click(&ws, &mut chrome, inner, col, row, ctrl),
            MouseOutcome::Handled,
            "{why}"
        );
        assert!(
            chrome.gesture.is_none() && chrome.selection.is_none(),
            "{why}: the click ended like any other"
        );
    }
    assert_eq!(
        click(
            &ws,
            &mut chrome,
            inner,
            col_of(rows[0], "example.com"),
            0,
            KeyModifiers::NONE
        ),
        MouseOutcome::Handled,
        "without ctrl the URL is ordinary text"
    );

    // Inside a pane that reports the mouse a plain press is reported to the
    // app (X10 here), but ctrl still resolves the link first.
    let vim_row = "open https://r.example/q now";
    let vim = pane_showing(
        &mut ws,
        "term-vim",
        frame_of(
            &[vim_row],
            PaneModes {
                mouse_all: true,
                ..PaneModes::default()
            },
        ),
    )
    .await;
    let vim_slot = chrome.open_pane(vim, "vim");
    chrome.compute_view(&ws, area);
    let vim_inner = inner_rect(&chrome, vim_slot);
    assert_eq!(chrome.focused_pane(), Some(vim));
    let url_col = col_of(vim_row, "r.example");
    let report = vec![
        0x1b,
        b'[',
        b'M',
        b' ',
        b'!' + u8::try_from(url_col).expect("column fits an x10 report"),
        b'!',
    ];
    assert_eq!(
        click(&ws, &mut chrome, vim_inner, url_col, 0, KeyModifiers::NONE),
        MouseOutcome::Write {
            pane: vim,
            bytes: report
        },
        "a reporting pane gets a plain press as a report"
    );
    assert_eq!(
        click(&ws, &mut chrome, vim_inner, url_col, 0, ctrl),
        MouseOutcome::OpenLink("https://r.example/q".to_string()),
        "ctrl resolves the link before any forwarding"
    );
}
