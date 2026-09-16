//! Drive a live PTY through the frame-producer API with no ratatui::Frame.

mod host_support;

use std::io::{ErrorKind, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use base64::engine::general_purpose::STANDARD;
use base64::Engine as _;
use bytes::Bytes;
use gobby_terminal::pane::{PaneShellConfig, ShellMode};
use gobby_terminal::protocol::{
    read_message, write_message, CellData, ClientMessage, FrameData, FramingError, PaneModes,
    RenderEncoding, ServerMessage, MAX_FRAME_SIZE, PROTOCOL_VERSION,
};
use gobby_terminal::runtime::TerminalRuntime;
use gobby_terminal::terminal_theme::TerminalTheme;
use host_support::{
    connect, hello_control, recv_json, rpc, send_json, spawn_host, spawn_host_with_args,
    temp_socket_dir, wait_exit, wait_socket, write_token, CONTROL_SOCKET, FRAMES_SOCKET,
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
    let dir = temp_socket_dir();
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
        .set_read_timeout(Some(Duration::from_millis(50)))
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
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match frames.read(&mut buffer) {
            Ok(0) => break,
            Ok(_) => continue,
            Err(error)
                if matches!(
                    error.kind(),
                    ErrorKind::WouldBlock | ErrorKind::TimedOut | ErrorKind::Interrupted
                ) =>
            {
                if Instant::now() >= deadline {
                    panic!("frame task did not close its socket: {error}");
                }
            }
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

/// Read the next message, or `None` when the peer stays silent for `idle`.
fn next_message(stream: &mut UnixStream, idle: Duration) -> Option<ServerMessage> {
    stream.set_read_timeout(Some(idle)).expect("read timeout");
    match read_message(stream, MAX_FRAME_SIZE) {
        Ok(message) => Some(message),
        Err(err) if is_timeout(&err) => None,
        other => panic!("frame peer should stay connected, got {other:?}"),
    }
}

/// Echo one unique line into the terminal so the next producer tick has a real
/// change. Repeating the same line would scroll into an identical screen, which
/// the encoder correctly treats as nothing to send.
fn write_line(control: &mut UnixStream, host_terminal_id: &str, operation_seq: u64) {
    let data = STANDARD.encode(format!("line-{operation_seq:06}"));
    assert_eq!(
        rpc(
            control,
            "write",
            json!({
                "operation_seq": operation_seq,
                "host_terminal_id": host_terminal_id,
                "kind": "text",
                "encoding": "utf8-b64",
                "data": data,
                "submit": true,
            }),
        )["ok"],
        true,
        "control write must reach the terminal"
    );
}

fn diag(message: &ServerMessage) -> String {
    match message {
        ServerMessage::Terminal(frame) => format!(
            "seq={} full={} wire={} body={:?}",
            frame.seq,
            frame.full,
            encoded_bytes(message),
            String::from_utf8_lossy(&frame.bytes)
        ),
        other => format!("{other:?}"),
    }
}

fn is_delta(message: &ServerMessage) -> bool {
    matches!(message, ServerMessage::Terminal(frame) if !frame.full)
}

/// The wire half of the slow-observer contract: the peer is caught up by
/// exactly one replacement keyframe, and the bytes the kernel held for it
/// never exceed `delta_queue_bytes`. No control stat reports per-observer
/// queued bytes, so the host-side half -- the mailbox high-water mark --
/// is proved by `queued_bytes_never_exceed_the_delta_queue_cap` in
/// `src/host/backpressure/tests.rs`.
#[test]
fn slow_observer_resyncs_with_one_keyframe() {
    let dir = temp_socket_dir();
    let rows = 8;
    let cols = 24;
    // Admits one ANSI keyframe for this screen (asserted from the wire below)
    // plus a few deltas. The host pins the peer socket's SO_SNDBUF to the same
    // value, so the kernel can never hold more than the cap either.
    let cap = 1024usize;
    let cap_arg = cap.to_string();
    let (_child, mut control, frames_path) = start_host(
        dir.path(),
        &["--delta-queue-bytes", &cap_arg, "--lag-timeout-ms", "5000"],
    );
    // Screen changes come from control writes, so the producer is quiet exactly
    // when the test stops writing. "Exactly one replacement keyframe" is then
    // observable on the peer socket as silence after that keyframe.
    let host_terminal_id = spawn_committed(
        &mut control,
        "term-keyframe",
        &["/bin/sh", "-c", "exec sleep 30"],
        rows,
        cols,
    );

    let mut slow = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::TerminalAnsi,
        rows,
        cols,
    );
    let mut fast = attach_observer(
        &frames_path,
        &host_terminal_id,
        RenderEncoding::TerminalAnsi,
        rows,
        cols,
    );
    let initial = drain_frames(&mut slow, Duration::from_millis(200));
    for message in &initial {
        eprintln!("DIAG initial {}", diag(message));
    }
    let first_keyframe = initial
        .iter()
        .find(|message| is_keyframe(message))
        .unwrap_or_else(|| panic!("slow observer should see the initial keyframe: {initial:?}"));
    assert!(
        encoded_bytes(first_keyframe) <= cap,
        "the byte cap must admit one keyframe; keyframe={} cap={cap}",
        encoded_bytes(first_keyframe)
    );
    let _ = drain_frames(&mut fast, Duration::from_millis(200));

    // `slow` stops reading here. Thirty screen changes are far more than its
    // byte cap holds, while `fast` drains every one of them.
    let mut operation_seq = 2u64;
    let mut kept_reading = 0usize;
    for _ in 0..30 {
        write_line(&mut control, &host_terminal_id, operation_seq);
        operation_seq += 1;
        // One change per producer tick: the 30 ms broadcast interval would
        // otherwise fold a burst of writes into a single delta.
        let tick = Instant::now() + Duration::from_millis(45);
        while Instant::now() < tick {
            if let Some(message) = next_message(&mut fast, Duration::from_millis(45)) {
                eprintln!(
                    "DIAG fast after write {} {}",
                    operation_seq - 1,
                    diag(&message)
                );
                kept_reading += 1;
            }
        }
    }
    assert!(
        kept_reading >= 4,
        "a continuously reading observer must keep receiving frames while the slow one pauses; got {kept_reading}"
    );

    // Resume. Whatever the kernel accepted before the queue collapsed is a stale
    // delta; the mailbox holds one keyframe in place of everything it dropped.
    let mut stale: Vec<ServerMessage> = Vec::new();
    let replacement = loop {
        let Some(message) = next_message(&mut slow, Duration::from_millis(1000)) else {
            panic!(
                "resync must deliver a replacement keyframe; stale={} frames",
                stale.len()
            );
        };
        eprintln!("DIAG slow resume {}", diag(&message));
        if is_keyframe(&message) {
            break message;
        }
        assert!(
            is_delta(&message),
            "pre-collapse traffic must be deltas, got {message:?}"
        );
        stale.push(message);
    };
    // Everything the kernel accepted before the writer blocked fits the cap; the
    // blocked write is the single message beyond it.
    let stale_bytes: usize = stale.iter().map(encoded_bytes).sum();
    let accepted = stale_bytes.saturating_sub(stale.last().map_or(0, encoded_bytes));
    assert!(
        accepted <= cap,
        "bytes held for the paused peer never exceeded the cap; accepted={accepted} cap={cap} stale={} frames",
        stale.len()
    );
    assert!(
        encoded_bytes(&replacement) <= cap,
        "the replacement keyframe fits the byte cap; keyframe={} cap={cap}",
        encoded_bytes(&replacement)
    );
    if let Some(extra) = next_message(&mut slow, Duration::from_millis(600)) {
        eprintln!("DIAG extra {}", diag(&extra));
        while let Some(more) = next_message(&mut slow, Duration::from_millis(600)) {
            eprintln!("DIAG more {}", diag(&more));
        }
        panic!(
            "exactly one replacement keyframe: the quiet producer added another frame (keyframe={}, {} bytes)",
            is_keyframe(&extra),
            encoded_bytes(&extra)
        );
    }

    // The peer is still attached and resynced: the next screen change reaches it
    // as one delta on the producer cadence, not as a second replacement.
    write_line(&mut control, &host_terminal_id, operation_seq);
    let live = next_message(&mut slow, Duration::from_millis(1000))
        .unwrap_or_else(|| panic!("the resynced observer must receive live output"));
    assert!(
        is_delta(&live),
        "post-resync frames arrive as deltas on the producer cadence, got {live:?}"
    );
    assert!(
        next_message(&mut slow, Duration::from_millis(400)).is_none(),
        "one screen change must produce one frame"
    );

    send_json(
        &mut control,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut control);
}

#[test]
fn lagged_observer_is_closed_and_released() {
    let dir = temp_socket_dir();
    let rows = 8;
    let cols = 24;
    let lag = Duration::from_millis(400);
    let lag_arg = lag.as_millis().to_string();
    // One local keyframe plus slack: the second queued frame overflows, so a
    // peer that never reads is lagged rather than merely slow.
    let cap_arg = one_keyframe_cap(rows, cols).saturating_add(256).to_string();
    let (_child, mut control, frames_path) = start_host(
        dir.path(),
        &[
            "--max-attachments-per-terminal",
            "2",
            "--lag-timeout-ms",
            &lag_arg,
            "--delta-queue-bytes",
            &cap_arg,
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

    // `slow` stops reading here. The host must close it at the configured lag
    // timeout, release its slot, and keep serving `fast` the whole time.
    let stopped = Instant::now();
    let mut kept_reading = 0usize;
    let mut released = None;
    while stopped.elapsed() < lag * 8 {
        kept_reading += read_frames_for(&mut fast, Duration::from_millis(60));
        match try_attach_observer(
            &frames_path,
            &host_terminal_id,
            RenderEncoding::SemanticFrame,
            rows,
            cols,
        ) {
            Ok(stream) => {
                released = Some(stream);
                break;
            }
            Err(code) => assert_eq!(code, "capacity", "unexpected attach refusal"),
        }
    }
    let closed_after = stopped.elapsed();
    let log = std::fs::read_to_string(dir.path().join("gterm.log")).unwrap_or_default();
    let mut replacement = released.unwrap_or_else(|| {
        panic!(
            "the lagged observer slot was never released within {:?}; log={log}",
            lag * 8
        )
    });
    assert!(
        closed_after >= lag / 2,
        "the lagged close must wait for the timeout, not fire immediately; closed_after={closed_after:?} lag={lag:?}"
    );
    assert!(
        closed_after <= lag * 4,
        "the lagged close must land near the timeout; closed_after={closed_after:?} lag={lag:?}"
    );
    assert!(
        kept_reading >= 4,
        "the remaining observer must keep receiving frames throughout the lag window; got {kept_reading}; log={log}"
    );

    // The closed peer learns why: `lagged` on the wire, then EOF.
    slow.set_read_timeout(Some(Duration::from_millis(2000)))
        .expect("drain timeout");
    let mut saw_lagged = false;
    loop {
        match read_message(&mut slow, MAX_FRAME_SIZE) {
            Ok(ServerMessage::Error { code, .. }) => {
                assert_eq!(code, "lagged", "the close must name the lag");
                saw_lagged = true;
            }
            Ok(_) => {}
            Err(FramingError::Eof) => break,
            other => panic!("lagged peer must end with `lagged` then EOF, got {other:?}"),
        }
    }
    assert!(
        saw_lagged,
        "the never-draining peer must receive `lagged` before EOF; log={log}"
    );

    // The released slot serves a new observer.
    let after = drain_frames(&mut replacement, Duration::from_millis(200));
    assert!(
        after.iter().any(is_keyframe),
        "a new observer must receive frames after the lagged close; got {after:?}"
    );

    send_json(
        &mut control,
        &json!({"method": "host_shutdown", "grace_ms": 20}),
    );
    let _ = recv_json(&mut control);
}
