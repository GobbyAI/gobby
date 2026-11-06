use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::super::outage_grant::GrantOutcome;
use super::{apply_control_outcome, apply_outage_grant, move_live_focus, release_live_control};
use crate::app::live_control::ControlOutcome;
use crate::app::{Backend, ControlState, Pane, PaneId, Workspace};
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
    // The keys were typed into the focused pane, so its grant still flushes them.
    workspace.focus = Some(PaneId(1));
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

/// A native direct pane that already holds the host's grant and the daemon
/// lease, the state a granted take leaves behind.
async fn held_direct_pane() -> Pane {
    let mut pane = cut_pane_with_keys().await;
    pane.control = ControlState::Held;
    pane.host_input_granted = true;
    pane
}

#[tokio::test]
async fn a_refused_host_ask_keeps_the_lease_a_pane_already_holds() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let pane = held_direct_pane().await;
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
        Ok(GrantOutcome::Refused("not_granted".into())),
        Vec::new(),
    );

    assert!(
        workspace.pane(PaneId(1)).is_held(),
        "a refused redundant host ask must not drop the lease the pane holds, or its release is never sent"
    );
}

#[tokio::test]
async fn a_failed_host_ask_keeps_the_lease_a_pane_already_holds() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let pane = held_direct_pane().await;
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
        Err(std::io::Error::other("connection reset")),
        Vec::new(),
    );

    assert!(
        workspace.pane(PaneId(1)).is_held(),
        "a failed redundant host ask must not drop the lease the pane holds, or its release is never sent"
    );
}

#[tokio::test]
async fn releasing_a_pane_that_only_waits_on_a_grant_drops_its_queued_keys() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    workspace
        .panes
        .insert(PaneId(1), cut_pane_with_keys().await);

    release_live_control(&mut workspace, PaneId(1));

    assert!(
        workspace.pane(PaneId(1)).pending_input.is_empty(),
        "keys queued for a grant the pane gave up must not run once it wins control later"
    );
}

#[tokio::test]
async fn a_late_grant_does_not_restore_control_to_a_pane_that_lost_focus() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    workspace.panes.insert(PaneId(1), pane);
    workspace.focus = Some(PaneId(2));
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

    assert!(
        !workspace.pane(PaneId(1)).is_held(),
        "a grant that arrives after focus left must not put the pane back in control"
    );
    assert!(
        chrome.alert_log.is_empty(),
        "keys queued for a pane that lost focus are dropped, not flushed to it"
    );
    assert!(
        workspace.pane(PaneId(1)).host_input_granted,
        "the host grant is real, so the pane records it for a later peer ask to revoke"
    );
}

/// A focused pane with keys typed into it, then focus moved away and back
/// before the grant those keys asked for answered. Focus leaving releases the
/// pane, which drops its queued keys.
async fn refocused_pane_awaiting_a_grant() -> (Workspace<LiveDaemon>, String) {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    workspace.panes.insert(PaneId(1), pane);
    workspace.panes.insert(
        PaneId(2),
        Pane::new(PaneId(2), "terminal-2", Backend::Native, "epoch-2"),
    );
    workspace.focus = Some(PaneId(1));
    assert!(move_live_focus(&mut workspace, PaneId(2)));
    assert!(move_live_focus(&mut workspace, PaneId(1)));
    (workspace, attachment)
}

#[tokio::test]
async fn a_stale_grant_after_refocus_does_not_give_an_unanswered_pane_control() {
    let (mut workspace, attachment) = refocused_pane_awaiting_a_grant().await;
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

    let pane = workspace.pane(PaneId(1));
    assert!(
        !pane.is_held(),
        "a grant answering keys the pane let go of must not restore control"
    );
    assert!(
        pane.host_input_granted,
        "the host grant is real, so the pane records it for a later peer ask to revoke"
    );
    assert!(chrome.alert_log.is_empty());
}

#[tokio::test]
async fn a_grant_after_refocus_answers_keys_typed_since() {
    let (mut workspace, attachment) = refocused_pane_awaiting_a_grant().await;
    assert!(workspace
        .panes
        .get_mut(&PaneId(1))
        .expect("pane exists")
        .queue_input(b"d", false));
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

    assert!(
        workspace.pane(PaneId(1)).is_held(),
        "keys typed after refocus are the ones this grant answers"
    );
    assert_eq!(
        chrome.alert_log.len(),
        1,
        "the key typed since refocus is flushed, not dropped"
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
