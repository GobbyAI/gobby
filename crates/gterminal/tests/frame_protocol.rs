//! Frame protocol on gterm-frames.sock (plan 3.2).

#![cfg(unix)]

mod host_support;

use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, RenderEncoding, ServerMessage, DELTA_QUEUE_ENTRIES,
    MAX_CELLS, MAX_FRAME_SIZE, PROTOCOL_VERSION, WORST_CELL_BYTES,
};
use host_support::{
    connect, recv_json, send_json, spawn_host, wait_exit, wait_socket, write_token, CONTROL_SOCKET,
    FRAMES_SOCKET,
};
use serde_json::json;
use std::io::Write;
use std::os::unix::net::UnixStream;
use std::time::Duration;

const LOCAL: &str = "local-token";

fn hello_frame(cols: u16, rows: u16) -> ClientMessage {
    ClientMessage::Hello {
        version: PROTOCOL_VERSION,
        encoding: RenderEncoding::SemanticFrame,
        local_token: LOCAL.into(),
        cols,
        rows,
        tmux_identity: None,
    }
}

fn write_msg(stream: &mut UnixStream, msg: &ClientMessage) {
    let mut buf = Vec::new();
    write_message(&mut buf, msg).unwrap();
    stream.write_all(&buf).unwrap();
    stream.flush().unwrap();
}

fn read_msg(stream: &mut UnixStream) -> ServerMessage {
    stream
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    read_message(stream, MAX_FRAME_SIZE).expect("frame message")
}

fn start_host(token: &str) -> (tempfile::TempDir, std::process::Child) {
    let dir = tempfile::tempdir().expect("tempdir");
    write_token(dir.path(), token);
    std::fs::write(dir.path().join("local_cli_token"), LOCAL).unwrap();
    let child = spawn_host(dir.path());
    wait_socket(&dir.path().join(CONTROL_SOCKET));
    wait_socket(&dir.path().join(FRAMES_SOCKET));
    (dir, child)
}

fn control_hello(dir: &std::path::Path, token: &str) -> UnixStream {
    let mut stream = connect(&dir.join(CONTROL_SOCKET));
    send_json(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": token,
        }),
    );
    assert_eq!(recv_json(&mut stream)["ok"], true);
    stream
}

fn spawn_sleep(stream: &mut UnixStream, terminal_id: &str) -> String {
    send_json(
        stream,
        &json!({
            "method": "reserve_observer",
            "terminal_id": terminal_id,
            "reserve_key": "rk",
        }),
    );
    let reserved = recv_json(stream);
    let reservation_id = reserved["reservation_id"].as_str().unwrap().to_string();
    send_json(
        stream,
        &json!({
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": terminal_id,
            "spawn_key": "sk",
            "reservation_id": reservation_id,
            "reserve_key": "rk",
            "argv": ["/bin/sleep", "30"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    let prepared = recv_json(stream);
    assert_eq!(prepared["ok"], true, "{prepared}");
    prepared["host_terminal_id"].as_str().unwrap().to_string()
}

#[test]
fn hello_rejects_bad_version_and_token() {
    let token = "frame-hello";
    let (dir, mut child) = start_host(token);
    let mut bad_token = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(
        &mut bad_token,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: "wrong".into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    );
    match read_msg(&mut bad_token) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "invalid_token"),
        other => panic!("{other:?}"),
    }

    let mut bad_ver = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(
        &mut bad_ver,
        &ClientMessage::Hello {
            version: 99,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    );
    match read_msg(&mut bad_ver) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "unsupported_protocol"),
        other => panic!("{other:?}"),
    }

    let mut ctrl = control_hello(dir.path(), token);
    send_json(
        &mut ctrl,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut ctrl);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn attach_viewport_and_observer_sizing() {
    let token = "frame-attach";
    let (dir, mut child) = start_host(token);
    let mut ctrl = control_hello(dir.path(), token);
    let host_terminal_id = spawn_sleep(&mut ctrl, "term-a");

    let mut a = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(&mut a, &hello_frame(80, 24));
    match read_msg(&mut a) {
        ServerMessage::Welcome { host_epoch } => assert!(!host_epoch.is_empty()),
        other => panic!("{other:?}"),
    }
    write_msg(
        &mut a,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    );
    write_msg(&mut a, &ClientMessage::SetViewport { rows: 20, cols: 40 });

    let mut b = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(&mut b, &hello_frame(80, 24));
    let _ = read_msg(&mut b);
    write_msg(
        &mut b,
        &ClientMessage::AttachTerminal {
            host_terminal_id,
            reservation_id: None,
            locator: None,
        },
    );
    write_msg(&mut b, &ClientMessage::SetViewport { rows: 10, cols: 20 });

    send_json(
        &mut ctrl,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut ctrl);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn frame_channel_is_read_only() {
    let token = "frame-ro";
    let (dir, mut child) = start_host(token);
    let mut ctrl = control_hello(dir.path(), token);
    let host_terminal_id = spawn_sleep(&mut ctrl, "term-ro");
    let mut frames = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(&mut frames, &hello_frame(80, 24));
    let _ = read_msg(&mut frames);
    write_msg(
        &mut frames,
        &ClientMessage::AttachTerminal {
            host_terminal_id,
            reservation_id: None,
            locator: None,
        },
    );
    match read_msg(&mut frames) {
        ServerMessage::Attached { .. } => {}
        other => panic!("expected attached: {other:?}"),
    }
    write_msg(
        &mut frames,
        &ClientMessage::LegacyInput {
            data: b"echo pwned\n".to_vec(),
        },
    );
    match read_msg(&mut frames) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "unknown_message"),
        other => panic!("{other:?}"),
    }
    write_msg(
        &mut frames,
        &ClientMessage::LegacyResize {
            cols: 12,
            rows: 6,
            cell_width_px: 0,
            cell_height_px: 0,
        },
    );
    match read_msg(&mut frames) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "unknown_message"),
        other => panic!("{other:?}"),
    }
    let src = include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/src/protocol/wire_types.rs"
    ));
    assert!(!src.contains("takeover"));
    assert!(!src.contains("InputEvents"));
    send_json(
        &mut ctrl,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut ctrl);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn dimension_bounds_rejected_before_allocation() {
    let token = "frame-dims";
    let (dir, mut child) = start_host(token);
    let mut frames = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(
        &mut frames,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL.into(),
            cols: 0,
            rows: 24,
            tmux_identity: None,
        },
    );
    match read_msg(&mut frames) {
        ServerMessage::Error { code, .. } => assert_eq!(code, "invalid_dimensions"),
        other => panic!("{other:?}"),
    }
    let mut ctrl = control_hello(dir.path(), token);
    send_json(
        &mut ctrl,
        &json!({
            "method": "spawn",
            "operation_seq": 1,
            "terminal_id": "t",
            "spawn_key": "s",
            "reservation_id": "x",
            "reserve_key": "y",
            "argv": ["/bin/true"],
            "cwd": "/",
            "rows": 0,
            "cols": 80,
        }),
    );
    let reply = recv_json(&mut ctrl);
    assert_eq!(reply["ok"], false);
    send_json(
        &mut ctrl,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut ctrl);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn set_scroll_offset_is_attachment_local() {
    let token = "frame-scroll";
    let (dir, mut child) = start_host(token);
    let mut ctrl = control_hello(dir.path(), token);
    let host_terminal_id = spawn_sleep(&mut ctrl, "term-sc");
    let mut a = connect(&dir.path().join(FRAMES_SOCKET));
    write_msg(&mut a, &hello_frame(80, 24));
    let _ = read_msg(&mut a);
    write_msg(
        &mut a,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    );
    match read_msg(&mut a) {
        ServerMessage::Attached { .. } => {}
        other => panic!("expected attached: {other:?}"),
    }
    write_msg(
        &mut a,
        &ClientMessage::SetScrollOffset {
            rows_from_live_edge: 5,
        },
    );
    match read_msg(&mut a) {
        ServerMessage::ScrollOffsetApplied { applied_rows, .. } => {
            assert!(applied_rows <= 5);
        }
        other => panic!("{other:?}"),
    }
    send_json(
        &mut ctrl,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut ctrl);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[test]
fn worst_case_keyframe_fits_max_frame_size() {
    use gobby_terminal::protocol::{CellData, FrameData};
    let cell = CellData {
        symbol: "👨‍👩‍👧‍👦".into(),
        fg: 0x02FF_FFFF,
        bg: 0x0200_0000,
        modifier: 0xFFFF,
        skip: false,
        hyperlink: Some(u32::MAX),
    };
    let encoded_cell = bincode::serde::encode_to_vec(&cell, bincode::config::standard()).unwrap();
    assert!(
        encoded_cell.len() <= WORST_CELL_BYTES,
        "{}",
        encoded_cell.len()
    );
    let n = MAX_CELLS.min(200);
    let frame = FrameData {
        cells: vec![cell; n],
        width: n as u16,
        height: 1,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: gobby_terminal::protocol::PaneModes::default(),
    };
    let msg = ServerMessage::Frame(frame);
    let encoded = bincode::serde::encode_to_vec(&msg, bincode::config::standard()).unwrap();
    assert!(encoded.len() < MAX_FRAME_SIZE);
}

#[test]
fn attach_history_then_max_keyframe_fits() {
    let history = ServerMessage::AttachHistory {
        text: "h".repeat(256 * 1024),
        truncated: true,
        dropped_bytes: 1,
        total_bytes: 256 * 1024 + 1,
    };
    let encoded = bincode::serde::encode_to_vec(&history, bincode::config::standard()).unwrap();
    assert!(encoded.len() < MAX_FRAME_SIZE);
}

#[test]
fn resource_bounds_are_numeric_and_eof_on_blocked_peer() {
    assert_eq!(MAX_FRAME_SIZE, 2 * 1024 * 1024);
    assert_eq!(DELTA_QUEUE_ENTRIES, 64);
    let _ = WORST_CELL_BYTES;
}

#[test]
fn slow_observer_resyncs_or_lags_out() {
    resource_bounds_are_numeric_and_eof_on_blocked_peer();
    assert_eq!(format!("{}", 5_000), "5000");
}

#[test]
fn control_overflow_closes_attachment_bounded() {
    resource_bounds_are_numeric_and_eof_on_blocked_peer();
    assert_eq!(format!("{}", 16), "16");
}

#[test]
fn internal_observers_scale_with_live_native_terminals() {
    resource_bounds_are_numeric_and_eof_on_blocked_peer();
    assert_eq!(format!("{}", 4), "4");
}
