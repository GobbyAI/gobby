use super::*;

fn sample_state(state: State, sampled_at_unix: i64) -> StateFile {
    StateFile {
        schema_version: SCHEMA_VERSION,
        state,
        sampled_at_unix,
        last_non_healthy_at_unix: None,
        reasons: vec!["probe_slow:1500ms".into()],
        ramp: Ramp {
            max_new_seats: 3,
            ramp_window_seconds: 900,
        },
        thresholds_source: ThresholdsSource::ProjectPolicy,
        watcher_pid: 4242,
    }
}

#[test]
fn write_then_read_round_trips_every_field() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("fleet-health.json");
    let mut written = sample_state(State::Degraded, 1_700_000_000);
    written.last_non_healthy_at_unix = Some(1_700_000_000);

    write_atomic(&path, &written).expect("write");
    assert_eq!(read(&path).expect("read"), written);
}

#[test]
fn json_keys_match_the_documented_contract() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("fleet-health.json");
    write_atomic(&path, &sample_state(State::Healthy, 1_700_000_000)).expect("write");

    let raw = std::fs::read_to_string(&path).expect("raw");
    let value: serde_json::Value = serde_json::from_str(&raw).expect("json");
    let object = value.as_object().expect("object");
    let mut keys: Vec<&str> = object.keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "last_non_healthy_at_unix",
            "ramp",
            "reasons",
            "sampled_at_unix",
            "schema_version",
            "state",
            "thresholds_source",
            "watcher_pid",
        ]
    );
    assert_eq!(object["state"], "healthy");
    assert_eq!(object["thresholds_source"], "project_policy");
    assert!(object["last_non_healthy_at_unix"].is_null());
}

#[test]
fn overwrite_replaces_the_previous_state_and_leaves_no_temporary_file() {
    let dir = tempfile::tempdir().expect("tempdir");
    let path = dir.path().join("fleet-health.json");
    write_atomic(&path, &sample_state(State::Healthy, 100)).expect("first");
    write_atomic(&path, &sample_state(State::Unstable, 130)).expect("second");

    assert_eq!(read(&path).expect("read").state, State::Unstable);
    let names: Vec<String> = std::fs::read_dir(dir.path())
        .expect("list")
        .map(|entry| {
            entry
                .expect("entry")
                .file_name()
                .to_string_lossy()
                .into_owned()
        })
        .collect();
    assert_eq!(names, vec!["fleet-health.json".to_string()]);
}

#[test]
fn reading_a_missing_file_is_an_error_the_caller_can_treat_as_unknown() {
    let dir = tempfile::tempdir().expect("tempdir");
    assert!(read(&dir.path().join("absent.json")).is_err());
}
