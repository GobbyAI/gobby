use super::Coalescer;
use crate::app::{Backend, Pane, PaneId, Workspace};
use crate::daemon::{Daemon, LiveDaemon};
use crate::frame_source::{PaneFrameSource, ProxyFrameSource, ScriptedFrameSource, Transport};

#[test]
fn coalescer_keeps_one_in_flight_and_latest_pending() {
    let mut coalescer = Coalescer::default();
    assert_eq!(
        coalescer.offer("pane", 1),
        Some(1),
        "an idle key issues at once"
    );
    assert_eq!(
        coalescer.offer("pane", 2),
        None,
        "a busy key holds the offer"
    );
    assert_eq!(
        coalescer.offer("pane", 3),
        None,
        "a newer offer replaces the held one"
    );
    assert_eq!(
        coalescer.offer("other", 9),
        Some(9),
        "keys coalesce independently"
    );
    assert_eq!(
        coalescer.settle(&"pane"),
        Some(3),
        "settling releases only the latest offer"
    );
    assert_eq!(
        coalescer.offer("pane", 4),
        None,
        "the released offer is in flight now"
    );
    assert_eq!(coalescer.settle(&"pane"), Some(4));
    assert_eq!(
        coalescer.settle(&"pane"),
        None,
        "with nothing held the key goes idle"
    );
    assert_eq!(
        coalescer.offer("pane", 5),
        Some(5),
        "an idle key issues again"
    );
    assert_eq!(coalescer.settle(&"other"), None);
}

#[test]
fn removing_the_source_clears_deferred_viewport() {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.viewport_deferred = true;

    assert!(pane.take_frame_source().is_some());
    assert!(pane.frame_source().is_none());
    assert!(
        !pane.viewport_deferred(),
        "a removed writer has no viewport backlog"
    );
}

#[test]
fn replacing_the_source_clears_deferred_viewport() {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    pane.viewport_deferred = true;

    pane.install_frame_source(PaneFrameSource::Scripted(ScriptedFrameSource::new(
        Transport::Proxy,
    )));

    assert_eq!(pane.transport(), Some(Transport::Proxy));
    assert!(
        !pane.viewport_deferred(),
        "the replacement writer does not inherit the old backlog"
    );
}

#[test]
fn retiring_the_current_attachment_clears_deferred_viewport() {
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    let attachment = pane.attachment_id().to_owned();
    pane.viewport_deferred = true;

    assert!(!pane.retire_attachment("another-attachment", None));
    assert!(
        pane.viewport_deferred(),
        "retiring another attachment cannot change this writer's backlog"
    );
    assert!(pane.retire_attachment(&attachment, Some("attachment finalized".into())));
    assert!(!pane.is_live());
    assert!(pane.frame_source().is_none());
    assert!(
        !pane.viewport_deferred(),
        "a retired attachment has no viewport backlog"
    );
}

#[test]
fn proxy_geometry_clears_deferred_viewport() {
    // This handle is never connected: staging returns messages for the
    // geometry job to send, without doing I/O itself.
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let (_, receiver) = daemon.subscribe();
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    let attachment = pane.attachment_id().to_owned();
    let source =
        ProxyFrameSource::from_attachment(daemon, "terminal-1", attachment.clone(), receiver)
            .expect("proxy source");
    pane.install_frame_source(PaneFrameSource::Proxy(source));
    // Exercise the Proxy branch independently of replacement's reset.
    pane.viewport_deferred = true;
    let mut workspace = Workspace::scripted();
    workspace.panes.insert(pane.id, pane);

    let messages = workspace
        .stage_geometry(PaneId(1), 30, 100)
        .expect("stage proxy geometry");

    assert_eq!(messages.len(), 2);
    assert_eq!(messages[0]["type"], "terminal_set_viewport");
    assert_eq!(messages[0]["attachment_id"], attachment);
    assert_eq!(messages[1]["type"], "terminal_resize");
    assert_eq!(workspace.pane(PaneId(1)).viewport(), (30, 100));
    assert!(
        !workspace.pane(PaneId(1)).viewport_deferred(),
        "proxy geometry cannot wait on a discarded direct writer"
    );
}
