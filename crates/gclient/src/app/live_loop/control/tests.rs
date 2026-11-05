use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::flush_attached_input;
use crate::app::{Backend, Pane, PaneId, Workspace};
use crate::daemon::{Generation, LiveDaemon};
use crate::frame_source::{AttachLocator, PaneFrameSource, Transport, UnixSocketFrameSource};
use crate::ui::Chrome;

/// A direct frame source whose socket is cut after the handshake, so every
/// host write through it fails the way a write to a departed host does.
async fn cut_direct_source(epoch: &str) -> PaneFrameSource {
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
        frame_socket_path: String::new(),
        pane: None,
    };
    let mut source = UnixSocketFrameSource::connect_stream(client, &locator, "token", 80, 24)
        .await
        .expect("direct source");
    drop(host_task.await.expect("host handshake"));
    source.cancel_reader_task();
    PaneFrameSource::Direct(source)
}

/// A native direct pane attached over a cut frame socket, with three keys
/// queued for it.
async fn cut_pane_with_keys() -> Pane {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.install_attachment(
        "attachment-1".to_owned(),
        Transport::Direct,
        Generation(0),
        0,
        cut_direct_source("epoch-1").await,
    );
    for key in [&b"a"[..], &b"b"[..], &b"c"[..]] {
        assert!(pane.queue_input(key, false));
    }
    pane
}

#[tokio::test]
async fn a_failed_flush_reports_every_key_it_could_not_write() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    workspace
        .panes
        .insert(PaneId(1), cut_pane_with_keys().await);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    flush_attached_input(&mut workspace, &mut chrome, &jobs);

    assert_eq!(
        chrome.alert_log.len(),
        3,
        "each key whose write failed is reported, not only the first"
    );
}
