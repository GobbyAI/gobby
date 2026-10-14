use anyhow::Result;
use gobby_core::bootstrap::{RunMode, parse_files_home_view, parse_hub_database_bootstrap};
use serde::Deserialize;
use serde_json::Value;

#[derive(Deserialize)]
struct Corpus {
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    name: String,
    bootstrap: Value,
    mode: Option<String>,
    hub_daemon_url: Option<String>,
    hub_cert: Option<String>,
    database_url: Option<String>,
    api_key: Option<String>,
    daemon_url: Option<String>,
    files_home: Option<String>,
    error: Option<String>,
}

#[test]
fn files_home_rejects_null_datastore_mode() {
    let error = gobby_core::bootstrap::parse_files_home_view(
        "datastore_mode: null\nfiles_home: /tmp/gobby-vector-files\n",
    )
    .expect_err("null datastore mode must be refused");
    assert_eq!(
        error.to_string(),
        "datastore_mode must be one of: local, remote"
    );
}

#[test]
fn shared_vectors_match_python() -> Result<()> {
    let corpus: Corpus = serde_json::from_str(include_str!(
        "../../../tests/fixtures/bootstrap_run_modes.json"
    ))?;
    let mut mismatches = Vec::new();
    for case in corpus.cases {
        let yaml = serde_yaml::to_string(&case.bootstrap)?;
        let parsed = parse_hub_database_bootstrap(&yaml);
        let actual = match parsed {
            Ok(Some(bootstrap)) => {
                let mode = match bootstrap.mode {
                    RunMode::Standalone => "standalone",
                    RunMode::Hub => "hub",
                    RunMode::Node => "node",
                };
                Ok((
                    Some(mode.to_owned()),
                    bootstrap.hub_daemon_url,
                    bootstrap.hub_cert,
                    bootstrap.database_url,
                    bootstrap.api_key,
                    bootstrap.daemon_url,
                ))
            }
            Ok(None) => Err("expected nonempty bootstrap mapping".to_owned()),
            Err(error) => Err(error.to_string()),
        };
        let expected = match case.error {
            Some(error) => Err(error),
            None => Ok((
                case.mode,
                case.hub_daemon_url,
                case.hub_cert,
                case.database_url,
                case.api_key,
                case.daemon_url,
            )),
        };
        if actual != expected {
            mismatches.push(format!(
                "{}: expected {expected:?}, got {actual:?}",
                case.name
            ));
        }
        if let Some(expected_path) = case.files_home {
            let view = parse_files_home_view(&yaml)?;
            let actual_path = view
                .files_home
                .and_then(|path| path.to_str().map(str::to_owned));
            if actual_path.as_deref() != Some(expected_path.as_str()) {
                mismatches.push(format!(
                    "{}: expected files_home {expected_path:?}, got {actual_path:?}",
                    case.name
                ));
            }
        }
    }
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
    Ok(())
}

#[test]
fn blank_daemon_url_falls_back_to_local_endpoint() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("bootstrap.yaml");
    for value in ["", " \t\r\n ", "\u{1c}", "\u{1d}", "\u{1e}", "\u{1f}"] {
        let yaml = serde_yaml::to_string(&serde_json::json!({
            "daemon_url": value,
            "bind_host": "localhost",
            "daemon_port": 61234,
        }))?;
        std::fs::write(&path, yaml)?;
        assert_eq!(
            gobby_core::daemon_url::daemon_url_at(&path)?,
            "http://127.0.0.1:61234",
            "blank daemon_url {value:?} must use the local endpoint"
        );
    }
    Ok(())
}

#[test]
fn files_home_invalid_ports_name_field_and_range() {
    for port in ["+443", "-443", "44x", "٤٤٣", "65536", "4'4", "443:80"] {
        let yaml = format!("datastore_mode: remote\nhub_daemon_url: https://hub.example:{port}\n");
        let error = gobby_core::bootstrap::parse_files_home_view(&yaml).unwrap_err();
        assert_eq!(
            error.to_string(),
            "hub_daemon_url port must be an integer between 0 and 65535",
            "{port}"
        );
    }
}

#[test]
fn files_home_non_string_names_expected_type() {
    for value in ["42", "true", "[]", "{}"] {
        let yaml = format!("files_home: {value}\n");
        let error = gobby_core::bootstrap::parse_files_home_view(&yaml).unwrap_err();
        assert_eq!(error.to_string(), "files_home must be a string", "{value}");
    }
}

#[test]
fn files_home_non_boolean_hub_is_refused() {
    let error = gobby_core::bootstrap::parse_files_home_view(
        "files_home: /tmp/gobby-vector-files\nhub: maybe\n",
    )
    .unwrap_err();
    assert_eq!(error.to_string(), "hub must be a boolean");
}

#[test]
fn files_home_remote_hub_is_refused() {
    for hub in ["true", "yes", "ON"] {
        let yaml =
            format!("datastore_mode: remote\nhub: {hub}\nhub_daemon_url: https://hub.example\n");
        let error = gobby_core::bootstrap::parse_files_home_view(&yaml).unwrap_err();
        assert_eq!(
            error.to_string(),
            "hub: true requires datastore_mode: local",
            "{hub}"
        );
    }
}
