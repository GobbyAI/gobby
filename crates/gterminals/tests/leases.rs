mod common;

use gobby_terminals::leases::{Backend, Error, Registry, Requester, Viewer, WriteAdmit};
use uuid::Uuid;

/// A fresh client connection bound to a fresh caller. Each attachment in a test
/// gets its own requester unless the test deliberately reuses one.
fn requester() -> Requester {
    Requester {
        connection: Uuid::new_v4(),
        caller: Uuid::new_v4(),
    }
}

fn drain<T>(events: &mut tokio::sync::mpsc::Receiver<T>) -> usize {
    let mut drained = 0;
    while events.try_recv().is_ok() {
        drained += 1;
    }
    drained
}

#[tokio::test]
async fn control_holder_transitions_are_deterministic() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let first_req = requester();
    let first = registry
        .attach(terminal, first_req, Viewer::Web, Backend::Native)
        .await?;
    let second_req = requester();
    let second = registry
        .attach(terminal, second_req, Viewer::Web, Backend::Native)
        .await?;
    assert_eq!(registry.snapshot(terminal).await.generation, 0);
    let take = registry.take_control(first, first_req, false).await?;
    assert!(take.granted);
    assert_eq!(take.generation, 1);
    assert_eq!(
        registry
            .take_control(first, first_req, false)
            .await?
            .generation,
        1
    );
    let refused = registry.take_control(second, second_req, false).await?;
    assert!(!refused.granted);
    assert_eq!(refused.reason.as_deref(), Some("held"));
    let takeover = registry.take_control(second, second_req, true).await?;
    assert_eq!(takeover.displaced, Some(first.id));
    assert_eq!(takeover.generation, 2);
    assert_eq!(
        registry.release_control(first, first_req).await?.generation,
        2
    );
    let released = registry.release_control(second, second_req).await?;
    assert!(!released.granted);
    assert_eq!(released.reason.as_deref(), Some("released"));
    assert_eq!(released.generation, 3);
    registry.take_control(first, first_req, false).await?;
    let finalized = registry
        .finalize(first, first_req, "socket_closed")
        .await?
        .expect("first finalize");
    assert!(finalized.holder_lost);
    assert_eq!(finalized.generation, 5);
    assert!(
        registry
            .finalize(first, first_req, "socket_closed")
            .await?
            .is_none()
    );
    assert_eq!(registry.snapshot(terminal).await.holder, None);
    assert!(matches!(
        registry.take_control(first, first_req, false).await,
        Err(Error::StaleAttachment)
    ));
    let mut wrong_terminal = second;
    wrong_terminal.terminal_id = Uuid::new_v4();
    assert!(matches!(
        registry.release_control(wrong_terminal, second_req).await,
        Err(Error::StaleAttachment)
    ));
    let mut old_generation = second;
    old_generation.generation += 1;
    assert!(matches!(
        registry.finalize(old_generation, second_req, "stale").await,
        Err(Error::StaleAttachment)
    ));
    let mut finalized_events = 0;
    while let Ok(event) = events.try_recv() {
        if event.attachment_id == first.id && event.kind == "finalized" {
            finalized_events += 1;
        }
    }
    assert_eq!(finalized_events, 1);
    Ok(())
}

#[tokio::test]
async fn sizing_and_scroll_are_attachment_scoped() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(64);
    let terminal = Uuid::new_v4();
    let desktop_req = requester();
    let desktop = registry
        .attach(terminal, desktop_req, Viewer::Gclient, Backend::Native)
        .await?;
    let web_req = requester();
    let web = registry
        .attach(terminal, web_req, Viewer::Web, Backend::Native)
        .await?;
    registry.resize(desktop, desktop_req, 40, 120).await?;
    let watching = registry.resize(web, web_req, 20, 60).await?;
    assert!(!watching.applied);
    assert_eq!(watching.owner, Some(Viewer::Gclient));
    registry.take_control(web, web_req, false).await?;
    assert_eq!(registry.snapshot(terminal).await.sizing_owner, Some(web.id));
    assert_eq!(
        registry.resize(desktop, desktop_req, 45, 125).await?.owner,
        Some(Viewer::Web)
    );
    registry.set_viewport(desktop, desktop_req, 10, 30).await?;
    registry.set_viewport(web, web_req, 15, 35).await?;
    assert_eq!(
        registry
            .set_scroll(web, web_req, 30, 12)
            .await?
            .applied_rows,
        12
    );
    assert_eq!(
        registry
            .set_scroll(desktop, desktop_req, -4, 12)
            .await?
            .applied_rows,
        0
    );
    let desktop_view = registry.attachment(desktop).await?;
    let web_view = registry.attachment(web).await?;
    assert_eq!(desktop_view.viewport, Some((10, 30)));
    assert_eq!(web_view.viewport, Some((15, 35)));
    assert_eq!(web_view.scroll_offset, 12);
    let before = web_view;
    assert!(matches!(
        registry.resize(web, web_req, 0, 80).await,
        Err(Error::InvalidDimensions)
    ));
    assert_eq!(registry.attachment(web).await?, before);
    let finalized = registry
        .finalize(web, web_req, "detach")
        .await?
        .expect("finalize web");
    assert_eq!(
        finalized.sizing.expect("reelection").owner,
        Some(Viewer::Gclient)
    );
    assert_eq!(
        registry.snapshot(terminal).await.sizing_owner,
        Some(desktop.id)
    );
    let web_only = Uuid::new_v4();
    let first_req = requester();
    let first = registry
        .attach(web_only, first_req, Viewer::Web, Backend::Native)
        .await?;
    let second_req = requester();
    let second = registry
        .attach(web_only, second_req, Viewer::Web, Backend::Native)
        .await?;
    assert!(registry.resize(first, first_req, 10, 40).await?.applied);
    assert!(registry.resize(second, second_req, 20, 60).await?.applied);
    assert_eq!(
        registry.snapshot(web_only).await.sizing_owner,
        Some(second.id)
    );
    let tmux_req = requester();
    let tmux = registry
        .attach(Uuid::new_v4(), tmux_req, Viewer::Web, Backend::Tmux)
        .await?;
    assert!(!registry.resize(tmux, tmux_req, 20, 60).await?.applied);
    registry.take_control(tmux, tmux_req, false).await?;
    assert!(registry.resize(tmux, tmux_req, 20, 60).await?.applied);
    Ok(())
}

#[tokio::test]
async fn write_admission_is_generation_and_payload_bound() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let holder_req = requester();
    let holder = registry
        .attach(terminal, holder_req, Viewer::Web, Backend::Native)
        .await?;
    let observer_req = requester();
    let observer = registry
        .attach(terminal, observer_req, Viewer::Web, Backend::Native)
        .await?;
    let control = registry.take_control(holder, holder_req, false).await?;
    assert!(matches!(
        registry
            .admit_write(observer, observer_req, control.generation, 0, "input", b"a")
            .await,
        Err(Error::Held)
    ));
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, 0, 0, "input", b"a")
            .await,
        Err(Error::StaleGeneration)
    ));
    assert_eq!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert_eq!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::JoinInflight
    );
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"b")
            .await,
        Err(Error::WriteConflict)
    ));
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "paste", b"a")
            .await,
        Err(Error::WriteConflict)
    ));
    registry
        .complete_write(holder, holder_req, 0, "written", None)
        .await?;
    assert_eq!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None
        }
    );
    registry.release_control(holder, holder_req).await?;
    let retaken = registry.take_control(holder, holder_req, false).await?;
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"a")
            .await,
        Err(Error::StaleGeneration)
    ));
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, retaken.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay { .. }
    ));
    registry.finalize(holder, holder_req, "detach").await?;
    assert!(matches!(
        registry
            .complete_write(holder, holder_req, 0, "written", None)
            .await,
        Err(Error::StaleAttachment)
    ));
    Ok(())
}

#[tokio::test]
async fn foreign_connection_cannot_admit_write() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let owner = requester();
    let holder = registry
        .attach(terminal, owner, Viewer::Web, Backend::Native)
        .await?;
    let control = registry.take_control(holder, owner, false).await?;
    assert!(control.granted);
    assert_eq!(
        registry
            .admit_write(holder, owner, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    drain(&mut events);

    // Same caller on a foreign connection, then same connection for a foreign caller.
    let foreign_connection = Requester {
        connection: Uuid::new_v4(),
        caller: owner.caller,
    };
    let foreign_caller = Requester {
        connection: owner.connection,
        caller: Uuid::new_v4(),
    };
    for intruder in [foreign_connection, foreign_caller] {
        assert!(matches!(
            registry.take_control(holder, intruder, true).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.release_control(holder, intruder).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.resize(holder, intruder, 10, 40).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.set_viewport(holder, intruder, 10, 40).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.set_scroll(holder, intruder, 3, 10).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry
                .admit_write(holder, intruder, control.generation, 1, "input", b"x")
                .await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry
                .complete_write(holder, intruder, 0, "forged", None)
                .await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.finalize(holder, intruder, "detach").await,
            Err(Error::StaleAttachment)
        ));
    }

    // Refused operations change no state and publish no events.
    assert_eq!(drain(&mut events), 0);
    let view = registry.snapshot(terminal).await;
    assert_eq!(view.holder, Some(holder.id));
    assert_eq!(view.generation, control.generation);
    let attached = registry.attachment(holder).await?;
    assert_eq!(attached.viewport, None);
    assert_eq!(attached.scroll_offset, 0);
    // The forged completion did not land, so seq 0 is still in flight.
    assert_eq!(
        registry
            .admit_write(holder, owner, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::JoinInflight
    );
    // The refused seq 1 reserved nothing.
    assert_eq!(
        registry
            .admit_write(holder, owner, control.generation, 1, "input", b"x")
            .await?,
        WriteAdmit::New
    );

    // The owner's own operations still succeed.
    registry
        .complete_write(holder, owner, 0, "written", None)
        .await?;
    assert_eq!(
        registry
            .admit_write(holder, owner, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None
        }
    );
    assert_eq!(
        registry.set_viewport(holder, owner, 12, 50).await?,
        (12, 50)
    );
    assert!(registry.resize(holder, owner, 12, 50).await?.applied);
    assert_eq!(
        registry
            .set_scroll(holder, owner, 2, 10)
            .await?
            .applied_rows,
        2
    );
    registry.release_control(holder, owner).await?;
    assert!(registry.take_control(holder, owner, false).await?.granted);
    assert!(drain(&mut events) > 0);
    assert!(registry.finalize(holder, owner, "detach").await?.is_some());
    Ok(())
}

#[tokio::test]
async fn write_fingerprint_distinguishes_kind_payload_boundaries() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let holder_req = requester();
    let holder = registry
        .attach(Uuid::new_v4(), holder_req, Viewer::Web, Backend::Native)
        .await?;
    let control = registry.take_control(holder, holder_req, false).await?;
    assert_eq!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"x:ls")
            .await?,
        WriteAdmit::New
    );
    registry
        .complete_write(holder, holder_req, 0, "written", None)
        .await?;
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input:x", b"ls")
            .await,
        Err(Error::WriteConflict)
    ));
    assert_eq!(
        registry
            .admit_write(holder, holder_req, control.generation, 0, "input", b"x:ls")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None,
        }
    );
    Ok(())
}

#[tokio::test]
async fn lifecycle_order_is_epoch_scoped() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let attachment_req = requester();
    let attachment = registry
        .attach(terminal, attachment_req, Viewer::Web, Backend::Native)
        .await?;
    registry
        .take_control(attachment, attachment_req, false)
        .await?;
    registry.release_control(attachment, attachment_req).await?;
    registry
        .finalize(attachment, attachment_req, "detach")
        .await?;
    let mut prior = 0;
    while let Ok(event) = events.try_recv() {
        assert_eq!(event.daemon_epoch, registry.epoch());
        assert_eq!(event.lifecycle_seq, prior + 1);
        assert_eq!(event.terminal_id, terminal);
        prior = event.lifecycle_seq;
    }
    assert_eq!(prior, 4);
    let (successor, mut new_events) = Registry::new(32);
    assert_ne!(successor.epoch(), registry.epoch());
    successor
        .attach(terminal, requester(), Viewer::Web, Backend::Native)
        .await?;
    let first = new_events.try_recv()?;
    assert_eq!(first.daemon_epoch, successor.epoch());
    assert_eq!(first.lifecycle_seq, 1);
    Ok(())
}

#[tokio::test]
async fn publication_failure_is_atomic_and_fail_closed() -> anyhow::Result<()> {
    for closed_queue in [false, true] {
        let (registry, mut events) = Registry::new(2);
        let mut fault = registry.subscribe_fault();
        let terminal = Uuid::new_v4();
        let attachment_req = requester();
        let connection = attachment_req.connection;
        let attachment = registry
            .attach(terminal, attachment_req, Viewer::Web, Backend::Native)
            .await?;
        registry
            .take_control(attachment, attachment_req, false)
            .await?;
        let before = registry.snapshot(terminal).await;
        if closed_queue {
            assert!(events.try_recv().is_ok());
            assert!(events.try_recv().is_ok());
            drop(events);
        }
        assert!(matches!(
            registry.release_control(attachment, attachment_req).await,
            Err(Error::PublicationFault)
        ));
        assert_eq!(registry.snapshot(terminal).await, before);
        assert!(fault.has_changed()?);
        assert!(
            fault
                .borrow_and_update()
                .as_ref()
                .expect("fault closure signal")
                .connections
                .contains(&connection)
        );
        assert!(matches!(
            registry
                .set_viewport(attachment, attachment_req, 20, 80)
                .await,
            Err(Error::PublicationFault)
        ));
        assert!(matches!(
            registry
                .finalize(attachment, attachment_req, "detach")
                .await,
            Err(Error::PublicationFault)
        ));
        assert!(!fault.has_changed()?, "one fault path signals once");
    }
    Ok(())
}

#[tokio::test]
async fn publisher_loss_blocks_attachment_mutations() -> anyhow::Result<()> {
    let (registry, events) = Registry::new(16);
    let attachment_req = requester();
    let connection = attachment_req.connection;
    let terminal = Uuid::new_v4();
    let attachment = registry
        .attach(terminal, attachment_req, Viewer::Web, Backend::Native)
        .await?;
    registry
        .take_control(attachment, attachment_req, false)
        .await?;
    let before = registry.snapshot(terminal).await;
    let mut fault = registry.subscribe_fault();
    drop(events);
    assert!(matches!(
        registry
            .set_viewport(attachment, attachment_req, 20, 80)
            .await,
        Err(Error::PublicationFault)
    ));
    assert_eq!(registry.snapshot(terminal).await, before);
    assert!(fault.has_changed()?);
    assert_eq!(
        fault
            .borrow_and_update()
            .as_ref()
            .expect("publisher fault")
            .connections,
        [connection].into()
    );
    assert!(matches!(
        registry.resize(attachment, attachment_req, 10, 40).await,
        Err(Error::PublicationFault)
    ));
    assert!(!fault.has_changed()?);
    Ok(())
}

#[tokio::test]
async fn write_window_is_bounded_and_evicted_sequences_expire() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(16);
    let holder_req = requester();
    let holder = registry
        .attach(Uuid::new_v4(), holder_req, Viewer::Web, Backend::Native)
        .await?;
    let generation = registry
        .take_control(holder, holder_req, false)
        .await?
        .generation;
    for seq in 0..64 {
        assert_eq!(
            registry
                .admit_write(holder, holder_req, generation, seq, "input", b"a")
                .await?,
            WriteAdmit::New
        );
    }
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, generation, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry
        .complete_write(holder, holder_req, 1, "written", None)
        .await?;
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, generation, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry
        .complete_write(
            holder,
            holder_req,
            0,
            "refused",
            Some("backend unavailable"),
        )
        .await?;
    assert_eq!(
        registry
            .admit_write(holder, holder_req, generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "refused".into(),
            reason: Some("backend unavailable".into())
        }
    );
    assert_eq!(
        registry
            .admit_write(holder, holder_req, generation, 64, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, generation, 0, "input", b"a")
            .await,
        Err(Error::WriteExpired)
    ));
    assert_eq!(
        registry
            .admit_write(holder, holder_req, generation, 65, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(holder, holder_req, generation, 1, "input", b"a")
            .await,
        Err(Error::WriteExpired)
    ));
    Ok(())
}

#[tokio::test]
async fn attachment_validation_and_numeric_bounds_are_atomic() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(16);
    let token_req = requester();
    let token = registry
        .attach(Uuid::new_v4(), token_req, Viewer::Web, Backend::Native)
        .await?;
    let generation = registry
        .take_control(token, token_req, false)
        .await?
        .generation;
    let before = registry.attachment(token).await?;
    for (rows, cols) in [(1025, 1), (1, 1025), (1024, 1024), (i64::MAX, i64::MAX)] {
        assert!(matches!(
            registry.set_viewport(token, token_req, rows, cols).await,
            Err(Error::InvalidDimensions)
        ));
    }
    assert!(matches!(
        registry.set_scroll(token, token_req, i64::MIN, 10).await,
        Err(Error::SafeIntegerOverflow)
    ));
    assert!(matches!(
        registry
            .admit_write(token, token_req, generation, 1 << 53, "input", b"a")
            .await,
        Err(Error::SafeIntegerOverflow)
    ));
    assert_eq!(registry.attachment(token).await?, before);
    let mut stale = token;
    stale.generation += 1;
    let stale_req = token_req;
    assert!(matches!(
        registry.attachment(stale).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.resize(stale, stale_req, 10, 40).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.set_viewport(stale, stale_req, 10, 40).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.set_scroll(stale, stale_req, 1, 10).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry
            .admit_write(stale, stale_req, generation, 0, "input", b"a")
            .await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry
            .complete_write(stale, stale_req, 0, "written", None)
            .await,
        Err(Error::StaleAttachment)
    ));
    assert_eq!(registry.attachment(token).await?, before);
    Ok(())
}

#[tokio::test]
async fn competing_control_requests_publish_one_ordered_winner() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(16);
    let terminal = Uuid::new_v4();
    let first_req = requester();
    let first = registry
        .attach(terminal, first_req, Viewer::Web, Backend::Native)
        .await?;
    let second_req = requester();
    let second = registry
        .attach(terminal, second_req, Viewer::Web, Backend::Native)
        .await?;
    let (a, b) = tokio::join!(
        registry.take_control(first, first_req, false),
        registry.take_control(second, second_req, false)
    );
    let (a, b) = (a?, b?);
    assert_ne!(a.granted, b.granted);
    let winner = if a.granted { first.id } else { second.id };
    let view = registry.snapshot(terminal).await;
    assert_eq!(view.holder, Some(winner));
    assert_eq!(view.generation, 1);
    let published: Vec<_> = std::iter::from_fn(|| events.try_recv().ok()).collect();
    assert_eq!(published.len(), 3);
    assert_eq!(
        published
            .iter()
            .map(|e| e.lifecycle_seq)
            .collect::<Vec<_>>(),
        vec![1, 2, 3]
    );
    assert_eq!(published[2].holder, Some(winner));
    Ok(())
}

#[tokio::test]
async fn last_viewer_finalize_releases_sizing_without_prior_owner() -> anyhow::Result<()> {
    for backend in [Backend::Native, Backend::Tmux] {
        let (registry, _events) = Registry::new(16);
        let terminal = Uuid::new_v4();
        let first_req = requester();
        let first = registry
            .attach(terminal, first_req, Viewer::Web, backend)
            .await?;
        let last_req = requester();
        let last = registry
            .attach(terminal, last_req, Viewer::Web, backend)
            .await?;
        assert_eq!(registry.snapshot(terminal).await.sizing_owner, None);
        assert!(
            registry
                .finalize(first, first_req, "detach")
                .await?
                .expect("first finalize")
                .sizing
                .is_none()
        );
        let finalized = registry
            .finalize(last, last_req, "detach")
            .await?
            .expect("last finalize");
        let sizing = finalized
            .sizing
            .expect("last viewer must release backend sizing even without an owner");
        assert!(!sizing.applied);
        assert_eq!(sizing.owner, None);
        assert_eq!(sizing.rows, None);
        assert_eq!(sizing.cols, None);
        assert!(registry.finalize(last, last_req, "detach").await?.is_none());
    }
    Ok(())
}

/// A gclient pane holds no lease: it never takes, holds, or releases control, and
/// its operator writes are admitted with no holder and any generation. The web
/// lease is unchanged: a web write still needs the web holder and its generation.
#[tokio::test]
async fn gclient_pane_holds_no_lease_and_its_writes_are_always_admitted() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let web_req = requester();
    let web = registry
        .attach(terminal, web_req, Viewer::Web, Backend::Native)
        .await?;
    let gclient_req = requester();
    let gclient = registry
        .attach(terminal, gclient_req, Viewer::Gclient, Backend::Native)
        .await?;

    let take = registry.take_control(gclient, gclient_req, true).await?;
    assert!(!take.granted);
    assert_eq!(take.reason.as_deref(), Some("not_web_viewer"));
    let release = registry.release_control(gclient, gclient_req).await?;
    assert!(!release.granted);
    assert_eq!(release.reason.as_deref(), Some("released"));
    assert_eq!(registry.snapshot(terminal).await.holder, None);

    // No holder yet: the gclient write is admitted; the web write is still held.
    assert_eq!(
        registry
            .admit_write(gclient, gclient_req, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(web, web_req, 0, 0, "input", b"a")
            .await,
        Err(Error::Held)
    ));

    // A web holder takes control; the gclient write is admitted at a generation
    // it never saw, and the web write is admitted at the holder's generation.
    let control = registry.take_control(web, web_req, false).await?;
    assert!(control.granted);
    assert_eq!(
        registry
            .admit_write(gclient, gclient_req, 0, 1, "input", b"b")
            .await?,
        WriteAdmit::New
    );
    assert_eq!(
        registry
            .admit_write(web, web_req, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(web, web_req, control.generation - 1, 1, "input", b"a")
            .await,
        Err(Error::StaleGeneration)
    ));

    // The gclient refusal leaves the web holder in place; a web takeover still displaces it.
    let snapshot = registry.snapshot(terminal).await;
    assert_eq!(snapshot.holder, Some(web.id));
    assert_eq!(snapshot.generation, control.generation);
    Ok(())
}

/// Shutdown finalization needs no handoff store: a web holder is released and
/// finalized, and the record is marked finalized.
#[tokio::test]
async fn shutdown_finalizes_a_web_holder_without_grant_state() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let holder_req = requester();
    let holder = registry
        .attach(terminal, holder_req, Viewer::Web, Backend::Native)
        .await?;
    assert!(
        registry
            .take_control(holder, holder_req, false)
            .await?
            .granted
    );
    let finalized = registry
        .finalize_shutdown(holder)
        .await?
        .expect("first finalize reports the lost holder");
    assert!(finalized.holder_lost);
    assert_eq!(registry.snapshot(terminal).await.holder, None);
    assert!(registry.finalize_shutdown(holder).await?.is_none());
    Ok(())
}
