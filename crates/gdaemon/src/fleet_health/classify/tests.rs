use super::*;

const START: i64 = 1_000_000;
const STEP: i64 = 30;

fn quiet_host() -> HostSignals {
    HostSignals {
        load_per_core: Some(0.8),
        memory_free_percent: Some(60.0),
        fseventsd_cpu_percent: Some(2.0),
        syspolicyd_cpu_percent: Some(1.0),
        mds_stores_cpu_percent: Some(0.5),
        rustc_running: Some(false),
    }
}

fn sample(index: usize) -> Sample {
    Sample {
        at_unix: START + STEP * index as i64,
        probe: Probe::Responded {
            status: 200,
            elapsed_ms: 120,
        },
        severe_lines: 0,
        warning_lines: 0,
        host: quiet_host(),
    }
}

/// Ten quiet samples; `now` is the last sample's time.
fn quiet_series() -> Vec<Sample> {
    (0..10).map(sample).collect()
}

fn now_of(samples: &[Sample]) -> i64 {
    samples.last().expect("series is non-empty").at_unix
}

fn classify_series(samples: &[Sample]) -> Verdict {
    classify(samples, &Thresholds::default(), now_of(samples))
}

fn last_mut(samples: &mut [Sample]) -> &mut Sample {
    samples.last_mut().expect("series is non-empty")
}

#[test]
fn quiet_series_is_healthy_with_no_reasons() {
    let verdict = classify_series(&quiet_series());
    assert_eq!(verdict.state, State::Healthy);
    assert!(verdict.reasons.is_empty(), "{:?}", verdict.reasons);
}

#[test]
fn no_samples_is_unstable() {
    let verdict = classify(&[], &Thresholds::default(), START);
    assert_eq!(verdict.state, State::Unstable);
    assert!(
        verdict
            .reasons
            .iter()
            .any(|reason| reason.contains("no_samples"))
    );
}

#[test]
fn probe_status_other_than_200_is_unstable() {
    let mut samples = quiet_series();
    last_mut(&mut samples).probe = Probe::Responded {
        status: 503,
        elapsed_ms: 80,
    };
    let verdict = classify_series(&samples);
    assert_eq!(verdict.state, State::Unstable);
    assert!(verdict.reasons.iter().any(|reason| reason.contains("503")));
}

#[test]
fn failed_probe_is_unstable() {
    let mut samples = quiet_series();
    last_mut(&mut samples).probe = Probe::Failed {
        elapsed_ms: 5,
        reason: "connection refused".into(),
    };
    assert_eq!(classify_series(&samples).state, State::Unstable);
}

#[test]
fn probe_at_three_seconds_is_not_unstable_but_above_is() {
    let mut at_limit = quiet_series();
    last_mut(&mut at_limit).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 3_000,
    };
    assert_eq!(classify_series(&at_limit).state, State::Degraded);

    let mut over_limit = quiet_series();
    last_mut(&mut over_limit).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 3_001,
    };
    assert_eq!(classify_series(&over_limit).state, State::Unstable);
}

#[test]
fn probe_over_one_second_is_degraded_and_exactly_one_second_is_healthy() {
    let mut at_limit = quiet_series();
    last_mut(&mut at_limit).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 1_000,
    };
    assert_eq!(classify_series(&at_limit).state, State::Healthy);

    let mut over_limit = quiet_series();
    last_mut(&mut over_limit).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 1_001,
    };
    let verdict = classify_series(&over_limit);
    assert_eq!(verdict.state, State::Degraded);
    assert!(
        verdict
            .reasons
            .iter()
            .any(|reason| reason.contains("probe"))
    );
}

#[test]
fn severe_line_inside_two_minutes_is_unstable() {
    let mut samples = quiet_series();
    samples[7].severe_lines = 1; // 60 seconds before the newest sample
    let verdict = classify_series(&samples);
    assert_eq!(verdict.state, State::Unstable);
    assert!(
        verdict
            .reasons
            .iter()
            .any(|reason| reason.contains("severe"))
    );
}

#[test]
fn severe_line_older_than_two_minutes_but_inside_five_is_degraded() {
    let mut samples = quiet_series();
    samples[4].severe_lines = 1; // 150 seconds before the newest sample
    assert_eq!(classify_series(&samples).state, State::Degraded);
}

#[test]
fn severe_line_outside_five_minutes_is_ignored() {
    let mut samples: Vec<Sample> = (0..14).map(sample).collect();
    samples[2].severe_lines = 1; // 330 seconds before the newest sample
    assert_eq!(classify_series(&samples).state, State::Healthy);
}

#[test]
fn ten_warning_lines_in_five_minutes_degrade_and_nine_do_not() {
    let mut nine = quiet_series();
    nine[6].warning_lines = 4;
    nine[8].warning_lines = 5;
    assert_eq!(classify_series(&nine).state, State::Healthy);

    let mut ten = quiet_series();
    ten[6].warning_lines = 4;
    ten[8].warning_lines = 6;
    let verdict = classify_series(&ten);
    assert_eq!(verdict.state, State::Degraded);
    assert!(
        verdict
            .reasons
            .iter()
            .any(|reason| reason.contains("warning"))
    );
}

#[test]
fn load_over_two_and_a_half_per_core_on_two_consecutive_samples_degrades() {
    let mut consecutive = quiet_series();
    consecutive[8].host.load_per_core = Some(2.6);
    consecutive[9].host.load_per_core = Some(2.6);
    assert_eq!(classify_series(&consecutive).state, State::Degraded);

    let mut single = quiet_series();
    single[9].host.load_per_core = Some(2.6);
    assert_eq!(classify_series(&single).state, State::Healthy);

    let mut at_limit = quiet_series();
    at_limit[8].host.load_per_core = Some(2.5);
    at_limit[9].host.load_per_core = Some(2.5);
    assert_eq!(classify_series(&at_limit).state, State::Healthy);
}

#[test]
fn memory_free_under_fifteen_percent_degrades() {
    let mut under = quiet_series();
    last_mut(&mut under).host.memory_free_percent = Some(14.9);
    let verdict = classify_series(&under);
    assert_eq!(verdict.state, State::Degraded);
    assert!(
        verdict
            .reasons
            .iter()
            .any(|reason| reason.contains("memory"))
    );

    let mut at_limit = quiet_series();
    last_mut(&mut at_limit).host.memory_free_percent = Some(15.0);
    assert_eq!(classify_series(&at_limit).state, State::Healthy);
}

#[test]
fn fseventsd_over_half_a_core_for_three_samples_without_rustc_degrades() {
    let mut hot = quiet_series();
    for sample in &mut hot[7..10] {
        sample.host.fseventsd_cpu_percent = Some(88.0);
    }
    assert_eq!(classify_series(&hot).state, State::Degraded);

    let mut two_only = quiet_series();
    for sample in &mut two_only[8..10] {
        sample.host.fseventsd_cpu_percent = Some(88.0);
    }
    assert_eq!(classify_series(&two_only).state, State::Healthy);

    let mut building = quiet_series();
    for sample in &mut building[7..10] {
        sample.host.fseventsd_cpu_percent = Some(88.0);
        sample.host.rustc_running = Some(true);
    }
    assert_eq!(classify_series(&building).state, State::Healthy);
}

#[test]
fn unreadable_host_signals_never_trip_a_rule() {
    let mut samples = quiet_series();
    for sample in &mut samples {
        sample.host = HostSignals::default();
    }
    assert_eq!(classify_series(&samples).state, State::Healthy);
}

#[test]
fn unstable_rule_wins_over_degraded_rule() {
    let mut samples = quiet_series();
    last_mut(&mut samples).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 1_500,
    };
    samples[9].severe_lines = 1;
    assert_eq!(classify_series(&samples).state, State::Unstable);
}

#[test]
fn reasons_name_every_rule_that_tripped() {
    let mut samples = quiet_series();
    last_mut(&mut samples).probe = Probe::Responded {
        status: 200,
        elapsed_ms: 1_500,
    };
    last_mut(&mut samples).host.memory_free_percent = Some(10.0);
    let verdict = classify_series(&samples);
    assert_eq!(verdict.state, State::Degraded);
    assert_eq!(verdict.reasons.len(), 2, "{:?}", verdict.reasons);
}

#[test]
fn severe_log_classes_are_recognised() {
    for line in [
        "2026-11-07 14:02:11 ERROR Database saturation boundary exceeded",
        "config-db db timed out after 5s",
        "Native WS publish failed for session abc",
        "Native WS rebind failed",
        "Mailbox wake dispatch cancelled",
    ] {
        assert_eq!(classify_log_line(line), Some(LogSeverity::Severe), "{line}");
    }
}

#[test]
fn config_db_timeouts_are_severe_for_every_lane_label() {
    // The daemon labels the lane (db, read, publish) in its timeout line.
    for line in [
        "config-db read timed out: admission=0.000s queued=0.000s running=5.001s",
        "config-db publish timed out: admission=0.000s queued=not-started running=-",
    ] {
        assert_eq!(classify_log_line(line), Some(LogSeverity::Severe), "{line}");
    }
    assert_eq!(classify_log_line("config-db read completed in 0.2s"), None);
}

#[test]
fn warning_log_classes_are_recognised() {
    for line in [
        "WARN Slow workspace snapshot took 4.1s",
        "WARN Slow workspace attach",
        "WARN Slow terminal attach",
        "WARN Inline mcp_call gobby-tasks:list_tasks timed out",
    ] {
        assert_eq!(
            classify_log_line(line),
            Some(LogSeverity::Warning),
            "{line}"
        );
    }
}

#[test]
fn ordinary_log_lines_are_not_fleet_signals() {
    for line in [
        "INFO Native WS connected",
        "INFO Inline mcp_call gobby-tasks:list_tasks succeeded",
        "INFO Slow-start guard released",
        "",
    ] {
        assert_eq!(classify_log_line(line), None, "{line}");
    }
}
