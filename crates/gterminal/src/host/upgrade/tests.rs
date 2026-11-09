use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use super::*;
use crate::host::config::HostConfig;
use crate::host::handover::{UpgradeAttempt, UpgradeOutcome, UpgradeReason, UpgradeRecord};
use crate::host::image::PinnedImage;

/// A drain that starts while an attempt holds `upgrade_lock` must not tear the
/// endpoint down until the recorded outcome has been served.
#[tokio::test(start_paused = true)]
async fn drain_waits_for_outcome_served() {
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
