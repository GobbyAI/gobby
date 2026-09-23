//! 3.3.13 / 3.3.14 / 3.3.20 scrollback, copy, and paste.

use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use gobby_client::app::{
    anchor_selection, extend_selection, finish_selection, route_mouse, MouseGesture, MouseOutcome,
    PaneId,
};
use gobby_client::copy_mode::{
    apply_text_read, copy_finalized_selection, copy_or_request_selection, copy_selection,
    extract_logical_line, route_mouse_selection, route_paste_event, write_selection_osc52,
    PASTE_MAX_BYTES,
};
use gobby_client::frame_source::{PaneFrameSource, ScriptedFrameSource, Transport};
use gobby_client::ui::chrome::{Chrome, Mode};
use gobby_client::Workspace;
use gobby_terminal::layout::PaneId as LayoutPaneId;
use gobby_terminal::protocol::{CellData, ClientMessage, FrameData, PaneModes, ServerMessage};
use gobby_terminal::raw_input::RawInputEvent;
use gobby_terminal::selection::Selection;
use ratatui::layout::Rect;
use serde_json::json;

#[test]
fn scrollback_copy_is_lease_independent() {
    let mut ws = Workspace::scripted();
    let a = ws.open_terminal("term-a", "tmux", "epoch-a").unwrap();
    let b = ws.open_terminal("term-b", "tmux", "epoch-a").unwrap();
    ws.seed_attach_history(a, "hello 👩‍💻 wrapped\nhard\n");
    ws.seed_attach_history(b, "hello 👩‍💻 wrapped\nhard\n");
    ws.set_scroll_offset(a, 4).unwrap();
    ws.set_scroll_offset(b, 1).unwrap();
    assert_eq!(ws.pane(a).scroll_offset(), 4);
    assert_eq!(ws.pane(b).scroll_offset(), 1);

    let line = extract_logical_line("A👩‍💻B\nc\nhard", 4);
    assert_eq!(line, "A👩‍💻Bc");

    let mut selection = Selection::anchor(LayoutPaneId::from_raw(0), 0, 0, None);
    selection.drag(4, 0, Rect::new(0, 0, 10, 2), None);
    assert!(selection.finish());
    let mut osc52 = Vec::new();
    assert!(write_selection_osc52(&mut osc52, &selection, "hello").unwrap());
    assert_eq!(osc52, b"\x1b]52;c;aGVsbG8=\x07");

    ws.push_frame(a, "new-output");
    assert!(ws.pane(a).has_new_output());
    assert!(ws.daemon().pty_mutation_count() == 0);
    let source = ws.pane(a).scripted_source().expect("scripted source");
    assert!(!source.sent_host_input());
    assert!(!source.sent_resize());

    let joiner = ws.open_terminal("term-join", "tmux", "epoch-a").unwrap();
    ws.seed_attach_history(joiner, "later joiner history");
    assert!(ws.pane(joiner).copy_seeded_from_history());
    assert!(!ws.pane(joiner).required_created_flag());
}

#[tokio::test]
async fn client_copy_path_emits_finalized_selection_as_osc52() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-copy", "native", "epoch-copy")
        .expect("open terminal");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(FrameData {
        cells: "copymore"
            .chars()
            .map(|symbol| CellData {
                symbol: symbol.to_string(),
                fg: 0,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
            .collect(),
        width: 4,
        height: 2,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    }));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("replace source");
    ws.recv_pane_frame(pane).await.expect("receive frame");

    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "copy");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    chrome.mode = Mode::Copy;
    let inner = chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .expect("pane geometry")
        .inner_rect;
    let mouse = |kind, column, row| {
        RawInputEvent::Mouse(MouseEvent {
            kind,
            column: inner.x + column,
            row: inner.y + row,
            modifiers: KeyModifiers::NONE,
        })
    };
    assert!(!route_mouse_selection(
        &ws,
        &mut chrome,
        &mouse(MouseEventKind::Down(MouseButton::Left), 1, 0),
    ));
    assert!(!route_mouse_selection(
        &ws,
        &mut chrome,
        &mouse(MouseEventKind::Drag(MouseButton::Left), 2, 1),
    ));
    assert!(route_mouse_selection(
        &ws,
        &mut chrome,
        &mouse(MouseEventKind::Up(MouseButton::Left), 2, 1),
    ));

    let mut output = Vec::new();
    assert!(copy_finalized_selection(&ws, &chrome, &mut output).expect("copy selection"));
    assert_eq!(output, b"\x1b]52;c;b3B5Cm1vcg==\x07");
}

#[test]
fn client_copy_path_uses_attach_history_before_the_first_frame() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-history", "tmux", "epoch-history")
        .expect("open terminal");
    ws.seed_attach_history(pane, "joined from history");
    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "history");
    let mut selection = Selection::anchor(slot, 0, 0, None);
    selection.force_dragging();
    assert!(selection.finish());
    chrome.selection = Some(selection);

    let mut output = Vec::new();
    assert!(copy_finalized_selection(&ws, &chrome, &mut output).expect("copy history"));
    assert_eq!(output, b"\x1b]52;c;am9pbmVkIGZyb20gaGlzdG9yeQ==\x07");
}

#[test]
fn client_paste_event_routes_through_terminal_paste() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-paste", "native", "epoch-paste")
        .expect("open terminal");
    // A proxy attachment, so the routed paste is the daemon's to carry; a
    // direct pane pastes on its own frame socket (#22573), which
    // `a_direct_paste_reaches_the_host_not_the_daemon` covers.
    ws.reattach_frames(pane).expect("proxy frame source");
    ws.force_held(pane);
    let mut chrome = Chrome::dark();
    chrome.open_pane(pane, "paste");

    assert!(
        route_paste_event(&mut ws, &chrome, &RawInputEvent::Paste("payload".into()),)
            .expect("route paste")
    );
    let messages = ws.daemon().ws_sent();
    let message = messages.last().expect("paste message");
    assert_eq!(message["type"], "terminal_paste");
    assert_eq!(message["text"], "payload");
}

#[test]
fn native_set_scroll_offset_and_tmux_wrap_history() {
    let mut ws = Workspace::scripted();
    let native = ws.open_terminal("n1", "native", "epoch-n").unwrap();
    ws.attach_frames(native).unwrap();
    ws.set_scroll_offset(native, 8).unwrap();
    assert!(matches!(
        ws.pane(native)
            .scripted_source()
            .expect("scripted source")
            .last_client_message(),
        Some(ClientMessage::SetScrollOffset {
            rows_from_live_edge: 8
        })
    ));
    ws.apply_scroll_applied(native, 8, 40);
    assert_eq!(ws.pane(native).scroll_offset(), 8);
    let other = ws.open_terminal("n2", "native", "epoch-n").unwrap();
    ws.attach_frames(other).unwrap();
    ws.set_scroll_offset(other, 2).unwrap();
    assert_eq!(ws.pane(native).scroll_offset(), 8);
    assert_eq!(ws.pane(other).scroll_offset(), 2);
    ws.push_frame(native, "live");
    assert!(ws.pane(native).has_new_output());
    ws.drop_and_reconnect_frames(native).unwrap();
    assert_eq!(ws.pane(native).scroll_offset(), 8);
    ws.jump_to_bottom(native).unwrap();
    assert_eq!(ws.pane(native).scroll_offset(), 0);
    let source = ws.pane(native).scripted_source().expect("scripted source");
    assert!(!source.sent_host_input());
    assert!(!source.sent_mouse_report());
    assert!(!source.sent_tiocswinsz());

    let tmux = ws.open_terminal("tm", "tmux", "epoch-t").unwrap();
    ws.seed_attach_history(tmux, "wide 👩‍💻 continues\u{23CE}on wrap\nhard line\n");
    let hist = ws.pane(tmux).attach_history().unwrap();
    assert!(hist.contains('\u{23CE}'));
    assert!(hist.contains('\n'));
    let logical = extract_logical_line(hist, 0);
    assert!(logical.contains("👩‍💻"));
    assert!(!logical.contains("hard line"));
}

#[test]
fn paste_is_lease_gated_and_bracketed() {
    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    // A proxy attachment: the bracketing and the write sequence this test
    // follows are the daemon paste protocol. gterm brackets a granted `Paste`
    // itself, so a direct pane has neither (#22573).
    ws.reattach_frames(pane).unwrap();
    ws.focus_pane(pane).unwrap();
    ws.set_bracketed_paste(pane, true);
    let text = "one\ntwo\nthree";
    ws.paste_to_pty(pane, text).unwrap();
    let pty = ws.daemon().last_pty_write().expect("pty write");
    assert!(pty.starts_with("\u{1b}[200~"));
    assert!(pty.ends_with("\u{1b}[201~"));
    assert_eq!(pty.matches('\n').count(), 2);
    assert_eq!(ws.daemon().pty_mutation_count(), 1);
    let paste_seq = ws.daemon().last_paste_seq().unwrap();
    ws.apply_ws(&json!({
        "type": "terminal_write_outcome",
        "terminal_id": "term-a",
        "attachment_id": ws.pane(pane).attachment_id(),
        "client_write_seq": paste_seq,
        "outcome": "delivered",
        "reason": null
    }))
    .unwrap();

    ws.release_control(pane).unwrap();
    let mutations = ws.daemon().pty_mutation_count();
    assert!(ws.paste_to_pty(pane, "nope").is_err());
    assert_eq!(ws.daemon().pty_mutation_count(), mutations);

    ws.focus_pane(pane).unwrap();
    let oversize = "x".repeat(PASTE_MAX_BYTES + 1);
    let err = ws.paste_to_pty(pane, &oversize).unwrap_err();
    assert_eq!(err.code(), "paste_too_large");

    ws.paste_to_pty(pane, "uncertain").unwrap();
    let uncertain_seq = ws.daemon().last_paste_seq().unwrap();
    ws.apply_ws(&json!({
        "type": "terminal_write_outcome",
        "terminal_id": "term-a",
        "attachment_id": ws.pane(pane).attachment_id(),
        "client_write_seq": uncertain_seq,
        "outcome": "indeterminate",
        "reason": "indeterminate_backend"
    }))
    .unwrap();
    assert!(ws.pane(pane).is_uncertain_readonly());
    let after_indeterminate = ws.daemon().pty_mutation_count();
    assert!(ws.paste_to_pty(pane, "must-not-resend").is_err());
    assert_eq!(ws.daemon().pty_mutation_count(), after_indeterminate);

    ws.enter_copy_search(pane);
    ws.paste_to_pty(pane, "query").unwrap();
    assert_eq!(ws.pane(pane).search_buffer(), "query");
    assert_eq!(ws.daemon().pty_mutation_count(), after_indeterminate);
}

/// A held native pane with a direct source pastes onto the frame stream: the
/// host sees one `Paste` after one `BindAttachment`, and the daemon carries
/// nothing (#22573). gterm brackets the text, so gclient sends it raw.
#[test]
fn a_direct_paste_reaches_the_host_not_the_daemon() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-direct", "native", "epoch-direct")
        .expect("open terminal");
    ws.attach_frames(pane).expect("attach frames");
    ws.focus_pane(pane).expect("focus takes control");
    ws.set_bracketed_paste(pane, true);
    let mutations = ws.daemon().pty_mutation_count();

    ws.paste_to_pty(pane, "one\ntwo").expect("paste");

    assert_eq!(
        ws.daemon().pty_mutation_count(),
        mutations,
        "a direct pane's paste never reaches the daemon"
    );
    let source = ws.pane(pane).scripted_source().expect("scripted source");
    assert!(source.sent_host_input(), "the host received the paste");
    let pasted: Vec<&String> = source
        .sent_messages()
        .iter()
        .filter_map(|message| match message {
            ClientMessage::Paste { text } => Some(text),
            _ => None,
        })
        .collect();
    assert_eq!(pasted, vec![&"one\ntwo".to_string()]);
    assert_eq!(
        source
            .sent_messages()
            .iter()
            .filter(|message| matches!(message, ClientMessage::BindAttachment { .. }))
            .count(),
        1,
        "one bind per installed source"
    );

    ws.release_control(pane).expect("release");
    assert!(
        ws.paste_to_pty(pane, "nope").is_err(),
        "an observing pane still refuses to type anywhere"
    );
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
        width: width as u16,
        height: rows.len() as u16,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes,
    }
}

/// Open `terminal` in `ws` and deliver `frame` as its latest frame.
async fn pane_showing(ws: &mut Workspace, terminal: &str, frame: FrameData) -> PaneId {
    let pane = ws
        .open_terminal(terminal, "native", "epoch-select")
        .expect("open terminal");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(frame));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("replace source");
    ws.recv_pane_frame(pane).await.expect("receive frame");
    pane
}

#[tokio::test]
async fn selection_spanning_scrollback_requests_read_text_and_copies_reply() {
    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-scrollback-copy", "native", "epoch-copy")
        .expect("open terminal");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(frame_of(
        &["near", "live"],
        PaneModes::default(),
    )));
    source.queue(ServerMessage::TextRead {
        text: "offscreen\nvisible".into(),
        truncated: false,
    });
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("replace source");
    ws.recv_pane_frame(pane).await.expect("receive frame");
    ws.apply_scroll_applied(pane, 0, 10);

    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "copy");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let inner = inner_rect(&chrome, slot);
    anchor_selection(&mut chrome, ws.pane(pane), slot, inner, 0, 1);
    ws.apply_scroll_applied(pane, 5, 10);
    extend_selection(&mut chrome, ws.pane(pane), inner, inner.x + 2, inner.y);
    assert!(finish_selection(&mut chrome));

    let mut output = Vec::new();
    assert!(copy_or_request_selection(&mut ws, &mut chrome, &mut output)
        .await
        .expect("request full selection"));
    assert!(output.is_empty());
    assert!(
        chrome.selection.is_some(),
        "selection stays visible while reading"
    );
    assert!(matches!(
        ws.pane(pane)
            .scripted_source()
            .expect("scripted source")
            .sent_messages()
            .last(),
        Some(ClientMessage::ReadText {
            start_rows_from_live_edge: 6,
            start_col: 2,
            end_rows_from_live_edge: 0,
            end_col: 0,
        })
    ));

    let reply = ws.recv_pane_frame(pane).await.expect("text read reply");
    let ServerMessage::TextRead { text, .. } = reply else {
        panic!("expected text read")
    };
    assert!(apply_text_read(&mut chrome, pane, text, &mut output).expect("copy reply"));
    assert_eq!(output, b"\x1b]52;c;b2Zmc2NyZWVuCnZpc2libGU=\x07");
    assert_eq!(chrome.last_copy.as_deref(), Some("offscreen\nvisible"));
    assert!(chrome.selection.is_none());

    let sent_before = ws
        .pane(pane)
        .scripted_source()
        .expect("scripted source")
        .sent_messages()
        .len();
    anchor_selection(&mut chrome, ws.pane(pane), slot, inner, 0, 0);
    extend_selection(&mut chrome, ws.pane(pane), inner, inner.x + 3, inner.y + 1);
    assert!(finish_selection(&mut chrome));
    let mut immediate = Vec::new();
    assert!(
        copy_or_request_selection(&mut ws, &mut chrome, &mut immediate)
            .await
            .expect("copy visible selection")
    );
    assert_eq!(immediate, b"\x1b]52;c;bmVhcmxpdmU=\x07");
    assert_eq!(
        ws.pane(pane)
            .scripted_source()
            .expect("scripted source")
            .sent_messages()
            .len(),
        sent_before
    );
}

fn inner_rect(chrome: &Chrome, slot: LayoutPaneId) -> Rect {
    chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .expect("pane geometry")
        .inner_rect
}

fn mouse(
    kind: MouseEventKind,
    inner: Rect,
    col: u16,
    row: u16,
    modifiers: KeyModifiers,
) -> MouseEvent {
    MouseEvent {
        kind,
        column: inner.x + col,
        row: inner.y + row,
        modifiers,
    }
}

#[tokio::test]
async fn terminal_mode_drag_selects_and_copies_on_release() {
    let mut ws = Workspace::scripted();
    let frame = frame_of(&["hello world", "second row"], PaneModes::default());
    let pane = pane_showing(&mut ws, "term-drag", frame).await;
    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "drag");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    assert_eq!(chrome.focused_pane(), Some(pane));
    assert_eq!(chrome.mode, Mode::Terminal);
    let inner = inner_rect(&chrome, slot);
    let left = |kind, col, row| mouse(kind, inner, col, row, KeyModifiers::NONE);

    let down = left(MouseEventKind::Down(MouseButton::Left), 0, 0);
    assert_eq!(route_mouse(&ws, &mut chrome, &down), MouseOutcome::Handled);
    assert_eq!(chrome.gesture, Some(MouseGesture::Select { slot }));
    assert!(chrome
        .selection
        .as_ref()
        .is_some_and(|selection| !selection.is_visible()));
    let drag = left(MouseEventKind::Drag(MouseButton::Left), 4, 0);
    assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Handled);
    assert!(chrome.selection.as_ref().is_some_and(Selection::is_visible));
    let up = left(MouseEventKind::Up(MouseButton::Left), 4, 0);
    assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Copy);
    assert_eq!(chrome.gesture, None);
    assert!(chrome
        .selection
        .as_ref()
        .is_some_and(Selection::is_finalized));
    let mut output = Vec::new();
    assert!(copy_selection(&ws, &mut chrome, &mut output).expect("copy selection"));
    assert_eq!(output, b"\x1b]52;c;aGVsbG8=\x07");
    assert_eq!(chrome.last_copy.as_deref(), Some("hello"));

    // A completed click without movement clears the selection and takes an
    // uncontrolled pane instead of copying.
    let down = left(MouseEventKind::Down(MouseButton::Left), 2, 1);
    assert_eq!(route_mouse(&ws, &mut chrome, &down), MouseOutcome::Handled);
    assert!(chrome.selection.is_some());
    let up = left(MouseEventKind::Up(MouseButton::Left), 2, 1);
    assert_eq!(
        route_mouse(&ws, &mut chrome, &up),
        MouseOutcome::TakeFreeControl { pane }
    );
    assert!(chrome.selection.is_none());
    assert!(!copy_selection(&ws, &mut chrome, &mut Vec::new()).expect("nothing to copy"));
    assert_eq!(chrome.last_copy.as_deref(), Some("hello"));
}

#[tokio::test]
async fn double_and_triple_click_select_token_and_row() {
    let mut ws = Workspace::scripted();
    let frame = frame_of(&["cd ~/src/gobby now  ", "x"], PaneModes::default());
    let pane = pane_showing(&mut ws, "term-token", frame).await;
    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "token");
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let inner = inner_rect(&chrome, slot);
    let click = |chrome: &mut Chrome, col: u16| {
        let down = mouse(
            MouseEventKind::Down(MouseButton::Left),
            inner,
            col,
            0,
            KeyModifiers::NONE,
        );
        let pressed = route_mouse(&ws, chrome, &down);
        let up = mouse(
            MouseEventKind::Up(MouseButton::Left),
            inner,
            col,
            0,
            KeyModifiers::NONE,
        );
        route_mouse(&ws, chrome, &up);
        pressed
    };

    // One click anchors and clears; the second takes the token; the third the
    // row without its blank tail; the fourth starts over.
    assert_eq!(click(&mut chrome, 6), MouseOutcome::Handled);
    assert!(chrome.selection.is_none());
    assert_eq!(click(&mut chrome, 6), MouseOutcome::Copy);
    assert!(chrome
        .selection
        .as_ref()
        .is_some_and(Selection::is_finalized));
    assert!(copy_selection(&ws, &mut chrome, &mut Vec::new()).expect("copy token"));
    assert_eq!(chrome.last_copy.as_deref(), Some("~/src/gobby"));
    assert_eq!(click(&mut chrome, 6), MouseOutcome::Copy);
    assert!(copy_selection(&ws, &mut chrome, &mut Vec::new()).expect("copy row"));
    assert_eq!(chrome.last_copy.as_deref(), Some("cd ~/src/gobby now"));
    assert_eq!(click(&mut chrome, 6), MouseOutcome::Handled);
    assert!(chrome.selection.is_none());

    // A double-click on a blank cell selects nothing.
    assert_eq!(click(&mut chrome, 2), MouseOutcome::Handled);
    assert_eq!(click(&mut chrome, 2), MouseOutcome::Handled);
    assert!(chrome.selection.is_none());

    // A pane that reports the mouse gets a plain press as an X10 report and
    // captures the button; shift selects there anyway.
    let modes = PaneModes {
        mouse_all: true,
        ..PaneModes::default()
    };
    let reporting = pane_showing(&mut ws, "term-mouse", frame_of(&["vim buffer"], modes)).await;
    let mouse_slot = chrome.open_pane(reporting, "mouse");
    assert!(chrome.focus_pane(reporting));
    chrome.compute_view(&ws, Rect::new(0, 0, 120, 40));
    let inner = inner_rect(&chrome, mouse_slot);
    let press = |modifiers| {
        mouse(
            MouseEventKind::Down(MouseButton::Left),
            inner,
            0,
            0,
            modifiers,
        )
    };
    assert_eq!(
        route_mouse(&ws, &mut chrome, &press(KeyModifiers::NONE)),
        MouseOutcome::Write {
            pane: reporting,
            bytes: b"\x1b[M !!".to_vec()
        }
    );
    assert!(
        matches!(chrome.gesture, Some(MouseGesture::Forwarding { .. })),
        "{:?}",
        chrome.gesture
    );
    assert_eq!(
        route_mouse(&ws, &mut chrome, &press(KeyModifiers::SHIFT)),
        MouseOutcome::Handled
    );
    assert_eq!(
        chrome.gesture,
        Some(MouseGesture::Select { slot: mouse_slot })
    );
    let drag = mouse(
        MouseEventKind::Drag(MouseButton::Left),
        inner,
        2,
        0,
        KeyModifiers::SHIFT,
    );
    assert_eq!(route_mouse(&ws, &mut chrome, &drag), MouseOutcome::Handled);
    let up = mouse(
        MouseEventKind::Up(MouseButton::Left),
        inner,
        2,
        0,
        KeyModifiers::SHIFT,
    );
    assert_eq!(route_mouse(&ws, &mut chrome, &up), MouseOutcome::Copy);
    assert!(copy_selection(&ws, &mut chrome, &mut Vec::new()).expect("copy shifted drag"));
    assert_eq!(chrome.last_copy.as_deref(), Some("vim"));
}
