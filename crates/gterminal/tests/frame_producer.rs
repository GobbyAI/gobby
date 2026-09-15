//! Drive a live PTY through the frame-producer API with no ratatui::Frame.

mod host_support;

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use bytes::Bytes;
use gobby_terminal::pane::{PaneShellConfig, ShellMode};
use gobby_terminal::protocol::{
    read_message, write_message, CellData, ClientMessage, FrameData, FramingError, PaneModes,
    RenderEncoding, ServerMessage, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use gobby_terminal::runtime::TerminalRuntime;
use gobby_terminal::terminal_theme::TerminalTheme;
use host_support::{
    connect, hello_control, recv_json, rpc, send_json, spawn_host, spawn_host_with_args, wait_exit,
    wait_socket, write_token, CONTROL_SOCKET, FRAMES_SOCKET,
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

    frames
        .set_read_timeout(Some(Duration::from_secs(2)))
        .expect("read timeout");
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

fn is_timeout(err: &FramingError) -> bool {
    match err {
        FramingError::Io(error) => matches!(
            error.kind(),
            ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
        ),
        _ => false,
    }
}

fn is_keyframe(message: &ServerMessage) -> bool {
    match message {
        ServerMessage::Terminal(frame) => frame.full,
        ServerMessage::Frame(_) => true,
        _ => false,
    }
}

fn encoded_bytes(message: &ServerMessage) -> usize {
    let mut buf = Vec::new();
    write_message(&mut buf, message)
        .map(|()| buf.len())
        .unwrap_or(0)
}

fn one_keyframe_cap(rows: u16, cols: u16) -> usize {
    let cell = CellData {
        symbol: "0".into(),
        fg: 0,
        bg: 0,
        modifier: 0,
        skip: false,
        hyperlink: None,
    };
    encoded_bytes(&ServerMessage::Frame(FrameData {
        cells: vec![cell; rows as usize * cols as usize],
        width: cols,
        height: rows,
        cursor: None,
        hyperlinks: Vec::new(),
        graphics: Vec::new(),
        modes: PaneModes::default(),
    }))
}

fn drain_frames(stream: &mut UnixStream, idle: Duration) -> Vec<ServerMessage> {
    if stream.set_read_timeout(Some(idle)).is_err() {
        return Vec::new();
    }
    let max = Instant::now() + Duration::from_millis(100);
    let mut frames = Vec::new();
    while Instant::now() < max {
        match read_message(stream, MAX_FRAME_SIZE) {
            Ok(message) => frames.push(message),
            Err(err) if is_timeout(&err) => break,
            Err(_) => break,
        }
    }
    frames
}

fn attach_observer(
    frames_path: &Path,
    host_terminal_id: &str,
    encoding: RenderEncoding,
    rows: u16,
    cols: u16,
) -> UnixStream {
    let mut stream = connect(frames_path);
    send_frame(
        &mut stream,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding,
            local_token: "local-token".into(),
            cols,
            rows,
            tmux_identity: None,
        },
    );
    assert!(matches!(
        recv_frame(&mut stream),
        ServerMessage::Welcome { .. }
    ));
    send_frame(
        &mut stream,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.to_string(),
            reservation_id: None,
            locator: None,
        },
    );
    match recv_frame(&mut stream) {
        ServerMessage::Attached { .. } => stream,
        other => panic!("expected Attached, got {other:?}"),
    }
}

fn try_attach_observer(
    frames_path: &Path,
    host_terminal_id: &str,
    encoding: RenderEncoding,
    rows: u16,
    cols: u16,
) -> Result<UnixStream, String> {
    let mut stream = connect(frames_path);
    send_frame(
        &mut stream,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding,
            local_token: "local-token".into(),
            cols,
            rows,
            tmux_identity: None,
        },
    );
    assert!(matches!(
        recv_frame(&mut stream),
        ServerMessage::Welcome { .. }
    ));
    send_frame(
        &mut stream,
        &ClientMessage::AttachTerminal {
            host_terminal_id: host_terminal_id.to_string(),
            reservation_id: None,
            locator: None,
        },
    );
    match recv_frame(&mut stream) {
        ServerMessage::Attached { .. } => Ok(stream),
        ServerMessage::Error { code, .. } => Err(code),
        other => panic!("expected Attached or Error, got {other:?}"),
    }
}

fn spawn_committed(
    control: &mut UnixStream,
    terminal_id: &str,
    argv: &[&str],
    rows: u16,
    cols: u16,
) -> String {
    let reserve_key = format!("rk-{terminal_id}");
    let spawn_key = format!("sk-{terminal_id}");
    let reserved = rpc(
        control,
        "reserve_observer",
        json!({"terminal_id": terminal_id, "reserve_key": reserve_key}),
    );
    let prepared = rpc(
        control,
        "spawn",
        json!({
            "operation_seq": 1,
            "terminal_id": terminal_id,
            "spawn_key": spawn_key,
            "reservation_id": reserved["reservation_id"],
            "reserve_key": reserve_key,
            "argv": argv,
            "cwd": "/",
            "rows": rows,
            "cols": cols,
            "commit_deadline_ms": 8000,
        }),
    );
    assert_eq!(prepared["ok"], true, "{prepared}");
    let host_terminal_id = prepared["host_terminal_id"]
        .as_str()
        .expect("host_terminal_id")
        .to_string();
    assert_eq!(
        rpc(
            control,
            "spawn_commit",
            json!({"terminal_id": terminal_id, "spawn_key": spawn_key}),
        )["ok"],
        true
    );
    host_terminal_id
}

fn read_frames_for(stream: &mut UnixStream, duration: Duration) -> usize {
    if stream
        .set_read_timeout(Some(Duration::from_millis(80)))
        .is_err()
    {
        return 0;
    }
    let deadline = Instant::now() + duration;
    let mut count = 0usize;
    while Instant::now() < deadline {
        match read_message(stream, MAX_FRAME_SIZE) {
            Ok(ServerMessage::Frame(_) | ServerMessage::Terminal(_)) => count += 1,
            Ok(_) => {}
            Err(err) if is_timeout(&err) => {}
            Err(_) => break,
        }
    }
    count
}

fn start_host(dir: &Path, extra: &[&str]) -> (host_support::HostProc, UnixStream, PathBuf) {
    write_token(dir, "frame-backpressure");
    let child = spawn_host_with_args(dir, extra);
    wait_socket(&dir.join(CONTROL_SOCKET));
    wait_socket(&dir.join(FRAMES_SOCKET));
    let mut control = connect(&dir.join(CONTROL_SOCKET));
    assert_eq!(
        hello_control(&mut control, "frame-backpressure")["ok"],
        true
    );
    (child, control, dir.join(FRAMES_SOCKET))
}

#[test]
fn slow_observer_resyncs_with_one_keyframe() {
    let dir = tempfile::tempdir().expect("tempdir");
    let rows = 8;
    let cols = 24;
    // One local 8x24 keyframe plus slack; a second live frame must overflow.
    let cap = one_keyframe_cap(rows, cols).saturating_add(256);
    let cap_arg = cap.to_string();
    let (_child, mut control, frames_path) = start_host(
        dir.path(),
        &["--delta-queue-bytes", &cap_arg, "--lag-timeout-ms", "5000"],
    );
    // Finite burst then idle so the replacement keyframe is not mixed with live
    // frames. 150 lines at 10ms overflows a one-keyframe cap without hitting lag.
    let host_terminal_id = spawn_committed(
        &mut control,
        "term-keyframe",
        &[
            "/bin/sh",
            "-c",
            "i=0; while [ \"$i\" -lt 150 ]; do i=$((i+1)); printf '%048d\\n' \"$i\"; sleep 0.01; done; exec sleep 30",
        ],
        rows,
        cols,
    );

    let mut slow = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::SemanticFrame,
        rows,
        cols,
    );
    let mut fast = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::SemanticFrame,
        rows,
        cols,
    );
    let initial = drain_frames(&mut slow, Duration::from_millis(80));
    assert!(
        initial.iter().any(is_keyframe),
        "slow observer should see the initial keyframe before pause: {initial:?}"
    );
    let _ = drain_frames(&mut fast, Duration::from_millis(80));
    let during = read_frames_for(&mut fast, Duration::from_millis(2000));
    assert!(
        during >= 4,
        "continuously reading observer must keep receiving frames while the slow one pauses; got {during}"
    );

    slow.set_read_timeout(Some(Duration::from_millis(500)))
        .expect("slow timeout");
    let replacement = match read_message(&mut slow, MAX_FRAME_SIZE) {
        Ok(message) => message,
        other => panic!("resync must deliver a replacement keyframe, got {other:?}"),
    };
    let queued_bytes = encoded_bytes(&replacement);
    assert!(is_keyframe(&replacement), "resync frame must be a keyframe");
    assert!(
        queued_bytes <= cap,
        "byte cap never exceeded; queued={queued_bytes} cap={cap}"
    );
    // The socket may still hold one in-flight write from before collapse.
    // The mailbox holds exactly one replacement keyframe. Later broadcasts
    // arrive on the 30ms producer interval, not in this burst.
    slow.set_read_timeout(Some(Duration::from_millis(2)))
        .expect("burst timeout");
    let mut burst = vec![replacement];
    loop {
        match read_message(&mut slow, MAX_FRAME_SIZE) {
            Ok(message) => burst.push(message),
            Err(err) if is_timeout(&err) => break,
            other => panic!("slow observer should stay connected, got {other:?}"),
        }
    }
    assert!(
        burst.len() <= 3,
        "exactly one replacement keyframe plus at most kernel in-flight frames; got {} frames",
        burst.len()
    );
    assert!(
        burst.iter().all(|message| encoded_bytes(message) <= cap),
        "byte cap never exceeded; cap={cap} frames={}",
        burst.len()
    );

    slow.set_read_timeout(Some(Duration::from_millis(200)))
        .expect("connected timeout");
    match read_message(&mut slow, MAX_FRAME_SIZE) {
        Ok(ServerMessage::Terminal(_) | ServerMessage::Frame(_)) => {}
        Ok(ServerMessage::Error { code, .. }) => {
            panic!("slow observer was closed after draining: {code}")
        }
        Err(err) if is_timeout(&err) => {}
        other => panic!("slow observer should stay connected, got {other:?}"),
    }

    send_json(
        &mut control,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut control);
}

#[test]
fn lagged_observer_is_closed_and_released() {
    let dir = tempfile::tempdir().expect("tempdir");
    let rows = 8;
    let cols = 24;
    let (_child, mut control, frames_path) = start_host(
        dir.path(),
        &[
            "--max-attachments-per-terminal",
            "2",
            "--lag-timeout-ms",
            "200",
            "--delta-queue-bytes",
            "4096",
        ],
    );
    let host_terminal_id = spawn_committed(
        &mut control,
        "term-lagged",
        &["/bin/sleep", "30"],
        rows,
        cols,
    );

    let mut slow = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::SemanticFrame,
        rows,
        cols,
    );
    let mut fast = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::SemanticFrame,
        rows,
        cols,
    );
    assert_eq!(
        try_attach_observer(
            &frames_path,
            &host_terminal_id,
            RenderEncoding::SemanticFrame,
            rows,
            cols,
        )
        .err()
        .as_deref(),
        Some("capacity"),
        "both observer slots should be occupied"
    );
    let _ = drain_frames(&mut fast, Duration::from_millis(80));
    let _ = drain_frames(&mut slow, Duration::from_millis(80));
    let during = read_frames_for(&mut fast, Duration::from_millis(500));
    let log = std::fs::read_to_string(dir.path().join("gterm.log")).unwrap_or_default();
    assert!(
        during >= 1,
        "the remaining observer must keep receiving frames; got {during}; log={log}"
    );
    try_attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::SemanticFrame,
        rows,
        cols,
    )
    .expect("lagged observer slot must be released");
    drop(slow);

    send_json(
        &mut control,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut control);
}
