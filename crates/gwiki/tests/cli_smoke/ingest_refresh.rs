use super::*;

#[test]
fn read_returns_scoped_wiki_document_contract() {
    let fixture = common::GwikiFixture::new();

    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    common::assert_success(&init, "init");

    let vault = fixture.topic_vault("rust");
    let ownership_path = vault.join("knowledge/topics/ownership.md");
    std::fs::write(
        &ownership_path,
        "# Ownership\n\nOwnership evidence stays scoped.\n",
    )
    .expect("write ownership page");
    std::fs::write(
        vault.join("knowledge/topics/shared.md"),
        "# Shared\n\nTopic page.\n",
    )
    .expect("write shared topic page");
    std::fs::write(
        vault.join("knowledge/concepts/shared.md"),
        "# Shared\n\nConcept page.\n",
    )
    .expect("write shared concept page");

    let by_path = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "read",
            "--path",
            "knowledge/topics/ownership.md",
        ],
    );
    common::assert_success(&by_path, "read by path");
    let by_path_payload = common::json_stdout(&by_path);
    assert_eq!(by_path_payload["command"], "read");
    assert_eq!(by_path_payload["status"], "found");
    assert_eq!(by_path_payload["scope"]["kind"], "topic");
    assert_eq!(by_path_payload["scope"]["id"], "rust");
    assert_eq!(by_path_payload["requested"]["kind"], "path");
    assert_eq!(
        by_path_payload["requested"]["value"],
        "knowledge/topics/ownership.md"
    );
    assert_eq!(
        by_path_payload["wiki_path"],
        "knowledge/topics/ownership.md"
    );
    assert_json_path(&by_path_payload["absolute_path"], &ownership_path);
    assert_eq!(by_path_payload["title"], "Ownership");
    assert_eq!(by_path_payload["content_format"], "markdown");
    assert!(
        by_path_payload["content"]
            .as_str()
            .is_some_and(|content| content.contains("Ownership evidence")),
        "{by_path_payload:#}"
    );
    assert!(
        by_path_payload["degradations"]
            .as_array()
            .is_some_and(Vec::is_empty)
    );

    let by_title = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "read",
            "--title",
            "Ownership",
        ],
    );
    common::assert_success(&by_title, "read by title");
    let by_title_payload = common::json_stdout(&by_title);
    assert_eq!(by_title_payload["status"], "found");
    assert_eq!(by_title_payload["requested"]["kind"], "title");
    assert_eq!(
        by_title_payload["wiki_path"],
        "knowledge/topics/ownership.md"
    );

    let missing = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "read",
            "--path",
            "knowledge/topics/missing.md",
        ],
    );
    common::assert_success(&missing, "read missing");
    let missing_payload = common::json_stdout(&missing);
    assert_eq!(missing_payload["status"], "not_found");
    assert_eq!(missing_payload["wiki_path"], "knowledge/topics/missing.md");
    assert_eq!(missing_payload["content"], serde_json::Value::Null);
    assert_eq!(missing_payload["degradations"][0]["reason"], "not_found");

    let invalid = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "read",
            "--path",
            "../secret.md",
        ],
    );
    common::assert_success(&invalid, "read invalid");
    let invalid_payload = common::json_stdout(&invalid);
    assert_eq!(invalid_payload["status"], "invalid_request");
    assert_eq!(invalid_payload["wiki_path"], serde_json::Value::Null);
    assert_eq!(invalid_payload["content"], serde_json::Value::Null);
    assert_eq!(
        invalid_payload["degradations"][0]["reason"],
        "invalid_request"
    );

    let ambiguous = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format", "json", "--topic", "rust", "read", "--title", "Shared",
        ],
    );
    common::assert_success(&ambiguous, "read ambiguous");
    let ambiguous_payload = common::json_stdout(&ambiguous);
    assert_eq!(ambiguous_payload["status"], "ambiguous");
    assert_eq!(ambiguous_payload["degradations"][0]["reason"], "ambiguous");
    assert_eq!(
        ambiguous_payload["candidates"]
            .as_array()
            .expect("candidates")
            .len(),
        2
    );
}

#[test]
fn ingest_url_requires_daemon() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("url-daemon");
    let output = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            "http://127.0.0.1:9/source",
        ],
    );
    assert!(
        !output.status.success(),
        "ingest-url succeeded without a daemon"
    );
    assert_daemon_required(&output, "ingest-url");
}

#[test]
fn refresh_help_and_project_scope_use_existing_scope_flags() {
    let fixture = common::GwikiFixture::new();
    let project_marker = common::write_gcode_json(fixture.root());

    let help = gwiki(&fixture, fixture.root(), &["refresh", "--help"]);
    common::assert_success(&help, "refresh help");
    let help_text = String::from_utf8_lossy(&help.stdout);
    assert!(help_text.contains("--id"));
    assert!(help_text.contains("--dry-run"));
    assert!(help_text.contains("--project"));
    assert!(help_text.contains("--topic"));
    assert!(!help_text.contains("--scope"));

    let init = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "init", "--project"],
    );
    common::assert_success(&init, "init project");
    let refresh = gwiki(
        &fixture,
        fixture.root(),
        &["--format", "json", "refresh", "--project", "--dry-run"],
    );
    common::assert_success(&refresh, "refresh project dry-run");
    let payload = common::json_stdout(&refresh);
    assert_eq!(payload["command"], "refresh");
    assert_eq!(payload["status"], "dry_run");
    assert_eq!(payload["scope"]["kind"], "project");
    common::assert_gcode_json_unchanged(&project_marker);
}
