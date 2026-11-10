use super::*;
use crate::fleet_health::classify::{HostSignals, Probe, State};

const START: i64 = 2_000_000;

fn sample_at(index: i64, elapsed_ms: u64) -> Sample {
    Sample {
        at_unix: START + 30 * index,
        probe: Probe::Responded {
            status: 200,
            elapsed_ms,
        },
        severe_lines: 0,
        warning_lines: 0,
        host: HostSignals {
            load_per_core: Some(0.5),
            memory_free_percent: Some(60.0),
            fseventsd_cpu_percent: Some(1.0),
            syspolicyd_cpu_percent: Some(1.0),
            mds_stores_cpu_percent: Some(1.0),
            rustc_running: Some(false),
        },
    }
}

#[test]
fn a_failed_policy_read_keeps_the_last_good_thresholds() {
    let mut cache = ThresholdCache::default();
    let tuned = Thresholds {
        warning_lines_degraded: 7,
        ..Thresholds::default()
    };
    assert_eq!(
        cache.resolve(Ok(Some(tuned.clone()))),
        (tuned.clone(), ThresholdsSource::ProjectPolicy)
    );
    assert_eq!(
        cache.resolve(Err("unreachable".into())),
        (tuned, ThresholdsSource::LastGood)
    );
}

#[test]
fn a_failed_first_policy_read_falls_back_to_the_defaults() {
    let mut cache = ThresholdCache::default();
    assert_eq!(
        cache.resolve(Err("unreachable".into())),
        (Thresholds::default(), ThresholdsSource::Defaults)
    );
}

#[test]
fn a_policy_without_a_fleet_block_reports_the_defaults_and_drops_the_last_good() {
    let mut cache = ThresholdCache::default();
    let tuned = Thresholds {
        warning_lines_degraded: 7,
        ..Thresholds::default()
    };
    cache.resolve(Ok(Some(tuned)));
    assert_eq!(
        cache.resolve(Ok(None)),
        (Thresholds::default(), ThresholdsSource::Defaults)
    );
    assert_eq!(
        cache.resolve(Err("unreachable".into())),
        (Thresholds::default(), ThresholdsSource::Defaults)
    );
}

#[test]
fn config_paths_live_under_gobby_home_and_the_logs_dir() {
    let config = WatchConfig::under_home(
        std::path::Path::new("/home/gobby"),
        std::path::Path::new("/var/log/gobby"),
        "127.0.0.1",
        60887,
        None,
        None,
    );
    assert_eq!(
        config.state_file,
        PathBuf::from("/home/gobby/state/fleet-health.json")
    );
    assert_eq!(
        config.daemon_log,
        PathBuf::from("/var/log/gobby/daemon.log")
    );
    assert_eq!(config.daemon_host, "127.0.0.1");
    assert_eq!(config.daemon_port, 60887);
    assert_eq!(config.sample_interval, SAMPLE_INTERVAL);
}

#[tokio::test]
async fn one_shot_run_writes_a_state_file_with_the_probe_verdict() {
    let dir = tempfile::tempdir().expect("tempdir");
    let state_path = dir.path().join("state").join("fleet-health.json");
    let config = WatchConfig {
        state_file: state_path.clone(),
        daemon_log: dir.path().join("logs").join("daemon.log"),
        daemon_host: "127.0.0.1".into(),
        // Nothing listens on port 1, so the probe fails and the state is unstable.
        daemon_port: 1,
        database_url: None,
        project_id: None,
        sample_interval: SAMPLE_INTERVAL,
    };
    run(config, true).await.expect("one sample");

    let state = crate::fleet_health::state_file::read(&state_path).expect("state file");
    assert_eq!(state.state, State::Unstable);
    assert_eq!(state.thresholds_source, ThresholdsSource::Defaults);
    assert_eq!(state.watcher_pid, std::process::id());
    assert!(
        state
            .reasons
            .iter()
            .any(|reason| reason.starts_with("probe_failed")),
        "{:?}",
        state.reasons
    );
}

/// A config whose samples fail fast: nothing listens on port 1, and the policy is not read.
fn failing_config(daemon_log: PathBuf, state_file: PathBuf) -> WatchConfig {
    WatchConfig {
        state_file,
        daemon_log,
        daemon_host: "127.0.0.1".into(),
        daemon_port: 1,
        database_url: None,
        project_id: None,
        sample_interval: Duration::from_millis(10),
    }
}

#[tokio::test]
async fn a_one_shot_run_reports_an_unreadable_log_as_an_error() {
    let dir = tempfile::tempdir().expect("tempdir");
    // A directory cannot be read as a log file, so the log poll fails.
    let log = dir.path().join("logs");
    std::fs::create_dir_all(&log).expect("log dir");
    let config = failing_config(log, dir.path().join("fleet-health.json"));
    assert!(run(config, true).await.is_err());
}

#[tokio::test]
async fn an_unreadable_log_does_not_stop_the_sampling_loop() {
    let dir = tempfile::tempdir().expect("tempdir");
    let log = dir.path().join("logs");
    std::fs::create_dir_all(&log).expect("log dir");
    let config = failing_config(log, dir.path().join("fleet-health.json"));
    // The loop never ends on its own. Still running after the window means every
    // failed sample was skipped rather than fatal.
    let outcome = tokio::time::timeout(Duration::from_millis(400), run(config, false)).await;
    assert!(
        outcome.is_err(),
        "the loop exited on a failed sample: {outcome:?}"
    );
}

#[tokio::test]
async fn a_failed_state_write_does_not_stop_the_sampling_loop() {
    let dir = tempfile::tempdir().expect("tempdir");
    // A regular file where the state directory should be makes every write fail.
    let blocker = dir.path().join("blocker");
    std::fs::write(&blocker, b"not a directory").expect("blocker");
    let config = failing_config(
        dir.path().join("logs").join("daemon.log"),
        blocker.join("fleet-health.json"),
    );
    let outcome = tokio::time::timeout(Duration::from_millis(400), run(config, false)).await;
    assert!(
        outcome.is_err(),
        "the loop exited on a failed write: {outcome:?}"
    );
}

#[test]
fn healthy_record_carries_no_non_healthy_timestamp() {
    let mut watcher = Watcher::new();
    let state = watcher.record(
        sample_at(0, 100),
        &Thresholds::default(),
        ThresholdsSource::Defaults,
        77,
    );
    assert_eq!(state.state, State::Healthy);
    assert_eq!(state.last_non_healthy_at_unix, None);
    assert_eq!(state.sampled_at_unix, START);
    assert_eq!(state.watcher_pid, 77);
    assert_eq!(state.thresholds_source, ThresholdsSource::Defaults);
}

#[test]
fn last_non_healthy_time_survives_recovery() {
    let thresholds = Thresholds::default();
    let mut watcher = Watcher::new();
    watcher.record(
        sample_at(0, 100),
        &thresholds,
        ThresholdsSource::ProjectPolicy,
        1,
    );

    let degraded = watcher.record(
        sample_at(1, 1_500),
        &thresholds,
        ThresholdsSource::ProjectPolicy,
        1,
    );
    assert_eq!(degraded.state, State::Degraded);
    assert_eq!(degraded.last_non_healthy_at_unix, Some(START + 30));

    let recovered = watcher.record(
        sample_at(2, 100),
        &thresholds,
        ThresholdsSource::ProjectPolicy,
        1,
    );
    assert_eq!(recovered.state, State::Healthy);
    assert_eq!(recovered.last_non_healthy_at_unix, Some(START + 30));
}

#[test]
fn samples_older_than_the_window_are_dropped() {
    let thresholds = Thresholds::default();
    let mut watcher = Watcher::new();
    for index in 0..40 {
        watcher.record(
            sample_at(index, 100),
            &thresholds,
            ThresholdsSource::ProjectPolicy,
            1,
        );
    }
    // A 300-second window at 30-second samples keeps the newest 11 samples.
    assert_eq!(watcher.sample_count(), 11);
}

#[test]
fn reasons_from_the_newest_sample_reach_the_state_file() {
    let thresholds = Thresholds::default();
    let mut watcher = Watcher::new();
    let state = watcher.record(
        sample_at(0, 3_500),
        &thresholds,
        ThresholdsSource::LastGood,
        1,
    );
    assert_eq!(state.state, State::Unstable);
    assert!(
        state
            .reasons
            .iter()
            .any(|reason| reason.contains("probe_slow"))
    );
    assert_eq!(state.thresholds_source, ThresholdsSource::LastGood);
}
