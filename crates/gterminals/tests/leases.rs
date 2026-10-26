mod common;

use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::{Duration, Instant};

use common::{Hub, terminal};
use gobby_core::postgres_pool::Pool;
use gobby_terminals::leases::{
    Attachment, Backend, Delivery, Error, GrantIdentity, HANDOFF_RECORD_DEADLINE, HandoffStore,
    HostInput, Registry, Requester, Viewer, WriteAdmit,
};
use gobby_terminals::reconcile::{
    HandoffDeadlines, INPUT_HANDOFF_REBIND, InputRevoke, expire_input_handoffs,
};
use gobby_terminals::repository::{self, InputHandoff, State, Terminal};
use serde_json::json;
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
        .attach(
            terminal,
            first_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
        .await?;
    let second_req = requester();
    let second = registry
        .attach(
            terminal,
            second_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            terminal,
            desktop_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
        .await?;
    let web_req = requester();
    let web = registry
        .attach(
            terminal,
            web_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            web_only,
            first_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
        .await?;
    let second_req = requester();
    let second = registry
        .attach(
            web_only,
            second_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
        .await?;
    assert!(registry.resize(first, first_req, 10, 40).await?.applied);
    assert!(registry.resize(second, second_req, 20, 60).await?.applied);
    assert_eq!(
        registry.snapshot(web_only).await.sizing_owner,
        Some(second.id)
    );
    let tmux_req = requester();
    let tmux = registry
        .attach(
            Uuid::new_v4(),
            tmux_req,
            Viewer::Web,
            Backend::Tmux,
            Delivery::Proxy,
        )
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
        .attach(
            terminal,
            holder_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
        .await?;
    let observer_req = requester();
    let observer = registry
        .attach(
            terminal,
            observer_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            terminal,
            owner,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
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
        .attach(
            Uuid::new_v4(),
            holder_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
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
        .attach(
            terminal,
            attachment_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            terminal,
            requester(),
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
            .attach(
                terminal,
                attachment_req,
                Viewer::Web,
                Backend::Native,
                Delivery::Proxy,
            )
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
        .attach(
            terminal,
            attachment_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            Uuid::new_v4(),
            holder_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
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
        .attach(
            Uuid::new_v4(),
            token_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
        .attach(
            terminal,
            first_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
        .await?;
    let second_req = requester();
    let second = registry
        .attach(
            terminal,
            second_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
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
            .attach(terminal, first_req, Viewer::Web, backend, Delivery::Proxy)
            .await?;
        let last_req = requester();
        let last = registry
            .attach(terminal, last_req, Viewer::Web, backend, Delivery::Proxy)
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

const EPOCH: &str = "E1";

fn slot(row: &Terminal) -> String {
    format!("ht-{}", row.id.simple())
}

/// A live native row bound to host epoch E1 at a row-unique host slot.
fn live_row() -> Terminal {
    let mut row = terminal();
    row.state = State::Live;
    row.host_epoch = Some(EPOCH.into());
    row.locator_key = Some(format!("native:{EPOCH}:{}", slot(&row)));
    row.locator = Some(json!({"host_terminal_id": slot(&row)}));
    row
}

fn identity(row: &Terminal) -> GrantIdentity {
    GrantIdentity {
        machine_id: row.machine_id,
        host_epoch: EPOCH.into(),
        host_terminal_id: slot(row),
    }
}

fn handoff(row: &Terminal, attachment_id: Uuid) -> InputHandoff {
    InputHandoff {
        attachment_id,
        host_epoch: EPOCH.into(),
        host_terminal_id: slot(row),
    }
}

async fn stored_handoff(hub: &Hub, id: Uuid) -> anyhow::Result<Option<InputHandoff>> {
    Ok(InputHandoff::of(&hub.get(id).await?))
}

async fn record_handoff(hub: &Hub, row: &Terminal, handoff: &InputHandoff) -> anyhow::Result<bool> {
    hub.pool
        .transaction(None, async |tx| {
            Ok::<_, anyhow::Error>(
                repository::record_input_handoff(tx, row.id, row.machine_id, handoff).await?,
            )
        })
        .await
}

/// Take control as a direct native gclient and confirm the host grant at `identity`.
async fn granted_holder(
    registry: &Registry,
    terminal_id: Uuid,
    identity: GrantIdentity,
) -> anyhow::Result<(Attachment, Requester)> {
    let holder_req = requester();
    let holder = registry
        .attach(
            terminal_id,
            holder_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
        .await?;
    let take = registry.take_control(holder, holder_req, false).await?;
    assert_eq!(take.input, HostInput::Grant(holder.id));
    assert!(
        registry
            .confirm_input_grant(holder, take.generation, identity)
            .await?
    );
    Ok((holder, holder_req))
}

struct FailingStore;

impl HandoffStore for FailingStore {
    async fn record(&self, _: Uuid, _: Uuid, _: &InputHandoff) -> anyhow::Result<bool> {
        anyhow::bail!("injected handoff record failure")
    }
}

struct HungStore;

impl HandoffStore for HungStore {
    async fn record(&self, _: Uuid, _: Uuid, _: &InputHandoff) -> anyhow::Result<bool> {
        std::future::pending().await
    }
}

/// A handoff write that lands while the expiry revoke is in flight.
struct Race<'a> {
    pool: &'a Pool,
    terminal_id: Uuid,
    replacement: InputHandoff,
}

/// Records attachment-guarded revokes; fails the first `failures` of them.
#[derive(Default)]
struct Revoker<'a> {
    calls: Mutex<Vec<(String, String, Uuid)>>,
    failures: AtomicUsize,
    race: Mutex<Option<Race<'a>>>,
}

impl Revoker<'_> {
    fn calls(&self) -> Vec<(String, String, Uuid)> {
        self.calls.lock().expect("revoke calls").clone()
    }
}

impl InputRevoke for Revoker<'_> {
    async fn revoke_input(
        &self,
        host_epoch: &str,
        host_terminal_id: &str,
        attachment_id: Uuid,
    ) -> anyhow::Result<()> {
        self.calls.lock().expect("revoke calls").push((
            host_epoch.into(),
            host_terminal_id.into(),
            attachment_id,
        ));
        let race = self.race.lock().expect("revoke race").take();
        if let Some(race) = race {
            let replacement = serde_json::to_string(&race.replacement)?;
            race.pool
                .transaction(None, async |tx| {
                    tx.execute(
                        "UPDATE terminals SET process = COALESCE(process, '{}'::jsonb)
                         || jsonb_build_object('native_input_handoff', $1::text::jsonb)
                         WHERE id=$2::text::uuid",
                        &[&replacement, &race.terminal_id.to_string()],
                    )
                    .await?;
                    anyhow::Ok(())
                })
                .await?;
        }
        if self
            .failures
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_sub(1))
            .is_ok()
        {
            anyhow::bail!("injected host revoke failure");
        }
        Ok(())
    }
}

#[tokio::test]
async fn shutdown_preserves_only_direct_native_input_grant() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let (registry, _events) = Registry::new(64);
    let rows: Vec<Terminal> = (0..7).map(|_| live_row()).collect();
    for row in &rows {
        hub.insert(row).await?;
    }
    let [direct, web, proxy, refused, socket, released, preempted] = &rows[..] else {
        unreachable!("seven fixture rows");
    };

    // Socket loss, release, and preemption revoke before shutdown begins.
    let (lost, lost_req) = granted_holder(&registry, socket.id, identity(socket)).await?;
    let finalized = registry
        .finalize(lost, lost_req, "socket_closed")
        .await?
        .expect("socket finalize");
    assert_eq!(finalized.input, HostInput::Revoke);
    let (release, release_req) = granted_holder(&registry, released.id, identity(released)).await?;
    assert_eq!(
        registry.release_control(release, release_req).await?.input,
        HostInput::Revoke
    );
    let (displaced, _) = granted_holder(&registry, preempted.id, identity(preempted)).await?;
    let web_takeover_req = requester();
    let web_takeover = registry
        .attach(
            preempted.id,
            web_takeover_req,
            Viewer::Web,
            Backend::Native,
            Delivery::Proxy,
        )
        .await?;
    let takeover = registry
        .take_control(web_takeover, web_takeover_req, true)
        .await?;
    assert_eq!(takeover.displaced, Some(displaced.id));
    assert_eq!(takeover.input, HostInput::Revoke);

    // Only `direct` holds a confirmed grant for a direct native gclient.
    let (preserved, _) = granted_holder(&registry, direct.id, identity(direct)).await?;
    let mut refused_holders = Vec::new();
    for (row, viewer, delivery) in [
        (web, Viewer::Web, Delivery::Proxy),
        (proxy, Viewer::Gclient, Delivery::Proxy),
    ] {
        let holder_req = requester();
        let holder = registry
            .attach(row.id, holder_req, viewer, Backend::Native, delivery)
            .await?;
        let take = registry.take_control(holder, holder_req, false).await?;
        assert_eq!(take.input, HostInput::Revoke);
        assert!(
            !registry
                .confirm_input_grant(holder, take.generation, identity(row))
                .await?
        );
        refused_holders.push(holder);
    }
    // The host refused this direct grant, so it was never confirmed.
    let unconfirmed_req = requester();
    let unconfirmed = registry
        .attach(
            refused.id,
            unconfirmed_req,
            Viewer::Gclient,
            Backend::Native,
            Delivery::Direct,
        )
        .await?;
    let take = registry
        .take_control(unconfirmed, unconfirmed_req, false)
        .await?;
    assert_eq!(take.input, HostInput::Grant(unconfirmed.id));
    refused_holders.push(unconfirmed);

    let pending = registry.close_admission().await;
    assert!(matches!(
        registry
            .attach(
                direct.id,
                requester(),
                Viewer::Gclient,
                Backend::Native,
                Delivery::Direct,
            )
            .await,
        Err(Error::AdmissionClosed)
    ));
    let mut outcomes = std::collections::BTreeMap::new();
    for attachment in pending {
        let finalized = registry
            .finalize_shutdown(attachment, &hub.pool)
            .await?
            .expect("shutdown finalize");
        outcomes.insert(attachment.id, finalized.input);
    }
    assert_eq!(outcomes.get(&preserved.id), Some(&HostInput::Preserved));
    for holder in &refused_holders {
        assert_eq!(outcomes.get(&holder.id), Some(&HostInput::Revoke));
    }
    assert_eq!(outcomes.get(&displaced.id), Some(&HostInput::Unchanged));
    assert_eq!(
        stored_handoff(&hub, direct.id).await?,
        Some(handoff(direct, preserved.id))
    );
    for row in [web, proxy, refused, socket, released, preempted] {
        assert_eq!(stored_handoff(&hub, row.id).await?, None);
    }

    // The adopting daemon revokes the unrebound grant once the window closes.
    let revoker = Revoker::default();
    let mut deadlines = HandoffDeadlines::new();
    let start = Instant::now();
    for elapsed in [
        Duration::ZERO,
        INPUT_HANDOFF_REBIND - Duration::from_millis(1),
    ] {
        expire_input_handoffs(
            &hub.pool,
            direct.machine_id,
            EPOCH,
            &mut deadlines,
            start + elapsed,
            &revoker,
        )
        .await?;
    }
    assert!(revoker.calls().is_empty());
    assert!(stored_handoff(&hub, direct.id).await?.is_some());
    expire_input_handoffs(
        &hub.pool,
        direct.machine_id,
        EPOCH,
        &mut deadlines,
        start + INPUT_HANDOFF_REBIND,
        &revoker,
    )
    .await?;
    assert_eq!(
        revoker.calls(),
        vec![(EPOCH.to_owned(), slot(direct), preserved.id)]
    );
    assert_eq!(stored_handoff(&hub, direct.id).await?, None);
    assert!(deadlines.is_empty());
    Ok(())
}

#[tokio::test]
async fn handoff_preservation_requires_a_durable_identity_cas() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let (registry, mut events) = Registry::new(64);

    // Each row diverges from its confirmed grant in one identity field.
    let mut moved_epoch = live_row();
    moved_epoch.host_epoch = Some("E2".into());
    let mut moved_slot = live_row();
    moved_slot.locator = Some(json!({"host_terminal_id": "ht-elsewhere"}));
    let foreign_machine = live_row();
    let mut orphaned = live_row();
    orphaned.state = State::Orphaned;
    let (failing, hung, dropped) = (live_row(), live_row(), live_row());
    for row in [
        &moved_epoch,
        &moved_slot,
        &foreign_machine,
        &orphaned,
        &failing,
        &hung,
        &dropped,
    ] {
        hub.insert(row).await?;
    }
    let mut mismatched = Vec::new();
    for row in [&moved_epoch, &moved_slot, &orphaned] {
        mismatched.push((
            granted_holder(&registry, row.id, identity(row)).await?.0,
            row,
        ));
    }
    let foreign = GrantIdentity {
        machine_id: Uuid::new_v4(),
        ..identity(&foreign_machine)
    };
    mismatched.push((
        granted_holder(&registry, foreign_machine.id, foreign)
            .await?
            .0,
        &foreign_machine,
    ));
    let (failing_holder, _) = granted_holder(&registry, failing.id, identity(&failing)).await?;
    let (hung_holder, _) = granted_holder(&registry, hung.id, identity(&hung)).await?;
    let (dropped_holder, _) = granted_holder(&registry, dropped.id, identity(&dropped)).await?;
    assert_eq!(registry.close_admission().await.len(), 7);

    for (holder, row) in mismatched {
        let finalized = registry
            .finalize_shutdown(holder, &hub.pool)
            .await?
            .expect("mismatch finalize");
        assert_eq!(finalized.input, HostInput::Revoke);
        assert_eq!(stored_handoff(&hub, row.id).await?, None);
    }
    let raised = registry
        .finalize_shutdown(failing_holder, &FailingStore)
        .await?
        .expect("failing finalize");
    assert_eq!(raised.input, HostInput::Revoke);
    let started = Instant::now();
    let cancelled = registry
        .finalize_shutdown(hung_holder, &HungStore)
        .await?
        .expect("hung finalize");
    assert_eq!(cancelled.input, HostInput::Revoke);
    assert!(started.elapsed() >= HANDOFF_RECORD_DEADLINE);

    // Dropping shutdown finalize mid-record commits nothing, so a retry still settles it.
    assert!(
        tokio::time::timeout(
            Duration::from_millis(50),
            registry.finalize_shutdown(dropped_holder, &HungStore),
        )
        .await
        .is_err()
    );
    assert_eq!(
        registry.snapshot(dropped.id).await.holder,
        Some(dropped_holder.id)
    );
    let finalized = registry
        .finalize_shutdown(dropped_holder, &hub.pool)
        .await?
        .expect("retried finalize");
    assert_eq!(finalized.input, HostInput::Preserved);
    assert_eq!(
        stored_handoff(&hub, dropped.id).await?,
        Some(handoff(&dropped, dropped_holder.id))
    );
    let mut dropped_finalizations = 0;
    while let Ok(event) = events.try_recv() {
        if event.attachment_id == dropped_holder.id && event.kind == "finalized" {
            dropped_finalizations += 1;
        }
    }
    assert_eq!(dropped_finalizations, 1);
    Ok(())
}

#[tokio::test]
async fn handoff_expiry_never_clears_a_rebound_record() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let start = Instant::now();
    let expired = start + INPUT_HANDOFF_REBIND;

    // A replacement for another attachment, or a newer record for the same
    // attachment at another slot, lands while the expiry revoke is in flight.
    for (case, same_attachment) in [("replacement", false), ("identity mismatch", true)] {
        let row = live_row();
        hub.insert(&row).await?;
        let read = handoff(&row, Uuid::new_v4());
        assert!(record_handoff(&hub, &row, &read).await?);
        let newer = InputHandoff {
            attachment_id: if same_attachment {
                read.attachment_id
            } else {
                Uuid::new_v4()
            },
            host_terminal_id: if same_attachment {
                "ht-rebound".into()
            } else {
                slot(&row)
            },
            ..read.clone()
        };
        let revoker = Revoker {
            race: Mutex::new(Some(Race {
                pool: &hub.pool,
                terminal_id: row.id,
                replacement: newer.clone(),
            })),
            ..Revoker::default()
        };
        let mut deadlines = HandoffDeadlines::new();
        for now in [start, expired] {
            expire_input_handoffs(
                &hub.pool,
                row.machine_id,
                EPOCH,
                &mut deadlines,
                now,
                &revoker,
            )
            .await?;
        }
        assert_eq!(revoker.calls().len(), 1, "{case}");
        assert_eq!(stored_handoff(&hub, row.id).await?, Some(newer), "{case}");
    }

    // A rebind that clears the record first leaves nothing to revoke.
    let row = live_row();
    hub.insert(&row).await?;
    assert!(record_handoff(&hub, &row, &handoff(&row, Uuid::new_v4())).await?);
    let revoker = Revoker::default();
    let mut deadlines = HandoffDeadlines::new();
    expire_input_handoffs(
        &hub.pool,
        row.machine_id,
        EPOCH,
        &mut deadlines,
        start,
        &revoker,
    )
    .await?;
    hub.pool
        .transaction(None, async |tx| {
            Ok::<_, anyhow::Error>(repository::clear_input_handoff(tx, row.id, None).await?)
        })
        .await?;
    expire_input_handoffs(
        &hub.pool,
        row.machine_id,
        EPOCH,
        &mut deadlines,
        expired,
        &revoker,
    )
    .await?;
    assert!(revoker.calls().is_empty());
    assert!(deadlines.is_empty());

    // A record from a replaced host epoch carries no authority and clears unrevoked.
    let row = live_row();
    hub.insert(&row).await?;
    assert!(record_handoff(&hub, &row, &handoff(&row, Uuid::new_v4())).await?);
    let mut deadlines = HandoffDeadlines::new();
    expire_input_handoffs(
        &hub.pool,
        row.machine_id,
        "E2",
        &mut deadlines,
        start,
        &revoker,
    )
    .await?;
    assert!(revoker.calls().is_empty());
    assert_eq!(stored_handoff(&hub, row.id).await?, None);
    Ok(())
}

#[tokio::test]
async fn failed_handoff_revoke_keeps_the_record_and_retries() -> anyhow::Result<()> {
    let hub = Hub::new().await?;
    let row = live_row();
    hub.insert(&row).await?;
    let preserved = handoff(&row, Uuid::new_v4());
    assert!(record_handoff(&hub, &row, &preserved).await?);
    let revoker = Revoker {
        failures: AtomicUsize::new(1),
        ..Revoker::default()
    };
    let mut deadlines = HandoffDeadlines::new();
    let start = Instant::now();
    let expired = start + INPUT_HANDOFF_REBIND;
    for now in [start, expired] {
        expire_input_handoffs(
            &hub.pool,
            row.machine_id,
            EPOCH,
            &mut deadlines,
            now,
            &revoker,
        )
        .await?;
    }
    assert_eq!(revoker.calls().len(), 1);
    assert_eq!(stored_handoff(&hub, row.id).await?, Some(preserved.clone()));
    assert_eq!(deadlines.len(), 1);

    // The next pass retries without granting a fresh rebind window.
    expire_input_handoffs(
        &hub.pool,
        row.machine_id,
        EPOCH,
        &mut deadlines,
        expired + Duration::from_millis(1),
        &revoker,
    )
    .await?;
    let expected = (EPOCH.to_owned(), slot(&row), preserved.attachment_id);
    assert_eq!(revoker.calls(), vec![expected.clone(), expected]);
    assert_eq!(stored_handoff(&hub, row.id).await?, None);
    assert!(deadlines.is_empty());
    Ok(())
}
