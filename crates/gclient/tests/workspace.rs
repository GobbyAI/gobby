//! 3.3.2 / 3.3.15 / 3.3.17 / 3.3.21 workspace control and attach.

mod mock_daemon;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use gobby_client::daemon::LiveDaemon;
use gobby_client::frame_source::{
    AttachLocator, FrameError, PaneFrameSource, ScriptedFrameSource, Transport,
    UnixSocketFrameSource,
};
use gobby_client::Workspace;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneLocator, PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use std::fs;
use std::path::PathBuf;
use tokio::net::UnixStream;
use tokio::time::{timeout, Duration};

const IO_TIMEOUT: Duration = Duration::from_secs(3);

fn semantic_frame(symbol: &str) -> ServerMessage {
    ServerMessage::Frame(FrameData {
        cells: vec![CellData {
            symbol: symbol.into(),
            fg: 1,
            bg: 0,
            modifier: 0,
            skip: false,
            hyperlink: None,
        }],
        width: 1,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    })
}

fn proxy_event(terminal_id: &str, attachment_id: &str, frame: &ServerMessage) -> serde_json::Value {
    let mut framed = Vec::new();
    write_message(&mut framed, frame).expect("encode proxy frame");
    json!({
        "type": "terminal_frame",
        "terminal_id": terminal_id,
        "attachment_id": attachment_id,
        "encoding": "bincode-b64",
        "payload": STANDARD.encode(&framed[4..]),
    })
}

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
    assert_ne!(ws.pane(a).attachment_id(), ws.pane(b).attachment_id());

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
    let mut frames = ScriptedFrameSource::new(Transport::Direct);
    frames.set_welcome_epoch("live-epoch");
    let locator = AttachLocator {
        backend: "native".into(),
        frame_host_epoch: "stale-epoch".into(),
        host_terminal_id: "ht-1".into(),
        frame_socket_path: "/tmp/gterm-frames.sock".into(),
        pane: None,
    };
    let err = frames.connect(&locator, 80, 24).unwrap_err();
    assert!(matches!(err, FrameError::HostEpochChanged { .. }));
    assert!(!frames.sent_attach());
    let mut buf = Vec::new();
    write_message(
        &mut buf,
        &gobby_client::views::observe_tmux_pane(&AttachLocator {
            backend: "tmux".into(),
            frame_host_epoch: "epoch".into(),
            host_terminal_id: "ht-1".into(),
            frame_socket_path: "/tmp/gterm-frames.sock".into(),
            pane: Some(PaneLocator {
                socket_path: "/tmp/tmux-sock".into(),
                pane_id: "%0".into(),
                server_pid: 9,
                server_start_time: 1,
            }),
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

    let mut tmux = ScriptedFrameSource::new(Transport::Direct);
    tmux.set_welcome_epoch("adopted-live");
    let stale = AttachLocator {
        backend: "tmux".into(),
        frame_host_epoch: "recycled".into(),
        host_terminal_id: "ht-1".into(),
        frame_socket_path: "/tmp/gterm-frames.sock".into(),
        pane: Some(PaneLocator {
            socket_path: "/tmp/tmux-sock".into(),
            pane_id: "%1".into(),
            server_pid: 9,
            server_start_time: 1,
        }),
    };
    assert!(tmux.connect(&stale, 80, 24).is_err());
    assert!(!tmux.sent_attach());
}

#[tokio::test]
async fn direct_frame_eof_detaches_before_reattach() {
    let mut ws = Workspace::scripted();
    let pane = ws.open_terminal("term-a", "native", "epoch-a").unwrap();
    let (client, mut host) = UnixStream::pair().expect("direct socket pair");
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "epoch-a".into(),
            },
        )
        .await
        .expect("welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("attach");
    });
    let source = UnixSocketFrameSource::connect_stream(
        client,
        &AttachLocator {
            backend: "native".into(),
            frame_host_epoch: "epoch-a".into(),
            host_terminal_id: "term-a".into(),
            frame_socket_path: "socket-pair".into(),
            pane: None,
        },
        "local-token",
        80,
        24,
    )
    .await
    .expect("real direct source");
    ws.replace_frame_source(pane, PaneFrameSource::Direct(source))
        .expect("install direct source");
    ws.focus_pane(pane).unwrap();
    let old_id = ws.pane(pane).attachment_id().to_string();
    assert!(ws.pane(pane).is_held());
    host_task.await.expect("fake host");
    assert!(matches!(
        ws.recv_pane_frame(pane).await,
        Err(FrameError::Eof)
    ));
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
    assert_eq!(ws.pane(pane).transport(), Some(Transport::Proxy));
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

#[tokio::test]
async fn direct_and_proxy_panes_run_together() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [
                {"terminal_id": "terminal-direct", "backend": "native", "state": "live"},
                {"terminal_id": "terminal-proxy", "backend": "native", "state": "live"}
            ],
            "next_cursor": null,
            "snapshot": {"daemon_epoch": "epoch-1", "seq": 0}
        }),
    );
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect daemon");
    let mut workspace = Workspace::live(daemon);
    workspace.select_project("project-1");
    workspace
        .reconcile_subscribe_first()
        .await
        .expect("reconcile workspace");

    let direct_id = workspace
        .pane_for_terminal("terminal-direct")
        .expect("direct pane");
    let proxy_id = workspace
        .pane_for_terminal("terminal-proxy")
        .expect("proxy pane");
    let proxy_attachment = workspace.pane(proxy_id).attachment_id().to_string();

    let (client, mut host) = UnixStream::pair().expect("direct socket pair");
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("direct hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "direct-epoch".into(),
            },
        )
        .await
        .expect("direct welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("direct attach");
        write_message_async(&mut host, &semantic_frame("D"))
            .await
            .expect("direct frame");
    });
    let direct = UnixSocketFrameSource::connect_stream(
        client,
        &AttachLocator {
            backend: "native".into(),
            frame_host_epoch: "direct-epoch".into(),
            host_terminal_id: "terminal-direct".into(),
            frame_socket_path: "socket-pair".into(),
            pane: None,
        },
        "local-token",
        80,
        24,
    )
    .await
    .expect("direct source");
    workspace
        .replace_frame_source(direct_id, PaneFrameSource::Direct(direct))
        .expect("install direct source");

    let proxy_frame = semantic_frame("P");
    mock.send_event_and_wait(proxy_event(
        "terminal-proxy",
        &proxy_attachment,
        &proxy_frame,
    ))
    .await;
    assert_eq!(
        workspace.pane(direct_id).transport(),
        Some(Transport::Direct)
    );
    assert_eq!(workspace.pane(proxy_id).transport(), Some(Transport::Proxy));
    assert_eq!(
        timeout(IO_TIMEOUT, workspace.recv_live_frame(direct_id))
            .await
            .expect("direct receive timeout")
            .expect("direct frame"),
        semantic_frame("D")
    );
    assert_eq!(
        timeout(IO_TIMEOUT, workspace.recv_live_frame(proxy_id))
            .await
            .expect("proxy receive timeout")
            .expect("proxy frame"),
        proxy_frame
    );

    host_task.await.expect("direct host task");
    let attach_count_before = mock
        .requests()
        .iter()
        .filter(|request| {
            request.body.as_ref().and_then(|body| body.get("type"))
                == Some(&json!("terminal_attach"))
        })
        .count();
    assert!(matches!(
        workspace.recv_live_frame(direct_id).await,
        Err(FrameError::Eof)
    ));
    let attach_requests: Vec<_> = mock
        .requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!("terminal_attach")))
        .collect();
    assert_eq!(attach_requests.len(), attach_count_before + 1);
    assert_eq!(
        attach_requests
            .last()
            .and_then(|body| body.get("frame_delivery")),
        Some(&json!("proxy"))
    );
    assert_eq!(
        attach_requests.last().and_then(|body| body.get("encoding")),
        Some(&json!("semantic_frame"))
    );
    assert_eq!(
        workspace.pane(direct_id).transport(),
        Some(Transport::Proxy)
    );
    assert_eq!(workspace.pane(proxy_id).attachment_id(), proxy_attachment);
    assert_eq!(workspace.pane(proxy_id).transport(), Some(Transport::Proxy));
    mock.shutdown().await;
}
