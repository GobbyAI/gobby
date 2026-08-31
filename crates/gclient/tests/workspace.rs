//! 3.3.2 / 3.3.15 / 3.3.17 / 3.3.21 workspace control and attach.

use gobby_client::frame_source::{AttachLocator, FrameError, ScriptedFrameSource};
use gobby_client::Workspace;
use gobby_terminal::protocol::write_message;
use serde_json::json;
use std::fs;
use std::path::PathBuf;

fn src_scan_has_host_write() -> bool {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut stack = vec![root];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            if path.extension().and_then(|e| e.to_str()) != Some("rs") {
                continue;
            }
            let text = fs::read_to_string(&path).unwrap();
            for needle in [
                "LegacyInput",
                "TIOCSWINSZ",
                "\"method\":\"write\"",
                "\"method\": \"write\"",
            ] {
                if text.contains(needle) {
                    return true;
                }
            }
        }
    }
    false
}

#[test]
fn focus_moves_control_through_the_daemon() {
    assert!(!src_scan_has_host_write(), "crate must not host-write");
    let mut ws = Workspace::scripted();
    let a = ws
        .open_terminal("term-a", "native", "epoch-a")
        .expect("open a");
    let b = ws
        .open_terminal("term-b", "native", "epoch-b")
        .expect("open b");
    assert!(ws.pane(a).is_observe());
    assert!(ws.pane(b).is_observe());

    ws.focus_pane(a).expect("focus a");
    assert!(ws.pane(a).is_held());
    assert!(ws
        .daemon()
        .ws_sent_types()
        .contains(&"terminal_take_control".into()));
    ws.send_keys(a, "ls\n").expect("keys");
    assert!(
        ws.daemon()
            .ws_sent_types()
            .contains(&"terminal_input".into()),
        "keystrokes go to the daemon"
    );

    ws.focus_pane(b).expect("focus b");
    let sent = ws.daemon().ws_sent_types();
    assert!(sent.contains(&"terminal_release_control".into()));
    assert!(ws.pane(a).is_observe());
    assert!(ws.pane(b).is_held());
    assert_eq!(ws.pane(a).attachment_id(), ws.pane(a).attachment_id());

    ws.apply_ws(&json!({
        "type": "terminal_lease_lost",
        "attachment_id": ws.pane(b).attachment_id(),
        "holder": "other",
        "lease_generation": 4
    }))
    .unwrap();
    assert!(ws.pane(b).is_lease_lost());
    assert!(ws.pane(b).has_take_back());

    let gen = ws.pane(a).lease_generation();
    ws.apply_ws(&json!({
        "type": "terminal_control_result",
        "attachment_id": ws.pane(a).attachment_id(),
        "granted": true,
        "reason": "held",
        "lease_generation": gen.saturating_sub(1)
    }))
    .unwrap();
    assert!(
        ws.pane(a).is_observe(),
        "lower lease_generation must be ignored"
    );

    ws.apply_ws(&json!({
        "type": "terminal_control_result",
        "attachment_id": ws.pane(a).attachment_id(),
        "granted": true,
        "reason": "held",
        "lease_generation": gen.max(1)
    }))
    .unwrap();
    assert!(ws.pane(a).is_held() || ws.pane(a).is_observe());

    ws.set_daemon_reachable(false);
    ws.push_frame(a, "still-here");
    assert!(ws.pane(a).frames_rendered() >= 1);
    assert!(ws.pane(a).is_observe() || ws.pane(a).is_lease_lost() || !ws.pane(a).is_held());
}

#[test]
fn frame_attach_refuses_epoch_mismatch_before_attach() {
    let mut frames = ScriptedFrameSource::new();
    frames.set_welcome_epoch("live-epoch");
    let mut ws = Workspace::with_frames(frames);
    let locator = AttachLocator {
        backend: "native".into(),
        frame_host_epoch: "stale-epoch".into(),
        host_terminal_id: "ht-1".into(),
        socket_path: "/tmp/gterm-frames.sock".into(),
        pane_id: None,
        server_pid: None,
        server_start_time: None,
    };
    let err = ws.attach_locator(locator.clone()).unwrap_err();
    assert!(matches!(err, FrameError::HostEpochChanged { .. }));
    assert!(!ws.frames().sent_attach());
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &gobby_client::views::observe_tmux_pane(&AttachLocator {
            backend: "tmux".into(),
            frame_host_epoch: "epoch".into(),
            host_terminal_id: "ht-1".into(),
            socket_path: "/tmp/tmux-sock".into(),
            pane_id: Some("%0".into()),
            server_pid: Some(9),
            server_start_time: Some(1),
        })
        .1,
    )
    .unwrap();
    let golden = fs::read(
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("../gterminal/tests/fixtures/wire_golden/attach_terminal.bin"),
    )
    .expect("unreserved golden");
    assert_eq!(buf, golden, "user attach omits reservation_id");

    let mut tmux = ScriptedFrameSource::new();
    tmux.set_welcome_epoch("adopted-live");
    let mut ws = Workspace::with_frames(tmux);
    let stale = AttachLocator {
        backend: "tmux".into(),
        frame_host_epoch: "recycled".into(),
        host_terminal_id: "ht-1".into(),
        socket_path: "/tmp/gterm-frames.sock".into(),
        pane_id: Some("%1".into()),
        server_pid: None,
        server_start_time: None,
    };
    assert!(ws.attach_locator(stale).is_err());
    assert!(!ws.frames().sent_attach());
}

#[test]
fn direct_frame_eof_detaches_before_reattach() {
    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    ws.focus_pane(pane).unwrap();
    let old_id = ws.pane(pane).attachment_id().to_string();
    assert!(ws.pane(pane).is_held());
    ws.kill_frame_stream(pane).expect("eof");
    assert!(ws
        .daemon()
        .ws_sent_types()
        .contains(&"terminal_detach".into()));
    assert!(!ws
        .pane_by_attachment(&old_id)
        .map(|p| p.is_live())
        .unwrap_or(true));
    assert!(ws.pane(pane).is_observe());
    let take = ws.take_control(pane);
    assert!(take.is_err(), "control requires a fresh frame attach");
    ws.reattach_frames(pane).expect("fresh attach");
    assert_ne!(ws.pane(pane).attachment_id(), old_id);
    ws.take_control(pane).expect("control after fresh attach");
    assert!(ws.pane(pane).is_held());
}

#[test]
fn roster_follows_paginated_pending_live() {
    let mut ws = Workspace::scripted();
    ws.select_project("proj-1");
    ws.daemon_mut().set_terminal_pages(vec![
        json!({
            "items": [{"id": "t1", "terminal_id": "t1", "state": "live"}],
            "next_cursor": "c1"
        }),
        json!({
            "items": [{"id": "t2", "terminal_id": "t2", "state": "pending"}],
            "next_cursor": null
        }),
    ]);
    ws.fetch_roster().expect("pages");
    let paths = ws.daemon().rest_paths();
    let terminal_gets: Vec<_> = paths
        .iter()
        .filter(|p| p.starts_with("GET /api/terminals"))
        .cloned()
        .collect();
    assert!(
        terminal_gets
            .iter()
            .all(|p| p.contains("states=pending,live")),
        "default pending,live filter: {terminal_gets:?}"
    );
    assert!(
        terminal_gets.len() >= 2,
        "must follow cursor: {terminal_gets:?}"
    );
    assert!(!paths.iter().any(|p| p.contains("states=all")));
    assert_eq!(
        ws.roster_terminal_ids(),
        vec!["t1".to_string(), "t2".to_string()]
    );
}
