use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::super::outage_grant::GrantOutcome;
use super::{apply_control_outcome, apply_outage_grant};
use crate::app::live_control::ControlOutcome;
use crate::app::{Backend, Pane, PaneId, Workspace};
use crate::daemon::LiveDaemon;
use crate::frame_source::{AttachLocator, PaneFrameSource, UnixSocketFrameSource};
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

/// A native direct pane on a cut frame socket, with three keys typed into it.
async fn cut_pane_with_keys() -> Pane {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.install_frame_source(cut_direct_source("epoch-1").await);
    for key in [&b"a"[..], &b"b"[..], &b"c"[..]] {
        assert!(pane.queue_input(key, false));
    }
    pane
}

#[tokio::test]
async fn a_failed_grant_flush_reports_every_key_it_could_not_write() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    workspace.panes.insert(PaneId(1), pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        Ok(GrantOutcome::Granted),
        Vec::new(),
    );

    assert_eq!(
        chrome.alert_log.len(),
        3,
        "each key whose write failed is reported, not only the first"
    );
}

#[tokio::test]
async fn a_failed_control_flush_reports_every_key_it_could_not_write() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let mut pane = cut_pane_with_keys().await;
    pane.control_request = Some(7);
    workspace.panes.insert(PaneId(1), pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    let outcome = ControlOutcome {
        pane_id: PaneId(1),
        request: 7,
        reply: Ok(json!({
            "granted": true,
            "host_input_granted": true,
            "lease_generation": 0,
        })),
    };

    apply_control_outcome(&mut workspace, &mut chrome, &jobs, outcome);

    assert_eq!(
        chrome.alert_log.len(),
        3,
        "each key whose write failed is reported, not only the first"
    );
}
