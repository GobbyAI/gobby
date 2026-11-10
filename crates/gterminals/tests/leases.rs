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
    // With no holder, the web attachment never takes sizing from the gclient pane.
    assert_eq!(
        registry.snapshot(terminal).await.sizing_owner,
        Some(desktop.id)
    );
    assert_eq!(
        registry.resize(desktop, desktop_req, 45, 125).await?.owner,
        Some(Viewer::Gclient)
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
    // The web attachment never owned sizing, so finalizing it re-elects nobody.
    let finalized = registry
        .finalize(web, web_req, "detach")
        .await?
        .expect("finalize web");
    assert!(finalized.sizing.is_none());
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
    // A tmux web attachment is never sized without a holder.
    assert!(!registry.resize(tmux, tmux_req, 20, 60).await?.applied);
    Ok(())
}

#[tokio::test]
async fn write_admission_is_generation_and_payload_bound() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let pane_req = requester();
    let pane = registry
        .attach(terminal, pane_req, Viewer::Gclient, Backend::Native)
        .await?;
    let observer_req = requester();
    let observer = registry
        .attach(terminal, observer_req, Viewer::Web, Backend::Native)
        .await?;
    // A web attachment without a holder is refused before any payload check.
    assert!(matches!(
        registry
            .admit_write(observer, observer_req, 0, 0, "input", b"a")
            .await,
        Err(Error::Held)
    ));
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::JoinInflight
    );
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"b")
            .await,
        Err(Error::WriteConflict)
    ));
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 0, "paste", b"a")
            .await,
        Err(Error::WriteConflict)
    ));
    registry
        .complete_write(pane, pane_req, 0, "written", None)
        .await?;
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None
        }
    );
    registry.finalize(pane, pane_req, "detach").await?;
    assert!(matches!(
        registry
            .complete_write(pane, pane_req, 0, "written", None)
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
    let attachment = registry
        .attach(terminal, owner, Viewer::Gclient, Backend::Native)
        .await?;
    assert_eq!(
        registry
            .admit_write(attachment, owner, 0, 0, "input", b"a")
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
            registry.resize(attachment, intruder, 10, 40).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.set_viewport(attachment, intruder, 10, 40).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.set_scroll(attachment, intruder, 3, 10).await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry
                .admit_write(attachment, intruder, 0, 1, "input", b"x")
                .await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry
                .complete_write(attachment, intruder, 0, "forged", None)
                .await,
            Err(Error::StaleAttachment)
        ));
        assert!(matches!(
            registry.finalize(attachment, intruder, "detach").await,
            Err(Error::StaleAttachment)
        ));
    }

    // Refused operations change no state and publish no events.
    assert_eq!(drain(&mut events), 0);
    let view = registry.snapshot(terminal).await;
    assert_eq!(view.holder, None);
    assert_eq!(view.generation, 0);
    let attached = registry.attachment(attachment).await?;
    assert_eq!(attached.viewport, None);
    assert_eq!(attached.scroll_offset, 0);
    // The forged completion did not land, so seq 0 is still in flight.
    assert_eq!(
        registry
            .admit_write(attachment, owner, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::JoinInflight
    );
    // The refused seq 1 reserved nothing.
    assert_eq!(
        registry
            .admit_write(attachment, owner, 0, 1, "input", b"x")
            .await?,
        WriteAdmit::New
    );

    // The owner's own operations still succeed.
    registry
        .complete_write(attachment, owner, 0, "written", None)
        .await?;
    assert_eq!(
        registry
            .admit_write(attachment, owner, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None
        }
    );
    assert_eq!(
        registry.set_viewport(attachment, owner, 12, 50).await?,
        (12, 50)
    );
    assert!(registry.resize(attachment, owner, 12, 50).await?.applied);
    assert_eq!(
        registry
            .set_scroll(attachment, owner, 2, 10)
            .await?
            .applied_rows,
        2
    );
    assert!(
        registry
            .finalize(attachment, owner, "detach")
            .await?
            .is_some()
    );
    Ok(())
}

#[tokio::test]
async fn write_fingerprint_distinguishes_kind_payload_boundaries() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let pane_req = requester();
    let pane = registry
        .attach(Uuid::new_v4(), pane_req, Viewer::Gclient, Backend::Native)
        .await?;
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"x:ls")
            .await?,
        WriteAdmit::New
    );
    registry
        .complete_write(pane, pane_req, 0, "written", None)
        .await?;
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input:x", b"ls")
            .await,
        Err(Error::WriteConflict)
    ));
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"x:ls")
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
        .finalize(attachment, attachment_req, "detach")
        .await?;
    let mut prior = 0;
    while let Ok(event) = events.try_recv() {
        assert_eq!(event.daemon_epoch, registry.epoch());
        assert_eq!(event.lifecycle_seq, prior + 1);
        assert_eq!(event.terminal_id, terminal);
        prior = event.lifecycle_seq;
    }
    assert_eq!(prior, 2);
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
        // The attach event fills the one-slot queue, so the next publication cannot reserve.
        let (registry, mut events) = Registry::new(1);
        let mut fault = registry.subscribe_fault();
        let terminal = Uuid::new_v4();
        let attachment_req = requester();
        let connection = attachment_req.connection;
        let attachment = registry
            .attach(terminal, attachment_req, Viewer::Web, Backend::Native)
            .await?;
        let before = registry.snapshot(terminal).await;
        if closed_queue {
            assert!(events.try_recv().is_ok());
            drop(events);
        }
        assert!(matches!(
            registry
                .finalize(attachment, attachment_req, "detach")
                .await,
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
            registry.resize(attachment, attachment_req, 10, 40).await,
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
    let pane_req = requester();
    let pane = registry
        .attach(Uuid::new_v4(), pane_req, Viewer::Gclient, Backend::Native)
        .await?;
    for seq in 0..64 {
        assert_eq!(
            registry
                .admit_write(pane, pane_req, 0, seq, "input", b"a")
                .await?,
            WriteAdmit::New
        );
    }
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry
        .complete_write(pane, pane_req, 1, "written", None)
        .await?;
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry
        .complete_write(pane, pane_req, 0, "refused", Some("backend unavailable"))
        .await?;
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "refused".into(),
            reason: Some("backend unavailable".into())
        }
    );
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 64, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 0, "input", b"a")
            .await,
        Err(Error::WriteExpired)
    ));
    assert_eq!(
        registry
            .admit_write(pane, pane_req, 0, 65, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(pane, pane_req, 0, 1, "input", b"a")
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
            .admit_write(token, token_req, 0, 1 << 53, "input", b"a")
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
            .admit_write(stale, stale_req, 0, 0, "input", b"a")
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

/// Shutdown finalizes a web attachment that holds nothing, exactly once: the
/// second shutdown finalize finds the record already finalized.
#[tokio::test]
async fn shutdown_finalizes_a_web_attachment_once() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let req = requester();
    let attachment = registry
        .attach(terminal, req, Viewer::Web, Backend::Native)
        .await?;
    let finalized = registry
        .finalize_shutdown(attachment)
        .await?
        .expect("first finalize reports the attachment");
    assert!(!finalized.holder_lost);
    assert_eq!(registry.snapshot(terminal).await.holder, None);
    assert!(registry.finalize_shutdown(attachment).await?.is_none());
    Ok(())
}

/// A gclient pane holds no lease, so its operator writes are admitted with no
/// holder and at any generation. A web attachment without a holder is refused.
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
    assert_eq!(registry.snapshot(terminal).await.holder, None);

    // No holder: the gclient write is admitted; the web write is refused.
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

    // The gclient write is admitted at a generation it never saw.
    assert_eq!(
        registry
            .admit_write(gclient, gclient_req, 7, 1, "input", b"b")
            .await?,
        WriteAdmit::New
    );
    assert_eq!(registry.snapshot(terminal).await.holder, None);
    Ok(())
}
