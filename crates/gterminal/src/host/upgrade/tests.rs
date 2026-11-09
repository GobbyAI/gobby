use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use super::*;
use crate::host::config::HostConfig;
use crate::host::handover::{UpgradeAttempt, UpgradeOutcome, UpgradeReason, UpgradeRecord};
use crate::host::image::PinnedImage;

/// A host whose last attempt was aborted while draining; nothing has read it yet.
fn recorded_host() -> Arc<HostState> {
    let (shutdown, _) = tokio::sync::watch::channel(false);
    let state = HostState::new(
        HostConfig::default(),
        "control".into(),
        "epoch".into(),
        PinnedImage::for_tests(),
        1,
        shutdown,
    );
    *state.upgrade.lock().unwrap() = Some(UpgradeRecord {
        attempt: UpgradeAttempt {
            attempt_id: "attempt-drain".into(),
            candidate_sha256: "candidate".into(),
            previous_sha256: "previous".into(),
        },
        outcome: Some(UpgradeOutcome::Aborted(UpgradeReason::HostDraining)),
    });
    state
}

/// A drain that starts while an attempt holds `upgrade_lock` must not tear the
/// endpoint down until the recorded outcome has been served.
#[tokio::test(start_paused = true)]
async fn drain_waits_for_outcome_served() {
    let state = recorded_host();
    let events = Arc::new(StdMutex::new(Vec::<&'static str>::new()));
    let held = Arc::clone(&state.attempts.lock)
        .try_lock_owned()
        .expect("upgrade lock is free before the drain");
    let drain = tokio::spawn({
        let state = Arc::clone(&state);
        let events = Arc::clone(&events);
        async move {
            let _settled = drain_endpoint(&state, || {
                events.lock().unwrap().push("teardown");
            })
            .await;
        }
    });
    drop(held);
    for _ in 0..50 {
        tokio::task::yield_now().await;
    }
    let drained_early = drain.is_finished();
    let before_served = events.lock().unwrap().clone();
    state.attempts.mark_outcome_served();
    events.lock().unwrap().push("served");
    tokio::time::timeout(Duration::from_secs(5), drain)
        .await
        .expect("drain never reached teardown after the outcome was served")
        .expect("drain task panicked");
    let order = events.lock().unwrap().clone();
    assert!(
        !drained_early,
        "drain finished before the outcome was served"
    );
    assert!(
        !before_served.contains(&"teardown"),
        "endpoint torn down before the outcome was served: {before_served:?}"
    );
    assert_eq!(
        order,
        vec!["served", "teardown"],
        "outcome must be served before drain teardown"
    );
}

/// With no client read, the drain waits at most `SERVE_GRACE`, tears down, and
/// the outcome stays recorded for any request that still reaches the host.
#[tokio::test(start_paused = true)]
async fn drain_stops_waiting_after_the_grace_and_keeps_the_outcome() {
    let state = recorded_host();
    let events = Arc::new(StdMutex::new(Vec::<&'static str>::new()));
    let started = tokio::time::Instant::now();
    let _settled = drain_endpoint(&state, || events.lock().unwrap().push("teardown")).await;
    assert!(
        started.elapsed() >= SERVE_GRACE,
        "drain tore down before the serve grace expired"
    );
    assert_eq!(*events.lock().unwrap(), vec!["teardown"]);
    assert!(
        state
            .upgrade
            .lock()
            .unwrap()
            .as_ref()
            .is_some_and(|record| record.outcome.is_some()),
        "the outcome must stay recorded after teardown"
    );
}

/// A reply that carries the recorded outcome counts as served, so a drain that
/// starts afterwards does not wait for the grace.
#[tokio::test(start_paused = true)]
async fn served_outcome_ends_the_drain_wait_early() {
    let state = recorded_host();
    let reply = state.with_upgrade(json!({"ok": true}));
    assert_eq!(reply["upgrade"]["last_outcome"]["outcome"], "aborted");
    let written = state
        .served_reply(&reply)
        .expect("a reply with a recorded outcome carries a hook");
    (written.0)();
    let started = tokio::time::Instant::now();
    let _settled = drain_endpoint(&state, || {}).await;
    assert!(
        started.elapsed() < SERVE_GRACE,
        "drain waited {:?} for an outcome that was already served",
        started.elapsed()
    );
}

/// A reply whose write never completes leaves the outcome unserved, so the drain
/// waits the full grace.
#[tokio::test(start_paused = true)]
async fn unwritten_reply_leaves_the_outcome_unserved() {
    let state = recorded_host();
    let reply = state.with_upgrade(json!({"ok": true}));
    drop(state.served_reply(&reply));
    let started = tokio::time::Instant::now();
    let _settled = drain_endpoint(&state, || {}).await;
    assert!(
        started.elapsed() >= SERVE_GRACE,
        "drain skipped the grace for a reply that was never written"
    );
}

/// A reply written for an older record must not mark a newer one.
#[tokio::test(start_paused = true)]
async fn stale_reply_does_not_mark_a_newer_outcome() {
    let state = recorded_host();
    let reply = state.with_upgrade(json!({"ok": true}));
    let written = state
        .served_reply(&reply)
        .expect("a reply with a recorded outcome carries a hook");
    *state.upgrade.lock().unwrap() = Some(UpgradeRecord {
        attempt: UpgradeAttempt {
            attempt_id: "attempt-newer".into(),
            candidate_sha256: "candidate".into(),
            previous_sha256: "previous".into(),
        },
        outcome: Some(UpgradeOutcome::Aborted(UpgradeReason::HostDraining)),
    });
    (written.0)();
    let started = tokio::time::Instant::now();
    let _settled = drain_endpoint(&state, || {}).await;
    assert!(
        started.elapsed() >= SERVE_GRACE,
        "a reply for an older record marked the newer outcome served"
    );
}
