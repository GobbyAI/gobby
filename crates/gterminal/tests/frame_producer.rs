//! Drive a live PTY through the frame-producer API with no ratatui::Frame.

mod host_support;

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::time::Duration;

use bytes::Bytes;
use gobby_terminal::pane::{PaneShellConfig, ShellMode};
use gobby_terminal::protocol::{
    read_message, write_message, ClientMessage, FrameData, RenderEncoding, ServerMessage,
    MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use gobby_terminal::runtime::TerminalRuntime;
use gobby_terminal::terminal_theme::TerminalTheme;
use host_support::{
    connect, hello_control, recv_json, rpc, send_json, spawn_host, wait_exit, wait_socket,
    write_token, CONTROL_SOCKET, FRAMES_SOCKET,
};
use serde_json::json;

fn frame_text(frame: &FrameData) -> String {
    frame
        .cells
        .chunks(frame.width as usize)
        .map(|row| {
            row.iter()
                .map(|cell| cell.symbol.as_str())
                .collect::<String>()
                .trim_end()
                .to_string()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn send_frame(stream: &mut UnixStream, message: &ClientMessage) {
    let mut frame = Vec::new();
    write_message(&mut frame, message).expect("encode frame");
    stream.write_all(&frame).expect("write frame");
    stream.flush().expect("flush frame");
}

fn recv_frame(stream: &mut UnixStream) -> ServerMessage {
    read_message(stream, MAX_FRAME_SIZE).expect("read frame")
}

#[test]
fn broadcast_task_exits_on_closed_channel() {
    let dir = tempfile::tempdir().expect("tempdir");
    let token = "frame-closed-channel";
    write_token(dir.path(), token);
    let mut child = spawn_host(dir.path());
    wait_socket(&dir.path().join(CONTROL_SOCKET));
    wait_socket(&dir.path().join(FRAMES_SOCKET));
    let mut control = connect(&dir.path().join(CONTROL_SOCKET));
    assert_eq!(hello_control(&mut control, token)["ok"], true);

    let reserved = rpc(
        &mut control,
        "reserve_observer",
        json!({"terminal_id": "term-close", "reserve_key": "rk-close"}),
    );
    let prepared = rpc(
        &mut control,
        "spawn",
        json!({
            "operation_seq": 1,
            "terminal_id": "term-close",
            "spawn_key": "sk-close",
            "reservation_id": reserved["reservation_id"],
            "reserve_key": "rk-close",
            "argv": ["/bin/sleep", "30"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 8000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"].as_str().unwrap().to_string();
    assert_eq!(
        rpc(
            &mut control,
            "spawn_commit",
            json!({"terminal_id": "term-close", "spawn_key": "sk-close"}),
        )["ok"],
        true
    );

    let mut frames = connect(&dir.path().join(FRAMES_SOCKET));
    send_frame(
        &mut frames,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: "local-token".into(),
            cols: 80,
            rows: 24,
            tmux_identity: None,
        },
    );
    assert!(matches!(
        recv_frame(&mut frames),
        ServerMessage::Welcome { .. }
    ));
    send_frame(
        &mut frames,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.clone(),
            reservation_id: None,
            locator: None,
        },
    );
    assert!(matches!(
        recv_frame(&mut frames),
        ServerMessage::Attached { .. }
    ));

    let killed = rpc(
        &mut control,
        "kill",
        json!({
            "operation_seq": 2,
            "host_terminal_id": host_terminal_id,
            "grace_ms": 20,
        }),
    );
    assert_eq!(killed["killed"], true, "{killed}");
    frames
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("read timeout");
    let mut buffer = [0_u8; 8192];
    loop {
        match frames.read(&mut buffer) {
            Ok(0) => break,
            Ok(_) => continue,
            Err(error) => panic!("frame task did not close its socket: {error}"),
        }
    }

    send_json(
        &mut control,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut control);
    let _ = wait_exit(&mut child, Duration::from_secs(5));
}

#[tokio::test]
async fn end_to_end_without_ratatui_frame() {
    let cwd = std::env::temp_dir();
    assert!(Path::new(&cwd).is_dir());

    let runtime = TerminalRuntime::spawn(
        24,
        80,
        cwd,
        64 * 1024,
        TerminalTheme::default(),
        None,
        PaneShellConfig::new("", ShellMode::NonLogin),
    )
    .expect("spawn interactive shell");

    runtime
        .send_bytes(Bytes::from_static(b"printf 'hello-from-gterm\\n'\n"))
        .await
        .expect("write to pty");

    let marker = "hello-from-gterm";
    let deadline = tokio::time::Instant::now() + Duration::from_secs(5);
    let mut frame = runtime.frame_data(80, 24);
    while !frame_text(&frame).contains(marker) {
        assert!(
            tokio::time::Instant::now() < deadline,
            "timed out waiting for PTY output in FrameData; got:\n{}",
            frame_text(&frame)
        );
        tokio::task::yield_now().await;
        frame = runtime.frame_data(80, 24);
    }

    let _ = runtime.dirty_patch();
    let _ = runtime.osc_title();
    let _ = runtime.osc_progress();

    runtime.resize(30, 100, 0, 0);
    let resized = runtime.frame_data(100, 30);
    assert_eq!(resized.width, 100);
    assert_eq!(resized.height, 30);
    assert_eq!(resized.cells.len(), 100 * 30);

    runtime.shutdown();
}
