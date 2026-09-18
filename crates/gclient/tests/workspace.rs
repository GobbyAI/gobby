//! 3.3.2 / 3.3.15 / 3.3.17 / 3.3.21 workspace control and attach.

mod mock_daemon;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use gobby_client::app::ViewerState;
use gobby_client::daemon::{
    LayoutAxis, LayoutNode, LiveDaemon, WorkspaceEvent, WorkspaceEventKind, WorkspaceSnapshot,
};
use gobby_client::frame_source::{
    AttachLocator, FrameError, PaneFrameSource, ScriptedFrameSource, Transport,
    UnixSocketFrameSource,
};
use gobby_client::ui::{render_workspace_with, Chrome};
use gobby_client::Workspace;
use gobby_terminal::layout::{self, TileLayout};
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneLocator, PaneModes, ServerMessage, MAX_FRAME_SIZE,
};
use ratatui::backend::TestBackend;
use ratatui::layout::{Direction, Rect};
use ratatui::Terminal;
use serde_json::json;
use std::collections::HashSet;
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

// -------------------------------------------- oversized tmux frame anchoring

/// A cell that names its own coordinate, so a capture says exactly which region
/// of the source frame was painted.
fn coordinate_symbol(x: u16, y: u16) -> String {
    let index = (usize::from(y) * 7 + usize::from(x)) % 26;
    char::from(b'a' + u8::try_from(index).expect("index fits 0..26")).to_string()
}

/// #21913: a tmux frame larger than its pane body is painted from its origin.
///
/// This drives the real composition rather than the renderer in isolation:
/// `Chrome::compute_view` supplies the pane geometry and `render_workspace_with`
/// installs the same content painter both run loops install, so a regression in
/// either the renderer or the pane rect fails here.
///
/// A terminal is anchored at its origin — column 0 begins every line and row 0
/// is the earliest output — so an oversized frame must be clipped at its far
/// edges, exactly as the native branch already does. Centering the source
/// instead cropped equally from all four edges, discarding the prompt and the
/// left of every line; a real 200x50 tmux pane rendered as an empty body.
/// Restoring that source centering moves the origin cell and fails this test.
#[tokio::test]
async fn an_oversized_tmux_frame_renders_from_its_origin() {
    const FRAME_WIDTH: u16 = 200;
    const FRAME_HEIGHT: u16 = 50;

    let cells = (0..FRAME_HEIGHT)
        .flat_map(|y| {
            (0..FRAME_WIDTH).map(move |x| CellData {
                symbol: coordinate_symbol(x, y),
                fg: 0,
                bg: 0,
                modifier: 0,
                skip: false,
                hyperlink: None,
            })
        })
        .collect();

    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-tmux-oversized", "tmux", "epoch-tmux")
        .expect("open tmux terminal");
    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(FrameData {
        cells,
        width: FRAME_WIDTH,
        height: FRAME_HEIGHT,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    }));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("replace frame source");
    ws.recv_pane_frame(pane).await.expect("receive frame");

    let area = Rect::new(0, 0, 120, 40);
    let mut chrome = Chrome::dark();
    let slot = chrome.open_pane(pane, "tmux");
    chrome.compute_view(&ws, area);
    let inner = chrome
        .view
        .pane_infos
        .iter()
        .find(|info| info.id == slot)
        .expect("pane geometry")
        .inner_rect;
    assert!(
        inner.width < FRAME_WIDTH && inner.height < FRAME_HEIGHT,
        "the frame must be wider and taller than the pane body for this to \
         exercise clipping at all, got {inner:?}"
    );

    let mut painted = None;
    let mut terminal =
        Terminal::new(TestBackend::new(area.width, area.height)).expect("test backend");
    terminal
        .draw(|frame| {
            let mut content = |frame: &mut ratatui::Frame<'_>, body: Rect, id| {
                painted = Some(body);
                gobby_client::views::grid::render(frame, body, ws.pane(id));
            };
            render_workspace_with(frame, &ws, &chrome, &mut content);
        })
        .expect("draw workspace");
    let painted = painted.expect("the workspace painted a pane body");
    assert_eq!(
        painted, inner,
        "the content painter receives the pane's inner rect"
    );

    let buffer = terminal.backend().buffer();
    let symbol_at = |x: u16, y: u16| buffer[(x, y)].symbol().to_string();

    assert_eq!(
        symbol_at(painted.x, painted.y),
        coordinate_symbol(0, 0),
        "the frame's own (0,0) must land on the pane body's origin"
    );

    // No leading column is dropped: the body's first row reads the frame's
    // first row starting at column 0.
    for column in 0..painted.width {
        assert_eq!(
            symbol_at(painted.x + column, painted.y),
            coordinate_symbol(column, 0),
            "body column {column} of the first row must be frame column {column}"
        );
    }

    // No leading row is dropped: the body's first column reads the frame's
    // first column starting at row 0.
    for row in 0..painted.height {
        assert_eq!(
            symbol_at(painted.x, painted.y + row),
            coordinate_symbol(0, row),
            "body row {row} of the first column must be frame row {row}"
        );
    }
}

#[tokio::test]
async fn frame_modes_follow_the_latest_frame() {
    use gobby_terminal::protocol::MouseTracking;

    let mut ws = Workspace::scripted();
    let pane = ws
        .open_terminal("term-modes", "native", "epoch-modes")
        .expect("open pane");
    assert!(ws.pane(pane).latest_frame().is_none());
    let ServerMessage::Frame(mut tracking) = semantic_frame("M") else {
        panic!("expected semantic frame");
    };
    tracking.modes = PaneModes {
        mouse_all: true,
        mouse_sgr: true,
        alternate_on: true,
        ..Default::default()
    };
    let mut without_modes = serde_json::to_value(&tracking).expect("serialize frame");
    without_modes
        .as_object_mut()
        .expect("frame object")
        .remove("modes");
    let without_modes: FrameData =
        serde_json::from_value(without_modes).expect("decode missing modes");
    assert_eq!(without_modes.modes.mouse_tracking(), MouseTracking::Off);

    let mut source = ScriptedFrameSource::new(Transport::Direct);
    source.queue(ServerMessage::Frame(tracking.clone()));
    source.queue(ServerMessage::Frame(without_modes.clone()));
    ws.replace_frame_source(pane, PaneFrameSource::Scripted(source))
        .expect("install source");
    ws.recv_pane_frame(pane).await.expect("tracking frame");
    let latest = ws.pane(pane).latest_frame().expect("latest tracking frame");
    assert_eq!(latest, &tracking);
    assert_eq!(latest.modes.mouse_tracking(), MouseTracking::AnyMotion);
    assert!(latest.modes.mouse_sgr && latest.modes.alternate_on);

    ws.recv_pane_frame(pane).await.expect("frame without modes");
    let latest = ws
        .pane(pane)
        .latest_frame()
        .expect("latest frame without modes");
    assert_eq!(latest, &without_modes);
    assert_eq!(latest.modes.mouse_tracking(), MouseTracking::Off);
}

// ---- plan gclient-workspaces 4.2.2 / 4.2.3: viewer-local state over a daemon layout ----

const WS_PROJECT: &str = "33333333-3333-4333-8333-333333333333";
const WS_TAB_FIRST: &str = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
const WS_TAB_SECOND: &str = "tab-2222-2222-4222-8222-222222222222";
const WS_TERMINAL_A: &str = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const WS_TERMINAL_B: &str = "22222222-2222-4222-8222-222222222222";

/// The golden snapshot plus a second tab whose one pane names a terminal the
/// roster has not delivered.
fn two_tab_snapshot() -> WorkspaceSnapshot {
    let mut snapshot: WorkspaceSnapshot = serde_json::from_str(include_str!(
        "../../../tests/fixtures/terminal_ws_golden/workspace_snapshot.json"
    ))
    .expect("workspace snapshot fixture");
    let mut second = snapshot.tabs[0].clone();
    second.id = WS_TAB_SECOND.into();
    second.reference = 2;
    second.position = 1;
    second.title = Some("second".into());
    second.focused_pane_id = Some("pane-3".into());
    second.layout = LayoutNode::Pane {
        pane_id: "pane-3".into(),
    };
    snapshot.tabs.push(second);
    let mut pane = snapshot.panes[0].clone();
    pane.id = "pane-3".into();
    pane.tab_id = WS_TAB_SECOND.into();
    pane.reference = 1;
    pane.terminal_id = Some("33333333-3333-4333-8333-333333333333".into());
    snapshot.panes.push(pane);
    snapshot
}

/// A `pane.added` on the first tab: a third pane, split under the second, on
/// a terminal the roster has not delivered.
fn pane_added_event(snapshot: &WorkspaceSnapshot) -> WorkspaceEvent {
    let mut tab = snapshot.tabs[0].clone();
    let LayoutNode::Split {
        axis,
        ratio,
        children,
    } = tab.layout.clone()
    else {
        panic!("the fixture tab is a split");
    };
    let [first, second] = *children;
    tab.layout = LayoutNode::Split {
        axis,
        ratio,
        children: Box::new([
            first,
            LayoutNode::Split {
                axis: LayoutAxis::Vertical,
                ratio: 0.5,
                children: Box::new([
                    second,
                    LayoutNode::Pane {
                        pane_id: "pane-4".into(),
                    },
                ]),
            },
        ]),
    };
    let mut pane = snapshot.panes[0].clone();
    pane.id = "pane-4".into();
    pane.reference = 3;
    pane.terminal_id = Some("44444444-4444-4444-8444-444444444444".into());
    WorkspaceEvent {
        kind: WorkspaceEventKind::PaneAdded,
        workspace_id: snapshot.workspace.id.clone(),
        project_id: Some(WS_PROJECT.into()),
        workspace: None,
        tabs: vec![tab],
        panes: vec![pane],
        daemon_epoch: snapshot.snapshot.daemon_epoch.clone(),
        seq: snapshot.snapshot.seq + 1,
        timestamp: "2026-01-01T00:00:01+00:00".into(),
    }
}

fn tab_ids(chrome: &Chrome) -> Vec<String> {
    chrome
        .tabs()
        .tabs
        .iter()
        .map(|tab| tab.id.clone())
        .collect()
}

/// The daemon pane ids behind the active tab's slots, in layout order.
fn daemon_panes(chrome: &Chrome) -> Vec<String> {
    let tab = chrome.active_tab().expect("active tab");
    tab.layout
        .pane_ids()
        .into_iter()
        .map(|slot| {
            chrome
                .viewer
                .panes
                .daemon_id(slot)
                .expect("interned slot")
                .to_string()
        })
        .collect()
}

#[test]
fn two_viewers_share_layout_and_keep_their_own_focus() {
    let mut ws = Workspace::scripted();
    let a = ws
        .open_terminal(WS_TERMINAL_A, "native", "epoch-a")
        .expect("open a");
    let b = ws
        .open_terminal(WS_TERMINAL_B, "native", "epoch-b")
        .expect("open b");
    let snapshot = two_tab_snapshot();
    let event = pane_added_event(&snapshot);
    ws.apply_workspace_snapshot(snapshot);

    let mut left = Chrome::dark();
    let mut right = Chrome::dark();
    for chrome in [&mut left, &mut right] {
        chrome.focus_project(WS_PROJECT);
        let unresolved = chrome.project_workspace(&ws, WS_PROJECT);
        assert_eq!(
            unresolved,
            vec!["33333333-3333-4333-8333-333333333333".to_string()],
            "the second tab's terminal is not on the roster yet"
        );
    }

    // Both windows show the daemon's tabs and panes, and seed focus from the row hints.
    assert_eq!(tab_ids(&left), vec![WS_TAB_FIRST, WS_TAB_SECOND]);
    assert_eq!(tab_ids(&right), tab_ids(&left));
    assert_eq!(daemon_panes(&left), daemon_panes(&right));
    assert_eq!(
        left.active_tab().map(|tab| tab.id.as_str()),
        Some(WS_TAB_FIRST)
    );
    assert_eq!(
        left.focused_pane(),
        Some(b),
        "focused_pane_id names terminal b"
    );
    assert_eq!(right.focused_pane(), Some(b));
    let second = &left.tabs().tabs[1];
    assert_eq!(second.layout.pane_count(), 1);
    assert!(second.slots.is_empty(), "an unresolved slot renders empty");

    // Focus, zoom, and the active tab belong to each window.
    assert!(left.focus_pane(a));
    left.toggle_zoom();
    assert!(right.activate_tab(1));
    assert_eq!(left.focused_pane(), Some(a));
    assert_eq!(
        right.focused_pane(),
        None,
        "the second tab's only slot has no terminal yet"
    );
    assert!(left.is_zoomed());
    assert!(!right.is_zoomed());
    assert_eq!(
        left.active_tab().map(|tab| tab.id.as_str()),
        Some(WS_TAB_FIRST)
    );
    assert_eq!(
        right.active_tab().map(|tab| tab.id.as_str()),
        Some(WS_TAB_SECOND)
    );

    // A daemon event re-projects both windows; the layout is shared, the viewer state kept.
    assert!(ws.apply_workspace_event(&event));
    for chrome in [&mut left, &mut right] {
        chrome.project_workspace(&ws, WS_PROJECT);
        assert_eq!(chrome.tabs().tabs[0].layout.pane_count(), 3);
    }
    assert_eq!(left.focused_pane(), Some(a));
    assert!(left.is_zoomed());
    assert_eq!(
        right.active_tab().map(|tab| tab.id.as_str()),
        Some(WS_TAB_SECOND)
    );
    right.activate_tab(0);
    assert_eq!(right.focused_pane(), Some(b));
    assert_eq!(daemon_panes(&left), daemon_panes(&right));
    assert_eq!(
        daemon_panes(&left).last().map(String::as_str),
        Some("pane-4")
    );
}

#[test]
fn pane_ids_are_interned_without_collision() {
    let (mut layout, root) = TileLayout::new();
    let mut left = ViewerState::default();
    let mut right = ViewerState::default();

    let left_one = left.panes.intern("pane-1");
    let right_one = right.panes.intern("pane-1");
    assert_eq!(
        left.panes.intern("pane-1"),
        left_one,
        "interning is stable per window"
    );
    assert_eq!(left.panes.slot("pane-1"), Some(left_one));
    assert_eq!(left.panes.daemon_id(left_one), Some("pane-1"));
    assert_eq!(left.panes.slot("pane-9"), None);

    let split = layout.split_focused(root, Direction::Horizontal);
    let left_two = left.panes.intern("pane-2");
    let minted: HashSet<layout::PaneId> = [root, split, left_one, right_one, left_two]
        .into_iter()
        .collect();
    assert_eq!(
        minted.len(),
        5,
        "interned ids never collide with layout-allocated ones"
    );
}
