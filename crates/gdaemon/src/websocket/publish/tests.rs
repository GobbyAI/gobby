use std::time::Duration;

use super::bind_timing_line;

#[test]
fn a_fast_bind_logs_nothing() {
    assert_eq!(
        bind_timing_line(Duration::from_millis(499), 1_700_000_000_000),
        None
    );
}

#[test]
fn a_bind_at_the_threshold_logs_its_elapsed_time_and_answer_instant() {
    assert_eq!(
        bind_timing_line(Duration::from_millis(750), 1_700_000_000_123).as_deref(),
        Some("native WS bind answered after 750ms (unix_ms=1700000000123)"),
    );
    assert!(bind_timing_line(Duration::from_millis(500), 1).is_some());
}
