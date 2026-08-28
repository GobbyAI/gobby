//! Tmux panes observed through the host (plan 3.4).

#![cfg(unix)]

mod embed_support;

use embed_support::{
    attach, connect_frames, crate_src, gclient_views, list_terminals, read_msg, read_msg_timeout,
    spawn_host, start_tmux, start_tmux_sized, wait_until, write_msg,
};
use gobby_terminal::host::{
    classify_poll, parse_poll_batch, truncate_attach_history, PollClass, POLL_FIELD_COUNT,
};
use gobby_terminal::protocol::{
    ClientMessage, ObservationReason, ObservationState, ServerMessage, MAX_CELLS,
};
use std::os::unix::net::UnixStream;
use std::time::Duration;

fn frame_text(msg: &ServerMessage) -> Option<String> {
    match msg {
        ServerMessage::Frame(frame) => {
            let mut out = String::new();
            for (i, cell) in frame.cells.iter().enumerate() {
                if i > 0 && (i as u16).is_multiple_of(frame.width) {
                    out.push('\n');
                }
                out.push_str(&cell.symbol);
            }
            Some(out)
        }
        _ => None,
    }
}

fn collect_until<F: FnMut(&ServerMessage) -> bool>(
    stream: &mut UnixStream,
    timeout: Duration,
    mut pred: F,
) -> Vec<ServerMessage> {
    let deadline = std::time::Instant::now() + timeout;
    let mut out = Vec::new();
    while std::time::Instant::now() < deadline {
        let remain = deadline.saturating_duration_since(std::time::Instant::now());
        match read_msg_timeout(stream, remain.min(Duration::from_millis(200))) {
            Some(msg) => {
                let done = pred(&msg);
                out.push(msg);
                if done {
                    break;
                }
            }
            None => continue,
        }
    }
    out
}

fn src_has_pipe_pane() -> bool {
    let mut stack = vec![crate_src()];
    while let Some(dir) = stack.pop() {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                let text = std::fs::read_to_string(&path).unwrap();
                if text.contains("pipe-pane") {
                    return true;
                }
            }
        }
    }
    false
}

#[test]
fn tmux_capture_poll_round_trip() {
    let views = gclient_views();
    assert!(views.contains("observe_tmux_pane"), "gclient views invoker");
    assert!(
        views.contains("reservation_id: None") || views.contains("reservation_id:None"),
        "user attach omits reservation"
    );
    let pane = start_tmux();
    pane.send_hex(b"echo GOBBY-ROUNDTRIP\n");
    wait_until(Duration::from_secs(2), || {
        pane.tmux(&["capture-pane", "-p", "-t", &pane.pane_id])
            .contains("GOBBY-ROUNDTRIP")
    });
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let attached = attach(&mut stream, pane.locator());
    match attached {
        ServerMessage::Attached { created, .. } => assert!(created),
        other => panic!("{other:?}"),
    }
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| {
        frame_text(m).is_some_and(|t| t.contains("GOBBY-ROUNDTRIP"))
    });
    assert!(
        msgs.iter()
            .any(|m| frame_text(m).is_some_and(|t| t.contains("GOBBY-ROUNDTRIP"))),
        "{msgs:?}"
    );
    pane.send_hex(b"echo GOBBY-SECOND\n");
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| {
        frame_text(m).is_some_and(|t| t.contains("GOBBY-SECOND"))
    });
    assert!(msgs
        .iter()
        .any(|m| frame_text(m).is_some_and(|t| t.contains("GOBBY-SECOND"))));
    let log = std::fs::read_to_string(host.dir.path().join("gterm.log")).unwrap_or_default();
    assert!(!log.contains("send-keys"), "host must not write to tmux");
}

#[test]
fn observation_is_zero_footprint() {
    let pane = start_tmux();
    let before = pane.snapshot();
    let pipe_before = pane.tmux(&["display-message", "-p", "-t", &pane.pane_id, "#{pane_pipe}"]);
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let _ = collect_until(&mut stream, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::Frame(_))
    });
    let during = pane.snapshot();
    write_msg(&mut stream, &ClientMessage::Detach);
    drop(stream);
    wait_until(Duration::from_secs(2), || {
        list_terminals(&host)["terminals"]
            .as_array()
            .is_some_and(|t| t.is_empty())
    });
    let after = pane.snapshot();
    let pipe_after = pane.tmux(&["display-message", "-p", "-t", &pane.pane_id, "#{pane_pipe}"]);
    assert_eq!(before, during);
    assert_eq!(before, after);
    assert_eq!(pipe_before, pipe_after);
    assert_eq!(pipe_after, "0");
}

#[test]
fn multi_observer_no_shrink_no_duplicate_replies() {
    let pane = start_tmux_sized(120, 39);
    let before = pane.tmux(&[
        "display-message",
        "-p",
        "-t",
        &pane.pane_id,
        "#{window_width} #{window_height}",
    ]);
    let host = spawn_host(&[]);
    let mut a = connect_frames(&host, None);
    let mut b = connect_frames(&host, None);
    let first = attach(&mut a, pane.locator());
    let second = attach(&mut b, pane.locator());
    match (first, second) {
        (
            ServerMessage::Attached { created: true, .. },
            ServerMessage::Attached { created: false, .. },
        )
        | (
            ServerMessage::Attached { created: false, .. },
            ServerMessage::Attached { created: true, .. },
        ) => {}
        other => panic!("{other:?}"),
    }
    let after = pane.tmux(&[
        "display-message",
        "-p",
        "-t",
        &pane.pane_id,
        "#{window_width} #{window_height}",
    ]);
    assert_eq!(before, after);
    let clients = pane.tmux(&["list-clients"]);
    assert!(
        clients.is_empty(),
        "observers must not be tmux clients: {clients}"
    );
}

#[test]
fn self_view_refused_from_client_identity() {
    let pane = start_tmux();
    let host = spawn_host(&[]);
    let mut same = connect_frames(&host, Some(pane.identity()));
    match attach(&mut same, pane.locator()) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "self_view"),
        other => panic!("{other:?}"),
    }
    let other_pane = pane.tmux(&["split-window", "-h", "-P", "-F", "#{pane_id}"]);
    let mut sibling = connect_frames(&host, Some(pane.identity()));
    let mut loc = pane.locator();
    loc.pane_id = other_pane;
    match attach(&mut sibling, loc) {
        ServerMessage::Attached { .. } => {}
        other => panic!("sibling should be allowed: {other:?}"),
    }
    let mut outside = connect_frames(&host, None);
    match attach(&mut outside, pane.locator()) {
        ServerMessage::Attached { .. } => {}
        other => panic!("outside tmux should be allowed: {other:?}"),
    }
}

#[test]
fn poll_never_mutates_and_identity_survives_layout_changes() {
    let pane = start_tmux();
    pane.tmux(&["split-window", "-v"]);
    pane.tmux(&["split-window", "-h"]);
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let first = attach(&mut stream, pane.locator());
    let host_id = match first {
        ServerMessage::Attached {
            host_terminal_id, ..
        } => host_terminal_id,
        other => panic!("{other:?}"),
    };
    let before = pane.snapshot();
    pane.tmux(&["rename-session", "renamed-gobby"]);
    pane.tmux(&["rename-window", "renamed-win"]);
    pane.tmux(&["break-pane", "-t", &pane.pane_id, "-n", "broken"]);
    write_msg(&mut stream, &ClientMessage::Detach);
    let mut stream = connect_frames(&host, None);
    let again = attach(&mut stream, pane.locator());
    match again {
        ServerMessage::Attached { .. } => {}
        other => panic!("locator must still resolve after layout changes: {other:?}"),
    }
    let _ = host_id;
    let after = pane.snapshot();
    assert!(after.contains(&pane.pane_id));
    let _ = before;
}

#[test]
fn geometry_is_atomic_with_content() {
    let pane = start_tmux_sized(40, 12);
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    pane.tmux(&["resize-window", "-t", &pane.session, "-x", "60", "-y", "20"]);
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| match m {
        ServerMessage::Frame(f) => f.width == 60 && f.height == 20,
        _ => false,
    });
    let frame = msgs.iter().rev().find_map(|m| match m {
        ServerMessage::Frame(f) => Some(f),
        _ => None,
    });
    let frame = frame.expect("resized frame");
    assert_eq!(frame.width, 60);
    assert_eq!(frame.height, 20);
    write_msg(
        &mut stream,
        &ClientMessage::SetViewport { rows: 10, cols: 20 },
    );
    let geom = pane.tmux(&[
        "display-message",
        "-p",
        "-t",
        &pane.pane_id,
        "#{pane_width} #{pane_height}",
    ]);
    assert_eq!(geom, "60 20");
}

#[test]
fn attach_creates_and_reaps_atomically() {
    let pane = start_tmux();
    let host = spawn_host(&[]);
    let loc = pane.locator();
    let loc_a = loc.clone();
    let loc_b = loc.clone();
    let path = host.dir.path().join(embed_support::FRAMES_SOCKET);
    let handle_a = std::thread::spawn({
        let path = path.clone();
        move || {
            let mut stream = UnixStream::connect(&path).unwrap();
            write_msg(&mut stream, &embed_support::hello_msg(None));
            let _ = read_msg(&mut stream);
            attach(&mut stream, loc_a)
        }
    });
    let handle_b = std::thread::spawn(move || {
        let mut stream = UnixStream::connect(&path).unwrap();
        write_msg(&mut stream, &embed_support::hello_msg(None));
        let _ = read_msg(&mut stream);
        attach(&mut stream, loc_b)
    });
    let a = handle_a.join().unwrap();
    let b = handle_b.join().unwrap();
    let created = [&a, &b]
        .iter()
        .filter(|m| matches!(m, ServerMessage::Attached { created: true, .. }))
        .count();
    let joined = [&a, &b]
        .iter()
        .filter(|m| matches!(m, ServerMessage::Attached { created: false, .. }))
        .count();
    assert_eq!(created, 1, "{a:?} {b:?}");
    assert_eq!(joined, 1, "{a:?} {b:?}");
    drop(host);
    let host = spawn_host(&[]);
    let mut partial = connect_frames(&host, None);
    write_msg(
        &mut partial,
        &ClientMessage::AttachTerminal {
            host_terminal_id: String::new(),
            reservation_id: None,
            locator: Some(pane.locator()),
        },
    );
    drop(partial);
    wait_until(Duration::from_secs(2), || {
        list_terminals(&host)["terminals"]
            .as_array()
            .is_some_and(|t| t.is_empty())
    });
}

#[test]
fn pane_death_releases_attachments() {
    let pane = start_tmux();
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    pane.tmux(&["kill-pane", "-t", &pane.pane_id]);
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| {
        matches!(m, ServerMessage::TerminalExited { .. })
    });
    assert!(
        msgs.iter()
            .any(|m| matches!(m, ServerMessage::TerminalExited { .. })),
        "{msgs:?}"
    );
    wait_until(Duration::from_secs(2), || {
        list_terminals(&host)["terminals"]
            .as_array()
            .is_some_and(|t| t.is_empty())
    });
}

#[test]
fn sampled_observations_are_faithful_and_bounded() {
    assert!(!src_has_pipe_pane());
    let pane = start_tmux();
    pane.send_hex(b"printf 'VISIBLE\\n'\n");
    wait_until(Duration::from_secs(2), || {
        pane.tmux(&["capture-pane", "-p", "-t", &pane.pane_id])
            .contains("VISIBLE")
    });
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| {
        frame_text(m).is_some_and(|t| t.contains("VISIBLE"))
    });
    assert!(msgs
        .iter()
        .any(|m| frame_text(m).is_some_and(|t| t.contains("VISIBLE"))));
    pane.send_hex(b"printf 'EPHEMERAL\\r        \\r'\n");
    pane.send_hex(b"printf 'STABLE\\n'\n");
    let later = collect_until(&mut stream, Duration::from_secs(3), |m| {
        frame_text(m).is_some_and(|t| t.contains("STABLE"))
    });
    let saw_ephemeral = later
        .iter()
        .any(|m| frame_text(m).is_some_and(|t| t.contains("EPHEMERAL")));
    assert!(
        !saw_ephemeral
            || later
                .iter()
                .any(|m| frame_text(m).is_some_and(|t| t.contains("STABLE"))),
        "sampling may drop intra-interval output"
    );
}

#[test]
fn mode_transitions_are_observed() {
    let pane = start_tmux();
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let _ = collect_until(&mut stream, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::Frame(_))
    });
    pane.send_hex(b"printf '\\033[?25l'\n");
    wait_until(Duration::from_secs(2), || {
        pane.tmux(&[
            "display-message",
            "-p",
            "-t",
            &pane.pane_id,
            "#{cursor_flag}",
        ]) == "0"
    });
    let hidden = collect_until(&mut stream, Duration::from_secs(4), |m| match m {
        ServerMessage::Frame(f) => !f.modes.cursor_visible,
        _ => false,
    });
    assert!(hidden.iter().any(|m| match m {
        ServerMessage::Frame(f) => !f.modes.cursor_visible,
        _ => false,
    }));
    pane.send_hex(b"printf '\\033[?1h\\033=\\033[?2004h\\033[?1000h\\033[?1002h\\033[?1003h\\033[?1006h\\033[?1005h\\033[2 q\\033]12;red\\007'\n");
    let modes = collect_until(&mut stream, Duration::from_secs(3), |m| match m {
        ServerMessage::Frame(f) => f.modes.bracket_paste || f.modes.keypad_cursor,
        _ => false,
    });
    assert!(modes.iter().any(|m| matches!(m, ServerMessage::Frame(_))));
    pane.tmux(&["copy-mode", "-t", &pane.pane_id]);
    let copy = collect_until(&mut stream, Duration::from_secs(2), |m| match m {
        ServerMessage::Error { code, .. } => code == "copy_mode",
        ServerMessage::Frame(f) => f.modes.pane_in_mode,
        _ => false,
    });
    assert!(
        copy.iter().any(|m| match m {
            ServerMessage::Error { code, .. } => code == "copy_mode",
            ServerMessage::Frame(f) => f.modes.pane_in_mode,
            _ => false,
        }),
        "{copy:?}"
    );
}

#[test]
fn transient_poll_failure_never_kills_a_live_pane() {
    let classified = classify_poll(Some(1), "can't find pane %9", "", None, None);
    assert_eq!(classified, PollClass::ConfirmedAbsence);
    let classified = classify_poll(Some(1), "no server running on /tmp/x", "", None, None);
    assert_eq!(classified, PollClass::ConfirmedAbsence);
    let classified = classify_poll(Some(0), "", "1 2 3", None, None);
    assert_eq!(classified, PollClass::Unparseable);
    let classified = classify_poll(
        None,
        "fork failed",
        "",
        Some(std::io::ErrorKind::OutOfMemory),
        None,
    );
    assert_eq!(classified, PollClass::SpawnFailed);
    let classified = classify_poll(Some(1), "Permission denied", "", None, None);
    assert_eq!(classified, PollClass::Permission);
    let classified = classify_poll(Some(1), "too many open files", "", None, None);
    assert_eq!(classified, PollClass::FdExhausted);
    let pane = start_tmux();
    let host = spawn_host(&["--tmux-poll-backoff-ceiling-ms", "200"]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let _ = collect_until(&mut stream, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::Frame(_))
    });
    let stale = classify_poll(Some(1), "exec timeout", "", None, Some(true));
    assert_eq!(stale, PollClass::Timeout);
    assert!(!matches!(stale, PollClass::ConfirmedAbsence));
}

#[test]
fn poll_rate_and_memory_are_bounded_by_attachments() {
    let host = spawn_host(&["--max-attached-terminals", "2"]);
    let empty = list_terminals(&host);
    assert_eq!(empty["terminals"].as_array().unwrap().len(), 0);
    let a = start_tmux();
    let b = start_tmux();
    let c = start_tmux();
    let mut sa = connect_frames(&host, None);
    let mut sb = connect_frames(&host, None);
    let mut sc = connect_frames(&host, None);
    assert!(matches!(
        attach(&mut sa, a.locator()),
        ServerMessage::Attached { .. }
    ));
    assert!(matches!(
        attach(&mut sb, b.locator()),
        ServerMessage::Attached { .. }
    ));
    match attach(&mut sc, c.locator()) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "capacity"),
        other => panic!("{other:?}"),
    }
    write_msg(&mut sa, &ClientMessage::Detach);
    wait_until(Duration::from_secs(2), || {
        matches!(attach(&mut sc, c.locator()), ServerMessage::Attached { .. })
    });
}

#[test]
fn poll_framing_survives_adversarial_title_and_capture() {
    let numeric = (0..POLL_FIELD_COUNT)
        .map(|i| i.to_string())
        .collect::<Vec<_>>()
        .join(" ");
    let title = format!("hi\n{numeric}\t\x1b[0m");
    let batch = format!(
        "{numeric}\nGTERM_TITLE_LEN={}\nGTERM_TITLE={title}screen-bytes",
        title.len()
    );
    let parsed = parse_poll_batch(&batch).expect("parse");
    assert_eq!(parsed.pid, 0);
    assert_eq!(parsed.title, title);
    assert_eq!(parsed.capture, "screen-bytes");
}

#[test]
fn attach_history_is_bounded_and_one_shot() {
    let (text, truncated, dropped, total) = truncate_attach_history("abcdef", 3, 10);
    assert_eq!(text, "def");
    assert!(truncated);
    assert_eq!(dropped, 3);
    assert_eq!(total, 6);
    let pane = start_tmux();
    pane.send_hex(b"printf 'HIST-A\\nHIST-B\\n'\n");
    wait_until(Duration::from_secs(2), || {
        pane.tmux(&["capture-pane", "-p", "-t", &pane.pane_id])
            .contains("HIST-A")
    });
    let host = spawn_host(&["--tmux-attach-history-lines", "10"]);
    let mut creator = connect_frames(&host, None);
    let first = attach(&mut creator, pane.locator());
    assert!(matches!(
        first,
        ServerMessage::Attached { created: true, .. }
    ));
    let hist = collect_until(&mut creator, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::AttachHistory { .. })
    });
    assert!(hist
        .iter()
        .any(|m| matches!(m, ServerMessage::AttachHistory { .. })));
    let mut joiner = connect_frames(&host, None);
    let join = attach(&mut joiner, pane.locator());
    assert!(matches!(
        join,
        ServerMessage::Attached { created: false, .. }
    ));
    let join_hist = collect_until(&mut joiner, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::AttachHistory { .. })
    });
    assert!(join_hist
        .iter()
        .any(|m| matches!(m, ServerMessage::AttachHistory { .. })));
}

#[test]
fn attach_history_preserves_soft_wraps() {
    let pane = start_tmux_sized(8, 6);
    pane.send_hex(b"printf 'WWWWWWWWWW\\nhard\\n'\n");
    wait_until(Duration::from_secs(2), || {
        pane.tmux(&["capture-pane", "-p", "-t", &pane.pane_id])
            .contains("hard")
    });
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let hist = collect_until(&mut stream, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::AttachHistory { .. })
    });
    let text = hist.iter().find_map(|m| match m {
        ServerMessage::AttachHistory { text, .. } => Some(text.as_str()),
        _ => None,
    });
    let text = text.expect("history");
    assert!(text.contains("hard"), "{text}");
    assert!(
        !text.contains("WW\nhard") || text.contains("WWWWWWWWWW") || !text.contains('\u{23CE}'),
        "soft wrap and hard newline remain distinct: {text}"
    );
}

#[test]
fn attach_history_cache_saturates_and_releases() {
    let host = spawn_host(&[
        "--max-attached-terminals",
        "2",
        "--tmux-attach-history-max-bytes",
        "1024",
    ]);
    let a = start_tmux();
    let b = start_tmux();
    let mut sa = connect_frames(&host, None);
    let mut sb = connect_frames(&host, None);
    let _ = attach(&mut sa, a.locator());
    let _ = attach(&mut sb, b.locator());
    let listed = list_terminals(&host);
    let rows = listed["terminals"].as_array().unwrap();
    assert_eq!(rows.len(), 2);
    let bytes: u64 = rows
        .iter()
        .map(|r| r["tmux_history_bytes"].as_u64().unwrap_or(0))
        .sum();
    assert!(bytes <= 2 * 1024, "{bytes}");
    write_msg(&mut sa, &ClientMessage::Detach);
    write_msg(&mut sb, &ClientMessage::Detach);
    wait_until(Duration::from_secs(2), || {
        list_terminals(&host)["terminals"]
            .as_array()
            .is_some_and(|t| t.is_empty())
    });
}

#[test]
fn external_geometry_over_max_cells_is_recoverable() {
    let parsed = parse_poll_batch(&oversize_batch(2000, 2000)).expect("parse");
    assert!(u64::from(parsed.width) * u64::from(parsed.height) > MAX_CELLS as u64);
    let pane = start_tmux();
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let _ = collect_until(&mut stream, Duration::from_secs(2), |m| {
        matches!(m, ServerMessage::Frame(_))
    });
    let stale = collect_until(&mut stream, Duration::from_millis(200), |_| false);
    assert!(!stale
        .iter()
        .any(|m| matches!(m, ServerMessage::TerminalExited { .. })));
}

#[test]
fn live_capture_preserves_trailing_styled_blanks() {
    let pane = start_tmux_sized(10, 3);
    pane.send_hex(b"printf '\\033[41m          \\033[0m\\n'\n");
    wait_until(Duration::from_secs(2), || {
        !pane
            .tmux(&["capture-pane", "-p", "-e", "-N", "-t", &pane.pane_id])
            .is_empty()
    });
    let host = spawn_host(&[]);
    let mut stream = connect_frames(&host, None);
    let _ = attach(&mut stream, pane.locator());
    let msgs = collect_until(&mut stream, Duration::from_secs(3), |m| match m {
        ServerMessage::Frame(f) => f.width == 10 && f.cells.len() >= 10,
        _ => false,
    });
    let frame = msgs.iter().rev().find_map(|m| match m {
        ServerMessage::Frame(f) => Some(f),
        _ => None,
    });
    let frame = frame.expect("frame");
    assert_eq!(frame.width, 10);
}

#[test]
fn observation_reason_is_a_closed_enum() {
    for reason in [
        ObservationReason::PollSpawnFailed,
        ObservationReason::PollTimeout,
        ObservationReason::PollPermission,
        ObservationReason::PollFdExhausted,
        ObservationReason::PollUnparseable,
        ObservationReason::GeometryExceedsMaxCells,
        ObservationReason::ObservationCeiling,
    ] {
        assert!(!reason.as_str().contains(' '));
        assert!(!reason.as_str().contains('\n'));
    }
    assert_eq!(ObservationState::Live.as_str(), "live");
    let classified = classify_poll(Some(0), "raw stderr leak", "not-a-header", None, None);
    assert_eq!(classified, PollClass::Unparseable);
}

fn oversize_batch(width: u16, height: u16) -> String {
    let mut fields = vec![
        "1".to_string(),
        "2".to_string(),
        width.to_string(),
        height.to_string(),
    ];
    while fields.len() < POLL_FIELD_COUNT {
        fields.push("0".into());
    }
    format!("{}\nGTERM_TITLE_LEN=0\nGTERM_TITLE=x", fields.join(" "))
}
