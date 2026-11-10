use serde_json::json;

use super::*;

#[test]
fn defaults_match_the_build_thresholds() {
    let defaults = Thresholds::default();
    assert_eq!(defaults.probe_degraded_ms, 1_000);
    assert_eq!(defaults.probe_unstable_ms, 3_000);
    assert_eq!(defaults.severe_unstable_window_seconds, 120);
    assert_eq!(defaults.window_seconds, 300);
    assert_eq!(defaults.warning_lines_degraded, 10);
    assert_eq!(defaults.load_per_core_degraded, 2.5);
    assert_eq!(defaults.load_consecutive_samples, 2);
    assert_eq!(defaults.memory_free_degraded_percent, 15.0);
    assert_eq!(defaults.fseventsd_cpu_degraded_percent, 50.0);
    assert_eq!(defaults.fseventsd_consecutive_samples, 3);
}

#[test]
fn empty_block_takes_every_default() {
    let parsed = Thresholds::from_fleet_health(&json!({})).expect("empty block parses");
    assert_eq!(parsed, Thresholds::default());
}

#[test]
fn partial_block_overrides_only_its_keys() {
    let parsed = Thresholds::from_fleet_health(&json!({
        "warning_lines_degraded": 5,
        "memory_free_degraded_percent": 20.0
    }))
    .expect("partial block parses");
    assert_eq!(parsed.warning_lines_degraded, 5);
    assert_eq!(parsed.memory_free_degraded_percent, 20.0);
    assert_eq!(parsed.window_seconds, 300);
    assert_eq!(parsed.probe_unstable_ms, 3_000);
}

#[test]
fn unknown_key_is_rejected() {
    let error = Thresholds::from_fleet_health(&json!({"window_second": 5}))
        .expect_err("misspelled key must fail");
    assert!(error.contains("window_second"), "{error}");
}

#[test]
fn the_retired_seat_ramp_keys_are_no_longer_thresholds() {
    for block in [
        json!({"max_new_seats": 3}),
        json!({"ramp_window_seconds": 900}),
    ] {
        assert!(Thresholds::from_fleet_health(&block).is_err(), "{block}");
    }
}

#[test]
fn non_object_block_is_rejected() {
    assert!(Thresholds::from_fleet_health(&json!(3)).is_err());
    assert!(Thresholds::from_fleet_health(&json!(["window_seconds"])).is_err());
}

#[test]
fn out_of_range_values_are_rejected() {
    for block in [
        json!({"probe_degraded_ms": 3000, "probe_unstable_ms": 3000}),
        json!({"memory_free_degraded_percent": 150.0}),
        json!({"fseventsd_cpu_degraded_percent": -1.0}),
        json!({"load_per_core_degraded": 0.0}),
        json!({"window_seconds": 0}),
        json!({"load_consecutive_samples": 0}),
        json!({"fseventsd_consecutive_samples": 0}),
        json!({"warning_lines_degraded": 0}),
    ] {
        assert!(Thresholds::from_fleet_health(&block).is_err(), "{block}");
    }
}

#[test]
fn consecutive_counts_beyond_the_retained_history_are_rejected() {
    // Defaults retain 300 s of samples at a 30 s cadence: 11 samples. A rule that needs
    // more consecutive samples than that could never fire.
    for block in [
        json!({"load_consecutive_samples": 12}),
        json!({"fseventsd_consecutive_samples": 12}),
    ] {
        assert!(Thresholds::from_fleet_health(&block).is_err(), "{block}");
    }
}

#[test]
fn consecutive_counts_within_the_retained_history_are_accepted() {
    let block = json!({"load_consecutive_samples": 11, "fseventsd_consecutive_samples": 11});
    assert!(Thresholds::from_fleet_health(&block).is_ok(), "{block}");
}
