use super::*;
use crate::app::{Backend, Pane};
use crate::daemon::LiveDaemon;
use crate::frame_source::{AttachLocator, UnixSocketFrameSource};
use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use tokio::net::UnixStream;

async fn retired_direct_attach() -> (Workspace<LiveDaemon>, super::super::DirectAttach) {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let generation = workspace.daemon.generation();
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.begin_attaching("request-1".into(), Transport::Direct, generation);
    pane.tombstones.insert("retired-attachment".into());
    workspace.panes.insert(pane.id, pane);
    let locator = AttachLocator {
        backend: "native".into(),
        frame_host_epoch: "epoch-1".into(),
        host_terminal_id: "terminal-1".into(),
        frame_socket_path: String::new(),
        pane: None,
    };
    let (client, mut host) = UnixStream::pair().expect("socket pair");
    let host_task = tokio::spawn(async move {
        let _: ClientMessage = read_message_async(&mut host, MAX_FRAME_SIZE)
            .await
            .expect("hello");
        write_message_async(
            &mut host,
            &ServerMessage::Welcome {
                host_epoch: "epoch-1".into(),
            },
        )
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
    let source = UnixSocketFrameSource::connect_stream(client, &locator, "token", 80, 24)
        .await
        .expect("direct source");
    drop(host_task.await.expect("host handshake"));
    (
        workspace,
        (json!({}), "retired-attachment".into(), locator, source),
    )
}

#[tokio::test]
async fn a_direct_recovery_with_a_retired_attachment_is_not_current() {
    let (workspace, outcome) = retired_direct_attach().await;
    let step = RecoveryStep::DirectAttached {
        terminal_id: "terminal-1".into(),
        request_id: "request-1".into(),
        outcome: Ok(outcome),
    };
    assert!(!workspace.step_is_current(PaneId(1), workspace.daemon.generation(), &step));
}

#[tokio::test]
async fn finishing_a_direct_attach_refuses_a_retired_attachment() {
    let (mut workspace, outcome) = retired_direct_attach().await;
    let generation = workspace.daemon.generation();
    assert!(workspace.finish_direct_attach(PaneId(1), generation, Ok(outcome)));
    let pane = &workspace.panes[&PaneId(1)];
    assert_ne!(pane.attachment_id(), "retired-attachment");
    assert_eq!(
        pane.status_message(),
        Some("attachment_reused: daemon reused a tombstoned attachment id")
    );
    assert!(!pane.live);
}

#[tokio::test]
async fn a_retired_direct_result_finishes_only_its_own_flight() {
    for request_id in ["request-1", "superseded-request"] {
        let (mut workspace, outcome) = retired_direct_attach().await;
        workspace
            .panes
            .get_mut(&PaneId(1))
            .expect("pane")
            .fallback_in_flight = true;
        let generation = workspace.daemon.generation();
        let next = workspace
            .apply_frame_recovery(Recovery {
                pane_id: PaneId(1),
                generation,
                started: Instant::now(),
                step: RecoveryStep::DirectAttached {
                    terminal_id: "terminal-1".into(),
                    request_id: request_id.into(),
                    outcome: Ok(outcome),
                },
            })
            .expect("apply recovery");
        assert!(next.is_none());
        let pane = &workspace.panes[&PaneId(1)];
        assert_ne!(pane.attachment_id(), "retired-attachment");
        assert_eq!(pane.fallback_in_flight, request_id != "request-1");
        if request_id == "request-1" {
            assert_eq!(
                pane.status_message(),
                Some("attachment_reused: daemon reused a tombstoned attachment id")
            );
        }
    }
}
