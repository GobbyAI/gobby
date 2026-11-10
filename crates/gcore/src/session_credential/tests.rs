use super::*;
use serde_json::json;

fn credential() -> SessionCredential {
    SessionCredential {
        token: "gobby-agent-v1.payload.signature".into(),
        session_id: "11111111-1111-4111-8111-111111111111".into(),
        project_id: "22222222-2222-4222-8222-222222222222".into(),
        provider_session_id: "provider-abc".into(),
    }
}

#[test]
fn extract_strips_the_credential_and_keeps_the_rest() {
    let body = json!({
        "continue": true,
        "_gobby_session_credential": {
            "token": "gobby-agent-v1.payload.signature",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "project_id": "22222222-2222-4222-8222-222222222222"
        }
    })
    .to_string();

    let (stripped, found) = extract(&body);

    let found = found.expect("credential is extracted");
    assert_eq!(found.token, "gobby-agent-v1.payload.signature");
    assert_eq!(found.session_id, "11111111-1111-4111-8111-111111111111");
    let rest: serde_json::Value = serde_json::from_str(&stripped).unwrap();
    assert_eq!(rest, json!({"continue": true}));
}

#[test]
fn extract_leaves_a_body_without_credential_untouched() {
    let body = r#"{"continue":true}"#;

    let (stripped, found) = extract(body);

    assert!(found.is_none());
    assert_eq!(stripped, body);
    assert!(matches!(stripped, Cow::Borrowed(_)));
}

#[test]
fn cli_pid_key_names_the_golden_record_file() {
    assert_eq!(cli_pid_key(4242), "cli-pid:4242");
    assert_eq!(
        record_path(Path::new("/home"), &cli_pid_key(4242))
            .file_name()
            .and_then(|name| name.to_str()),
        Some("d94d9bec-bb52-5978-b9f8-1c9d4c49d457.json")
    );
}

#[test]
fn store_then_load_round_trips_under_each_key() {
    let home = tempfile::tempdir().unwrap();
    let keys = vec![provider_key("provider-abc"), cli_pid_key(4242)];

    store(home.path(), &keys, &credential()).unwrap();

    for key in &keys {
        assert_eq!(load(home.path(), key), Some(credential()), "key {key}");
    }
}

#[cfg(unix)]
#[test]
fn records_are_owner_only() {
    use std::os::unix::fs::PermissionsExt;

    let home = tempfile::tempdir().unwrap();
    let key = provider_key("provider-abc");
    store(home.path(), std::slice::from_ref(&key), &credential()).unwrap();

    let mode = std::fs::metadata(record_path(home.path(), &key))
        .unwrap()
        .permissions()
        .mode();
    assert_eq!(mode & 0o777, 0o600);
}

#[test]
fn provider_binding_refuses_a_different_provider_session() {
    let home = tempfile::tempdir().unwrap();
    store(home.path(), &[provider_key("provider-abc")], &credential()).unwrap();

    assert_eq!(
        bound_for_provider(home.path(), "provider-abc"),
        Some(credential())
    );
    assert!(bound_for_provider(home.path(), "provider-other").is_none());
}

#[test]
fn blank_managed_marker_is_not_managed() {
    assert!(marker_is_set(std::ffi::OsStr::new("run-1")));
    assert!(!marker_is_set(std::ffi::OsStr::new("")));
    assert!(!marker_is_set(std::ffi::OsStr::new("   ")));
}

#[test]
fn managed_environment_is_any_spawned_run_marker() {
    assert!(!managed_environment_with(|_| false));
    assert!(managed_environment_with(|name| name == "GOBBY_AGENT_RUN_ID"));
    assert!(managed_environment_with(
        |name| name == "GOBBY_MANAGED_EXECUTION_BOOTSTRAP"
    ));
    assert!(managed_environment_with(
        |name| name == "GOBBY_AGENT_API_TOKEN"
    ));
    assert!(!managed_environment_with(|name| name == "GOBBY_SESSION_ID"));
}

#[test]
fn debug_never_prints_the_token() {
    let rendered = format!("{:?}", credential());

    assert!(!rendered.contains("gobby-agent-v1.payload.signature"));
    assert!(rendered.contains("<redacted>"));
}
