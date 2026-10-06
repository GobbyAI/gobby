use super::super::jobs_apply::apply_job_outcome;
use super::{Coalescer, JobKey, JobOutcome, JobResult, JobTag, LoopJobs};
use crate::app::{Backend, ControlState, Pane, PaneId, Workspace};
use crate::daemon::{Daemon, LiveDaemon};
use crate::frame_source::{PaneFrameSource, ProxyFrameSource, ScriptedFrameSource, Transport};
use crate::ui::Chrome;

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

/// R6 F2: a re-attach without a reconnect keeps the connection, so a write
/// outcome for the attachment it replaced still arrives as current. It is
/// reported, but leaves the new attachment's control and in-flight write
/// alone; the same outcome for the pane's own attachment still lands.
#[test]
fn a_late_write_outcome_for_a_replaced_attachment_leaves_the_new_one_alone() {
    let daemon = LiveDaemon::unconnected("http://127.0.0.1:1", "test-token")
        .expect("unconnected daemon handle");
    let mut workspace = Workspace::live(daemon.clone());
    let mut pane = Pane::new(PaneId(1), "terminal-1", Backend::Native, "epoch-1");
    let attachment = pane.attachment_id().to_owned();
    pane.control = ControlState::Observe;
    pane.in_flight_write = Some(7);
    workspace.panes.insert(pane.id, pane);
    let mut jobs = LoopJobs::new(&workspace);
    let mut chrome = Chrome::dark();
    let outcome = |result| JobOutcome {
        tag: JobTag {
            id: 7,
            generation: daemon.generation(),
            key: JobKey::Write(PaneId(1)),
        },
        result,
    };

    let replaced = "attachment-replaced".to_owned();
    let abandoned = JobResult::WriteAbandoned {
        pane: PaneId(1),
        attachment: replaced.clone(),
        messages: 2,
        bytes: 2,
    };
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome(abandoned));
    let unconfirmed = JobResult::WriteUnconfirmed {
        pane: PaneId(1),
        attachment: replaced,
    };
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome(unconfirmed));
    let pane = workspace.pane(PaneId(1));
    assert_eq!(
        pane.control,
        ControlState::Observe,
        "the replaced attachment's uncertainty is not the new one's"
    );
    assert_eq!(pane.in_flight_write(), Some(7));

    let unconfirmed = JobResult::WriteUnconfirmed {
        pane: PaneId(1),
        attachment,
    };
    apply_job_outcome(&mut workspace, &mut chrome, &mut jobs, outcome(unconfirmed));
    let pane = workspace.pane(PaneId(1));
    assert_eq!(pane.control, ControlState::UncertainReadOnly);
    assert_eq!(pane.in_flight_write(), None);
    let titles: Vec<&str> = chrome
        .alert_log
        .iter()
        .map(|toast| toast.title.as_str())
        .collect();
    assert_eq!(
        titles,
        [
            "Input not sent: 2 bytes.",
            "Input delivery unconfirmed.",
            "Input delivery unconfirmed."
        ],
        "every outcome is still reported"
    );
}
