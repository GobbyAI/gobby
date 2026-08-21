//! 3.3.13 / 3.3.14 / 3.3.20 scrollback, copy, and paste.

use gobby_client::copy_mode::{extract_logical_line, PASTE_MAX_BYTES};
use gobby_client::Workspace;
use gobby_terminal::protocol::ClientMessage;
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

    let line = extract_logical_line("👩‍💻 wraps onto two cells then continues", 4);
    assert!(
        line.contains("👩‍💻"),
        "wide grapheme stays on the logical line: {line}"
    );

    ws.push_frame(a, "new-output");
    assert!(ws.pane(a).has_new_output());
    assert!(ws.daemon().pty_mutation_count() == 0);
    assert!(!ws.frames().sent_host_input());
    assert!(!ws.frames().sent_resize());

    let joiner = ws.open_terminal("term-join", "tmux", "epoch-a").unwrap();
    ws.seed_attach_history(joiner, "later joiner history");
    assert!(ws.pane(joiner).copy_seeded_from_history());
    assert!(!ws.pane(joiner).required_created_flag());
}

#[test]
fn native_set_scroll_offset_and_tmux_wrap_history() {
    let mut ws = Workspace::scripted();
    let native = ws.open_terminal("n1", "native", "epoch-n").unwrap();
    ws.attach_frames(native).unwrap();
    ws.set_scroll_offset(native, 8).unwrap();
    assert!(matches!(
        ws.frames().last_client_message(),
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
    assert!(!ws.frames().sent_host_input());
    assert!(!ws.frames().sent_mouse_report());
    assert!(!ws.frames().sent_tiocswinsz());

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
    ws.attach_frames(pane).unwrap();
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

    ws.enter_copy_search(pane);
    ws.paste_local(pane, "query").unwrap();
    assert_eq!(ws.pane(pane).search_buffer(), "query");
    assert_eq!(ws.daemon().pty_mutation_count(), mutations);
}
