use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use serde_json::json;
use std::io;
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::super::outage_grant::GrantOutcome;
use super::super::outage_grant::CONTROL_UNREACHABLE;
use super::{
    apply_control_outcome, apply_outage_grant, move_live_focus, release_live_control,
    start_live_control,
};
use crate::app::live_control::ControlOutcome;
use crate::app::{Backend, ControlState, Pane, PaneId, Workspace};
use crate::daemon::LiveDaemon;
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
        0,
        Ok(GrantOutcome::Granted),
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

/// Run a repeated host ask on a held direct pane, then two loop passes of
/// `start_live_control`. Returns how many lease releases the passes queued.
async fn downgrade_held_pane(
    daemon_ready: bool,
    grant: io::Result<GrantOutcome>,
) -> (Workspace<LiveDaemon>, usize) {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    workspace.daemon_ready = daemon_ready;
    let pane = held_direct_pane().await;
    let attachment = pane.attachment_id().to_owned();
    workspace.panes.insert(PaneId(1), pane);
    let jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    let (control_tx, _control_rx) = tokio::sync::mpsc::unbounded_channel();

    let lease = workspace.pane(PaneId(1)).lease_generation();
    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        lease,
        grant,
    );

    let mut releases = 0;
    let mut last: Option<tokio::sync::watch::Receiver<bool>> = None;
    for _ in 0..2 {
        start_live_control(&mut workspace, &jobs, &control_tx);
        let current = workspace.pane(PaneId(1)).release_done.clone();
        if let Some(current) = current {
            if !last
                .as_ref()
                .is_some_and(|seen| seen.same_channel(&current))
            {
                releases += 1;
            }
            last = Some(current);
        }
    }
    (workspace, releases)
}

#[tokio::test]
async fn a_refused_host_ask_on_a_held_pane_observes_and_releases_its_lease_once() {
    let (workspace, releases) =
        downgrade_held_pane(true, Ok(GrantOutcome::Refused("not_granted".into()))).await;
    let pane = workspace.pane(PaneId(1));

    assert_eq!(
        pane.control,
        ControlState::Observe,
        "a refused ask gives the pane back"
    );
    assert!(pane.has_take_back(), "the pane offers take-back");
    assert!(
        !pane.direct_input(),
        "the refused host grant is no longer typed through"
    );
    assert_eq!(
        releases, 1,
        "exactly one lease release follows the downgrade"
    );
    assert!(
        !pane.release_pending,
        "the release was sent, not left pending"
    );
}

#[tokio::test]
async fn a_failed_host_ask_on_a_held_pane_observes_and_releases_its_lease_once() {
    let (workspace, releases) =
        downgrade_held_pane(true, Err(std::io::Error::other("connection reset"))).await;
    let pane = workspace.pane(PaneId(1));

    assert_eq!(
        pane.control,
        ControlState::Observe,
        "a failed ask gives the pane back"
    );
    assert!(pane.has_take_back(), "the pane offers take-back");
    assert_eq!(
        releases, 1,
        "exactly one lease release follows the downgrade"
    );
    assert!(
        !pane.release_pending,
        "the release was sent, not left pending"
    );
}

#[tokio::test]
async fn a_failed_host_ask_with_the_daemon_away_still_observes_without_a_release() {
    let (workspace, releases) =
        downgrade_held_pane(false, Err(std::io::Error::other("connection reset"))).await;
    let pane = workspace.pane(PaneId(1));

    assert_eq!(
        pane.control,
        ControlState::Observe,
        "the pane lets go without waiting on a daemon it cannot reach"
    );
    assert!(pane.has_take_back(), "the pane offers take-back");
    assert_eq!(releases, 0, "no release is sent to a daemon that is away");
    assert!(
        !pane.release_pending,
        "no release waits on the daemon's return"
    );
    assert_eq!(pane.status_message.as_deref(), Some(CONTROL_UNREACHABLE));
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

    let lease = workspace.pane(PaneId(1)).lease_generation();
    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        lease,
        Ok(GrantOutcome::Granted),
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

    let lease = workspace.pane(PaneId(1)).lease_generation();
    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        lease,
        Ok(GrantOutcome::Granted),
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

    let lease = workspace.pane(PaneId(1)).lease_generation();
    apply_outage_grant(
        &mut workspace,
        &mut chrome,
        &jobs,
        PaneId(1),
        &attachment,
        lease,
        Ok(GrantOutcome::Granted),
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
