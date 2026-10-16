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
        frame_socket_path: String::new(),
        pane: None,
    };
    let source = UnixSocketFrameSource::connect_stream(client, &locator, "token", 80, 24)
        .await
        .expect("restored source");
    drop(host_task.await.expect("host handshake"));
    source
}

#[tokio::test]
async fn a_restore_for_a_pane_that_moved_epochs_gives_up_as_stale() {
    let captured = Arc::new(Mutex::new(Vec::new()));
    let writer = LogWriter(Arc::clone(&captured));
    let subscriber = tracing_subscriber::fmt()
        .with_ansi(false)
        .without_time()
        .with_target(false)
        .with_writer(move || writer.clone())
        .finish();
    let _guard = tracing::subscriber::set_default(subscriber);
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    // The reconnect began on epoch-1; the pane now expects epoch-2.
    let pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-2");
    workspace.panes.insert(pane.id, pane);
    workspace.host_recovering.insert(PaneId(1));

    let next = workspace.apply_host_recovery(HostRecovery {
        pane_id: PaneId(1),
        terminal_id: "terminal-1".into(),
        epoch: "epoch-1".into(),
        started: Instant::now(),
        outcome: HostRecoveryOutcome::Restored(restored_source("epoch-1").await),
    });

    assert!(next.is_none(), "a stale restore starts no daemon recovery");
    let logs =
        String::from_utf8(captured.lock().expect("captured logs").clone()).expect("UTF-8 logs");
    let gave_up: Vec<&str> = logs
        .lines()
        .filter(|line| line.contains("frame_recovery_gave_up"))
        .collect();
    assert_eq!(gave_up.len(), 1, "{logs}");
    for field in [
        "pane_id=1",
        "terminal_id=terminal-1",
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
    assert!(!logs.contains("frame_recovery_completed"), "{logs}");
}
