mod mock_daemon;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use gobby_client::daemon::{Daemon, LiveDaemon};
use gobby_client::frame_source::{
    AttachLocator, FrameError, FrameSource, PaneFrameSource, ProxyFrameSource, ScriptedFrameSource,
    Transport, UnixSocketFrameSource,
};
use gobby_client::Workspace;
use gobby_terminal::protocol::{
    read_message_async, write_message, write_message_async, CellData, ClientMessage, FrameData,
    PaneLocator, PaneModes, RenderEncoding, ServerMessage, TmuxClientIdentity, MAX_FRAME_SIZE,
    PROTOCOL_VERSION,
};
use serde_json::{json, Value};
use std::io::{Cursor, Read, Write};
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixStream as StdUnixStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::oneshot;
use tokio::time::{sleep, timeout, Duration};

const IO_TIMEOUT: Duration = Duration::from_secs(3);
const HOST_TIMEOUT: Duration = Duration::from_secs(8);
const CONTROL_SOCKET: &str = "gterm-control.sock";
const FRAMES_SOCKET: &str = "gterm-frames.sock";
const LOCAL_TOKEN: &str = "local-token";

struct TestHost {
    dir: tempfile::TempDir,
    child: Child,
}

impl TestHost {
    async fn spawn(extra: &[&str]) -> Self {
        let binary = tokio::task::spawn_blocking(build_test_gterm)
            .await
            .expect("gterm build task");
        let dir = tempfile::tempdir().expect("gterm socket dir");
        let control_token = dir.path().join("gterm-control.token");
        std::fs::write(&control_token, "control-token").expect("write control token");
        let mut permissions = std::fs::metadata(&control_token)
            .expect("control token metadata")
            .permissions();
        permissions.set_mode(0o600);
        std::fs::set_permissions(&control_token, permissions).expect("protect control token");
        std::fs::write(dir.path().join("local_cli_token"), LOCAL_TOKEN).expect("write local token");
        let child = Command::new(binary)
            .arg("host")
            .arg("--socket-dir")
            .arg(dir.path())
            .arg("--tmux-poll-interval-ms")
            .arg("50")
            .args(extra)
            .env("GTERM_LOG_FILE", dir.path().join("gterm.log"))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("spawn test gterm host");
        let host = Self { dir, child };
        wait_for_socket(&host.dir.path().join(CONTROL_SOCKET)).await;
        wait_for_socket(&host.dir.path().join(FRAMES_SOCKET)).await;
        host
    }

    fn frame_socket(&self) -> PathBuf {
        self.dir.path().join(FRAMES_SOCKET)
    }

    fn socket_dir(&self) -> &Path {
        self.dir.path()
    }
}

impl Drop for TestHost {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

struct TestTmux {
    _dir: tempfile::TempDir,
    socket: PathBuf,
    pane_id: String,
    server_pid: i32,
    server_start_time: i64,
}

impl TestTmux {
    fn start() -> Self {
        let dir = tempfile::tempdir().expect("tmux socket dir");
        let socket = dir.path().join("tmux.sock");
        let status = Command::new("tmux")
            .arg("-S")
            .arg(&socket)
            .args([
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "gclient-frame-source",
                "-x",
                "80",
                "-y",
                "24",
                "--",
                "/bin/sh",
            ])
            .status()
            .expect("spawn test tmux");
        assert!(status.success(), "tmux new-session failed");
        let pane_id = tmux_output(&socket, &["display-message", "-p", "#{pane_id}"]);
        let server_pid = tmux_output(&socket, &["display-message", "-p", "#{pid}"])
            .parse()
            .expect("tmux pid");
        let server_start_time = tmux_output(&socket, &["display-message", "-p", "#{start_time}"])
            .parse()
            .expect("tmux start time");
        Self {
            _dir: dir,
            socket,
            pane_id,
            server_pid,
            server_start_time,
        }
    }

    fn pane_locator(&self) -> PaneLocator {
        PaneLocator {
            socket_path: self.socket.to_string_lossy().into_owned(),
            pane_id: self.pane_id.clone(),
            server_pid: self.server_pid,
            server_start_time: self.server_start_time,
        }
    }

    fn identity(&self) -> TmuxClientIdentity {
        TmuxClientIdentity {
            socket_path: self.socket.to_string_lossy().into_owned(),
            pane_id: self.pane_id.clone(),
            server_pid: self.server_pid,
            server_start_time: self.server_start_time,
        }
    }

    fn send_hex(&self, bytes: &[u8]) {
        for chunk in bytes.chunks(128) {
            let encoded: Vec<String> = chunk.iter().map(|byte| format!("{byte:02x}")).collect();
            let mut args = vec!["send-keys", "-H", "-t", self.pane_id.as_str()];
            args.extend(encoded.iter().map(String::as_str));
            let _ = tmux_output(&self.socket, &args);
        }
    }

    fn capture(&self) -> String {
        tmux_output(&self.socket, &["capture-pane", "-p", "-t", &self.pane_id])
    }
}

impl Drop for TestTmux {
    fn drop(&mut self) {
        let _ = Command::new("tmux")
            .arg("-S")
            .arg(&self.socket)
            .arg("kill-server")
            .status();
    }
}

fn build_test_gterm() -> PathBuf {
    let workspace = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    let binary = std::env::current_exe()
        .expect("current test binary")
        .parent()
        .and_then(Path::parent)
        .expect("target profile directory")
        .join("gterm");
    if binary.is_file() {
        return binary;
    }
    let status = Command::new(env!("CARGO"))
        .current_dir(&workspace)
        .args([
            "build",
            "-p",
            "gobby-terminal",
            "--features",
            "vt-engine",
            "--bin",
            "gterm",
        ])
        .status()
        .expect("build test gterm");
    assert!(status.success(), "test gterm build failed");
    binary
}

async fn wait_for_socket(path: &Path) {
    timeout(HOST_TIMEOUT, async {
        loop {
            if UnixStream::connect(path).await.is_ok() {
                return;
            }
            sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {}", path.display()));
}

fn tmux_output(socket: &Path, args: &[&str]) -> String {
    let output = Command::new("tmux")
        .arg("-S")
        .arg(socket)
        .args(args)
        .output()
        .expect("run tmux");
    assert!(output.status.success(), "tmux command failed: {output:?}");
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn send_control(stream: &mut StdUnixStream, value: &Value) {
    let mut line = serde_json::to_vec(value).expect("encode control request");
    line.push(b'\n');
    stream.write_all(&line).expect("write control request");
    stream.flush().expect("flush control request");
}

fn recv_control(stream: &mut StdUnixStream) -> Value {
    stream
        .set_read_timeout(Some(HOST_TIMEOUT))
        .expect("control read timeout");
    let mut line = Vec::new();
    let mut byte = [0_u8; 1];
    loop {
        stream.read_exact(&mut byte).expect("read control reply");
        if byte[0] == b'\n' {
            break;
        }
        line.push(byte[0]);
    }
    serde_json::from_slice(&line).expect("decode control reply")
}

fn control_connection_at(host_dir: &Path) -> StdUnixStream {
    let mut stream =
        StdUnixStream::connect(host_dir.join(CONTROL_SOCKET)).expect("connect control");
    send_control(
        &mut stream,
        &json!({
            "method": "hello",
            "protocol_version": 1,
            "control_token": "control-token"
        }),
    );
    let reply = recv_control(&mut stream);
    assert_eq!(reply["ok"], true, "control hello: {reply}");
    stream
}

fn control_epoch_at(host_dir: &Path) -> String {
    let mut control = control_connection_at(host_dir);
    send_control(&mut control, &json!({"method": "ping"}));
    let reply = recv_control(&mut control);
    assert_eq!(reply["ok"], true, "control ping: {reply}");
    reply["host_epoch"]
        .as_str()
        .expect("host epoch")
        .to_string()
}

fn spawn_native_terminal_at(host_dir: &Path) -> (String, String) {
    let mut control = control_connection_at(host_dir);
    send_control(&mut control, &json!({"method": "ping"}));
    let ping = recv_control(&mut control);
    let epoch = ping["host_epoch"].as_str().expect("host epoch").to_string();
    send_control(
        &mut control,
        &json!({
            "method": "reserve_observer",
            "terminal_id": "gclient-real-native",
            "reserve_key": "gclient-reserve"
        }),
    );
    let reserved = recv_control(&mut control);
    assert_eq!(reserved["ok"], true, "reserve native observer: {reserved}");
    send_control(
        &mut control,
        &json!({
            "method": "spawn",
            "id": "spawn-1",
            "operation_seq": 1,
            "terminal_id": "gclient-real-native",
            "spawn_key": "gclient-spawn",
            "reservation_id": reserved["reservation_id"],
            "reserve_key": "gclient-reserve",
            "argv": ["/bin/sh", "-c", "printf 'GCLIENT-NATIVE-READY\\n'; exec cat"],
            "cwd": host_dir.to_string_lossy(),
            "rows": 24,
            "cols": 80,
            "commit_deadline_ms": 5000
        }),
    );
    let prepared = recv_control(&mut control);
    assert_eq!(prepared["ok"], true, "prepare native terminal: {prepared}");
    let host_terminal_id = prepared["host_terminal_id"]
        .as_str()
        .expect("native host terminal id")
        .to_string();
    send_control(
        &mut control,
        &json!({
            "method": "spawn_commit",
            "terminal_id": "gclient-real-native",
            "spawn_key": "gclient-spawn"
        }),
    );
    let committed = recv_control(&mut control);
    assert_eq!(committed["ok"], true, "commit native terminal: {committed}");
    (epoch, host_terminal_id)
}

fn frame_text(message: &ServerMessage) -> Option<String> {
    let ServerMessage::Frame(frame) = message else {
        return None;
    };
    let mut text = String::new();
    for (index, cell) in frame.cells.iter().enumerate() {
        if index > 0 && (index as u16).is_multiple_of(frame.width) {
            text.push('\n');
        }
        text.push_str(&cell.symbol);
    }
    Some(text)
}

async fn collect_direct_until<F>(
    source: &mut UnixSocketFrameSource,
    mut predicate: F,
) -> Vec<ServerMessage>
where
    F: FnMut(&ServerMessage) -> bool,
{
    timeout(HOST_TIMEOUT, async {
        let mut messages = Vec::new();
        loop {
            let message = source.recv().await.expect("receive direct frame");
            let done = predicate(&message);
            messages.push(message);
            if done {
                return messages;
            }
        }
    })
    .await
    .expect("direct frame deadline")
}

fn native_locator(socket_path: &std::path::Path, epoch: &str) -> AttachLocator {
    native_locator_for(socket_path, epoch, "host-terminal-1")
}

fn native_locator_for(socket_path: &Path, epoch: &str, host_terminal_id: &str) -> AttachLocator {
    AttachLocator {
        backend: "native".into(),
        frame_host_epoch: epoch.into(),
        host_terminal_id: host_terminal_id.into(),
        frame_socket_path: socket_path.display().to_string(),
        pane: None,
    }
}

fn semantic_frame(symbol: &str) -> ServerMessage {
    ServerMessage::Frame(FrameData {
        cells: vec![CellData {
            symbol: symbol.into(),
            fg: 2,
            bg: 0,
            modifier: 1,
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

#[tokio::test]
async fn direct_frames_verify_epoch_and_render() {
    let host = TestHost::spawn(&[]).await;
    let host_dir = host.socket_dir().to_path_buf();
    let (epoch, host_terminal_id) =
        tokio::task::spawn_blocking(move || spawn_native_terminal_at(&host_dir))
            .await
            .expect("spawn native control task");

    let relay_dir = tempfile::tempdir().expect("epoch relay dir");
    let relay_path = relay_dir.path().join("frames-relay.sock");
    let relay = UnixListener::bind(&relay_path).expect("bind epoch relay");
    let upstream_path = host.frame_socket();
    let relay_task = tokio::spawn(async move {
        let (mut client, _) = relay.accept().await.expect("accept epoch client");
        let mut upstream = UnixStream::connect(upstream_path)
            .await
            .expect("connect real host from relay");
        let hello: ClientMessage = read_message_async(&mut client, MAX_FRAME_SIZE)
            .await
            .expect("relayed hello");
        write_message_async(&mut upstream, &hello)
            .await
            .expect("forward hello");
        let welcome: ServerMessage = read_message_async(&mut upstream, MAX_FRAME_SIZE)
            .await
            .expect("real welcome");
        write_message_async(&mut client, &welcome)
            .await
            .expect("forward welcome");
        matches!(
            timeout(
                Duration::from_secs(1),
                read_message_async::<_, ClientMessage>(&mut client, MAX_FRAME_SIZE),
            )
            .await,
            Ok(Err(_))
        )
    });
    let mismatch = UnixSocketFrameSource::connect(
        &native_locator_for(&relay_path, "stale-host-epoch", &host_terminal_id),
        LOCAL_TOKEN,
        80,
        24,
    )
    .await
    .expect_err("epoch mismatch must refuse direct attach");
    assert!(matches!(
        mismatch,
        FrameError::HostEpochChanged { expected, actual }
            if expected == "stale-host-epoch" && actual == epoch
    ));
    assert!(
        relay_task.await.expect("epoch relay task"),
        "epoch mismatch wrote AttachTerminal"
    );

    let mut source = UnixSocketFrameSource::connect(
        &native_locator_for(&host.frame_socket(), &epoch, &host_terminal_id),
        LOCAL_TOKEN,
        80,
        24,
    )
    .await
    .expect("connect real direct source");
    assert_eq!(source.transport(), Transport::Direct);
    assert!(matches!(
        timeout(IO_TIMEOUT, source.recv()).await.expect("attach timeout"),
        Ok(ServerMessage::Attached { host_terminal_id: attached, .. })
            if attached == host_terminal_id
    ));
    let messages = collect_direct_until(&mut source, |message| {
        frame_text(message).is_some_and(|text| text.contains("GCLIENT-NATIVE-READY"))
    })
    .await;
    assert!(messages.iter().any(|message| {
        frame_text(message).is_some_and(|text| text.contains("GCLIENT-NATIVE-READY"))
    }));
}

#[tokio::test]
async fn tmux_pane_attaches_through_host_observer() {
    let tmux = TestTmux::start();
    tmux.send_hex(b"printf 'GCLIENT-TMUX-HISTORY\\n'\n");
    timeout(HOST_TIMEOUT, async {
        while !tmux.capture().contains("GCLIENT-TMUX-HISTORY") {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("tmux initial content");
    let host = TestHost::spawn(&[]).await;
    let host_dir = host.socket_dir().to_path_buf();
    let epoch = tokio::task::spawn_blocking(move || control_epoch_at(&host_dir))
        .await
        .expect("control epoch task");
    let locator = AttachLocator {
        backend: "tmux".into(),
        frame_host_epoch: epoch.clone(),
        host_terminal_id: "tmux-observer".into(),
        frame_socket_path: host.frame_socket().display().to_string(),
        pane: Some(tmux.pane_locator()),
    };
    let mut source = UnixSocketFrameSource::connect(&locator, LOCAL_TOKEN, 80, 24)
        .await
        .expect("real tmux direct source");
    assert!(matches!(
        timeout(IO_TIMEOUT, source.recv())
            .await
            .expect("tmux attach timeout"),
        Ok(ServerMessage::Attached { created: true, .. })
    ));
    let initial = collect_direct_until(&mut source, |message| {
        matches!(message, ServerMessage::AttachHistory { .. })
    })
    .await;
    assert!(matches!(
        initial.last(),
        Some(ServerMessage::AttachHistory { text, .. }) if text.contains("GCLIENT-TMUX-HISTORY")
    ));
    tmux.send_hex(b"printf 'GCLIENT-TMUX-CHANGED\\n'\n");
    let changed = collect_direct_until(&mut source, |message| {
        frame_text(message).is_some_and(|text| text.contains("GCLIENT-TMUX-CHANGED"))
    })
    .await;
    assert!(changed.iter().any(|message| {
        frame_text(message).is_some_and(|text| text.contains("GCLIENT-TMUX-CHANGED"))
    }));

    let mut same_pane = UnixStream::connect(host.frame_socket())
        .await
        .expect("connect same-pane client");
    write_message_async(
        &mut same_pane,
        &ClientMessage::Hello {
            version: PROTOCOL_VERSION,
            encoding: RenderEncoding::SemanticFrame,
            local_token: LOCAL_TOKEN.into(),
            cols: 80,
            rows: 24,
            tmux_identity: Some(tmux.identity()),
        },
    )
    .await
    .expect("same-pane hello");
    assert!(matches!(
        read_message_async::<_, ServerMessage>(&mut same_pane, MAX_FRAME_SIZE)
            .await
            .expect("same-pane welcome"),
        ServerMessage::Welcome { host_epoch } if host_epoch == epoch
    ));
    write_message_async(
        &mut same_pane,
        &ClientMessage::AttachTerminal {
            host_terminal_id: locator.host_terminal_id,
            reservation_id: None,
            locator: locator.pane,
        },
    )
    .await
    .expect("same-pane attach");
    assert!(matches!(
        read_message_async::<_, ServerMessage>(&mut same_pane, MAX_FRAME_SIZE)
            .await
            .expect("typed same-pane refusal"),
        ServerMessage::Error { code, .. } if code == "self_view"
    ));
}

#[tokio::test]
async fn epoch_mismatch_refuses_attach() {
    let (client, mut socket) = UnixStream::pair().expect("frame socket pair");
    let host = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut socket, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(
            &mut socket,
            &ServerMessage::Welcome {
                host_epoch: "new-epoch".into(),
            },
        )
        .await
        .expect("welcome");
        timeout(
            IO_TIMEOUT,
            read_message_async::<_, ClientMessage>(&mut socket, MAX_FRAME_SIZE),
        )
        .await
    });

    let error = UnixSocketFrameSource::connect_stream(
        client,
        &native_locator(std::path::Path::new("socket-pair"), "old-epoch"),
        "local-token",
        80,
        24,
    )
    .await
    .expect_err("epoch mismatch");
    assert!(matches!(
        error,
        FrameError::HostEpochChanged { expected, actual }
            if expected == "old-epoch" && actual == "new-epoch"
    ));
    let post_welcome = host.await.expect("fake host task");
    assert!(
        !matches!(post_welcome, Ok(Ok(_))),
        "epoch mismatch must close without sending AttachTerminal"
    );
}

#[tokio::test]
async fn cancelled_direct_read_retires_and_closes_both_halves() {
    let mut framed = Vec::new();
    write_message(&mut framed, &semantic_frame("partial")).expect("encode partial frame");
    for partial_len in [2, 6] {
        let (client, mut host) = UnixStream::pair().expect("frame socket pair");
        let partial = framed[..partial_len].to_vec();
        let (partial_written, partial_observed) = oneshot::channel();
        let host_task = tokio::spawn(async move {
            let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
                .await
                .expect("hello");
            write_message_async(
                &mut host,
                &ServerMessage::Welcome {
                    host_epoch: "cancel-epoch".into(),
                },
            )
            .await
            .expect("welcome");
            let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
                .await
                .expect("attach");
            host.write_all(&partial).await.expect("partial frame");
            partial_written.send(()).expect("partial signal");
            let mut byte = [0_u8; 1];
            timeout(IO_TIMEOUT, host.read(&mut byte))
                .await
                .expect("cancelled source closes socket")
                .expect("socket close")
        });
        let mut source = UnixSocketFrameSource::connect_stream(
            client,
            &native_locator(std::path::Path::new("socket-pair"), "cancel-epoch"),
            "token",
            80,
            24,
        )
        .await
        .expect("direct source");
        partial_observed.await.expect("partial frame observed");
        source.cancel_reader_task();
        assert!(matches!(source.recv().await, Err(FrameError::Cancelled)));
        assert!(matches!(
            source
                .send(&ClientMessage::SetViewport { rows: 24, cols: 80 })
                .await,
            Err(FrameError::Cancelled)
        ));
        assert_eq!(host_task.await.expect("host task"), 0);
    }

    let (client, mut host) = UnixStream::pair().expect("writer socket pair");
    let (attached, attached_rx) = oneshot::channel();
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("writer hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "cancel-writer-epoch".into(),
            },
        )
        .await
        .expect("writer welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("writer attach");
        attached.send(()).expect("writer attached signal");
        let mut byte = [0_u8; 1];
        timeout(IO_TIMEOUT, host.read(&mut byte))
            .await
            .expect("writer cancellation closes socket")
            .expect("writer socket close")
    });
    let mut source = UnixSocketFrameSource::connect_stream(
        client,
        &native_locator(std::path::Path::new("socket-pair"), "cancel-writer-epoch"),
        "token",
        80,
        24,
    )
    .await
    .expect("writer direct source");
    attached_rx.await.expect("writer attached");
    source.cancel_writer_task();
    assert!(matches!(source.recv().await, Err(FrameError::Cancelled)));
    assert!(matches!(
        source
            .send(&ClientMessage::SetScrollOffset {
                rows_from_live_edge: 1,
            })
            .await,
        Err(FrameError::Cancelled)
    ));
    assert_eq!(host_task.await.expect("writer host"), 0);
}

#[tokio::test]
async fn direct_frame_overflow_fails_typed_without_dropping() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": "terminal-overflow",
                "backend": "native",
                "state": "live"
            }],
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
    let pane_id = workspace
        .pane_for_terminal("terminal-overflow")
        .expect("overflow pane");
    let old_attachment = workspace.pane(pane_id).attachment_id().to_string();
    let attach_count_before = terminal_attach_requests(&mock).len();

    let (client, mut host) = UnixStream::pair().expect("direct socket pair");
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "overflow-epoch".into(),
            },
        )
        .await
        .expect("welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("attach");
        for index in 0..257 {
            write_message_async(
                &mut host,
                &ServerMessage::AttachHistory {
                    text: index.to_string(),
                    truncated: false,
                    dropped_bytes: 0,
                    total_bytes: 0,
                },
            )
            .await
            .expect("incremental frame");
        }
        timeout(
            IO_TIMEOUT,
            read_message_async::<_, ClientMessage>(&mut host, MAX_FRAME_SIZE),
        )
        .await
        .expect("source closes overflowed socket")
        .expect_err("overflow closes direct observer")
    });
    let direct = UnixSocketFrameSource::connect_stream(
        client,
        &native_locator(std::path::Path::new("socket-pair"), "overflow-epoch"),
        "local-token",
        80,
        24,
    )
    .await
    .expect("direct source");
    workspace
        .replace_frame_source(pane_id, PaneFrameSource::Direct(direct))
        .expect("install direct source");
    host_task.await.expect("overflow host");

    for index in 0..256 {
        assert!(matches!(
            workspace.recv_live_frame(pane_id).await,
            Ok(ServerMessage::AttachHistory { text, .. }) if text == index.to_string()
        ));
    }
    assert!(matches!(
        workspace.recv_live_frame(pane_id).await,
        Err(FrameError::Lag)
    ));
    let attaches = terminal_attach_requests(&mock);
    assert_eq!(attaches.len(), attach_count_before + 1);
    assert_eq!(
        attaches.last().and_then(|body| body.get("frame_delivery")),
        Some(&json!("proxy"))
    );
    assert_eq!(
        attaches.last().and_then(|body| body.get("encoding")),
        Some(&json!("semantic_frame"))
    );
    assert!(workspace.pane_by_attachment(&old_attachment).is_none());
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
    mock.shutdown().await;
}

#[tokio::test]
async fn proxy_source_decodes_cell_frames() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect daemon");
    let mut source = ProxyFrameSource::attach(daemon, "terminal-1")
        .await
        .expect("proxy attach");
    assert_eq!(source.transport(), Transport::Proxy);

    let corpus = include_bytes!("../../gterminal/tests/fixtures/wire_golden/frame.bin");
    let expected: ServerMessage =
        gobby_terminal::protocol::read_message(&mut Cursor::new(corpus), MAX_FRAME_SIZE)
            .expect("canonical semantic frame");
    let payload = &corpus[4..];
    mock.send_event_and_wait(json!({
        "type": "terminal_frame",
        "terminal_id": "terminal-1",
        "attachment_id": source.attachment_id(),
        "encoding": "bincode-b64",
        "payload": STANDARD.encode(payload),
    }))
    .await;
    assert_eq!(
        timeout(IO_TIMEOUT, source.recv())
            .await
            .expect("proxy frame timeout")
            .expect("proxy frame"),
        expected
    );

    source
        .send(&ClientMessage::SetViewport {
            rows: 42,
            cols: 120,
        })
        .await
        .expect("proxy viewport");
    source
        .send(&ClientMessage::SetScrollOffset {
            rows_from_live_edge: 9,
        })
        .await
        .expect("proxy scroll");
    assert_eq!(source.daemon().pending_counts(), (0, 0, 0));
    mock.send_event_and_wait(json!({
        "type": "terminal_scroll_offset_applied",
        "terminal_id": "terminal-1",
        "attachment_id": source.attachment_id(),
        "applied_rows": 7,
        "max_rows": 50,
    }))
    .await;
    assert_eq!(
        source.recv().await.expect("applied scroll"),
        ServerMessage::ScrollOffsetApplied {
            applied_rows: 7,
            max_rows: 50,
        }
    );

    let notifications: Vec<_> = mock
        .requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| {
            matches!(
                body.get("type").and_then(serde_json::Value::as_str),
                Some("terminal_set_viewport" | "terminal_set_scroll_offset")
            )
        })
        .collect();
    assert_eq!(notifications.len(), 2);

    let mut encoded = Vec::new();
    write_message(&mut encoded, &expected).expect("encode verification frame");
    let decoded: ServerMessage =
        gobby_terminal::protocol::read_message(&mut Cursor::new(encoded), MAX_FRAME_SIZE)
            .expect("canonical frame remains valid");
    assert_eq!(decoded, expected);

    let daemon = source.daemon().clone();
    mock.use_unique_attachment_ids();
    exercise_proxy_boundary_schedule(
        &mock,
        &daemon,
        "attachment-1",
        BoundarySchedule::BeforeResult,
    )
    .await;
    exercise_proxy_boundary_schedule(
        &mock,
        &daemon,
        "attachment-2",
        BoundarySchedule::BetweenResultAndConstruction,
    )
    .await;
    exercise_proxy_boundary_schedule(
        &mock,
        &daemon,
        "attachment-3",
        BoundarySchedule::AfterConstruction,
    )
    .await;
    mock.shutdown().await;
}

#[tokio::test]
async fn proxy_lag_recovers_from_a_fresh_keyframe() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": "terminal-lag",
                "backend": "native",
                "state": "live"
            }],
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
    let pane_id = workspace
        .pane_for_terminal("terminal-lag")
        .expect("lag pane");
    let old_attachment = workspace.pane(pane_id).attachment_id().to_string();
    let old_delta = semantic_frame("old-delta");
    let mut framed = Vec::new();
    write_message(&mut framed, &old_delta).expect("encode old delta");
    let old_payload = STANDARD.encode(&framed[4..]);
    for _ in 0..257 {
        mock.send_event_and_wait(json!({
            "type": "terminal_frame",
            "terminal_id": "terminal-lag",
            "attachment_id": old_attachment,
            "encoding": "bincode-b64",
            "payload": old_payload,
        }))
        .await;
    }
    workspace
        .daemon()
        .send(json!({
            "type": "terminal_take_control",
            "terminal_id": "terminal-lag",
            "attachment_id": old_attachment,
        }))
        .await
        .expect("ordered delivery barrier");
    let attach_count_before = terminal_attach_requests(&mock).len();

    assert!(matches!(
        workspace.recv_live_frame(pane_id).await,
        Err(FrameError::Lag)
    ));
    let new_attachment = workspace.pane(pane_id).attachment_id().to_string();
    assert_ne!(new_attachment, old_attachment);
    assert!(workspace.pane_by_attachment(&old_attachment).is_none());
    assert_eq!(workspace.pane(pane_id).frames_rendered(), 0);
    let attaches = terminal_attach_requests(&mock);
    assert_eq!(attaches.len(), attach_count_before + 1);
    assert_eq!(
        attaches.last().and_then(|body| body.get("encoding")),
        Some(&json!("semantic_frame"))
    );
    let detaches: Vec<_> = mock
        .requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!("terminal_detach")))
        .collect();
    assert_eq!(detaches.len(), 1);
    assert_eq!(
        detaches[0].get("attachment_id"),
        Some(&json!(old_attachment))
    );

    let keyframe = semantic_frame("fresh-keyframe");
    let mut framed = Vec::new();
    write_message(&mut framed, &keyframe).expect("encode fresh keyframe");
    mock.send_event_and_wait(json!({
        "type": "terminal_frame",
        "terminal_id": "terminal-lag",
        "attachment_id": new_attachment,
        "encoding": "bincode-b64",
        "payload": STANDARD.encode(&framed[4..]),
    }))
    .await;
    assert_eq!(
        workspace
            .recv_live_frame(pane_id)
            .await
            .expect("fresh keyframe"),
        keyframe
    );
    assert_eq!(workspace.pane(pane_id).frames_rendered(), 1);
    mock.shutdown().await;
}

#[tokio::test]
async fn proxy_finalization_tombstones_without_followup_requests() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{"terminal_id": "terminal-final", "backend": "native", "state": "live"}],
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
    let pane_id = workspace
        .pane_for_terminal("terminal-final")
        .expect("finalized pane");
    let attachment_id = workspace.pane(pane_id).attachment_id().to_string();
    timeout(IO_TIMEOUT, async {
        loop {
            if mock.requests().iter().any(|request| {
                request.body.as_ref().and_then(|body| body.get("type"))
                    == Some(&json!("terminal_set_viewport"))
            }) {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("initial viewport reaches the mock before the request snapshot");
    let requests_before = mock.requests().len();
    mock.send_event_and_wait(json!({
        "type": "terminal_attachment_finalized",
        "daemon_epoch": "epoch-1",
        "seq": 1,
        "terminal_id": "terminal-final",
        "attachment_id": attachment_id,
        "code": "host_eof",
        "reason": "host observer ended",
    }))
    .await;
    assert!(matches!(
        workspace.recv_live_frame(pane_id).await,
        Err(FrameError::Finalized { code, reason })
            if code == "host_eof" && reason == "host observer ended"
    ));
    assert_eq!(workspace.pane(pane_id).attachment_id(), "");
    assert!(workspace.pane(pane_id).frame_source().is_none());
    assert!(!workspace.pane(pane_id).writable());
    assert_eq!(mock.requests().len(), requests_before);

    let daemon = workspace.daemon().clone();
    let (_, receiver) = daemon.subscribe();
    mock.send_event_and_wait(json!({
        "type": "terminal_attachment_finalized",
        "daemon_epoch": "epoch-1",
        "seq": 2,
        "terminal_id": "terminal-final",
        "attachment_id": "attachment-2",
        "code": "attach_eof",
        "reason": "ended before attach result",
    }))
    .await;
    let reply = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": "terminal-final",
            "frame_delivery": "proxy",
            "encoding": "semantic_frame",
        }))
        .await
        .expect("attach result after finalization");
    assert_eq!(reply.get("attachment_id"), Some(&json!("attachment-2")));
    let error =
        ProxyFrameSource::from_attachment(daemon, "terminal-final", "attachment-2", receiver)
            .expect_err("pre-result finalization prevents source installation");
    assert!(matches!(
        error,
        FrameError::Finalized { code, reason }
            if code == "attach_eof" && reason == "ended before attach result"
    ));
    let forbidden: Vec<_> = mock
        .requests()
        .into_iter()
        .skip(requests_before)
        .filter_map(|request| request.body)
        .filter(|body| {
            matches!(
                body.get("type").and_then(serde_json::Value::as_str),
                Some(
                    "terminal_detach"
                        | "terminal_set_viewport"
                        | "terminal_take_control"
                        | "terminal_release_control"
                )
            )
        })
        .collect();
    assert!(forbidden.is_empty());
    mock.shutdown().await;
}

#[derive(Clone, Copy)]
enum BoundarySchedule {
    BeforeResult,
    BetweenResultAndConstruction,
    AfterConstruction,
}

async fn exercise_proxy_boundary_schedule(
    mock: &mock_daemon::MockDaemon,
    daemon: &LiveDaemon,
    expected_attachment: &str,
    schedule: BoundarySchedule,
) {
    let (_, receiver) = daemon.subscribe();
    if matches!(schedule, BoundarySchedule::BeforeResult) {
        emit_proxy_boundary(mock, expected_attachment).await;
    }
    let reply = daemon
        .send(json!({
            "type": "terminal_attach",
            "request_id": uuid::Uuid::new_v4().to_string(),
            "terminal_id": "terminal-schedule",
            "frame_delivery": "proxy",
            "encoding": "semantic_frame",
        }))
        .await
        .expect("scheduled proxy attach");
    let attachment_id = reply
        .get("attachment_id")
        .and_then(serde_json::Value::as_str)
        .expect("scheduled attachment id");
    assert_eq!(attachment_id, expected_attachment);
    if matches!(schedule, BoundarySchedule::BetweenResultAndConstruction) {
        emit_proxy_boundary(mock, expected_attachment).await;
    }
    let mut source = ProxyFrameSource::from_attachment(
        daemon.clone(),
        "terminal-schedule",
        attachment_id,
        receiver,
    )
    .expect("scheduled proxy source");
    if matches!(schedule, BoundarySchedule::AfterConstruction) {
        emit_proxy_boundary(mock, expected_attachment).await;
    }
    assert!(matches!(
        source.recv().await,
        Ok(ServerMessage::AttachHistory { text, .. }) if text == expected_attachment
    ));
    assert_eq!(
        source.recv().await.expect("scheduled keyframe"),
        semantic_frame(expected_attachment)
    );
    assert!(timeout(Duration::from_millis(20), source.recv())
        .await
        .is_err());
}

async fn emit_proxy_boundary(mock: &mock_daemon::MockDaemon, attachment_id: &str) {
    mock.send_event_and_wait(json!({
        "type": "terminal_attach_history",
        "terminal_id": "terminal-schedule",
        "attachment_id": attachment_id,
        "text": attachment_id,
        "truncated": false,
        "dropped_bytes": 0,
        "total_bytes": attachment_id.len(),
    }))
    .await;
    let frame = semantic_frame(attachment_id);
    let mut framed = Vec::new();
    write_message(&mut framed, &frame).expect("encode scheduled keyframe");
    mock.send_event_and_wait(json!({
        "type": "terminal_frame",
        "terminal_id": "terminal-schedule",
        "attachment_id": attachment_id,
        "encoding": "bincode-b64",
        "payload": STANDARD.encode(&framed[4..]),
    }))
    .await;
}

#[tokio::test]
async fn all_three_sources_share_one_surface() {
    let source_text = include_str!("../src/frame_source.rs");
    let trait_body = source_text
        .split_once("pub trait FrameSource {")
        .expect("FrameSource declaration")
        .1
        .split_once("}\n")
        .expect("FrameSource body")
        .0;
    assert_eq!(trait_body.matches("fn ").count(), 3);
    assert!(trait_body.contains("fn send"));
    assert!(trait_body.contains("fn recv"));
    assert!(trait_body.contains("fn transport"));
    assert!(!trait_body.contains("connect"));
    assert!(!trait_body.contains("sent_"));

    let history = ServerMessage::AttachHistory {
        text: "surface".into(),
        truncated: false,
        dropped_bytes: 0,
        total_bytes: 7,
    };
    let mut scripted_direct = ScriptedFrameSource::new(Transport::Direct);
    scripted_direct.queue(history.clone());
    drive_source(&mut scripted_direct, Transport::Direct, &history).await;
    assert!(scripted_direct.last_client_message().is_some());

    let mut scripted_proxy = ScriptedFrameSource::new(Transport::Proxy);
    scripted_proxy.queue(history.clone());
    drive_source(&mut scripted_proxy, Transport::Proxy, &history).await;

    let (client, mut host) = UnixStream::pair().expect("direct socket pair");
    let direct_history = history.clone();
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "surface-epoch".into(),
            },
        )
        .await
        .expect("welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("attach");
        write_message_async(&mut host, &direct_history)
            .await
            .expect("history");
        assert!(matches!(
            read_message_async(&mut host, MAX_FRAME_SIZE).await,
            Ok(ClientMessage::SetViewport { rows: 25, cols: 81 })
        ));
    });
    let mut direct = UnixSocketFrameSource::connect_stream(
        client,
        &native_locator(std::path::Path::new("socket-pair"), "surface-epoch"),
        "token",
        80,
        24,
    )
    .await
    .expect("direct source");
    drive_source(&mut direct, Transport::Direct, &history).await;
    host_task.await.expect("direct host");

    let mock = mock_daemon::MockDaemon::start("local-token").await;
    let daemon = LiveDaemon::connect(mock.url(), "local-token")
        .await
        .expect("connect daemon");
    let mut proxy = ProxyFrameSource::attach(daemon, "terminal-surface")
        .await
        .expect("proxy source");
    mock.send_event_and_wait(json!({
        "type": "terminal_attach_history",
        "terminal_id": "terminal-surface",
        "attachment_id": proxy.attachment_id(),
        "text": "surface",
        "truncated": false,
        "dropped_bytes": 0,
        "total_bytes": 7,
    }))
    .await;
    drive_source(&mut proxy, Transport::Proxy, &history).await;
    mock.shutdown().await;
}

async fn drive_source<S: FrameSource>(
    source: &mut S,
    transport: Transport,
    expected: &ServerMessage,
) {
    assert_eq!(source.transport(), transport);
    assert_eq!(source.recv().await.expect("source receive"), *expected);
    source
        .send(&ClientMessage::SetViewport { rows: 25, cols: 81 })
        .await
        .expect("source send");
}

#[tokio::test]
async fn live_roster_prefers_direct_then_falls_back_once() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": "terminal-direct-candidate",
                "backend": "native",
                "state": "live",
                "attach": {
                    "backend": "native",
                    "frame_host_epoch": "host-epoch",
                    "host_socket": "/missing/test-owned-gterm-frames.sock",
                    "host_terminal_id": "host-terminal-direct",
                    "socket_path": null,
                    "pane_id": null,
                    "server_pid": null,
                    "server_start_time": null
                }
            }],
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

    let attaches = terminal_attach_requests(&mock);
    assert_eq!(attaches.len(), 2, "direct failure must have one fallback");
    assert_eq!(attaches[0]["frame_delivery"], json!("direct"));
    assert_eq!(attaches[0]["encoding"], json!("semantic_frame"));
    assert_eq!(attaches[1]["frame_delivery"], json!("proxy"));
    assert_eq!(attaches[1]["encoding"], json!("semantic_frame"));
    let detaches: Vec<_> = mock
        .requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!("terminal_detach")))
        .collect();
    assert_eq!(detaches.len(), 1);
    assert_eq!(detaches[0]["attachment_id"], json!("attachment-1"));
    let pane_id = workspace
        .pane_for_terminal("terminal-direct-candidate")
        .expect("direct candidate pane");
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
    assert_eq!(workspace.pane(pane_id).attachment_id(), "attachment-2");
    mock.shutdown().await;
}

/// A real tmux row names its pane, not a host terminal.
///
/// `TerminalManager.attach_locator` leaves `host_terminal_id` null for tmux —
/// the pane locator is the identity, and the frame host ignores the host id
/// entirely once one is present. Requiring it here matched no real tmux row, so
/// the client never asked for direct and every tmux pane came through the proxy.
#[tokio::test]
async fn a_tmux_roster_row_still_asks_for_direct_first() {
    let mock = mock_daemon::MockDaemon::start("local-token").await;
    mock.use_unique_attachment_ids();
    mock.enqueue(
        "GET",
        "/api/terminals?",
        200,
        json!({
            "items": [{
                "terminal_id": "terminal-tmux-pane",
                "backend": "tmux",
                "state": "live",
                "attach": {
                    "backend": "tmux",
                    "frame_host_epoch": "host-epoch",
                    "host_socket": "/missing/test-owned-gterm-frames.sock",
                    "host_terminal_id": null,
                    "socket_path": "/missing/test-owned-tmux.sock",
                    "pane_id": "%9",
                    "server_pid": 4242,
                    "server_start_time": 1717171717
                }
            }],
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

    let attaches = terminal_attach_requests(&mock);
    assert_eq!(attaches.len(), 2, "direct failure must have one fallback");
    assert_eq!(attaches[0]["frame_delivery"], json!("direct"));
    assert_eq!(attaches[1]["frame_delivery"], json!("proxy"));
    let pane_id = workspace
        .pane_for_terminal("terminal-tmux-pane")
        .expect("tmux pane");
    assert_eq!(workspace.pane(pane_id).transport(), Some(Transport::Proxy));
    mock.shutdown().await;
}

fn terminal_attach_requests(mock: &mock_daemon::MockDaemon) -> Vec<serde_json::Value> {
    mock.requests()
        .into_iter()
        .filter_map(|request| request.body)
        .filter(|body| body.get("type") == Some(&json!("terminal_attach")))
        .collect()
}
