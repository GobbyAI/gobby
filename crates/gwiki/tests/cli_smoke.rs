use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::Path;
use std::process::Output;
use std::thread;
use std::time::{Duration, Instant};

mod common;

use gobby_wiki::session::{AcceptedResearchNote, DaemonDispatch, ResearchScope, ResearchSession};
use gobby_wiki::sources::{
    CompileStatus, IngestionMethod, SourceDraft, SourceKind, SourceManifest,
};

fn gwiki(hub: &Path, cwd: &Path, args: &[&str]) -> Output {
    common::gwiki_command()
        .args(args)
        .env("GOBBY_WIKI_HUB", hub)
        .env("HOME", cwd.join("home"))
        .env_remove("GWIKI_DATABASE_URL")
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .current_dir(cwd)
        .output()
        .expect("gwiki binary runs")
}

fn gwiki_with_database_url(hub: &Path, cwd: &Path, database_url: &str, args: &[&str]) -> Output {
    common::gwiki_command()
        .args(args)
        .env("GOBBY_WIKI_HUB", hub)
        .env("HOME", cwd.join("home"))
        .env("GWIKI_DATABASE_URL", database_url)
        .env_remove("GOBBY_POSTGRES_DSN")
        .env_remove("GCODE_DATABASE_URL")
        .current_dir(cwd)
        .output()
        .expect("gwiki binary runs")
}

fn assert_success(output: &Output, label: &str) {
    assert!(
        output.status.success(),
        "{label} failed\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

fn json_output(output: &Output) -> serde_json::Value {
    serde_json::from_slice(&output.stdout).expect("stdout is JSON")
}

fn assert_json_path(value: &serde_json::Value, expected: &Path) {
    assert_eq!(
        value.as_str(),
        Some(expected.to_str().expect("path utf8")),
        "{value:#}"
    );
}

fn unique_topic(label: &str) -> String {
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("system time after epoch")
        .as_nanos();
    format!("{label}-{}-{nanos}", std::process::id())
}

fn serve_http_responses(
    responses: Vec<(&'static str, &'static str)>,
) -> (String, thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").expect("bind HTTP fixture");
    listener
        .set_nonblocking(true)
        .expect("configure HTTP fixture timeout");
    let base_url = format!("http://{}", listener.local_addr().expect("local addr"));
    let handle = thread::spawn(move || {
        for (status, body) in responses {
            let deadline = Instant::now() + Duration::from_secs(5);
            let (mut stream, _) = loop {
                match listener.accept() {
                    Ok(accepted) => break accepted,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        assert!(
                            Instant::now() < deadline,
                            "timed out waiting for HTTP fixture request"
                        );
                        thread::sleep(Duration::from_millis(10));
                    }
                    Err(error) => panic!("accept HTTP fixture request: {error}"),
                }
            };
            let mut buffer = [0_u8; 1024];
            let _ = stream.read(&mut buffer);
            let response = format!(
                "HTTP/1.1 {status}\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write HTTP fixture response");
        }
    });
    (base_url, handle)
}

fn seed_accepted_research_checkpoint(vault: &Path) {
    let note_path = vault.join("raw/research/ownership-evidence.md");
    fs::create_dir_all(note_path.parent().expect("note parent")).expect("create research dir");
    fs::write(
        &note_path,
        r#"---
title: Ownership evidence
indexable: true
---

Ownership evidence is grounded in accepted research notes.
citation: Rust Reference, Ownership
"#,
    )
    .expect("write accepted research note");
    SourceManifest::register(
        vault,
        SourceDraft {
            location: "raw/research/ownership-evidence.md".to_string(),
            kind: SourceKind::ResearchNote,
            fetched_at: "2026-05-30T00:00:00Z".to_string(),
            content: b"Ownership evidence is grounded in accepted research notes.".to_vec(),
            title: Some("Ownership evidence".to_string()),
            citation: Some("Rust Reference, Ownership".to_string()),
            license: None,
            ingestion_method: IngestionMethod::Research,
            compile_status: CompileStatus::Pending,
        },
    )
    .expect("register research source");

    let mut session = ResearchSession::new(
        "How should ownership evidence compile?",
        ResearchScope::topic("rust", vault),
        Vec::new(),
        1,
        Some("#306".to_string()),
    )
    .expect("research session");
    session.accepted_notes.push(AcceptedResearchNote {
        title: "Ownership evidence".to_string(),
        path: note_path,
    });
    session.save_checkpoint().expect("save checkpoint");
}

#[test]
fn command_modules_do_not_define_static_placeholder_results() {
    let commands_dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("src/commands");
    let placeholder_patterns = [
        "\"objects\": []",
        "\"created\": []",
        "\"results\": []",
        "\"backlinks\": []",
        "\"suggestions\": []",
        "\"documents\": 0",
        "\"chunks\": 0",
        "\"links\": 0",
        "Init ready",
        "Setup ready",
        "Index ready",
        "Ingest file ready",
    ];

    for entry in fs::read_dir(commands_dir).expect("read commands dir") {
        let path = entry.expect("read command entry").path();
        if path.extension().and_then(|extension| extension.to_str()) != Some("rs") {
            continue;
        }

        let source = fs::read_to_string(&path).expect("read command source");
        for pattern in placeholder_patterns {
            assert!(
                !source.contains(pattern),
                "{} still contains placeholder pattern {pattern:?}",
                path.display()
            );
        }
    }
}

#[test]
fn configured_index_uses_postgres_writer_when_database_url_is_set() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let topic = unique_topic("pg-writer-contract");
    let invalid_database_url = "postgresql://127.0.0.1:1/gwiki";

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", &topic],
    );
    assert_success(&init, "init");

    let vault = hub.join("topics").join(&topic);
    fs::create_dir_all(vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        vault.join("wiki/topics/durable-search.md"),
        "# Durable Search\n\nConfigured indexing must use PostgreSQL.\n",
    )
    .expect("write topic page");

    let index = gwiki_with_database_url(
        &hub,
        tmp.path(),
        invalid_database_url,
        &["--format", "json", "index", "--topic", &topic],
    );
    assert!(
        !index.status.success(),
        "configured index unexpectedly succeeded\nstdout:\n{}\nstderr:\n{}",
        String::from_utf8_lossy(&index.stdout),
        String::from_utf8_lossy(&index.stderr)
    );
    assert!(
        String::from_utf8_lossy(&index.stderr)
            .contains("failed to connect to PostgreSQL for gwiki index"),
        "stderr:\n{}",
        String::from_utf8_lossy(&index.stderr)
    );
}

#[test]
fn configured_postgres_index_feeds_configured_search_when_test_database_is_available() {
    let Some(database_url) = std::env::var("GWIKI_POSTGRES_TEST_DATABASE_URL")
        .ok()
        .or_else(|| std::env::var("GCODE_POSTGRES_TEST_DATABASE_URL").ok())
    else {
        eprintln!(
            "skipping configured_postgres_index_feeds_configured_search_when_test_database_is_available: GWIKI_POSTGRES_TEST_DATABASE_URL/GCODE_POSTGRES_TEST_DATABASE_URL is not set"
        );
        return;
    };

    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let topic = unique_topic("pg-index-search");
    let _cleanup = PostgresTopicCleanup::new(database_url.clone(), topic.clone());

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", &topic],
    );
    assert_success(&init, "init");

    let setup = gwiki_with_database_url(
        &hub,
        tmp.path(),
        &database_url,
        &[
            "--format",
            "json",
            "setup",
            "--standalone",
            "--no-services",
            "--database-url",
            &database_url,
            "--topic",
            &topic,
        ],
    );
    assert_success(&setup, "setup");

    let vault = hub.join("topics").join(&topic);
    fs::create_dir_all(vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        vault.join("wiki/topics/durable-search.md"),
        "# Durable Search\n\nDurable bm25needle content is searchable after indexing.\n",
    )
    .expect("write topic page");

    let index = gwiki_with_database_url(
        &hub,
        tmp.path(),
        &database_url,
        &["--format", "json", "index", "--topic", &topic],
    );
    assert_success(&index, "index");

    let search = gwiki_with_database_url(
        &hub,
        tmp.path(),
        &database_url,
        &[
            "--format",
            "json",
            "search",
            "--topic",
            &topic,
            "bm25needle",
            "--limit",
            "3",
        ],
    );
    assert_success(&search, "search");
    let search_payload = json_output(&search);
    assert!(
        search_payload["results"].as_array().is_some_and(|results| {
            results.iter().any(|result| {
                result["wiki_page"] == "wiki/topics/durable-search.md"
                    && result["sources"]
                        .as_array()
                        .is_some_and(|sources| sources.iter().any(|source| source == "bm25"))
            })
        }),
        "{search_payload:#}"
    );
}

struct PostgresTopicCleanup {
    database_url: String,
    topic: String,
}

impl PostgresTopicCleanup {
    fn new(database_url: String, topic: String) -> Self {
        Self {
            database_url,
            topic,
        }
    }
}

impl Drop for PostgresTopicCleanup {
    fn drop(&mut self) {
        let _ = cleanup_postgres_topic(&self.database_url, &self.topic);
    }
}

fn cleanup_postgres_topic(database_url: &str, topic: &str) -> anyhow::Result<()> {
    let mut client = gobby_core::postgres::connect_readwrite(database_url)?;
    let mut tx = client.transaction()?;
    tx.execute(
        "DELETE FROM gwiki_ingestions WHERE scope_kind = 'topic' AND scope_id = $1",
        &[&topic],
    )?;
    tx.execute(
        "DELETE FROM gwiki_links WHERE scope_kind = 'topic' AND scope_id = $1",
        &[&topic],
    )?;
    tx.execute(
        "DELETE FROM gwiki_sources WHERE scope_kind = 'topic' AND scope_id = $1",
        &[&topic],
    )?;
    tx.execute(
        "DELETE FROM gwiki_chunks WHERE scope_kind = 'topic' AND scope_id = $1",
        &[&topic],
    )?;
    tx.execute(
        "DELETE FROM gwiki_documents WHERE scope_kind = 'topic' AND scope_id = $1",
        &[&topic],
    )?;
    tx.commit()?;
    Ok(())
}

#[test]
fn read_returns_scoped_wiki_document_contract() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    assert_success(&init, "init");

    let vault = hub.join("topics").join("rust");
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
        &hub,
        tmp.path(),
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
    assert_success(&by_path, "read by path");
    let by_path_payload = json_output(&by_path);
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
        &hub,
        tmp.path(),
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
    assert_success(&by_title, "read by title");
    let by_title_payload = json_output(&by_title);
    assert_eq!(by_title_payload["status"], "found");
    assert_eq!(by_title_payload["requested"]["kind"], "title");
    assert_eq!(by_title_payload["wiki_path"], "wiki/topics/ownership.md");

    let missing = gwiki(
        &hub,
        tmp.path(),
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
    assert_success(&missing, "read missing");
    let missing_payload = json_output(&missing);
    assert_eq!(missing_payload["status"], "not_found");
    assert_eq!(missing_payload["wiki_path"], "wiki/topics/missing.md");
    assert_eq!(missing_payload["content"], serde_json::Value::Null);
    assert_eq!(missing_payload["degradations"][0]["reason"], "not_found");

    let invalid = gwiki(
        &hub,
        tmp.path(),
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
    assert_success(&invalid, "read invalid");
    let invalid_payload = json_output(&invalid);
    assert_eq!(invalid_payload["status"], "invalid_request");
    assert_eq!(invalid_payload["wiki_path"], serde_json::Value::Null);
    assert_eq!(invalid_payload["content"], serde_json::Value::Null);
    assert_eq!(
        invalid_payload["degradations"][0]["reason"],
        "invalid_request"
    );

    let ambiguous = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format", "json", "--topic", "rust", "read", "--title", "Shared",
        ],
    );
    assert_success(&ambiguous, "read ambiguous");
    let ambiguous_payload = json_output(&ambiguous);
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
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let topic = unique_topic("url-partial");
    let init = gwiki(&hub, tmp.path(), &["init", "--topic", &topic]);
    assert_success(&init, "init topic");

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
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic,
            &accepted_url,
            &failed_url,
        ],
    );
    server.join().expect("HTTP fixture completed");
    assert_success(&output, "ingest-url partial");

    let payload = json_output(&output);
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
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let topic = unique_topic("url-failed");
    let init = gwiki(&hub, tmp.path(), &["init", "--topic", &topic]);
    assert_success(&init, "init topic");

    let (base_url, server) =
        serve_http_responses(vec![("500 Internal Server Error", "fixture failure")]);
    let failed_url = format!("{base_url}/failed");
    let output = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "ingest-url",
            "--topic",
            &topic,
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
    let payload = json_output(&output);
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
fn public_cli_smoke_uses_gwiki_modules() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");
    let source = tmp.path().join("ownership-source.md");
    fs::write(
        &source,
        "# Ownership Source\n\nOwnership evidence for Rust borrowing.\n",
    )
    .expect("write source");

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    assert_success(&init, "init");

    let setup = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "setup", "--topic", "rust"],
    );
    assert_success(&setup, "setup");
    let setup_payload = json_output(&setup);
    assert!(
        setup_payload["objects"]
            .as_array()
            .is_some_and(|objects| !objects.is_empty()),
        "{setup_payload:#}"
    );

    let vault = hub.join("topics").join("rust");
    fs::create_dir_all(vault.join("wiki/topics")).expect("create topic dir");
    fs::write(
        vault.join("wiki/topics/ownership.md"),
        "# Ownership\n\nOwnership explains borrowing.\n",
    )
    .expect("write ownership page");
    fs::write(
        vault.join("wiki/topics/rust.md"),
        "# Rust\n\nRust links to [[Ownership]]. Missing [[Borrow checker]].\n",
    )
    .expect("write rust page");

    let ingest = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "ingest-file",
            "--topic",
            "rust",
            source.to_str().expect("source path utf8"),
        ],
    );
    assert_success(&ingest, "ingest-file");
    let ingest_payload = json_output(&ingest);
    assert_eq!(ingest_payload["command"], "ingest-file");
    assert!(ingest_payload["raw_path"].as_str().is_some());

    let index = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "index", "--topic", "rust"],
    );
    assert_success(&index, "index");
    let index_payload = json_output(&index);
    assert!(
        index_payload["indexed"]["documents"]
            .as_u64()
            .is_some_and(|count| count >= 2),
        "{index_payload:#}"
    );
    assert!(
        index_payload["indexed"]["chunks"]
            .as_u64()
            .is_some_and(|count| count >= 2),
        "{index_payload:#}"
    );

    let search = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "search",
            "--topic",
            "rust",
            "ownership",
            "--limit",
            "3",
        ],
    );
    assert_success(&search, "search");
    let search_payload = json_output(&search);
    assert!(
        search_payload["results"]
            .as_array()
            .is_some_and(|results| !results.is_empty()),
        "{search_payload:#}"
    );
    let first_result = &search_payload["results"][0];
    assert!(
        first_result["sources"]
            .as_array()
            .is_some_and(|sources| sources.iter().any(|source| source == "bm25")),
        "{search_payload:#}"
    );
    assert!(
        first_result["explanations"]
            .as_array()
            .is_some_and(|explanations| !explanations.is_empty()),
        "{search_payload:#}"
    );

    let backlinks = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "backlinks",
            "--topic",
            "rust",
            "wiki/topics/ownership.md",
        ],
    );
    assert_success(&backlinks, "backlinks");
    let backlinks_payload = json_output(&backlinks);
    assert!(
        backlinks_payload["backlinks"]
            .as_array()
            .is_some_and(|links| !links.is_empty()),
        "{backlinks_payload:#}"
    );

    let suggestions = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "link-suggest",
            "--topic",
            "rust",
            "--limit",
            "3",
        ],
    );
    assert_success(&suggestions, "link-suggest");
    let suggestions_payload = json_output(&suggestions);
    assert!(
        suggestions_payload["suggestions"]
            .as_array()
            .is_some_and(|suggestions| !suggestions.is_empty()),
        "{suggestions_payload:#}"
    );

    seed_accepted_research_checkpoint(&vault);

    let research = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format", "json", "--topic", "rust", "research", "--resume",
        ],
    );
    assert_success(&research, "research");
    let research_payload = json_output(&research);
    assert_eq!(research_payload["command"], "research");
    assert_json_path(&research_payload["session"]["scope"]["root"], &vault);

    let compile = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format",
            "json",
            "--topic",
            "rust",
            "compile",
            "--outline",
            "Overview",
            "--target",
            "wiki/topics/ownership-synthesis.md",
        ],
    );
    assert_success(&compile, "compile");
    let compile_payload = json_output(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert_json_path(
        &compile_payload["article_path"],
        &vault.join("wiki/topics/ownership-synthesis.md"),
    );
    assert!(vault.join("wiki/sources/ownership-evidence.md").is_file());

    let audit = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "--topic", "rust", "audit"],
    );
    assert_success(&audit, "audit");
    let audit_payload = json_output(&audit);
    assert_eq!(audit_payload["command"], "audit");
    assert_json_path(&audit_payload["root"], &vault);
}

#[test]
fn public_cli_smoke_continues_research_compile_audit_in_topic_scope() {
    let tmp = tempfile::tempdir().expect("tempdir");
    let hub = tmp.path().join("hub");

    let init = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "init", "--topic", "rust"],
    );
    assert_success(&init, "init");

    let vault = hub.join("topics").join("rust");
    let note_path = vault.join("raw/research/session-scope.md");
    fs::create_dir_all(note_path.parent().expect("note parent")).expect("create research dir");
    fs::write(
        &note_path,
        "---\ntitle: Session scope\nindexable: true\n---\n\ncitation: Gobby wiki scope resolver\nTopic research should use the configured hub.\n",
    )
    .expect("write research note");

    let mut session = ResearchSession::new(
        "How should gwiki resolve topic research scope?",
        ResearchScope::topic("rust", &vault),
        vec!["Use local smoke fixture".to_string()],
        1,
        Some("#300".to_string()),
    )
    .expect("research session");
    session.dispatch = Some(DaemonDispatch {
        dispatch_id: "dispatch-smoke".to_string(),
        daemon_base_url: "http://daemon.test".to_string(),
        agent_run_ids: vec!["run-smoke".to_string()],
    });
    session.accepted_notes.push(AcceptedResearchNote {
        title: "Session scope".to_string(),
        path: note_path,
    });
    session.save_checkpoint().expect("save checkpoint");

    let research = gwiki(
        &hub,
        tmp.path(),
        &[
            "--format", "json", "--topic", "rust", "research", "--resume",
        ],
    );
    assert_success(&research, "research");
    let research_payload = json_output(&research);
    assert_eq!(research_payload["command"], "research");
    assert_eq!(research_payload["scope"]["kind"], "topic");
    assert_eq!(research_payload["scope"]["id"], "rust");
    assert!(vault.join(".gwiki/session-events.jsonl").exists());

    let compile = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "--topic", "rust", "compile"],
    );
    assert_success(&compile, "compile");
    let compile_payload = json_output(&compile);
    assert_eq!(compile_payload["command"], "compile");
    assert!(
        compile_payload["article_path"]
            .as_str()
            .is_some_and(|path| path.ends_with("wiki/topics/rust.md")),
        "{compile_payload:#}"
    );

    let audit = gwiki(
        &hub,
        tmp.path(),
        &["--format", "json", "--topic", "rust", "audit"],
    );
    assert_success(&audit, "audit");
    let audit_payload = json_output(&audit);
    assert_eq!(audit_payload["command"], "audit");
    assert_eq!(audit_payload["scope"]["kind"], "topic");
    assert_eq!(audit_payload["scope"]["id"], "rust");
}
