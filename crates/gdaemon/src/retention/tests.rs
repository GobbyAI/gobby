use super::*;

#[path = "tests/database.rs"]
mod database;

#[test]
fn daily_schedule_uses_next_0330_utc() {
    for (second, expected) in [
        (0, DAILY_RUN_SECONDS),
        (DAILY_RUN_SECONDS - 1, 1),
        (DAILY_RUN_SECONDS, DAY_SECONDS),
        (DAY_SECONDS - 1, DAILY_RUN_SECONDS + 1),
    ] {
        assert_eq!(
            until_daily_run(UNIX_EPOCH + Duration::from_secs(second)),
            Duration::from_secs(expected)
        );
    }
}

#[tokio::test(start_paused = true)]
async fn between_batch_yield_waits_100_milliseconds() {
    let (_sender, mut stop) = watch::channel(false);
    let start = tokio::time::Instant::now();
    assert!(!wait_or_stop(&mut stop, BATCH_YIELD).await);
    assert_eq!(start.elapsed(), Duration::from_millis(100));
}

#[tokio::test(start_paused = true)]
async fn requested_shutdown_skips_waiting_for_next_batch() {
    let (sender, mut stop) = watch::channel(false);
    sender.send_replace(true);
    let start = tokio::time::Instant::now();
    assert!(wait_or_stop(&mut stop, BATCH_YIELD).await);
    assert_eq!(start.elapsed(), Duration::ZERO);
}

#[tokio::test(start_paused = true)]
async fn dropped_shutdown_owner_stops_the_worker() {
    let (sender, mut stop) = watch::channel(false);
    drop(sender);
    assert!(wait_or_stop(&mut stop, BATCH_YIELD).await);
}
