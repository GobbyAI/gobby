use super::lock_reporting;
use std::{sync::Mutex as StdMutex, time::Duration};
use tokio::{sync::Mutex, time::timeout};

/// A lease lock held by another task is reported each interval while it stays held, and taking
/// it succeeds once the holder releases it.
#[tokio::test]
async fn lock_reporting_reports_each_interval_until_the_lock_is_released() {
    let lock = Mutex::new(7u8);
    let held = lock.lock().await;
    let lines = StdMutex::new(Vec::<String>::new());
    let reporter = |line: &str| lines.lock().expect("report sink").push(line.to_owned());

    let waited = timeout(
        Duration::from_millis(250),
        lock_reporting(&lock, Duration::from_millis(20), &reporter),
    )
    .await;
    assert!(
        waited.is_err(),
        "the wait ends only when the lock is released"
    );
    let reports = lines.lock().expect("report sink").clone();
    assert!(
        reports.len() >= 3,
        "expected a report each interval: {reports:?}"
    );
    assert!(
        reports.iter().all(|line| line.contains("lease lock")),
        "{reports:?}"
    );

    drop(held);
    let guard = lock_reporting(&lock, Duration::from_millis(20), &reporter).await;
    assert_eq!(*guard, 7);
}
