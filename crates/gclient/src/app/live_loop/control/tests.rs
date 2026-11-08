use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::super::outage_grant::{GrantOutcome, CONTROL_UNREACHABLE};
use super::{apply_control_outcome, apply_outage_grant};
use crate::app::live_control::ControlOutcome;
use crate::app::{Backend, ControlState, Pane, PaneId, Workspace, HOST_GRANT_UNAVAILABLE};
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
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        0,
        Ok(GrantOutcome::Granted),
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

/// A host grant asked at lease 0 is refused after the daemon's take granted
/// the pane at lease 1. That refusal describes the host before the take, so it
/// must not revoke the grant the take gave (#25790).
#[tokio::test]
async fn a_refused_host_grant_from_before_the_daemon_grant_leaves_it_standing() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let mut pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    pane.set_lease_generation(1);
    pane.control = ControlState::Held;
    assert!(pane.apply_host_grant(Some(true)));
    workspace.panes.insert(PaneId(1), pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        0,
        Ok(GrantOutcome::Refused("input_not_granted".into())),
    );

    let pane = workspace.pane(PaneId(1));
    assert_eq!(
        pane.control,
        ControlState::Held,
        "the daemon's grant stands"
    );
    assert!(
        pane.direct_input(),
        "the pane keeps typing on its host socket"
    );
}

/// A refusal to a grant asked under the pane's current lease is the host's
/// answer to that lease, so the pane gives the grant back (#25790).
#[tokio::test]
async fn a_refused_host_grant_under_the_current_lease_gives_the_pane_back() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let mut pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    pane.set_lease_generation(1);
    pane.control = ControlState::Held;
    assert!(pane.apply_host_grant(Some(true)));
    workspace.panes.insert(PaneId(1), pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        1,
        Ok(GrantOutcome::Refused("input_not_granted".into())),
    );

    let pane = workspace.pane(PaneId(1));
    assert_eq!(pane.control, ControlState::Observe);
    assert!(pane.take_back, "the pane offers take-back");
    assert!(!pane.direct_input());
}

/// Runs `grant` against a direct pane holding three queued keys under lease 0,
/// and returns the toast titles it raised and the keys still queued.
async fn outage_grant_answer(grant: std::io::Result<GrantOutcome>) -> (Vec<String>, usize) {
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
        0,
        grant,
    );

    let titles = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.clone())
        .collect();
    let queued = workspace
        .panes
        .get_mut(&PaneId(1))
        .expect("pane")
        .take_pending_input()
        .len();
    (titles, queued)
}

/// A key whose grant the host's control socket could not be asked for is not
/// sent, and the pane's status alone is never drawn on a live pane, so a toast
/// says so.
#[tokio::test]
async fn an_unreachable_host_grant_says_the_queued_keys_were_not_sent() {
    let (titles, queued) =
        outage_grant_answer(Err(std::io::Error::other("no control socket"))).await;

    assert_eq!(queued, 0, "the unreachable grant drops the queued keys");
    assert_eq!(
        titles,
        [CONTROL_UNREACHABLE],
        "the dropped keys are reported"
    );
}

/// The host refusing the grant drops the queued keys too, so the refusal is
/// reported the way a take without a host grant is.
#[tokio::test]
async fn a_refused_host_grant_says_the_queued_keys_were_not_sent() {
    let (titles, queued) =
        outage_grant_answer(Ok(GrantOutcome::Refused("input_not_granted".into()))).await;

    assert_eq!(queued, 0, "the refused grant drops the queued keys");
    assert_eq!(titles, [HOST_GRANT_UNAVAILABLE], "the refusal is reported");
}
