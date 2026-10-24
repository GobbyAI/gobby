use std::io::Write;
use std::sync::{Arc, Mutex};

use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use tokio::net::UnixStream;
use tokio::time::Instant;

use super::{HostRecovery, HostRecoveryOutcome};
use crate::app::{Backend, Pane, PaneId, Workspace};
use crate::daemon::LiveDaemon;
use crate::frame_source::{AttachLocator, UnixSocketFrameSource};

#[derive(Clone)]
struct LogWriter(Arc<Mutex<Vec<u8>>>);

impl Write for LogWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0
            .lock()
            .expect("captured logs")
            .extend_from_slice(bytes);
        Ok(bytes.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

/// A live direct source from a host that answers the handshake on `epoch`.
async fn restored_source(epoch: &str) -> UnixSocketFrameSource {
    restored_source_with_host(epoch, "").await.0
}

async fn restored_source_with_host(
    epoch: &str,
    frame_socket: &str,
) -> (UnixSocketFrameSource, UnixStream) {
    let (client, mut host) = UnixStream::pair().expect("socket pair");
    let host_epoch = epoch.to_string();
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(&mut host, &ServerMessage::Welcome { host_epoch })
            .await
            .expect("welcome");
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("attach");
        write_message_async(
            &mut host,
            &ServerMessage::Attached {
                created: false,
                host_terminal_id: "terminal-1".into(),
                host_attachment_id: 1,
            },
        )
        .await
        .expect("attached");
        host
    });
    let locator = AttachLocator {
        backend: "native".into(),
        frame_host_epoch: epoch.into(),
        host_terminal_id: "terminal-1".into(),
        frame_socket_path: frame_socket.into(),
        pane: None,
    };
    let source = UnixSocketFrameSource::connect_stream(client, &locator, "token", 80, 24)
        .await
        .expect("restored source");
    (source, host_task.await.expect("host handshake"))
}

#[tokio::test]
async fn a_restore_for_a_pane_that_moved_epochs_gives_up_as_stale() {
    let logs = stale_restore_logs(false).await;
    assert!(!logs.contains("frame_recovery_completed"), "{logs}");
}

#[tokio::test]
async fn a_restore_for_a_closed_pane_gives_up_as_stale() {
    let logs = stale_restore_logs(true).await;
    assert!(!logs.contains("frame_recovery_completed"), "{logs}");
}

#[tokio::test]
async fn a_restored_stream_keeps_the_lease_but_requires_a_fresh_host_grant() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").unwrap();
    let mut workspace = Workspace::live(daemon);
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.restore_host_control();
    workspace.panes.insert(pane.id, pane);
    let generation = workspace.daemon.generation();
    let recovery = workspace.apply_host_recovery(HostRecovery {
        pane_id: PaneId(1),
        terminal_id: "terminal-1".into(),
        generation,
        epoch: "epoch-1".into(),
        started: Instant::now(),
        outcome: HostRecoveryOutcome::Restored(restored_source("epoch-1").await),
    });
    assert!(recovery.is_none());
    let pane = &workspace.panes[&PaneId(1)];
    assert!(pane.is_held());
    assert!(
        !pane.host_input_granted,
        "a new stream has no inherited host authority"
    );
    assert_eq!(pane.host_attachment_id(), Some(1));
}

#[tokio::test]
async fn input_on_a_held_stream_without_a_host_grant_queues_and_requests_control() {
    use crate::app::live_loop::{control::send_live_input, jobs::LoopJobs};
    use crate::ui::Chrome;

    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").unwrap();
    let mut workspace = Workspace::live(daemon);
    workspace.daemon_ready = true;
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.install_frame_source(crate::frame_source::PaneFrameSource::Direct(
        restored_source("epoch-1").await,
    ));
    pane.live = true;
    pane.restore_host_control();
    pane.host_input_granted = false;
    workspace.panes.insert(pane.id, pane);
    let mut jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    send_live_input(
        &mut workspace,
        &mut chrome,
        &mut jobs,
        PaneId(1),
        b"queued",
        false,
    )
    .expect("queue input until host grant");
    let pane = &workspace.panes[&PaneId(1)];
    assert_eq!(pane.pending_input.len(), 1);
    assert_eq!(pane.pending_input[0].parts(), (b"queued".as_slice(), false));
    assert!(workspace.awaiting_control(PaneId(1)));
    assert!(chrome.alert_log.is_empty());
}

#[tokio::test]
async fn recovered_held_stream_regrants_and_flushes_input_during_daemon_outage() {
    use crate::app::live_loop::{
        control::send_live_input, jobs::LoopJobs, jobs_apply::apply_job_outcome,
    };
    use crate::ui::Chrome;
    use std::os::unix::fs::PermissionsExt;
    use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
    use tokio::time::{timeout, Duration};

    let dir = tempfile::tempdir().unwrap();
    let frame_socket = dir.path().join("gterm-frames.sock");
    let control_socket = dir.path().join("gterm-control.sock");
    let token = dir.path().join("gterm-control.token");
    std::fs::write(&token, "test-token").unwrap();
    std::fs::set_permissions(&token, std::fs::Permissions::from_mode(0o600)).unwrap();
    let listener = tokio::net::UnixListener::bind(&control_socket).unwrap();
    std::fs::set_permissions(&control_socket, std::fs::Permissions::from_mode(0o600)).unwrap();
    let control = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut stream = BufReader::new(stream);
        let mut line = String::new();
        stream.read_line(&mut line).await.unwrap();
        let hello: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(hello["control_token"], "test-token");
        stream
            .get_mut()
            .write_all(b"{\"ok\":true}\n")
            .await
            .unwrap();
        line.clear();
        stream.read_line(&mut line).await.unwrap();
        let request: serde_json::Value = serde_json::from_str(&line).unwrap();
        assert_eq!(request["method"], "grant_input");
        assert_eq!(request["host_terminal_id"], "terminal-1");
        assert_eq!(request["host_attachment_id"], 1);
        stream
            .get_mut()
            .write_all(b"{\"ok\":true,\"granted\":true}\n")
            .await
            .unwrap();
    });
    let (source, mut host) =
        restored_source_with_host("epoch-1", frame_socket.to_str().unwrap()).await;
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").unwrap();
    let mut workspace = Workspace::live(daemon);
    workspace.daemon_ready = false;
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.install_frame_source(crate::frame_source::PaneFrameSource::Direct(source));
    pane.remember_host_locator(&AttachLocator {
        backend: "native".into(),
        frame_host_epoch: "epoch-1".into(),
        host_terminal_id: "terminal-1".into(),
        frame_socket_path: frame_socket.to_string_lossy().into_owned(),
        pane: None,
    });
    pane.live = true;
    pane.restore_host_control();
    pane.host_input_granted = false;
    workspace.panes.insert(pane.id, pane);
    let mut jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    send_live_input(
        &mut workspace,
        &mut chrome,
        &mut jobs,
        PaneId(1),
        b"queued",
        false,
    )
    .unwrap();
    let outcome = timeout(Duration::from_secs(2), jobs.rx.recv())
        .await
        .expect("recovered Held stream must dispatch HostGrant during outage")
        .expect("grant outcome");
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome);
    timeout(Duration::from_secs(2), control)
        .await
        .unwrap()
        .unwrap();
    let input = timeout(Duration::from_secs(2), async {
        loop {
            let message: ClientMessage =
                read_message_async(&mut host, MAX_FRAME_SIZE).await.unwrap();
            if let ClientMessage::Input { .. } = message {
                break message;
            }
        }
    })
    .await
    .expect("queued input must reach the recovered host stream");
    assert!(matches!(input, ClientMessage::Input { data } if data == b"queued"));
    assert!(workspace.panes[&PaneId(1)].pending_input.is_empty());
    assert!(chrome.alert_log.is_empty());
}

fn apply_recovery_and_capture(
    workspace: &mut Workspace<LiveDaemon>,
    recovery: HostRecovery,
) -> String {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let writer = LogWriter(Arc::clone(&captured));
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .without_time()
        .with_target(false)
        .with_writer(move || writer.clone())
        .finish();
    let _guard = tracing::subscriber::set_default(subscriber);
    assert!(workspace.apply_host_recovery(recovery).is_none());
    let bytes = captured.lock().expect("captured logs").clone();
    String::from_utf8(bytes).expect("UTF-8 logs")
}

#[test]
fn closed_host_attempts_record_cancelled_and_failed_giveups() {
    for (outcome, error_class) in [
        (HostRecoveryOutcome::Cancelled, "cancelled"),
        (HostRecoveryOutcome::Failed, "stale"),
    ] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
            .expect("unconnected daemon handle");
        let mut workspace = Workspace::live(daemon);
        let logs = apply_recovery_and_capture(
            &mut workspace,
            HostRecovery {
                pane_id: PaneId(1),
                terminal_id: "original-terminal".into(),
                generation: crate::daemon::Generation(7),
                epoch: "epoch-1".into(),
                started: Instant::now(),
                outcome,
            },
        );
        let gave_up: Vec<_> = logs
            .lines()
            .filter(|line| line.contains("frame_recovery_gave_up"))
            .collect();
        assert_eq!(gave_up.len(), 1, "{logs}");
        for field in [
            "pane_id=1",
            "terminal_id=original-terminal",
            "generation=7",
            "stage=\"host_reconnect\"",
            "elapsed_ms=",
            error_class,
        ] {
            assert!(gave_up[0].contains(field), "{field} missing from {logs}");
        }
        assert!(!logs.contains("frame_recovery_completed"), "{logs}");
    }
}

async fn stale_restore_logs(closed: bool) -> String {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    // The reconnect began on epoch-1; the pane now expects epoch-2.
    let pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-2");
    workspace.panes.insert(pane.id, pane);
    workspace.host_recovering.insert(PaneId(1));
    if closed {
        workspace.panes.remove(&PaneId(1));
    } else {
        // A newer recovery owns the pane's changed epoch and flight slot.
        workspace
            .panes
            .get_mut(&PaneId(1))
            .expect("pane")
            .fallback_in_flight = true;
    }

    let generation = workspace.daemon.generation();
    let logs = apply_recovery_and_capture(
        &mut workspace,
        HostRecovery {
            pane_id: PaneId(1),
            terminal_id: "terminal-1".into(),
            generation,
            epoch: "epoch-1".into(),
            started: Instant::now(),
            outcome: HostRecoveryOutcome::Restored(restored_source("epoch-1").await),
        },
    );

    if !closed {
        assert!(workspace.panes[&PaneId(1)].fallback_in_flight);
        assert!(workspace.host_recovering.contains(&PaneId(1)));
    }

    let gave_up: Vec<&str> = logs
        .lines()
        .filter(|line| line.contains("frame_recovery_gave_up"))
        .collect();
    assert_eq!(gave_up.len(), 1, "{logs}");
    for field in [
        "pane_id=1",
        "terminal_id=terminal-1",
        "generation=0",
        "stage=\"host_reconnect\"",
        "error_class=\"stale\"",
        "elapsed_ms=",
    ] {
        assert!(
            gave_up[0].contains(field),
            "{field} missing from {}",
            gave_up[0]
        );
    }
    logs
}
