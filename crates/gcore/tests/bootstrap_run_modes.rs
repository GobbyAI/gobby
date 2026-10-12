use anyhow::Result;
use gobby_core::bootstrap::{RunMode, parse_hub_database_bootstrap};
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
    for case in corpus.cases {
        let yaml = serde_yaml::to_string(&case.bootstrap)?;
        let parsed = parse_hub_database_bootstrap(&yaml);
        if let Some(error) = case.error {
            assert_eq!(
                parsed.expect_err(&case.name).to_string(),
                error,
                "{}",
                case.name
            );
        } else {
            let bootstrap = parsed
                .expect(&case.name)
                .expect("nonempty bootstrap mapping");
            let mode = match bootstrap.mode {
                RunMode::Standalone => "standalone",
                RunMode::Hub => "hub",
                RunMode::Node => "node",
            };
            assert_eq!(Some(mode), case.mode.as_deref(), "{}", case.name);
            assert_eq!(
                bootstrap.hub_daemon_url, case.hub_daemon_url,
                "{}",
                case.name
            );
            assert_eq!(bootstrap.hub_cert, case.hub_cert, "{}", case.name);
        }
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
