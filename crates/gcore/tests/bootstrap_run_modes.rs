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
