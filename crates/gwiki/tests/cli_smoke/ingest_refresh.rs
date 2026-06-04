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
    let ownership_path = vault.join("wiki/topics/ownership.md");
    std::fs::write(
        &ownership_path,
        "# Ownership\n\nOwnership evidence stays scoped.\n",
    )
    .expect("write ownership page");
    std::fs::write(
        vault.join("wiki/topics/shared.md"),
        "# Shared\n\nTopic page.\n",
    )
    .expect("write shared topic page");
    std::fs::write(
        vault.join("wiki/concepts/shared.md"),
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
            "wiki/topics/ownership.md",
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
        "wiki/topics/ownership.md"
    );
    assert_eq!(by_path_payload["wiki_path"], "wiki/topics/ownership.md");
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
    assert_eq!(by_title_payload["wiki_path"], "wiki/topics/ownership.md");

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
            "wiki/topics/missing.md",
        ],
    );
    common::assert_success(&missing, "read missing");
    let missing_payload = common::json_stdout(&missing);
    assert_eq!(missing_payload["status"], "not_found");
    assert_eq!(missing_payload["wiki_path"], "wiki/topics/missing.md");
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
fn ingest_url_json_reports_partial_success() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("url-partial");

    let (base_url, server) = serve_http_responses(vec![
        (
            "200 OK",
            "<!doctype html><html><head><title>URL Good</title></head><body><p>URL body.</p></body></html>",
        ),
        ("500 Internal Server Error", "fixture failure"),
    ]);
    let accepted_url = format!("{base_url}/accepted");
    let failed_url = format!("{base_url}/failed");
    let output = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            &accepted_url,
            &failed_url,
        ],
    );
    server.join().expect("HTTP fixture completed");
    common::assert_success(&output, "ingest-url partial");

    let payload = common::json_stdout(&output);
    assert_eq!(payload["command"], "ingest-url");
    assert_eq!(payload["status"], "partial");
    assert_eq!(payload["accepted"].as_array().expect("accepted").len(), 1);
    assert_eq!(payload["failed"].as_array().expect("failed").len(), 1);
    assert_eq!(
        payload["accepted"][0]["requested_url"].as_str(),
        Some(accepted_url.as_str())
    );
    assert_eq!(
        payload["accepted"][0]["final_url"].as_str(),
        Some(accepted_url.as_str())
    );
    assert!(payload["accepted"][0]["raw_path"].as_str().is_some());
    assert_eq!(payload["accepted"][0]["source"]["kind"], "url");
    assert_eq!(
        payload["failed"][0]["url"].as_str(),
        Some(failed_url.as_str())
    );
    assert_eq!(payload["failed"][0]["code"], "http_status");
    assert!(
        payload["indexed"]["documents"]
            .as_u64()
            .is_some_and(|count| count >= 1),
        "{payload:#}"
    );
}

#[test]
fn ingest_url_json_reports_all_failed_with_nonzero_exit() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("url-failed");

    let (base_url, server) =
        serve_http_responses(vec![("500 Internal Server Error", "fixture failure")]);
    let failed_url = format!("{base_url}/failed");
    let output = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            &failed_url,
        ],
    );
    server.join().expect("HTTP fixture completed");

    assert!(
        !output.status.success(),
        "ingest-url all-failed succeeded unexpectedly\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    let payload = common::json_stdout(&output);
    assert_eq!(payload["command"], "ingest-url");
    assert_eq!(payload["status"], "failed");
    assert_eq!(payload["accepted"].as_array().expect("accepted").len(), 0);
    assert_eq!(payload["failed"].as_array().expect("failed").len(), 1);
    assert_eq!(
        payload["failed"][0]["url"].as_str(),
        Some(failed_url.as_str())
    );
    assert_eq!(payload["failed"][0]["code"], "http_status");
    assert_eq!(payload["indexed"]["documents"].as_u64(), Some(0));
}

#[test]
fn refresh_url_json_reports_changed_and_reindexes_once() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("refresh-changed");

    let (base_url, server) = serve_http_responses(vec![
        (
            "200 OK",
            "<!doctype html><html><head><title>Old</title></head><body><p>old body.</p></body></html>",
        ),
        (
            "200 OK",
            "<!doctype html><html><head><title>New</title></head><body><p>new body.</p></body></html>",
        ),
    ]);
    let url = format!("{base_url}/source");
    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            &url,
        ],
    );
    common::assert_success(&ingest, "ingest-url initial");
    let ingest_payload = common::json_stdout(&ingest);
    let old_id = ingest_payload["accepted"][0]["source"]["id"]
        .as_str()
        .expect("old source id")
        .to_string();
    let old_raw_path = ingest_payload["accepted"][0]["raw_path"]
        .as_str()
        .expect("old raw path")
        .to_string();

    let refresh = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "refresh",
            "--topic",
            &topic.name,
            "--id",
            &old_id,
        ],
    );
    server.join().expect("HTTP fixture completed");
    common::assert_success(&refresh, "refresh changed");
    let payload = common::json_stdout(&refresh);
    assert_eq!(payload["command"], "refresh");
    assert_eq!(payload["status"], "refreshed");
    assert_eq!(payload["refreshed"].as_array().expect("refreshed").len(), 1);
    assert_eq!(payload["refreshed"][0]["old_id"], old_id);
    let new_id = payload["refreshed"][0]["new_id"]
        .as_str()
        .expect("new source id");
    assert_ne!(new_id, old_id);
    assert_eq!(payload["refreshed"][0]["previous_raw_path"], old_raw_path);
    assert!(
        payload["indexed"]["documents"]
            .as_u64()
            .is_some_and(|count| count >= 1),
        "{payload:#}"
    );
    assert_eq!(payload["index_status"]["index_required"], false);

    let manifest = SourceManifest::read(&topic.vault).expect("read source manifest");
    let matching = manifest
        .entries
        .iter()
        .filter(|entry| entry.canonical_location == url)
        .collect::<Vec<_>>();
    assert_eq!(matching.len(), 1);
    assert_eq!(matching[0].id, new_id);
    assert!(!topic.vault.join(old_raw_path).exists());
    assert!(topic.vault.join(format!("raw/{new_id}.md")).exists());
}

#[test]
fn refresh_url_json_reports_unchanged_without_indexing() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("refresh-unchanged");

    let body = "<!doctype html><html><head><title>Same</title></head><body><p>same body.</p></body></html>";
    let (base_url, server) = serve_http_responses(vec![("200 OK", body), ("200 OK", body)]);
    let url = format!("{base_url}/source");
    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            &url,
        ],
    );
    common::assert_success(&ingest, "ingest-url initial");
    let source_id = common::json_stdout(&ingest)["accepted"][0]["source"]["id"]
        .as_str()
        .expect("source id")
        .to_string();

    let refresh = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "refresh",
            "--topic",
            &topic.name,
            "--id",
            &source_id,
        ],
    );
    server.join().expect("HTTP fixture completed");
    common::assert_success(&refresh, "refresh unchanged");
    let payload = common::json_stdout(&refresh);
    assert_eq!(payload["status"], "unchanged");
    assert_eq!(payload["unchanged"][0]["id"], source_id);
    assert_eq!(payload["index_status"]["status"], "not_run");
    assert_eq!(payload["index_status"]["index_required"], false);
}

#[test]
fn refresh_ingest_file_json_replays_local_dry_run_and_unchanged() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("refresh-local");

    let source = fixture.root().join("local-source.md");
    fs::write(&source, "# Local\n\nsame body\n").expect("write local source");
    let source_arg = source.to_str().expect("source path utf8");
    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            &topic.name,
            "--no-ai",
            source_arg,
        ],
    );
    common::assert_success(&ingest, "ingest-file initial");
    let source_id = common::json_stdout(&ingest)["source"]["id"]
        .as_str()
        .expect("source id")
        .to_string();

    let dry_run = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "refresh",
            "--topic",
            &topic.name,
            "--id",
            &source_id,
            "--dry-run",
        ],
    );
    common::assert_success(&dry_run, "refresh local dry-run");
    let dry_payload = common::json_stdout(&dry_run);
    assert_eq!(dry_payload["status"], "dry_run");
    assert_eq!(dry_payload["planned"][0]["id"], source_id);
    assert_eq!(dry_payload["planned"][0]["replay_kind"], "local_file");

    let refresh = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "refresh",
            "--topic",
            &topic.name,
            "--id",
            &source_id,
        ],
    );
    common::assert_success(&refresh, "refresh local unchanged");
    let payload = common::json_stdout(&refresh);
    assert_eq!(payload["status"], "unchanged");
    assert_eq!(payload["unchanged"][0]["id"], source_id);
    assert_eq!(payload["unchanged"][0]["replay_kind"], "local_file");
    assert_eq!(payload["index_status"]["status"], "not_run");
}

#[test]
fn refresh_explicit_all_failed_preserves_json_stdout() {
    let fixture = common::GwikiFixture::new();
    let topic = fixture.init_topic("refresh-failed");

    let (base_url, server) = serve_http_responses(vec![
        (
            "200 OK",
            "<!doctype html><html><head><title>Seed</title></head><body><p>seed body.</p></body></html>",
        ),
        ("500 Internal Server Error", "fixture failure"),
    ]);
    let url = format!("{base_url}/source");
    let ingest = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic.name,
            &url,
        ],
    );
    common::assert_success(&ingest, "ingest-url initial");
    let source_id = common::json_stdout(&ingest)["accepted"][0]["source"]["id"]
        .as_str()
        .expect("source id")
        .to_string();

    let refresh = gwiki(
        &fixture,
        fixture.root(),
        &[
            "--format",
            "json",
            "refresh",
            "--topic",
            &topic.name,
            "--id",
            &source_id,
        ],
    );
    server.join().expect("HTTP fixture completed");
    assert!(
        !refresh.status.success(),
        "refresh all-failed succeeded unexpectedly\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&refresh.stdout),
        String::from_utf8_lossy(&refresh.stderr)
    );
    let payload = common::json_stdout(&refresh);
    assert_eq!(payload["command"], "refresh");
    assert_eq!(payload["status"], "failed");
    assert_eq!(payload["failed"][0]["id"], source_id);
    assert_eq!(payload["failed"][0]["code"], "http_status");
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
