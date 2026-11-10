use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use std::io;
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::super::outage_grant::{GrantOutcome, CONTROL_UNREACHABLE};
use super::{apply_control_outcome, apply_outage_grant, send_live_input, DAEMON_DOWN_INPUT};
use crate::app::live_control::ControlOutcome;
use crate::app::{Backend, ControlState, Pane, PaneId, Workspace, HOST_GRANT_UNAVAILABLE};
use crate::daemon::{Generation, LiveDaemon};
use crate::frame_source::{
    AttachLocator, PaneFrameSource, ScriptedFrameSource, Transport, UnixSocketFrameSource,
};
use crate::ui::Chrome;

async fn host_grant_error_after_take(daemon_ready: bool, daemon_first: bool) -> String {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    workspace.daemon_ready = daemon_ready;
    let pane_id = PaneId(1);
    let mut pane = cut_pane_with_keys().await;
    let attachment = pane.attachment_id().to_owned();
    pane.set_lease_generation(1);
    pane.control_request = Some(7);
    workspace.panes.insert(pane_id, pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    let take = || ControlOutcome {
        pane_id,
        request: 7,
        reply: Ok(json!({
            "granted": true,
            "host_input_granted": false,
            "lease_generation": 1,
        })),
    };
    if daemon_first {
        apply_control_outcome(&mut workspace, &mut chrome, &jobs, take());
    }
    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        pane_id,
        &attachment,
        1,
        Err(io::Error::from(io::ErrorKind::NotFound)),
    );
    if !daemon_first {
        apply_control_outcome(&mut workspace, &mut chrome, &jobs, take());
    }

    let pane = workspace.pane(pane_id);
    assert!(pane.is_observe(), "neither outcome grants host input");
    assert!(pane.has_take_back());
    assert!(!pane.direct_input());
    assert!(
        pane.pending_input.is_empty(),
        "undeliverable keys are dropped"
    );
    assert!(
        chrome
            .alert_log
            .iter()
            .any(|toast| toast.title.contains(crate::app::HOST_GRANT_UNAVAILABLE)),
        "the daemon's explicit host refusal remains visible"
    );
    pane.status_message()
        .expect("host refusal status")
        .to_owned()
}

#[tokio::test]
async fn host_grant_error_with_ready_daemon_after_take_keeps_host_refusal() {
    assert_eq!(
        host_grant_error_after_take(true, true).await,
        crate::app::HOST_GRANT_UNAVAILABLE
    );
}

#[tokio::test]
async fn host_grant_error_with_ready_daemon_before_take_keeps_host_refusal() {
    assert_eq!(
        host_grant_error_after_take(true, false).await,
        crate::app::HOST_GRANT_UNAVAILABLE
    );
}

#[tokio::test]
async fn host_grant_error_with_down_daemon_after_take_reports_outage() {
    assert_eq!(
        host_grant_error_after_take(false, true).await,
        super::super::outage_grant::CONTROL_UNREACHABLE
    );
}

#[tokio::test]
async fn host_grant_error_with_down_daemon_before_take_keeps_explicit_refusal() {
    assert_eq!(
        host_grant_error_after_take(false, false).await,
        crate::app::HOST_GRANT_UNAVAILABLE
    );
}

async fn stale_host_grant_error(older_attachment: bool) -> Vec<ControlState> {
    let mut controls = Vec::new();
    for daemon_ready in [false, true] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
            .expect("unconnected daemon handle");
        let mut workspace = Workspace::live(daemon);
        workspace.daemon_ready = daemon_ready;
        let pane_id = PaneId(1);
        let mut pane = cut_pane_with_keys().await;
        let current_attachment = pane.attachment_id().to_owned();
        pane.set_lease_generation(2);
        pane.control = ControlState::Held;
        assert!(pane.apply_host_grant(Some(true)));
        let attachment = if older_attachment {
            format!("{current_attachment}-old")
        } else {
            current_attachment
        };
        let lease = if older_attachment { 2 } else { 1 };
        workspace.panes.insert(pane_id, pane);
        let jobs = LoopJobs::new(&workspace);
        let mut chrome = Chrome::dark();

        apply_outage_grant(
            &mut workspace,
            &mut chrome,
            &jobs,
            pane_id,
            &attachment,
            lease,
            Err(io::Error::from(io::ErrorKind::NotFound)),
        );

        let pane = workspace.pane(pane_id);
        assert!(
            pane.direct_input(),
            "an old error cannot revoke a new grant"
        );
        assert!(!pane.has_take_back());
        assert_eq!(
            pane.pending_input.len(),
            3,
            "newer queued input is retained"
        );
        assert_eq!(pane.status_message(), None);
        assert!(chrome.alert_log.is_empty());
        controls.push(pane.control);
    }
    controls
}

#[tokio::test]
async fn host_grant_error_for_stale_attachment_preserves_current_grant() {
    assert_eq!(
        stale_host_grant_error(true).await,
        vec![ControlState::Held, ControlState::Held]
    );
}

#[tokio::test]
async fn host_grant_error_for_stale_lease_preserves_current_grant() {
    assert_eq!(
        stale_host_grant_error(false).await,
        vec![ControlState::Held, ControlState::Held]
    );
}

#[tokio::test]
async fn a_roster_refresh_requests_control_for_new_focus_or_a_pending_attachment() {
    for pending_attachment in [false, true] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
            .expect("unconnected daemon handle");
        let mut workspace = Workspace::live(daemon);
        let pane_id = PaneId(1);
        let mut pane = Pane::new(pane_id, "terminal-1", Backend::Native, "epoch-1");
        if pending_attachment {
            pane.defer_attach("waiting", "host not ready", tokio::time::Instant::now());
            workspace.focus = Some(pane_id);
        }
        workspace.panes.insert(pane_id, pane);
        let mut chrome = Chrome::dark();
        chrome.open_pane(pane_id, "terminal-1");

        super::super::request_focused_control(&mut workspace, &chrome);

        let requested = workspace.pending_control.expect("focus asks for control");
        assert_eq!(requested.pane_id, pane_id);
        assert!(!requested.takeover, "roster focus never displaces a peer");
    }
}

/// A roster answer is a read: it must not take control back from an
/// already-focused pane that recovery left observing.
#[tokio::test]
async fn a_roster_refresh_keeps_an_existing_live_focus_observing() {
    for transport in [Transport::Direct, Transport::Proxy] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
            .expect("unconnected daemon handle");
        let mut workspace = Workspace::live(daemon);
        let pane_id = PaneId(1);
        let mut pane = Pane::new(pane_id, "terminal-1", Backend::Native, "epoch-1");
        pane.install_frame_source(PaneFrameSource::Scripted(ScriptedFrameSource::new(
            transport,
        )));
        assert!(pane.is_live());
        assert_eq!(pane.control, ControlState::Observe);
        workspace.panes.insert(pane_id, pane);
        workspace.focus = Some(pane_id);
        let mut chrome = Chrome::dark();
        chrome.open_pane(pane_id, "terminal-1");

        super::super::request_focused_control(&mut workspace, &chrome);

        assert!(
            workspace.pending_control.is_none(),
            "an unchanged live focus must keep observing after a roster refresh: {transport:?}"
        );
    }
}

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

/// The panes whose keys go through the daemon: a tmux pane, and a native pane
/// whose frames come over the daemon's proxy.
fn daemon_bound_panes() -> [(&'static str, Pane); 2] {
    let tmux = Pane::new(PaneId(1), "terminal-1", Backend::Tmux, "epoch-1");
    let mut native_proxy = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    native_proxy.install_attachment(
        "attachment-1".to_owned(),
        Transport::Proxy,
        Generation(0),
        0,
        PaneFrameSource::Scripted(ScriptedFrameSource::new(Transport::Proxy)),
    );
    [("tmux", tmux), ("native proxy", native_proxy)]
}

/// A key typed into a daemon-bound pane while the daemon is away is dropped.
/// The pane's status note is drawn only for a pane with no frame, so a pane
/// that was live before the outage shows the drop only through the toast.
#[tokio::test]
async fn a_key_typed_into_a_daemon_bound_pane_during_an_outage_is_reported_not_sent() {
    for paste in [false, true] {
        for (kind, pane) in daemon_bound_panes() {
            let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
                .expect("unconnected daemon handle");
            let mut workspace = Workspace::live(daemon);
            workspace.daemon_ready = false;
            workspace.panes.insert(PaneId(1), pane);
            let mut jobs = LoopJobs::new(&workspace);
            let mut chrome = Chrome::dark();

            send_live_input(
                &mut workspace,
                &mut chrome,
                &mut jobs,
                PaneId(1),
                b"x",
                paste,
            )
            .expect("a refused key is not an error");

            let case = format!("{kind} paste={paste}");
            let pane = workspace.pane(PaneId(1));
            assert_eq!(
                pane.status_message(),
                Some(DAEMON_DOWN_INPUT),
                "{case}: the key took the outage refusal"
            );
            assert_eq!(pane.client_write_seq, 0, "{case}: no write was issued");
            assert!(pane.writer.is_none(), "{case}: no writer took the key");
            assert!(pane.pending_input.is_empty(), "{case}: the key is not held");
            let titles: Vec<&str> = chrome
                .alert_log
                .iter()
                .map(|toast| toast.title.as_str())
                .collect();
            assert_eq!(
                titles,
                [DAEMON_DOWN_INPUT],
                "{case}: the dropped key is shown"
            );
        }
    }
}
