use gobby_terminals::leases::{Backend, Error, Registry, Viewer, WriteAdmit};
use uuid::Uuid;

#[tokio::test]
async fn control_holder_transitions_are_deterministic() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let first = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Gclient, Backend::Native)
        .await?;
    let second = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    assert_eq!(registry.snapshot(terminal).await.generation, 0);
    let take = registry.take_control(first, false).await?;
    assert!(take.granted);
    assert_eq!(take.generation, 1);
    assert_eq!(registry.take_control(first, false).await?.generation, 1);
    let refused = registry.take_control(second, false).await?;
    assert!(!refused.granted);
    assert_eq!(refused.reason.as_deref(), Some("held"));
    let takeover = registry.take_control(second, true).await?;
    assert_eq!(takeover.displaced, Some(first.id));
    assert_eq!(takeover.generation, 2);
    assert_eq!(registry.release_control(first).await?.generation, 2);
    let released = registry.release_control(second).await?;
    assert!(!released.granted);
    assert_eq!(released.reason.as_deref(), Some("released"));
    assert_eq!(released.generation, 3);
    registry.take_control(first, false).await?;
    let finalized = registry
        .finalize(first, "socket_closed")
        .await?
        .expect("first finalize");
    assert!(finalized.holder_lost);
    assert_eq!(finalized.generation, 5);
    assert!(registry.finalize(first, "socket_closed").await?.is_none());
    assert_eq!(registry.snapshot(terminal).await.holder, None);
    assert!(matches!(
        registry.take_control(first, false).await,
        Err(Error::StaleAttachment)
    ));
    let mut wrong_terminal = second;
    wrong_terminal.terminal_id = Uuid::new_v4();
    assert!(matches!(
        registry.release_control(wrong_terminal).await,
        Err(Error::StaleAttachment)
    ));
    let mut old_generation = second;
    old_generation.generation += 1;
    assert!(matches!(
        registry.finalize(old_generation, "stale").await,
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
    let desktop = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Gclient, Backend::Native)
        .await?;
    let web = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    registry.resize(desktop, 40, 120).await?;
    let watching = registry.resize(web, 20, 60).await?;
    assert!(!watching.applied);
    assert_eq!(watching.owner, Some(Viewer::Gclient));
    registry.take_control(web, false).await?;
    assert_eq!(registry.snapshot(terminal).await.sizing_owner, Some(web.id));
    assert_eq!(
        registry.resize(desktop, 45, 125).await?.owner,
        Some(Viewer::Web)
    );
    registry.set_viewport(desktop, 10, 30).await?;
    registry.set_viewport(web, 15, 35).await?;
    assert_eq!(registry.set_scroll(web, 30, 12).await?.applied_rows, 12);
    assert_eq!(registry.set_scroll(desktop, -4, 12).await?.applied_rows, 0);
    let desktop_view = registry.attachment(desktop).await?;
    let web_view = registry.attachment(web).await?;
    assert_eq!(desktop_view.viewport, Some((10, 30)));
    assert_eq!(web_view.viewport, Some((15, 35)));
    assert_eq!(web_view.scroll_offset, 12);
    let before = web_view;
    assert!(matches!(
        registry.resize(web, 0, 80).await,
        Err(Error::InvalidDimensions)
    ));
    assert_eq!(registry.attachment(web).await?, before);
    let finalized = registry
        .finalize(web, "detach")
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
    let first = registry
        .attach(web_only, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    let second = registry
        .attach(web_only, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    assert!(registry.resize(first, 10, 40).await?.applied);
    assert!(registry.resize(second, 20, 60).await?.applied);
    assert_eq!(
        registry.snapshot(web_only).await.sizing_owner,
        Some(second.id)
    );
    let tmux = registry
        .attach(Uuid::new_v4(), Uuid::new_v4(), Viewer::Web, Backend::Tmux)
        .await?;
    assert!(!registry.resize(tmux, 20, 60).await?.applied);
    registry.take_control(tmux, false).await?;
    assert!(registry.resize(tmux, 20, 60).await?.applied);
    Ok(())
}

#[tokio::test]
async fn write_admission_is_generation_and_payload_bound() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let holder = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Gclient, Backend::Native)
        .await?;
    let observer = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    let control = registry.take_control(holder, false).await?;
    assert!(matches!(
        registry
            .admit_write(observer, control.generation, 0, "input", b"a")
            .await,
        Err(Error::Held)
    ));
    assert!(matches!(
        registry.admit_write(holder, 0, 0, "input", b"a").await,
        Err(Error::StaleGeneration)
    ));
    assert_eq!(
        registry
            .admit_write(holder, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert_eq!(
        registry
            .admit_write(holder, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::JoinInflight
    );
    assert!(matches!(
        registry
            .admit_write(holder, control.generation, 0, "input", b"b")
            .await,
        Err(Error::WriteConflict)
    ));
    assert!(matches!(
        registry
            .admit_write(holder, control.generation, 0, "paste", b"a")
            .await,
        Err(Error::WriteConflict)
    ));
    registry.complete_write(holder, 0, "written", None).await?;
    assert_eq!(
        registry
            .admit_write(holder, control.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "written".into(),
            reason: None
        }
    );
    registry.release_control(holder).await?;
    let retaken = registry.take_control(holder, false).await?;
    assert!(matches!(
        registry
            .admit_write(holder, control.generation, 0, "input", b"a")
            .await,
        Err(Error::StaleGeneration)
    ));
    assert!(matches!(
        registry
            .admit_write(holder, retaken.generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay { .. }
    ));
    registry.finalize(holder, "detach").await?;
    assert!(matches!(
        registry.complete_write(holder, 0, "written", None).await,
        Err(Error::StaleAttachment)
    ));
    Ok(())
}

#[tokio::test]
async fn lifecycle_order_is_epoch_scoped() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(32);
    let terminal = Uuid::new_v4();
    let attachment = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    registry.take_control(attachment, false).await?;
    registry.release_control(attachment).await?;
    registry.finalize(attachment, "detach").await?;
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
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
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
        let connection = Uuid::new_v4();
        let attachment = registry
            .attach(terminal, connection, Viewer::Web, Backend::Native)
            .await?;
        registry.take_control(attachment, false).await?;
        let before = registry.snapshot(terminal).await;
        if closed_queue {
            assert!(events.try_recv().is_ok());
            assert!(events.try_recv().is_ok());
            drop(events);
        }
        assert!(matches!(
            registry.release_control(attachment).await,
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
            registry.set_viewport(attachment, 20, 80).await,
            Err(Error::PublicationFault)
        ));
        assert!(matches!(
            registry.finalize(attachment, "detach").await,
            Err(Error::PublicationFault)
        ));
        assert!(!fault.has_changed()?, "one fault path signals once");
    }
    Ok(())
}

#[tokio::test]
async fn publisher_loss_blocks_attachment_mutations() -> anyhow::Result<()> {
    let (registry, events) = Registry::new(16);
    let connection = Uuid::new_v4();
    let terminal = Uuid::new_v4();
    let attachment = registry
        .attach(terminal, connection, Viewer::Web, Backend::Native)
        .await?;
    registry.take_control(attachment, false).await?;
    let before = registry.snapshot(terminal).await;
    let mut fault = registry.subscribe_fault();
    drop(events);
    assert!(matches!(
        registry.set_viewport(attachment, 20, 80).await,
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
        registry.resize(attachment, 10, 40).await,
        Err(Error::PublicationFault)
    ));
    assert!(!fault.has_changed()?);
    Ok(())
}

#[tokio::test]
async fn write_window_is_bounded_and_evicted_sequences_expire() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(16);
    let holder = registry
        .attach(
            Uuid::new_v4(),
            Uuid::new_v4(),
            Viewer::Gclient,
            Backend::Native,
        )
        .await?;
    let generation = registry.take_control(holder, false).await?.generation;
    for seq in 0..64 {
        assert_eq!(
            registry
                .admit_write(holder, generation, seq, "input", b"a")
                .await?,
            WriteAdmit::New
        );
    }
    assert!(matches!(
        registry
            .admit_write(holder, generation, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry.complete_write(holder, 1, "written", None).await?;
    assert!(matches!(
        registry
            .admit_write(holder, generation, 64, "input", b"a")
            .await,
        Err(Error::WriteCapacity)
    ));
    registry
        .complete_write(holder, 0, "refused", Some("backend unavailable"))
        .await?;
    assert_eq!(
        registry
            .admit_write(holder, generation, 0, "input", b"a")
            .await?,
        WriteAdmit::Replay {
            outcome: "refused".into(),
            reason: Some("backend unavailable".into())
        }
    );
    assert_eq!(
        registry
            .admit_write(holder, generation, 64, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(holder, generation, 0, "input", b"a")
            .await,
        Err(Error::WriteExpired)
    ));
    assert_eq!(
        registry
            .admit_write(holder, generation, 65, "input", b"a")
            .await?,
        WriteAdmit::New
    );
    assert!(matches!(
        registry
            .admit_write(holder, generation, 1, "input", b"a")
            .await,
        Err(Error::WriteExpired)
    ));
    Ok(())
}

#[tokio::test]
async fn attachment_validation_and_numeric_bounds_are_atomic() -> anyhow::Result<()> {
    let (registry, _events) = Registry::new(16);
    let token = registry
        .attach(Uuid::new_v4(), Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    let generation = registry.take_control(token, false).await?.generation;
    let before = registry.attachment(token).await?;
    for (rows, cols) in [(1025, 1), (1, 1025), (1024, 1024), (i64::MAX, i64::MAX)] {
        assert!(matches!(
            registry.set_viewport(token, rows, cols).await,
            Err(Error::InvalidDimensions)
        ));
    }
    assert!(matches!(
        registry.set_scroll(token, i64::MIN, 10).await,
        Err(Error::SafeIntegerOverflow)
    ));
    assert!(matches!(
        registry
            .admit_write(token, generation, 1 << 53, "input", b"a")
            .await,
        Err(Error::SafeIntegerOverflow)
    ));
    assert_eq!(registry.attachment(token).await?, before);
    let mut stale = token;
    stale.generation += 1;
    assert!(matches!(
        registry.attachment(stale).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.resize(stale, 10, 40).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.set_viewport(stale, 10, 40).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.set_scroll(stale, 1, 10).await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry
            .admit_write(stale, generation, 0, "input", b"a")
            .await,
        Err(Error::StaleAttachment)
    ));
    assert!(matches!(
        registry.complete_write(stale, 0, "written", None).await,
        Err(Error::StaleAttachment)
    ));
    assert_eq!(registry.attachment(token).await?, before);
    Ok(())
}

#[tokio::test]
async fn competing_control_requests_publish_one_ordered_winner() -> anyhow::Result<()> {
    let (registry, mut events) = Registry::new(16);
    let terminal = Uuid::new_v4();
    let first = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    let second = registry
        .attach(terminal, Uuid::new_v4(), Viewer::Web, Backend::Native)
        .await?;
    let (a, b) = tokio::join!(
        registry.take_control(first, false),
        registry.take_control(second, false)
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
