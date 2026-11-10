use std::time::{Duration, UNIX_EPOCH};

use super::{bind_timing_line, unix_ms};

#[test]
fn a_fast_bind_logs_nothing() {
    assert_eq!(
        bind_timing_line(Duration::from_millis(499), Some(1_700_000_000_000)),
        None
    );
}

#[test]
fn a_bind_at_the_threshold_logs_its_elapsed_time_and_answer_instant() {
    assert_eq!(
        bind_timing_line(Duration::from_millis(750), Some(1_700_000_000_123)).as_deref(),
        Some("native WS bind answered after 750ms (unix_ms=1700000000123)"),
    );
    assert!(bind_timing_line(Duration::from_millis(500), Some(1)).is_some());
}

#[test]
fn a_slow_bind_whose_clock_predates_the_epoch_logs_the_instant_as_unavailable() {
    assert_eq!(
        bind_timing_line(Duration::from_millis(750), None).as_deref(),
        Some("native WS bind answered after 750ms (unix_ms=unavailable)"),
    );
}

#[test]
fn the_answer_instant_is_milliseconds_since_the_epoch_or_none_before_it() {
    assert_eq!(
        unix_ms(UNIX_EPOCH + Duration::from_millis(1_500)),
        Some(1_500)
    );
    assert_eq!(unix_ms(UNIX_EPOCH - Duration::from_secs(1)), None);
}
