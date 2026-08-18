use super::*;

#[test]
fn init_output_is_not_a_static_placeholder() {
    let fixture = common::GwikiFixture::new();
    let topic = common::unique_topic("placeholder-output");
    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--topic", &topic],
    );
    common::assert_success(&init, "init");
    let init_json = serde_json::to_string_pretty(&common::json_stdout(&init)).expect("pretty init");
    assert!(
        !init_json.contains("\"created\": []"),
        "init output still contains placeholder pattern:\n{init_json}"
    );
    let text_init = gwiki(&fixture, fixture.root(), &["init", "--topic", &topic]);
    common::assert_success(&text_init, "text init");
    let text = String::from_utf8_lossy(&text_init.stdout);
    assert!(
        !text.contains("Init ready"),
        "text init still contains placeholder pattern:\n{text}"
    );
}

#[test]
fn datastore_commands_require_daemon() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("daemon-required");
    let source = fixture.root().join("source.md");
    fs::write(&source, "# Source\n\nbody\n").expect("write source");
    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            &topic.name,
            source.to_str().expect("utf8"),
        ],
    );
    assert!(
        !ingest.status.success(),
        "ingest-file succeeded without a daemon"
    );
    assert_daemon_required(&ingest, "ingest-file");
    let index = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "index", "--topic", &topic.name],
    );
    assert!(!index.status.success(), "index succeeded without a daemon");
    assert_daemon_required(&index, "index");
    let search = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "search",
            "--topic",
            &topic.name,
            "needle",
        ],
    );
    assert!(
        !search.status.success(),
        "search succeeded without a daemon"
    );
    assert_daemon_required(&search, "search");
}

#[test]
fn grant_without_daemon_is_config_error() {
    let fixture = common::GwikiFixture::new();
    common::write_gcode_json(fixture.project());
    let topic = fixture.init_topic("grant-without-daemon");

    let index = gwiki_with_database_url(
        &fixture,
        fixture.project(),
        "postgresql://127.0.0.1:1/gwiki",
        &["--format", "json", "index", "--topic", &topic.name],
    );
    assert!(
        !index.status.success(),
        "grant-backed index unexpectedly succeeded\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&index.stdout),
        String::from_utf8_lossy(&index.stderr)
    );
    let stderr = String::from_utf8_lossy(&index.stderr);
    assert!(
        stderr.contains("config_error"),
        "grant without daemon should fail as config_error, not a DSN connect\nstderr:\n{stderr}"
    );
}
