use std::time::{Duration, Instant};

use super::{ExitTails, CAPACITY, RETENTION};

#[test]
fn final_text_is_readable_until_the_retention_window_lapses() {
    let retired = Instant::now();
    let mut tails = ExitTails::default();
    tails.record("ht-1", "boom".to_owned(), retired);

    assert_eq!(
        tails.get("ht-1", retired + RETENTION - Duration::from_millis(1)),
        Some("boom")
    );
    assert_eq!(tails.get("ht-1", retired + RETENTION), None);
}

#[test]
fn the_oldest_retired_terminal_is_dropped_first() {
    let now = Instant::now();
    let mut tails = ExitTails::default();
    for index in 0..=CAPACITY {
        tails.record(&format!("ht-{index}"), index.to_string(), now);
    }

    assert_eq!(tails.get("ht-0", now), None);
    let newest = CAPACITY.to_string();
    assert_eq!(
        tails.get(&format!("ht-{CAPACITY}"), now),
        Some(newest.as_str())
    );
}
