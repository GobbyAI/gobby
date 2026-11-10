use gobby_terminal::protocol::{
    read_message_async, write_message_async, ClientMessage, ServerMessage, MAX_FRAME_SIZE,
};
use tokio::net::UnixStream;

use super::super::jobs::LoopJobs;
use super::{flush_attached_input, send_live_input, CLOSING_NATIVE_INPUT, DAEMON_DOWN_INPUT};
use crate::app::{Backend, Pane, PaneId, Workspace};
use crate::daemon::{Generation, LiveDaemon};
use crate::frame_source::{AttachLocator, PaneFrameSource, Transport, UnixSocketFrameSource};
use crate::ui::Chrome;

#[tokio::test]
async fn daemon_attachment_state_cannot_gate_a_present_native_host_stream() {
    for daemon_ready in [false, true] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").unwrap();
        let mut workspace = Workspace::live(daemon);
        workspace.daemon_ready = daemon_ready;
        let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
        pane.install_attachment(
            "old-metadata".into(),
            Transport::Direct,
            Generation(0),
            0,
            PaneFrameSource::Scripted(crate::frame_source::ScriptedFrameSource::new(
                Transport::Direct,
            )),
        );
        // Deliberately unset the daemon attachment's live flag without losing
        // the host stream. Bookkeeping must not choose the keystroke route.
        pane.begin_attaching("new-metadata".into(), Transport::Direct, Generation(1));
        workspace.panes.insert(pane.id, pane);
        let mut jobs = LoopJobs::new(&workspace);
        let mut chrome = Chrome::dark();
        send_live_input(
            &mut workspace,
            &mut chrome,
            &mut jobs,
            PaneId(1),
            b"x",
            false,
        )
        .expect("native input uses the present host stream");
        send_live_input(
            &mut workspace,
            &mut chrome,
            &mut jobs,
            PaneId(1),
            b"paste",
            true,
        )
        .expect("native paste uses the present host stream");
        let source = workspace.pane(PaneId(1)).scripted_source().unwrap();
        assert!(
            source
                .sent_messages()
                .iter()
                .any(|message| matches!(message,
            ClientMessage::Input { data } if data == b"x")),
            "daemon_ready={daemon_ready}: metadata must never gate a native key"
        );
        assert!(
            source
                .sent_messages()
                .iter()
                .any(|message| matches!(message,
            ClientMessage::Paste { text } if text == "paste")),
            "daemon_ready={daemon_ready}: metadata must never gate native paste"
        );
        assert!(
            workspace.pane(PaneId(1)).writer.is_none(),
            "native input must not spawn a daemon writer"
        );
        assert!(chrome.alert_log.is_empty());
    }
}

#[tokio::test]
async fn a_missing_native_host_stream_never_falls_back_to_the_daemon_writer() {
    for paste in [false, true] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token").unwrap();
        let mut workspace = Workspace::live(daemon);
        workspace.daemon_ready = true;
        let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
        pane.install_attachment(
            "metadata".into(),
            Transport::Direct,
            Generation(0),
            0,
            PaneFrameSource::Scripted(crate::frame_source::ScriptedFrameSource::new(
                Transport::Direct,
            )),
        );
        drop(pane.take_frame_source());
        workspace.panes.insert(pane.id, pane);
        let mut jobs = LoopJobs::new(&workspace);
        let mut chrome = Chrome::dark();
        let result = send_live_input(
            &mut workspace,
            &mut chrome,
            &mut jobs,
            PaneId(1),
            b"x",
            paste,
        );
        assert!(
            workspace.pane(PaneId(1)).writer.is_none(),
            "paste={paste}: losing a native host stream must never create a daemon writer"
        );
        assert!(
            result.is_err(),
            "missing native input stream must report a local failure"
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

/// A native pane whose close is in flight refuses a typed key on the spot. The
/// key never gets a daemon write sequence or writer, and the refusal is shown.
#[tokio::test]
async fn a_key_typed_into_a_closing_native_pane_never_reaches_the_daemon() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon);
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.install_attachment(
        "attachment-1".to_owned(),
        Transport::Direct,
        Generation(0),
        0,
        cut_direct_source("epoch-1").await,
    );
    assert!(
        pane.begin_closing_detach().is_some(),
        "the pane starts its close"
    );
    workspace.panes.insert(PaneId(1), pane);
    let mut jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();

    send_live_input(
        &mut workspace,
        &mut chrome,
        &mut jobs,
        PaneId(1),
        b"x",
        false,
    )
    .expect("a refused key is not an error");

    let pane = workspace.pane(PaneId(1));
    assert_eq!(
        pane.client_write_seq, 0,
        "no terminal_input sequence was issued"
    );
    assert!(pane.writer.is_none(), "no daemon writer took the key");
    assert_eq!(
        chrome
            .alert_log
            .iter()
            .filter(|toast| toast.title == CLOSING_NATIVE_INPUT)
            .count(),
        1,
        "the refused key is shown"
    );
}

/// A key typed into a tmux pane while the daemon is away is dropped. The
/// pane's status note is drawn only for a pane with no frame, so a pane that
/// was live before the outage shows the drop only through the toast.
#[tokio::test]
async fn a_key_typed_into_a_tmux_pane_during_an_outage_is_reported_not_sent() {
    for paste in [false, true] {
        let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
            .expect("unconnected daemon handle");
        let mut workspace = Workspace::live(daemon);
        workspace.daemon_ready = false;
        workspace.panes.insert(
            PaneId(1),
            Pane::new(PaneId(1), "terminal-1", Backend::Tmux, "epoch-1"),
        );
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

        let pane = workspace.pane(PaneId(1));
        assert_eq!(
            pane.status_message(),
            Some(DAEMON_DOWN_INPUT),
            "paste={paste}: the key took the outage refusal"
        );
        assert_eq!(
            pane.client_write_seq, 0,
            "paste={paste}: no write was issued"
        );
        assert!(
            pane.writer.is_none(),
            "paste={paste}: no writer took the key"
        );
        let titles: Vec<&str> = chrome
            .alert_log
            .iter()
            .map(|toast| toast.title.as_str())
            .collect();
        assert_eq!(
            titles,
            [DAEMON_DOWN_INPUT],
            "paste={paste}: the dropped key is shown"
        );
    }
}
